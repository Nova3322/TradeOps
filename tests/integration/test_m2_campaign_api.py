from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    Direction,
    IntentKind,
    ProposalSource,
    ReviewDecision,
    RiskTier,
    Role,
    SystemRiskState,
)
from trading_control_plane.models import Campaign, OrderIntent, ReconciliationRun, RiskReservation
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.queries import TradingQueries
from trading_control_plane.service import TradingService
from trading_control_plane.telegram import MockTelegramGateway


def seed_authorized_campaign(service: TradingService) -> dict[str, UUID]:
    now = datetime.now(UTC)
    admin = service.bootstrap_admin("admin", now=now)
    proposer = service.create_user("proposer", admin, now=now)
    reviewer_one = service.create_user("reviewer-1", admin, now=now)
    reviewer_two = service.create_user("reviewer-2", admin, now=now)
    operator = service.create_user("operator", admin, now=now)
    for user_id, role in (
        (proposer, Role.PROPOSER),
        (reviewer_one, Role.REVIEWER),
        (reviewer_two, Role.REVIEWER),
        (operator, Role.OPERATOR),
    ):
        service.assign_role(user_id, role, admin, "acct-1", "BINANCE", now=now)
    instrument = service.register_instrument(
        actor_id=admin,
        venue="BINANCE",
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        lot_size=Decimal("0.001"),
        minimum_notional=Decimal("5"),
        contract_multiplier=Decimal("1"),
        quote_currency="USDT",
        collateral_currency="USDT",
        protection_supported=True,
        now=now,
    )
    service.set_risk_policy(
        actor_id=admin,
        version="m2-risk-v1",
        system_state=SystemRiskState.NORMAL,
        max_total_risk=Decimal("100"),
        max_fact_age=timedelta(minutes=5),
        now=now,
    )
    service.record_position(
        "acct-1",
        "BINANCE",
        instrument,
        Decimal(0),
        Decimal(0),
        Decimal("100"),
        True,
        operator,
        now=now,
    )
    service.record_account_equity(
        "acct-1",
        "BINANCE",
        Decimal("10000"),
        Decimal("9000"),
        "USDT",
        True,
        operator,
        now=now,
    )
    proposal = service.create_proposal(
        actor_id=proposer,
        source=ProposalSource.MANUAL,
        risk_tier=RiskTier.HIGH,
        account_id="acct-1",
        venue="BINANCE",
        instrument_id=instrument,
        direction=Direction.LONG,
        quantity=Decimal(1),
        max_risk=Decimal(40),
        expires_at=now + timedelta(hours=2),
        idempotency_key="m2-proposal",
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
        idempotency_key="m2-risk",
        now=now,
    )
    authorization = service.issue_authorization(
        proposal_id=proposal,
        actor_id=operator,
        expires_at=now + timedelta(minutes=30),
        allowed_adds=0,
        idempotency_key="m2-authorization",
        now=now,
    )
    return {
        "operator": operator,
        "instrument": instrument,
        "proposal": proposal,
        "authorization": authorization,
    }


def app(database: Database, telegram: MockTelegramGateway) -> FastAPI:
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="m2-test-signing-secret-that-is-long-enough",  # noqa: S106
        public_base_url="http://test",
        _env_file=None,
    )
    perptape = PerptapeClient(
        base_url="https://perptape.com",
        api_key=None,
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
    )
    return create_app(settings, database, perptape, telegram)


async def login(client: AsyncClient) -> None:
    response = await client.post("/api/auth/mock/login", json={"username": "operator"})
    assert response.status_code == 200, response.text


def intent_payload(ids: dict[str, UUID], key: str = "m2-initial") -> dict[str, Any]:
    return {
        "kind": "INITIAL",
        "account_id": "acct-1",
        "venue": "BINANCE",
        "instrument_id": str(ids["instrument"]),
        "direction": "LONG",
        "quantity": "1",
        "idempotency_key": key,
    }


