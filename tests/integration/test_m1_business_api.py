from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from conftest import add_exchange_account_fixture, set_test_team_environment
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from workflow_builder import ActorSpec, WorkflowFixture

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    Direction,
    ExecutionEnvironment,
    ProposalSource,
    ReviewDecision,
    RiskTier,
    Role,
    SystemRiskState,
)
from trading_control_plane.models import (
    AccountEquity,
    AuditEvent,
    Campaign,
    Instrument,
    OrderIntent,
    Position,
    Proposal,
    RiskDecision,
    RiskReservation,
    TradingAuthorization,
    VenueOrder,
)
from trading_control_plane.perptape import (
    PerptapeClient,
    perptape_legacy_candidate_id,
)
from trading_control_plane.queries import TradingQueries
from trading_control_plane.service import TradingService
from trading_control_plane.service_domains.proposal_automation import (
    advance_approved_proposal,
    refresh_approved_proposal_risk,
)
from trading_control_plane.telegram import MockTelegramGateway
from trading_control_plane.venue_read_only import VenueInstrument


def seed(service: TradingService) -> dict[str, UUID]:
    now = datetime.now(UTC)
    fixture = WorkflowFixture.create(
        service,
        now=now,
        admin_username="admin",
        account_id="acct-1",
        venue="BINANCE",
        actors=(
            ActorSpec("proposer", "proposer", Role.PROPOSER),
            ActorSpec("reviewer_one", "reviewer-1", Role.REVIEWER),
            ActorSpec("reviewer_two", "reviewer-2", Role.REVIEWER),
            ActorSpec("operator", "operator", Role.OPERATOR),
            ActorSpec("perptape", "perptape", Role.PROPOSER, service_principal=True),
            ActorSpec(
                "runtime_sync",
                "runtime-sync",
                Role.OPERATOR,
                service_principal=True,
            ),
        ),
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        lot_size=Decimal("0.001"),
        minimum_notional=Decimal("5"),
        quote_currency="USDT",
        risk_version="m1-risk-v1",
        max_fact_age=timedelta(minutes=5),
        mark_price=Decimal("120000"),
    )
    return fixture.ids


def add_live_account(service: TradingService, ids: dict[str, UUID]) -> str:
    account_id = "live-acct-1"
    now = datetime.now(UTC)
    add_exchange_account_fixture(
        service.database,
        ids["admin"],
        account_id,
        "BINANCE",
        environment="LIVE",
    )
    for key, role in (
        ("proposer", Role.PROPOSER),
        ("reviewer_one", Role.REVIEWER),
        ("reviewer_two", Role.REVIEWER),
        ("operator", Role.OPERATOR),
        ("perptape", Role.PROPOSER),
        ("runtime_sync", Role.OPERATOR),
    ):
        service.assign_role(ids[key], role, ids["admin"], account_id, "BINANCE", now=now)
    return account_id


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


