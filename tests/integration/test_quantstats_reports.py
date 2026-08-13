from __future__ import annotations

import asyncio
import importlib.util
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from test_m7_capital_center import login
from test_shadow_mode import (
    configure_shadow_prerequisites,
    shadow_app,
    shadow_team_fixture,
)

from trading_control_plane.binance_execution import BinancePortfolioMarginClient
from trading_control_plane.capital import MockCapitalTransferAdapter
from trading_control_plane.database import Database
from trading_control_plane.domain import DomainRejected, ExecutionEnvironment, Role
from trading_control_plane.freqtrade import FreqtradeWorkerClient
from trading_control_plane.hyperliquid_execution import HyperliquidLiveClient
from trading_control_plane.models import (
    AnalyticsEquitySnapshot,
    AnalyticsReport,
    AuditEvent,
    Team,
    TeamShadowAccount,
    User,
    VenueFill,
)
from trading_control_plane.quantstats_adapter import QuantStatsReportAdapter
from trading_control_plane.queries import TradingQueries
from trading_control_plane.report_engines import ReportArtifact

ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "shadow_analytics_fixture", ROOT / "scripts" / "seed_shadow_analytics_fixture.py"
)
assert _FIXTURE_SPEC is not None and _FIXTURE_SPEC.loader is not None
_FIXTURE_MODULE = importlib.util.module_from_spec(_FIXTURE_SPEC)
_FIXTURE_SPEC.loader.exec_module(_FIXTURE_MODULE)
cleanup = _FIXTURE_MODULE.cleanup
resolve_scope = _FIXTURE_MODULE.resolve_scope
seed = _FIXTURE_MODULE.seed

START = datetime(2026, 7, 1, tzinfo=UTC)


def activate_shadow(database: Database):
    service, ids = shadow_team_fixture(database)
    configure_shadow_prerequisites(service, ids)
    service.activate_team_shadow_mode(
        actor_id=ids["admin"],
        team_id=ids["team"],
        expected_version=1,
        idempotency_key="activate-analytics-shadow",
        now=START,
    )
    return service, ids


def seed_shadow_nav(
    database: Database,
    *,
    team_id: object,
    generation: int,
    start: datetime,
    days: int = 31,
) -> None:
    with database.session_factory.begin() as session:
        session.add_all(
            [
                AnalyticsEquitySnapshot(
                    team_id=team_id,
                    environment="SHADOW",
                    account_id="TEAM_SHADOW",
                    venue="TRADINGOPS",
                    generation=generation,
                    equity=Decimal("100000") + Decimal(offset * 100),
                    currency="U",
                    source_kind="INTEGRATION_DAILY_NAV",
                    source_id=f"generation:{generation}:day:{offset}",
                    version=offset + 1,
                    fact_metadata={"fixture": True},
                    observed_at=start + timedelta(days=offset),
                    recorded_at=start + timedelta(days=offset),
                )
                for offset in range(days + 1)
            ]
        )


def test_local_shadow_fixture_is_deterministic_idempotent_and_cleanable(
    database: Database,
) -> None:
    activate_shadow(database)
    with database.session_factory.begin() as session:
        _user, team, shadow_account, account = resolve_scope(session, "shadow-admin")
        created = seed(session, team, shadow_account, account)
    with database.session_factory.begin() as session:
        _user, team, shadow_account, account = resolve_scope(session, "shadow-admin")
        replay = seed(session, team, shadow_account, account)
    assert created["status"] == "CREATED"
    assert created["generation"] == 1
    assert created["nav_points"] == 121
    assert created["return_points"] == 120
    assert created["orders"] == created["fills"] == 24
    assert created["positions"] == 1
    assert replay == {
        "fixture_id": "TRADINGOPS_SHADOW_ANALYTICS_V1",
        "status": "ALREADY_PRESENT",
        "generation": 1,
    }
    with database.session_factory.begin() as session:
        _user, team, shadow_account, _account = resolve_scope(session, "shadow-admin")
        removed = cleanup(session, team, shadow_account)
    assert removed == {
        "snapshots_deleted": 121,
        "orders_deleted": 24,
        "fills_deleted": 24,
        "positions_deleted": 1,
    }


