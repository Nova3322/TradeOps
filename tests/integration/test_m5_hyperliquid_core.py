from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    CampaignStatus,
    Direction,
    DomainRejected,
    ExecutionEnvironment,
    IntentKind,
    OrderIntentStatus,
    ProposalSource,
    ReservationStatus,
    ReviewDecision,
    RiskTier,
    Role,
    SystemRiskState,
    TargetCandidate,
    TargetUrgency,
)
from trading_control_plane.hyperliquid import (
    HyperliquidEquity,
    HyperliquidFill,
    HyperliquidFunding,
    HyperliquidInstrument,
    HyperliquidOrder,
    HyperliquidPosition,
    HyperliquidProtection,
    HyperliquidReadOnlySnapshot,
)
from trading_control_plane.hyperliquid_execution import HyperliquidTestnetClient
from trading_control_plane.models import Campaign, OrderIntent, RiskReservation, VenueOrder
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.service import TradingService

ACCOUNT_ADDRESS = "0x1111111111111111111111111111111111111111"
ACCOUNT_ID = "acct-hyperliquid"
SCOPE = f"TESTNET:{ACCOUNT_ID}:HYPERLIQUID"


class SimulatedHyperliquidCore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self.next_order_id = 700
        self.unknown_after_next_exchange = False

    def __call__(
        self, url: str, payload: dict[str, Any], _timeout: float
    ) -> dict[str, Any] | list[Any]:
        self.calls.append((url, payload))
        if url.endswith("/info"):
            if payload["type"] == "metaAndAssetCtxs":
                return [
                    {"universe": [{"name": "BTC", "szDecimals": 5}]},
                    [{"markPx": "100"}],
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
        self.next_order_id += 1
        cloid = str(item["c"])
        trigger = "trigger" in item["t"]
        status = "open" if trigger else "filled"
        order = {
            "coin": "BTC",
            "oid": self.next_order_id,
            "cloid": cloid,
            "side": "B" if item["b"] else "A",
            "limitPx": item["p"],
            "sz": item["s"] if trigger else "0",
            "origSz": item["s"],
            "timestamp": int(datetime.now(UTC).timestamp() * 1_000),
            "triggerPx": item["t"].get("trigger", {}).get("triggerPx", "0"),
            "isTrigger": trigger,
            "reduceOnly": item["r"],
        }
        self.orders[cloid] = {
            "status": "order",
            "order": {
                "order": order,
                "status": status,
                "statusTimestamp": int(datetime.now(UTC).timestamp() * 1_000),
            },
        }
        if self.unknown_after_next_exchange:
            self.unknown_after_next_exchange = False
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_OUTCOME_UNKNOWN",
                "fixture accepted the action before timeout",
            )
        acknowledgement = (
            {"resting": {"oid": self.next_order_id}}
            if trigger
            else {
                "filled": {
                    "totalSz": item["s"],
                    "avgPx": item["p"],
                    "oid": self.next_order_id,
                }
            }
        )
        return {
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [acknowledgement]}},
        }

    @property
    def order_exchange_count(self) -> int:
        return sum(
            url.endswith("/exchange") and payload["action"]["type"] == "order"
            for url, payload in self.calls
        )


class MutableHyperliquidReader:
    configured = True
    fact_environment = "TESTNET"

    def __init__(self) -> None:
        self.snapshot: HyperliquidReadOnlySnapshot | None = None

    def read_snapshot(self, symbol: str, *, now: datetime) -> HyperliquidReadOnlySnapshot:
        del symbol, now
        assert self.snapshot is not None
        return self.snapshot


