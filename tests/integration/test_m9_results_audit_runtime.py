from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from test_m7_capital_center import build_app, login, seed
from test_m8_capital_automation import close_profitable_testnet_campaign

from trading_control_plane.database import Database
from trading_control_plane.domain import DomainRejected
from trading_control_plane.models import Campaign, FundingPayment, OrderIntent, VenueFill
from trading_control_plane.queries import TradingQueries
from trading_control_plane.service import TradingService
from trading_control_plane.telegram import MockTelegramGateway


def add_recorded_cost_facts(database: Database, ids: dict[str, object], now: datetime) -> None:
    with database.session_factory.begin() as session:
        campaign = session.scalar(select(Campaign))
        assert campaign is not None
        intent = session.scalar(
            select(OrderIntent).where(OrderIntent.campaign_id == campaign.campaign_id)
        )
        assert intent is not None
        session.add_all(
            [
                VenueFill(
                    venue="BINANCE",
                    venue_fill_id="m9-fill-1",
                    order_intent_id=intent.intent_id,
                    campaign_id=campaign.campaign_id,
                    account_id="acct-1",
                    environment="TESTNET",
                    instrument_id=ids["instrument"],
                    side="BUY",
                    quantity=Decimal("0.5"),
                    price=Decimal("100"),
                    fee=Decimal("1"),
                    fee_currency="USDT",
                    slippage_cost=Decimal("1.5"),
                    executed_at=now,
                ),
                VenueFill(
                    venue="BINANCE",
                    venue_fill_id="m9-fill-2",
                    order_intent_id=intent.intent_id,
                    campaign_id=campaign.campaign_id,
                    account_id="acct-1",
                    environment="TESTNET",
                    instrument_id=ids["instrument"],
                    side="SELL",
                    quantity=Decimal("0.5"),
                    price=Decimal("110"),
                    fee=Decimal("2"),
                    fee_currency="USDT",
                    slippage_cost=Decimal("2.5"),
                    executed_at=now,
                ),
                FundingPayment(
                    campaign_id=campaign.campaign_id,
                    account_id="acct-1",
                    venue="BINANCE",
                    environment="TESTNET",
                    instrument_id=ids["instrument"],
                    venue_payment_id="m9-funding-1",
                    amount=Decimal("-2"),
                    currency="USDT",
                    paid_at=now,
                ),
            ]
        )