def test_shadow_dataset_and_reset_keep_generations_separate(database: Database) -> None:
    service, ids = activate_shadow(database)
    seed_shadow_nav(
        database,
        team_id=ids["team"],
        generation=1,
        start=START,
        days=2,
    )
    queries = TradingQueries(database)

    first = queries.analytics_dataset(
        ids["admin"],
        "SHADOW",
        account_id=None,
        venue=None,
        generation=1,
        from_time=START,
        to_time=START + timedelta(days=2),
    )
    reset = service.reset_shadow_account(
        actor_id=ids["admin"],
        expected_version=1,
        confirmation="RESET_TO_100000_U",
        idempotency_key="reset-analytics-shadow",
        now=START + timedelta(days=3),
    )
    seed_shadow_nav(
        database,
        team_id=ids["team"],
        generation=2,
        start=START + timedelta(days=3),
        days=2,
    )
    second = queries.analytics_dataset(
        ids["admin"],
        "SHADOW",
        account_id=None,
        venue=None,
        generation=2,
        from_time=START + timedelta(days=3),
        to_time=START + timedelta(days=5),
    )

    assert first.scope.generation == 1
    assert second.scope.generation == 2
    assert any("generation:1" in point.source_id for point in first.nav_series)
    assert not any("generation:2" in point.source_id for point in first.nav_series)
    assert any("generation:2" in point.source_id for point in second.nav_series)
    assert not any("generation:1" in point.source_id for point in second.nav_series)
    assert reset["previous_generation"] == 1
    assert reset["shadow_account"]["equity"] == "100000"
    with database.session_factory() as session:
        accounts = session.scalars(
            select(TeamShadowAccount).order_by(TeamShadowAccount.generation)
        ).all()
        assert [(item.generation, item.status) for item in accounts] == [
            (1, "ARCHIVED"),
            (2, "ACTIVE"),
        ]
        assert session.scalar(select(func.count(AnalyticsEquitySnapshot.snapshot_id))) == 8


def test_live_dataset_uses_trusted_nav_and_never_mixes_shadow(database: Database) -> None:
    service, ids = activate_shadow(database)
    for offset in range(4):
        service.record_account_equity(
            account_id="paper-1",
            venue="BINANCE",
            equity=Decimal("50000") + Decimal(offset * 250),
            available_balance=Decimal("50000") + Decimal(offset * 250),
            currency="USDT",
            known=True,
            actor_id=ids["admin"],
            environment=ExecutionEnvironment.LIVE,
            observed_at=START + timedelta(days=offset),
            now=START + timedelta(days=4),
        )
    with database.session_factory.begin() as session:
        session.add(
            VenueFill(
                team_id=ids["team"],
                venue="BINANCE",
                venue_fill_id="live-partial-fill-1",
                order_intent_id=None,
                campaign_id=None,
                account_id="paper-1",
                environment="LIVE",
                instrument_id=ids["instrument"],
                side="BUY",
                quantity=Decimal("0.25"),
                price=Decimal("25000"),
                fee=Decimal("2.5"),
                fee_currency="USDT",
                slippage_cost=Decimal("1"),
                executed_at=START + timedelta(days=2),
            )
        )

    queries = TradingQueries(database)
    live = queries.analytics_dataset(
        ids["admin"],
        "LIVE",
        account_id="paper-1",
        venue="BINANCE",
        generation=None,
        from_time=START,
        to_time=START + timedelta(days=3),
    )

    assert live.scope.environment == "LIVE"
    assert live.scope.generation is None
    assert len(live.nav_series) == 4
    assert len(live.returns) == 3
    assert len(live.transactions) == 1
    assert live.transactions[0].quantity == Decimal("0.25")
    assert live.transactions[0].idempotency_key == ("LIVE:paper-1:BINANCE:live-partial-fill-1")
    assert live.coverage["transaction_count"] == 1
    assert live.coverage["positions_complete"] is False
    assert live.metadata["position_history_source"] == (
        "CURRENT_STATE_ONLY_NO_HISTORICAL_SNAPSHOTS"
    )
    assert live.scope.account_venues == (("paper-1", "BINANCE"),)
    assert all(point.currency == "USD" for point in live.nav_series)
    assert "TEAM_SHADOW_ACCOUNT" not in live.metadata["source_facts"]
    with database.session_factory() as session:
        team = session.get(Team, ids["team"])
        assert team is not None and team.execution_mode == "SHADOW"


