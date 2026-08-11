from __future__ import annotations

import asyncio
import base64
import urllib.parse
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from trading_control_plane.api import create_app
from trading_control_plane.binance_execution import (
    BinancePortfolioMarginClient,
    BinanceTestnetOrder,
)
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    CapabilityStatus,
    Direction,
    DomainRejected,
    ExecutionEnvironment,
    IntentKind,
    ProposalSource,
    ReviewDecision,
    RiskTier,
    Role,
    SystemRiskState,
    TargetCandidate,
    TargetUrgency,
)
from trading_control_plane.exchange_connection import ConnectionProbeResult
from trading_control_plane.freqtrade import FreqtradeWorkerClient, FreqtradeWorkerSpec
from trading_control_plane.hyperliquid_execution import (
    HyperliquidLiveClient,
    HyperliquidTestnetOrder,
)
from trading_control_plane.models import (
    CapabilityGate,
    OrderIntent,
    ProtectionOrder,
    RiskReservation,
)
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.queries import TradingQueries
from trading_control_plane.service import TradingService

NOW = datetime.now(UTC)
HYPERLIQUID_ACCOUNT = "0x1111111111111111111111111111111111111111"


def credential_encryption_key() -> str:
    return base64.urlsafe_b64encode(b"live-execution-fixture-key-32byt"[:32]).decode().rstrip("=")


class PortfolioMarginVenue:
    def __init__(self, *, fill_orders: bool = True) -> None:
        self.calls: list[tuple[str, str]] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self.algos: dict[str, dict[str, Any]] = {}
        self.next_id = 1000
        self.fill_orders = fill_orders

    def __call__(
        self, method: str, url: str, _headers: dict[str, str], _timeout: float
    ) -> dict[str, Any]:
        self.calls.append((method, url))
        parsed = urllib.parse.urlparse(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        if parsed.path == "/papi/v1/um/order":
            client_id = query.get("origClientOrderId") or query.get("newClientOrderId")
            assert client_id is not None
            if method == "GET":
                return self.orders.get(client_id, {"code": -2013, "msg": "not found"})
            if method == "DELETE":
                self.orders[client_id]["status"] = "CANCELED"
                return self.orders[client_id]
            self.next_id += 1
            order = {
                "symbol": query["symbol"],
                "orderId": self.next_id,
                "clientOrderId": client_id,
                "status": "FILLED" if self.fill_orders else "NEW",
                "side": query["side"],
                "type": "MARKET",
                "origQty": query["quantity"],
                "executedQty": query["quantity"] if self.fill_orders else "0",
                "stopPrice": "0",
                "reduceOnly": query.get("reduceOnly") == "true",
                "closePosition": False,
                "updateTime": int(NOW.timestamp() * 1000),
            }
            self.orders[client_id] = order
            return order
        if parsed.path == "/papi/v1/um/algo/algoOrder":
            return self.algos.get(
                query["clientAlgoId"],
                {"code": -2013, "msg": "not found"},
            )
        assert parsed.path == "/papi/v1/um/algo/order"
        client_id = query["clientAlgoId"]
        if method == "DELETE":
            self.algos[client_id]["algoStatus"] = "CANCELED"
            return {"complete": True}
        self.next_id += 1
        algo = {
            "algoId": self.next_id,
            "clientAlgoId": client_id,
            "algoStatus": "NEW",
            "side": query["side"],
            "orderType": query["type"],
            "quantity": query["quantity"],
            "triggerPrice": query["triggerPrice"],
            "reduceOnly": True,
            "updateTime": int(NOW.timestamp() * 1000),
        }
        self.algos[client_id] = algo
        return algo

    @property
    def write_count(self) -> int:
        return sum(method in {"POST", "DELETE"} for method, _url in self.calls)


class HyperliquidVenue:
    def __init__(self, *, fill_orders: bool = True) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self.next_id = 2000
        self.fill_orders = fill_orders

    def __call__(
        self, url: str, payload: dict[str, Any], _timeout: float
    ) -> dict[str, Any] | list[Any]:
        self.calls.append((url, payload))
        if url.endswith("/info"):
            if payload["type"] == "metaAndAssetCtxs":
                return [
                    {"universe": [{"name": "BTC", "szDecimals": 5}]},
                    [{"markPx": "60000"}],
                ]
            return self.orders.get(str(payload["oid"]), {"status": "unknownOid"})
        action = payload["action"]
        if action["type"] == "cancelByCloid":
            cloid = str(action["cancels"][0]["cloid"])
            self.orders[cloid]["order"]["status"] = "canceled"
            return {
                "status": "ok",
                "response": {"type": "cancel", "data": {"statuses": ["success"]}},
            }
        item = action["orders"][0]
        self.next_id += 1
        cloid = str(item["c"])
        trigger = "trigger" in item["t"]
        status = "open" if trigger or not self.fill_orders else "filled"
        order = {
            "coin": "BTC",
            "oid": self.next_id,
            "cloid": cloid,
            "side": "B" if item["b"] else "A",
            "limitPx": item["p"],
            "sz": item["s"] if trigger else "0",
            "origSz": item["s"],
            "timestamp": int(NOW.timestamp() * 1000),
            "triggerPx": item["t"].get("trigger", {}).get("triggerPx", "0"),
            "isTrigger": trigger,
            "reduceOnly": item["r"],
        }
        self.orders[cloid] = {
            "status": "order",
            "order": {
                "order": order,
                "status": status,
                "statusTimestamp": int(NOW.timestamp() * 1000),
            },
        }
        acknowledgement = (
            {"resting": {"oid": self.next_id}}
            if trigger or not self.fill_orders
            else {
                "filled": {
                    "totalSz": item["s"],
                    "avgPx": item["p"],
                    "oid": self.next_id,
                }
            }
        )
        return {
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [acknowledgement]}},
        }

    @property
    def write_count(self) -> int:
        return sum(url.endswith("/exchange") for url, _payload in self.calls)


class FailingBinanceLiveClient:
    configured = True

    def __init__(self, code: str) -> None:
        self.code = code

    def ensure_order(self, _command: Any, *, now: datetime) -> None:
        del now
        raise DomainRejected(self.code, "controlled live failure")


class FailingHyperliquidLiveClient(FailingBinanceLiveClient):
    account_scope = "MAIN_ACCOUNT"


class RecoveringBinanceLiveClient:
    configured = True

    @staticmethod
    def recover_order(command: Any, *, now: datetime) -> BinanceTestnetOrder:
        return BinanceTestnetOrder(
            order_id="recovered-binance",
            client_order_id=command.client_order_id,
            status="FILLED",
            side=command.side,
            order_type="MARKET",
            ordered_quantity=command.quantity,
            filled_quantity=command.quantity,
            stop_price=Decimal(0),
            reduce_only=command.reduce_only,
            close_position=False,
            observed_at=now,
        )


class RecoveringHyperliquidLiveClient:
    configured = True
    account_scope = "MAIN_ACCOUNT"

    @staticmethod
    def recover_order(command: Any, *, now: datetime) -> HyperliquidTestnetOrder:
        return HyperliquidTestnetOrder(
            order_id="recovered-hyperliquid",
            client_order_id=command.client_order_id,
            status="FILLED",
            side=command.side,
            order_type="IOC_LIMIT",
            ordered_quantity=command.quantity,
            filled_quantity=command.quantity,
            limit_price=command.limit_price,
            stop_price=Decimal(0),
            reduce_only=command.reduce_only,
            close_position=False,
            observed_at=now,
        )


class CancelOutcomeBinanceLiveClient:
    configured = True

    def __init__(self, *, unknown: bool) -> None:
        self.unknown = unknown

    def cancel_order(self, _command: Any, *, now: datetime) -> None:
        del now
        if self.unknown:
            raise DomainRejected("BINANCE_LIVE_OUTCOME_UNKNOWN", "controlled cancel outcome")
        return None


class CancelOutcomeHyperliquidLiveClient(CancelOutcomeBinanceLiveClient):
    account_scope = "MAIN_ACCOUNT"

    def cancel_order(self, _command: Any, *, now: datetime) -> None:
        del now
        if self.unknown:
            raise DomainRejected("HYPERLIQUID_LIVE_OUTCOME_UNKNOWN", "controlled cancel outcome")
        return None


