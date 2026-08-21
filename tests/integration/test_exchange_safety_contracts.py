from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from workflow_builder import ActorSpec, WorkflowFixture

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    CapabilityStatus,
    Direction,
    DomainRejected,
    ExecutionEnvironment,
    OrderIntentStatus,
    ReservationStatus,
    Role,
)
from trading_control_plane.execution_runtime import AutomaticExecutionWorker
from trading_control_plane.freqtrade import (
    FreqtradeEntryCommand,
    FreqtradeOrder,
    FreqtradeTrade,
    FreqtradeWorkerSpec,
)
from trading_control_plane.models import (
    AccountEquity,
    AuditEvent,
    Campaign,
    ExchangeAccount,
    OrderIntent,
    RiskReservation,
    SenderLease,
    TradingAuthorization,
    VenueOrder,
)
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.queries import TradingQueries
from trading_control_plane.runtime_contracts import (
    ConnectionProbeResult,
    PreparedFreqtradeWorkerBinding,
)
from trading_control_plane.service import TradingService
from trading_control_plane.service_domains.execution_facts import release_zero_fill_in_session


def _credential_key() -> str:
    return base64.urlsafe_b64encode(b"exchange-safety-contract-key-032").decode().rstrip("=")


def _settings(database: Database) -> Settings:
    return Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="exchange-safety-contract-session-secret",  # noqa: S106
        credential_encryption_key=_credential_key(),
        public_base_url="http://test",
        freqtrade_workers_enabled=True,
        _env_file=None,
    )


def _perptape() -> PerptapeClient:
    return PerptapeClient(
        base_url="https://perptape.com",
        api_key=None,
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
    )


@dataclass(frozen=True)
class _ExecutionContract:
    service: TradingService
    fixture: WorkflowFixture
    intent_id: UUID
    exchange_account_id: UUID
    binding: PreparedFreqtradeWorkerBinding
    scope: str
    owner_id: str
    fencing_token: int
    now: datetime


