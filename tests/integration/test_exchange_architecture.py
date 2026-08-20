from __future__ import annotations

import asyncio
import base64
import urllib.parse
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from workflow_builder import ActorSpec, WorkflowFixture

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    AddCandidateFacts,
    CapabilityStatus,
    Direction,
    ExecutionEnvironment,
    IntentKind,
    Role,
    TargetCandidate,
    TargetUrgency,
)
from trading_control_plane.execution_runtime import (
    AUTOMATIC_EXECUTION_OWNER,
    AutomaticExecutionWorker,
)
from trading_control_plane.freqtrade import (
    FreqtradeRpcMessage,
    FreqtradeWorkerClient,
    FreqtradeWorkerSpec,
    parse_freqtrade_trade,
)
from trading_control_plane.models import (
    CapabilityGate,
    ExchangeAccount,
    OrderIntent,
    ProtectionOrder,
    VenueOrder,
)
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.queries import TradingQueries
from trading_control_plane.runtime_contracts import ConnectionProbeResult
from trading_control_plane.service import TradingService

NOW = datetime.now(UTC).replace(microsecond=0)


def _credential_key() -> str:
    return base64.urlsafe_b64encode(b"architecture-contract-key-32-byte"[:32]).decode().rstrip("=")


def _perptape() -> PerptapeClient:
    return PerptapeClient(
        base_url="https://perptape.com",
        api_key=None,
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
    )


def _settings(database: Database, *, workers: bool = False) -> Settings:
    return Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="architecture-contract-session-secret",  # noqa: S106
        credential_encryption_key=_credential_key(),
        public_base_url="http://test",
        freqtrade_workers_enabled=workers,
        _env_file=None,
    )


def test_openapi_contains_only_account_facts_and_backend_neutral_execution(
    database: Database,
) -> None:
    app = create_app(_settings(database), database, _perptape())
    paths = set(app.openapi()["paths"])

    assert "/api/exchange-accounts/{exchange_account_id}/facts" in paths
    assert "/api/exchange-accounts/{exchange_account_id}/fact-health" in paths
    assert "/api/intents/{intent_id}/execute" in paths
    assert not any(path.startswith("/api/venues/") for path in paths)
    assert not any(
        marker in path
        for path in paths
        for marker in (
            "/binance/testnet",
            "/binance/live",
            "/hyperliquid/testnet",
            "/hyperliquid/live",
        )
    )