class FailingProtectionBinanceLiveClient:
    configured = True

    @staticmethod
    def ensure_protection(_command: Any, *, now: datetime) -> None:
        del now
        raise DomainRejected("BINANCE_LIVE_OUTCOME_UNKNOWN", "controlled protection outcome")


class FailingProtectionHyperliquidLiveClient:
    configured = True
    account_scope = "MAIN_ACCOUNT"

    @staticmethod
    def ensure_protection(_command: Any, *, now: datetime) -> None:
        del now
        raise DomainRejected("HYPERLIQUID_LIVE_OUTCOME_UNKNOWN", "controlled protection outcome")


def register_live_exchange_account(
    service: TradingService,
    *,
    admin: UUID,
    key: str,
    account_id: str,
    venue: str,
) -> UUID:
    exchange_account = service.create_exchange_account(
        actor_id=admin,
        account_id=account_id,
        venue=venue,
        label=f"{key} account",
        credentials=(
            {"account_address": HYPERLIQUID_ACCOUNT}
            if venue == "HYPERLIQUID"
            else {
                "api_key": f"{key}-fixture-key",
                "api_secret": f"{key}-fixture-secret",
                **({"passphrase": f"{key}-fixture-passphrase"} if venue == "OKX" else {}),
            }
        ),
        idempotency_key=f"{key}-account",
        now=NOW,
    )
    connection_command, connection_replay = (
        service.prepare_exchange_account_connection_verification(
            exchange_account,
            actor_id=admin,
            expected_version=1,
            idempotency_key=f"{key}-connection",
        )
    )
    assert connection_replay is None and connection_command is not None
    connection = service.record_exchange_account_connection_verification(
        connection_command,
        ConnectionProbeResult(True, None),
        actor_id=admin,
        idempotency_key=f"{key}-connection",
        now=NOW,
    )
    runtime_binding = service.configure_exchange_account_runtime_sync(
        exchange_account,
        actor_id=admin,
        enabled=True,
        expected_version=int(connection["version"]),
        idempotency_key=f"{key}-runtime-binding",
        now=NOW,
    )
    trading_eligibility = service.configure_exchange_account_trading(
        exchange_account,
        actor_id=admin,
        enabled=True,
        expected_version=int(runtime_binding["version"]),
        idempotency_key=f"{key}-trading-eligibility",
        now=NOW,
    )
    assert trading_eligibility["trading_enabled"] is True
    return exchange_account


def seed_live(
    service: TradingService,
    *,
    key: str,
    account_id: str,
    venue: str,
    symbol: str,
    quantity: Decimal,
    mark_price: Decimal,
    tick_size: Decimal,
    lot_size: Decimal,
    minimum_notional: Decimal,
) -> dict[str, UUID]:
    admin = service.bootstrap_admin(f"{key}-admin", now=NOW)
    exchange_account = register_live_exchange_account(
        service,
        admin=admin,
        key=key,
        account_id=account_id,
        venue=venue,
    )
    proposer = service.create_user(f"{key}-proposer", admin, now=NOW)
    reviewer_one = service.create_user(f"{key}-reviewer-1", admin, now=NOW)
    reviewer_two = service.create_user(f"{key}-reviewer-2", admin, now=NOW)
    operator = service.create_user(f"{key}-operator", admin, now=NOW)
    for user_id, role in (
        (proposer, Role.PROPOSER),
        (reviewer_one, Role.REVIEWER),
        (reviewer_two, Role.REVIEWER),
        (operator, Role.OPERATOR),
    ):
        service.assign_role(user_id, role, admin, account_id, venue, now=NOW)
    instrument = service.register_instrument(
        actor_id=admin,
        venue=venue,
        symbol=symbol,
        tick_size=tick_size,
        lot_size=lot_size,
        minimum_notional=minimum_notional,
        contract_multiplier=Decimal(1),
        quote_currency="USDC" if venue == "HYPERLIQUID" else "USDT",
        collateral_currency="USDC" if venue == "HYPERLIQUID" else "USDT",
        protection_supported=True,
        now=NOW,
    )
    service.set_risk_policy(
        actor_id=admin,
        version=f"{key}-risk-v1",
        system_state=SystemRiskState.NORMAL,
        max_total_risk=Decimal(100),
        max_account_risk=Decimal(100),
        max_single_loss=Decimal(100),
        max_consecutive_losses=3,
        loss_cooldown=timedelta(hours=1),
        max_fact_age=timedelta(minutes=10),
        now=NOW,
    )
    runtime = service.create_service_principal(f"{key}-runtime", admin, now=NOW)
    service.assign_role(
        runtime,
        Role.OPERATOR,
        admin,
        account_scope=account_id,
        venue_scope=venue,
        now=NOW,
    )
    service.record_runtime_source_health(
        runtime,
        {venue: {"status": "SUCCESS", "items_observed": 1}},
        scopes={venue: (account_id, venue)},
        now=NOW,
    )
    service.record_position(
        account_id,
        venue,
        instrument,
        Decimal(0),
        Decimal(0),
        mark_price,
        True,
        operator,
        environment=ExecutionEnvironment.LIVE,
        now=NOW,
    )
    service.record_account_equity(
        account_id,
        venue,
        Decimal(100),
        Decimal(90),
        "USDC" if venue == "HYPERLIQUID" else "USDT",
        True,
        operator,
        environment=ExecutionEnvironment.LIVE,
        now=NOW,
    )
    details = {"invalidation_price": str(mark_price * Decimal("0.95"))}
    if venue == "HYPERLIQUID":
        details["limit_price"] = str(mark_price)
    proposal = service.create_proposal(
        actor_id=proposer,
        source=ProposalSource.MANUAL,
        risk_tier=RiskTier.HIGH,
        account_id=account_id,
        venue=venue,
        instrument_id=instrument,
        direction=Direction.LONG,
        quantity=quantity,
        max_risk=Decimal(5),
        expires_at=NOW + timedelta(hours=2),
        idempotency_key=f"{key}-proposal",
        environment=ExecutionEnvironment.LIVE,
        details=details,
        now=NOW,
    )
    service.submit_proposal(proposal, proposer, now=NOW)
    service.review_proposal(proposal, reviewer_one, ReviewDecision.APPROVE, "first", now=NOW)
    service.review_proposal(proposal, reviewer_two, ReviewDecision.APPROVE, "second", now=NOW)
    service.decide_risk(
        proposal_id=proposal,
        actor_id=operator,
        kind=IntentKind.INITIAL,
        idempotency_key=f"{key}-risk",
        now=NOW,
    )
    authorization = service.issue_authorization(
        proposal_id=proposal,
        actor_id=operator,
        expires_at=NOW + timedelta(minutes=30),
        allowed_adds=0,
        idempotency_key=f"{key}-authorization",
        now=NOW,
    )
    opening = service.create_order_intent(
        authorization,
        operator,
        IntentKind.INITIAL,
        account_id,
        venue,
        instrument,
        Direction.LONG,
        quantity,
        f"{key}-opening",
        now=NOW,
    )
    return {
        "admin": admin,
        "exchange_account": exchange_account,
        "operator": operator,
        "runtime": runtime,
        "instrument": instrument,
        "campaign": opening.campaign_id,
        "opening": opening.intent_id,
    }


def application(
    database: Database,
    *,
    venue: str,
    binance: BinancePortfolioMarginClient | None = None,
    hyperliquid: HyperliquidLiveClient | None = None,
    perptape_client: PerptapeClient | None = None,
) -> FastAPI:
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="live-integration-signing-secret-is-long-enough",  # noqa: S106
        public_base_url="http://test",
        execution_backend="DIRECT_LEGACY",
        binance_live_order_send_enabled=venue == "BINANCE",
        binance_api_key="fixture-key",
        binance_api_secret="fixture-secret",  # noqa: S106
        hyperliquid_live_order_send_enabled=venue == "HYPERLIQUID",
        hyperliquid_account_address=HYPERLIQUID_ACCOUNT,
        hyperliquid_api_wallet_private_key="0x"
        "1111111111111111111111111111111111111111111111111111111111111111",
        _env_file=None,
    )
    perptape = perptape_client or PerptapeClient(
        base_url="https://perptape.com",
        api_key=None,
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
    )
    return create_app(
        settings,
        database,
        perptape,
        binance_live_client=binance,
        hyperliquid_live_client=hyperliquid,
    )


