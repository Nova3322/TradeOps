from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    ProposalSource,
    RiskTier,
    Role,
    SystemRiskState,
)
from trading_control_plane.models import (
    Instrument,
    OrderIntent,
    Proposal,
    RiskDecision,
    TradingAuthorization,
)
from trading_control_plane.perptape import PerptapeClient, perptape_legacy_candidate_id
from trading_control_plane.queries import TradingQueries
from trading_control_plane.service import TradingService
from trading_control_plane.telegram import MockTelegramGateway


def seed(service: TradingService) -> dict[str, UUID]:
    now = datetime.now(UTC)
    admin = service.bootstrap_admin("admin", now=now)
    proposer = service.create_user("proposer", admin, now=now)
    reviewer_one = service.create_user("reviewer-1", admin, now=now)
    reviewer_two = service.create_user("reviewer-2", admin, now=now)
    operator = service.create_user("operator", admin, now=now)
    perptape = service.create_service_principal("perptape", admin, now=now)
    service.assign_role(proposer, Role.PROPOSER, admin, "acct-1", "BINANCE", now=now)
    service.assign_role(reviewer_one, Role.REVIEWER, admin, "acct-1", "BINANCE", now=now)
    service.assign_role(reviewer_two, Role.REVIEWER, admin, "acct-1", "BINANCE", now=now)
    service.assign_role(operator, Role.OPERATOR, admin, "acct-1", "BINANCE", now=now)
    service.assign_role(perptape, Role.PROPOSER, admin, "acct-1", "BINANCE", now=now)
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
        version="m1-risk-v1",
        system_state=SystemRiskState.NORMAL,
        max_total_risk=Decimal("100"),
        max_fact_age=timedelta(minutes=5),
        now=now,
    )
    service.record_position(
        "acct-1",
        "BINANCE",
        instrument,
        Decimal("0"),
        Decimal("0"),
        Decimal("120000"),
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
    return {
        "admin": admin,
        "proposer": proposer,
        "reviewer_one": reviewer_one,
        "reviewer_two": reviewer_two,
        "operator": operator,
        "perptape": perptape,
        "instrument": instrument,
    }


def perptape_client() -> PerptapeClient:
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    def fetch(_url: str, _headers: dict[str, str], _timeout: float) -> dict[str, Any]:
        return {
            "type": "breakouts",
            "generatedAt": now_ms,
            "data": [
                {
                    "exchange": "BN",
                    "symbol": "BTCUSDT",
                    "canonicalSymbol": "BTC",
                    "direction": "HH",
                    "timeframe": "1h",
                    "price": 120000,
                    "breakoutPrice": 120000,
                    "threshold": 119500,
                    "klineReadiness": {"status": "ready"},
                    "triggeredAt": now_ms - 1_000,
                    "updatedAt": now_ms,
                }
            ],
        }

    return PerptapeClient(
        base_url="https://perptape.com",
        api_key="test-key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
        fetcher=fetch,
    )


def perptape_client_for_contract(symbol: str, canonical_symbol: str) -> PerptapeClient:
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    def fetch(_url: str, _headers: dict[str, str], _timeout: float) -> dict[str, Any]:
        return {
            "type": "breakouts",
            "generatedAt": now_ms,
            "data": [
                {
                    "exchange": "BN",
                    "symbol": symbol,
                    "canonicalSymbol": canonical_symbol,
                    "direction": "HH",
                    "timeframe": "1h",
                    "price": 120000,
                    "breakoutPrice": 120000,
                    "threshold": 119500,
                    "klineReadiness": {"status": "ready"},
                    "triggeredAt": now_ms - 1_000,
                    "updatedAt": now_ms,
                }
            ],
        }

    return PerptapeClient(
        base_url="https://perptape.com",
        api_key="test-key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
        fetcher=fetch,
    )


def app(
    database: Database,
    telegram: MockTelegramGateway,
    client: PerptapeClient | None = None,
) -> FastAPI:
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="m1-test-signing-secret-that-is-long-enough",  # noqa: S106
        perptape_api_key="not-used-by-injected-client",
        perptape_service_username="perptape",
        public_base_url="http://test",
        _env_file=None,
    )
    return create_app(settings, database, client or perptape_client(), telegram)


async def login(client: AsyncClient, username: str) -> None:
    response = await client.post("/api/auth/mock/login", json={"username": username})
    assert response.status_code == 200, response.text


async def logout(client: AsyncClient) -> None:
    response = await client.post("/api/auth/logout")
    assert response.status_code == 200