def perptape_hip3_client() -> PerptapeClient:
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    def fetch(_url: str, _headers: dict[str, str], _timeout: float) -> dict[str, Any]:
        return {
            "type": "breakouts",
            "generatedAt": now_ms,
            "data": [
                {
                    "exchange": "HL",
                    "symbol": "xyz:TSLA",
                    "canonicalSymbol": "TSLA",
                    "direction": "HH",
                    "timeframe": "4h",
                    "price": 325.19,
                    "breakoutPrice": 325.19,
                    "threshold": 320,
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

    return create_app(
        settings,
        database,
        client or perptape_client(),
        telegram,
    )


async def login(client: AsyncClient, username: str) -> None:
    response = await client.post("/api/auth/mock/login", json={"username": username})
    assert response.status_code == 200, response.text


def test_team_risk_policy_api_versions_explicit_limits_and_rejects_non_admin(
    database: Database,
) -> None:
    service = TradingService(database)
    seed(service)

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app(database, MockTelegramGateway())),
            base_url="http://test",
        ) as client:
            await login(client, "admin")
            current = await client.get("/api/risk-controls")
            assert current.status_code == 200, current.text
            assert current.json()["policy"]["limits_configured"] is True
            changed = await client.put(
                "/api/risk-controls/policy",
                json={
                    "version": "m1-risk-v2-tightened",
                    "max_total_risk": "90",
                    "max_account_risk": "80",
                    "max_single_loss": "50",
                    "max_consecutive_losses": 2,
                    "loss_cooldown_seconds": 7200,
                    "max_fact_age_seconds": 240,
                    "expected_revision": 1,
                    "reason": "tighten explicit team and account risk limits",
                    "idempotency_key": "m1-risk-policy-api",
                },
            )
            assert changed.status_code == 200, changed.text
            projected = (await client.get("/api/risk-controls")).json()["policy"]
            assert projected["revision"] == 2
            assert projected["max_account_risk"] == "80.000000000000000000"
            assert projected["max_single_loss"] == "50.000000000000000000"
            assert projected["max_consecutive_losses"] == 2
            assert projected["loss_cooldown_seconds"] == 7200

            loosened = await client.put(
                "/api/risk-controls/policy",
                json={
                    "version": "m1-risk-v3-loosened",
                    "max_total_risk": "95",
                    "max_account_risk": "80",
                    "max_single_loss": "50",
                    "max_consecutive_losses": 2,
                    "loss_cooldown_seconds": 7200,
                    "max_fact_age_seconds": 240,
                    "expected_revision": 2,
                    "reason": "attempt to loosen one configured team limit directly",
                    "idempotency_key": "m1-risk-policy-loosened",
                },
            )
            assert loosened.status_code == 200, loosened.text
            loosened_policy = (await client.get("/api/risk-controls")).json()["policy"]
            assert loosened_policy["revision"] == 3

            await login(client, "proposer")
            denied = await client.put(
                "/api/risk-controls/policy",
                json={
                    "version": "forbidden-policy",
                    "max_total_risk": "80",
                    "max_account_risk": "70",
                    "max_single_loss": "40",
                    "max_consecutive_losses": 2,
                    "loss_cooldown_seconds": 7200,
                    "max_fact_age_seconds": 200,
                    "expected_revision": 3,
                    "reason": "attempt outside assigned team risk administration",
                    "idempotency_key": "forbidden-risk-policy",
                },
            )
            assert denied.status_code == 403
            assert denied.json()["error"]["code"] == "RBAC_DENIED"

    asyncio.run(scenario())


async def logout(client: AsyncClient) -> None:
    response = await client.post("/api/auth/logout")
    assert response.status_code == 200


def test_api_process_prefers_fresh_persisted_perptape_feed(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    now = datetime.now(UTC)
    feed = perptape_client().refresh(now=now, force=True)
    service.record_perptape_feed(
        ids["admin"],
        feed,
        now=now,
        base_snapshot=None,
    )
    direct_fetches = 0

    def unexpected_fetch(
        _url: str, _headers: dict[str, str], _timeout: float
    ) -> dict[str, Any]:
        nonlocal direct_fetches
        direct_fetches += 1
        raise AssertionError("fresh runtime feed must prevent a direct Perptape request")

    direct_client = PerptapeClient(
        base_url="https://perptape.com",
        api_key="test-key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
        fetcher=unexpected_fetch,
    )

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(
                app=app(database, MockTelegramGateway(), direct_client)
            ),
            base_url="http://test",
        ) as http:
            await login(http, "proposer")
            response = await http.get("/api/opportunities")
            assert response.status_code == 200, response.text
            payload = response.json()
            assert len(payload["data"]) == 1
            assert payload["snapshot_generated_at"] == feed.generated_at.isoformat()
            assert payload["retry_at"] == feed.next_allowed_at.isoformat()

    asyncio.run(scenario())
    assert direct_fetches == 0


def test_freqtrade_backend_status_is_explicit_and_order_send_remains_closed(
    database: Database, service: TradingService
) -> None:
    seed(service)

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app(database, MockTelegramGateway())),
            base_url="http://test",
        ) as http:
            await login(http, "admin")
            response = await http.get("/api/execution/freqtrade/status")
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["backend"] == "FREQTRADE"
            assert payload["workers_enabled"] is False
            assert payload["direct_venue_send"] is False
            assert payload["live_order_send"] == "DISABLED"
            assert payload["gate_source"] == "DATABASE"
            assert payload["execution_worker"]["status"] == "UNKNOWN"
            assert payload["workers"] == []
            assert len(payload["account_bindings"]) == 1
            binding = payload["account_bindings"][0]
            assert binding["account_id"] == "acct-1"
            assert binding["mode"] == "UNCONFIGURED"
            assert binding["status"] == "UNCONFIGURED"
            assert binding["endpoint"] is None
            assert binding["order_send"] is False

    asyncio.run(scenario())


def test_configured_hip3_catalog_contract_is_proposal_eligible(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    now = datetime.now(UTC)
    service.register_instrument(
        actor_id=ids["admin"],
        venue="HYPERLIQUID",
        symbol="xyz:TSLA",
        tick_size=Decimal("0.001"),
        lot_size=Decimal("0.001"),
        minimum_notional=Decimal("10"),
        contract_multiplier=Decimal(1),
        quote_currency="USDC",
        collateral_currency="USDC",
        protection_supported=True,
        now=now,
    )

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(
                app=app(database, MockTelegramGateway(), perptape_hip3_client())
            ),
            base_url="http://test",
        ) as http:
            await login(http, "admin")
            response = await http.get("/api/opportunities")
            assert response.status_code == 200, response.text
            candidate = response.json()["data"][0]
            assert candidate["venue"] == "HYPERLIQUID"
            assert candidate["symbol"] == "xyz:TSLA"
            assert candidate["proposal_eligible"] is True
            assert candidate["proposal_blocker"] is None

    asyncio.run(scenario())