def seed(service: TradingService, *, key: str) -> dict[str, UUID]:
    now = datetime.now(UTC)
    admin = service.bootstrap_admin(f"{key}-admin", now=now)
    proposer = service.create_user(f"{key}-proposer", admin, now=now)
    reviewer_one = service.create_user(f"{key}-reviewer-1", admin, now=now)
    reviewer_two = service.create_user(f"{key}-reviewer-2", admin, now=now)
    operator = service.create_user(f"{key}-operator", admin, now=now)
    for user_id, role in (
        (proposer, Role.PROPOSER),
        (reviewer_one, Role.REVIEWER),
        (reviewer_two, Role.REVIEWER),
        (operator, Role.OPERATOR),
    ):
        service.assign_role(user_id, role, admin, ACCOUNT_ID, "HYPERLIQUID", now=now)
    instrument = service.register_instrument(
        actor_id=admin,
        venue="HYPERLIQUID",
        symbol="BTC",
        tick_size=Decimal("0.1"),
        lot_size=Decimal("0.00001"),
        minimum_notional=Decimal(10),
        contract_multiplier=Decimal(1),
        quote_currency="USDC",
        collateral_currency="USDC",
        protection_supported=True,
        now=now,
    )
    service.set_risk_policy(
        actor_id=admin,
        version=f"{key}-risk-v1",
        system_state=SystemRiskState.NORMAL,
        max_total_risk=Decimal(100),
        max_fact_age=timedelta(minutes=10),
        now=now,
    )
    service.record_position(
        ACCOUNT_ID,
        "HYPERLIQUID",
        instrument,
        Decimal(0),
        Decimal(0),
        Decimal(100),
        True,
        operator,
        environment=ExecutionEnvironment.TESTNET,
        now=now,
    )
    service.record_account_equity(
        ACCOUNT_ID,
        "HYPERLIQUID",
        Decimal(10_000),
        Decimal(9_000),
        "USDC",
        True,
        operator,
        environment=ExecutionEnvironment.TESTNET,
        now=now,
    )
    proposal = service.create_proposal(
        actor_id=proposer,
        source=ProposalSource.MANUAL,
        risk_tier=RiskTier.HIGH,
        account_id=ACCOUNT_ID,
        venue="HYPERLIQUID",
        instrument_id=instrument,
        direction=Direction.LONG,
        quantity=Decimal(1),
        max_risk=Decimal(40),
        expires_at=now + timedelta(hours=2),
        idempotency_key=f"{key}-proposal",
        environment=ExecutionEnvironment.TESTNET,
        details={"limit_price": "100", "invalidation_price": "95"},
        now=now,
    )
    service.submit_proposal(proposal, proposer, now=now)
    service.review_proposal(proposal, reviewer_one, ReviewDecision.APPROVE, "first", now=now)
    service.review_proposal(proposal, reviewer_two, ReviewDecision.APPROVE, "second", now=now)
    service.decide_risk(
        proposal_id=proposal,
        actor_id=operator,
        kind=IntentKind.INITIAL,
        idempotency_key=f"{key}-risk",
        now=now,
    )
    authorization = service.issue_authorization(
        proposal_id=proposal,
        actor_id=operator,
        expires_at=now + timedelta(minutes=30),
        allowed_adds=0,
        idempotency_key=f"{key}-authorization",
        now=now,
    )
    opening = service.create_order_intent(
        authorization,
        operator,
        IntentKind.INITIAL,
        ACCOUNT_ID,
        "HYPERLIQUID",
        instrument,
        Direction.LONG,
        Decimal(1),
        f"{key}-opening",
        now=now,
    )
    return {
        "operator": operator,
        "instrument": instrument,
        "campaign": opening.campaign_id,
        "reservation": opening.reservation_id,
        "opening": opening.intent_id,
    }


def app(
    database: Database,
    venue: SimulatedHyperliquidCore,
    reader: MutableHyperliquidReader,
    *,
    enabled: bool = True,
) -> FastAPI:
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="m5-test-signing-secret-that-is-long-enough",  # noqa: S106
        public_base_url="http://test",
        hyperliquid_read_only_enabled=True,
        hyperliquid_fact_environment="TESTNET",
        hyperliquid_account_address=ACCOUNT_ADDRESS,
        hyperliquid_testnet_order_send_enabled=enabled,
        hyperliquid_testnet_api_wallet_private_key="0x"
        "1111111111111111111111111111111111111111111111111111111111111111",
        _env_file=None,
    )
    execution = HyperliquidTestnetClient(
        base_url="https://api.hyperliquid-testnet.xyz",
        account_address=ACCOUNT_ADDRESS,
        signer=lambda _action, _nonce: {"r": "0x01", "s": "0x02", "v": 27},
        requester=venue,
    )
    perptape = PerptapeClient(
        base_url="https://perptape.invalid",
        api_key=None,
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
    )
    return create_app(
        settings,
        database,
        perptape,
        hyperliquid_client=reader,  # type: ignore[arg-type]
        hyperliquid_testnet_client=execution,
    )


def snapshot(
    now: datetime,
    *,
    orders: tuple[HyperliquidOrder, ...],
    fills: tuple[HyperliquidFill, ...],
    quantity: Decimal,
    entry: Decimal,
    mark: Decimal,
    protection: HyperliquidProtection | None,
    funding: tuple[HyperliquidFunding, ...] = (),
) -> HyperliquidReadOnlySnapshot:
    return HyperliquidReadOnlySnapshot(
        symbol="BTC",
        observed_at=now,
        instrument=HyperliquidInstrument(
            "BTC", Decimal("0.1"), Decimal("0.00001"), Decimal(10), "USDC", "USDC", True
        ),
        orders=orders,
        fills=fills,
        position=HyperliquidPosition(quantity, entry, mark, now),
        equity=HyperliquidEquity(Decimal(10_010), Decimal(9_000), "USDC", now),
        funding=funding,
        protection=protection,
    )