def test_opportunity_marks_ready_but_uncatalogued_raw_contract_ineligible(
    database: Database, service: TradingService
) -> None:
    seed(service)
    candidate_client = perptape_client_for_contract("BTCUSDC", "BTC")

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app(database, MockTelegramGateway(), candidate_client)),
            base_url="http://test",
        ) as http:
            await login(http, "proposer")
            opportunities = await http.get("/api/opportunities")
            assert opportunities.status_code == 200, opportunities.text
            candidate = opportunities.json()["data"][0]
            assert candidate["symbol"] == "BTCUSDC"
            assert candidate["canonical_symbol"] == "BTC"
            assert candidate["readiness"] == "READY"
            assert candidate["proposal_eligible"] is False
            assert candidate["proposal_blocker"] == "INSTRUMENT_UNAVAILABLE"

            rejected = await http.post(
                f"/api/opportunities/{candidate['candidate_id']}/proposals",
                json={
                    "account_id": "acct-1",
                    "risk_tier": "LOW",
                    "quantity": "1",
                    "max_risk": "40",
                    "expires_in_minutes": 120,
                    "invalidation_price": "118000",
                    "rationale": "must not guess a quote contract",
                },
            )
            assert rejected.status_code == 422, rejected.text
            assert rejected.json()["error"]["code"] == "INSTRUMENT_UNAVAILABLE"

    asyncio.run(scenario())
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Proposal)) == 0


def test_opportunity_rejects_exact_but_inactive_catalog_instrument(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    with database.session_factory() as session:
        instrument = session.get(Instrument, ids["instrument"])
        assert instrument is not None
        instrument.active = False
        session.commit()

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app(database, MockTelegramGateway(), perptape_client())),
            base_url="http://test",
        ) as http:
            await login(http, "proposer")
            opportunities = await http.get("/api/opportunities")
            assert opportunities.status_code == 200, opportunities.text
            candidate = opportunities.json()["data"][0]
            assert candidate["symbol"] == "BTCUSDT"
            assert candidate["proposal_eligible"] is False
            assert candidate["proposal_blocker"] == "INSTRUMENT_UNAVAILABLE"

            rejected = await http.post(
                f"/api/opportunities/{candidate['candidate_id']}/proposals",
                json={
                    "account_id": "acct-1",
                    "risk_tier": "LOW",
                    "quantity": "1",
                    "max_risk": "40",
                    "expires_in_minutes": 120,
                    "invalidation_price": "118000",
                    "rationale": "inactive Catalog instruments remain unavailable",
                },
            )
            assert rejected.status_code == 422, rejected.text
            assert rejected.json()["error"]["code"] == "INSTRUMENT_UNAVAILABLE"

    asyncio.run(scenario())
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Proposal)) == 0


def test_api_reuses_exact_legacy_proposal_without_deduplicating_another_contract(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    now = datetime.now(UTC)
    client = perptape_client()
    candidate = client.list_candidates(now=now)[0]
    legacy_candidate_id = perptape_legacy_candidate_id(candidate)
    request_payload = {
        "account_id": "acct-1",
        "risk_tier": "LOW",
        "quantity": "1",
        "max_risk": "40",
        "expires_in_minutes": 120,
        "invalidation_price": "118000",
        "rationale": "review Perptape breakout",
    }
    legacy_proposal_id = service.create_proposal(
        actor_id=ids["perptape"],
        source=ProposalSource.SYSTEM,
        risk_tier=RiskTier.LOW,
        account_id="acct-1",
        venue=candidate.venue,
        instrument_id=ids["instrument"],
        direction=candidate.direction,
        quantity=Decimal("1"),
        max_risk=Decimal("40"),
        expires_at=now + timedelta(minutes=120),
        idempotency_key=f"perptape:{legacy_candidate_id}",
        strategy_id="perptape",
        strategy_version=candidate.source_contract_version,
        source_candidate_id=legacy_candidate_id,
        source_link=candidate.detail_url,
        source_observed_at=candidate.observed_at,
        source_readiness=candidate.readiness,
        details={
            "candidate": candidate.to_dict(),
            "invalidation_price": "118000",
            "initial_quantity": "1",
            "allow_auto_add": False,
            "requested_adds": 0,
            "add_trigger_price": None,
            "rationale": "review Perptape breakout",
        },
        idempotency_payload={
            "candidate_id": legacy_candidate_id,
            "account_id": "acct-1",
            "risk_tier": "LOW",
            "quantity": "1",
            "initial_quantity": None,
            "max_risk": "40",
            "expires_in_minutes": 120,
            "invalidation_price": "118000",
            "allow_auto_add": False,
            "requested_adds": 0,
            "add_trigger_price": None,
            "rationale": "review Perptape breakout",
        },
        now=now,
    )

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app(database, MockTelegramGateway(), client)),
            base_url="http://test",
        ) as http:
            await login(http, "proposer")
            created = await http.post(
                f"/api/opportunities/{candidate.candidate_id}/proposals",
                json=request_payload,
            )
            assert created.status_code == 200, created.text
            assert created.json()["proposal_id"] == str(legacy_proposal_id)
            assert created.json()["source_candidate_id"] == legacy_candidate_id
            legacy_created = await http.post(
                f"/api/opportunities/{legacy_candidate_id}/proposals",
                json=request_payload,
            )
            assert legacy_created.status_code == 200, legacy_created.text
            assert legacy_created.json()["proposal_id"] == str(legacy_proposal_id)

    asyncio.run(scenario())
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Proposal)) == 1
    queries = TradingQueries(database)

    assert (
        queries.compatible_legacy_system_candidate_id(
            legacy_candidate_id,
            candidate,
            ids["instrument"],
        )
        == legacy_candidate_id
    )
    assert (
        queries.compatible_legacy_system_candidate_id(
            legacy_candidate_id,
            replace(candidate, symbol="BTCUSDC", candidate_id="pt_exact_usdc"),
            ids["instrument"],
        )
        is None
    )


