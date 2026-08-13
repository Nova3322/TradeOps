from __future__ import annotations

import asyncio
import base64
import urllib.parse
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from conftest import set_test_team_environment
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from trading_control_plane.api import create_app
from trading_control_plane.binance import (
    BinanceEquity,
    BinanceFill,
    BinanceFunding,
    BinanceInstrument,
    BinanceOrder,
    BinancePosition,
    BinanceProtection,
    BinanceReadOnlySnapshot,
)
from trading_control_plane.binance_execution import BinanceTestnetClient
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
    ReconciliationStatus,
    ReservationStatus,
    ReviewDecision,
    RiskTier,
    Role,
    SystemRiskState,
    TargetCandidate,
    TargetUrgency,
    VenueOrderStatus,
)
from trading_control_plane.models import (
    Campaign,
    OrderIntent,
    RiskReservation,
    VenueFill,
    VenueOrder,
)
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.service import TradingService


class SimulatedTestnetVenue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self.next_order_id = 400
        self.unknown_after_next_post = False
        self.reject_next_post = False

    def __call__(
        self, method: str, url: str, _headers: dict[str, str], _timeout: float
    ) -> dict[str, Any]:
        self.calls.append((method, url))
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        client_id = query.get("origClientOrderId") or query.get("newClientOrderId")
        assert client_id is not None
        if method == "GET":
            return self.orders.get(client_id, {"code": -2013, "msg": "Order not found"})
        if method == "DELETE":
            order = self.orders[client_id]
            order["status"] = "CANCELED"
            order["updateTime"] = int(datetime.now(UTC).timestamp() * 1_000)
            return order
        if self.reject_next_post:
            self.reject_next_post = False
            return {"code": -2010, "msg": "Rejected by testnet fixture"}
        self.next_order_id += 1
        protection = query["type"] == "STOP_MARKET"
        order = {
            "symbol": query["symbol"],
            "orderId": self.next_order_id,
            "clientOrderId": client_id,
            "status": "NEW",
            "side": query["side"],
            "type": query["type"],
            "origQty": "0" if protection else query["quantity"],
            "executedQty": "0",
            "stopPrice": query.get("stopPrice", "0"),
            "reduceOnly": query.get("reduceOnly") == "true",
            "closePosition": query.get("closePosition") == "true",
            "updateTime": int(datetime.now(UTC).timestamp() * 1_000),
        }
        self.orders[client_id] = order
        if self.unknown_after_next_post:
            self.unknown_after_next_post = False
            raise DomainRejected(
                "BINANCE_TESTNET_OUTCOME_UNKNOWN", "fixture accepted the order then timed out"
            )
        return order

    @property
    def post_count(self) -> int:
        return sum(method == "POST" for method, _url in self.calls)


class MutableTestnetReader:
    configured = True

    def __init__(self) -> None:
        self.snapshot: BinanceReadOnlySnapshot | None = None

    def read_snapshot(self, symbol: str, *, now: datetime) -> BinanceReadOnlySnapshot:
        del symbol, now
        assert self.snapshot is not None
        return self.snapshot


def seed_testnet(service: TradingService, *, key: str = "m4") -> dict[str, UUID]:
    now = datetime.now(UTC)
    admin = service.bootstrap_admin(f"{key}-admin", now=now)
    set_test_team_environment(service.database, admin, "TESTNET")
    service.create_exchange_account(
        actor_id=admin,
        environment=ExecutionEnvironment.TESTNET,
        account_id="acct-testnet",
        venue="BINANCE",
        label="Binance Testnet",
        credentials={"api_key": "testnet-key", "api_secret": "testnet-secret"},
        idempotency_key=f"{key}-testnet-account",
        now=now,
    )
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
        service.assign_role(user_id, role, admin, "acct-testnet", "BINANCE", now=now)
    instrument = service.register_instrument(
        actor_id=admin,
        venue="BINANCE",
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        lot_size=Decimal("0.001"),
        minimum_notional=Decimal("5"),
        contract_multiplier=Decimal(1),
        quote_currency="USDT",
        collateral_currency="USDT",
        protection_supported=True,
        now=now,
    )
    service.set_risk_policy(
        actor_id=admin,
        version=f"{key}-risk-v1",
        system_state=SystemRiskState.NORMAL,
        max_total_risk=Decimal("100"),
        max_account_risk=Decimal("100"),
        max_single_loss=Decimal("100"),
        max_consecutive_losses=3,
        loss_cooldown=timedelta(hours=1),
        max_fact_age=timedelta(minutes=10),
        now=now,
    )
    service.record_position(
        "acct-testnet",
        "BINANCE",
        instrument,
        Decimal(0),
        Decimal(0),
        Decimal("100"),
        True,
        operator,
        environment=ExecutionEnvironment.TESTNET,
        now=now,
    )
    service.record_account_equity(
        "acct-testnet",
        "BINANCE",
        Decimal("10000"),
        Decimal("9000"),
        "USDT",
        True,
        operator,
        environment=ExecutionEnvironment.TESTNET,
        now=now,
    )
    proposal = service.create_proposal(
        actor_id=proposer,
        source=ProposalSource.MANUAL,
        risk_tier=RiskTier.HIGH,
        account_id="acct-testnet",
        venue="BINANCE",
        instrument_id=instrument,
        direction=Direction.LONG,
        quantity=Decimal(1),
        max_risk=Decimal(40),
        expires_at=now + timedelta(hours=2),
        idempotency_key=f"{key}-proposal",
        environment=ExecutionEnvironment.TESTNET,
        now=now,
    )
    service.submit_proposal(proposal, proposer, now=now)
    service.review_proposal(proposal, reviewer_one, ReviewDecision.APPROVE, "first review", now=now)
    service.review_proposal(
        proposal, reviewer_two, ReviewDecision.APPROVE, "second review", now=now
    )
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
        "acct-testnet",
        "BINANCE",
        instrument,
        Direction.LONG,
        Decimal(1),
        f"{key}-opening",
        now=now,
    )
    return {
        "admin": admin,
        "operator": operator,
        "instrument": instrument,
        "proposal": proposal,
        "authorization": authorization,
        "campaign": opening.campaign_id,
        "reservation": opening.reservation_id,
        "opening": opening.intent_id,
    }


