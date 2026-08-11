from __future__ import annotations

import asyncio
import base64
import json
import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event

from trading_control_plane.api import create_app
from trading_control_plane.binance import (
    BinanceEquity,
    BinanceInstrument,
    BinancePosition,
    BinanceReadOnlySnapshot,
)
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    Direction,
    DomainRejected,
    ExecutionEnvironment,
    ProposalSource,
    ReviewDecision,
    RiskTier,
    Role,
    SignalSourceMode,
    SystemRiskState,
)
from trading_control_plane.exchange_connection import ConnectionProbeResult
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
    PERPTAPE_CANDIDATE_WINDOW,
    PERPTAPE_PAYLOAD_MAX_BYTES,
    PERPTAPE_STRING_FIELD_MAX_BYTES,
    PerptapeCandidate,
    PerptapeClient,
    PerptapeFeedSnapshot,
    perptape_payload_size_bytes,
    perptape_snapshot_identity,
)
from trading_control_plane.perptape_stream import PerptapeSocket, PerptapeStreamWorker
from trading_control_plane.queries import TradingQueries
from trading_control_plane.runtime import RuntimeBindingSupervisor, RuntimeSyncWorker
from trading_control_plane.service import TradingService
from trading_control_plane.telegram import MockTelegramGateway
from trading_control_plane.venue_read_only import (
    VenueEquity,
    VenueInstrument,
    VenuePosition,
    VenueReadOnlySnapshot,
)

NOW = datetime.now(UTC)
AGENT = "0x2222222222222222222222222222222222222222"
VAULT = "0x1111111111111111111111111111111111111111"
OWNER = "0x3333333333333333333333333333333333333333"


def runtime_encryption_key() -> str:
    return base64.urlsafe_b64encode(b"runtime-binding-test-key-32-bytes"[:32]).decode().rstrip("=")


class BinanceReader:
    configured = True

    @staticmethod
    def read_active_instruments() -> tuple[BinanceInstrument, ...]:
        return (
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
                symbol="BTCUSDC",
                tick_size=Decimal("0.1"),
                lot_size=Decimal("0.001"),
                minimum_notional=Decimal("5"),
                quote_currency="USDC",
                collateral_currency="USDC",
                active=True,
            ),
        )

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

    def read_account_snapshots(
        self, symbols: tuple[str, ...], *, now: datetime
    ) -> tuple[BinanceReadOnlySnapshot, ...]:
        assert symbols == ("BTCUSDT",)
        return (self.read_snapshot(symbols[0], now=now),)


class HyperliquidReader:
    configured = True
    fact_environment = "LIVE"

    @staticmethod
    def read_active_instruments() -> tuple[HyperliquidInstrument, ...]:
        return (
            HyperliquidInstrument(
                symbol="BTC",
                tick_size=Decimal("1"),
                lot_size=Decimal("0.00001"),
                minimum_notional=Decimal("10"),
                quote_currency="USD",
                collateral_currency="USDC",
                active=True,
            ),
        )

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

    def read_account_snapshots(
        self, symbols: tuple[str, ...], *, now: datetime
    ) -> tuple[HyperliquidReadOnlySnapshot, ...]:
        assert symbols == ("BTC",)
        return (self.read_snapshot(symbols[0], now=now),)


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


def perptape_test_service(
    database: Database,
    label: str,
) -> tuple[TradingService, TradingQueries, UUID, PerptapeClient]:
    service = TradingService(database)
    queries = TradingQueries(database)
    admin = service.bootstrap_admin(f"{label}-admin", now=NOW)
    actor = service.create_service_principal(f"{label}-perptape", admin, now=NOW)
    service.assign_role(actor, Role.PROPOSER, admin, now=NOW)
    return service, queries, actor, perptape_test_client()


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
        base_snapshot=None,
    )
    http = perptape_feed(
        base.candidates[0],
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
                base_snapshot=base,
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
    persisted = queries.perptape_feed(actor)
    assert persisted is not None
    assert [candidate.symbol for candidate in persisted.candidates] == [
        "BASEUSDT",
        "HTTPUSDT",
        "WSSUSDT",
    ]
    assert persisted.fetched_at == max(http.fetched_at, wss.fetched_at)


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
        base_snapshot=None,
    )
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
            base_snapshot=base,
        )

    persisted = queries.perptape_feed(actor)
    assert persisted is not None
    assert len(persisted.candidates) == 1
    assert persisted.candidates[0].observed_at == fresh.candidates[0].observed_at
    assert persisted.candidates[0].reference_price == Decimal(120)


@pytest.mark.parametrize("completion_first", [True, False])
def test_three_way_merge_does_not_revive_completed_candidate(
    database: Database,
    completion_first: bool,
) -> None:
    service, queries, actor, client = perptape_test_service(
        database,
        f"three-way-complete-{completion_first}",
    )
    target = replace(
        perptape_candidate(
            client,
            symbol="TARGETUSDT",
            triggered_at=NOW,
            observed_at=NOW,
        ),
        data_health="DEGRADED",
        readiness="INCOMPLETE",
    )
    base = perptape_feed(target, fetched_at=NOW)
    service.record_perptape_feed(actor, base, now=NOW, base_snapshot=None)
    ready = replace(
        target,
        observed_at=NOW + timedelta(seconds=1),
        data_health="CURRENT",
        readiness="READY",
    )
    other = perptape_candidate(
        client,
        symbol="OTHERUSDT",
        triggered_at=NOW + timedelta(seconds=2),
        observed_at=NOW + timedelta(seconds=2),
    )
    completion = perptape_feed(ready, fetched_at=NOW + timedelta(seconds=3))
    stale_writer = perptape_feed(
        target,
        other,
        fetched_at=NOW + timedelta(seconds=3),
    )
    writes = (completion, stale_writer) if completion_first else (stale_writer, completion)
    for desired in writes:
        service.record_perptape_feed(
            actor,
            desired,
            now=NOW + timedelta(seconds=10),
            base_snapshot=base,
        )

    persisted = queries.perptape_feed(actor)
    assert persisted is not None
    assert {candidate.symbol for candidate in persisted.candidates} == {
        "TARGETUSDT",
        "OTHERUSDT",
    }
    completed = next(
        candidate for candidate in persisted.candidates if candidate.symbol == "TARGETUSDT"
    )
    assert completed.readiness == "READY"
    assert completed.data_health == "CURRENT"