def test_opportunity_requires_an_exact_active_instrument_catalog_contract(
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
            assert "active Instrument Catalog match" in candidate["missing_fields"]

            rejected = await http.post(
                f"/api/opportunities/{candidate['candidate_id']}/proposals",
                json={
                    "account_id": "acct-1",
                    "risk_tier": "LOW",
                    "quantity": "1",
                    "max_risk": "40",
                    "expires_in_minutes": 480,
                    "invalidation_price": "118000",
                    "rationale": "exact Catalog contract is required before proposal creation",
                },
            )
            assert rejected.status_code == 422, rejected.text
            assert rejected.json()["error"]["code"] == "INSTRUMENT_UNAVAILABLE"

    asyncio.run(scenario())
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Proposal)) == 0
        discovered = session.scalar(
            select(Instrument).where(Instrument.venue == "BINANCE", Instrument.symbol == "BTCUSDC")
        )
        assert discovered is None


def test_official_catalog_sync_activates_current_contracts_and_deactivates_absent_ones(
    database: Database,
    service: TradingService,
) -> None:
    ids = seed(service)
    now = datetime.now(UTC)
    result = service.synchronize_active_venue_instruments(
        actor_id=ids["runtime_sync"],
        account_id="acct-1",
        venue="BINANCE",
        instruments=(
            VenueInstrument(
                symbol="BTCUSDT",
                tick_size=Decimal("0.1"),
                lot_size=Decimal("0.001"),
                minimum_notional=Decimal("5"),
                quote_currency="USDT",
                collateral_currency="USDT",
                active=True,
            ),
            VenueInstrument(
                symbol="TUTUSDT",
                tick_size=Decimal("0.00001"),
                lot_size=Decimal("1"),
                minimum_notional=Decimal("5"),
                quote_currency="USDT",
                collateral_currency="USDT",
                active=True,
            ),
        ),
        now=now,
    )
    assert result == {
        "active": 2,
        "created": 1,
        "refreshed": 0,
        "deactivated": 0,
        "unchanged": 1,
    }

    result = service.synchronize_active_venue_instruments(
        actor_id=ids["runtime_sync"],
        account_id="acct-1",
        venue="BINANCE",
        instruments=(
            VenueInstrument(
                symbol="TUTUSDT",
                tick_size=Decimal("0.00001"),
                lot_size=Decimal("1"),
                minimum_notional=Decimal("5"),
                quote_currency="USDT",
                collateral_currency="USDT",
                active=True,
            ),
        ),
        now=now + timedelta(minutes=1),
    )
    assert result["deactivated"] == 1
    with database.session_factory() as session:
        instruments = {
            row.symbol: row.active
            for row in session.scalars(select(Instrument).where(Instrument.venue == "BINANCE"))
        }
        catalog_audits = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == "INSTRUMENT_CATALOG_SYNCED")
        )
    assert instruments == {"BTCUSDT": False, "TUTUSDT": True}
    assert catalog_audits == 2


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
            transport=ASGITransport(
                app=app(
                    database,
                    MockTelegramGateway(),
                    perptape_client(),
                )
            ),
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
                    "expires_in_minutes": 480,
                    "invalidation_price": "118000",
                    "rationale": "inactive Catalog instruments remain unavailable",
                },
            )
            assert rejected.status_code == 422, rejected.text
            assert rejected.json()["error"]["code"] == "INSTRUMENT_UNAVAILABLE"

    asyncio.run(scenario())
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Proposal)) == 0


