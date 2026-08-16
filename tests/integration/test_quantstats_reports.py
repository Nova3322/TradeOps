from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID

import pytest
from conftest import add_exchange_account_fixture, set_test_team_environment
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from test_m7_capital_center import build_app, login, seed

from trading_control_plane.binance_execution import BinancePortfolioMarginClient
from trading_control_plane.capital import MockCapitalTransferAdapter
from trading_control_plane.database import Database
from trading_control_plane.domain import DomainRejected, ExecutionEnvironment, Role
from trading_control_plane.freqtrade import FreqtradeWorkerClient
from trading_control_plane.hyperliquid_execution import HyperliquidLiveClient
from trading_control_plane.models import (
    AccountEquityObservation,
    AnalyticsReport,
    AuditEvent,
    Team,
    User,
    VenueFill,
)
from trading_control_plane.queries import TradingQueries
from trading_control_plane.report_engines import ReportArtifact
from trading_control_plane.service import TradingService
from trading_control_plane.telegram import MockTelegramGateway


def seed_nav(
    service: TradingService,
    ids: dict[str, UUID],
    *,
    environment: ExecutionEnvironment,
    account_id: str,
    start: datetime,
    days: int = 35,
) -> None:
    set_test_team_environment(service.database, ids["admin"], environment.value)
    for offset in range(days + 1):
        equity = Decimal("50000") + Decimal(offset * 125)
        if offset and offset % 7 == 0:
            equity -= Decimal("350")
        observed_at = start + timedelta(days=offset)
        service.record_account_equity(
            account_id=account_id,
            venue="BINANCE",
            equity=equity,
            available_balance=equity,
            currency="USDT",
            known=True,
            actor_id=ids["admin"],
            environment=environment,
            observed_at=observed_at,
            now=start + timedelta(days=days, hours=1),
        )


def seed_testnet_analytics(
    database: Database,
    service: TradingService,
) -> tuple[dict[str, UUID], datetime, datetime]:
    ids = seed(service)
    start = datetime.now(UTC) - timedelta(days=40)
    end = start + timedelta(days=35)
    # The capital-center seed records a current point. Remove only that point so
    # this fixture can replay an ascending, exchange-sourced historical series.
    with database.session_factory.begin() as session:
        session.execute(
            delete(AccountEquityObservation).where(
                AccountEquityObservation.environment == "TESTNET",
                AccountEquityObservation.account_id == "acct-1",
                AccountEquityObservation.venue == "BINANCE",
            )
        )
    seed_nav(
        service,
        ids,
        environment=ExecutionEnvironment.TESTNET,
        account_id="acct-1",
        start=start,
    )
    with database.session_factory.begin() as session:
        team = session.scalar(select(Team).where(Team.created_by == ids["admin"]))
        assert team is not None
        session.add(
            VenueFill(
                team_id=team.team_id,
                venue="BINANCE",
                venue_fill_id="testnet-report-fill-1",
                order_intent_id=None,
                campaign_id=None,
                account_id="acct-1",
                environment="TESTNET",
                instrument_id=ids["instrument"],
                side="BUY",
                quantity=Decimal("0.25"),
                price=Decimal("25000"),
                fee=Decimal("2.5"),
                fee_currency="USDT",
                slippage_cost=Decimal("1"),
                executed_at=start + timedelta(days=2),
            )
        )
    return ids, start, end


def test_testnet_and_live_analytics_use_exchange_facts_without_cross_environment_mixing(
    database: Database,
    service: TradingService,
) -> None:
    ids, start, end = seed_testnet_analytics(database, service)
    add_exchange_account_fixture(
        database,
        ids["admin"],
        "acct-1",
        "BINANCE",
        environment="LIVE",
    )
    seed_nav(
        service,
        ids,
        environment=ExecutionEnvironment.LIVE,
        account_id="acct-1",
        start=start,
        days=3,
    )
    set_test_team_environment(database, ids["admin"], "TESTNET")

    queries = TradingQueries(database)
    testnet = queries.analytics_dataset(
        ids["admin"],
        "TESTNET",
        account_id="acct-1",
        venue="BINANCE",
        generation=None,
        from_time=start,
        to_time=end,
    )
    live = queries.analytics_dataset(
        ids["admin"],
        "LIVE",
        account_id="acct-1",
        venue="BINANCE",
        generation=None,
        from_time=start,
        to_time=start + timedelta(days=3),
    )

    assert testnet.scope.environment == "TESTNET"
    assert live.scope.environment == "LIVE"
    assert testnet.scope.account_venues == (("acct-1", "BINANCE"),)
    assert live.scope.account_venues == (("acct-1", "BINANCE"),)
    assert len(testnet.nav_series) == 36
    assert len(live.nav_series) == 4
    assert len(testnet.transactions) == 1
    assert live.transactions == ()
    assert testnet.transactions[0].idempotency_key == (
        "TESTNET:acct-1:BINANCE:testnet-report-fill-1"
    )
    assert all(point.currency == "USD" for point in testnet.nav_series)
    assert all("SHADOW" not in item for item in testnet.metadata["source_facts"])
    assert all("SHADOW" not in item for item in live.metadata["source_facts"])
    with pytest.raises(DomainRejected, match="ANALYTICS_GENERATION_FORBIDDEN"):
        queries.analytics_dataset(
            ids["admin"],
            "TESTNET",
            account_id="acct-1",
            venue="BINANCE",
            generation=1,
            from_time=start,
            to_time=end,
        )