@pytest.mark.parametrize("delete_first", [True, False])
def test_three_way_delete_tombstone_wins_over_unchanged_stale_snapshot(
    database: Database,
    delete_first: bool,
) -> None:
    service, queries, actor, client = perptape_test_service(
        database,
        f"three-way-delete-{delete_first}",
    )
    removed = perptape_candidate(
        client,
        symbol="REMOVEUSDT",
        triggered_at=NOW,
        observed_at=NOW,
    )
    kept = perptape_candidate(
        client,
        symbol="KEEPUSDT",
        triggered_at=NOW + timedelta(seconds=1),
        observed_at=NOW + timedelta(seconds=1),
    )
    other = perptape_candidate(
        client,
        symbol="OTHERUSDT",
        triggered_at=NOW + timedelta(seconds=2),
        observed_at=NOW + timedelta(seconds=2),
    )
    base = perptape_feed(removed, kept, fetched_at=NOW)
    service.record_perptape_feed(actor, base, now=NOW, base_snapshot=None)
    deletion = perptape_feed(kept, fetched_at=NOW + timedelta(seconds=3))
    stale_writer = perptape_feed(
        removed,
        kept,
        other,
        fetched_at=NOW + timedelta(seconds=3),
    )
    writes = (deletion, stale_writer) if delete_first else (stale_writer, deletion)
    for desired in writes:
        service.record_perptape_feed(
            actor,
            desired,
            now=NOW + timedelta(seconds=10),
            base_snapshot=base,
        )

    persisted = queries.perptape_feed(actor)
    assert persisted is not None
    assert [candidate.symbol for candidate in persisted.candidates] == [
        "KEEPUSDT",
        "OTHERUSDT",
    ]


@pytest.mark.parametrize("delete_first", [True, False])
@pytest.mark.parametrize(
    ("case", "expected_present"),
    [
        ("NEWER", True),
        ("OLDER", False),
        ("SAME", False),
        ("READY_COMPLETION", True),
    ],
)
def test_three_way_delete_conflict_preserves_only_provably_newer_or_healthier_fact(
    database: Database,
    delete_first: bool,
    case: str,
    expected_present: bool,
) -> None:
    service, queries, actor, client = perptape_test_service(
        database,
        f"delete-conflict-{case}-{delete_first}",
    )
    base_candidate = perptape_candidate(
        client,
        symbol="CONFLICTUSDT",
        triggered_at=NOW,
        observed_at=NOW,
        price=100,
    )
    if case == "READY_COMPLETION":
        base_candidate = replace(
            base_candidate,
            data_health="DEGRADED",
            readiness="INCOMPLETE",
        )
        upsert_candidate = replace(
            base_candidate,
            data_health="CURRENT",
            readiness="READY",
        )
    elif case == "NEWER":
        upsert_candidate = replace(
            base_candidate,
            observed_at=NOW + timedelta(seconds=1),
            reference_price=Decimal(110),
        )
    elif case == "OLDER":
        upsert_candidate = replace(
            base_candidate,
            observed_at=NOW - timedelta(seconds=1),
            reference_price=Decimal(90),
        )
    else:
        assert case == "SAME"
        upsert_candidate = base_candidate
    base = perptape_feed(base_candidate, fetched_at=NOW)
    deletion = perptape_feed(fetched_at=NOW + timedelta(seconds=2))
    upsert = perptape_feed(upsert_candidate, fetched_at=NOW + timedelta(seconds=2))
    service.record_perptape_feed(actor, base, now=NOW, base_snapshot=None)
    writes = (deletion, upsert) if delete_first else (upsert, deletion)
    for desired in writes:
        service.record_perptape_feed(
            actor,
            desired,
            now=NOW + timedelta(seconds=10),
            base_snapshot=base,
        )

    persisted = queries.perptape_feed(actor)
    assert persisted is not None
    assert bool(persisted.candidates) is expected_present
    if expected_present:
        assert persisted.candidates == (upsert_candidate,)


@pytest.mark.parametrize("cooldown_first", [True, False])
def test_three_way_metadata_does_not_restore_stale_next_allowed_at(
    database: Database,
    cooldown_first: bool,
) -> None:
    service, queries, actor, client = perptape_test_service(
        database,
        f"three-way-cooldown-{cooldown_first}",
    )
    original = perptape_candidate(
        client,
        symbol="BASEUSDT",
        triggered_at=NOW,
        observed_at=NOW,
    )
    other = perptape_candidate(
        client,
        symbol="OTHERUSDT",
        triggered_at=NOW + timedelta(seconds=1),
        observed_at=NOW + timedelta(seconds=1),
    )
    base = perptape_feed(original, fetched_at=NOW)
    service.record_perptape_feed(actor, base, now=NOW, base_snapshot=None)
    new_deadline = NOW + timedelta(minutes=10)
    cooldown = replace(
        base,
        fetched_at=NOW + timedelta(seconds=1),
        next_allowed_at=new_deadline,
    )
    stale_writer = replace(
        base,
        fetched_at=NOW + timedelta(seconds=2),
        candidates=(original, other),
    )
    writes = (cooldown, stale_writer) if cooldown_first else (stale_writer, cooldown)
    for desired in writes:
        service.record_perptape_feed(
            actor,
            desired,
            now=NOW + timedelta(minutes=11),
            base_snapshot=base,
        )

    persisted = queries.perptape_feed(actor)
    assert persisted is not None
    assert persisted.next_allowed_at == new_deadline
    assert {candidate.symbol for candidate in persisted.candidates} == {
        "BASEUSDT",
        "OTHERUSDT",
    }


def test_three_way_three_writers_and_retry_are_idempotent(
    database: Database,
) -> None:
    service, queries, actor, client = perptape_test_service(database, "three-way-retry")
    obsolete = perptape_candidate(
        client,
        symbol="OLDUSDT",
        triggered_at=NOW,
        observed_at=NOW,
    )
    first = perptape_candidate(
        client,
        symbol="FIRSTUSDT",
        triggered_at=NOW + timedelta(seconds=1),
        observed_at=NOW + timedelta(seconds=1),
    )
    second = perptape_candidate(
        client,
        symbol="SECONDUSDT",
        triggered_at=NOW + timedelta(seconds=2),
        observed_at=NOW + timedelta(seconds=2),
    )
    base = perptape_feed(obsolete, fetched_at=NOW)
    service.record_perptape_feed(actor, base, now=NOW, base_snapshot=None)
    first_writer = perptape_feed(
        obsolete,
        first,
        fetched_at=NOW + timedelta(seconds=3),
    )
    second_writer = perptape_feed(
        obsolete,
        second,
        fetched_at=NOW + timedelta(seconds=3),
    )
    cleanup = perptape_feed(fetched_at=NOW + timedelta(seconds=3))
    for desired in (first_writer, second_writer, cleanup):
        version = service.record_perptape_feed(
            actor,
            desired,
            now=NOW + timedelta(seconds=10),
            base_snapshot=base,
        )
    retry_version = service.record_perptape_feed(
        actor,
        first_writer,
        now=NOW + timedelta(seconds=10),
        base_snapshot=base,
    )

    assert retry_version == version
    persisted = queries.perptape_feed(actor)
    assert persisted is not None
    assert [candidate.symbol for candidate in persisted.candidates] == [
        "FIRSTUSDT",
        "SECONDUSDT",
    ]