def test_incomplete_opportunity_explains_missing_fields_and_post_revalidates(
    database: Database,
    service: TradingService,
) -> None:
    seed(service)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    incomplete_client = PerptapeClient(
        base_url="https://perptape.com",
        api_key="test-key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
        fetcher=lambda _url, _headers, _timeout: {
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
                    "klineReadiness": {"status": "incomplete"},
                    "triggeredAt": now_ms - 1_000,
                    "updatedAt": now_ms,
                }
            ],
        },
    )

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app(database, MockTelegramGateway(), incomplete_client)),
            base_url="http://test",
        ) as http:
            await login(http, "proposer")
            opportunities = await http.get("/api/opportunities")
            assert opportunities.status_code == 200, opportunities.text
            candidate = opportunities.json()["data"][0]
            assert candidate["proposal_eligible"] is False
            assert candidate["proposal_blocker"] == "PERPTAPE_REQUIRED_FIELDS_MISSING"
            assert candidate["missing_fields"] == [
                "threshold",
                "klineReadiness.status=ready",
                "data_health=CURRENT",
            ]
            assert candidate["missing_field_labels"] == [
                "突破阈值",
                "K 线就绪状态",
                "实时完整数据",
            ]
            assert candidate["last_complete_at"] is None
            assert opportunities.json()["snapshot_id"]
            rejected = await http.post(
                f"/api/opportunities/{candidate['candidate_id']}/proposals",
                json={
                    "account_id": "acct-1",
                    "risk_tier": "LOW",
                    "quantity": "1",
                    "max_risk": "40",
                    "expires_in_minutes": 480,
                    "invalidation_price": "118000",
                    "rationale": "server must reject incomplete facts",
                },
            )
            assert rejected.status_code == 422, rejected.text
            assert rejected.json()["error"]["code"] == "PERPTAPE_CANDIDATE_NOT_READY"

    asyncio.run(scenario())
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Proposal)) == 0


def test_opportunity_snapshot_hides_malformed_binance_identity(
    database: Database,
    service: TradingService,
) -> None:
    seed(service)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    rows = []
    for symbol, canonical_symbol in (
        ("BTCUSDT", "BTC"),
        ("我踏马来了USDT", "我踏马来了"),
    ):
        rows.append(
            {
                "exchange": "BN",
                "symbol": symbol,
                "canonicalSymbol": canonical_symbol,
                "direction": "HH",
                "timeframe": "1h",
                "price": 120000,
                "threshold": 119500,
                "volume24hQuote": 1_000_000,
                "openInterestQuote": 500_000,
                "klineReadiness": {"status": "ready"},
                "triggeredAt": now_ms - 1_000,
                "updatedAt": now_ms,
            }
        )
    candidate_client = PerptapeClient(
        base_url="https://perptape.com",
        api_key="test-key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
        fetcher=lambda _url, _headers, _timeout: {
            "type": "breakouts",
            "generatedAt": now_ms,
            "data": rows,
        },
    )

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app(database, MockTelegramGateway(), candidate_client)),
            base_url="http://test",
        ) as http:
            await login(http, "proposer")
            response = await http.get("/api/opportunities")

            assert response.status_code == 200, response.text
            assert [item["symbol"] for item in response.json()["data"]] == ["BTCUSDT"]
            assert response.json()["discarded_candidate_count"] == 1

    asyncio.run(scenario())


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
        "max_risk": "2000",
        "expires_in_minutes": 480,
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
        max_risk=Decimal("2000"),
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
            "max_risk": "2000",
            "expires_in_minutes": 480,
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


def test_perptape_candidate_can_start_as_explicit_live_proposal(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    live_account_id = add_live_account(service, ids)
    set_test_team_environment(database, ids["admin"], "LIVE")
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
                    "account_id": live_account_id,
                    "risk_tier": "LOW",
                    "quantity": "0.001",
                    "max_risk": "2",
                    "expires_in_minutes": 480,
                    "invalidation_price": "118000",
                    "rationale": "explicit live proposal still requires review and risk",
                },
            )
            assert created.status_code == 200, created.text
            assert created.json()["environment"] == "LIVE"
            assert created.json()["status"] == "PENDING_REVIEW"
            detail = await client.get(f"/api/proposals/{created.json()['proposal_id']}")
            assert detail.status_code == 200, detail.text
            assert detail.json()["execution_preview"] == {
                "account_id": live_account_id,
                "venue": "BINANCE",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "order_type": "MARKET",
                "quantity": "0.001000000000000000",
                "estimated_notional": "120.000000000000000000",
                "quote_currency": "USDT",
                "leverage": "3.000000000000000000",
            }

    asyncio.run(scenario())


