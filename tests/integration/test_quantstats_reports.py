from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from test_m7_capital_center import login
from test_shadow_mode import (
    configure_shadow_prerequisites,
    shadow_app,
    shadow_team_fixture,
)

from trading_control_plane.binance_execution import BinancePortfolioMarginClient
from trading_control_plane.database import Database
from trading_control_plane.domain import DomainRejected, ExecutionEnvironment
from trading_control_plane.freqtrade import FreqtradeWorkerClient
from trading_control_plane.hyperliquid_execution import HyperliquidLiveClient
from trading_control_plane.models import (
    AnalyticsEquitySnapshot,
    Team,
    TeamShadowAccount,
    VenueFill,
)
from trading_control_plane.quantstats_adapter import QuantStatsReportAdapter
from trading_control_plane.queries import TradingQueries

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
    assert live.transactions[0].idempotency_key == (
        "LIVE:paper-1:BINANCE:live-partial-fill-1"
    )
    assert live.coverage["transaction_count"] == 1
    assert all(point.currency == "USD" for point in live.nav_series)
    assert "TEAM_SHADOW_ACCOUNT" not in live.metadata["source_facts"]
    with database.session_factory() as session:
        team = session.get(Team, ids["team"])
        assert team is not None and team.execution_mode == "SHADOW"


def test_quantstats_api_is_scoped_offline_and_preserves_legacy_results(
    database: Database,
) -> None:
    _service, ids = activate_shadow(database)
    seed_shadow_nav(
        database,
        team_id=ids["team"],
        generation=1,
        start=START,
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
            ):
                report = await client.get(
                    "/api/results/quantstats",
                    params={
                        "environment": "SHADOW",
                        "generation": 1,
                        "from_time": START.isoformat(),
                        "to_time": (START + timedelta(days=31)).isoformat(),
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

            legacy = await client.get("/api/results?environment=SHADOW")
            assert legacy.status_code == 200
            assert legacy.json()["data"]["environment"] == "SHADOW"
            denied = await client.get(
                "/api/results/quantstats",
                params={
                    "environment": "SHADOW",
                    "account_id": "other-team-account",
                    "generation": 1,
                    "from_time": START.isoformat(),
                    "to_time": (START + timedelta(days=31)).isoformat(),
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
        start=START,
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
                side_effect=DomainRejected(
                    "QUANTSTATS_REPORT_FAILED", "isolated report failure"
                ),
            ):
                failed = await client.get(
                    "/api/results/quantstats",
                    params={
                        "environment": "SHADOW",
                        "generation": 1,
                        "from_time": START.isoformat(),
                        "to_time": (START + timedelta(days=2)).isoformat(),
                    },
                )
            assert failed.status_code == 422
            assert failed.json()["error"]["code"] == "QUANTSTATS_REPORT_FAILED"
            assert (await client.get("/api/trading-mode")).status_code == 200
            assert (await client.get("/api/results?environment=SHADOW")).status_code == 200

    asyncio.run(scenario())