@pytest.mark.parametrize("trim_first", [True, False])
def test_three_way_window_trim_is_a_stable_tombstone(
    database: Database,
    trim_first: bool,
) -> None:
    service, queries, actor, client = perptape_test_service(
        database,
        f"three-way-trim-{trim_first}",
    )
    template = perptape_candidate(
        client,
        symbol="TEMPLATEUSDT",
        triggered_at=NOW,
        observed_at=NOW,
    )
    candidates = tuple(
        replace(
            template,
            candidate_id=f"pt_trim_{index}",
            symbol=f"T{index}USDT",
            canonical_symbol=f"T{index}",
            observed_at=NOW + timedelta(microseconds=index),
            triggered_at=NOW + timedelta(microseconds=index),
        )
        for index in range(PERPTAPE_CANDIDATE_WINDOW)
    )
    base = perptape_feed(*candidates, fetched_at=NOW)
    service.record_perptape_feed(actor, base, now=NOW, base_snapshot=None)
    added_first = replace(
        template,
        candidate_id="pt_trim_first",
        symbol="NEWFIRSTUSDT",
        canonical_symbol="NEWFIRST",
        observed_at=NOW + timedelta(seconds=1),
        triggered_at=NOW + timedelta(seconds=1),
    )
    added_second = replace(
        template,
        candidate_id="pt_trim_second",
        symbol="NEWSECONDUSDT",
        canonical_symbol="NEWSECOND",
        observed_at=NOW + timedelta(seconds=2),
        triggered_at=NOW + timedelta(seconds=2),
    )
    trim_writer = perptape_feed(
        *candidates,
        added_first,
        fetched_at=NOW + timedelta(seconds=3),
    )
    stale_writer = perptape_feed(
        *candidates,
        added_second,
        fetched_at=NOW + timedelta(seconds=3),
    )
    writes = (trim_writer, stale_writer) if trim_first else (stale_writer, trim_writer)
    for desired in writes:
        service.record_perptape_feed(
            actor,
            desired,
            now=NOW + timedelta(seconds=10),
            base_snapshot=base,
        )

    persisted = queries.perptape_feed(actor)
    assert persisted is not None
    symbols = {candidate.symbol for candidate in persisted.candidates}
    assert len(persisted.candidates) == PERPTAPE_CANDIDATE_WINDOW
    assert {"NEWFIRSTUSDT", "NEWSECONDUSDT"} <= symbols
    assert "T0USDT" not in symbols
    assert "T1USDT" not in symbols


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
        base_snapshot=None,
    )

    persisted = queries.perptape_feed(actor)
    assert persisted is not None
    assert len(persisted.candidates) == 2_048
    assert persisted.candidates[0].symbol == "B2USDT"
    assert persisted.candidates[-1].symbol == "B2049USDT"


def test_postgres_rejects_oversized_payload_before_replacing_authoritative_feed(
    database: Database,
) -> None:
    service, queries, actor, client = perptape_test_service(database, "payload-bytes")
    original = perptape_feed(
        perptape_candidate(
            client,
            symbol="ORIGINALUSDT",
            triggered_at=NOW,
            observed_at=NOW,
        ),
        fetched_at=NOW,
    )
    service.record_perptape_feed(actor, original, now=NOW, base_snapshot=None)
    template = original.candidates[0]
    candidates = tuple(
        replace(
            template,
            candidate_id=f"pt_payload_{index}",
            symbol=f"P{index}USDT",
            canonical_symbol=f"P{index}",
            observed_at=NOW + timedelta(microseconds=index),
            triggered_at=NOW + timedelta(microseconds=index),
            rationale="r" * PERPTAPE_STRING_FIELD_MAX_BYTES["rationale"],
            detail_url="d" * PERPTAPE_STRING_FIELD_MAX_BYTES["detail_url"],
            chart_url="c" * PERPTAPE_STRING_FIELD_MAX_BYTES["chart_url"],
        )
        for index in range(1_000)
    )
    oversized = perptape_feed(
        *candidates,
        fetched_at=NOW + timedelta(seconds=1),
    )
    assert perptape_payload_size_bytes(oversized) > PERPTAPE_PAYLOAD_MAX_BYTES

    with pytest.raises(DomainRejected, match="PERPTAPE_PAYLOAD_TOO_LARGE"):
        service.record_perptape_feed(
            actor,
            oversized,
            now=NOW + timedelta(seconds=1),
            base_snapshot=original,
        )

    persisted = queries.perptape_feed(actor)
    assert persisted is not None
    assert perptape_snapshot_identity(persisted) == perptape_snapshot_identity(original)


@pytest.mark.parametrize("field", ["generated_at", "fetched_at", "next_allowed_at"])
def test_postgres_first_write_rejects_naive_metadata_without_creating_feed(
    database: Database,
    field: str,
) -> None:
    service, queries, actor, client = perptape_test_service(
        database,
        f"naive-metadata-{field}",
    )
    feed = perptape_feed(
        perptape_candidate(
            client,
            symbol="NAIVEUSDT",
            triggered_at=NOW,
            observed_at=NOW,
        ),
        fetched_at=NOW,
    )
    invalid = replace(feed, **{field: getattr(feed, field).replace(tzinfo=None)})

    with pytest.raises(DomainRejected, match="PERPTAPE_DATETIME_INVALID"):
        service.record_perptape_feed(
            actor,
            invalid,
            now=NOW,
            base_snapshot=None,
        )
    assert queries.perptape_feed(actor) is None


@pytest.mark.parametrize("initial_write", [True, False])
@pytest.mark.parametrize(
    "invalid_time",
    [
        pytest.param(
            datetime.min.replace(tzinfo=timezone(timedelta(hours=23, minutes=59))),
            id="min-underflow",
        ),
        pytest.param(
            datetime.max.replace(tzinfo=timezone(-timedelta(hours=23, minutes=59))),
            id="max-overflow",
        ),
    ],
)
@pytest.mark.parametrize(
    "field",
    ["generated_at", "fetched_at", "next_allowed_at", "observed_at", "triggered_at"],
)
def test_postgres_rejects_utc_conversion_overflow_without_mutating_feed(
    database: Database,
    field: str,
    invalid_time: datetime,
    initial_write: bool,
) -> None:
    service, queries, actor, client = perptape_test_service(
        database,
        f"utc-overflow-{field}-{initial_write}",
    )
    valid = perptape_feed(
        perptape_candidate(
            client,
            symbol="TIMEBOUNDARYUSDT",
            triggered_at=NOW,
            observed_at=NOW,
        ),
        fetched_at=NOW,
    )
    if not initial_write:
        service.record_perptape_feed(actor, valid, now=NOW, base_snapshot=None)
    if field in {"observed_at", "triggered_at"}:
        invalid = replace(
            valid,
            candidates=(replace(valid.candidates[0], **{field: invalid_time}),),
        )
    else:
        invalid = replace(valid, **{field: invalid_time})

    with pytest.raises(DomainRejected, match="PERPTAPE_DATETIME_INVALID"):
        service.record_perptape_feed(
            actor,
            invalid,
            now=NOW,
            base_snapshot=None if initial_write else valid,
        )

    persisted = queries.perptape_feed(actor)
    if initial_write:
        assert persisted is None
    else:
        assert persisted is not None
        assert perptape_snapshot_identity(persisted) == perptape_snapshot_identity(valid)