def test_high_risk_review_refreshes_only_the_remaining_reviewer_notification(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    live_account_id = add_live_account(service, ids)
    set_test_team_environment(database, ids["admin"], "LIVE")
    telegram = MockTelegramGateway()

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app(database, telegram)), base_url="http://test"
        ) as client:
            await login(client, "proposer")
            created = await client.post(
                "/api/proposals/manual",
                json={
                    "environment": "LIVE",
                    "account_id": live_account_id,
                    "venue": "BINANCE",
                    "instrument_id": str(ids["instrument"]),
                    "direction": "LONG",
                    "risk_tier": "HIGH",
                    "quantity": "0.001",
                    "max_risk": "2",
                    "expires_in_minutes": 480,
                    "trigger_price": "120000",
                    "invalidation_price": "118000",
                    "rationale": "two independent reviewers must receive the current version",
                    "idempotency_key": "high-risk-review-refresh",
                },
            )
            assert created.status_code == 200, created.text
            proposal_id = created.json()["proposal_id"]
            first_version = created.json()["version"]
            assert {item.reviewer_id for item in telegram.notifications()} == {
                ids["reviewer_one"],
                ids["reviewer_two"],
            }

            await logout(client)
            await login(client, "reviewer-1")
            first_review = await client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={
                    "decision": "APPROVE",
                    "reason": "first independent review",
                    "expected_version": first_version,
                    "idempotency_key": "high-risk-first-review",
                },
            )
            assert first_review.status_code == 200, first_review.text
            assert first_review.json()["status"] == "PENDING_REVIEW"
            current_version = first_review.json()["detail"]["version"]

            reviewer_one_notifications = [
                item for item in telegram.notifications() if item.reviewer_id == ids["reviewer_one"]
            ]
            reviewer_two_notifications = [
                item for item in telegram.notifications() if item.reviewer_id == ids["reviewer_two"]
            ]
            assert len(reviewer_one_notifications) == 1
            assert [item.proposal_version for item in reviewer_two_notifications] == [
                first_version,
                current_version,
            ]

            await logout(client)
            await login(client, "reviewer-2")
            second_review = await client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={
                    "decision": "APPROVE",
                    "reason": "second independent review",
                    "expected_version": current_version,
                    "idempotency_key": "high-risk-second-review",
                },
            )
            assert second_review.status_code == 200, second_review.text
            assert second_review.json()["status"] == "APPROVED"
            assert second_review.json()["detail"]["risk_decision"] is not None
            assert second_review.json()["detail"]["risk_decision"]["result"] == "DENY"
            assert second_review.json()["detail"]["risk_decision"]["reasons"] == [
                "READ_ONLY_SOURCE_UNAVAILABLE"
            ]
            assert len(telegram.notifications()) == 3

    asyncio.run(scenario())


def test_session_review_keeps_auth_rbac_scope_version_and_identity_fail_closed(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    now = datetime.now(UTC)
    scoped_account_id = "scope-acct-2"
    add_exchange_account_fixture(
        database,
        ids["admin"],
        scoped_account_id,
        "BINANCE",
        environment="TESTNET",
    )
    service.assign_role(
        ids["proposer"],
        Role.PROPOSER,
        ids["admin"],
        scoped_account_id,
        "BINANCE",
        now=now,
    )
    scope_proposal_id = service.create_proposal(
        actor_id=ids["proposer"],
        source=ProposalSource.MANUAL,
        risk_tier=RiskTier.LOW,
        account_id=scoped_account_id,
        venue="BINANCE",
        instrument_id=ids["instrument"],
        direction=Direction.LONG,
        quantity=Decimal("0.001"),
        max_risk=Decimal("1"),
        expires_at=now + timedelta(hours=2),
        idempotency_key="scope-mismatch-proposal",
        environment=ExecutionEnvironment.TESTNET,
        details={"trigger_price": "120000"},
        now=now,
    )
    service.submit_proposal(scope_proposal_id, ids["proposer"], now=now)

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app(database, MockTelegramGateway())),
            base_url="http://test",
        ) as client:
            await login(client, "proposer")
            created = await client.post(
                "/api/proposals/manual",
                json={
                    "environment": "TESTNET",
                    "account_id": "acct-1",
                    "venue": "BINANCE",
                    "instrument_id": str(ids["instrument"]),
                    "direction": "LONG",
                    "risk_tier": "HIGH",
                    "quantity": "0.001",
                    "max_risk": "2",
                    "expires_in_minutes": 480,
                    "trigger_price": "120000",
                    "invalidation_price": "118000",
                    "rationale": "exercise session review fail-closed boundaries",
                    "idempotency_key": "session-review-boundaries",
                },
            )
            assert created.status_code == 200, created.text
            proposal_id = created.json()["proposal_id"]
            original_version = created.json()["version"]
            payload = {
                "decision": "APPROVE",
                "reason": "independent session review",
                "expected_version": original_version,
                "idempotency_key": "unauthenticated-review",
            }

            await logout(client)
            unauthenticated = await client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json=payload,
            )
            assert unauthenticated.status_code == 401, unauthenticated.text

            await login(client, "proposer")
            self_review = await client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={**payload, "idempotency_key": "self-review"},
            )
            assert self_review.status_code == 403, self_review.text
            assert self_review.json()["error"]["code"] == "SELF_REVIEW_FORBIDDEN"

            await logout(client)
            await login(client, "operator")
            insufficient_role = await client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={**payload, "idempotency_key": "operator-review"},
            )
            assert insufficient_role.status_code == 403, insufficient_role.text
            assert insufficient_role.json()["error"]["code"] == "RBAC_DENIED"

            await logout(client)
            await login(client, "reviewer-1")
            scope_mismatch = await client.post(
                f"/api/proposals/{scope_proposal_id}/reviews",
                json={
                    "decision": "REJECT",
                    "reason": "must not cross the exact account scope",
                    "expected_version": 2,
                    "idempotency_key": "scope-mismatch-review",
                },
            )
            assert scope_mismatch.status_code == 403, scope_mismatch.text
            assert scope_mismatch.json()["error"]["code"] == "RBAC_DENIED"

            first = await client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={**payload, "idempotency_key": "first-session-review"},
            )
            assert first.status_code == 200, first.text
            assert first.json()["status"] == "PENDING_REVIEW"
            current_version = first.json()["detail"]["version"]

            duplicate = await client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={
                    **payload,
                    "expected_version": current_version,
                    "idempotency_key": "duplicate-session-review",
                },
            )
            assert duplicate.status_code == 409, duplicate.text
            assert duplicate.json()["error"]["code"] == "REVIEW_ALREADY_RECORDED"

            await logout(client)
            await login(client, "reviewer-2")
            stale = await client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={**payload, "idempotency_key": "stale-session-review"},
            )
            assert stale.status_code == 409, stale.text
            assert stale.json()["error"]["code"] == "VERSION_CONFLICT"

            second = await client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={
                    **payload,
                    "expected_version": current_version,
                    "idempotency_key": "second-session-review",
                },
            )
            assert second.status_code == 200, second.text
            assert second.json()["status"] == "APPROVED"

        with database.session_factory() as session:
            reviewers = session.scalars(
                select(AuditEvent.actor_id).where(
                    AuditEvent.event_type == "PROPOSAL_REVIEWED",
                    AuditEvent.object_id == proposal_id,
                )
            ).all()
            assert len(reviewers) == 2
            assert set(reviewers) == {
                str(ids["reviewer_one"]),
                str(ids["reviewer_two"]),
            }

    asyncio.run(scenario())