def test_results_are_environment_separated_and_derive_costs_curve_and_audit(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    now = datetime.now(UTC)
    close_profitable_testnet_campaign(database, service, ids, now=now)
    add_recorded_cost_facts(database, ids, now)
    queries = TradingQueries(database)

    results = queries.actual_results(ids["operator"], "TESTNET")
    assert results["environment"] == "TESTNET"
    assert len(results["campaigns"]) == 1
    campaign = results["campaigns"][0]
    assert campaign["actuality"] == "NON_PRODUCTION_RECORDED_FACTS"
    assert campaign["source"] == "MANUAL"
    assert campaign["source_type"] == "MANUAL"
    assert campaign["fill_count"] == 2
    assert campaign["fees"] == "3.000000000000000000"
    assert campaign["funding"] == "-2.000000000000000000"
    assert campaign["slippage"] == "4.000000000000000000"
    assert results["totals_by_currency"]["USDT"]["final_pnl"] == ("180.000000000000000000")
    curve = results["curves_by_currency"]["USDT"]
    assert curve["percentage_available"] is False
    assert curve["points"][0]["cumulative_pnl"] == "180.000000000000000000"
    assert queries.actual_results(ids["operator"], "SHADOW")["campaigns"] == []
    assert (
        len(
            queries.actual_results(
                ids["operator"],
                "TESTNET",
                source="MANUAL",
                source_type="MANUAL",
                venue="BINANCE",
                account_id="acct-1",
                instrument_id=ids["instrument"],
                direction="LONG",
                risk_tier="LOW",
                campaign_id=UUID(campaign["campaign_id"]),
                from_time=now.replace(year=now.year - 1),
                to_time=now.replace(year=now.year + 1),
            )["campaigns"]
        )
        == 1
    )
    assert queries.actual_results(ids["operator"], "TESTNET", source="SYSTEM")["campaigns"] == []
    for filters in (
        {"source_type": "PERPTAPE_BREAKOUT"},
        {"source_candidate_id": "unknown-candidate"},
        {"source_version": "unknown-version"},
        {"risk_tier": "HIGH"},
    ):
        assert queries.actual_results(ids["operator"], "TESTNET", **filters)["campaigns"] == []
    with pytest.raises(DomainRejected, match="ENVIRONMENT_INVALID"):
        queries.actual_results(ids["operator"], "ALL")
    with pytest.raises(DomainRejected, match="TIME_RANGE_INVALID"):
        queries.actual_results(
            ids["operator"],
            "TESTNET",
            from_time=now.replace(year=now.year + 1),
            to_time=now,
        )

    timeline = queries.audit_timeline(ids["operator"], "TESTNET")
    assert timeline
    assert any(item["event_type"] == "ORDER_INTENT_PREPARED" for item in timeline)
    assert all(item["correlation_id"] for item in timeline)
    assert any(item["actor"] == "operator" for item in timeline)

    runtime = queries.runtime_snapshot(ids["operator"])
    assert runtime["database_ready"] is True
    assert runtime["schema_revision"] == "20260805_0012"
    assert runtime["business_table_count"] == 33
    assert set(runtime["capability_gates"]) == {
        "LIVE_ORDER_SEND",
        "CAPITAL_TRANSFER",
        "AUTO_ADD",
        "AUTO_PROFIT_SWEEP",
        "AUTO_OPERATING_REFILL",
    }
    assert all(item["status"] == "DISABLED" for item in runtime["capability_gates"].values())
    assert runtime["perptape_feed"] == {
        "available": False,
        "contract_version": None,
        "candidate_count": 0,
        "generated_at": None,
        "fetched_at": None,
        "updated_at": None,
    }


def test_results_audit_and_runtime_api_do_not_mix_environments_or_expose_secrets(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    now = datetime.now(UTC)
    close_profitable_testnet_campaign(database, service, ids, now=now)
    add_recorded_cost_facts(database, ids, now)
    telegram = MockTelegramGateway()

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=build_app(database, telegram)),
            base_url="http://test",
        ) as client:
            await login(client, "operator")
            testnet = await client.get("/api/results?environment=TESTNET")
            shadow = await client.get("/api/results?environment=SHADOW")
            invalid = await client.get("/api/results?environment=ALL")
            assert testnet.status_code == 200 and len(testnet.json()["data"]["campaigns"]) == 1
            assert shadow.status_code == 200 and shadow.json()["data"]["campaigns"] == []
            assert invalid.status_code == 422
            filtered = await client.get(
                "/api/results",
                params={
                    "environment": "TESTNET",
                    "source": "MANUAL",
                    "source_type": "MANUAL",
                    "venue": "BINANCE",
                    "account_id": "acct-1",
                    "instrument_id": str(ids["instrument"]),
                    "direction": "LONG",
                    "risk_tier": "LOW",
                    "campaign_id": testnet.json()["data"]["campaigns"][0]["campaign_id"],
                    "from_time": now.replace(year=now.year - 1).isoformat(),
                    "to_time": now.replace(year=now.year + 1).isoformat(),
                },
            )
            assert filtered.status_code == 200
            assert len(filtered.json()["data"]["campaigns"]) == 1
            assert filtered.json()["data"]["filters"]["source"] == "MANUAL"
            invalid_range = await client.get(
                "/api/results",
                params={
                    "environment": "TESTNET",
                    "from_time": now.replace(year=now.year + 1).isoformat(),
                    "to_time": now.isoformat(),
                },
            )
            assert invalid_range.status_code == 422
            assert invalid_range.json()["error"]["code"] == "TIME_RANGE_INVALID"
            audit = await client.get("/api/audit?environment=TESTNET")
            assert audit.status_code == 200 and audit.json()["data"]
            runtime = await client.get("/api/runtime/status")
            assert runtime.status_code == 200
            payload = runtime.json()["data"]
            assert payload["perptape_feed"] == {
                "available": False,
                "contract_version": None,
                "candidate_count": 0,
                "generated_at": None,
                "fetched_at": None,
                "updated_at": None,
            }
            assert payload["external_boundaries"]["perptape"] == {
                "configured": False,
                "mode": "READ_ONLY",
                "status": "NOT_CONFIGURED",
                "contract_version": "breakouts-v1",
                "feed_available": False,
                "candidate_count": 0,
                "last_fetched_at": None,
                "last_generated_at": None,
            }
            assert payload["external_boundaries"]["capital_transfer"] == {
                "mode": "MOCK_OR_NOTILT_UNSIGNED_HANDOFF",
                "real_configured": False,
            }
            assert payload["external_boundaries"]["telegram"]["polling"] == {
                "state": "DISABLED",
                "running": False,
                "last_success_at": None,
                "last_error_at": None,
                "last_error_code": None,
                "consecutive_failures": 0,
            }
            assert (
                payload["external_boundaries"]["hyperliquid_read_only"]["account_scope"]
                == "MAIN_ACCOUNT"
            )
            assert (
                payload["external_boundaries"]["hyperliquid_testnet_send"]["account_scope"]
                == "MAIN_ACCOUNT"
            )
            serialized = runtime.text.lower()
            assert "api_secret" not in serialized
            assert "private_key" not in serialized
            web = await client.get("/results")
            assert web.status_code == 200
            assert "<title>交易控制台</title>" in web.text

    asyncio.run(scenario())