async def login(http: AsyncClient, username: str) -> None:
    response = await http.post("/api/auth/mock/login", json={"username": username})
    assert response.status_code == 200, response.text


async def lease(http: AsyncClient, owner: str) -> dict[str, Any]:
    response = await http.post(
        "/api/sender-leases",
        json={"execution_scope": SCOPE, "owner_id": owner, "lease_seconds": 300},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def complete_flow(database: Database) -> None:
    service = TradingService(database)
    ids = seed(service, key="m5")
    venue = SimulatedHyperliquidCore()
    reader = MutableHyperliquidReader()
    async with AsyncClient(
        transport=ASGITransport(app=app(database, venue, reader)), base_url="http://test"
    ) as http:
        await login(http, "m5-operator")
        status = await http.get("/api/venues/hyperliquid/testnet/status")
        assert status.status_code == 200
        assert status.json()["domain"] == "CORE"
        assert status.json()["hip3_available"] is False
        sender = await lease(http, "m5-worker")
        action = {
            "execution_scope": sender["execution_scope"],
            "owner_id": sender["owner_id"],
            "fencing_token": sender["fencing_token"],
        }

        sent = await http.post(
            f"/api/intents/{ids['opening']}/hyperliquid-testnet/send", json=action
        )
        assert sent.status_code == 200, sent.text
        opening_cloid = sent.json()["client_order_id"]
        assert opening_cloid == f"0x{ids['opening'].hex}"
        duplicate = await http.post(
            f"/api/intents/{ids['opening']}/hyperliquid-testnet/send", json=action
        )
        assert duplicate.status_code == 200
        assert venue.order_exchange_count == 1
        opening_order_id = str(venue.orders[opening_cloid]["order"]["order"]["oid"])

        observed = datetime.now(UTC)
        opening_fill = HyperliquidFill(
            "open-fill",
            opening_order_id,
            "BUY",
            Decimal(1),
            Decimal(100),
            Decimal("0.4"),
            "USDC",
            observed,
        )
        reader.snapshot = snapshot(
            observed,
            orders=(),
            fills=(opening_fill,),
            quantity=Decimal(1),
            entry=Decimal(100),
            mark=Decimal(105),
            protection=None,
        )
        first_sync = await http.post(
            "/api/venues/hyperliquid/sync",
            json={"account_id": ACCOUNT_ID, "symbol": "BTC"},
        )
        assert first_sync.status_code == 200, first_sync.text
        assert first_sync.json()["reconciliation"]["status"] == "UNKNOWN"

        protected = await http.post(
            f"/api/campaigns/{ids['campaign']}/hyperliquid-testnet/protection",
            json={**action, "trigger_price": "95", "limit_price": "94"},
        )
        assert protected.status_code == 200, protected.text
        protection_cloid = protected.json()["client_order_id"]
        protection_order_id = str(venue.orders[protection_cloid]["order"]["order"]["oid"])
        protected_at = datetime.now(UTC)
        protection_order = HyperliquidOrder(
            protection_order_id,
            protection_cloid,
            "SENT",
            "SELL",
            "TRIGGER_MARKET",
            Decimal(1),
            Decimal(0),
            Decimal(95),
            True,
            False,
            protected_at,
        )
        reader.snapshot = snapshot(
            protected_at,
            orders=(protection_order,),
            fills=(opening_fill,),
            quantity=Decimal(1),
            entry=Decimal(100),
            mark=Decimal(105),
            protection=HyperliquidProtection(
                protection_order_id, Decimal(1), Decimal(95), protected_at
            ),
            funding=(HyperliquidFunding("funding-1", Decimal("-0.5"), "USDC", protected_at),),
        )
        protected_sync = await http.post(
            "/api/venues/hyperliquid/sync",
            json={"account_id": ACCOUNT_ID, "symbol": "BTC"},
        )
        assert protected_sync.status_code == 200, protected_sync.text
        assert protected_sync.json()["facts"]["positions"][0]["protection"]["fully_covered"]

        service.update_campaign_target(
            ids["campaign"],
            ids["operator"],
            (TargetCandidate(Decimal(0), TargetUrgency.IMMEDIATE, "exit Core testnet"),),
            now=datetime.now(UTC),
        )
        exit_intent = service.create_reduction_intent(
            ids["campaign"],
            ids["operator"],
            "m5-exit",
            limit_price=Decimal(110),
            now=datetime.now(UTC),
        )
        exit_sent = await http.post(
            f"/api/intents/{exit_intent}/hyperliquid-testnet/send", json=action
        )
        assert exit_sent.status_code == 200, exit_sent.text
        exit_cloid = exit_sent.json()["client_order_id"]
        exit_order_id = str(venue.orders[exit_cloid]["order"]["order"]["oid"])
        exited_at = datetime.now(UTC)
        exit_fill = HyperliquidFill(
            "exit-fill",
            exit_order_id,
            "SELL",
            Decimal(1),
            Decimal(110),
            Decimal(1),
            "USDC",
            exited_at,
        )
        reader.snapshot = snapshot(
            exited_at,
            orders=(),
            fills=(opening_fill, exit_fill),
            quantity=Decimal(0),
            entry=Decimal(0),
            mark=Decimal(110),
            protection=None,
            funding=(HyperliquidFunding("funding-1", Decimal("-0.5"), "USDC", protected_at),),
        )
        final_sync = await http.post(
            "/api/venues/hyperliquid/sync",
            json={"account_id": ACCOUNT_ID, "symbol": "BTC"},
        )
        assert final_sync.status_code == 200, final_sync.text
        assert final_sync.json()["reconciliation"]["status"] == "MATCH"

    pnl = service.refresh_campaign_pnl(ids["campaign"], ids["operator"], now=datetime.now(UTC))
    assert pnl.total_pnl == Decimal("8.1")
    service.close_campaign(ids["campaign"], ids["operator"], now=datetime.now(UTC))
    with database.session_factory() as session:
        campaign = session.get(Campaign, ids["campaign"])
        reservation = session.get(RiskReservation, ids["reservation"])
        assert campaign is not None and campaign.status == CampaignStatus.CLOSED.value
        assert campaign.final_pnl == Decimal("8.1")
        assert reservation is not None and reservation.status == ReservationStatus.RELEASED.value


def test_core_program_owns_proposal_to_ioc_protection_reconcile_exit_and_pnl(
    database: Database,
) -> None:
    asyncio.run(complete_flow(database))


async def unknown_and_fencing(database: Database) -> None:
    service = TradingService(database)
    ids = seed(service, key="m5-recovery")
    venue = SimulatedHyperliquidCore()
    venue.unknown_after_next_exchange = True
    reader = MutableHyperliquidReader()
    async with AsyncClient(
        transport=ASGITransport(app=app(database, venue, reader)), base_url="http://test"
    ) as http:
        await login(http, "m5-recovery-operator")
        first = await lease(http, "old-worker")
        stale = await http.post(
            f"/api/intents/{ids['opening']}/hyperliquid-testnet/send",
            json={
                "execution_scope": SCOPE,
                "owner_id": first["owner_id"],
                "fencing_token": first["fencing_token"] + 1,
            },
        )
        assert stale.status_code == 422
        assert stale.json()["error"]["code"] == "FENCING_TOKEN_REJECTED"
        assert venue.calls == []
        action = {
            "execution_scope": SCOPE,
            "owner_id": first["owner_id"],
            "fencing_token": first["fencing_token"],
        }
        unknown = await http.post(
            f"/api/intents/{ids['opening']}/hyperliquid-testnet/send", json=action
        )
        assert unknown.status_code == 503, unknown.text

    with database.session_factory() as session:
        intent = session.get(OrderIntent, ids["opening"])
        reservation = session.get(RiskReservation, ids["reservation"])
        order = session.scalar(
            select(VenueOrder).where(VenueOrder.order_intent_id == ids["opening"])
        )
        assert intent is not None and intent.status == OrderIntentStatus.UNKNOWN.value
        assert reservation is not None and reservation.status == ReservationStatus.UNKNOWN.value
        assert order is not None and order.status == "UNKNOWN"

    async with AsyncClient(
        transport=ASGITransport(app=app(database, venue, reader)), base_url="http://test"
    ) as restarted:
        await login(restarted, "m5-recovery-operator")
        current = await lease(restarted, "old-worker")
        recovered = await restarted.post(
            f"/api/intents/{ids['opening']}/hyperliquid-testnet/recover",
            json={
                "execution_scope": SCOPE,
                "owner_id": current["owner_id"],
                "fencing_token": current["fencing_token"],
            },
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["recovered"] is True
    assert venue.order_exchange_count == 1
    with database.session_factory() as session:
        intent = session.get(OrderIntent, ids["opening"])
        reservation = session.get(RiskReservation, ids["reservation"])
        assert intent is not None and intent.status == OrderIntentStatus.FILLED.value
        assert reservation is not None and reservation.status == ReservationStatus.OPEN.value


def test_old_fencing_token_never_reaches_venue_and_unknown_recovers_without_resend(
    database: Database,
) -> None:
    asyncio.run(unknown_and_fencing(database))