def test_authenticated_reviewer_approves_without_password_step_up(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    reviewer_password = "reviewer-one-password"  # noqa: S105
    service.ensure_local_human_password(
        "reviewer-1",
        reviewer_password,
        now=datetime.now(UTC),
    )

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app(database, MockTelegramGateway())),
            base_url="http://test",
        ) as client:
            await login(client, "proposer")
            created = await client.post(
                "/api/proposals/manual",
                json={
                    "environment": "TESTNET",
                    "account_id": "acct-1",
                    "venue": "BINANCE",
                    "instrument_id": str(ids["instrument"]),
                    "direction": "LONG",
                    "risk_tier": "LOW",
                    "quantity": "0.001",
                    "max_risk": "2",
                    "expires_in_minutes": 480,
                    "trigger_price": "120000",
                    "invalidation_price": "118000",
                    "rationale": "verify the authenticated reviewer session is sufficient",
                    "idempotency_key": "session-review-proposal",
                },
            )
            assert created.status_code == 200, created.text
            proposal_id = created.json()["proposal_id"]
            version = created.json()["version"]
            await logout(client)

            password_login = await client.post(
                "/api/auth/login",
                json={"username": "reviewer-1", "password": reviewer_password},
            )
            assert password_login.status_code == 200, password_login.text
            reviewed = await client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={
                    "decision": "APPROVE",
                    "reason": "session-authenticated independent review",
                    "expected_version": version,
                    "idempotency_key": "session-authenticated-review",
                },
            )
            assert reviewed.status_code == 200, reviewed.text
            assert reviewed.json()["status"] == "APPROVED"
            assert reviewed.json()["detail"]["risk_decision"]["result"] == "ALLOW"
            assert reviewed.json()["automation"]["status"] == "READY"
            assert reviewed.json()["detail"]["authorization"] is not None
            assert reviewed.json()["detail"]["initial_entry"]["intent_status"] == "READY"
            replay = advance_approved_proposal(
                service,
                proposal_id=UUID(proposal_id),
                fallback_service_username="runtime-sync",
                now=datetime.now(UTC),
            )
            assert replay["status"] == "READY"
            assert replay["authorization_id"] == reviewed.json()["automation"][
                "authorization_id"
            ]
            assert replay["intent_id"] == reviewed.json()["automation"]["intent_id"]

        with database.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(TradingAuthorization)) == 1
            assert session.scalar(select(func.count()).select_from(Campaign)) == 1
            assert session.scalar(select(func.count()).select_from(RiskReservation)) == 1
            assert session.scalar(select(func.count()).select_from(OrderIntent)) == 1
            assert session.scalar(select(func.count()).select_from(VenueOrder)) == 0
            authorization = session.scalar(select(TradingAuthorization))
            intent = session.scalar(select(OrderIntent))
            assert authorization is not None
            assert intent is not None
            assert authorization.actor_id == str(ids["runtime_sync"])
            assert intent.actor_id == str(ids["runtime_sync"])

    asyncio.run(scenario())


