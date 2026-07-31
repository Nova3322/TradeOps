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

import pytest
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
from trading_control_plane.perptape import (
    PerptapeCandidate,
    PerptapeClient,
    PerptapeFeedSnapshot,
    perptape_snapshot_identity,
)
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


def perptape_candidate(
    client: PerptapeClient,
    *,
    symbol: str,
    triggered_at: datetime,
    observed_at: datetime,
    price: int = 100_000,
) -> PerptapeCandidate:
    return client.parse_stream_alert(
        {
            "id": f"{symbol}-{observed_at.timestamp()}",
            "ex": "BN",
            "s": symbol,
            "cs": symbol,
            "dir": "HH",
            "p": price,
            "th": price - 1,
            "tf": "1h",
            "t": int(triggered_at.timestamp() * 1_000),
            "u": int(observed_at.timestamp() * 1_000),
            "kr": {"status": "ready"},
            "vq24": 20_000,
            "oi": 10_000,
        },
        event_time=observed_at,
    )


def perptape_feed(
    *candidates: PerptapeCandidate,
    fetched_at: datetime,
) -> PerptapeFeedSnapshot:
    return PerptapeFeedSnapshot(
        contract_version="breakouts-v1",
        generated_at=fetched_at,
        fetched_at=fetched_at,
        next_allowed_at=fetched_at,
        candidates=tuple(candidates),
    )


def perptape_test_client() -> PerptapeClient:
    return PerptapeClient(
        base_url="https://perptape.com",
        api_key="integration-stream-key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(0),
        fetcher=lambda _url, _headers, _timeout: perptape_payload(),
    )