def test_report_access_requires_exact_account_and_venue_scope(
    database: Database,
    service: TradingService,
) -> None:
    ids, start, end = seed_testnet_analytics(database, service)
    dataset = TradingQueries(database).analytics_dataset(
        ids["admin"],
        "TESTNET",
        account_id="acct-1",
        venue="BINANCE",
        generation=None,
        from_time=start,
        to_time=end,
    )
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
                "RETURNS_READY": True,
                "POSITIONS_READY": False,
                "TRANSACTIONS_READY": True,
                "BENCHMARK_READY": False,
            },
        ),
        "exact-testnet-report-scope",
        now=end + timedelta(days=1),
    )
    restricted = service.create_managed_user(
        "wrong-venue-report-reader",
        [Role.OBSERVER],
        ids["admin"],
        "acct-1",
        "BYBIT",
        "ordinary-user-password",
        now=end + timedelta(days=1),
    )
    with pytest.raises(DomainRejected, match="ANALYTICS_ACCOUNT_SCOPE_DENIED"):
        TradingQueries(database).analytics_report(restricted, UUID(persisted["report_id"]))


def test_testnet_report_apis_are_offline_idempotent_and_persist_real_artifacts(
    database: Database,
    service: TradingService,
) -> None:
    _ids, start, end = seed_testnet_analytics(database, service)
    telegram = MockTelegramGateway()

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=build_app(database, telegram)),
            base_url="http://test",
        ) as client:
            await login(client, "admin")
            options = await client.get("/api/results/quantstats/options")
            assert options.status_code == 200
            assert options.json()["data"]["current_trading_mode"] == "TESTNET"
            assert options.json()["data"]["environments"] == ["TESTNET", "LIVE"]
            assert {
                (item["environment"], item["account_id"], item["venue"])
                for item in options.json()["data"]["accounts"]
            } >= {("TESTNET", "acct-1", "BINANCE")}

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
                    "environment": "TESTNET",
                    "account_id": "acct-1",
                    "venue": "BINANCE",
                    "from_time": start.isoformat(),
                    "to_time": end.isoformat(),
                    "idempotency_key": f"testnet-report-{engine.lower()}",
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
                assert data["environment"] == "TESTNET"
                assert data["account_scopes"] == [{"account_id": "acct-1", "venue": "BINANCE"}]
                assert data["chart_count"] > 0
                report_id = data["report_id"]
                report_ids.append(report_id)
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
                assert capital_write.call_count == 0

            quantstats = await client.get(
                "/api/results/quantstats",
                params={
                    "environment": "TESTNET",
                    "account_id": "acct-1",
                    "venue": "BINANCE",
                    "from_time": start.isoformat(),
                    "to_time": end.isoformat(),
                },
            )
            assert quantstats.status_code == 200, quantstats.text
            quantstats_data = quantstats.json()["data"]
            assert quantstats_data["environment"] == "TESTNET"
            assert quantstats_data["generation"] is None
            assert quantstats_data["data_status"] == "READY"
            assert quantstats_data["quantstats"]["external_market_downloads"] is False
            assert quantstats_data["quantstats"]["exchange_write_adapter_calls"] == 0

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


def test_report_artifact_is_denied_after_active_team_changes(
    database: Database,
    service: TradingService,
) -> None:
    ids, start, end = seed_testnet_analytics(database, service)
    dataset = TradingQueries(database).analytics_dataset(
        ids["admin"],
        "TESTNET",
        account_id="acct-1",
        venue="BINANCE",
        generation=None,
        from_time=start,
        to_time=end,
    )
    from trading_control_plane.report_engines import render_report

    persisted = service.persist_analytics_report(
        ids["admin"],
        dataset,
        render_report("PYFOLIO", dataset),
        "team-scoped-testnet-report",
        now=end + timedelta(days=1),
    )
    other_team = service.create_team(
        actor_id=ids["admin"],
        name="Other Analytics Team",
        slug="other-analytics-team",
        idempotency_key="create-other-analytics-team",
        now=end + timedelta(days=2),
    )
    with database.session_factory.begin() as session:
        user = session.get(User, ids["admin"])
        assert user is not None
        user.active_team_id = other_team
    with pytest.raises(DomainRejected, match="ANALYTICS_REPORT_NOT_FOUND"):
        TradingQueries(database).analytics_report(ids["admin"], UUID(persisted["report_id"]))
