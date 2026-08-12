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
from trading_control_plane.binance import BinanceInstrument
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    ProposalSource,
    RiskTier,
    Role,
    SystemRiskState,
)
from trading_control_plane.models import (
    AuditEvent,
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
    runtime_sync = service.create_service_principal("runtime-sync", admin, now=now)
    service.assign_role(proposer, Role.PROPOSER, admin, "acct-1", "BINANCE", now=now)
    service.assign_role(reviewer_one, Role.REVIEWER, admin, "acct-1", "BINANCE", now=now)
    service.assign_role(reviewer_two, Role.REVIEWER, admin, "acct-1", "BINANCE", now=now)
    service.assign_role(operator, Role.OPERATOR, admin, "acct-1", "BINANCE", now=now)
    service.assign_role(perptape, Role.PROPOSER, admin, "acct-1", "BINANCE", now=now)
    service.assign_role(runtime_sync, Role.OPERATOR, admin, "acct-1", "BINANCE", now=now)
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
        max_account_risk=Decimal("100"),
        max_single_loss=Decimal("100"),
        max_consecutive_losses=3,
        loss_cooldown=timedelta(hours=1),
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
        "runtime_sync": runtime_sync,
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
    *,
    catalog_active: bool = True,
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

    class StaticBinanceCatalog:
        configured = True

        def read_instrument(self, symbol: str) -> BinanceInstrument:
            return BinanceInstrument(
                symbol=symbol,
                tick_size=Decimal("0.1"),
                lot_size=Decimal("0.001"),
                minimum_notional=Decimal("5"),
                quote_currency="USDC" if symbol.endswith("USDC") else "USDT",
                collateral_currency="USDC" if symbol.endswith("USDC") else "USDT",
                active=catalog_active,
            )

    return create_app(
        settings,
        database,
        client or perptape_client(),
        telegram,
        binance_client=StaticBinanceCatalog(),  # type: ignore[arg-type]
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
            assert loosened.status_code == 422
            assert loosened.json()["error"]["code"] == "REVIEWED_POLICY_CHANGE_REQUIRED"

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
                    "expected_revision": 2,
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
            assert payload["live_order_send"] is False
            assert [(item["venue"], item["status"]) for item in payload["workers"]] == [
                ("BINANCE", "DISABLED"),
                ("HYPERLIQUID", "DISABLED"),
            ]
            assert payload["workers"][1]["hip3_dexes"] == ["xyz"]

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
            BinanceInstrument(
                symbol="BTCUSDT",
                tick_size=Decimal("0.1"),
                lot_size=Decimal("0.001"),
                minimum_notional=Decimal("5"),
                quote_currency="USDT",
                collateral_currency="USDT",
                active=True,
            ),
            BinanceInstrument(
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
            BinanceInstrument(
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
                    catalog_active=False,
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
        "max_risk": "40",
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
                "expires_in_minutes": 480,
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
            assert proposal["proposer_username"] == "perptape"
            assert proposal["quote_currency"] == "USDT"
            assert proposal["collateral_currency"] == "USDT"
            proposal_id = proposal["proposal_id"]
            version = proposal["version"]

            listed = await client.get("/api/proposals?proposal_status=PENDING_REVIEW")
            assert listed.status_code == 200, listed.text
            assert listed.json()["data"][0]["symbol"] == "BTCUSDT"
            assert listed.json()["data"][0]["proposer_username"] == "perptape"
            assert listed.json()["data"][0]["collateral_currency"] == "USDT"
            assert listed.json()["data"][0]["estimated_notional"] == "120000.000000000000000000"
            assert listed.json()["data"][0]["campaign_id"] is None

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
            assert approved.json()["detail"]["approvals"][0]["reviewer_username"] == ("reviewer-1")

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
            risk_detail = risk.json()["detail"]["risk_decision"]
            assert risk_detail["result"] == "ALLOW"
            assert risk_detail["created_at"] is not None
            assert Decimal(risk_detail["context"]["requested_quantity"]) == 1
            assert risk_detail["context"]["position_status"] == "KNOWN"
            assert risk_detail["context"]["equity_status"] == "KNOWN"
            assert risk_detail["context"]["managed_capital_known"] is True
            assert risk_detail["context"]["protection_status"] == "NOT_REQUIRED"
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
            assert Decimal(detail["authorization"]["used_quantity"]) == 0
            assert Decimal(detail["authorization"]["remaining_quantity"]) == 1
            assert detail["authorization"]["created_at"] is not None
            assert detail["initial_entry"] is None
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
                    "expires_in_minutes": 480,
                    "invalidation_price": "118000",
                    "rationale": "explicit live proposal still requires review and risk",
                },
            )
            assert created.status_code == 200, created.text
            assert created.json()["environment"] == "LIVE"
            assert created.json()["status"] == "PENDING_REVIEW"

    asyncio.run(scenario())


def test_high_risk_review_refreshes_only_the_remaining_reviewer_notification(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
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
                    "account_id": "acct-1",
                    "venue": "BINANCE",
                    "instrument_id": str(ids["instrument"]),
                    "direction": "LONG",
                    "risk_tier": "HIGH",
                    "quantity": "0.001",
                    "max_risk": "1",
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
            first_grant = await client.post(
                "/api/auth/mock/step-up",
                json={
                    "action": "proposal.approve",
                    "object_id": proposal_id,
                    "object_version": first_version,
                },
            )
            first_review = await client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={
                    "decision": "APPROVE",
                    "reason": "first independent review",
                    "expected_version": first_version,
                    "action_grant": first_grant.json()["action_grant"],
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
            second_grant = await client.post(
                "/api/auth/mock/step-up",
                json={
                    "action": "proposal.approve",
                    "object_id": proposal_id,
                    "object_version": current_version,
                },
            )
            second_review = await client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={
                    "decision": "APPROVE",
                    "reason": "second independent review",
                    "expected_version": current_version,
                    "action_grant": second_grant.json()["action_grant"],
                },
            )
            assert second_review.status_code == 200, second_review.text
            assert second_review.json()["status"] == "APPROVED"
            assert len(telegram.notifications()) == 3

    asyncio.run(scenario())


def test_manual_api_is_idempotent_and_semantic_conflicts_are_explicit(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    second_proposer = service.create_user("proposer-2", ids["admin"], now=datetime.now(UTC))
    service.assign_role(
        second_proposer,
        Role.PROPOSER,
        ids["admin"],
        "acct-1",
        "BINANCE",
        now=datetime.now(UTC),
    )
    service.assign_role(
        second_proposer,
        Role.REVIEWER,
        ids["admin"],
        "acct-1",
        "BINANCE",
        now=datetime.now(UTC),
    )
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
                "expires_in_minutes": 480,
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
            assert len(telegram.notifications()) == 3

            same_trade = {
                **payload,
                "idempotency_key": "manual-api-2",
                "rationale": "same frozen trade with different human commentary",
            }
            reused = await client.post("/api/proposals/manual", json=same_trade)
            assert reused.status_code == 200
            assert reused.json()["proposal_id"] == first.json()["proposal_id"]
            assert len(telegram.notifications()) == 3

            await login(client, "proposer-2")
            cross_proposer = await client.post(
                "/api/proposals/manual",
                json={**payload, "idempotency_key": "manual-api-cross-proposer"},
            )
            assert cross_proposer.status_code == 200
            assert cross_proposer.json()["proposal_id"] == first.json()["proposal_id"]
            assert cross_proposer.json()["actionable_for_current_user"] is False
            assert len(telegram.notifications()) == 3
            eligible_reviewer_ids = {
                item.user_id
                for item in TradingQueries(database).reviewers_for_proposal(
                    UUID(first.json()["proposal_id"])
                )
            }
            assert second_proposer not in eligible_reviewer_ids
            self_review = await client.post(
                f"/api/proposals/{first.json()['proposal_id']}/reviews",
                json={
                    "decision": "REJECT",
                    "reason": "must not review a proposal I tried to create",
                    "expected_version": first.json()["version"],
                },
            )
            assert self_review.status_code == 403
            assert self_review.json()["error"]["code"] == "SELF_REVIEW_FORBIDDEN"
            await login(client, "proposer")

            live_scope = await client.post(
                "/api/proposals/manual",
                json={**payload, "idempotency_key": "manual-api-live", "environment": "LIVE"},
            )
            assert live_scope.status_code == 200
            assert live_scope.json()["proposal_id"] != first.json()["proposal_id"]
            assert len(telegram.notifications()) == 6

            conflict = await client.post(
                "/api/proposals/manual", json={**payload, "quantity": "0.2"}
            )
            assert conflict.status_code == 409
            assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

            distinct = await client.post(
                "/api/proposals/manual",
                json={**payload, "idempotency_key": "manual-api-3", "quantity": "0.2"},
            )
            assert distinct.status_code == 200
            assert distinct.json()["proposal_id"] != first.json()["proposal_id"]
            assert len(telegram.notifications()) == 9

    asyncio.run(scenario())
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Proposal)) == 3
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "PROPOSAL_DUPLICATE_REUSED")
            )
            == 2
        )


def test_manual_proposal_accepts_u_margin_amount_and_resolves_frozen_quantity(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
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
                "account_id": "acct-1",
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