@pytest.mark.parametrize("initial_write", [True, False])
@pytest.mark.parametrize(
    "invalid_now",
    [
        datetime.min.replace(tzinfo=timezone(timedelta(hours=23, minutes=59))),
        datetime.max.replace(tzinfo=timezone(-timedelta(hours=23, minutes=59))),
    ],
)
def test_postgres_rejects_invalid_service_clock_without_mutating_feed(
    database: Database,
    invalid_now: datetime,
    initial_write: bool,
) -> None:
    service, queries, actor, client = perptape_test_service(
        database,
        f"invalid-service-clock-{initial_write}",
    )
    valid = perptape_feed(
        perptape_candidate(
            client,
            symbol="SERVICECLOCKUSDT",
            triggered_at=NOW,
            observed_at=NOW,
        ),
        fetched_at=NOW,
    )
    if not initial_write:
        service.record_perptape_feed(actor, valid, now=NOW, base_snapshot=None)

    with pytest.raises(DomainRejected, match="PERPTAPE_DATETIME_INVALID"):
        service.record_perptape_feed(
            actor,
            valid,
            now=invalid_now,
            base_snapshot=None if initial_write else valid,
        )

    persisted = queries.perptape_feed(actor)
    if initial_write:
        assert persisted is None
    else:
        assert persisted is not None
        assert perptape_snapshot_identity(persisted) == perptape_snapshot_identity(valid)


def test_postgres_exact_utc_max_feed_remains_updatable_at_same_fetched_at(
    database: Database,
) -> None:
    service, queries, actor, client = perptape_test_service(database, "utc-max-update")
    maximum = datetime.max.replace(tzinfo=UTC)
    candidate = replace(
        perptape_candidate(
            client,
            symbol="MAXTIMEUSDT",
            triggered_at=NOW,
            observed_at=NOW,
        ),
        triggered_at=maximum,
        observed_at=maximum,
    )
    initial = perptape_feed(candidate, fetched_at=maximum)
    updated = replace(
        initial,
        candidates=(replace(candidate, reference_price=Decimal("100001")),),
    )

    first_version = service.record_perptape_feed(
        actor,
        initial,
        now=maximum,
        base_snapshot=None,
    )
    second_version = service.record_perptape_feed(
        actor,
        updated,
        now=maximum,
        base_snapshot=initial,
    )

    persisted = queries.perptape_feed(actor)
    assert persisted is not None
    assert first_version == 1
    assert second_version == 2
    assert persisted.fetched_at == maximum
    assert persisted.candidates[0].reference_price == Decimal("100001")
    assert perptape_snapshot_identity(persisted) == perptape_snapshot_identity(updated)


def test_postgres_rejects_decimal_extremes_without_creating_feed(
    database: Database,
) -> None:
    service, queries, actor, client = perptape_test_service(database, "decimal-extremes")
    candidate = perptape_candidate(
        client,
        symbol="DECIMALUSDT",
        triggered_at=NOW,
        observed_at=NOW,
    )
    for value in (
        Decimal("1E999999999999999999"),
        Decimal("1E-999999999999999999"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-0"),
    ):
        invalid = perptape_feed(
            replace(candidate, reference_price=value),
            fetched_at=NOW,
        )
        with pytest.raises(DomainRejected, match="PERPTAPE_DECIMAL_INVALID"):
            service.record_perptape_feed(
                actor,
                invalid,
                now=NOW,
                base_snapshot=None,
            )
        assert queries.perptape_feed(actor) is None


@pytest.mark.parametrize(
    "invalid_time",
    [
        datetime.max.replace(tzinfo=UTC),
        datetime(2026, 7, 31, 8),
        datetime.min.replace(tzinfo=timezone(timedelta(hours=23, minutes=59))),
    ],
)
@pytest.mark.parametrize("failure_point", ["START", "COMPLETION"])
def test_runtime_invalid_clock_executes_zero_postgres_statements(
    database: Database,
    invalid_time: datetime,
    failure_point: str,
) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        _env_file=None,
    )
    worker: Any = object.__new__(RuntimeSyncWorker)
    worker.settings = settings
    values = iter(
        [invalid_time]
        if failure_point == "START"
        else [datetime(2026, 7, 31, tzinfo=UTC), invalid_time]
    )
    worker.clock = lambda: next(values)
    worker.queries = TradingQueries(database)
    statements: list[str] = []

    def record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(database.engine, "before_cursor_execute", record_statement)
    try:
        with pytest.raises(DomainRejected, match="PERPTAPE_DATETIME_INVALID"):
            worker.run_once()
    finally:
        event.remove(database.engine, "before_cursor_execute", record_statement)

    assert statements == []


def test_runtime_health_preserves_last_success_and_rate_limit_backoff(
    database: Database,
) -> None:
    service = TradingService(database)
    admin = service.bootstrap_admin("runtime-health-admin", now=NOW)
    actor = service.create_service_principal("runtime-health-sync", admin, now=NOW)
    queries = TradingQueries(database)

    service.record_runtime_source_health(
        actor,
        {"HYPERLIQUID": {"status": "SUCCESS", "items_observed": 3}},
        now=NOW,
    )
    first_failure = NOW + timedelta(minutes=1)
    service.record_runtime_source_health(
        actor,
        {
            "HYPERLIQUID": {
                "status": "FAILED",
                "error_code": "HYPERLIQUID_RATE_LIMITED",
            }
        },
        now=first_failure,
    )
    failed = queries.runtime_source_health(actor, "HYPERLIQUID")
    assert failed is not None
    assert failed["status"] == "FAILED"
    assert failed["checked_at"] == first_failure.isoformat()
    assert failed["last_success_at"] == NOW.isoformat()
    assert failed["retry_at"] == (first_failure + timedelta(seconds=60)).isoformat()
    assert failed["consecutive_failures"] == 1

    service.record_runtime_source_health(
        actor,
        {
            "HYPERLIQUID": {
                "status": "SKIPPED",
                "error_code": "HYPERLIQUID_RATE_LIMITED_COOLDOWN",
            }
        },
        now=first_failure + timedelta(seconds=30),
    )
    assert queries.runtime_source_health(actor, "HYPERLIQUID") == failed

    second_failure = first_failure + timedelta(seconds=60)
    service.record_runtime_source_health(
        actor,
        {
            "HYPERLIQUID": {
                "status": "FAILED",
                "error_code": "HYPERLIQUID_RATE_LIMITED",
            }
        },
        now=second_failure,
    )
    repeated = queries.runtime_source_health(actor, "HYPERLIQUID")
    assert repeated is not None
    assert repeated["retry_at"] == (second_failure + timedelta(seconds=120)).isoformat()
    assert repeated["consecutive_failures"] == 2
    assert repeated["last_success_at"] == NOW.isoformat()

    recovered_at = second_failure + timedelta(minutes=3)
    service.record_runtime_source_health(
        actor,
        {"HYPERLIQUID": {"status": "SUCCESS", "items_observed": 4}},
        now=recovered_at,
    )
    recovered = queries.runtime_source_health(actor, "HYPERLIQUID")
    assert recovered is not None
    assert recovered["last_success_at"] == recovered_at.isoformat()
    assert recovered["retry_at"] is None
    assert recovered["consecutive_failures"] == 0