def _execution_contract(database: Database, *, enable_live_gate: bool = True) -> _ExecutionContract:
    now = datetime.now(UTC).replace(microsecond=0)
    service = TradingService(database, credential_encryption_key=_credential_key())
    fixture = WorkflowFixture.create(
        service,
        now=now,
        admin_username="exchange-safety-admin",
        account_id="exchange-safety-account",
        venue="BINANCE",
        environment=ExecutionEnvironment.LIVE,
        actors=(
            ActorSpec("proposer", "exchange-safety-proposer", Role.PROPOSER),
            ActorSpec("reviewer_one", "exchange-safety-reviewer-1", Role.REVIEWER),
            ActorSpec("reviewer_two", "exchange-safety-reviewer-2", Role.REVIEWER),
            ActorSpec("operator", "exchange-safety-operator", Role.OPERATOR),
        ),
        symbol="XRPUSDT",
        tick_size=Decimal("0.01"),
        lot_size=Decimal(1),
        minimum_notional=Decimal(5),
        quote_currency="USDT",
        risk_version="exchange-safety-risk-v1",
        max_fact_age=timedelta(minutes=10),
    )
    projected = TradingQueries(database).exchange_accounts(fixture.ids["admin"])["data"][0]
    exchange_account_id = UUID(projected["exchange_account_id"])
    version = service.rotate_exchange_account_credentials(
        exchange_account_id,
        actor_id=fixture.ids["admin"],
        credentials={"api_key": "account-key", "api_secret": "account-secret"},
        expected_version=int(projected["version"]),
        idempotency_key="exchange-safety-credentials",
        now=now,
    )
    verification, replay = service.prepare_exchange_account_connection_verification(
        exchange_account_id,
        actor_id=fixture.ids["admin"],
        expected_version=version,
        idempotency_key="exchange-safety-connection",
    )
    assert verification is not None and replay is None
    verified = service.record_exchange_account_connection_verification(
        verification,
        ConnectionProbeResult(True, None),
        actor_id=fixture.ids["admin"],
        idempotency_key="exchange-safety-connection",
        now=now,
    )
    runtime = service.configure_exchange_account_runtime_sync(
        exchange_account_id,
        actor_id=fixture.ids["admin"],
        enabled=True,
        expected_version=int(verified["version"]),
        idempotency_key="exchange-safety-runtime",
        now=now,
    )
    configured = service.configure_exchange_account_freqtrade_worker(
        exchange_account_id,
        actor_id=fixture.ids["admin"],
        mode="LIVE",
        name="exchange-safety-worker",
        base_url="http://127.0.0.1:18095",
        username="control-plane",
        password="worker-password",  # noqa: S106
        ws_token="worker-rpc-token-exchange-safety",  # noqa: S106
        hip3_dexes=(),
        expected_version=int(runtime["version"]),
        idempotency_key="exchange-safety-worker",
        now=now,
    )
    binding, replay = service.prepare_exchange_account_freqtrade_verification(
        exchange_account_id,
        actor_id=fixture.ids["admin"],
        expected_version=int(configured["version"]),
        idempotency_key="exchange-safety-worker-verification",
    )
    assert binding is not None and replay is None
    worker_verified = service.record_exchange_account_freqtrade_verification(
        binding,
        actor_id=fixture.ids["admin"],
        error_code=None,
        idempotency_key="exchange-safety-worker-verification",
        now=now,
    )
    service.configure_exchange_account_trading(
        exchange_account_id,
        actor_id=fixture.ids["admin"],
        enabled=True,
        expected_version=int(worker_verified["version"]),
        idempotency_key="exchange-safety-trading",
        now=now,
    )
    runtime_binding = service.runtime_account_bindings()[0]
    service.record_runtime_source_health(
        runtime_binding.service_principal_id,
        {"BINANCE": {"status": "SUCCESS", "items_observed": 1}},
        scopes={"BINANCE": (fixture.account_id, "BINANCE")},
        now=now,
    )
    proposal = fixture.approved_proposal(key="exchange-safety", direction=Direction.LONG)
    opening = fixture.opening_order(proposal=proposal, key="exchange-safety")
    scope = f"LIVE:{fixture.account_id}:BINANCE"
    owner_id = "exchange-safety-sender"
    fencing_token = service.acquire_sender(
        scope,
        owner_id,
        fixture.ids["operator"],
        now,
        lease_duration=timedelta(minutes=5),
    )
    if enable_live_gate:
        service.set_capability_gate(
            "LIVE_ORDER_SEND",
            CapabilityStatus.ENABLED,
            "exchange safety contract",
            fixture.ids["admin"],
            now=now,
        )
    return _ExecutionContract(
        service=service,
        fixture=fixture,
        intent_id=opening.intent_id,
        exchange_account_id=exchange_account_id,
        binding=binding,
        scope=scope,
        owner_id=owner_id,
        fencing_token=fencing_token,
        now=now,
    )


