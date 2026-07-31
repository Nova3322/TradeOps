from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
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
from trading_control_plane.domain import Role, SystemRiskState
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
from trading_control_plane.perptape_stream import PerptapeSocket, PerptapeStreamWorker
from trading_control_plane.queries import TradingQueries
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
    service.set_risk_policy(
        actor_id=admin,
        version="runtime-test-v1",
        system_state=SystemRiskState.NORMAL,
        max_total_risk=Decimal(1_000),
        max_fact_age=timedelta(minutes=5),
        now=NOW,
    )
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
        base_url="https://perptape.com",
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

    assert {source: result.status for source, result in first.sources.items()} == {
        "PERPTAPE": "SUCCESS",
        "BINANCE": "SUCCESS",
        "HYPERLIQUID": "SUCCESS",
        "NOTILT:42161": "SUCCESS",
    }
    assert {source: result.status for source, result in duplicate.sources.items()} == {
        "PERPTAPE": "SUCCESS",
        "BINANCE": "SUCCESS",
        "HYPERLIQUID": "SUCCESS",
        "NOTILT:42161": "SUCCESS",
    }
    assert first.successful is duplicate.successful is True
    assert replayed_version == 1
    assert first.ready_for_new_risk is duplicate.ready_for_new_risk is True
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
            base_url="https://perptape.com",
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


def test_websocket_alert_updates_the_existing_authoritative_perptape_feed(
    database: Database,
) -> None:
    service = TradingService(database)
    queries = TradingQueries(database)
    admin = service.bootstrap_admin("stream-admin", now=NOW)
    perptape_actor = service.create_service_principal("perptape", admin, now=NOW)
    service.assign_role(perptape_actor, Role.PROPOSER, admin, now=NOW)
    https_calls: list[str] = []

    def fetch(url: str, _headers: dict[str, str], _timeout: float) -> dict[str, Any]:
        https_calls.append(url)
        return {
            "type": "breakouts",
            "generatedAt": int(NOW.timestamp() * 1_000),
            "data": [],
        }

    client = PerptapeClient(
        base_url="https://perptape.com",
        api_key="integration-stream-key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
        fetcher=fetch,
    )
    event_time = NOW + timedelta(seconds=1)
    alert = json.dumps(
        {
            "e": "alert",
            "seq": 2,
            "E": int(event_time.timestamp() * 1_000),
            "d": {
                "id": "integration-alert-1",
                "ex": "BN",
                "s": "ETHUSDT",
                "cs": "ETHUSDT",
                "dir": "HH",
                "p": 4_000,
                "th": 3_900,
                "tf": "1h",
                "t": int(event_time.timestamp() * 1_000),
                "u": int(event_time.timestamp() * 1_000),
                "kr": {"status": "ready"},
                "vq24": 2_000_000,
                "oi": 1_000_000,
            },
        }
    )
    stop = threading.Event()

    class Socket:
        def __init__(self) -> None:
            self.messages = deque(
                [
                    json.dumps(
                        {
                            "e": "hello",
                            "seq": 1,
                            "E": int(NOW.timestamp() * 1_000),
                        }
                    ),
                    alert,
                ]
            )

        def send(self, _message: str) -> None:
            return None

        def recv(self, timeout: float | None = None) -> str | bytes:
            assert timeout == 1.0
            if self.messages:
                return self.messages.popleft()
            stop.set()
            raise TimeoutError

    @contextmanager
    def connector(
        url: str,
        headers: dict[str, str],
        timeout: float,
    ) -> Iterator[PerptapeSocket]:
        assert url == "wss://perptape.com/ws/v1/alerts"
        assert headers["x-api-key"] == "integration-stream-key"
        assert timeout == 5
        yield Socket()

    stream = PerptapeStreamWorker(
        client=client,
        websocket_url="wss://perptape.com/ws/v1/alerts",
        api_key="integration-stream-key",
        contract_version="breakouts-v1",
        load_snapshot=queries.perptape_feed,
        record_snapshot=lambda feed, now: service.record_perptape_feed(
            perptape_actor,
            feed,
            now=now,
        ),
        timeout_seconds=5,
        heartbeat_timeout_seconds=45,
        reconciliation_interval_seconds=300,
        reconnect_initial_seconds=1,
        reconnect_max_seconds=8,
        max_reconnect_attempts=3,
        connector=connector,
        clock=lambda: NOW + timedelta(seconds=2),
    )

    stream.run_forever(stop)

    persisted = queries.perptape_feed()
    assert persisted is not None
    assert len(persisted.candidates) == 1
    assert persisted.candidates[0].symbol == "ETHUSDT"
    assert persisted.candidates[0].readiness == "READY"
    assert stream.stats.alerts_applied == 1
    assert len(https_calls) == 1
