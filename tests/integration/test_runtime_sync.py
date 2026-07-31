from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from httpx import ASGITransport, AsyncClient

from trading_control_plane.api import create_app
from trading_control_plane.binance import (
    BinanceEquity,
    BinanceInstrument,
    BinancePosition,
    BinanceReadOnlySnapshot,
)
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import Role
from trading_control_plane.hyperliquid import (
    HyperliquidEquity,
    HyperliquidInstrument,
    HyperliquidPosition,
    HyperliquidReadOnlySnapshot,
)
from trading_control_plane.notilt import (
    NoTiltAssetBudget,
    NoTiltUsdValuator,
    NoTiltVaultSnapshot,
)
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.runtime import RuntimeSyncWorker
from trading_control_plane.service import TradingService
from trading_control_plane.telegram import MockTelegramGateway

NOW = datetime.now(UTC)
AGENT = "0x2222222222222222222222222222222222222222"
VAULT = "0x1111111111111111111111111111111111111111"
OWNER = "0x3333333333333333333333333333333333333333"


class BinanceReader:
    configured = True

    def read_snapshot(self, symbol: str, *, now: datetime) -> BinanceReadOnlySnapshot:
        assert symbol == "BTCUSDT"
        return BinanceReadOnlySnapshot(
            symbol=symbol,
            observed_at=now,
            instrument=BinanceInstrument(
                symbol=symbol,
                tick_size=Decimal("0.1"),
                lot_size=Decimal("0.001"),
                minimum_notional=Decimal("5"),
                quote_currency="USDT",
                collateral_currency="USDT",
                active=True,
            ),
            orders=(),
            fills=(),
            position=BinancePosition(Decimal(0), Decimal(0), Decimal("100000"), now),
            equity=BinanceEquity(Decimal(10), Decimal(10), "USDT", now),
            funding=(),
            protection=None,
        )


class HyperliquidReader:
    configured = True
    fact_environment = "LIVE"

    def read_snapshot(self, symbol: str, *, now: datetime) -> HyperliquidReadOnlySnapshot:
        assert symbol == "BTC"
        return HyperliquidReadOnlySnapshot(
            symbol=symbol,
            observed_at=now,
            instrument=HyperliquidInstrument(
                symbol=symbol,
                tick_size=Decimal("1"),
                lot_size=Decimal("0.00001"),
                minimum_notional=Decimal("10"),
                quote_currency="USD",
                collateral_currency="USDC",
                active=True,
            ),
            orders=(),
            fills=(),
            position=HyperliquidPosition(Decimal(0), Decimal(0), Decimal("100000"), now),
            equity=HyperliquidEquity(Decimal(20), Decimal(20), "USDC", now),
            funding=(),
            protection=None,
        )


class NoTiltReader:
    available = True

    def read_vault(self, chain_id: int, vault: str, agent: str) -> NoTiltVaultSnapshot:
        assert (chain_id, vault, agent) == (42161, VAULT, AGENT)
        return NoTiltVaultSnapshot(
            chain_id=42161,
            chain="ARBITRUM",
            vault=VAULT,
            agent=AGENT,
            budgets=(
                NoTiltAssetBudget(
                    chain_id=42161,
                    chain="ARBITRUM",
                    block_number=123,
                    block_timestamp=NOW,
                    vault=VAULT,
                    agent=AGENT,
                    owner=OWNER,
                    asset="USDC",
                    asset_address="0xaf88d065e77c8cc2239327c5edb3a432268e5831",
                    decimals=6,
                    native=False,
                    is_official_vault=True,
                    is_active_whitelist=True,
                    assigned_whitelist_vault=VAULT,
                    balance=Decimal(30),
                    max_release_net=Decimal(5),
                    pending_net=Decimal(0),
                    panic_locked=False,
                    daily_release_rate=Decimal("0.1"),
                    daily_fee_rate=Decimal("0.05"),
                ),
            ),
        )


def perptape_payload() -> dict[str, Any]:
    timestamp = int(NOW.timestamp() * 1_000)
    return {
        "type": "breakouts",
        "generatedAt": timestamp,
        "data": [
            {
                "exchange": "BN",
                "symbol": "BTCUSDT",
                "canonicalSymbol": "BTCUSDT",
                "direction": "HH",
                "timeframe": "1h",
                "price": 100_000,
                "threshold": 99_000,
                "updatedAt": timestamp,
                "triggeredAt": timestamp,
                "klineReadiness": {"status": "ready"},
            }
        ],
    }