def test_live_report_access_checks_exact_account_and_venue_pair(database: Database) -> None:
    service, ids = activate_shadow(database)
    service.create_exchange_account(
        actor_id=ids["admin"],
        account_id="paper-1",
        venue="BYBIT",
        label="Same identifier on a different venue",
        credentials=None,
        idempotency_key="analytics-bybit-account",
        now=START,
    )
    for offset in range(4):
        observed_at = START + timedelta(days=offset)
        service.record_account_equity(
            account_id="paper-1",
            venue="BYBIT",
            equity=Decimal("40000") + Decimal(offset * 100),
            available_balance=Decimal("40000") + Decimal(offset * 100),
            currency="USDT",
            known=True,
            actor_id=ids["admin"],
            environment=ExecutionEnvironment.LIVE,
            observed_at=observed_at,
            now=START + timedelta(days=4),
        )
    dataset = TradingQueries(database).analytics_dataset(
        ids["admin"],
        "LIVE",
        account_id="paper-1",
        venue="BYBIT",
        generation=None,
        from_time=START,
        to_time=START + timedelta(days=3),
    )
    assert dataset.scope.account_venues == (("paper-1", "BYBIT"),)
    persisted = service.persist_analytics_report(
        ids["admin"],
        dataset,
        ReportArtifact(
            engine="QUANTSTATS",
            library="QuantStats",
            library_version="0.0.81",
            html="<html><body>exact scope fixture</body></html>",
            metrics={},
            chart_count=0,
            readiness={
                "RETURNS_READY": False,
                "POSITIONS_READY": False,
                "TRANSACTIONS_READY": True,
                "BENCHMARK_READY": False,
            },
        ),
        "exact-account-venue-report",
        now=START + timedelta(days=5),
    )
    restricted_id = service.create_managed_user(
        "binance-report-reader",
        [Role.OBSERVER],
        ids["admin"],
        "paper-1",
        "BINANCE",
        "ordinary-user-password",
        now=START + timedelta(days=5),
    )

    with pytest.raises(DomainRejected) as rejected:
        TradingQueries(database).analytics_report(
            restricted_id,
            UUID(persisted["report_id"]),
        )
    assert rejected.value.code == "ANALYTICS_ACCOUNT_SCOPE_DENIED"