async def run_shadow_campaign_flow(database: Database, telegram: MockTelegramGateway) -> None:
    ids = seed_authorized_campaign(TradingService(database))
    async with AsyncClient(
        transport=ASGITransport(app=app(database, telegram)), base_url="http://test"
    ) as client:
        await login(client)
        path = f"/api/authorizations/{ids['authorization']}/intents"
        created = await client.post(path, json=intent_payload(ids))
        duplicate = await client.post(path, json=intent_payload(ids))
        conflict_payload = intent_payload(ids)
        conflict_payload["quantity"] = "0.5"
        conflict = await client.post(path, json=conflict_payload)
        assert created.status_code == 200, created.text
        assert duplicate.status_code == 200, duplicate.text
        assert conflict.status_code == 409, conflict.text
        assert duplicate.json()["intent_id"] == created.json()["intent_id"]
        campaign_id = created.json()["campaign_id"]
        opening_intent = created.json()["intent_id"]
        proposal_detail = await client.get(f"/api/proposals/{ids['proposal']}")
        assert proposal_detail.status_code == 200, proposal_detail.text
        entry = proposal_detail.json()["initial_entry"]
        assert entry["campaign_id"] == campaign_id
        assert entry["campaign_status"] == "OPENING"
        assert entry["intent_id"] == opening_intent
        assert entry["intent_status"] == "READY"
        assert entry["created_at"] is not None
        assert Decimal(proposal_detail.json()["authorization"]["remaining_quantity"]) == 0

        lease = await client.post(
            "/api/sender-leases",
            json={
                "execution_scope": "acct-1:BINANCE",
                "owner_id": "m2-web-worker",
                "lease_seconds": 120,
            },
        )
        assert lease.status_code == 200, lease.text
        sent = await client.post(
            f"/api/intents/{opening_intent}/shadow-send",
            json={
                "execution_scope": "acct-1:BINANCE",
                "owner_id": "m2-web-worker",
                "fencing_token": lease.json()["fencing_token"],
                "venue_order_id": "m2-shadow-open",
            },
        )
        assert sent.status_code == 200, sent.text
        fill = await client.post(
            f"/api/intents/{opening_intent}/fills",
            json={
                "venue_fill_id": "m2-fill-open",
                "side": "BUY",
                "quantity": "1",
                "price": "100",
                "fee": "1",
                "fee_currency": "USDT",
                "slippage_cost": "0.5",
            },
        )
        assert fill.status_code == 200, fill.text
        position = await client.post(
            "/api/facts/positions",
            json={
                "account_id": "acct-1",
                "venue": "BINANCE",
                "instrument_id": str(ids["instrument"]),
                "quantity": "1",
                "average_entry_price": "100",
                "mark_price": "110",
                "known": True,
            },
        )
        assert position.status_code == 200, position.text
        protection = await client.post(
            f"/api/campaigns/{campaign_id}/protection",
            json={
                "position_id": position.json()["position_id"],
                "venue_order_id": "m2-shadow-stop",
                "quantity": "1",
                "trigger_price": "90",
                "fully_covered": True,
                "known": True,
            },
        )
        assert protection.status_code == 200, protection.text
        funding = await client.post(
            f"/api/campaigns/{campaign_id}/funding",
            json={
                "venue": "BINANCE",
                "venue_payment_id": "m2-funding",
                "amount": "-0.2",
                "currency": "USDT",
            },
        )
        assert funding.status_code == 200, funding.text
        pnl = await client.post(f"/api/campaigns/{campaign_id}/pnl")
        assert pnl.status_code == 200, pnl.text
        assert pnl.json()["pnl"]["total_pnl"] == "8.300000000000000000"

        target = await client.post(
            f"/api/campaigns/{campaign_id}/target",
            json={
                "candidates": [
                    {"target_quantity": "0.4", "urgency": "URGENT", "reason": "risk"},
                    {"target_quantity": "0", "urgency": "IMMEDIATE", "reason": "exit"},
                ]
            },
        )
        assert target.status_code == 200, target.text
        assert target.json()["decision"] == {
            "target_quantity": "0",
            "urgency": "IMMEDIATE",
            "reasons": ["exit", "risk"],
        }
        reduction = await client.post(
            f"/api/campaigns/{campaign_id}/reduction-intents",
            json={"idempotency_key": "m2-exit"},
        )
        assert reduction.status_code == 200, reduction.text
        exit_intent = reduction.json()["intent_id"]
        exit_send = await client.post(
            f"/api/intents/{exit_intent}/shadow-send",
            json={
                "execution_scope": "acct-1:BINANCE",
                "owner_id": "m2-web-worker",
                "fencing_token": lease.json()["fencing_token"],
                "venue_order_id": "m2-shadow-exit",
            },
        )
        assert exit_send.status_code == 200, exit_send.text
        exit_fill = await client.post(
            f"/api/intents/{exit_intent}/fills",
            json={
                "venue_fill_id": "m2-fill-exit",
                "side": "SELL",
                "quantity": "1",
                "price": "115",
                "fee": "1",
                "fee_currency": "USDT",
                "slippage_cost": "0.5",
            },
        )
        assert exit_fill.status_code == 200, exit_fill.text
        flat = await client.post(
            "/api/facts/positions",
            json={
                "account_id": "acct-1",
                "venue": "BINANCE",
                "instrument_id": str(ids["instrument"]),
                "quantity": "0",
                "average_entry_price": "0",
                "mark_price": "115",
                "known": True,
            },
        )
        assert flat.status_code == 200, flat.text
        reconcile = await client.post(
            f"/api/campaigns/{campaign_id}/reconcile",
            json={"execution_scope": "acct-1:BINANCE"},
        )
        assert reconcile.status_code == 200, reconcile.text
        assert reconcile.json()["detail"]["reconciliation"]["status"] == "MATCH"
        final_pnl = await client.post(f"/api/campaigns/{campaign_id}/pnl")
        assert final_pnl.status_code == 200, final_pnl.text
        assert final_pnl.json()["pnl"]["total_pnl"] == "11.800000000000000000"
        closed = await client.post(f"/api/campaigns/{campaign_id}/close")
        assert closed.status_code == 200, closed.text
        assert closed.json()["status"] == "CLOSED"
        assert all(item["status"] == "RELEASED" for item in closed.json()["reservations"])
        exceptions = await client.get("/api/campaign-exceptions")
        assert exceptions.status_code == 200, exceptions.text
        assert exceptions.json()["data"] == []
        notifications = await client.get("/api/telegram/mock/notifications")
        assert notifications.json() == {
            "transport": "MOCK_ONLY",
            "scope": "PROPOSAL_REVIEW_ONLY",
            "data": [],
        }
        events = [item.event_type for item in telegram.campaign_notifications()]
        assert events == [
            "SHADOW_FILL_RECORDED",
            "PROTECTION_ACTIVE",
            "SHADOW_FILL_RECORDED",
            "CAMPAIGN_CLOSED",
        ]
        for web_path in (
            "/campaigns",
            f"/campaigns/{campaign_id}",
            "/positions",
            "/orders",
            "/risk",
            "/exceptions",
        ):
            web = await client.get(web_path)
            assert web.status_code == 200, web.text
            assert "<title>交易控制台</title>" in web.text

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Campaign)) == 1
        assert session.scalar(select(func.count()).select_from(OrderIntent)) == 2
        assert session.scalar(select(func.count()).select_from(RiskReservation)) == 1