class _WorkerFixture:
    def __init__(
        self,
        *,
        exchange: str,
        pair: str,
        dry_run: bool,
        quantity: Decimal,
        mark: Decimal,
        entry_fill: Decimal | None = None,
    ) -> None:
        self.exchange = exchange
        self.pair = pair
        self.dry_run = dry_run
        self.expected_initial_quantity = quantity
        self.entry_fill = entry_fill
        self.mark = mark
        self.enter_tag: str | None = None
        self.side = "long"
        self.open_quantity = Decimal(0)
        self.last_closed_quantity = Decimal(0)
        self.orders: list[dict[str, Any]] = []
        self.is_open = False
        self.stop_sequence = 0
        self.writes = 0

    def _trade(self) -> dict[str, Any]:
        assert self.enter_tag is not None
        reported_quantity = self.open_quantity or self.last_closed_quantity
        stop_id = f"{self.exchange}-stop-{self.stop_sequence}"
        orders = list(self.orders)
        if self.is_open:
            orders.append(
                {
                    "ft_order_side": "stoploss",
                    "order_id": stop_id,
                    "status": "open",
                    "is_open": True,
                    "amount": str(self.open_quantity),
                    "filled": "0",
                    "safe_price": str(self.mark * Decimal("0.95")),
                    "ft_order_tag": None,
                    "order_filled_timestamp": None,
                }
            )
        return {
            "trade_id": f"{self.exchange}-trade-1",
            "pair": self.pair,
            "is_short": self.side == "short",
            "amount": str(reported_quantity),
            "stake_amount": str(reported_quantity * self.mark * Decimal("0.98")),
            "open_rate": str(self.mark),
            "current_rate": str(self.mark),
            "close_rate": None if self.is_open else str(self.mark),
            "is_open": self.is_open,
            "enter_tag": self.enter_tag,
            "leverage": "1",
            "stop_loss_abs": str(self.mark * Decimal("0.95")),
            "stoploss_order_id": stop_id if self.is_open else None,
            "open_timestamp": int(NOW.timestamp() * 1_000),
            "close_timestamp": None if self.is_open else int(datetime.now(UTC).timestamp() * 1_000),
            "orders": orders,
        }

    def __call__(
        self,
        url: str,
        method: str,
        payload: dict[str, Any] | None,
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        del headers, timeout
        path = urllib.parse.urlparse(url).path
        if path.endswith("/ping"):
            return {"status": "pong"}
        if path.endswith("/token/login"):
            return {"access_token": "short-lived-fixture-token"}
        if path.endswith("/show_config"):
            return {
                "exchange": self.exchange,
                "trading_mode": "futures",
                "dry_run": self.dry_run,
                "force_entry_enable": True,
                "position_adjustment_enable": True,
                "state": "running",
            }
        if path.endswith("/version"):
            return {"version": "2026.7"}
        if path.endswith("/whitelist"):
            return {"whitelist": [self.pair]}
        if path.endswith("/status"):
            return [self._trade()] if self.is_open else []
        if path.endswith("/forceenter"):
            assert method == "POST" and payload is not None
            assert payload["pair"] == self.pair
            quantity = (
                Decimal(str(payload["stakeamount"]))
                * Decimal(str(payload["leverage"]))
                / (self.mark * Decimal("0.98"))
            )
            filled = self.entry_fill if self.entry_fill is not None else quantity
            assert Decimal(0) < filled <= quantity
            tag = str(payload["entry_tag"])
            if self.enter_tag is None:
                assert quantity == self.expected_initial_quantity
                self.enter_tag = tag
                self.side = str(payload["side"])
            else:
                assert self.is_open and payload["side"] == self.side
            self.open_quantity += filled
            self.is_open = True
            self.stop_sequence += 1
            self.writes += 1
            self.orders.append(
                {
                    "ft_order_side": "sell" if self.side == "short" else "buy",
                    "order_id": f"{self.exchange}-entry-{self.writes}",
                    "status": "closed",
                    "is_open": False,
                    "amount": str(quantity),
                    "filled": str(filled),
                    "average": str(self.mark),
                    "ft_order_tag": tag,
                    "order_filled_timestamp": int(datetime.now(UTC).timestamp() * 1_000),
                }
            )
            return {"status": "created"}
        if path.endswith("/forceexit"):
            assert method == "POST" and payload is not None and self.is_open
            quantity = Decimal(str(payload.get("amount", self.open_quantity)))
            assert Decimal(0) < quantity <= self.open_quantity
            self.last_closed_quantity = quantity
            self.open_quantity -= quantity
            self.is_open = self.open_quantity > 0
            self.stop_sequence += 1
            self.writes += 1
            self.orders.append(
                {
                    "ft_order_side": "buy" if self.side == "short" else "sell",
                    "order_id": f"{self.exchange}-exit-{self.writes}",
                    "status": "closed",
                    "is_open": False,
                    "amount": str(quantity),
                    "filled": str(quantity),
                    "average": str(self.mark),
                    "ft_order_tag": "force_exit",
                    "order_filled_timestamp": int(datetime.now(UTC).timestamp() * 1_000),
                }
            )
            return {"status": "closed"}
        if f"/trade/{self.exchange}-trade-1" in path:
            return self._trade()
        raise AssertionError((method, path))


@pytest.mark.parametrize(
    (
        "venue",
        "symbol",
        "pair",
        "exchange",
        "environment",
        "direction",
        "hip3_dexes",
        "partial_fill",
    ),
    [
        ("BINANCE", "XRPUSDT", "XRP/USDT:USDT", "binance", "LIVE", "LONG", (), False),
        (
            "HYPERLIQUID",
            "BTC",
            "BTC/USDC:USDC",
            "hyperliquid",
            "LIVE",
            "SHORT",
            (),
            False,
        ),
        (
            "HYPERLIQUID",
            "xyz:TSLA",
            "XYZ-TSLA/USDC:USDC",
            "hyperliquid",
            "LIVE",
            "LONG",
            ("xyz",),
            False,
        ),
        ("OKX", "XRP-USDT-SWAP", "XRP/USDT:USDT", "okx", "LIVE", "LONG", (), False),
        ("BYBIT", "XRPUSDT", "XRP/USDT:USDT", "bybit", "LIVE", "SHORT", (), False),
        ("BINANCE", "XRPUSDT", "XRP/USDT:USDT", "binance", "TESTNET", "LONG", (), True),
    ],
)
def test_exact_account_freqtrade_is_the_only_execution_chain(
    database: Database,
    venue: str,
    symbol: str,
    pair: str,
    exchange: str,
    environment: str,
    direction: str,
    hip3_dexes: tuple[str, ...],
    partial_fill: bool,
) -> None:
    slug = (
        f"architecture-{venue.lower()}-{environment.lower()}-{direction.lower()}-{symbol.lower()}"
    ).replace(":", "-")
    account_id = f"acct-{slug}"
    mark = Decimal("100")
    quantity = Decimal("2") if partial_fill else Decimal("1")
    lifecycle = (venue, environment, direction) == ("BINANCE", "LIVE", "LONG")
    service = TradingService(database, credential_encryption_key=_credential_key())
    fixture = WorkflowFixture.create(
        service,
        now=NOW,
        admin_username=f"{slug}-admin",
        account_id=account_id,
        venue=venue,
        environment=ExecutionEnvironment(environment),
        actors=(
            ActorSpec("proposer", f"{slug}-proposer", Role.PROPOSER),
            ActorSpec("reviewer_one", f"{slug}-reviewer-1", Role.REVIEWER),
            ActorSpec("reviewer_two", f"{slug}-reviewer-2", Role.REVIEWER),
            ActorSpec("operator", f"{slug}-operator", Role.OPERATOR),
        ),
        symbol=symbol,
        tick_size=Decimal("0.01"),
        lot_size=Decimal("1"),
        minimum_notional=Decimal("5"),
        quote_currency="USDC" if venue == "HYPERLIQUID" else "USDT",
        risk_version=f"{slug}-risk-v1",
        max_fact_age=timedelta(minutes=10),
        mark_price=mark,
    )
    account = TradingQueries(database).exchange_accounts(fixture.ids["admin"])["data"][0]
    exchange_account_id = account["exchange_account_id"]
    credentials = (
        {"account_address": "0x1111111111111111111111111111111111111111"}
        if venue == "HYPERLIQUID"
        else {
            "api_key": f"{slug}-key",
            "api_secret": f"{slug}-secret",
            **({"passphrase": f"{slug}-passphrase"} if venue == "OKX" else {}),
        }
    )
    rotated_version = service.rotate_exchange_account_credentials(
        exchange_account_id,
        actor_id=fixture.ids["admin"],
        credentials=credentials,
        expected_version=int(account["version"]),
        idempotency_key=f"{slug}-credentials",
        now=NOW,
    )
    connection_command, replay = service.prepare_exchange_account_connection_verification(
        exchange_account_id,
        actor_id=fixture.ids["admin"],
        expected_version=rotated_version,
        idempotency_key=f"{slug}-connection",
    )
    assert connection_command is not None and replay is None
    verified = service.record_exchange_account_connection_verification(
        connection_command,
        ConnectionProbeResult(True, None),
        actor_id=fixture.ids["admin"],
        idempotency_key=f"{slug}-connection",
        now=NOW,
    )
    runtime = service.configure_exchange_account_runtime_sync(
        exchange_account_id,
        actor_id=fixture.ids["admin"],
        enabled=True,
        expected_version=int(verified["version"]),
        idempotency_key=f"{slug}-runtime",
        now=NOW,
    )
    configured = service.configure_exchange_account_freqtrade_worker(
        exchange_account_id,
        actor_id=fixture.ids["admin"],
        mode="LIVE" if environment == "LIVE" else "DRY_RUN",
        name=f"{slug}-worker",
        base_url="http://127.0.0.1:18091",
        username="control-plane",
        password="worker-fixture-password",  # noqa: S106
        ws_token="worker-rpc-token-fixture-0123456789",  # noqa: S106
        hip3_dexes=hip3_dexes,
        expected_version=int(runtime["version"]),
        idempotency_key=f"{slug}-worker",
        now=NOW,
    )
    binding, replay = service.prepare_exchange_account_freqtrade_verification(
        exchange_account_id,
        actor_id=fixture.ids["admin"],
        expected_version=int(configured["version"]),
        idempotency_key=f"{slug}-worker-verification",
    )
    assert binding is not None and replay is None
    worker_verified = service.record_exchange_account_freqtrade_verification(
        binding,
        actor_id=fixture.ids["admin"],
        error_code=None,
        idempotency_key=f"{slug}-worker-verification",
        now=NOW,
    )
    service.configure_exchange_account_trading(
        exchange_account_id,
        actor_id=fixture.ids["admin"],
        enabled=True,
        expected_version=int(worker_verified["version"]),
        idempotency_key=f"{slug}-trading",
        now=NOW,
    )
    runtime_binding = service.runtime_account_bindings()[0]
    service.record_runtime_source_health(
        runtime_binding.service_principal_id,
        {venue: {"status": "SUCCESS", "items_observed": 1}},
        scopes={venue: (account_id, venue)},
        now=NOW,
    )
    proposal = fixture.approved_proposal(
        key=slug,
        direction=Direction(direction),
        quantity=Decimal(2) if lifecycle else quantity,
        max_risk=Decimal("5"),
        details={
            "invalidation_price": "95",
            "allow_auto_add": lifecycle,
            "requested_adds": 1 if lifecycle else 0,
            "add_trigger_price": "100" if lifecycle else None,
        },
    )
    if lifecycle:
        with database.session_factory.begin() as session:
            auto_add_gate = session.get(CapabilityGate, "AUTO_ADD", with_for_update=True)
            assert auto_add_gate is not None
            auto_add_gate.status = CapabilityStatus.ENABLED.value
            auto_add_gate.reason = "shared Freqtrade lifecycle fixture precondition"
            auto_add_gate.operator_id = str(fixture.ids["admin"])
            auto_add_gate.version += 1
            auto_add_gate.updated_at = NOW
    opening = fixture.opening_order(
        proposal=proposal,
        key=slug,
        quantity=quantity,
        direction=Direction(direction),
        allowed_adds=1 if lifecycle else 0,
    )
    scope = f"{environment}:{account_id}:{venue}"
    owner_id = AUTOMATIC_EXECUTION_OWNER if lifecycle else f"{slug}-sender"
    if environment == "LIVE":
        service.set_capability_gate(
            "LIVE_ORDER_SEND",
            CapabilityStatus.ENABLED,
            "shared Freqtrade architecture contract",
            fixture.ids["admin"],
            now=NOW,
        )

    worker_fixture = _WorkerFixture(
        exchange=exchange,
        pair=pair,
        dry_run=environment == "TESTNET",
        quantity=quantity,
        mark=mark,
        entry_fill=Decimal("1") if partial_fill else None,
    )
    worker = FreqtradeWorkerClient(
        FreqtradeWorkerSpec(
            name=binding.worker_name,
            venue=venue,  # type: ignore[arg-type]
            base_url=binding.worker_url,
            username=binding.username,
            password=binding.password,
            ws_token=binding.ws_token,
            hip3_dexes=hip3_dexes,
            exchange_account_id=str(exchange_account_id),
            team_id=str(binding.team_id),
            account_id=account_id,
        ),
        confirmation_timeout_seconds=10,
        fetcher=worker_fixture,
    )
    if lifecycle:
        automatic_settings = _settings(database, workers=True).model_copy(
            update={"execution_worker_enabled": True}
        )
        automatic_worker = AutomaticExecutionWorker(
            settings=automatic_settings,
            database=database,
            worker_factory=lambda _binding: worker,
            clock=lambda: NOW,
        )
        automatic_report = automatic_worker.run_once()
        assert automatic_report.intents_selected == 1
        assert automatic_report.intents_completed == 1
        assert automatic_report.blocked == {}
        assert worker_fixture.writes == 1
        replay_report = automatic_worker.run_once()
        assert replay_report.intents_selected == 0
        assert worker_fixture.writes == 1
    fencing_token = service.acquire_sender(
        scope,
        owner_id,
        fixture.ids["operator"],
        NOW,
        lease_duration=timedelta(minutes=5),
    )
    app = create_app(
        _settings(database, workers=True),
        database,
        _perptape(),
        freqtrade_workers=(worker,),
    )
    with database.session_factory() as session:
        opening_row = session.get(OrderIntent, opening.intent_id)
        assert opening_row is not None
        authorization_id = opening_row.authorization_id

    async def scenario() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            login = await client.post(
                "/api/auth/mock/login",
                json={"username": f"{slug}-operator"},
            )
            assert login.status_code == 200, login.text
            action = {
                "execution_scope": scope,
                "owner_id": owner_id,
                "fencing_token": fencing_token,
                "idempotency_key": f"{slug}-dispatch",
            }
            if not lifecycle:
                response = await client.post(
                    f"/api/intents/{opening.intent_id}/execute",
                    json=action,
                )
                assert response.status_code == 200, response.text
                assert response.json()["backend"] == "FREQTRADE"
                assert response.json()["environment"] == environment
                assert response.json()["pair"] == pair
                replayed = await client.post(
                    f"/api/intents/{opening.intent_id}/execute",
                    json=action,
                )
                assert replayed.status_code == 200, replayed.text
                assert replayed.json()["replayed"] is True
            if lifecycle:
                fresh = datetime.now(UTC)
                worker_fixture.mark = Decimal(101)
                service.record_position(
                    account_id,
                    venue,
                    fixture.ids["instrument"],
                    Decimal(1),
                    mark,
                    Decimal(101),
                    True,
                    fixture.ids["operator"],
                    environment=ExecutionEnvironment.LIVE,
                    now=fresh,
                )
                add = service.create_order_intent(
                    authorization_id,
                    fixture.ids["operator"],
                    IntentKind.ADD,
                    account_id,
                    venue,
                    fixture.ids["instrument"],
                    Direction.LONG,
                    Decimal(1),
                    f"{slug}-add",
                    add_candidate=AddCandidateFacts(
                        candidate_id=f"{slug}-candidate-2",
                        contract_version="breakouts-v1",
                        venue=venue,
                        symbol=symbol,
                        direction=Direction.LONG,
                        observed_at=fresh,
                        reference_price=Decimal(101),
                        readiness="READY",
                    ),
                    now=fresh,
                )
                add_action = {**action, "idempotency_key": f"{slug}-add-dispatch"}
                add_response = await client.post(
                    f"/api/intents/{add.intent_id}/execute",
                    json=add_action,
                )
                assert add_response.status_code == 200, add_response.text
                assert add_response.json()["trade_id"] == f"{exchange}-trade-1"

                fresh = datetime.now(UTC)
                service.record_position(
                    account_id,
                    venue,
                    fixture.ids["instrument"],
                    Decimal(2),
                    mark,
                    mark,
                    True,
                    fixture.ids["operator"],
                    environment=ExecutionEnvironment.LIVE,
                    now=fresh,
                )
                reduction_id = service.create_reduction_intent(
                    opening.campaign_id,
                    fixture.ids["operator"],
                    f"{slug}-reduce",
                    candidates=(
                        TargetCandidate(
                            Decimal(1),
                            TargetUrgency.URGENT,
                            "shared partial reduction contract",
                        ),
                    ),
                    now=fresh,
                )
                reduce_action = {**action, "idempotency_key": f"{slug}-reduce-dispatch"}
                reduce_response = await client.post(
                    f"/api/intents/{reduction_id}/execute",
                    json=reduce_action,
                )
                assert reduce_response.status_code == 200, reduce_response.text
                assert reduce_response.json()["is_open"] is True

                fresh = datetime.now(UTC)
                service.record_position(
                    account_id,
                    venue,
                    fixture.ids["instrument"],
                    Decimal(1),
                    mark,
                    mark,
                    True,
                    fixture.ids["operator"],
                    environment=ExecutionEnvironment.LIVE,
                    now=fresh,
                )
                exit_id = service.create_reduction_intent(
                    opening.campaign_id,
                    fixture.ids["operator"],
                    f"{slug}-exit",
                    candidates=(
                        TargetCandidate(
                            Decimal(0),
                            TargetUrgency.URGENT,
                            "shared full exit contract",
                        ),
                    ),
                    now=fresh,
                )
                exit_action = {**action, "idempotency_key": f"{slug}-exit-dispatch"}
                exit_response = await client.post(
                    f"/api/intents/{exit_id}/execute",
                    json=exit_action,
                )
                assert exit_response.status_code == 200, exit_response.text
                assert exit_response.json()["is_open"] is False
                exit_replay = await client.post(
                    f"/api/intents/{exit_id}/execute",
                    json=exit_action,
                )
                assert exit_replay.status_code == 200, exit_replay.text
                assert exit_replay.json()["replayed"] is True

    asyncio.run(scenario())

    assert worker_fixture.writes == (4 if lifecycle else 1)
    if (venue, environment, direction) == ("BINANCE", "LIVE", "LONG"):
        rpc_binding = service.runtime_freqtrade_worker_bindings()[0]
        controlled_trade = parse_freqtrade_trade(worker_fixture._trade())
        controlled_message = FreqtradeRpcMessage(
            event_type="entry_fill",
            payload={"trade_id": controlled_trade.trade_id, "order_id": "binance-entry-1"},
            observed_at=NOW,
        )
        assert (
            service.record_freqtrade_rpc_event(
                rpc_binding,
                controlled_message,
                controlled_trade,
                now=NOW,
            )
            == "CONTROLLED"
        )
        assert (
            service.record_freqtrade_rpc_event(
                rpc_binding,
                controlled_message,
                controlled_trade,
                now=NOW,
            )
            == "CONTROLLED"
        )
        assert (
            service.record_freqtrade_rpc_event(
                rpc_binding,
                FreqtradeRpcMessage(
                    event_type="entry_fill",
                    payload={
                        "trade_id": controlled_trade.trade_id,
                        "order_id": "binance-entry-2",
                    },
                    observed_at=NOW,
                ),
                controlled_trade,
                now=NOW,
            )
            == "CONTROLLED"
        )
        assert (
            service.record_freqtrade_rpc_event(
                rpc_binding,
                FreqtradeRpcMessage(
                    event_type="exit_fill",
                    payload={"trade_id": controlled_trade.trade_id},
                    observed_at=NOW,
                ),
                controlled_trade,
                now=NOW,
            )
            == "EXTERNAL_UNBOUND"
        )
        external_trade = replace(
            controlled_trade,
            trade_id="external-unbound-trade",
            enter_tag="manual-external-trade",
        )
        assert (
            service.record_freqtrade_rpc_event(
                rpc_binding,
                FreqtradeRpcMessage(
                    event_type="entry",
                    payload={"trade_id": external_trade.trade_id},
                    observed_at=NOW,
                ),
                external_trade,
                now=NOW,
            )
            == "EXTERNAL_UNBOUND"
        )
    with database.session_factory() as session:
        intent = session.get(OrderIntent, opening.intent_id)
        order = session.scalar(
            select(VenueOrder).where(VenueOrder.order_intent_id == opening.intent_id)
        )
        protection = session.scalar(select(ProtectionOrder))
        expected_status = "PARTIALLY_FILLED" if partial_fill else "FILLED"
        assert intent is not None and intent.status == expected_status
        assert intent.dispatch_backend == "FREQTRADE"
        assert order is not None and order.status == expected_status
        if partial_fill:
            assert order.ordered_quantity == Decimal(2)
            assert order.filled_quantity == Decimal(1)
        assert protection is not None and protection.status == "ACTIVE"
        if lifecycle:
            lifecycle_intents = session.scalars(
                select(OrderIntent)
                .where(OrderIntent.campaign_id == opening.campaign_id)
                .order_by(OrderIntent.created_at)
            ).all()
            assert [item.kind for item in lifecycle_intents] == [
                "INITIAL",
                "ADD",
                "REDUCE",
                "EXIT",
            ]
            assert all(item.status == "FILLED" for item in lifecycle_intents)
            lifecycle_orders = session.scalars(
                select(VenueOrder)
                .where(VenueOrder.order_intent_id.is_not(None))
                .order_by(VenueOrder.updated_at)
            ).all()
            assert [item.filled_quantity for item in lifecycle_orders] == [
                Decimal(1),
                Decimal(1),
                Decimal(1),
                Decimal(1),
            ]
        if (venue, environment, direction) == ("BINANCE", "LIVE", "LONG"):
            account_row = session.get(ExchangeAccount, exchange_account_id)
            assert account_row is not None and account_row.trading_status == "BLOCKED"
            assert session.query(OrderIntent).count() == 4