def test_perptape_to_review_to_risk_and_authorization_api_flow(
    database: Database, service: TradingService
) -> None:
    seed(service)
    telegram = MockTelegramGateway()

    async def scenario() -> str:
        async with AsyncClient(
            transport=ASGITransport(app=app(database, telegram)), base_url="http://test"
        ) as client:
            await login(client, "proposer")
            opportunities = await client.get("/api/opportunities")
            assert opportunities.status_code == 200, opportunities.text
            candidate = opportunities.json()["data"][0]
            assert candidate["direction"] == "LONG"
            assert candidate["proposal_eligible"] is True
            assert candidate["proposal_blocker"] is None
            payload = {
                "account_id": "acct-1",
                "risk_tier": "LOW",
                "quantity": "1",
                "max_risk": "40",
                "expires_in_minutes": 120,
                "invalidation_price": "118000",
                "rationale": "review Perptape breakout",
            }
            created = await client.post(
                f"/api/opportunities/{candidate['candidate_id']}/proposals", json=payload
            )
            assert created.status_code == 200, created.text
            proposal = created.json()
            assert proposal["source"] == "SYSTEM"
            assert proposal["environment"] == "SHADOW"
            assert proposal["status"] == "PENDING_REVIEW"
            assert proposal["source_candidate_id"] == candidate["candidate_id"]
            assert proposal["symbol"] == "BTCUSDT"
            assert proposal["quote_currency"] == "USDT"
            assert proposal["collateral_currency"] == "USDT"
            proposal_id = proposal["proposal_id"]
            version = proposal["version"]

            listed = await client.get("/api/proposals?proposal_status=PENDING_REVIEW")
            assert listed.status_code == 200, listed.text
            assert listed.json()["data"][0]["symbol"] == "BTCUSDT"
            assert listed.json()["data"][0]["collateral_currency"] == "USDT"

            duplicate = await client.post(
                f"/api/opportunities/{candidate['candidate_id']}/proposals", json=payload
            )
            assert duplicate.status_code == 200
            assert duplicate.json()["proposal_id"] == proposal_id
            assert len(telegram.notifications()) == 2

            await logout(client)
            await login(client, "reviewer-1")
            notifications = await client.get("/api/telegram/mock/notifications")
            assert notifications.status_code == 200
            assert notifications.json()["transport"] == "MOCK_ONLY"
            notification = notifications.json()["data"][0]

            no_grant = await client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={
                    "decision": "APPROVE",
                    "reason": "checked",
                    "expected_version": version,
                },
            )
            assert no_grant.status_code == 403
            assert no_grant.json()["error"]["code"] == "ACTION_GRANT_REQUIRED"

            reference_is_not_grant = await client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={
                    "decision": "APPROVE",
                    "reason": "checked",
                    "expected_version": version,
                    "action_grant": notification["review_code"],
                },
            )
            assert reference_is_not_grant.status_code == 403
            assert reference_is_not_grant.json()["error"]["code"] == ("ACTION_GRANT_SCOPE_INVALID")

            step_up = await client.post(
                "/api/auth/mock/step-up",
                json={
                    "action": "proposal.approve",
                    "object_id": proposal_id,
                    "object_version": version,
                },
            )
            assert step_up.status_code == 200
            approved = await client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={
                    "decision": "APPROVE",
                    "reason": "checked",
                    "expected_version": version,
                    "action_grant": step_up.json()["action_grant"],
                },
            )
            assert approved.status_code == 200, approved.text
            assert approved.json()["status"] == "APPROVED"

            replay = await client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={
                    "decision": "APPROVE",
                    "reason": "checked",
                    "expected_version": version,
                    "action_grant": step_up.json()["action_grant"],
                },
            )
            assert replay.status_code == 409
            assert replay.json()["error"]["code"] == "VERSION_CONFLICT"

            await logout(client)
            await login(client, "operator")
            risk = await client.post(
                f"/api/proposals/{proposal_id}/risk-decisions",
                json={"idempotency_key": "api-risk-1"},
            )
            assert risk.status_code == 200, risk.text
            assert risk.json()["detail"]["risk_decision"]["result"] == "ALLOW"
            authorization = await client.post(
                f"/api/proposals/{proposal_id}/authorizations",
                json={
                    "idempotency_key": "api-authorization-1",
                    "expires_in_minutes": 30,
                    "allowed_adds": 0,
                },
            )
            assert authorization.status_code == 200, authorization.text
            detail = authorization.json()["detail"]
            assert detail["authorization"]["allowed_adds"] == 0
            return proposal_id

    proposal_id = asyncio.run(scenario())

    with database.session_factory() as session:
        proposal = session.get(Proposal, UUID(proposal_id))
        assert proposal is not None
        assert proposal.strategy_id == "perptape"
        assert proposal.strategy_version == "breakouts-v1"
        assert session.scalar(select(func.count()).select_from(Proposal)) == 1
        assert session.scalar(select(func.count()).select_from(RiskDecision)) == 1
        assert session.scalar(select(func.count()).select_from(TradingAuthorization)) == 1
        assert session.scalar(select(func.count()).select_from(OrderIntent)) == 0