def test_runtime_worker_refreshes_perptape_two_venues_and_vault_without_sending(
    database: Database,
) -> None:
    service = TradingService(database)
    admin = service.bootstrap_admin("runtime-admin", now=NOW)
    actor = service.create_service_principal("runtime-sync", admin, now=NOW)
    perptape_actor = service.create_service_principal("perptape", admin, now=NOW)
    service.assign_role(actor, Role.OPERATOR, admin, now=NOW)
    service.assign_role(actor, Role.TREASURY_ADMIN, admin, now=NOW)
    service.assign_role(perptape_actor, Role.PROPOSER, admin, now=NOW)
    settings = Settings(
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        perptape_api_key="runtime-test-key",
        runtime_sync_enabled=True,
        runtime_binance_account_id="binance-main",
        runtime_hyperliquid_account_id="hyperliquid-main",
        binance_read_only_enabled=True,
        binance_api_key="runtime-test-key",
        binance_api_secret="runtime-test-secret",  # noqa: S106
        hyperliquid_read_only_enabled=True,
        hyperliquid_account_address=AGENT,
        notilt_enabled=True,
        notilt_agent_address=AGENT,
        notilt_arbitrum_vault_address=VAULT,
    )
    perptape_calls: list[str] = []

    def fetch_perptape(url: str, _headers: dict[str, str], _timeout: float) -> dict[str, Any]:
        perptape_calls.append(url)
        return perptape_payload()

    perptape = PerptapeClient(
        base_url="https://perptape.invalid",
        api_key="runtime-test-key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(seconds=settings.runtime_sync_interval_seconds),
        fetcher=fetch_perptape,
    )
    worker = RuntimeSyncWorker(
        settings=settings,
        database=database,
        perptape=perptape,
        binance=BinanceReader(),  # type: ignore[arg-type]
        hyperliquid=HyperliquidReader(),  # type: ignore[arg-type]
        notilt=NoTiltReader(),  # type: ignore[arg-type]
        notilt_valuator=NoTiltUsdValuator(),
        clock=lambda: NOW,
    )

    first = worker.run_once()
    persisted_feed = perptape.refresh(now=NOW)
    replayed_version = service.record_perptape_feed(
        perptape_actor,
        persisted_feed,
        now=NOW,
    )
    duplicate = worker.run_once()

    assert first.successful is duplicate.successful is True
    assert replayed_version == 1
    assert first.ready_for_new_risk is duplicate.ready_for_new_risk is True
    assert {source: result.status for source, result in first.sources.items()} == {
        "PERPTAPE": "SUCCESS",
        "BINANCE": "SUCCESS",
        "HYPERLIQUID": "SUCCESS",
        "NOTILT:42161": "SUCCESS",
    }
    assert first.sources["PERPTAPE"].items_observed == 1
    assert len(perptape_calls) == 1
    assert first.net_worth["venues"] == {
        "BINANCE": "10.000000000000000000",
        "HYPERLIQUID": "20.000000000000000000",
    }
    assert first.net_worth["vault"] == "30.000000000000000000"
    assert first.net_worth["total"] == "60.000000000000000000"
    assert first.net_worth["complete"] is True
    assert first.net_worth["issues"] == []

    async def cached_api_scenario() -> None:
        def must_not_fetch(_url: str, _headers: dict[str, str], _timeout: float) -> dict[str, Any]:
            raise AssertionError("runtime-enabled API must use the shared PostgreSQL feed")

        failing_client = PerptapeClient(
            base_url="https://perptape.invalid",
            api_key="runtime-test-key",
            contract_version="breakouts-v1",
            cache_ttl=timedelta(minutes=1),
            fetcher=must_not_fetch,
        )
        api_settings = settings.model_copy(
            update={
                "environment": "test",
                "allow_mock_identity": True,
                "session_signing_secret": "runtime-cache-test-signing-secret",
            }
        )
        app = create_app(
            api_settings,
            database,
            failing_client,
            MockTelegramGateway(),
            binance_client=BinanceReader(),  # type: ignore[arg-type]
            hyperliquid_client=HyperliquidReader(),  # type: ignore[arg-type]
            notilt_gateway=NoTiltReader(),  # type: ignore[arg-type]
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            login = await client.post(
                "/api/auth/mock/login",
                json={"username": "runtime-admin"},
            )
            assert login.status_code == 200
            opportunities = await client.get("/api/opportunities")
            assert opportunities.status_code == 200, opportunities.text
            assert len(opportunities.json()["data"]) == 1

    asyncio.run(cached_api_scenario())