def test_approved_proposal_rechecks_denied_risk_only_after_new_facts(
    database: Database,
    service: TradingService,
) -> None:
    ids = seed(service)
    now = datetime.now(UTC)
    stale_at = now - timedelta(minutes=10)
    with database.session_factory.begin() as session:
        team_id = session.scalar(
            select(Position.team_id).where(
                Position.account_id == "acct-1",
                Position.venue == "BINANCE",
                Position.environment == "TESTNET",
            )
        )
        assert team_id is not None
        session.add(
            AccountEquity(
                account_equity_id=uuid4(),
                team_id=team_id,
                account_id="vault-1",
                venue="VAULT",
                environment="TESTNET",
                equity=Decimal("100"),
                available_balance=Decimal("100"),
                withdrawable_balance=Decimal("100"),
                currency="USDT",
                location_type="VAULT",
                control_status="CONTROLLED",
                deposit_status="READY",
                network="test",
                address_reference=None,
                valuation_currency="USD",
                valuation_price=Decimal(1),
                valuation_equity=Decimal("100"),
                valuation_observed_at=stale_at,
                fact_status="KNOWN",
                observed_at=stale_at,
                updated_at=stale_at,
            )
        )
        for position in session.scalars(
            select(Position).where(
                Position.account_id == "acct-1",
                Position.venue == "BINANCE",
                Position.environment == "TESTNET",
            )
        ):
            position.observed_at = stale_at
            position.updated_at = stale_at
        for equity in session.scalars(
            select(AccountEquity).where(
                AccountEquity.account_id == "acct-1",
                AccountEquity.venue == "BINANCE",
                AccountEquity.environment == "TESTNET",
            )
        ):
            equity.observed_at = stale_at
            equity.updated_at = stale_at

    proposal_id = service.create_proposal(
        actor_id=ids["proposer"],
        source=ProposalSource.MANUAL,
        risk_tier=RiskTier.LOW,
        account_id="acct-1",
        venue="BINANCE",
        instrument_id=ids["instrument"],
        direction=Direction.LONG,
        quantity=Decimal("0.001"),
        max_risk=Decimal(1),
        expires_at=now + timedelta(hours=8),
        idempotency_key="automatic-risk-retry-proposal",
        environment=ExecutionEnvironment.TESTNET,
        details={"trigger_price": "120000"},
        submit_for_review=True,
        now=now,
    )
    assert (
        service.review_proposal(
            proposal_id,
            ids["reviewer_one"],
            ReviewDecision.APPROVE,
            "approve frozen testnet proposal",
            automatic_risk_service_username="runtime-sync",
            now=now,
        ).value
        == "APPROVED"
    )
    with database.session_factory() as session:
        first = session.scalar(
            select(RiskDecision)
            .where(RiskDecision.proposal_id == proposal_id)
            .order_by(RiskDecision.created_at.desc())
        )
        assert first is not None
        assert first.result == "DENY"
        assert first.reasons == ["STALE_FACTS"]
        assert (
            session.scalar(
                select(func.count()).select_from(RiskDecision).where(
                    RiskDecision.proposal_id == proposal_id
                )
            )
            == 1
        )

    assert not refresh_approved_proposal_risk(
        service,
        proposal_id=proposal_id,
        fallback_service_username="runtime-sync",
        now=now + timedelta(seconds=1),
    )

    refreshed_at = now + timedelta(seconds=2)
    with database.session_factory.begin() as session:
        for position in session.scalars(
            select(Position).where(
                Position.account_id == "acct-1",
                Position.venue == "BINANCE",
                Position.environment == "TESTNET",
            )
        ):
            position.observed_at = refreshed_at
            position.updated_at = refreshed_at
        for equity in session.scalars(
            select(AccountEquity).where(
                AccountEquity.account_id == "acct-1",
                AccountEquity.venue == "BINANCE",
                AccountEquity.environment == "TESTNET",
            )
        ):
            equity.observed_at = refreshed_at
            equity.updated_at = refreshed_at

    for offset in range(3, 8):
        assert not refresh_approved_proposal_risk(
            service,
            proposal_id=proposal_id,
            fallback_service_username="runtime-sync",
            now=now + timedelta(seconds=offset),
        )
    with database.session_factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(RiskDecision).where(
                    RiskDecision.proposal_id == proposal_id
                )
            )
            == 1
        )

    vault_refreshed_at = now + timedelta(seconds=8)
    with database.session_factory.begin() as session:
        vault = session.scalar(
            select(AccountEquity).where(
                AccountEquity.account_id == "vault-1",
                AccountEquity.venue == "VAULT",
                AccountEquity.environment == "TESTNET",
            )
        )
        assert vault is not None
        vault.observed_at = vault_refreshed_at
        vault.valuation_observed_at = vault_refreshed_at
        vault.updated_at = vault_refreshed_at

    assert refresh_approved_proposal_risk(
        service,
        proposal_id=proposal_id,
        fallback_service_username="runtime-sync",
        now=now + timedelta(seconds=9),
    )
    assert not refresh_approved_proposal_risk(
        service,
        proposal_id=proposal_id,
        fallback_service_username="runtime-sync",
        now=now + timedelta(seconds=10),
    )
    service.set_risk_policy(
        actor_id=ids["admin"],
        version="m1-risk-v2",
        system_state=SystemRiskState.NORMAL,
        max_total_risk=Decimal(100),
        max_account_risk=Decimal(100),
        max_single_loss=Decimal(100),
        max_consecutive_losses=3,
        loss_cooldown=timedelta(hours=1),
        max_fact_age=timedelta(minutes=5),
        now=now + timedelta(seconds=11),
    )
    assert refresh_approved_proposal_risk(
        service,
        proposal_id=proposal_id,
        fallback_service_username="runtime-sync",
        now=now + timedelta(seconds=12),
    )
    assert not refresh_approved_proposal_risk(
        service,
        proposal_id=proposal_id,
        fallback_service_username="runtime-sync",
        now=now + timedelta(seconds=13),
    )
    with database.session_factory() as session:
        decisions = session.scalars(
            select(RiskDecision)
            .where(RiskDecision.proposal_id == proposal_id)
            .order_by(RiskDecision.created_at)
        ).all()
        assert [item.result for item in decisions] == ["DENY", "ALLOW", "ALLOW"]
        assert decisions[-1].actor_id == str(ids["runtime_sync"])