def test_quantstats_api_is_scoped_offline_and_preserves_legacy_results(
    database: Database,
) -> None:
    _service, ids = activate_shadow(database)
    seed_shadow_nav(
        database,
        team_id=ids["team"],
        generation=1,
        start=START + timedelta(hours=1),
    )

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=shadow_app(database)),
            base_url="http://test",
        ) as client:
            await login(client, "shadow-admin")
            options = await client.get("/api/results/quantstats/options")
            assert options.status_code == 200
            assert options.json()["data"]["current_trading_mode"] == "SHADOW"
            assert options.json()["data"]["environments"] == ["SHADOW", "LIVE"]

            with (
                patch.object(
                    BinancePortfolioMarginClient,
                    "ensure_order",
                    side_effect=AssertionError("exchange write"),
                ) as binance_write,
                patch.object(
                    HyperliquidLiveClient,
                    "ensure_order",
                    side_effect=AssertionError("exchange write"),
                ) as hyperliquid_write,
                patch.object(
                    FreqtradeWorkerClient,
                    "force_enter",
                    side_effect=AssertionError("exchange write"),
                ) as freqtrade_write,
                patch.object(
                    FreqtradeWorkerClient,
                    "force_exit",
                    side_effect=AssertionError("exchange write"),
                ) as freqtrade_exit,
                patch.object(
                    BinancePortfolioMarginClient,
                    "cancel_order",
                    side_effect=AssertionError("exchange write"),
                ) as binance_cancel,
                patch.object(
                    BinancePortfolioMarginClient,
                    "ensure_protection",
                    side_effect=AssertionError("exchange write"),
                ) as binance_protection,
                patch.object(
                    HyperliquidLiveClient,
                    "cancel_order",
                    side_effect=AssertionError("exchange write"),
                ) as hyperliquid_cancel,
                patch.object(
                    HyperliquidLiveClient,
                    "ensure_protection",
                    side_effect=AssertionError("exchange write"),
                ) as hyperliquid_protection,
                patch.object(
                    MockCapitalTransferAdapter,
                    "submit",
                    side_effect=AssertionError("capital write"),
                ) as capital_write,
            ):
                report = await client.get(
                    "/api/results/quantstats",
                    params={
                        "environment": "SHADOW",
                        "generation": 1,
                        "from_time": (START + timedelta(hours=1)).isoformat(),
                        "to_time": (START + timedelta(days=31, hours=1)).isoformat(),
                    },
                )
            assert report.status_code == 200, report.text
            data = report.json()["data"]
            assert data["environment"] == "SHADOW"
            assert data["generation"] == 1
            assert data["data_status"] == "READY"
            assert data["quantstats"]["library"] == "QuantStats"
            assert data["quantstats"]["version"] == "0.0.81"
            assert data["quantstats"]["periods_per_year"] == 365
            assert data["quantstats"]["external_market_downloads"] is False
            assert data["quantstats"]["exchange_write_adapter_calls"] == 0
            assert data["quantstats"]["readiness"] == {
                "RETURNS_READY": True,
                "POSITIONS_READY": True,
                "TRANSACTIONS_READY": True,
                "BENCHMARK_READY": False,
            }
            assert "<script" not in data["report_html"].lower()
            assert "content-security-policy" in data["report_html"].lower()
            assert binance_write.call_count == 0
            assert hyperliquid_write.call_count == 0
            assert freqtrade_write.call_count == 0
            assert freqtrade_exit.call_count == 0
            assert binance_cancel.call_count == 0
            assert binance_protection.call_count == 0
            assert hyperliquid_cancel.call_count == 0
            assert hyperliquid_protection.call_count == 0
            assert capital_write.call_count == 0

            legacy = await client.get("/api/results?environment=SHADOW")
            assert legacy.status_code == 200
            assert legacy.json()["data"]["environment"] == "SHADOW"
            denied = await client.get(
                "/api/results/quantstats",
                params={
                    "environment": "SHADOW",
                    "account_id": "other-team-account",
                    "generation": 1,
                    "from_time": (START + timedelta(hours=1)).isoformat(),
                    "to_time": (START + timedelta(days=31, hours=1)).isoformat(),
                },
            )
            assert denied.status_code == 422
            assert denied.json()["error"]["code"] == "ANALYTICS_SHADOW_SCOPE_INVALID"

    asyncio.run(scenario())