class _OutcomeUnknownWorker:
    def __init__(
        self,
        binding: PreparedFreqtradeWorkerBinding,
        *,
        probe_error: str | None = None,
    ) -> None:
        self.spec = FreqtradeWorkerSpec(
            name=binding.worker_name,
            venue=binding.venue,  # type: ignore[arg-type]
            base_url=binding.worker_url,
            username=binding.username,
            password=binding.password,
            ws_token=binding.ws_token,
            hip3_dexes=binding.hip3_dexes,
            exchange_account_id=str(binding.exchange_account_id),
            team_id=str(binding.team_id),
            account_id=binding.account_id,
        )
        self.probe_error = probe_error
        self.writes = 0
        self.recoveries = 0

    def probe(self, **_kwargs: Any) -> dict[str, Any]:
        if self.probe_error is not None:
            raise DomainRejected(self.probe_error, "worker probe failed")
        return {
            "status": "READY",
            "runtime_fingerprint": "a" * 64,
            "whitelist": ["XRP/USDT:USDT"],
            "exchange": "binance",
            "trading_mode": "futures",
            "dry_run": False,
            "demo_trading": False,
            "worker_state": "running",
            "version": "fixture",
            "bot_name": "fixture-worker",
            "force_entry_enabled": True,
            "position_adjustment_enabled": True,
            "external_order_send": True,
            "network": "LIVE",
        }

    def find_open_trade(self, **_kwargs: Any) -> None:
        return None

    @staticmethod
    def _confirmed_trade(command: FreqtradeEntryCommand) -> FreqtradeTrade:
        observed_at = datetime.now(UTC)
        entry = FreqtradeOrder(
            order_id="entry-confirmed-after-timeout",
            side="buy",
            amount=command.max_quantity,
            filled=command.max_quantity,
            price=Decimal(100),
            is_open=False,
            status="closed",
            tag=command.enter_tag,
            filled_at=observed_at,
        )
        stop = FreqtradeOrder(
            order_id="stop-confirmed-after-timeout",
            side="stoploss",
            amount=command.max_quantity,
            filled=Decimal(0),
            price=Decimal(95),
            is_open=True,
            status="open",
            tag=None,
            filled_at=None,
        )
        return FreqtradeTrade(
            trade_id="trade-confirmed-after-timeout",
            pair=command.pair,
            side=command.side,
            amount=command.max_quantity,
            stake_amount=command.stake_amount,
            open_rate=Decimal(100),
            current_rate=Decimal(100),
            close_rate=None,
            is_open=True,
            enter_tag=command.enter_tag,
            leverage=command.leverage,
            stop_loss_abs=Decimal(95),
            stoploss_order_id=stop.order_id,
            entry_order_id=entry.order_id,
            exit_order_id=None,
            observed_at=observed_at,
            orders=(entry, stop),
        )

    def force_enter(self, command: FreqtradeEntryCommand, **_kwargs: Any) -> FreqtradeTrade:
        self.writes += 1
        raise DomainRejected(
            "FREQTRADE_LIVE_OUTCOME_UNKNOWN",
            "the external write timed out after dispatch",
        )

    def recover_entry(self, command: FreqtradeEntryCommand, **_kwargs: Any) -> FreqtradeTrade:
        self.recoveries += 1
        return self._confirmed_trade(command)


async def _login(client: AsyncClient, username: str) -> None:
    response = await client.post("/api/auth/mock/login", json={"username": username})
    assert response.status_code == 200, response.text


def test_freqtrade_timeout_persists_unknown_and_replays_query_only_without_second_write(
    database: Database,
) -> None:
    contract = _execution_contract(database)
    worker = _OutcomeUnknownWorker(contract.binding)
    app = create_app(
        _settings(database),
        database,
        _perptape(),
        freqtrade_workers=(worker,),  # type: ignore[arg-type]
    )
    action = {
        "execution_scope": contract.scope,
        "owner_id": contract.owner_id,
        "fencing_token": contract.fencing_token,
        "idempotency_key": "exchange-safety-dispatch",
    }

    async def first_attempt() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _login(client, "exchange-safety-operator")
            response = await client.post(
                f"/api/intents/{contract.intent_id}/execute",
                json=action,
            )
            assert response.status_code == 503, response.text
            assert response.json()["error"]["code"] == "FREQTRADE_LIVE_OUTCOME_UNKNOWN"

    asyncio.run(first_attempt())
    assert worker.writes == 1
    assert worker.recoveries == 0
    with database.session_factory() as session:
        intent = session.get(OrderIntent, contract.intent_id)
        assert intent is not None and intent.status == "UNKNOWN"
        reservation = session.get(RiskReservation, intent.reservation_id)
        assert reservation is not None and reservation.status == ReservationStatus.UNKNOWN.value
        order = session.scalar(
            select(VenueOrder).where(VenueOrder.order_intent_id == contract.intent_id)
        )
        assert order is not None and order.status == "UNKNOWN"
        assert order.venue_order_id.startswith("UNKNOWN:")
        assert (
            session.scalar(
                select(AuditEvent).where(
                    AuditEvent.event_type == "FREQTRADE_OUTCOME_UNKNOWN",
                    AuditEvent.object_id == str(contract.intent_id),
                )
            )
            is not None
        )

    async def recovery_attempt() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _login(client, "exchange-safety-operator")
            response = await client.post(
                f"/api/intents/{contract.intent_id}/execute",
                json=action,
            )
            assert response.status_code == 200, response.text
            assert response.json()["replayed"] is True
            assert response.json()["trade_id"] == "trade-confirmed-after-timeout"

    asyncio.run(recovery_attempt())
    assert worker.writes == 1
    assert worker.recoveries == 1
    with database.session_factory() as session:
        intent = session.get(OrderIntent, contract.intent_id)
        assert intent is not None and intent.status == "FILLED"
        reservation = session.get(RiskReservation, intent.reservation_id)
        assert reservation is not None and reservation.status == ReservationStatus.OPEN.value
        order = session.scalar(
            select(VenueOrder).where(VenueOrder.order_intent_id == contract.intent_id)
        )
        assert order is not None and order.venue_order_id == "entry-confirmed-after-timeout"