def test_manual_proposal_accepts_u_margin_amount_and_resolves_frozen_quantity(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    live_account_id = add_live_account(service, ids)
    set_test_team_environment(database, ids["admin"], "LIVE")
    service.register_instrument(
        actor_id=ids["admin"],
        venue="BINANCE",
        symbol="BTCUSD_PERP",
        tick_size=Decimal("0.1"),
        lot_size=Decimal("1"),
        minimum_notional=Decimal("100"),
        contract_multiplier=Decimal("100"),
        quote_currency="USD",
        collateral_currency="BTC",
        protection_supported=True,
        now=datetime.now(UTC),
    )

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app(database, MockTelegramGateway())),
            base_url="http://test",
        ) as client:
            await login(client, "proposer")
            catalog = await client.get("/api/instruments")
            assert catalog.status_code == 200, catalog.text
            assert catalog.json()["catalog_scope"] == {
                "contract_family": "U_MARGINED_PERPETUAL",
                "strategy_allowlist_applied": False,
                "exchange_trading_status_required": True,
            }
            assert [item["symbol"] for item in catalog.json()["data"]] == ["BTCUSDT"]

            payload = {
                "environment": "LIVE",
                "account_id": live_account_id,
                "venue": "BINANCE",
                "instrument_id": str(ids["instrument"]),
                "direction": "LONG",
                "risk_tier": "MEDIUM",
                "max_position_notional": "250",
                "max_risk": "20",
                "expires_in_minutes": 480,
                "trigger_price": "120000",
                "invalidation_price": "118000",
                "rationale": "freeze a U-margined amount and resolve exact contract quantity",
                "idempotency_key": "manual-usdt-notional",
            }
            created = await client.post("/api/proposals/manual", json=payload)
            assert created.status_code == 200, created.text
            assert Decimal(created.json()["quantity"]) == Decimal("0.002")
            assert Decimal(created.json()["estimated_notional"]) == Decimal("240")
            details = created.json()["frozen_payload"]["details"]
            assert details["requested_max_position_notional"] == "250"
            assert Decimal(details["resolved_position_notional"]) == Decimal("240")
            assert details["position_notional_currency"] == "USDT"
            assert Decimal(details["initial_quantity"]) == Decimal("0.002")

            too_small = await client.post(
                "/api/proposals/manual",
                json={
                    **payload,
                    "max_position_notional": "100",
                    "initial_position_notional": None,
                    "idempotency_key": "manual-usdt-notional-too-small",
                },
            )
            assert too_small.status_code == 422
            assert too_small.json()["error"]["code"] == "POSITION_NOTIONAL_TOO_SMALL"

    asyncio.run(scenario())