def test_quantstats_failure_does_not_affect_trading_or_legacy_results(
    database: Database,
) -> None:
    _service, ids = activate_shadow(database)
    seed_shadow_nav(
        database,
        team_id=ids["team"],
        generation=1,
        start=START + timedelta(hours=1),
        days=2,
    )

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=shadow_app(database)),
            base_url="http://test",
        ) as client:
            await login(client, "shadow-admin")
            with patch.object(
                QuantStatsReportAdapter,
                "render",
                side_effect=DomainRejected("QUANTSTATS_REPORT_FAILED", "isolated report failure"),
            ):
                failed = await client.get(
                    "/api/results/quantstats",
                    params={
                        "environment": "SHADOW",
                        "generation": 1,
                        "from_time": (START + timedelta(hours=1)).isoformat(),
                        "to_time": (START + timedelta(days=2, hours=1)).isoformat(),
                    },
                )
            assert failed.status_code == 422
            assert failed.json()["error"]["code"] == "QUANTSTATS_REPORT_FAILED"
            assert (await client.get("/api/trading-mode")).status_code == 200
            assert (await client.get("/api/results?environment=SHADOW")).status_code == 200

    asyncio.run(scenario())


def test_dual_report_tasks_persist_real_artifacts_and_are_idempotent(
    database: Database,
) -> None:
    _service, ids = activate_shadow(database)
    seed_shadow_nav(
        database,
        team_id=ids["team"],
        generation=1,
        start=START + timedelta(hours=1),
        days=45,
    )
    with database.session_factory.begin() as session:
        rows = session.scalars(
            select(AnalyticsEquitySnapshot).where(
                AnalyticsEquitySnapshot.team_id == ids["team"],
                AnalyticsEquitySnapshot.source_kind == "INTEGRATION_DAILY_NAV",
            )
        ).all()
        for offset, row in enumerate(rows):
            if offset and offset % 7 == 0:
                row.equity -= Decimal("450")

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=shadow_app(database)),
            base_url="http://test",
        ) as client:
            await login(client, "shadow-admin")
            catalog = await client.get("/api/results/report-engines")
            assert catalog.status_code == 200
            assert {item["engine"] for item in catalog.json()["data"]["engines"]} == {
                "QUANTSTATS",
                "PYFOLIO",
            }
            report_ids: list[str] = []
            for engine in ("QUANTSTATS", "PYFOLIO"):
                payload = {
                    "engine": engine,
                    "environment": "SHADOW",
                    "generation": 1,
                    "from_time": (START + timedelta(hours=1)).isoformat(),
                    "to_time": (START + timedelta(days=45, hours=1)).isoformat(),
                    "idempotency_key": f"dual-report-{engine.lower()}",
                }
                with (
                    patch.object(
                        BinancePortfolioMarginClient,
                        "ensure_order",
                        side_effect=AssertionError("exchange write"),
                    ) as binance_write,
                    patch.object(
                        HyperliquidLiveClient,
                        "ensure_order",
                        side_effect=AssertionError("exchange write"),
                    ) as hyperliquid_write,
                    patch.object(
                        FreqtradeWorkerClient,
                        "force_enter",
                        side_effect=AssertionError("exchange write"),
                    ) as freqtrade_write,
                    patch.object(
                        FreqtradeWorkerClient,
                        "force_exit",
                        side_effect=AssertionError("exchange write"),
                    ) as freqtrade_exit,
                    patch.object(
                        BinancePortfolioMarginClient,
                        "cancel_order",
                        side_effect=AssertionError("exchange write"),
                    ) as binance_cancel,
                    patch.object(
                        BinancePortfolioMarginClient,
                        "ensure_protection",
                        side_effect=AssertionError("exchange write"),
                    ) as binance_protection,
                    patch.object(
                        HyperliquidLiveClient,
                        "cancel_order",
                        side_effect=AssertionError("exchange write"),
                    ) as hyperliquid_cancel,
                    patch.object(
                        HyperliquidLiveClient,
                        "ensure_protection",
                        side_effect=AssertionError("exchange write"),
                    ) as hyperliquid_protection,
                    patch.object(
                        MockCapitalTransferAdapter,
                        "submit",
                        side_effect=AssertionError("capital write"),
                    ) as capital_write,
                ):
                    created = await client.post("/api/results/reports", json=payload)
                assert created.status_code == 201, created.text
                data = created.json()["data"]
                assert data["status"] == "READY"
                assert data["engine"] == engine
                assert data["chart_count"] > 0
                assert set(data["metrics"]) == {
                    "total_return",
                    "annual_return",
                    "annual_volatility",
                    "sharpe",
                    "sortino",
                    "max_drawdown",
                    "win_rate",
                    "fees",
                }
                report_id = data["report_id"]
                report_ids.append(report_id)
                assert (await client.get(f"/api/results/reports/{report_id}")).status_code == 200
                artifact = await client.get(f"/api/results/reports/{report_id}/artifact")
                assert artifact.status_code == 200
                assert len(artifact.content) > 1000
                assert "content-security-policy" in artifact.text.lower()
                download = await client.get(f"/api/results/reports/{report_id}/download")
                assert download.status_code == 200
                assert "attachment" in download.headers["content-disposition"]
                replay = await client.post("/api/results/reports", json=payload)
                assert replay.status_code == 201
                assert replay.json()["data"]["report_id"] == report_id
                assert binance_write.call_count == 0
                assert hyperliquid_write.call_count == 0
                assert freqtrade_write.call_count == 0
                assert freqtrade_exit.call_count == 0
                assert binance_cancel.call_count == 0
                assert binance_protection.call_count == 0
                assert hyperliquid_cancel.call_count == 0
                assert hyperliquid_protection.call_count == 0
                assert capital_write.call_count == 0
            with database.session_factory() as session:
                assert session.scalar(select(func.count()).select_from(AnalyticsReport)) == 2
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(AuditEvent)
                        .where(
                            AuditEvent.object_id.in_(report_ids),
                            AuditEvent.event_type == "ANALYTICS_REPORT_GENERATED",
                        )
                    )
                    == 2
                )

    asyncio.run(scenario())