def test_shadow_campaign_api_runs_full_operator_flow(database: Database) -> None:
    asyncio.run(run_shadow_campaign_flow(database, MockTelegramGateway()))


async def run_unknown_flow(database: Database) -> None:
    ids = seed_authorized_campaign(TradingService(database))
    gateway = MockTelegramGateway()
    async with AsyncClient(
        transport=ASGITransport(app=app(database, gateway)), base_url="http://test"
    ) as client:
        await login(client)
        created = await client.post(
            f"/api/authorizations/{ids['authorization']}/intents",
            json=intent_payload(ids, "m2-unknown"),
        )
        intent_id = created.json()["intent_id"]
        campaign_id = created.json()["campaign_id"]
        lease = await client.post(
            "/api/sender-leases",
            json={
                "execution_scope": "acct-1:BINANCE",
                "owner_id": "unknown-worker",
                "lease_seconds": 60,
            },
        )
        await client.post(
            f"/api/intents/{intent_id}/shadow-send",
            json={
                "execution_scope": "acct-1:BINANCE",
                "owner_id": "unknown-worker",
                "fencing_token": lease.json()["fencing_token"],
                "venue_order_id": "unknown-order",
            },
        )
        unknown = await client.post(
            f"/api/intents/{intent_id}/unknown", json={"reason": "ambiguous timeout"}
        )
        assert unknown.status_code == 200, unknown.text
        assert unknown.json()["status"] == "UNKNOWN"
        assert unknown.json()["reservations"][0]["status"] == "UNKNOWN"
        release = await client.post(
            f"/api/intents/{intent_id}/release",
            json={"terminal_status": "CANCELLED", "reason": "unsafe release attempt"},
        )
        assert release.status_code == 422, release.text
        retry = await client.post(
            f"/api/authorizations/{ids['authorization']}/intents",
            json=intent_payload(ids, "m2-unknown"),
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["intent_id"] == intent_id
        exceptions = await client.get("/api/campaign-exceptions")
        codes = {item["code"] for item in exceptions.json()["data"]}
        assert {"CAMPAIGN_UNKNOWN", "ORDER_INTENT_UNKNOWN", "RISK_RESERVATION_UNKNOWN"} <= codes
        notifications = await client.get("/api/telegram/mock/notifications")
        assert notifications.json()["scope"] == "PROPOSAL_REVIEW_ONLY"
        assert "campaign_data" not in notifications.json()
        assert [item.event_type for item in gateway.campaign_notifications()] == [
            "ORDER_INTENT_UNKNOWN"
        ]
        detail = await client.get(f"/api/campaigns/{campaign_id}")
        assert detail.json()["intents"][0]["order"]["status"] == "UNKNOWN"


def test_unknown_api_keeps_risk_and_blocks_release_or_retry(database: Database) -> None:
    asyncio.run(run_unknown_flow(database))


def test_exception_view_marks_active_facts_stale_but_ignores_closed_history(
    database: Database,
) -> None:
    service = TradingService(database)
    ids = seed_authorized_campaign(service)
    now = datetime.now(UTC)
    opening = service.create_order_intent(
        ids["authorization"],
        ids["operator"],
        IntentKind.INITIAL,
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Direction.LONG,
        Decimal(1),
        "m2-stale-exception",
        now=now,
    )
    stale_at = now - timedelta(minutes=10)
    position_id = service.record_position(
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Decimal(1),
        Decimal(100),
        Decimal(105),
        True,
        ids["operator"],
        observed_at=stale_at,
        now=now,
    )
    service.record_protection(
        position_id,
        "stale-protection",
        Decimal(1),
        Decimal(90),
        True,
        ids["operator"],
        observed_at=stale_at,
        now=now,
    )
    with database.session_factory.begin() as session:
        session.add(
            ReconciliationRun(
                execution_scope="acct-1:BINANCE",
                campaign_id=opening.campaign_id,
                status="MATCH",
                is_computed=True,
                differences=[],
                resolution_reason=None,
                actor_id=str(ids["operator"]),
                correlation_id=uuid4(),
                started_at=stale_at - timedelta(seconds=1),
                completed_at=stale_at - timedelta(seconds=1),
            )
        )

    queries = TradingQueries(database)
    current_exceptions = queries.list_exceptions(ids["operator"], now=now)
    codes = {item["code"] for item in current_exceptions}
    assert {"POSITION_STALE", "PROTECTION_STALE", "RECONCILIATION_STALE"} <= codes
    assert all(item["occurred_at"] for item in current_exceptions)
    assert all(item["last_checked_at"] == now.isoformat() for item in current_exceptions)

    with database.session_factory.begin() as session:
        campaign = session.get(Campaign, opening.campaign_id)
        assert campaign is not None
        campaign.status = "CLOSED"

    assert queries.list_exceptions(ids["operator"], now=now + timedelta(hours=1)) == []