async def login(http: AsyncClient, username: str) -> None:
    response = await http.post("/api/auth/mock/login", json={"username": username})
    assert response.status_code == 200, response.text


async def exercise_binance_live(database: Database) -> None:
    service = TradingService(database, credential_encryption_key=credential_encryption_key())
    ids = seed_live(
        service,
        key="live-binance",
        account_id="acct-live-binance",
        venue="BINANCE",
        symbol="XRPUSDT",
        quantity=Decimal(5),
        mark_price=Decimal(1),
        tick_size=Decimal("0.0001"),
        lot_size=Decimal("0.1"),
        minimum_notional=Decimal(5),
    )
    venue = PortfolioMarginVenue()
    execution = BinancePortfolioMarginClient(
        base_url="https://papi.binance.com",
        api_key="fixture-key",
        api_secret="fixture-secret",  # noqa: S106
        requester=venue,
        server_time_fetcher=lambda _timeout: int(NOW.timestamp() * 1000),
    )
    async with AsyncClient(
        transport=ASGITransport(app=application(database, venue="BINANCE", binance=execution)),
        base_url="http://test",
    ) as http:
        await login(http, "live-binance-operator")
        lease_response = await http.post(
            "/api/sender-leases",
            json={
                "execution_scope": "LIVE:acct-live-binance:BINANCE",
                "owner_id": "binance-live-worker",
                "lease_seconds": 300,
            },
        )
        assert lease_response.status_code == 200, lease_response.text
        lease = lease_response.json()
        action = {
            "execution_scope": lease["execution_scope"],
            "owner_id": lease["owner_id"],
            "fencing_token": lease["fencing_token"],
        }

        blocked = await http.post(f"/api/intents/{ids['opening']}/binance/live/send", json=action)
        assert blocked.status_code == 422
        assert blocked.json()["error"]["code"] == "LIVE_ORDER_SEND_DISABLED"
        assert venue.write_count == 0

        service.set_capability_gate(
            "LIVE_ORDER_SEND",
            CapabilityStatus.ENABLED,
            "integration fixture",
            ids["admin"],
            now=NOW,
        )
        account_projection = TradingQueries(database).exchange_accounts(ids["admin"])["data"][0]
        disabled_account = service.configure_exchange_account_trading(
            ids["exchange_account"],
            actor_id=ids["admin"],
            enabled=False,
            expected_version=int(account_projection["version"]),
            idempotency_key="live-binance-disable-account-trading",
            now=NOW,
        )
        account_blocked = await http.post(
            f"/api/intents/{ids['opening']}/binance/live/send", json=action
        )
        assert account_blocked.status_code == 422
        assert account_blocked.json()["error"]["code"] == "EXCHANGE_ACCOUNT_TRADING_DISABLED"
        assert venue.write_count == 0
        service.configure_exchange_account_trading(
            ids["exchange_account"],
            actor_id=ids["admin"],
            enabled=True,
            expected_version=int(disabled_account["version"]),
            idempotency_key="live-binance-reenable-account-trading",
            now=NOW,
        )
        service.record_runtime_source_health(
            ids["runtime"],
            {"BINANCE": {"status": "FAILED", "error_code": "BINANCE_NETWORK_UNAVAILABLE"}},
            scopes={"BINANCE": ("acct-live-binance", "BINANCE")},
            now=NOW,
        )
        source_blocked = await http.post(
            f"/api/intents/{ids['opening']}/binance/live/send", json=action
        )
        assert source_blocked.status_code == 422
        assert source_blocked.json()["error"]["code"] == "READ_ONLY_SOURCE_UNAVAILABLE"
        assert venue.write_count == 0
        service.record_runtime_source_health(
            ids["runtime"],
            {"BINANCE": {"status": "SUCCESS", "items_observed": 1}},
            scopes={"BINANCE": ("acct-live-binance", "BINANCE")},
            now=NOW,
        )
        sent = await http.post(f"/api/intents/{ids['opening']}/binance/live/send", json=action)
        duplicate = await http.post(f"/api/intents/{ids['opening']}/binance/live/send", json=action)
        assert sent.status_code == 200, sent.text
        assert duplicate.status_code == 200, duplicate.text
        assert venue.write_count == 1
        assert len(sent.json()["client_order_id"]) <= 32

        fact_now = datetime.now(UTC)
        service.record_position(
            "acct-live-binance",
            "BINANCE",
            ids["instrument"],
            Decimal(5),
            Decimal(1),
            Decimal(1),
            True,
            ids["operator"],
            environment=ExecutionEnvironment.LIVE,
            now=fact_now,
        )
        protected = await http.post(
            f"/api/campaigns/{ids['campaign']}/binance/live/protection",
            json={**action, "trigger_price": "0.95"},
        )
        assert protected.status_code == 200, protected.text
        assert venue.write_count == 2

        unsafe_cancel = await http.post(
            f"/api/campaigns/{ids['campaign']}/binance/live/protection/cancel",
            json=action,
        )
        assert unsafe_cancel.status_code == 422
        assert unsafe_cancel.json()["error"]["code"] == "PROTECTION_CANCEL_UNSAFE"
        service.update_campaign_target(
            ids["campaign"],
            ids["operator"],
            (TargetCandidate(Decimal(0), TargetUrgency.IMMEDIATE, "exit"),),
            now=datetime.now(UTC),
        )
        cancelled = await http.post(
            f"/api/campaigns/{ids['campaign']}/binance/live/protection/cancel",
            json=action,
        )
        assert cancelled.status_code == 200, cancelled.text
        assert venue.write_count == 3

        stale = await http.post(
            f"/api/intents/{ids['opening']}/binance/live/send",
            json={**action, "fencing_token": lease["fencing_token"] + 1},
        )
        assert stale.status_code == 422
        assert stale.json()["error"]["code"] == "FENCING_TOKEN_REJECTED"
        assert venue.write_count == 3

        service.set_capability_gate(
            "LIVE_ORDER_SEND",
            CapabilityStatus.DISABLED,
            "integration fixture complete",
            ids["admin"],
            now=datetime.now(UTC),
        )