def build_testnet_app(
    database: Database,
    venue: SimulatedTestnetVenue,
    reader: MutableTestnetReader,
    *,
    enabled: bool = True,
) -> FastAPI:
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="m4-test-signing-secret-that-is-long-enough",  # noqa: S106
        public_base_url="http://test",
        execution_backend="DIRECT_LEGACY",
        binance_testnet_order_send_enabled=enabled,
        binance_testnet_api_key="fixture-testnet-key",
        binance_testnet_api_secret="fixture-testnet-secret",  # noqa: S106
        _env_file=None,
    )
    execution = BinanceTestnetClient(
        base_url="https://testnet.binancefuture.com",
        api_key="fixture-testnet-key",
        api_secret="fixture-testnet-secret",  # noqa: S106
        requester=venue,
    )
    perptape = PerptapeClient(
        base_url="https://perptape.com",
        api_key=None,
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
    )
    return create_app(
        settings,
        database,
        perptape,
        binance_testnet_client=execution,
        binance_testnet_reader=reader,  # type: ignore[arg-type]
    )


async def login(http: AsyncClient, username: str) -> None:
    response = await http.post("/api/auth/mock/login", json={"username": username})
    assert response.status_code == 200, response.text


async def acquire_sender(http: AsyncClient, owner: str = "m4-worker") -> dict[str, Any]:
    response = await http.post(
        "/api/sender-leases",
        json={
            "execution_scope": "TESTNET:acct-testnet:BINANCE",
            "owner_id": owner,
            "lease_seconds": 300,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def order_fact(
    order_id: str,
    client_id: str,
    status: str,
    side: str,
    quantity: Decimal,
    filled: Decimal,
    now: datetime,
    *,
    order_type: str = "MARKET",
    stop_price: Decimal = Decimal(0),
    reduce_only: bool = False,
    close_position: bool = False,
) -> BinanceOrder:
    return BinanceOrder(
        order_id=order_id,
        client_order_id=client_id,
        status=status,
        side=side,
        order_type=order_type,
        ordered_quantity=quantity,
        filled_quantity=filled,
        stop_price=stop_price,
        reduce_only=reduce_only,
        close_position=close_position,
        observed_at=now,
    )


def snapshot(
    now: datetime,
    *,
    orders: tuple[BinanceOrder, ...],
    fills: tuple[BinanceFill, ...],
    quantity: Decimal,
    entry: Decimal,
    mark: Decimal,
    protection: BinanceProtection | None,
    funding: tuple[BinanceFunding, ...] = (),
) -> BinanceReadOnlySnapshot:
    return BinanceReadOnlySnapshot(
        symbol="BTCUSDT",
        observed_at=now,
        instrument=BinanceInstrument(
            "BTCUSDT",
            Decimal("0.1"),
            Decimal("0.001"),
            Decimal("5"),
            "USDT",
            "USDT",
            True,
        ),
        orders=orders,
        fills=fills,
        position=BinancePosition(quantity, entry, mark, now),
        equity=BinanceEquity(Decimal("10010"), Decimal("9000"), "USDT", now),
        funding=funding,
        protection=protection,
    )


async def run_complete_testnet_flow(database: Database) -> None:
    service = TradingService(
        database,
        credential_encryption_key=base64.urlsafe_b64encode(b"testnet-flow-key-32-bytes-long!!"[:32])
        .decode()
        .rstrip("="),
    )
    ids = seed_testnet(service)
    venue = SimulatedTestnetVenue()
    reader = MutableTestnetReader()
    app = build_testnet_app(database, venue, reader)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        await login(http, "m4-operator")
        status = await http.get("/api/venues/binance/testnet/status")
        assert status.status_code == 200
        assert status.json()["environment"] == "TESTNET"
        assert status.json()["live_order_send"] is False
        lease = await acquire_sender(http)
        action = {
            "execution_scope": lease["execution_scope"],
            "owner_id": lease["owner_id"],
            "fencing_token": lease["fencing_token"],
        }

        sent = await http.post(f"/api/intents/{ids['opening']}/binance-testnet/send", json=action)
        assert sent.status_code == 200, sent.text
        opening_client_id = sent.json()["client_order_id"]
        assert opening_client_id.startswith("tcp-")
        assert len(opening_client_id) <= 32
        duplicate = await http.post(
            f"/api/intents/{ids['opening']}/binance-testnet/send", json=action
        )
        assert duplicate.status_code == 200, duplicate.text
        assert venue.post_count == 1
        opening_order_id = str(next(iter(venue.orders.values()))["orderId"])

        partial_time = datetime.now(UTC)
        partial_fill = BinanceFill(
            "open-fill-1",
            opening_order_id,
            "BUY",
            Decimal("0.4"),
            Decimal("100"),
            Decimal("0.4"),
            "USDT",
            partial_time,
        )
        reader.snapshot = snapshot(
            partial_time,
            orders=(
                order_fact(
                    opening_order_id,
                    opening_client_id,
                    "PARTIALLY_FILLED",
                    "BUY",
                    Decimal(1),
                    Decimal("0.4"),
                    partial_time,
                ),
            ),
            fills=(partial_fill,),
            quantity=Decimal("0.4"),
            entry=Decimal("100"),
            mark=Decimal("105"),
            protection=None,
        )
        partial_sync = await http.post(
            "/api/venues/binance/testnet/sync",
            json={"account_id": "acct-testnet", "symbol": "BTCUSDT"},
        )
        assert partial_sync.status_code == 200, partial_sync.text
        assert partial_sync.json()["reconciliation"]["status"] == "UNKNOWN"
        assert partial_sync.json()["facts"]["orders"][0]["client_order_id"] == opening_client_id

        protected = await http.post(
            f"/api/campaigns/{ids['campaign']}/binance-testnet/protection",
            json={**action, "trigger_price": "95"},
        )
        assert protected.status_code == 200, protected.text
        protection_client_id = protected.json()["client_order_id"]
        protection_order = venue.orders[protection_client_id]
        protection_order_id = str(protection_order["orderId"])

        filled_time = datetime.now(UTC)
        reader.snapshot = snapshot(
            filled_time,
            orders=(
                order_fact(
                    protection_order_id,
                    protection_client_id,
                    "SENT",
                    "SELL",
                    Decimal(0),
                    Decimal(0),
                    filled_time,
                    order_type="STOP_MARKET",
                    stop_price=Decimal("95"),
                    close_position=True,
                ),
            ),
            fills=(
                partial_fill,
                BinanceFill(
                    "open-fill-2",
                    opening_order_id,
                    "BUY",
                    Decimal("0.6"),
                    Decimal("100"),
                    Decimal("0.6"),
                    "USDT",
                    filled_time,
                ),
            ),
            quantity=Decimal(1),
            entry=Decimal("100"),
            mark=Decimal("105"),
            protection=BinanceProtection(
                protection_order_id, Decimal(1), Decimal("95"), filled_time
            ),
            funding=(BinanceFunding("funding-1", Decimal("-0.5"), "USDT", filled_time),),
        )
        filled_sync = await http.post(
            "/api/venues/binance/testnet/sync",
            json={"account_id": "acct-testnet", "symbol": "BTCUSDT"},
        )
        assert filled_sync.status_code == 200, filled_sync.text
        detail = filled_sync.json()["facts"]
        assert detail["positions"][0]["protection"]["fully_covered"] is True

        service.update_campaign_target(
            ids["campaign"],
            ids["operator"],
            (TargetCandidate(Decimal(0), TargetUrgency.IMMEDIATE, "exit testnet"),),
            now=datetime.now(UTC),
        )
        exit_intent = service.create_reduction_intent(
            ids["campaign"], ids["operator"], "m4-testnet-exit", now=datetime.now(UTC)
        )
        exit_sent = await http.post(f"/api/intents/{exit_intent}/binance-testnet/send", json=action)
        assert exit_sent.status_code == 200, exit_sent.text
        exit_client_id = exit_sent.json()["client_order_id"]
        exit_order_id = str(venue.orders[exit_client_id]["orderId"])

        exit_time = datetime.now(UTC)
        reader.snapshot = snapshot(
            exit_time,
            orders=(),
            fills=(
                partial_fill,
                BinanceFill(
                    "open-fill-2",
                    opening_order_id,
                    "BUY",
                    Decimal("0.6"),
                    Decimal("100"),
                    Decimal("0.6"),
                    "USDT",
                    filled_time,
                ),
                BinanceFill(
                    "exit-fill-1",
                    exit_order_id,
                    "SELL",
                    Decimal(1),
                    Decimal("110"),
                    Decimal(1),
                    "USDT",
                    exit_time,
                ),
            ),
            quantity=Decimal(0),
            entry=Decimal(0),
            mark=Decimal("110"),
            protection=None,
            funding=(BinanceFunding("funding-1", Decimal("-0.5"), "USDT", filled_time),),
        )
        exit_sync = await http.post(
            "/api/venues/binance/testnet/sync",
            json={"account_id": "acct-testnet", "symbol": "BTCUSDT"},
        )
        assert exit_sync.status_code == 200, exit_sync.text
        assert exit_sync.json()["reconciliation"]["status"] == "MATCH"
        assert exit_sync.json()["facts"]["positions"][0]["protection"] is None

    pnl = service.refresh_campaign_pnl(ids["campaign"], ids["operator"], now=datetime.now(UTC))
    assert pnl.total_pnl == Decimal("7.5")
    service.close_campaign(ids["campaign"], ids["operator"], now=datetime.now(UTC))
    with database.session_factory() as session:
        campaign = session.get(Campaign, ids["campaign"])
        opening = session.get(OrderIntent, ids["opening"])
        exit_order = session.get(OrderIntent, exit_intent)
        reservation = session.get(RiskReservation, ids["reservation"])
        assert campaign is not None and campaign.status == CampaignStatus.CLOSED.value
        assert campaign.final_pnl == Decimal("7.5")
        assert opening is not None and opening.status == OrderIntentStatus.FILLED.value
        assert exit_order is not None and exit_order.status == OrderIntentStatus.FILLED.value
        assert reservation is not None and reservation.status == ReservationStatus.RELEASED.value


def test_controlled_testnet_flow_handles_partial_protection_exit_reconcile_and_pnl(
    database: Database,
) -> None:
    asyncio.run(run_complete_testnet_flow(database))


async def run_unknown_recovery(database: Database) -> None:
    service = TradingService(
        database,
        credential_encryption_key=base64.urlsafe_b64encode(b"testnet-flow-key-32-bytes-long!!"[:32])
        .decode()
        .rstrip("="),
    )
    ids = seed_testnet(service, key="m4-recovery")
    venue = SimulatedTestnetVenue()
    venue.unknown_after_next_post = True
    reader = MutableTestnetReader()
    async with AsyncClient(
        transport=ASGITransport(app=build_testnet_app(database, venue, reader)),
        base_url="http://test",
    ) as http:
        await login(http, "m4-recovery-operator")
        lease = await acquire_sender(http, "restart-worker")
        action = {
            "execution_scope": lease["execution_scope"],
            "owner_id": lease["owner_id"],
            "fencing_token": lease["fencing_token"],
        }
        unknown = await http.post(
            f"/api/intents/{ids['opening']}/binance-testnet/send", json=action
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

    # A fresh application/client instance queries the stable identity. It does not POST again.
    async with AsyncClient(
        transport=ASGITransport(app=build_testnet_app(database, venue, reader)),
        base_url="http://test",
    ) as restarted:
        await login(restarted, "m4-recovery-operator")
        lease = await acquire_sender(restarted, "restart-worker")
        recovered = await restarted.post(
            f"/api/intents/{ids['opening']}/binance-testnet/recover",
            json={
                "execution_scope": lease["execution_scope"],
                "owner_id": lease["owner_id"],
                "fencing_token": lease["fencing_token"],
            },
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["recovered"] is True
    assert venue.post_count == 1
    with database.session_factory() as session:
        intent = session.get(OrderIntent, ids["opening"])
        reservation = session.get(RiskReservation, ids["reservation"])
        assert intent is not None and intent.status == OrderIntentStatus.SENT.value
        assert reservation is not None and reservation.status == ReservationStatus.RESERVED.value


def test_unknown_keeps_risk_and_restart_recovers_by_query_without_resend(
    database: Database,
) -> None:
    asyncio.run(run_unknown_recovery(database))


async def run_cancel_and_fencing(database: Database) -> None:
    service = TradingService(
        database,
        credential_encryption_key=base64.urlsafe_b64encode(b"testnet-flow-key-32-bytes-long!!"[:32])
        .decode()
        .rstrip("="),
    )
    ids = seed_testnet(service, key="m4-cancel")
    venue = SimulatedTestnetVenue()
    reader = MutableTestnetReader()
    async with AsyncClient(
        transport=ASGITransport(app=build_testnet_app(database, venue, reader)),
        base_url="http://test",
    ) as http:
        await login(http, "m4-cancel-operator")
        lease = await acquire_sender(http, "cancel-worker")
        action = {
            "execution_scope": lease["execution_scope"],
            "owner_id": lease["owner_id"],
            "fencing_token": lease["fencing_token"],
        }
        sent = await http.post(f"/api/intents/{ids['opening']}/binance-testnet/send", json=action)
        assert sent.status_code == 200, sent.text
        calls_before = len(venue.calls)
        stale = await http.post(
            f"/api/intents/{ids['opening']}/binance-testnet/cancel",
            json={**action, "fencing_token": lease["fencing_token"] + 1},
        )
        assert stale.status_code == 422, stale.text
        assert len(venue.calls) == calls_before
        cancelled = await http.post(
            f"/api/intents/{ids['opening']}/binance-testnet/cancel", json=action
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["confirmed"] is True

    with database.session_factory() as session:
        intent = session.get(OrderIntent, ids["opening"])
        reservation = session.get(RiskReservation, ids["reservation"])
        assert intent is not None and intent.status == OrderIntentStatus.CANCELLED.value
        assert reservation is not None and reservation.status == ReservationStatus.RELEASED.value


def test_fencing_rejects_before_external_call_and_zero_fill_cancel_releases_once(
    database: Database,
) -> None:
    asyncio.run(run_cancel_and_fencing(database))


async def run_rejection(database: Database) -> None:
    service = TradingService(
        database,
        credential_encryption_key=base64.urlsafe_b64encode(b"testnet-flow-key-32-bytes-long!!"[:32])
        .decode()
        .rstrip("="),
    )
    ids = seed_testnet(service, key="m4-reject")
    venue = SimulatedTestnetVenue()
    venue.reject_next_post = True
    reader = MutableTestnetReader()
    async with AsyncClient(
        transport=ASGITransport(app=build_testnet_app(database, venue, reader)),
        base_url="http://test",
    ) as http:
        await login(http, "m4-reject-operator")
        lease = await acquire_sender(http, "reject-worker")
        rejected = await http.post(
            f"/api/intents/{ids['opening']}/binance-testnet/send",
            json={
                "execution_scope": lease["execution_scope"],
                "owner_id": lease["owner_id"],
                "fencing_token": lease["fencing_token"],
            },
        )
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["error"]["code"] == "BINANCE_TESTNET_REJECTED"

    with database.session_factory() as session:
        intent = session.get(OrderIntent, ids["opening"])
        reservation = session.get(RiskReservation, ids["reservation"])
        order = session.scalar(
            select(VenueOrder).where(VenueOrder.order_intent_id == ids["opening"])
        )
        assert intent is not None and intent.status == OrderIntentStatus.REJECTED.value
        assert reservation is not None and reservation.status == ReservationStatus.RELEASED.value
        assert order is not None and order.status == "REJECTED"


def test_confirmed_testnet_rejection_is_terminal_and_releases_zero_fill_risk(
    database: Database,
) -> None:
    asyncio.run(run_rejection(database))


async def run_partial_cancel(database: Database) -> None:
    service = TradingService(
        database,
        credential_encryption_key=base64.urlsafe_b64encode(b"testnet-flow-key-32-bytes-long!!"[:32])
        .decode()
        .rstrip("="),
    )
    ids = seed_testnet(service, key="m4-partial-cancel")
    venue = SimulatedTestnetVenue()
    reader = MutableTestnetReader()
    async with AsyncClient(
        transport=ASGITransport(app=build_testnet_app(database, venue, reader)),
        base_url="http://test",
    ) as http:
        await login(http, "m4-partial-cancel-operator")
        lease = await acquire_sender(http, "partial-cancel-worker")
        action = {
            "execution_scope": lease["execution_scope"],
            "owner_id": lease["owner_id"],
            "fencing_token": lease["fencing_token"],
        }
        sent = await http.post(f"/api/intents/{ids['opening']}/binance-testnet/send", json=action)
        assert sent.status_code == 200, sent.text
        client_id = sent.json()["client_order_id"]
        external = venue.orders[client_id]
        external["status"] = "PARTIALLY_FILLED"
        external["executedQty"] = "0.25"
        order_id = str(external["orderId"])
        now = datetime.now(UTC)
        reader.snapshot = snapshot(
            now,
            orders=(
                order_fact(
                    order_id,
                    client_id,
                    "PARTIALLY_FILLED",
                    "BUY",
                    Decimal(1),
                    Decimal("0.25"),
                    now,
                ),
            ),
            fills=(
                BinanceFill(
                    "partial-cancel-fill",
                    order_id,
                    "BUY",
                    Decimal("0.25"),
                    Decimal("100"),
                    Decimal("0.25"),
                    "USDT",
                    now,
                ),
            ),
            quantity=Decimal("0.25"),
            entry=Decimal("100"),
            mark=Decimal("101"),
            protection=None,
        )
        synced = await http.post(
            "/api/venues/binance/testnet/sync",
            json={"account_id": "acct-testnet", "symbol": "BTCUSDT"},
        )
        assert synced.status_code == 200, synced.text
        cancelled = await http.post(
            f"/api/intents/{ids['opening']}/binance-testnet/cancel", json=action
        )
        assert cancelled.status_code == 200, cancelled.text

    with database.session_factory() as session:
        intent = session.get(OrderIntent, ids["opening"])
        reservation = session.get(RiskReservation, ids["reservation"])
        assert intent is not None and intent.status == OrderIntentStatus.CANCELLED.value
        assert reservation is not None and reservation.status == ReservationStatus.OPEN.value


def test_partial_fill_cancel_is_terminal_but_does_not_release_open_risk(
    database: Database,
) -> None:
    asyncio.run(run_partial_cancel(database))


async def run_disabled_guard(database: Database) -> None:
    service = TradingService(
        database,
        credential_encryption_key=base64.urlsafe_b64encode(b"testnet-flow-key-32-bytes-long!!"[:32])
        .decode()
        .rstrip("="),
    )
    ids = seed_testnet(service, key="m4-disabled")
    venue = SimulatedTestnetVenue()
    reader = MutableTestnetReader()
    async with AsyncClient(
        transport=ASGITransport(app=build_testnet_app(database, venue, reader, enabled=False)),
        base_url="http://test",
    ) as http:
        await login(http, "m4-disabled-operator")
        blocked = await http.post(
            f"/api/intents/{ids['opening']}/binance-testnet/send",
            json={
                "execution_scope": "TESTNET:acct-testnet:BINANCE",
                "owner_id": "disabled-worker",
                "fencing_token": 1,
            },
        )
        assert blocked.status_code == 503, blocked.text
        assert blocked.json()["error"]["code"] == "BINANCE_TESTNET_DISABLED"
    assert venue.calls == []


def test_testnet_send_is_default_off_and_disabled_guard_precedes_external_call(
    database: Database,
) -> None:
    asyncio.run(run_disabled_guard(database))


def test_read_only_sync_binds_legacy_freqtrade_fill_and_accepts_bounded_lot(
    database: Database,
) -> None:
    service = TradingService(
        database,
        credential_encryption_key=base64.urlsafe_b64encode(b"testnet-flow-key-32-bytes-long!!"[:32])
        .decode()
        .rstrip("="),
    )
    ids = seed_testnet(service, key="m4-freqtrade-fill")
    now = datetime.now(UTC)
    with database.session_factory.begin() as session:
        intent = session.get(OrderIntent, ids["opening"], with_for_update=True)
        campaign = session.get(Campaign, ids["campaign"], with_for_update=True)
        assert intent is not None and campaign is not None
        intent.status = OrderIntentStatus.FILLED.value
        intent.updated_at = now
        campaign.status = CampaignStatus.OPEN.value
        campaign.updated_at = now
        session.add(
            VenueOrder(
                team_id=campaign.team_id,
                order_intent_id=intent.intent_id,
                account_id="acct-testnet",
                venue="BINANCE",
                environment=ExecutionEnvironment.TESTNET.value,
                instrument_id=ids["instrument"],
                venue_order_id="freqtrade:41:entry",
                client_order_id="tcp-freqtrade-fixture",
                side="BUY",
                order_type="MARKET",
                reduce_only=False,
                status=VenueOrderStatus.FILLED.value,
                ordered_quantity=Decimal("0.8"),
                filled_quantity=Decimal("0.8"),
                observed_at=now - timedelta(seconds=5),
                updated_at=now,
            )
        )

    service.ingest_binance_read_only_snapshot(
        "acct-testnet",
        ids["operator"],
        snapshot(
            now,
            orders=(
                order_fact(
                    "native-stop-41",
                    "external-stop-41",
                    VenueOrderStatus.SENT.value,
                    "SELL",
                    Decimal("0.8"),
                    Decimal(0),
                    now,
                    order_type="STOP_MARKET",
                    stop_price=Decimal("95"),
                    reduce_only=True,
                ),
            ),
            fills=(
                BinanceFill(
                    "native-fill-41",
                    "native-entry-41",
                    "BUY",
                    Decimal("0.8"),
                    Decimal("100"),
                    Decimal("0.04"),
                    "USDT",
                    now,
                ),
            ),
            quantity=Decimal("0.8"),
            entry=Decimal("100"),
            mark=Decimal("101"),
            protection=BinanceProtection("native-stop-41", Decimal("0.8"), Decimal("95"), now),
        ),
        environment=ExecutionEnvironment.TESTNET,
        now=now,
    )
    reconciliation_id = service.reconcile_scope(
        "TESTNET:acct-testnet:BINANCE", ids["operator"], now=now
    )

    assert service.reconciliation_status(reconciliation_id) is ReconciliationStatus.MATCH
    with database.session_factory() as session:
        intent = session.get(OrderIntent, ids["opening"])
        order = session.scalar(
            select(VenueOrder).where(VenueOrder.order_intent_id == ids["opening"])
        )
        fill = session.scalar(select(VenueFill).where(VenueFill.venue_fill_id == "native-fill-41"))
        assert intent is not None and intent.status == OrderIntentStatus.FILLED.value
        assert order is not None and order.venue_order_id == "native-entry-41"
        assert fill is not None and fill.order_intent_id == ids["opening"]


def test_read_only_sync_recovers_unique_late_fill_for_unknown_freqtrade_intent(
    database: Database,
) -> None:
    service = TradingService(
        database,
        credential_encryption_key=base64.urlsafe_b64encode(b"testnet-flow-key-32-bytes-long!!"[:32])
        .decode()
        .rstrip("="),
    )
    ids = seed_testnet(service, key="m4-freqtrade-late-fill")
    now = datetime.now(UTC)
    with database.session_factory.begin() as session:
        intent = session.get(OrderIntent, ids["opening"], with_for_update=True)
        campaign = session.get(Campaign, ids["campaign"], with_for_update=True)
        reservation = session.get(RiskReservation, ids["reservation"], with_for_update=True)
        assert intent is not None and campaign is not None and reservation is not None
        intent.status = OrderIntentStatus.UNKNOWN.value
        intent.updated_at = now
        campaign.status = CampaignStatus.UNKNOWN.value
        campaign.updated_at = now
        reservation.status = ReservationStatus.UNKNOWN.value
        reservation.updated_at = now
        session.add(
            VenueOrder(
                team_id=campaign.team_id,
                order_intent_id=intent.intent_id,
                account_id="acct-testnet",
                venue="BINANCE",
                environment=ExecutionEnvironment.TESTNET.value,
                instrument_id=ids["instrument"],
                venue_order_id="UNKNOWN:tcp-late-fill",
                client_order_id="tcp-late-fill",
                side="BUY",
                order_type="MARKET",
                reduce_only=False,
                status=VenueOrderStatus.UNKNOWN.value,
                ordered_quantity=Decimal("0.8"),
                filled_quantity=Decimal(0),
                observed_at=now - timedelta(seconds=20),
                updated_at=now,
            )
        )

    service.ingest_binance_read_only_snapshot(
        "acct-testnet",
        ids["operator"],
        snapshot(
            now,
            orders=(),
            fills=(
                BinanceFill(
                    "native-late-fill-41",
                    "native-late-entry-41",
                    "BUY",
                    Decimal("0.75"),
                    Decimal("100"),
                    Decimal("0.04"),
                    "USDT",
                    now - timedelta(seconds=30),
                ),
                BinanceFill(
                    "native-cleanup-fill-41",
                    "native-cleanup-exit-41",
                    "SELL",
                    Decimal("0.75"),
                    Decimal("101"),
                    Decimal("0.04"),
                    "USDT",
                    now,
                ),
            ),
            quantity=Decimal(0),
            entry=Decimal(0),
            mark=Decimal("101"),
            protection=None,
        ),
        environment=ExecutionEnvironment.TESTNET,
        now=now,
    )

    with database.session_factory() as session:
        intent = session.get(OrderIntent, ids["opening"])
        reservation = session.get(RiskReservation, ids["reservation"])
        order = session.scalar(
            select(VenueOrder).where(VenueOrder.order_intent_id == ids["opening"])
        )
        fill = session.scalar(
            select(VenueFill).where(VenueFill.venue_fill_id == "native-late-fill-41")
        )
        assert intent is not None and intent.status == OrderIntentStatus.FILLED.value
        assert reservation is not None and reservation.status == ReservationStatus.OPEN.value
        assert order is not None and order.venue_order_id == "native-late-entry-41"
        assert order.ordered_quantity == Decimal("0.75")
        assert order.filled_quantity == Decimal("0.75")
        assert fill is not None and fill.order_intent_id == ids["opening"]

    recovered_exit = service.recover_freqtrade_emergency_exit(
        ids["campaign"],
        ids["admin"],
        "confirmed unique emergency cleanup fill",
        now=now,
    )
    reconciliation_id = service.reconcile_scope(
        "TESTNET:acct-testnet:BINANCE", ids["operator"], now=now
    )
    assert service.reconciliation_status(reconciliation_id) is ReconciliationStatus.MATCH
    service.close_campaign(ids["campaign"], ids["operator"], now=now)

    with database.session_factory() as session:
        campaign = session.get(Campaign, ids["campaign"])
        exit_intent = session.get(OrderIntent, recovered_exit)
        reservation = session.get(RiskReservation, ids["reservation"])
        cleanup_fill = session.scalar(
            select(VenueFill).where(VenueFill.venue_fill_id == "native-cleanup-fill-41")
        )
        assert campaign is not None and campaign.status == CampaignStatus.CLOSED.value
        assert exit_intent is not None and exit_intent.status == OrderIntentStatus.FILLED.value
        assert exit_intent.trigger_source == "FREQTRADE_EMERGENCY_RECOVERY"
        assert reservation is not None and reservation.status == ReservationStatus.RELEASED.value
        assert cleanup_fill is not None and cleanup_fill.order_intent_id == recovered_exit

    service.ingest_binance_read_only_snapshot(
        "acct-testnet",
        ids["operator"],
        snapshot(
            now,
            orders=(),
            fills=(
                BinanceFill(
                    "native-late-fill-41",
                    "native-late-entry-41",
                    "BUY",
                    Decimal("0.75"),
                    Decimal("100"),
                    Decimal("0.04"),
                    "USDT",
                    now - timedelta(seconds=30),
                ),
                BinanceFill(
                    "native-cleanup-fill-41",
                    "native-cleanup-exit-41",
                    "SELL",
                    Decimal("0.75"),
                    Decimal("101"),
                    Decimal("0.04"),
                    "USDT",
                    now,
                ),
            ),
            quantity=Decimal(0),
            entry=Decimal(0),
            mark=Decimal("101"),
            protection=None,
        ),
        environment=ExecutionEnvironment.TESTNET,
        now=now + timedelta(seconds=1),
    )
    with database.session_factory() as session:
        campaign = session.get(Campaign, ids["campaign"])
        reservation = session.get(RiskReservation, ids["reservation"])
        assert campaign is not None and campaign.status == CampaignStatus.CLOSED.value
        assert reservation is not None and reservation.status == ReservationStatus.RELEASED.value


def test_emergency_recovery_binds_interrupted_ready_entry_and_cleanup(
    database: Database,
) -> None:
    service = TradingService(
        database,
        credential_encryption_key=base64.urlsafe_b64encode(b"testnet-flow-key-32-bytes-long!!"[:32])
        .decode()
        .rstrip("="),
    )
    ids = seed_testnet(service, key="m4-freqtrade-interrupted-entry")
    now = datetime.now(UTC)
    observed_at = now + timedelta(seconds=1)
    service.ingest_binance_read_only_snapshot(
        "acct-testnet",
        ids["operator"],
        snapshot(
            now,
            orders=(),
            fills=(
                BinanceFill(
                    "interrupted-entry-fill",
                    "interrupted-entry-order",
                    "BUY",
                    Decimal("0.75"),
                    Decimal("100"),
                    Decimal("0.04"),
                    "USDT",
                    now,
                ),
                BinanceFill(
                    "interrupted-cleanup-fill",
                    "interrupted-cleanup-order",
                    "SELL",
                    Decimal("0.75"),
                    Decimal("101"),
                    Decimal("0.04"),
                    "USDT",
                    observed_at,
                ),
            ),
            quantity=Decimal(0),
            entry=Decimal(0),
            mark=Decimal("101"),
            protection=None,
        ),
        environment=ExecutionEnvironment.TESTNET,
        now=observed_at,
    )

    recovered_exit = service.recover_freqtrade_emergency_exit(
        ids["campaign"],
        ids["admin"],
        "worker process interrupted after the official entry fill",
        now=observed_at,
    )
    reconciliation_id = service.reconcile_scope(
        "TESTNET:acct-testnet:BINANCE", ids["operator"], now=observed_at
    )
    assert service.reconciliation_status(reconciliation_id) is ReconciliationStatus.MATCH
    service.close_campaign(ids["campaign"], ids["operator"], now=observed_at)

    with database.session_factory() as session:
        campaign = session.get(Campaign, ids["campaign"])
        entry = session.get(OrderIntent, ids["opening"])
        exit_intent = session.get(OrderIntent, recovered_exit)
        reservation = session.get(RiskReservation, ids["reservation"])
        entry_fill = session.scalar(
            select(VenueFill).where(VenueFill.venue_fill_id == "interrupted-entry-fill")
        )
        cleanup_fill = session.scalar(
            select(VenueFill).where(VenueFill.venue_fill_id == "interrupted-cleanup-fill")
        )
        assert campaign is not None and campaign.status == CampaignStatus.CLOSED.value
        assert entry is not None and entry.status == OrderIntentStatus.FILLED.value
        assert exit_intent is not None and exit_intent.status == OrderIntentStatus.FILLED.value
        assert reservation is not None and reservation.status == ReservationStatus.RELEASED.value
        assert entry_fill is not None and entry_fill.order_intent_id == ids["opening"]
        assert cleanup_fill is not None and cleanup_fill.order_intent_id == recovered_exit