def test_perptape_candidate_can_start_as_explicit_live_proposal(
    database: Database, service: TradingService
) -> None:
    seed(service)
    telegram = MockTelegramGateway()

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app(database, telegram)), base_url="http://test"
        ) as client:
            await login(client, "proposer")
            opportunities = await client.get("/api/opportunities")
            candidate = opportunities.json()["data"][0]
            created = await client.post(
                f"/api/opportunities/{candidate['candidate_id']}/proposals",
                json={
                    "environment": "LIVE",
                    "account_id": "acct-1",
                    "risk_tier": "LOW",
                    "quantity": "0.001",
                    "max_risk": "1",
                    "expires_in_minutes": 30,
                    "invalidation_price": "118000",
                    "rationale": "explicit live proposal still requires review and risk",
                },
            )
            assert created.status_code == 200, created.text
            assert created.json()["environment"] == "LIVE"
            assert created.json()["status"] == "PENDING_REVIEW"

    asyncio.run(scenario())


def test_manual_api_is_idempotent_and_semantic_conflicts_are_explicit(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    telegram = MockTelegramGateway()

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app(database, telegram)), base_url="http://test"
        ) as client:
            await login(client, "proposer")
            payload = {
                "account_id": "acct-1",
                "venue": "BINANCE",
                "instrument_id": str(ids["instrument"]),
                "direction": "LONG",
                "risk_tier": "MEDIUM",
                "quantity": "0.1",
                "max_risk": "20",
                "expires_in_minutes": 60,
                "trigger_price": "120000",
                "limit_price": None,
                "invalidation_price": "118000",
                "rationale": "manual reviewed setup",
                "idempotency_key": "manual-api-1",
            }
            first = await client.post("/api/proposals/manual", json=payload)
            second = await client.post("/api/proposals/manual", json=payload)
            assert first.status_code == 200, first.text
            assert second.status_code == 200
            assert first.json()["proposal_id"] == second.json()["proposal_id"]
            assert len(telegram.notifications()) == 2

            payload["quantity"] = "0.2"
            conflict = await client.post("/api/proposals/manual", json=payload)
            assert conflict.status_code == 409
            assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    asyncio.run(scenario())
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Proposal)) == 1