async def exercise_hyperliquid_live(database: Database) -> None:
    service = TradingService(database, credential_encryption_key=credential_encryption_key())
    ids = seed_live(
        service,
        key="live-hyperliquid",
        account_id="acct-live-hyperliquid",
        venue="HYPERLIQUID",
        symbol="BTC",
        quantity=Decimal("0.0002"),
        mark_price=Decimal(60000),
        tick_size=Decimal("0.1"),
        lot_size=Decimal("0.00001"),
        minimum_notional=Decimal(10),
    )
    venue = HyperliquidVenue()
    execution = HyperliquidLiveClient(
        base_url="https://api.hyperliquid.xyz",
        account_address=HYPERLIQUID_ACCOUNT,
        signer=lambda _action, _nonce: {"r": "0x01", "s": "0x02", "v": 27},
        requester=venue,
    )
    async with AsyncClient(
        transport=ASGITransport(
            app=application(
                database,
                venue="HYPERLIQUID",
                hyperliquid=execution,
            )
        ),
        base_url="http://test",
    ) as http:
        await login(http, "live-hyperliquid-operator")
        lease_response = await http.post(
            "/api/sender-leases",
            json={
                "execution_scope": "LIVE:acct-live-hyperliquid:HYPERLIQUID",
                "owner_id": "hyperliquid-live-worker",
                "lease_seconds": 300,
            },
        )
        assert lease_response.status_code == 200, lease_response.text
        lease = lease_response.json()
        action = {
            "execution_scope": lease["execution_scope"],
            "owner_id": lease["owner_id"],
            "fencing_token": lease["fencing_token"],
        }

        blocked = await http.post(
            f"/api/intents/{ids['opening']}/hyperliquid/live/send",
            json=action,
        )
        assert blocked.status_code == 422
        assert blocked.json()["error"]["code"] == "LIVE_ORDER_SEND_DISABLED"
        assert venue.write_count == 0

        service.set_capability_gate(
            "LIVE_ORDER_SEND",
            CapabilityStatus.ENABLED,
            "integration fixture",
            ids["admin"],
            now=NOW,
        )
        service.record_runtime_source_health(
            ids["runtime"],
            {
                "HYPERLIQUID": {
                    "status": "FAILED",
                    "error_code": "HYPERLIQUID_NETWORK_UNAVAILABLE",
                }
            },
            scopes={
                "HYPERLIQUID": ("acct-live-hyperliquid", "HYPERLIQUID")
            },
            now=NOW,
        )
        source_blocked = await http.post(
            f"/api/intents/{ids['opening']}/hyperliquid/live/send",
            json=action,
        )
        assert source_blocked.status_code == 422
        assert source_blocked.json()["error"]["code"] == "READ_ONLY_SOURCE_UNAVAILABLE"
        assert venue.write_count == 0
        service.record_runtime_source_health(
            ids["runtime"],
            {"HYPERLIQUID": {"status": "SUCCESS", "items_observed": 1}},
            scopes={
                "HYPERLIQUID": ("acct-live-hyperliquid", "HYPERLIQUID")
            },
            now=NOW,
        )
        sent = await http.post(
            f"/api/intents/{ids['opening']}/hyperliquid/live/send",
            json=action,
        )
        duplicate = await http.post(
            f"/api/intents/{ids['opening']}/hyperliquid/live/send",
            json=action,
        )
        assert sent.status_code == 200, sent.text
        assert duplicate.status_code == 200, duplicate.text
        assert venue.write_count == 1

        fact_now = datetime.now(UTC)
        service.record_position(
            "acct-live-hyperliquid",
            "HYPERLIQUID",
            ids["instrument"],
            Decimal("0.0002"),
            Decimal(60000),
            Decimal(60000),
            True,
            ids["operator"],
            environment=ExecutionEnvironment.LIVE,
            now=fact_now,
        )
        protected = await http.post(
            f"/api/campaigns/{ids['campaign']}/hyperliquid/live/protection",
            json={
                **action,
                "trigger_price": "57000",
                "limit_price": "56500",
            },
        )
        assert protected.status_code == 200, protected.text
        assert venue.write_count == 2

        service.update_campaign_target(
            ids["campaign"],
            ids["operator"],
            (TargetCandidate(Decimal(0), TargetUrgency.IMMEDIATE, "exit"),),
            now=datetime.now(UTC),
        )
        cancelled = await http.post(
            f"/api/campaigns/{ids['campaign']}/hyperliquid/live/protection/cancel",
            json=action,
        )
        assert cancelled.status_code == 200, cancelled.text
        assert venue.write_count == 3

        stale = await http.post(
            f"/api/intents/{ids['opening']}/hyperliquid/live/send",
            json={**action, "fencing_token": lease["fencing_token"] + 1},
        )
        assert stale.status_code == 422
        assert stale.json()["error"]["code"] == "FENCING_TOKEN_REJECTED"
        assert venue.write_count == 3

        service.set_capability_gate(
            "LIVE_ORDER_SEND",
            CapabilityStatus.DISABLED,
            "integration fixture complete",
            ids["admin"],
            now=datetime.now(UTC),
        )