@pytest.mark.parametrize(
    ("first_source", "first_offset", "second_offset"),
    [
        ("HTTP", 0, 0),
        ("WSS", 0, 0),
        ("WSS", 2, 1),
    ],
)
def test_concurrent_perptape_writers_merge_stale_snapshot_identities(
    database: Database,
    first_source: str,
    first_offset: int,
    second_offset: int,
) -> None:
    service = TradingService(database)
    queries = TradingQueries(database)
    admin = service.bootstrap_admin(
        f"concurrent-{first_source}-{first_offset}-{second_offset}",
        now=NOW,
    )
    actor = service.create_service_principal(
        f"perptape-{first_source}-{first_offset}-{second_offset}",
        admin,
        now=NOW,
    )
    service.assign_role(actor, Role.PROPOSER, admin, now=NOW)
    client = perptape_test_client()
    base = perptape_feed(
        perptape_candidate(
            client,
            symbol="BASEUSDT",
            triggered_at=NOW,
            observed_at=NOW,
        ),
        fetched_at=NOW,
    )
    service.record_perptape_feed(
        actor,
        base,
        now=NOW,
        expected_snapshot_identity=None,
    )
    expected_identity = perptape_snapshot_identity(base)
    http = perptape_feed(
        perptape_candidate(
            client,
            symbol="HTTPUSDT",
            triggered_at=NOW + timedelta(seconds=1),
            observed_at=NOW + timedelta(seconds=1),
        ),
        fetched_at=NOW
        + timedelta(seconds=(first_offset if first_source == "HTTP" else second_offset)),
    )
    wss = perptape_feed(
        base.candidates[0],
        perptape_candidate(
            client,
            symbol="WSSUSDT",
            triggered_at=NOW + timedelta(seconds=2),
            observed_at=NOW + timedelta(seconds=2),
        ),
        fetched_at=NOW
        + timedelta(seconds=(first_offset if first_source == "WSS" else second_offset)),
    )
    first = http if first_source == "HTTP" else wss
    second = wss if first_source == "HTTP" else http
    barrier = threading.Barrier(2)
    first_done = threading.Event()
    errors: list[BaseException] = []

    def write(feed: PerptapeFeedSnapshot, *, wait_for_first: bool) -> None:
        try:
            barrier.wait(timeout=2)
            if wait_for_first:
                assert first_done.wait(timeout=2)
            TradingService(database).record_perptape_feed(
                actor,
                feed,
                now=NOW + timedelta(seconds=10),
                expected_snapshot_identity=expected_identity,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            if not wait_for_first:
                first_done.set()

    threads = [
        threading.Thread(target=write, args=(first,), kwargs={"wait_for_first": False}),
        threading.Thread(target=write, args=(second,), kwargs={"wait_for_first": True}),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    persisted = queries.perptape_feed()
    assert persisted is not None
    assert [candidate.symbol for candidate in persisted.candidates] == [
        "BASEUSDT",
        "HTTPUSDT",
        "WSSUSDT",
    ]
    assert persisted.fetched_at > max(http.fetched_at, wss.fetched_at)


@pytest.mark.parametrize("fresh_first", [True, False])
def test_concurrent_same_key_never_allows_older_fact_to_win(
    database: Database,
    fresh_first: bool,
) -> None:
    service = TradingService(database)
    queries = TradingQueries(database)
    admin = service.bootstrap_admin(f"same-key-{fresh_first}", now=NOW)
    actor = service.create_service_principal(
        f"perptape-same-key-{fresh_first}",
        admin,
        now=NOW,
    )
    service.assign_role(actor, Role.PROPOSER, admin, now=NOW)
    client = perptape_test_client()
    base = perptape_feed(
        perptape_candidate(
            client,
            symbol="SAMEUSDT",
            triggered_at=NOW,
            observed_at=NOW,
            price=100,
        ),
        fetched_at=NOW,
    )
    service.record_perptape_feed(
        actor,
        base,
        now=NOW,
        expected_snapshot_identity=None,
    )
    expected_identity = perptape_snapshot_identity(base)
    stale = perptape_feed(
        perptape_candidate(
            client,
            symbol="SAMEUSDT",
            triggered_at=NOW,
            observed_at=NOW + timedelta(seconds=1),
            price=110,
        ),
        fetched_at=NOW,
    )
    fresh = perptape_feed(
        perptape_candidate(
            client,
            symbol="SAMEUSDT",
            triggered_at=NOW,
            observed_at=NOW + timedelta(seconds=2),
            price=120,
        ),
        fetched_at=NOW,
    )
    ordered = (fresh, stale) if fresh_first else (stale, fresh)
    for feed in ordered:
        service.record_perptape_feed(
            actor,
            feed,
            now=NOW + timedelta(seconds=10),
            expected_snapshot_identity=expected_identity,
        )

    persisted = queries.perptape_feed()
    assert persisted is not None
    assert len(persisted.candidates) == 1
    assert persisted.candidates[0].observed_at == fresh.candidates[0].observed_at
    assert persisted.candidates[0].reference_price == Decimal(120)


def test_postgres_perptape_payload_is_bounded_to_candidate_window(
    database: Database,
) -> None:
    service = TradingService(database)
    queries = TradingQueries(database)
    admin = service.bootstrap_admin("bounded-feed-admin", now=NOW)
    actor = service.create_service_principal(
        "bounded-feed-perptape",
        admin,
        now=NOW,
    )
    service.assign_role(actor, Role.PROPOSER, admin, now=NOW)
    client = perptape_test_client()
    candidates = tuple(
        perptape_candidate(
            client,
            symbol=f"B{index}USDT",
            triggered_at=NOW + timedelta(milliseconds=index),
            observed_at=NOW + timedelta(milliseconds=index),
        )
        for index in range(2_050)
    )

    service.record_perptape_feed(
        actor,
        perptape_feed(*candidates, fetched_at=NOW + timedelta(seconds=3)),
        now=NOW + timedelta(seconds=3),
        expected_snapshot_identity=None,
    )

    persisted = queries.perptape_feed()
    assert persisted is not None
    assert len(persisted.candidates) == 2_048
    assert persisted.candidates[0].symbol == "B2USDT"
    assert persisted.candidates[-1].symbol == "B2049USDT"


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
        expected_snapshot_identity=perptape_snapshot_identity(persisted_feed),
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
        record_snapshot=lambda feed, now, expected_snapshot_identity: service.record_perptape_feed(
            perptape_actor,
            feed,
            now=now,
            expected_snapshot_identity=expected_snapshot_identity,
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