def test_confirmed_zero_fill_releases_frozen_risk_and_authorization(database: Database) -> None:
    contract = _execution_contract(database)
    with database.session_factory.begin() as session:
        intent = session.get(OrderIntent, contract.intent_id, with_for_update=True)
        assert intent is not None and intent.reservation_id is not None
        authorization = session.get(
            TradingAuthorization, intent.authorization_id, with_for_update=True
        )
        assert authorization is not None
        used_quantity = authorization.used_quantity
        release_zero_fill_in_session(
            session,
            intent,
            OrderIntentStatus.CANCELLED,
            confirmed_external=True,
            now=datetime.now(UTC),
        )
        assert intent.status == OrderIntentStatus.CANCELLED.value
        reservation = session.get(RiskReservation, intent.reservation_id)
        assert reservation is not None
        assert reservation.status == ReservationStatus.RELEASED.value
        assert authorization.used_quantity == used_quantity - intent.quantity


def test_pre_send_rejects_insufficient_exact_account_available_margin(
    database: Database,
) -> None:
    contract = _execution_contract(database)
    worker = _OutcomeUnknownWorker(contract.binding)
    with database.session_factory.begin() as session:
        equity = session.scalar(
            select(AccountEquity)
            .where(
                AccountEquity.account_id == contract.fixture.account_id,
                AccountEquity.venue == contract.fixture.venue,
                AccountEquity.environment == ExecutionEnvironment.LIVE.value,
            )
            .with_for_update()
        )
        assert equity is not None
        equity.available_balance = Decimal("10")
    app = create_app(
        _settings(database),
        database,
        _perptape(),
        freqtrade_workers=(worker,),  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _login(client, "exchange-safety-operator")
            response = await client.post(
                f"/api/intents/{contract.intent_id}/execute",
                json={
                    "execution_scope": contract.scope,
                    "owner_id": contract.owner_id,
                    "fencing_token": contract.fencing_token,
                    "idempotency_key": "insufficient-exact-account-margin",
                },
            )
            assert response.status_code == 422, response.text
            assert response.json()["error"]["code"] == "INSUFFICIENT_AVAILABLE_MARGIN"

    asyncio.run(scenario())
    assert worker.writes == 0
    assert worker.recoveries == 0
    with database.session_factory() as session:
        intent = session.get(OrderIntent, contract.intent_id)
        assert intent is not None
        assert intent.status == OrderIntentStatus.READY.value
        assert intent.dispatch_backend is None


@pytest.mark.parametrize(
    ("code", "reason_fragment"),
    (
        ("FREQTRADE_LIVE_MODE_REQUIRED", "模拟模式"),
        ("FREQTRADE_INSTRUMENT_NOT_ALLOWED", "白名单"),
        ("FREQTRADE_FORCE_ENTRY_DISABLED", "Force Entry"),
    ),
)
def test_pre_send_worker_blocker_is_persisted_for_campaign_and_audit(
    database: Database,
    code: str,
    reason_fragment: str,
) -> None:
    contract = _execution_contract(database)
    checked_at = contract.now + timedelta(seconds=1)

    assert contract.service.record_execution_blocker(
        contract.intent_id,
        actor_id=contract.fixture.ids["operator"],
        error_code=code,
        now=checked_at,
        retry_after_seconds=60,
    )
    with database.session_factory() as session:
        intent = session.get(OrderIntent, contract.intent_id)
        assert intent is not None
        campaign_id = intent.campaign_id
        assert intent.status == OrderIntentStatus.READY.value
        assert intent.dispatch_backend is None
        assert intent.execution_blocker_code == code
        assert reason_fragment in str(intent.execution_blocker_reason)
        assert intent.execution_last_checked_at == checked_at
        assert intent.execution_retry_at == checked_at + timedelta(seconds=60)
        audits = session.scalars(
            select(AuditEvent).where(
                AuditEvent.object_id == str(intent.intent_id),
                AuditEvent.event_type == "AUTOMATIC_EXECUTION_PRE_SEND_BLOCKED",
            )
        ).all()
        assert len(audits) == 1
        assert f"code={code}" in audits[0].reason
        assert "external_send=none" in audits[0].reason
    detail = TradingQueries(database).campaign_detail(
        contract.fixture.ids["admin"],
        campaign_id,
    )
    blocker = detail["intents"][0]["execution_blocker"]
    assert blocker["code"] == code
    assert reason_fragment in blocker["reason"]
    assert blocker["component"] == "execution-worker"
    assert blocker["occurred_at"] == checked_at.isoformat()
    assert blocker["last_checked_at"] == checked_at.isoformat()


def test_automatic_worker_cancels_expired_unsent_intent_and_releases_risk(
    database: Database,
) -> None:
    contract = _execution_contract(database)
    worker_client = _OutcomeUnknownWorker(contract.binding)
    run_at = contract.now + timedelta(seconds=2)
    with database.session_factory.begin() as session:
        intent = session.get(OrderIntent, contract.intent_id, with_for_update=True)
        assert intent is not None
        authorization = session.get(
            TradingAuthorization,
            intent.authorization_id,
            with_for_update=True,
        )
        assert authorization is not None
        authorization.expires_at = run_at - timedelta(seconds=1)
        lease = session.get(SenderLease, (contract.binding.team_id, contract.scope))
        assert lease is not None
        lease.expires_at = run_at - timedelta(seconds=1)

    settings = _settings(database).model_copy(
        update={
            "execution_worker_enabled": True,
            "execution_worker_batch_size": 20,
            "runtime_sync_interval_seconds": 60,
        }
    )
    worker = AutomaticExecutionWorker(
        settings=settings,
        database=database,
        worker_factory=lambda _binding: worker_client,  # type: ignore[arg-type]
        clock=lambda: run_at,
    )

    report = worker.run_once()

    assert report.blocked["AUTHORIZATION_EXPIRED"] == 1
    assert worker_client.writes == 0
    with database.session_factory() as session:
        intent = session.get(OrderIntent, contract.intent_id)
        assert intent is not None
        reservation = session.get(RiskReservation, intent.reservation_id)
        authorization = session.get(TradingAuthorization, intent.authorization_id)
        campaign = session.get(Campaign, intent.campaign_id)
        assert intent.status == OrderIntentStatus.CANCELLED.value
        assert intent.dispatch_backend is None
        assert reservation is not None
        assert reservation.status == ReservationStatus.RELEASED.value
        assert authorization is not None and authorization.active is False
        assert campaign is not None and campaign.status == "CLOSED"


@pytest.mark.parametrize(
    "scope",
    (
        "LIVE:another-account:BINANCE",
        "LIVE:exchange-safety-account:OKX",
        "TESTNET:exchange-safety-account:BINANCE",
    ),
)
def test_execute_rejects_account_venue_and_mode_mismatch_before_worker_io(
    database: Database,
    scope: str,
) -> None:
    contract = _execution_contract(database)
    worker = _OutcomeUnknownWorker(contract.binding)
    token = contract.service.acquire_sender(
        scope,
        "mismatch-sender",
        contract.fixture.ids["admin"],
        contract.now,
        lease_duration=timedelta(minutes=5),
    )
    app = create_app(
        _settings(database),
        database,
        _perptape(),
        freqtrade_workers=(worker,),  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _login(client, "exchange-safety-admin")
            response = await client.post(
                f"/api/intents/{contract.intent_id}/execute",
                json={
                    "execution_scope": scope,
                    "owner_id": "mismatch-sender",
                    "fencing_token": token,
                    "idempotency_key": f"mismatch-{scope}",
                },
            )
            assert response.status_code == 422, response.text
            assert response.json()["error"]["code"] == "EXECUTION_SCOPE_MISMATCH"

    asyncio.run(scenario())
    assert worker.writes == 0
    assert worker.recoveries == 0


def test_execute_rejects_expired_sender_before_dispatch(
    database: Database,
) -> None:
    contract = _execution_contract(database)
    worker = _OutcomeUnknownWorker(contract.binding)
    with database.session_factory.begin() as session:
        lease = session.get(SenderLease, (contract.binding.team_id, contract.scope))
        assert lease is not None
        lease.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    app = create_app(
        _settings(database),
        database,
        _perptape(),
        freqtrade_workers=(worker,),  # type: ignore[arg-type]
    )

    async def expired_scenario() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _login(client, "exchange-safety-operator")
            response = await client.post(
                f"/api/intents/{contract.intent_id}/execute",
                json={
                    "execution_scope": contract.scope,
                    "owner_id": contract.owner_id,
                    "fencing_token": contract.fencing_token,
                    "idempotency_key": "expired-dispatch",
                },
            )
            assert response.status_code == 422, response.text
            assert response.json()["error"]["code"] == "FENCING_TOKEN_REJECTED"

    asyncio.run(expired_scenario())
    assert worker.writes == 0
    with database.session_factory() as session:
        intent = session.get(OrderIntent, contract.intent_id)
        assert intent is not None and intent.dispatch_backend is None


def test_execute_rejects_worker_unavailability_before_dispatch(database: Database) -> None:
    contract = _execution_contract(database)
    unavailable = _OutcomeUnknownWorker(
        contract.binding,
        probe_error="FREQTRADE_WORKER_UNAVAILABLE",
    )
    unavailable_app = create_app(
        _settings(database),
        database,
        _perptape(),
        freqtrade_workers=(unavailable,),  # type: ignore[arg-type]
    )

    async def unavailable_scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=unavailable_app),
            base_url="http://test",
        ) as client:
            await _login(client, "exchange-safety-operator")
            response = await client.post(
                f"/api/intents/{contract.intent_id}/execute",
                json={
                    "execution_scope": contract.scope,
                    "owner_id": contract.owner_id,
                    "fencing_token": contract.fencing_token,
                    "idempotency_key": "unavailable-dispatch",
                },
            )
            assert response.status_code == 503, response.text
            assert response.json()["error"]["code"] == "FREQTRADE_WORKER_UNAVAILABLE"

    asyncio.run(unavailable_scenario())
    assert unavailable.writes == 0
    with database.session_factory() as session:
        intent = session.get(OrderIntent, contract.intent_id)
        assert intent is not None and intent.dispatch_backend is None