def test_report_artifact_is_denied_after_active_team_changes(database: Database) -> None:
    service, ids = activate_shadow(database)
    seed_shadow_nav(
        database,
        team_id=ids["team"],
        generation=1,
        start=START + timedelta(hours=1),
        days=32,
    )
    with database.session_factory.begin() as session:
        rows = session.scalars(
            select(AnalyticsEquitySnapshot).where(
                AnalyticsEquitySnapshot.team_id == ids["team"],
                AnalyticsEquitySnapshot.source_kind == "INTEGRATION_DAILY_NAV",
            )
        ).all()
        for offset, row in enumerate(rows):
            if offset and offset % 7 == 0:
                row.equity -= Decimal("450")
    dataset = TradingQueries(database).analytics_dataset(
        ids["admin"],
        "SHADOW",
        account_id=None,
        venue=None,
        generation=1,
        from_time=START + timedelta(hours=1),
        to_time=START + timedelta(days=32, hours=1),
    )
    from trading_control_plane.report_engines import render_report

    persisted = service.persist_analytics_report(
        ids["admin"],
        dataset,
        render_report("PYFOLIO", dataset),
        "scope-report",
        now=START + timedelta(days=33),
    )
    other_team_id = service.create_team(
        actor_id=ids["admin"],
        name="Other Analytics Team",
        slug="other-analytics-team",
        idempotency_key="create-other-analytics-team",
        now=START + timedelta(days=34),
    )
    with database.session_factory.begin() as session:
        user = session.get(User, ids["admin"])
        assert user is not None
        user.active_team_id = other_team_id
    with pytest.raises(DomainRejected) as rejected:
        TradingQueries(database).analytics_report(ids["admin"], UUID(persisted["report_id"]))
    assert rejected.value.code == "ANALYTICS_REPORT_NOT_FOUND"