def test_runtime_feed_and_health_are_isolated_by_team_and_account(
    database: Database,
) -> None:
    service = TradingService(database)
    queries = TradingQueries(database)
    admin = service.bootstrap_admin("runtime-scope-admin", now=NOW)
    context = queries.user_context(admin)
    workspace_id = UUID(context["active_workspace"]["workspace_id"])
    first_team_id = UUID(context["active_team"]["team_id"])
    service.create_exchange_account(
        actor_id=admin,
        account_id="shared-runtime-account",
        venue="BINANCE",
        label="First Runtime Account",
        credentials=None,
        idempotency_key="create-first-runtime-account",
        now=NOW,
    )
    first_principal = service.create_service_principal(
        "runtime-scope-first", admin, now=NOW
    )
    service.assign_role(
        first_principal,
        Role.OPERATOR,
        admin,
        account_scope="shared-runtime-account",
        venue_scope="BINANCE",
        now=NOW,
    )
    service.assign_role(first_principal, Role.PROPOSER, admin, now=NOW)
    client = perptape_test_client()
    first_feed = perptape_feed(
        perptape_candidate(
            client,
            symbol="BTCUSDT",
            triggered_at=NOW,
            observed_at=NOW,
        ),
        fetched_at=NOW,
    )
    service.record_perptape_feed(
        first_principal,
        first_feed,
        now=NOW,
        base_snapshot=None,
    )
    service.record_runtime_source_health(
        first_principal,
        {"BINANCE": {"status": "SUCCESS", "items_observed": 1}},
        scopes={"BINANCE": ("shared-runtime-account", "BINANCE")},
        now=NOW,
    )

    second_team_id = service.create_team(
        actor_id=admin,
        name="Runtime Scope Two",
        slug="runtime-scope-two",
        idempotency_key="create-runtime-scope-two",
        now=NOW + timedelta(seconds=1),
    )
    service.create_exchange_account(
        actor_id=admin,
        account_id="shared-runtime-account",
        venue="BINANCE",
        label="Second Runtime Account",
        credentials=None,
        idempotency_key="create-second-runtime-account",
        now=NOW + timedelta(seconds=1),
    )
    second_principal = service.create_service_principal(
        "runtime-scope-second", admin, now=NOW + timedelta(seconds=1)
    )
    service.assign_role(
        second_principal,
        Role.OPERATOR,
        admin,
        account_scope="shared-runtime-account",
        venue_scope="BINANCE",
        now=NOW + timedelta(seconds=1),
    )
    service.assign_role(
        second_principal,
        Role.PROPOSER,
        admin,
        now=NOW + timedelta(seconds=1),
    )
    second_feed = perptape_feed(
        perptape_candidate(
            client,
            symbol="ETHUSDT",
            triggered_at=NOW + timedelta(seconds=1),
            observed_at=NOW + timedelta(seconds=1),
        ),
        fetched_at=NOW + timedelta(seconds=1),
    )
    service.record_perptape_feed(
        second_principal,
        second_feed,
        now=NOW + timedelta(seconds=1),
        base_snapshot=None,
    )
    service.record_runtime_source_health(
        second_principal,
        {
            "BINANCE": {
                "status": "FAILED",
                "items_observed": 0,
                "error_code": "BINANCE_AUTHENTICATION_FAILED",
            }
        },
        scopes={"BINANCE": ("shared-runtime-account", "BINANCE")},
        now=NOW + timedelta(seconds=1),
    )

    assert second_team_id != first_team_id
    assert queries.perptape_feed(second_principal).candidates[0].symbol == "ETHUSDT"  # type: ignore[union-attr]
    second_health = queries.runtime_source_health(
        second_principal,
        "BINANCE",
        account_id="shared-runtime-account",
        venue="BINANCE",
    )
    assert second_health is not None and second_health["status"] == "FAILED"
    assert "BINANCE:shared-runtime-account" in queries.runtime_snapshot(admin)[
        "source_health"
    ]

    service.select_scope(
        actor_id=admin,
        workspace_id=workspace_id,
        team_id=first_team_id,
        idempotency_key="return-first-runtime-scope",
        now=NOW + timedelta(seconds=2),
    )
    assert queries.perptape_feed(first_principal).candidates[0].symbol == "BTCUSDT"  # type: ignore[union-attr]
    first_health = queries.runtime_source_health(
        first_principal,
        "BINANCE",
        account_id="shared-runtime-account",
        venue="BINANCE",
    )
    assert first_health is not None and first_health["status"] == "SUCCESS"
    assert queries.runtime_snapshot(admin)["source_health"][
        "BINANCE:shared-runtime-account"
    ]["status"] == "SUCCESS"