def test_live_gate_and_worker_auth_rotation_remain_fail_closed(database: Database) -> None:
    contract = _execution_contract(database, enable_live_gate=False)
    worker = _OutcomeUnknownWorker(contract.binding)
    app = create_app(
        _settings(database),
        database,
        _perptape(),
        freqtrade_workers=(worker,),  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _login(client, "exchange-safety-operator")
            response = await client.post(
                f"/api/intents/{contract.intent_id}/execute",
                json={
                    "execution_scope": contract.scope,
                    "owner_id": contract.owner_id,
                    "fencing_token": contract.fencing_token,
                    "idempotency_key": "gate-disabled-dispatch",
                },
            )
            assert response.status_code == 422, response.text
            assert response.json()["error"]["code"] == "LIVE_ORDER_SEND_DISABLED"

    asyncio.run(scenario())
    assert worker.writes == 0

    with database.session_factory() as session:
        account = session.get(ExchangeAccount, contract.exchange_account_id)
        assert account is not None
        current_version = account.version
    contract.service.configure_exchange_account_freqtrade_worker(
        contract.exchange_account_id,
        actor_id=contract.fixture.ids["admin"],
        mode="LIVE",
        name="exchange-safety-worker-rotated",
        base_url="http://127.0.0.1:18096",
        username="control-plane-rotated",
        password="worker-password-rotated",  # noqa: S106
        ws_token="worker-rpc-token-rotated-safety",  # noqa: S106
        hip3_dexes=(),
        expected_version=current_version,
        idempotency_key="exchange-safety-worker-rotation",
        now=datetime.now(UTC),
    )
    with pytest.raises(DomainRejected, match="FREQTRADE_WORKER_BINDING_CHANGED"):
        contract.service.validate_freqtrade_worker_binding(contract.binding)