async def exercise_perptape_binance_live_lifecycle(database: Database) -> None:
    service = TradingService(database, credential_encryption_key=credential_encryption_key())
    account_id = "acct-perptape-live"
    venue_name = "BINANCE"
    admin = service.bootstrap_admin("perptape-live-admin", now=NOW)
    register_live_exchange_account(
        service,
        admin=admin,
        key="perptape-live",
        account_id=account_id,
        venue=venue_name,
    )
    proposer = service.create_user("perptape-live-proposer", admin, now=NOW)
    reviewer_one = service.create_user("perptape-live-reviewer-1", admin, now=NOW)
    reviewer_two = service.create_user("perptape-live-reviewer-2", admin, now=NOW)
    operator = service.create_user("perptape-live-operator", admin, now=NOW)
    perptape_actor = service.create_service_principal("perptape", admin, now=NOW)
    for user_id, role in (
        (proposer, Role.PROPOSER),
        (reviewer_one, Role.REVIEWER),
        (reviewer_two, Role.REVIEWER),
        (operator, Role.OPERATOR),
        (perptape_actor, Role.PROPOSER),
    ):
        service.assign_role(user_id, role, admin, account_id, venue_name, now=NOW)
    instrument = service.register_instrument(
        actor_id=admin,
        venue=venue_name,
        symbol="XRPUSDT",
        tick_size=Decimal("0.0001"),
        lot_size=Decimal("0.1"),
        minimum_notional=Decimal(5),
        contract_multiplier=Decimal(1),
        quote_currency="USDT",
        collateral_currency="USDT",
        protection_supported=True,
        now=NOW,
    )
    service.set_risk_policy(
        actor_id=admin,
        version="perptape-live-risk-v1",
        system_state=SystemRiskState.NORMAL,
        max_total_risk=Decimal(100),
        max_account_risk=Decimal(100),
        max_single_loss=Decimal(100),
        max_consecutive_losses=3,
        loss_cooldown=timedelta(hours=1),
        max_fact_age=timedelta(minutes=10),
        now=NOW,
    )
    service.record_position(
        account_id,
        venue_name,
        instrument,
        Decimal(0),
        Decimal(0),
        Decimal(10),
        True,
        operator,
        environment=ExecutionEnvironment.LIVE,
        now=NOW,
    )
    service.record_account_equity(
        account_id,
        venue_name,
        Decimal(100),
        Decimal(90),
        "USDT",
        True,
        operator,
        environment=ExecutionEnvironment.LIVE,
        now=NOW,
    )
    runtime = service.create_service_principal("perptape-live-runtime", admin, now=NOW)
    service.assign_role(
        runtime,
        Role.OPERATOR,
        admin,
        account_scope=account_id,
        venue_scope=venue_name,
        now=NOW,
    )
    service.record_runtime_source_health(
        runtime,
        {venue_name: {"status": "SUCCESS", "items_observed": 1}},
        scopes={venue_name: (account_id, venue_name)},
        now=NOW,
    )
    candidate_time = int(datetime.now(UTC).timestamp() * 1000)
    candidate_state = {
        "triggered_at": candidate_time - 1_000,
        "updated_at": candidate_time,
        "price": 10,
    }

    def perptape_fetcher(_url: str, _headers: dict[str, str], _timeout: float) -> dict[str, Any]:
        return {
            "type": "breakouts",
            "generatedAt": candidate_state["updated_at"],
            "data": [
                {
                    "exchange": "BN",
                    "symbol": "XRPUSDT",
                    "canonicalSymbol": "XRP",
                    "direction": "HH",
                    "timeframe": "1h",
                    "price": candidate_state["price"],
                    "breakoutPrice": candidate_state["price"],
                    "threshold": 9.9,
                    "klineReadiness": {"status": "ready"},
                    "triggeredAt": candidate_state["triggered_at"],
                    "updatedAt": candidate_state["updated_at"],
                }
            ],
        }

    perptape = PerptapeClient(
        base_url="https://perptape.com",
        api_key="fixture-contract-key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=5),
        fetcher=perptape_fetcher,
    )
    venue = PortfolioMarginVenue()
    execution = BinancePortfolioMarginClient(
        base_url="https://papi.binance.com",
        api_key="fixture-key",
        api_secret="fixture-secret",  # noqa: S106
        requester=venue,
        server_time_fetcher=lambda _timeout: int(datetime.now(UTC).timestamp() * 1000),
    )
    async with AsyncClient(
        transport=ASGITransport(
            app=application(
                database,
                venue="BINANCE",
                binance=execution,
                perptape_client=perptape,
            )
        ),
        base_url="http://test",
    ) as http:
        await login(http, "perptape-live-proposer")
        candidate_response = await http.get("/api/opportunities")
        assert candidate_response.status_code == 200, candidate_response.text
        candidate = candidate_response.json()["data"][0]
        proposal_response = await http.post(
            f"/api/opportunities/{candidate['candidate_id']}/proposals",
            json={
                "environment": "LIVE",
                "account_id": account_id,
                "risk_tier": "HIGH",
                "quantity": "2",
                "initial_quantity": "1",
                "max_risk": "5",
                "expires_in_minutes": 480,
                "invalidation_price": "9",
                "allow_auto_add": True,
                "requested_adds": 1,
                "add_trigger_price": "10.5",
                "rationale": "Perptape production lifecycle contract",
            },
        )
        assert proposal_response.status_code == 200, proposal_response.text
        proposal_id = UUID(proposal_response.json()["proposal_id"])
        assert proposal_response.json()["source_candidate_id"] == candidate["candidate_id"]
        await http.post("/api/auth/logout")

        service.review_proposal(
            proposal_id,
            reviewer_one,
            ReviewDecision.APPROVE,
            "first independent review",
            now=NOW,
        )
        service.review_proposal(
            proposal_id,
            reviewer_two,
            ReviewDecision.APPROVE,
            "second independent review",
            now=NOW,
        )
        service.decide_risk(
            proposal_id=proposal_id,
            actor_id=operator,
            kind=IntentKind.INITIAL,
            idempotency_key="perptape-live-risk",
            now=NOW,
        )
        with database.session_factory.begin() as session:
            gate = session.get(CapabilityGate, "AUTO_ADD", with_for_update=True)
            assert gate is not None
            gate.status = CapabilityStatus.ENABLED.value
            gate.reason = "Perptape lifecycle integration fixture precondition"
            gate.operator_id = str(admin)
            gate.version += 1
            gate.updated_at = NOW
        authorization = service.issue_authorization(
            proposal_id=proposal_id,
            actor_id=operator,
            expires_at=NOW + timedelta(minutes=30),
            allowed_adds=1,
            idempotency_key="perptape-live-authorization",
            now=NOW,
        )
        opening = service.create_order_intent(
            authorization,
            operator,
            IntentKind.INITIAL,
            account_id,
            venue_name,
            instrument,
            Direction.LONG,
            Decimal(1),
            "perptape-live-opening",
            now=NOW,
        )
        service.set_capability_gate(
            "LIVE_ORDER_SEND",
            CapabilityStatus.ENABLED,
            "Perptape lifecycle integration fixture",
            admin,
            now=NOW,
        )
        await login(http, "perptape-live-operator")
        lease_response = await http.post(
            "/api/sender-leases",
            json={
                "execution_scope": f"LIVE:{account_id}:{venue_name}",
                "owner_id": "perptape-live-worker",
                "lease_seconds": 300,
            },
        )
        lease = lease_response.json()
        action = {
            "execution_scope": lease["execution_scope"],
            "owner_id": lease["owner_id"],
            "fencing_token": lease["fencing_token"],
        }
        opening_send = await http.post(
            f"/api/intents/{opening.intent_id}/binance/live/send",
            json=action,
        )
        opening_duplicate = await http.post(
            f"/api/intents/{opening.intent_id}/binance/live/send",
            json=action,
        )
        assert opening_send.status_code == opening_duplicate.status_code == 200
        assert venue.write_count == 1

        fact_now = datetime.now(UTC)
        position_id = service.record_position(
            account_id,
            venue_name,
            instrument,
            Decimal(1),
            Decimal(10),
            Decimal(11),
            True,
            operator,
            environment=ExecutionEnvironment.LIVE,
            now=fact_now,
        )
        service.record_protection(
            position_id,
            "perptape-live-stop-1",
            Decimal(1),
            Decimal(9),
            True,
            operator,
            now=fact_now,
        )
        candidate_state["triggered_at"] += 1
        candidate_state["updated_at"] = int(datetime.now(UTC).timestamp() * 1000)
        candidate_state["price"] = 11
        perptape._cached_at = None
        campaign_path = f"/api/campaigns/{opening.campaign_id}"
        add_candidates = await http.get(f"{campaign_path}/add-candidates")
        assert add_candidates.status_code == 200, add_candidates.text
        add_candidate = add_candidates.json()["data"][0]
        add = await http.post(
            f"{campaign_path}/auto-add",
            json={
                "candidate_id": add_candidate["candidate_id"],
                "quantity": "0.5",
                "idempotency_key": "perptape-live-add",
            },
        )
        assert add.status_code == 200, add.text
        add_send = await http.post(
            f"/api/intents/{add.json()['intent_id']}/binance/live/send",
            json=action,
        )
        assert add_send.status_code == 200, add_send.text
        assert venue.write_count == 2

        fact_now = datetime.now(UTC)
        service.record_position(
            account_id,
            venue_name,
            instrument,
            Decimal("1.5"),
            Decimal("10.333333333333333333"),
            Decimal(11),
            True,
            operator,
            environment=ExecutionEnvironment.LIVE,
            now=fact_now,
        )
        service.record_protection(
            position_id,
            "perptape-live-stop-2",
            Decimal("1.5"),
            Decimal(9),
            True,
            operator,
            now=fact_now,
        )
        reduction = await http.post(
            f"{campaign_path}/managed-reductions",
            json={
                "target_quantity": "1",
                "urgency": "URGENT",
                "reason": "verified production reduction",
                "idempotency_key": "perptape-live-reduction",
            },
        )
        assert reduction.status_code == 200, reduction.text
        assert reduction.json()["detail"]["intents"][-1]["reduce_only"] is True
        reduction_send = await http.post(
            f"/api/intents/{reduction.json()['intent_id']}/binance/live/send",
            json=action,
        )
        assert reduction_send.status_code == 200, reduction_send.text
        assert venue.write_count == 3

        fact_now = datetime.now(UTC)
        service.record_position(
            account_id,
            venue_name,
            instrument,
            Decimal(1),
            Decimal("10.333333333333333333"),
            Decimal("8.5"),
            True,
            operator,
            environment=ExecutionEnvironment.LIVE,
            now=fact_now,
        )
        exit_response = await http.post(
            f"{campaign_path}/automatic-exit",
            json={"idempotency_key": "perptape-live-exit"},
        )
        assert exit_response.status_code == 200, exit_response.text
        assert exit_response.json()["triggered"] is True
        exit_intent = exit_response.json()["detail"]["intents"][-1]
        assert exit_intent["kind"] == "EXIT"
        assert exit_intent["reduce_only"] is True
        exit_send = await http.post(
            f"/api/intents/{exit_intent['intent_id']}/binance/live/send",
            json=action,
        )
        exit_duplicate = await http.post(
            f"/api/intents/{exit_intent['intent_id']}/binance/live/send",
            json=action,
        )
        assert exit_send.status_code == exit_duplicate.status_code == 200
        assert venue.write_count == 4
        detail = await http.get(campaign_path)
        assert detail.status_code == 200
        assert [item["kind"] for item in detail.json()["intents"]] == [
            "INITIAL",
            "ADD",
            "REDUCE",
            "EXIT",
        ]

        service.set_capability_gate(
            "AUTO_ADD",
            CapabilityStatus.DISABLED,
            "Perptape lifecycle integration fixture complete",
            admin,
            now=datetime.now(UTC),
        )
        service.set_capability_gate(
            "LIVE_ORDER_SEND",
            CapabilityStatus.DISABLED,
            "Perptape lifecycle integration fixture complete",
            admin,
            now=datetime.now(UTC),
        )