def test_runtime_worker_refreshes_perptape_two_venues_and_vault_without_sending(
    database: Database,
) -> None:
    service = TradingService(database)
    admin = service.bootstrap_admin("runtime-admin", now=NOW)
    actor = service.create_service_principal("runtime-sync", admin, now=NOW)
    perptape_actor = service.create_service_principal("perptape", admin, now=NOW)
    service.assign_role(actor, Role.OPERATOR, admin, now=NOW)
    service.assign_role(
        actor,
        Role.OPERATOR,
        admin,
        account_scope="binance-main",
        venue_scope="BINANCE",
        now=NOW,
    )
    service.assign_role(
        actor,
        Role.OPERATOR,
        admin,
        account_scope="hyperliquid-main",
        venue_scope="HYPERLIQUID",
        now=NOW,
    )
    service.assign_role(actor, Role.TREASURY_ADMIN, admin, now=NOW)
    service.assign_role(perptape_actor, Role.PROPOSER, admin, now=NOW)
    service.set_risk_policy(
        actor_id=admin,
        version="runtime-test-v1",
        system_state=SystemRiskState.NORMAL,
        max_total_risk=Decimal(1_000),
        max_account_risk=Decimal(1_000),
        max_single_loss=Decimal(1_000),
        max_consecutive_losses=3,
        loss_cooldown=timedelta(hours=1),
        # Keep the freshness policy wider than the full PostgreSQL suite runtime.
        # The worker still uses a fixed clock, so a short window makes this
        # readiness assertion depend on test collection order rather than facts.
        max_fact_age=timedelta(hours=1),
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
        base_snapshot=persisted_feed,
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

    # The full integration suite can run longer than the runtime freshness
    # window after module-level NOW is captured. Refresh only persisted feed
    # timing here so this assertion continues to test shared-cache behavior.
    api_now = datetime.now(UTC)
    service.record_perptape_feed(
        perptape_actor,
        replace(
            persisted_feed,
            generated_at=api_now,
            fetched_at=api_now,
            next_allowed_at=api_now,
        ),
        now=api_now,
        base_snapshot=persisted_feed,
    )

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


def test_database_bound_supervisor_persists_account_facts_without_trading(
    database: Database,
) -> None:
    service = TradingService(
        database,
        credential_encryption_key=runtime_encryption_key(),
    )
    admin = service.bootstrap_admin("bound-runtime-admin", now=NOW)
    exchange_account_id = service.create_exchange_account(
        actor_id=admin,
        account_id="bound-binance-main",
        venue="BINANCE",
        label="Bound Binance Main",
        credentials={"api_key": "bound-key", "api_secret": "bound-secret"},
        idempotency_key="create-bound-binance-main",
        now=NOW,
    )
    command, replay = service.prepare_exchange_account_connection_verification(
        exchange_account_id,
        actor_id=admin,
        expected_version=1,
        idempotency_key="verify-bound-binance-main",
    )
    assert replay is None and command is not None
    verification = service.record_exchange_account_connection_verification(
        command,
        ConnectionProbeResult(True, None),
        actor_id=admin,
        idempotency_key="verify-bound-binance-main",
        now=NOW,
    )
    service.configure_exchange_account_runtime_sync(
        exchange_account_id,
        actor_id=admin,
        enabled=True,
        expected_version=int(verification["version"]),
        idempotency_key="enable-bound-binance-main",
        now=NOW,
    )
    service.set_risk_policy(
        actor_id=admin,
        version="bound-runtime-risk-v1",
        system_state=SystemRiskState.NORMAL,
        max_total_risk=Decimal(1_000),
        max_account_risk=Decimal(1_000),
        max_single_loss=Decimal(1_000),
        max_consecutive_losses=3,
        loss_cooldown=timedelta(hours=1),
        max_fact_age=timedelta(minutes=5),
        now=NOW,
    )
    settings = Settings(
        database_url=str(database.engine.url),
        credential_encryption_key=runtime_encryption_key(),
        runtime_sync_enabled=True,
        runtime_sync_interval_seconds=60,
        _env_file=None,
    )
    observed_settings: list[Settings] = []

    def worker_factory(scoped: Settings, scoped_database: Database) -> RuntimeSyncWorker:
        observed_settings.append(scoped)
        return RuntimeSyncWorker(
            settings=scoped,
            database=scoped_database,
            perptape=PerptapeClient(
                base_url="https://perptape.com",
                api_key=None,
                contract_version="breakouts-v1",
                cache_ttl=timedelta(minutes=1),
            ),
            binance=BinanceReader(),  # type: ignore[arg-type]
            hyperliquid=HyperliquidReader(),  # type: ignore[arg-type]
            notilt=NoTiltReader(),  # type: ignore[arg-type]
            notilt_valuator=NoTiltUsdValuator(),
            clock=lambda: NOW,
        )

    supervisor = RuntimeBindingSupervisor(
        settings=settings,
        database=database,
        clock=lambda: NOW,
        worker_factory=worker_factory,
    )

    assert supervisor.has_bindings() is True
    report = supervisor.run_once(started_at=NOW, completed_at=NOW)

    assert report.successful is True
    assert len(report.sources) == 1
    assert next(iter(report.sources.values())).items_observed == 1
    assert len(observed_settings) == 1
    assert observed_settings[0].binance_api_key == "bound-key"
    assert observed_settings[0].binance_api_secret == "bound-secret"  # noqa: S105
    facts = TradingQueries(database).venue_facts(
        admin,
        "bound-binance-main",
        "BINANCE",
        "LIVE",
    )
    assert facts["equity"]["equity"] == "10.000000000000000000"
    assert facts["positions"][0]["quantity"] == "0E-18"
    health = TradingQueries(database).runtime_source_health(
        admin,
        "BINANCE",
        account_id="bound-binance-main",
        venue="BINANCE",
    )
    assert health is not None and health["status"] == "SUCCESS"
    account = TradingQueries(database).exchange_accounts(admin)["data"][0]
    assert account["runtime_binding"]["bound"] is True
    assert account["trading"]["enabled"] is False


@pytest.mark.parametrize(
    ("venue", "credentials", "symbol"),
    [
        (
            "OKX",
            {"api_key": "okx-key", "api_secret": "okx-secret", "passphrase": "okx-pass"},
            "BTC-USDT-SWAP",
        ),
        (
            "BYBIT",
            {"api_key": "bybit-key", "api_secret": "bybit-secret"},
            "BTCUSDT",
        ),
    ],
)
def test_database_bound_okx_bybit_facts_use_exact_account_scope_without_trading(
    database: Database,
    venue: str,
    credentials: dict[str, str],
    symbol: str,
) -> None:
    service = TradingService(database, credential_encryption_key=runtime_encryption_key())
    admin = service.bootstrap_admin(f"bound-{venue.lower()}-admin", now=NOW)
    account_id = f"bound-{venue.lower()}-main"
    exchange_account_id = service.create_exchange_account(
        actor_id=admin,
        account_id=account_id,
        venue=venue,
        label=f"Bound {venue}",
        credentials=credentials,
        idempotency_key=f"create-{account_id}",
        now=NOW,
    )
    command, replay = service.prepare_exchange_account_connection_verification(
        exchange_account_id,
        actor_id=admin,
        expected_version=1,
        idempotency_key=f"verify-{account_id}",
    )
    assert replay is None and command is not None
    verification = service.record_exchange_account_connection_verification(
        command,
        ConnectionProbeResult(True, None),
        actor_id=admin,
        idempotency_key=f"verify-{account_id}",
        now=NOW,
    )
    service.configure_exchange_account_runtime_sync(
        exchange_account_id,
        actor_id=admin,
        enabled=True,
        expected_version=int(verification["version"]),
        idempotency_key=f"enable-{account_id}",
        now=NOW,
    )
    service.set_risk_policy(
        actor_id=admin,
        version=f"{venue.lower()}-runtime-risk-v1",
        system_state=SystemRiskState.NORMAL,
        max_total_risk=Decimal(1_000),
        max_account_risk=Decimal(1_000),
        max_single_loss=Decimal(1_000),
        max_consecutive_losses=3,
        loss_cooldown=timedelta(hours=1),
        max_fact_age=timedelta(minutes=5),
        now=NOW,
    )
    binding = service.runtime_account_bindings()[0]
    instrument = VenueInstrument(
        symbol=symbol,
        tick_size=Decimal("0.1"),
        lot_size=Decimal("0.001"),
        minimum_notional=Decimal("5"),
        quote_currency="USDT",
        collateral_currency="USDT",
        active=True,
    )
    snapshot = VenueReadOnlySnapshot(
        symbol=symbol,
        observed_at=NOW,
        instrument=instrument,
        orders=(),
        fills=(),
        position=VenuePosition(Decimal(0), Decimal(0), Decimal("50000"), NOW),
        equity=VenueEquity(Decimal("1000"), Decimal("900"), "USD", NOW),
        funding=(),
        protection=None,
    )

    class Reader:
        @staticmethod
        def read_active_instruments() -> tuple[VenueInstrument, ...]:
            return (instrument,)

        @staticmethod
        def read_account_snapshots(
            symbols: tuple[str, ...], *, now: datetime
        ) -> tuple[VenueReadOnlySnapshot, ...]:
            assert symbols == ()
            assert now == NOW
            return (snapshot,)

    settings = Settings(
        database_url=str(database.engine.url),
        credential_encryption_key=runtime_encryption_key(),
        runtime_sync_enabled=True,
        runtime_sync_service_username=binding.service_principal_username,
        _env_file=None,
    )
    worker = RuntimeSyncWorker(
        settings=settings,
        database=database,
        perptape=PerptapeClient(
            base_url="https://perptape.com",
            api_key=None,
            contract_version="breakouts-v1",
            cache_ttl=timedelta(minutes=1),
        ),
        binance=BinanceReader(),  # type: ignore[arg-type]
        hyperliquid=HyperliquidReader(),  # type: ignore[arg-type]
        notilt=NoTiltReader(),  # type: ignore[arg-type]
        notilt_valuator=NoTiltUsdValuator(),
        clock=lambda: NOW,
    )
    worker._database_account_reader = Reader()  # type: ignore[assignment]
    worker._database_account_scope = (venue, account_id)

    result = worker.run_bound_account_once(binding, now=NOW)

    assert result.status == "SUCCESS"
    facts = TradingQueries(database).venue_facts(admin, account_id, venue, "LIVE")
    assert facts["equity"]["equity"] == "1000.000000000000000000"
    assert facts["positions"][0]["symbol"] == symbol
    health = TradingQueries(database).runtime_source_health(
        admin,
        venue,
        account_id=account_id,
        venue=venue,
    )
    assert health is not None and health["status"] == "SUCCESS"
    account = TradingQueries(database).exchange_accounts(admin)["data"][0]
    assert account["runtime_binding"]["bound"] is True
    assert account["trading"]["enabled"] is False


def test_three_timeframe_resonance_creates_one_pending_system_proposal(
    database: Database,
) -> None:
    service = TradingService(database)
    admin = service.bootstrap_admin("resonance-admin", now=NOW)
    perptape_actor = service.create_service_principal("perptape", admin, now=NOW)
    service.assign_role(
        perptape_actor,
        Role.PROPOSER,
        admin,
        "acct-1",
        "BINANCE",
        now=NOW,
    )
    instrument_id = service.register_instrument(
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
        now=NOW,
    )
    service.set_proposal_default_config(
        admin,
        "resonance-policy-v1",
        account_id="acct-1",
        risk_tier=RiskTier.MEDIUM,
        notional=Decimal("100"),
        max_risk=Decimal("1"),
        invalidation_bps=200,
        expires_in_minutes=480,
        rationale="automatic resonance proposal awaiting independent review",
        auto_proposal_enabled=True,
        auto_proposal_min_timeframes=3,
        now=NOW,
    )
    settings = Settings(
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        perptape_api_key="fixture-key",
        runtime_sync_enabled=True,
        _env_file=None,
    )
    client = PerptapeClient(
        base_url="https://perptape.com",
        api_key="fixture-key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(0),
        fetcher=lambda _url, _headers, _timeout: {},
    )
    candidates = tuple(
        client.parse_stream_alert(
            {
                "ex": "BN",
                "s": "BTCUSDT",
                "cs": "BTCUSDT",
                "dir": "HH",
                "p": 100_000,
                "th": 99_000,
                "tf": timeframe,
                "t": int(NOW.timestamp() * 1_000),
                "u": int(NOW.timestamp() * 1_000),
                "kr": {"status": "ready"},
            },
            event_time=NOW,
        )
        for timeframe in ("1h", "4h", "1d")
    )
    feed = PerptapeFeedSnapshot(
        contract_version="breakouts-v1",
        generated_at=NOW,
        fetched_at=NOW,
        next_allowed_at=NOW,
        candidates=candidates,
    )
    worker = RuntimeSyncWorker(
        settings=settings,
        database=database,
        perptape=client,
        binance=BinanceReader(),  # type: ignore[arg-type]
        hyperliquid=HyperliquidReader(),  # type: ignore[arg-type]
        notilt=NoTiltReader(),  # type: ignore[arg-type]
        notilt_valuator=NoTiltUsdValuator(),
        clock=lambda: NOW,
    )

    assert worker._create_resonance_proposals(perptape_actor, feed, now=NOW) == 1
    assert worker._create_resonance_proposals(perptape_actor, feed, now=NOW) == 0
    proposals = TradingQueries(database).list_proposals(admin, now=NOW)
    assert len(proposals) == 1
    assert proposals[0]["source"] == "SYSTEM"
    assert proposals[0]["status"] == "PENDING_REVIEW"
    detail = TradingQueries(database).proposal_detail(
        admin,
        UUID(proposals[0]["proposal_id"]),
        now=NOW,
    )
    assert detail["frozen_payload"]["details"]["resonance_timeframes"] == [
        "1h",
        "4h",
        "1d",
    ]
    assert detail["frozen_payload"]["details"]["allow_auto_add"] is False
    assert detail["frozen_payload"]["details"]["default_config_version"] == 1
    assert detail["frozen_payload"]["details"]["rationale"] == (
        "创建提案时，Perptape 中同一精确合约、同一方向在 1h、4h、1d 同时突破。"  # noqa: RUF001
        "automatic resonance proposal awaiting independent review "
        "来源与参数已冻结，仅创建待审核提案；不会自动授权或下单。"  # noqa: RUF001
    )

    legacy_duplicate_id = service.create_proposal(
        actor_id=perptape_actor,
        source=ProposalSource.SYSTEM,
        risk_tier=RiskTier.MEDIUM,
        account_id="acct-1",
        venue="BINANCE",
        instrument_id=instrument_id,
        direction=Direction.LONG,
        quantity=Decimal("0.001"),
        max_risk=Decimal("1"),
        expires_at=NOW + timedelta(hours=8),
        idempotency_key="legacy-refresh-duplicate",
        strategy_id="perptape",
        strategy_version="legacy-refresh-identity",
        environment=ExecutionEnvironment.LIVE,
        source_candidate_id="ptr_legacy_refresh_duplicate",
        source_observed_at=NOW + timedelta(seconds=10),
        source_readiness="READY",
        details={"resonance_timeframes": ["1h", "4h", "1d"]},
        now=NOW + timedelta(seconds=10),
    )
    service.submit_proposal(
        legacy_duplicate_id,
        perptape_actor,
        now=NOW + timedelta(seconds=10),
    )
    refreshed_at = NOW + timedelta(seconds=30)
    refreshed_candidates = tuple(
        client.parse_stream_alert(
            {
                "ex": "BN",
                "s": "BTCUSDT",
                "cs": "BTCUSDT",
                "dir": "HH",
                "p": 100_100,
                "th": 99_000,
                "tf": timeframe,
                "t": int(refreshed_at.timestamp() * 1_000),
                "u": int(refreshed_at.timestamp() * 1_000),
                "kr": {"status": "ready"},
            },
            event_time=refreshed_at,
        )
        for timeframe in ("1h", "4h", "1d")
    )
    refreshed_feed = replace(
        feed,
        generated_at=refreshed_at,
        fetched_at=refreshed_at,
        candidates=refreshed_candidates,
    )
    assert (
        worker._create_resonance_proposals(
            perptape_actor,
            refreshed_feed,
            now=refreshed_at,
        )
        == 0
    )
    proposals_after_cleanup = TradingQueries(database).list_proposals(admin, now=refreshed_at)
    assert sum(item["status"] == "PENDING_REVIEW" for item in proposals_after_cleanup) == 1
    assert sum(item["status"] == "EXPIRED" for item in proposals_after_cleanup) == 1
    assert (
        TradingQueries(database).proposal_detail(
            admin,
            legacy_duplicate_id,
            now=refreshed_at,
        )["status"]
        == "EXPIRED"
    )

    stale = replace(
        feed,
        candidates=tuple(
            replace(item, observed_at=NOW - timedelta(minutes=5)) for item in candidates
        ),
    )
    assert worker._create_resonance_proposals(perptape_actor, stale, now=NOW) == 0

    later = NOW + timedelta(minutes=1)
    service.set_proposal_default_config(
        admin,
        "resonance-policy-v2",
        account_id="acct-1",
        risk_tier=RiskTier.LOW,
        notional=Decimal("80"),
        max_risk=Decimal("0.8"),
        invalidation_bps=150,
        expires_in_minutes=480,
        rationale="four timeframe policy for new signals only",
        auto_proposal_enabled=True,
        auto_proposal_min_timeframes=4,
        now=later,
    )
    four_candidates = tuple(
        client.parse_stream_alert(
            {
                "ex": "BN",
                "s": "BTCUSDT",
                "cs": "BTCUSDT",
                "dir": "HH",
                "p": 101_000,
                "th": 100_000,
                "tf": timeframe,
                "t": int(later.timestamp() * 1_000),
                "u": int(later.timestamp() * 1_000),
                "kr": {"status": "ready"},
            },
            event_time=later,
        )
        for timeframe in ("1h", "4h", "1d", "1w")
    )
    three_only = replace(feed, generated_at=later, fetched_at=later, candidates=four_candidates[:3])
    four_feed = replace(feed, generated_at=later, fetched_at=later, candidates=four_candidates)
    assert worker._create_resonance_proposals(perptape_actor, three_only, now=later) == 0
    assert worker._create_resonance_proposals(perptape_actor, four_feed, now=later) == 0
    service.review_proposal(
        UUID(detail["proposal_id"]),
        admin,
        ReviewDecision.REJECT,
        "superseded by a newly qualified signal",
        expected_version=detail["version"],
        now=later,
    )
    after_review = later + timedelta(seconds=1)
    assert (
        worker._create_resonance_proposals(
            perptape_actor,
            four_feed,
            now=after_review,
        )
        == 1
    )
    assert (
        worker._create_resonance_proposals(
            perptape_actor,
            four_feed,
            now=after_review,
        )
        == 0
    )
    proposals = TradingQueries(database).list_proposals(admin, now=after_review)
    new_proposal_id = next(
        item["proposal_id"]
        for item in proposals
        if item["proposal_id"] != detail["proposal_id"] and item["status"] == "PENDING_REVIEW"
    )
    new_detail = TradingQueries(database).proposal_detail(
        admin,
        UUID(new_proposal_id),
        now=later,
    )
    assert new_detail["frozen_payload"]["details"]["resonance_threshold"] == 4
    assert new_detail["frozen_payload"]["details"]["default_config_version"] == 2


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
        load_snapshot=lambda: queries.perptape_feed(perptape_actor),
        record_snapshot=lambda feed, now, base_snapshot: service.record_perptape_feed(
            perptape_actor,
            feed,
            now=now,
            base_snapshot=base_snapshot,
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

    persisted = queries.perptape_feed(perptape_actor)
    assert persisted is not None
    assert len(persisted.candidates) == 1
    assert persisted.candidates[0].symbol == "ETHUSDT"
    assert persisted.candidates[0].readiness == "READY"
    assert stream.stats.alerts_applied == 1
    assert len(https_calls) == 1


def test_database_bound_team_websocket_updates_exact_feed_and_health(
    database: Database,
) -> None:
    encryption_key = runtime_encryption_key()
    service = TradingService(database, credential_encryption_key=encryption_key)
    admin = service.bootstrap_admin("bound-stream-admin", now=NOW)
    current = service.signal_source_status(admin)["source"]
    service.configure_signal_source(
        actor_id=admin,
        mode=SignalSourceMode.PERPTAPE,
        secret="bound-team-stream-secret",  # noqa: S106
        enabled=True,
        webhook_max_age_seconds=300,
        expected_version=0 if current is None else int(current["version"]),
        idempotency_key="configure-bound-team-stream",
        now=NOW,
    )
    binding = service.perptape_runtime_bindings()[0]
    event_time = NOW + timedelta(seconds=1)
    alert_applied = threading.Event()

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
                    json.dumps(
                        {
                            "e": "alert",
                            "seq": 2,
                            "E": int(event_time.timestamp() * 1_000),
                            "d": {
                                "id": "bound-team-alert-1",
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
                    ),
                ]
            )

        def send(self, _message: str) -> None:
            return None

        def recv(self, timeout: float | None = None) -> str | bytes:
            assert timeout == 1.0
            if self.messages:
                return self.messages.popleft()
            alert_applied.set()
            threading.Event().wait(0.01)
            raise TimeoutError

    @contextmanager
    def connector(
        url: str,
        headers: dict[str, str],
        timeout: float,
    ) -> Iterator[PerptapeSocket]:
        assert url == "wss://perptape.com/ws/v1/alerts"
        assert headers["x-api-key"] == "bound-team-stream-secret"
        assert timeout == 5
        yield Socket()

    settings = Settings(
        database_url=str(database.engine.url),
        credential_encryption_key=encryption_key,
        runtime_sync_enabled=True,
        perptape_websocket_enabled=True,
        perptape_timeout_seconds=5,
        perptape_websocket_reconnect_initial_seconds=0.1,
        perptape_websocket_reconnect_max_seconds=1,
        _env_file=None,
    )
    built_streams: list[PerptapeStreamWorker] = []

    class BoundWorker(RuntimeSyncWorker):
        def build_bound_perptape_stream(
            self,
            prepared: Any,
        ) -> PerptapeStreamWorker:
            stream = super().build_bound_perptape_stream(prepared)
            stream._connector = connector
            built_streams.append(stream)
            return stream

    def worker_factory(scoped: Settings, scoped_database: Database) -> RuntimeSyncWorker:
        return BoundWorker(
            settings=scoped,
            database=scoped_database,
            perptape=PerptapeClient(
                base_url="https://perptape.com",
                api_key=scoped.perptape_api_key,
                contract_version="breakouts-v1",
                cache_ttl=timedelta(minutes=1),
                timeout_seconds=5,
                fetcher=lambda _url, _headers, _timeout: {
                    "type": "breakouts",
                    "generatedAt": int(NOW.timestamp() * 1_000),
                    "data": [],
                },
            ),
            binance=BinanceReader(),  # type: ignore[arg-type]
            hyperliquid=HyperliquidReader(),  # type: ignore[arg-type]
            notilt=NoTiltReader(),  # type: ignore[arg-type]
            notilt_valuator=NoTiltUsdValuator(),
            clock=lambda: event_time + timedelta(seconds=1),
        )

    supervisor = RuntimeBindingSupervisor(
        settings=settings,
        database=database,
        clock=lambda: event_time + timedelta(seconds=1),
        worker_factory=worker_factory,
    )
    try:
        supervisor._reconcile_perptape_streams((binding,), now=NOW)
        assert alert_applied.wait(2)
        assert built_streams[0].stats.alerts_applied == 1
        supervisor._reconcile_perptape_streams(
            (binding,),
            now=event_time + timedelta(seconds=2),
        )

        feed = TradingQueries(database).perptape_feed(admin)
        assert feed is not None
        assert [item.symbol for item in feed.candidates] == ["ETHUSDT"]
        health = TradingQueries(database).runtime_source_health(
            admin,
            "PERPTAPE_WEBSOCKET",
        )
        assert health is not None
        assert health["status"] == "SUCCESS"
        assert health["items_observed"] == 2
        assert "bound-team-stream-secret" not in repr(health)
    finally:
        supervisor._shutdown_perptape_streams()

    assert supervisor.dependencies_in_use is False