async def exercise_failed_live_send(
    database: Database,
    *,
    venue: str,
    code: str,
) -> None:
    service = TradingService(database, credential_encryption_key=credential_encryption_key())
    is_binance = venue == "BINANCE"
    key = f"failed-{venue.lower()}-{code.lower()}"
    ids = seed_live(
        service,
        key=key,
        account_id=f"acct-{key}",
        venue=venue,
        symbol="XRPUSDT" if is_binance else "BTC",
        quantity=Decimal(5) if is_binance else Decimal("0.0002"),
        mark_price=Decimal(1) if is_binance else Decimal(60000),
        tick_size=Decimal("0.0001") if is_binance else Decimal("0.1"),
        lot_size=Decimal("0.1") if is_binance else Decimal("0.00001"),
        minimum_notional=Decimal(5) if is_binance else Decimal(10),
    )
    client: Any = (
        FailingBinanceLiveClient(code) if is_binance else FailingHyperliquidLiveClient(code)
    )
    app = application(
        database,
        venue=venue,
        binance=client if is_binance else None,
        hyperliquid=None if is_binance else client,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        await login(http, f"{key}-operator")
        lease_response = await http.post(
            "/api/sender-leases",
            json={
                "execution_scope": f"LIVE:acct-{key}:{venue}",
                "owner_id": f"{key}-worker",
                "lease_seconds": 300,
            },
        )
        assert lease_response.status_code == 200
        action = {
            "execution_scope": lease_response.json()["execution_scope"],
            "owner_id": lease_response.json()["owner_id"],
            "fencing_token": lease_response.json()["fencing_token"],
        }
        service.set_capability_gate(
            "LIVE_ORDER_SEND",
            CapabilityStatus.ENABLED,
            "failure-path fixture",
            ids["admin"],
            now=NOW,
        )
        path = (
            f"/api/intents/{ids['opening']}/binance/live/send"
            if is_binance
            else f"/api/intents/{ids['opening']}/hyperliquid/live/send"
        )
        response = await http.post(path, json=action)
        expected_unknown = code.endswith("OUTCOME_UNKNOWN")
        assert response.status_code == (503 if expected_unknown else 422)
        assert response.json()["error"] == {
            "code": code,
            "message": "controlled live failure",
            "retryable": expected_unknown,
        }
    with database.session_factory() as session:
        intent = session.get(OrderIntent, ids["opening"])
        reservation = (
            None if intent is None else session.get(RiskReservation, intent.reservation_id)
        )
        assert intent is not None and reservation is not None
        assert intent.status == ("UNKNOWN" if expected_unknown else "REJECTED")
        assert reservation.status == ("UNKNOWN" if expected_unknown else "RELEASED")


async def exercise_live_cancel(database: Database, *, venue: str) -> None:
    service = TradingService(database, credential_encryption_key=credential_encryption_key())
    is_binance = venue == "BINANCE"
    key = f"cancel-{venue.lower()}"
    ids = seed_live(
        service,
        key=key,
        account_id=f"acct-{key}",
        venue=venue,
        symbol="XRPUSDT" if is_binance else "BTC",
        quantity=Decimal(5) if is_binance else Decimal("0.0002"),
        mark_price=Decimal(1) if is_binance else Decimal(60000),
        tick_size=Decimal("0.0001") if is_binance else Decimal("0.1"),
        lot_size=Decimal("0.1") if is_binance else Decimal("0.00001"),
        minimum_notional=Decimal(5) if is_binance else Decimal(10),
    )
    wire = (
        PortfolioMarginVenue(fill_orders=False)
        if is_binance
        else HyperliquidVenue(fill_orders=False)
    )
    client: Any = (
        BinancePortfolioMarginClient(
            base_url="https://papi.binance.com",
            api_key="fixture-key",
            api_secret="fixture-secret",  # noqa: S106
            requester=wire,
            server_time_fetcher=lambda _timeout: int(NOW.timestamp() * 1000),
        )
        if is_binance
        else HyperliquidLiveClient(
            base_url="https://api.hyperliquid.xyz",
            account_address=HYPERLIQUID_ACCOUNT,
            signer=lambda _action, _nonce: {"r": "0x01", "s": "0x02", "v": 27},
            requester=wire,
        )
    )
    app = application(
        database,
        venue=venue,
        binance=client if is_binance else None,
        hyperliquid=None if is_binance else client,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        await login(http, f"{key}-operator")
        lease_response = await http.post(
            "/api/sender-leases",
            json={
                "execution_scope": f"LIVE:acct-{key}:{venue}",
                "owner_id": f"{key}-worker",
                "lease_seconds": 300,
            },
        )
        action = {
            "execution_scope": lease_response.json()["execution_scope"],
            "owner_id": lease_response.json()["owner_id"],
            "fencing_token": lease_response.json()["fencing_token"],
        }
        service.set_capability_gate(
            "LIVE_ORDER_SEND",
            CapabilityStatus.ENABLED,
            "cancel fixture",
            ids["admin"],
            now=NOW,
        )
        prefix = "binance" if is_binance else "hyperliquid"
        sent = await http.post(
            f"/api/intents/{ids['opening']}/{prefix}/live/send",
            json=action,
        )
        cancelled = await http.post(
            f"/api/intents/{ids['opening']}/{prefix}/live/cancel",
            json=action,
        )
        assert sent.status_code == 200, sent.text
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["confirmed"] is True
        assert wire.write_count == 2
    with database.session_factory() as session:
        intent = session.get(OrderIntent, ids["opening"])
        assert intent is not None and intent.status == "CANCELLED"


async def exercise_live_recovery(database: Database, *, venue: str) -> None:
    service = TradingService(database, credential_encryption_key=credential_encryption_key())
    is_binance = venue == "BINANCE"
    key = f"recover-{venue.lower()}"
    ids = seed_live(
        service,
        key=key,
        account_id=f"acct-{key}",
        venue=venue,
        symbol="XRPUSDT" if is_binance else "BTC",
        quantity=Decimal(5) if is_binance else Decimal("0.0002"),
        mark_price=Decimal(1) if is_binance else Decimal(60000),
        tick_size=Decimal("0.0001") if is_binance else Decimal("0.1"),
        lot_size=Decimal("0.1") if is_binance else Decimal("0.00001"),
        minimum_notional=Decimal(5) if is_binance else Decimal(10),
    )
    scope = f"LIVE:acct-{key}:{venue}"
    owner = f"{key}-worker"
    token = service.acquire_sender(
        scope,
        owner,
        ids["operator"],
        NOW,
        lease_duration=timedelta(minutes=30),
    )
    service.set_capability_gate(
        "LIVE_ORDER_SEND",
        CapabilityStatus.ENABLED,
        "recovery fixture",
        ids["admin"],
        now=NOW,
    )
    if is_binance:
        command = service.prepare_binance_live_send(
            ids["opening"], ids["operator"], scope, owner, token, now=NOW
        )
        service.record_binance_live_unknown(
            ids["opening"],
            ids["operator"],
            scope,
            owner,
            token,
            command,
            "controlled unknown",
            now=NOW,
        )
        client: Any = RecoveringBinanceLiveClient()
    else:
        command = service.prepare_hyperliquid_live_send(
            ids["opening"], ids["operator"], scope, owner, token, now=NOW
        )
        service.record_hyperliquid_live_unknown(
            ids["opening"],
            ids["operator"],
            scope,
            owner,
            token,
            command,
            "controlled unknown",
            now=NOW,
        )
        client = RecoveringHyperliquidLiveClient()
    app = application(
        database,
        venue=venue,
        binance=client if is_binance else None,
        hyperliquid=None if is_binance else client,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        await login(http, f"{key}-operator")
        prefix = "binance" if is_binance else "hyperliquid"
        response = await http.post(
            f"/api/intents/{ids['opening']}/{prefix}/live/recover",
            json={
                "execution_scope": scope,
                "owner_id": owner,
                "fencing_token": token,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["recovered"] is True
    with database.session_factory() as session:
        intent = session.get(OrderIntent, ids["opening"])
        assert intent is not None and intent.status == "FILLED"


async def exercise_live_cancel_outcome(database: Database, *, venue: str, unknown: bool) -> None:
    service = TradingService(database, credential_encryption_key=credential_encryption_key())
    is_binance = venue == "BINANCE"
    key = f"cancel-outcome-{venue.lower()}-{unknown}"
    quantity = Decimal(5) if is_binance else Decimal("0.0002")
    ids = seed_live(
        service,
        key=key,
        account_id=f"acct-{key}",
        venue=venue,
        symbol="XRPUSDT" if is_binance else "BTC",
        quantity=quantity,
        mark_price=Decimal(1) if is_binance else Decimal(60000),
        tick_size=Decimal("0.0001") if is_binance else Decimal("0.1"),
        lot_size=Decimal("0.1") if is_binance else Decimal("0.00001"),
        minimum_notional=Decimal(5) if is_binance else Decimal(10),
    )
    scope = f"LIVE:acct-{key}:{venue}"
    owner = f"{key}-worker"
    token = service.acquire_sender(
        scope,
        owner,
        ids["operator"],
        NOW,
        lease_duration=timedelta(minutes=30),
    )
    service.set_capability_gate(
        "LIVE_ORDER_SEND",
        CapabilityStatus.ENABLED,
        "cancel outcome fixture",
        ids["admin"],
        now=NOW,
    )
    if is_binance:
        prepared = service.prepare_binance_live_send(
            ids["opening"], ids["operator"], scope, owner, token, now=NOW
        )
        service.record_binance_live_order(
            ids["opening"],
            ids["operator"],
            scope,
            owner,
            token,
            prepared,
            BinanceTestnetOrder(
                order_id="sent-binance",
                client_order_id=prepared.client_order_id,
                status="SENT",
                side=prepared.side,
                order_type="MARKET",
                ordered_quantity=prepared.quantity,
                filled_quantity=Decimal(0),
                stop_price=Decimal(0),
                reduce_only=prepared.reduce_only,
                close_position=False,
                observed_at=NOW,
            ),
            now=NOW,
        )
        client: Any = CancelOutcomeBinanceLiveClient(unknown=unknown)
    else:
        prepared = service.prepare_hyperliquid_live_send(
            ids["opening"], ids["operator"], scope, owner, token, now=NOW
        )
        service.record_hyperliquid_live_order(
            ids["opening"],
            ids["operator"],
            scope,
            owner,
            token,
            prepared,
            HyperliquidTestnetOrder(
                order_id="sent-hyperliquid",
                client_order_id=prepared.client_order_id,
                status="SENT",
                side=prepared.side,
                order_type="IOC_LIMIT",
                ordered_quantity=prepared.quantity,
                filled_quantity=Decimal(0),
                limit_price=prepared.limit_price,
                stop_price=Decimal(0),
                reduce_only=prepared.reduce_only,
                close_position=False,
                observed_at=NOW,
            ),
            now=NOW,
        )
        client = CancelOutcomeHyperliquidLiveClient(unknown=unknown)
    app = application(
        database,
        venue=venue,
        binance=client if is_binance else None,
        hyperliquid=None if is_binance else client,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        await login(http, f"{key}-operator")
        prefix = "binance" if is_binance else "hyperliquid"
        response = await http.post(
            f"/api/intents/{ids['opening']}/{prefix}/live/cancel",
            json={
                "execution_scope": scope,
                "owner_id": owner,
                "fencing_token": token,
            },
        )
        if unknown:
            assert response.status_code == 503
            assert response.json()["error"]["retryable"] is True
        else:
            assert response.status_code == 200
            assert response.json()["confirmed"] is False
    with database.session_factory() as session:
        intent = session.get(OrderIntent, ids["opening"])
        assert intent is not None and intent.status == "UNKNOWN"


async def exercise_unknown_live_protection(database: Database, *, venue: str) -> None:
    service = TradingService(database, credential_encryption_key=credential_encryption_key())
    action_now = datetime.now(UTC)
    is_binance = venue == "BINANCE"
    key = f"unknown-protection-{venue.lower()}"
    quantity = Decimal(5) if is_binance else Decimal("0.0002")
    mark = Decimal(1) if is_binance else Decimal(60000)
    ids = seed_live(
        service,
        key=key,
        account_id=f"acct-{key}",
        venue=venue,
        symbol="XRPUSDT" if is_binance else "BTC",
        quantity=quantity,
        mark_price=mark,
        tick_size=Decimal("0.0001") if is_binance else Decimal("0.1"),
        lot_size=Decimal("0.1") if is_binance else Decimal("0.00001"),
        minimum_notional=Decimal(5) if is_binance else Decimal(10),
    )
    scope = f"LIVE:acct-{key}:{venue}"
    owner = f"{key}-worker"
    token = service.acquire_sender(scope, owner, ids["operator"], action_now)
    service.set_capability_gate(
        "LIVE_ORDER_SEND",
        CapabilityStatus.ENABLED,
        "unknown protection fixture",
        ids["admin"],
        now=action_now,
    )
    if is_binance:
        prepared = service.prepare_binance_live_send(
            ids["opening"], ids["operator"], scope, owner, token, now=action_now
        )
        service.record_binance_live_order(
            ids["opening"],
            ids["operator"],
            scope,
            owner,
            token,
            prepared,
            BinanceTestnetOrder(
                order_id="filled-binance",
                client_order_id=prepared.client_order_id,
                status="FILLED",
                side=prepared.side,
                order_type="MARKET",
                ordered_quantity=prepared.quantity,
                filled_quantity=prepared.quantity,
                stop_price=Decimal(0),
                reduce_only=False,
                close_position=False,
                observed_at=action_now,
            ),
            now=action_now,
        )
        client: Any = FailingProtectionBinanceLiveClient()
    else:
        prepared = service.prepare_hyperliquid_live_send(
            ids["opening"], ids["operator"], scope, owner, token, now=action_now
        )
        service.record_hyperliquid_live_order(
            ids["opening"],
            ids["operator"],
            scope,
            owner,
            token,
            prepared,
            HyperliquidTestnetOrder(
                order_id="filled-hyperliquid",
                client_order_id=prepared.client_order_id,
                status="FILLED",
                side=prepared.side,
                order_type="IOC_LIMIT",
                ordered_quantity=prepared.quantity,
                filled_quantity=prepared.quantity,
                limit_price=prepared.limit_price,
                stop_price=Decimal(0),
                reduce_only=False,
                close_position=False,
                observed_at=action_now,
            ),
            now=action_now,
        )
        client = FailingProtectionHyperliquidLiveClient()
    position_id = service.record_position(
        f"acct-{key}",
        venue,
        ids["instrument"],
        quantity,
        mark,
        mark,
        True,
        ids["operator"],
        environment=ExecutionEnvironment.LIVE,
        now=action_now,
    )
    app = application(
        database,
        venue=venue,
        binance=client if is_binance else None,
        hyperliquid=None if is_binance else client,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        await login(http, f"{key}-operator")
        prefix = "binance" if is_binance else "hyperliquid"
        payload: dict[str, Any] = {
            "execution_scope": scope,
            "owner_id": owner,
            "fencing_token": token,
            "trigger_price": "0.95" if is_binance else "57000",
        }
        if not is_binance:
            payload["limit_price"] = "56500"
        response = await http.post(
            f"/api/campaigns/{ids['campaign']}/{prefix}/live/protection",
            json=payload,
        )
        assert response.status_code == 503, response.text
        assert response.json()["error"]["retryable"] is True
    with database.session_factory() as session:
        protection = session.scalar(
            select(ProtectionOrder).where(ProtectionOrder.position_id == position_id)
        )
        assert protection is not None and protection.status == "UNKNOWN"


def test_binance_live_flow_is_gated_idempotent_fenced_and_cleans_protection(
    database: Database,
) -> None:
    asyncio.run(exercise_binance_live(database))


def test_freqtrade_live_never_falls_back_to_an_unbound_venue_worker(
    database: Database,
) -> None:
    service = TradingService(database, credential_encryption_key=credential_encryption_key())
    ids = seed_live(
        service,
        key="freqtrade-unbound",
        account_id="acct-freqtrade-unbound",
        venue="BINANCE",
        symbol="XRPUSDT",
        quantity=Decimal(5),
        mark_price=Decimal(1),
        tick_size=Decimal("0.0001"),
        lot_size=Decimal("0.1"),
        minimum_notional=Decimal(5),
    )
    scope = "LIVE:acct-freqtrade-unbound:BINANCE"
    owner = "freqtrade-unbound-worker"
    token = service.acquire_sender(
        scope,
        owner,
        ids["operator"],
        NOW,
        lease_duration=timedelta(minutes=30),
    )
    service.set_capability_gate(
        "LIVE_ORDER_SEND",
        CapabilityStatus.ENABLED,
        "exact worker binding fixture",
        ids["admin"],
        now=NOW,
    )
    calls: list[str] = []

    def forbidden_fetcher(
        url: str,
        method: str,
        payload: dict[str, Any] | None,
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]:
        del method, payload, headers, timeout
        calls.append(url)
        raise AssertionError("unbound venue worker must never be contacted")

    legacy_worker = FreqtradeWorkerClient(
        FreqtradeWorkerSpec(
            name="binance-default",
            venue="BINANCE",
            base_url="http://127.0.0.1:18081",
            username="legacy-user",
            password="legacy-password",  # noqa: S106
        ),
        fetcher=forbidden_fetcher,
    )
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="freqtrade-binding-test-secret-long-enough",  # noqa: S106
        credential_encryption_key=credential_encryption_key(),
        public_base_url="http://test",
        execution_backend="FREQTRADE",
        freqtrade_workers_enabled=True,
        freqtrade_live_order_send_enabled=True,
        _env_file=None,
    )
    app = create_app(
        settings,
        database,
        PerptapeClient(
            base_url="https://perptape.com",
            api_key=None,
            contract_version="breakouts-v1",
            cache_ttl=timedelta(minutes=1),
        ),
        freqtrade_workers=(legacy_worker,),
    )

    async def scenario() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            await login(http, "freqtrade-unbound-operator")
            response = await http.post(
                f"/api/intents/{ids['opening']}/freqtrade/live/send",
                json={
                    "execution_scope": scope,
                    "owner_id": owner,
                    "fencing_token": token,
                },
            )
            assert response.status_code == 422, response.text
            assert response.json()["error"]["code"] == "FREQTRADE_WORKER_NOT_CONFIGURED"
            assert calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("venue", "symbol", "exchange"),
    [
        ("OKX", "XRP-USDT-SWAP", "okx"),
        ("BYBIT", "XRPUSDT", "bybit"),
    ],
)
def test_okx_bybit_live_execution_reuses_exact_account_freqtrade_chain(
    database: Database, venue: str, symbol: str, exchange: str
) -> None:
    service = TradingService(database, credential_encryption_key=credential_encryption_key())
    key = f"freqtrade-{venue.lower()}"
    account_id = f"acct-{key}"
    ids = seed_live(
        service,
        key=key,
        account_id=account_id,
        venue=venue,
        symbol=symbol,
        quantity=Decimal(5),
        mark_price=Decimal(1),
        tick_size=Decimal("0.0001"),
        lot_size=Decimal("0.1"),
        minimum_notional=Decimal(5),
    )
    account_projection = TradingQueries(database).exchange_accounts(ids["admin"])["data"][0]
    worker_url = "http://127.0.0.1:18085"
    configured = service.configure_exchange_account_freqtrade_worker(
        ids["exchange_account"],
        actor_id=ids["admin"],
        mode="LIVE",
        name=f"{exchange}-exact-worker",
        base_url=worker_url,
        username="control-plane",
        password="worker-fixture-password",  # noqa: S106
        hip3_dexes=(),
        expected_version=int(account_projection["version"]),
        idempotency_key=f"{key}-worker-configure",
        now=NOW,
    )
    binding, replay = service.prepare_exchange_account_freqtrade_verification(
        ids["exchange_account"],
        actor_id=ids["admin"],
        expected_version=int(configured["version"]),
        idempotency_key=f"{key}-worker-verify",
    )
    assert replay is None and binding is not None
    service.record_exchange_account_freqtrade_verification(
        binding,
        actor_id=ids["admin"],
        error_code=None,
        idempotency_key=f"{key}-worker-verify",
        now=NOW,
    )

    class ExactWorkerFixture:
        def __init__(self) -> None:
            self.entry_tag: str | None = None
            self.write_count = 0

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
                return {"access_token": "short-lived-token"}
            if path.endswith("/show_config"):
                return {
                    "exchange": exchange,
                    "trading_mode": "futures",
                    "dry_run": False,
                    "force_entry_enable": True,
                    "state": "running",
                }
            if path.endswith("/version"):
                return {"version": "2026.3"}
            if path.endswith("/whitelist"):
                return {"whitelist": ["XRP/USDT:USDT"]}
            if path.endswith("/forceenter"):
                assert method == "POST" and payload is not None
                assert payload["pair"] == "XRP/USDT:USDT"
                self.entry_tag = str(payload["entry_tag"])
                self.write_count += 1
                return {"status": "created"}
            if path.endswith("/status"):
                if self.entry_tag is None:
                    return []
                return [
                    {
                        "trade_id": f"{exchange}-trade-1",
                        "pair": "XRP/USDT:USDT",
                        "is_short": False,
                        "amount": "5",
                        "stake_amount": "4.9",
                        "open_rate": "1",
                        "current_rate": "1",
                        "close_rate": None,
                        "is_open": True,
                        "enter_tag": self.entry_tag,
                        "leverage": "1",
                        "stop_loss_abs": "0.95",
                        "stoploss_order_id": f"{exchange}-stop-1",
                        "open_timestamp": int(NOW.timestamp() * 1000),
                        "orders": [
                            {
                                "ft_order_side": "buy",
                                "order_id": f"{exchange}-entry-1",
                                "status": "closed",
                                "is_open": False,
                            }
                        ],
                    }
                ]
            raise AssertionError((method, path))

    fixture = ExactWorkerFixture()
    worker = FreqtradeWorkerClient(
        FreqtradeWorkerSpec(
            name=binding.worker_name,
            venue=venue,  # type: ignore[arg-type]
            base_url=worker_url,
            username="control-plane",
            password="worker-fixture-password",  # noqa: S106
            exchange_account_id=str(ids["exchange_account"]),
            team_id=str(binding.team_id),
            account_id=account_id,
        ),
        confirmation_timeout_seconds=10,
        fetcher=fixture,
    )
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="freqtrade-okx-bybit-test-secret-long-enough",  # noqa: S106
        credential_encryption_key=credential_encryption_key(),
        public_base_url="http://test",
        execution_backend="FREQTRADE",
        freqtrade_workers_enabled=True,
        freqtrade_live_order_send_enabled=True,
        _env_file=None,
    )
    app = create_app(
        settings,
        database,
        PerptapeClient(
            base_url="https://perptape.com",
            api_key=None,
            contract_version="breakouts-v1",
            cache_ttl=timedelta(minutes=1),
        ),
        freqtrade_workers=(worker,),
    )
    scope = f"LIVE:{account_id}:{venue}"
    owner = f"{key}-worker"
    token = service.acquire_sender(
        scope,
        owner,
        ids["operator"],
        NOW,
        lease_duration=timedelta(minutes=30),
    )
    service.set_capability_gate(
        "LIVE_ORDER_SEND",
        CapabilityStatus.ENABLED,
        "OKX/Bybit exact-account Freqtrade integration fixture",
        ids["admin"],
        now=NOW,
    )

    async def scenario() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            await login(http, f"{key}-operator")
            response = await http.post(
                f"/api/intents/{ids['opening']}/freqtrade/live/send",
                json={
                    "execution_scope": scope,
                    "owner_id": owner,
                    "fencing_token": token,
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["backend"] == "FREQTRADE"
            assert response.json()["pair"] == "XRP/USDT:USDT"
            assert response.json()["worker"] == f"{exchange}-exact-worker"

    asyncio.run(scenario())

    assert fixture.write_count == 1
    with database.session_factory() as session:
        intent = session.get(OrderIntent, ids["opening"])
        protection = session.scalar(select(ProtectionOrder))
        assert intent is not None and intent.status == "FILLED"
        assert protection is not None and protection.status == "ACTIVE"
        assert protection.venue_order_id == f"{exchange}-stop-1"


def test_hyperliquid_live_flow_is_gated_idempotent_fenced_and_cleans_protection(
    database: Database,
) -> None:
    asyncio.run(exercise_hyperliquid_live(database))


def test_perptape_live_candidate_runs_open_add_reduce_and_exit_through_binance_adapter(
    database: Database,
) -> None:
    asyncio.run(exercise_perptape_binance_live_lifecycle(database))


@pytest.mark.parametrize(
    ("venue", "code"),
    [
        ("BINANCE", "BINANCE_LIVE_REJECTED"),
        ("BINANCE", "BINANCE_LIVE_OUTCOME_UNKNOWN"),
        ("HYPERLIQUID", "HYPERLIQUID_LIVE_REJECTED"),
        ("HYPERLIQUID", "HYPERLIQUID_LIVE_OUTCOME_UNKNOWN"),
    ],
)
def test_live_send_persists_explicit_rejection_or_unknown(
    database: Database, venue: str, code: str
) -> None:
    asyncio.run(exercise_failed_live_send(database, venue=venue, code=code))


@pytest.mark.parametrize("venue", ["BINANCE", "HYPERLIQUID"])
def test_live_open_order_can_be_cancelled(database: Database, venue: str) -> None:
    asyncio.run(exercise_live_cancel(database, venue=venue))


@pytest.mark.parametrize("venue", ["BINANCE", "HYPERLIQUID"])
def test_live_unknown_order_can_be_recovered(database: Database, venue: str) -> None:
    asyncio.run(exercise_live_recovery(database, venue=venue))


@pytest.mark.parametrize("venue", ["BINANCE", "HYPERLIQUID"])
@pytest.mark.parametrize("unknown", [False, True])
def test_live_cancel_not_found_or_unknown_freezes_intent(
    database: Database, venue: str, unknown: bool
) -> None:
    asyncio.run(exercise_live_cancel_outcome(database, venue=venue, unknown=unknown))


@pytest.mark.parametrize("venue", ["BINANCE", "HYPERLIQUID"])
def test_live_protection_unknown_is_persisted(database: Database, venue: str) -> None:
    asyncio.run(exercise_unknown_live_protection(database, venue=venue))
