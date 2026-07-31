from __future__ import annotations

import json
import logging
import threading
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import trading_control_plane.perptape_stream as perptape_stream_module
from trading_control_plane.domain import DomainRejected
from trading_control_plane.perptape import (
    PerptapeClient,
    PerptapeFeedSnapshot,
    PerptapeRateLimited,
)
from trading_control_plane.perptape_stream import PerptapeSocket, PerptapeStreamWorker

NOW = datetime(2026, 7, 31, 8, tzinfo=UTC)
API_KEY = "test-perptape-platform-key"


def candidate_payload(
    *,
    triggered_at: datetime = NOW,
    updated_at: datetime | None = None,
    symbol: str = "BTCUSDT",
    direction: str = "HH",
) -> dict[str, Any]:
    updated_at = updated_at or triggered_at
    return {
        "exchange": "BN",
        "symbol": symbol,
        "canonicalSymbol": symbol,
        "direction": direction,
        "timeframe": "1h",
        "price": 100_000,
        "threshold": 99_000,
        "volume24hQuote": 1_000_000,
        "openInterestQuote": 500_000,
        "updatedAt": int(updated_at.timestamp() * 1_000),
        "triggeredAt": int(triggered_at.timestamp() * 1_000),
        "klineReadiness": {"status": "ready"},
    }


def response(*candidates: dict[str, Any], generated_at: datetime = NOW) -> dict[str, Any]:
    return {
        "type": "breakouts",
        "generatedAt": int(generated_at.timestamp() * 1_000),
        "data": list(candidates),
    }


def message(
    event_type: str,
    *,
    sequence: int,
    event_time: datetime,
    payload: dict[str, Any] | None = None,
) -> str:
    value: dict[str, Any] = {
        "e": event_type,
        "seq": sequence,
        "E": int(event_time.timestamp() * 1_000),
    }
    if payload is not None:
        value["d"] = payload
    return json.dumps(value)


def short_alert(
    alert_id: str,
    symbol: str,
    event_time: datetime,
) -> dict[str, Any]:
    return {
        "id": alert_id,
        "ex": "BN",
        "s": symbol,
        "dir": "HH",
        "p": 4_000,
        "tf": "1h",
        "t": int(event_time.timestamp() * 1_000),
        "u": int(event_time.timestamp() * 1_000),
        "kr": {"status": "ready"},
        "vq24": 20_000,
        "oi": 10_000,
    }


class SnapshotStore:
    def __init__(self) -> None:
        self.current: PerptapeFeedSnapshot | None = None
        self.writes: list[PerptapeFeedSnapshot] = []

    def load(self) -> PerptapeFeedSnapshot | None:
        return self.current

    def record(self, feed: PerptapeFeedSnapshot, now: datetime) -> None:
        assert now == feed.fetched_at
        self.current = feed
        self.writes.append(feed)


class FakeSocket:
    def __init__(self, actions: list[object]) -> None:
        self.actions = deque(actions)
        self.sent: list[str] = []

    def send(self, message_value: str) -> None:
        self.sent.append(message_value)

    def recv(self, timeout: float | None = None) -> str | bytes:
        assert timeout == 1.0
        action = self.actions.popleft()
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return action()
        assert isinstance(action, (str, bytes))
        return action


class RecordingConnector:
    def __init__(self, sockets: list[FakeSocket | BaseException]) -> None:
        self.sockets = deque(sockets)
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def __call__(
        self,
        url: str,
        headers: dict[str, str],
        timeout: float,
    ) -> AbstractContextManager[PerptapeSocket]:
        self.calls.append((url, headers, timeout))
        value = self.sockets.popleft()
        if isinstance(value, BaseException):
            raise value

        @contextmanager
        def connected() -> Iterator[PerptapeSocket]:
            yield value

        return connected()


class RecordingStopEvent:
    def __init__(self) -> None:
        self.stopped = False
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return self.stopped

    def set(self) -> None:
        self.stopped = True

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return self.stopped


def worker(
    *,
    responses: list[dict[str, Any] | DomainRejected | Callable[[], dict[str, Any]]],
    connector: RecordingConnector,
    store: SnapshotStore,
    clock: Any = lambda: NOW,
    monotonic: Any = lambda: 0.0,
    max_reconnect_attempts: int = 3,
    reconciliation_interval_seconds: float = 300,
    heartbeat_timeout_seconds: float = 45,
    cache_ttl_seconds: float = 60,
) -> tuple[PerptapeStreamWorker, list[str]]:
    calls: list[str] = []
    values = deque(responses)

    def fetch(url: str, _headers: dict[str, str], _timeout: float) -> dict[str, Any]:
        calls.append(url)
        value = values.popleft()
        if isinstance(value, DomainRejected):
            raise value
        if callable(value):
            return value()
        return value

    client = PerptapeClient(
        base_url="https://perptape.com",
        api_key=API_KEY,
        contract_version="breakouts-v1",
        cache_ttl=timedelta(seconds=cache_ttl_seconds),
        fetcher=fetch,
    )
    return (
        PerptapeStreamWorker(
            client=client,
            websocket_url="wss://perptape.com/ws/v1/alerts",
            api_key=API_KEY,
            contract_version="breakouts-v1",
            load_snapshot=store.load,
            record_snapshot=store.record,
            timeout_seconds=5,
            heartbeat_timeout_seconds=heartbeat_timeout_seconds,
            reconciliation_interval_seconds=reconciliation_interval_seconds,
            reconnect_initial_seconds=1,
            reconnect_max_seconds=8,
            max_reconnect_attempts=max_reconnect_attempts,
            connector=connector,
            clock=clock,
            monotonic=monotonic,
        ),
        calls,
    )


def test_stream_deduplicates_alert_and_uses_https_to_complete_missing_fields() -> None:
    stop = threading.Event()
    complete_time = NOW + timedelta(seconds=2)
    first_alert = {
        "id": "alert-1",
        "ex": "BN",
        "s": "ETHUSDT",
        "cs": "ETHUSDT",
        "dir": "HH",
        "p": 4_000,
        "th": 3_900,
        "tf": "1h",
        "t": int((NOW + timedelta(seconds=1)).timestamp() * 1_000),
        "u": int((NOW + timedelta(seconds=1)).timestamp() * 1_000),
        "kr": {"status": "ready"},
        "vq24": 20_000,
        "oi": 10_000,
    }
    incomplete_alert = {
        "id": "alert-2",
        "ex": "BN",
        "s": "SOLUSDT",
        "cs": "SOLUSDT",
        "dir": "LL",
        "p": 150,
        "th": 151,
        "tf": "1h",
        "t": int(complete_time.timestamp() * 1_000),
    }

    def stop_receiving() -> str:
        stop.set()
        raise TimeoutError

    socket = FakeSocket(
        [
            message("hello", sequence=10, event_time=NOW),
            message(
                "alert",
                sequence=11,
                event_time=NOW + timedelta(seconds=1),
                payload=first_alert,
            ),
            message(
                "alert",
                sequence=11,
                event_time=NOW + timedelta(seconds=1),
                payload=first_alert,
            ),
            message(
                "alert",
                sequence=12,
                event_time=complete_time,
                payload=incomplete_alert,
            ),
            stop_receiving,
        ]
    )
    connector = RecordingConnector([socket])
    store = SnapshotStore()
    stream, https_calls = worker(
        responses=[
            response(candidate_payload()),
            response(
                candidate_payload(),
                candidate_payload(
                    triggered_at=NOW + timedelta(seconds=1),
                    updated_at=NOW + timedelta(seconds=1),
                    symbol="ETHUSDT",
                ),
                candidate_payload(
                    triggered_at=complete_time,
                    updated_at=complete_time,
                    symbol="SOLUSDT",
                    direction="LL",
                ),
                generated_at=complete_time,
            ),
        ],
        connector=connector,
        store=store,
        clock=lambda: complete_time,
        cache_ttl_seconds=0,
    )

    stream.run_forever(stop)

    assert len(https_calls) == 2
    assert stream.stats.alerts_applied == 2
    assert stream.stats.duplicates_dropped == 1
    assert stream.stats.https_backfills == 1
    assert store.current is not None
    sol = next(item for item in store.current.candidates if item.symbol == "SOLUSDT")
    assert sol.readiness == "READY"
    assert sol.quote_volume == 1_000_000
    assert connector.calls[0][0] == "wss://perptape.com/ws/v1/alerts"
    assert "apiKey" not in connector.calls[0][0]
    assert connector.calls[0][1]["x-api-key"] == API_KEY
    assert API_KEY not in socket.sent[0]


def test_sequence_gap_and_out_of_order_message_each_trigger_https_backfill() -> None:
    stop = threading.Event()

    def stop_receiving() -> str:
        stop.set()
        raise TimeoutError

    socket = FakeSocket(
        [
            message("hello", sequence=1, event_time=NOW),
            message("heartbeat", sequence=3, event_time=NOW + timedelta(seconds=15)),
            message("heartbeat", sequence=2, event_time=NOW + timedelta(seconds=10)),
            message("heartbeat", sequence=4, event_time=NOW + timedelta(seconds=61)),
            stop_receiving,
        ]
    )
    connector = RecordingConnector([socket])
    store = SnapshotStore()
    stream, https_calls = worker(
        responses=[response(), response(), response(), response()],
        connector=connector,
        store=store,
        clock=lambda: NOW + timedelta(seconds=61),
        cache_ttl_seconds=0,
    )

    stream.run_forever(stop)

    assert len(https_calls) == 4
    assert stream.stats.gaps_detected == 3
    assert stream.stats.https_backfills == 3


def test_failed_https_completion_degrades_snapshot_and_keeps_alert_incomplete() -> None:
    stop = threading.Event()
    event_time = NOW + timedelta(seconds=1)
    incomplete_alert = {
        "id": "alert-incomplete",
        "ex": "BN",
        "s": "ETHUSDT",
        "dir": "HH",
        "p": 4_000,
        "tf": "1h",
        "t": int(event_time.timestamp() * 1_000),
    }

    def stop_receiving() -> str:
        stop.set()
        raise TimeoutError

    socket = FakeSocket(
        [
            message("hello", sequence=1, event_time=NOW),
            message(
                "alert",
                sequence=2,
                event_time=event_time,
                payload=incomplete_alert,
            ),
            stop_receiving,
        ]
    )
    connector = RecordingConnector([socket])
    store = SnapshotStore()
    stream, https_calls = worker(
        responses=[
            response(candidate_payload()),
            DomainRejected("PERPTAPE_RATE_LIMITED", "rate limited"),
        ],
        connector=connector,
        store=store,
        clock=lambda: event_time,
        cache_ttl_seconds=0,
    )

    stream.run_forever(stop)

    assert len(https_calls) == 2
    assert stream.stats.degraded_writes == 1
    assert store.current is not None
    assert all(item.readiness != "READY" for item in store.current.candidates)
    eth = next(item for item in store.current.candidates if item.symbol == "ETHUSDT")
    assert eth.readiness == "INCOMPLETE"


def test_missing_canonical_symbol_and_threshold_are_completed_by_https() -> None:
    stop = threading.Event()
    event_time = NOW + timedelta(seconds=1)
    short_alert = {
        "id": "alert-short",
        "ex": "BN",
        "s": "ETHUSDT",
        "dir": "HH",
        "p": 4_000,
        "tf": "1h",
        "t": int(event_time.timestamp() * 1_000),
        "u": int(event_time.timestamp() * 1_000),
        "kr": {"status": "ready"},
        "vq24": 20_000,
        "oi": 10_000,
    }
    completed = candidate_payload(
        triggered_at=event_time,
        updated_at=event_time,
        symbol="ETHUSDT",
    )
    completed["canonicalSymbol"] = "ETH"

    def stop_receiving() -> str:
        stop.set()
        raise TimeoutError

    connector = RecordingConnector(
        [
            FakeSocket(
                [
                    message("hello", sequence=1, event_time=NOW),
                    message(
                        "alert",
                        sequence=2,
                        event_time=event_time,
                        payload=short_alert,
                    ),
                    stop_receiving,
                ]
            )
        ]
    )
    store = SnapshotStore()
    stream, https_calls = worker(
        responses=[
            response(),
            response(completed, generated_at=event_time),
        ],
        connector=connector,
        store=store,
        clock=lambda: event_time,
        cache_ttl_seconds=0,
    )

    stream.run_forever(stop)

    assert len(https_calls) == 2
    assert stream.stats.https_backfills == 1
    assert store.current is not None
    candidate = next(item for item in store.current.candidates if item.symbol == "ETHUSDT")
    assert candidate.canonical_symbol == "ETH"
    assert candidate.threshold == 99_000
    assert candidate.readiness == "READY"


def test_future_next_allowed_blocks_gap_and_short_field_completion_until_stop() -> None:
    stop = threading.Event()
    event_time = NOW + timedelta(seconds=1)
    initial = response(candidate_payload())
    initial["rateLimit"] = {"nextAllowedAt": int((NOW + timedelta(minutes=2)).timestamp() * 1_000)}
    short_alert = {
        "id": "alert-future-window",
        "ex": "BN",
        "s": "ETHUSDT",
        "dir": "HH",
        "p": 4_000,
        "tf": "1h",
        "t": int(event_time.timestamp() * 1_000),
        "u": int(event_time.timestamp() * 1_000),
        "kr": {"status": "ready"},
        "vq24": 20_000,
        "oi": 10_000,
    }

    def stop_receiving() -> str:
        stop.set()
        raise TimeoutError

    connector = RecordingConnector(
        [
            FakeSocket(
                [
                    message("hello", sequence=1, event_time=NOW),
                    message(
                        "alert",
                        sequence=2,
                        event_time=event_time,
                        payload=short_alert,
                    ),
                    message(
                        "heartbeat",
                        sequence=4,
                        event_time=event_time + timedelta(seconds=1),
                    ),
                    stop_receiving,
                ]
            )
        ]
    )
    store = SnapshotStore()
    stream, https_calls = worker(
        responses=[initial],
        connector=connector,
        store=store,
        clock=lambda: event_time,
    )

    stream.run_forever(stop)

    assert len(https_calls) == 1
    assert stream.stats.gaps_detected == 1
    assert stream.stats.https_backfills == 0
    assert store.current is not None
    assert all(item.readiness != "READY" for item in store.current.candidates)
    eth = next(item for item in store.current.candidates if item.symbol == "ETHUSDT")
    assert eth.readiness == "INCOMPLETE"


def test_deferred_short_field_completion_runs_when_server_window_opens() -> None:
    stop = threading.Event()
    clock_value = NOW + timedelta(seconds=1)
    event_time = clock_value
    initial = response()
    initial["rateLimit"] = {"nextAllowedAt": int((NOW + timedelta(minutes=2)).timestamp() * 1_000)}
    short_alert = {
        "id": "alert-deferred",
        "ex": "BN",
        "s": "ETHUSDT",
        "dir": "HH",
        "p": 4_000,
        "tf": "1h",
        "t": int(event_time.timestamp() * 1_000),
        "u": int(event_time.timestamp() * 1_000),
        "kr": {"status": "ready"},
        "vq24": 20_000,
        "oi": 10_000,
    }
    completed = candidate_payload(
        triggered_at=event_time,
        updated_at=event_time,
        symbol="ETHUSDT",
    )
    completed["canonicalSymbol"] = "ETH"

    def advance_past_window() -> str:
        nonlocal clock_value
        clock_value = NOW + timedelta(minutes=2, seconds=1)
        raise TimeoutError

    def stop_receiving() -> str:
        stop.set()
        raise TimeoutError

    connector = RecordingConnector(
        [
            FakeSocket(
                [
                    message("hello", sequence=1, event_time=NOW),
                    message(
                        "alert",
                        sequence=2,
                        event_time=event_time,
                        payload=short_alert,
                    ),
                    advance_past_window,
                    stop_receiving,
                ]
            )
        ]
    )
    store = SnapshotStore()
    stream, https_calls = worker(
        responses=[
            initial,
            response(completed, generated_at=event_time),
        ],
        connector=connector,
        store=store,
        clock=lambda: clock_value,
        cache_ttl_seconds=0,
    )

    stream.run_forever(stop)

    assert len(https_calls) == 2
    assert stream.stats.https_backfills == 1
    assert store.current is not None
    candidate = next(item for item in store.current.candidates if item.symbol == "ETHUSDT")
    assert candidate.canonical_symbol == "ETH"
    assert candidate.readiness == "READY"


def test_periodic_reconciliation_does_not_call_https_inside_server_window() -> None:
    stop = threading.Event()
    monotonic_value = 0.0
    initial = response(candidate_payload())
    initial["rateLimit"] = {"nextAllowedAt": int((NOW + timedelta(minutes=2)).timestamp() * 1_000)}

    def monotonic() -> float:
        return monotonic_value

    def advance_inside_window() -> str:
        nonlocal monotonic_value
        monotonic_value = 121.0
        raise TimeoutError

    def stop_receiving() -> str:
        stop.set()
        raise TimeoutError

    connector = RecordingConnector(
        [
            FakeSocket(
                [
                    message("hello", sequence=1, event_time=NOW),
                    advance_inside_window,
                    stop_receiving,
                ]
            )
        ]
    )
    store = SnapshotStore()
    stream, https_calls = worker(
        responses=[initial],
        connector=connector,
        store=store,
        clock=lambda: NOW + timedelta(seconds=monotonic_value / 2),
        monotonic=monotonic,
        reconciliation_interval_seconds=60,
        heartbeat_timeout_seconds=300,
    )

    stream.run_forever(stop)

    assert len(https_calls) == 1
    assert stream.stats.https_reconciliations == 1
    assert store.current is not None
    assert all(item.readiness != "READY" for item in store.current.candidates)


def test_periodic_full_reconciliation_runs_while_connection_is_quiet() -> None:
    stop = threading.Event()
    monotonic_value = 0.0

    def monotonic() -> float:
        return monotonic_value

    def advance() -> str:
        nonlocal monotonic_value
        monotonic_value = 61.0
        raise TimeoutError

    def stop_receiving() -> str:
        stop.set()
        raise TimeoutError

    socket = FakeSocket(
        [
            message("hello", sequence=1, event_time=NOW),
            advance,
            stop_receiving,
        ]
    )
    connector = RecordingConnector([socket])
    store = SnapshotStore()
    stream, https_calls = worker(
        responses=[response(), response()],
        connector=connector,
        store=store,
        clock=lambda: NOW + timedelta(seconds=monotonic_value),
        monotonic=monotonic,
        reconciliation_interval_seconds=60,
        heartbeat_timeout_seconds=120,
    )

    stream.run_forever(stop)

    assert len(https_calls) == 2
    assert stream.stats.https_reconciliations == 2
    assert stream.stats.https_backfills == 0


def test_reconnect_uses_bounded_exponential_backoff_and_wait_is_stoppable() -> None:
    stop = RecordingStopEvent()

    def stop_receiving() -> str:
        stop.set()
        raise TimeoutError

    socket = FakeSocket(
        [
            message("hello", sequence=1, event_time=NOW),
            message("heartbeat", sequence=2, event_time=NOW + timedelta(seconds=15)),
            stop_receiving,
        ]
    )
    disconnected = FakeSocket(
        [
            message("hello", sequence=1, event_time=NOW),
            message("heartbeat", sequence=2, event_time=NOW + timedelta(seconds=15)),
            DomainRejected("PERPTAPE_STREAM_UNAVAILABLE", "disconnected"),
        ]
    )
    connector = RecordingConnector(
        [
            disconnected,
            DomainRejected("PERPTAPE_STREAM_UNAVAILABLE", "unavailable"),
            socket,
        ]
    )
    store = SnapshotStore()
    stream, _https_calls = worker(
        responses=[response(), response(), response()],
        connector=connector,
        store=store,
    )

    stream.run_forever(stop)

    assert stop.waits == [1, 2]
    assert len(connector.calls) == 3
    assert stream.fatal_error_code is None


def test_failed_startup_snapshot_retries_once_per_backoff_cycle() -> None:
    stop = RecordingStopEvent()

    def stop_receiving() -> str:
        stop.set()
        raise TimeoutError

    socket = FakeSocket(
        [
            message("hello", sequence=1, event_time=NOW),
            stop_receiving,
        ]
    )
    connector = RecordingConnector([socket])
    store = SnapshotStore()
    stream, https_calls = worker(
        responses=[
            DomainRejected("PERPTAPE_UNAVAILABLE", "unavailable"),
            response(candidate_payload()),
        ],
        connector=connector,
        store=store,
    )

    stream.run_forever(stop)

    assert len(https_calls) == 2
    assert stop.waits == [1]
    assert len(connector.calls) == 1


def test_startup_429_waits_for_server_window_and_stop_interrupts_wait() -> None:
    class StopOnFirstWait(RecordingStopEvent):
        def wait(self, timeout: float) -> bool:
            self.waits.append(timeout)
            self.set()
            return True

    stop = StopOnFirstWait()
    connector = RecordingConnector([])
    store = SnapshotStore()
    stream, https_calls = worker(
        responses=[
            PerptapeRateLimited(
                NOW + timedelta(minutes=2),
                is_remote=True,
            )
        ],
        connector=connector,
        store=store,
    )

    stream.run_forever(stop)

    assert len(https_calls) == 1
    assert stop.waits == [120]
    assert connector.calls == []
    assert stream.fatal_error_code is None


def test_startup_real_429_stops_at_the_shared_attempt_limit() -> None:
    clock_value = NOW

    class AdvancingStopEvent(RecordingStopEvent):
        def wait(self, timeout: float) -> bool:
            nonlocal clock_value
            self.waits.append(timeout)
            clock_value += timedelta(seconds=timeout)
            return False

    stop = AdvancingStopEvent()
    connector = RecordingConnector([])
    store = SnapshotStore()
    stream, https_calls = worker(
        responses=[
            PerptapeRateLimited(
                NOW + timedelta(seconds=1),
                is_remote=True,
            ),
            PerptapeRateLimited(
                NOW + timedelta(seconds=2),
                is_remote=True,
            ),
        ],
        connector=connector,
        store=store,
        clock=lambda: clock_value,
        max_reconnect_attempts=2,
    )

    stream.run_forever(stop)

    assert len(https_calls) == 2
    assert stop.waits == [1]
    assert stop.stopped is True
    assert connector.calls == []
    assert stream.fatal_error_code == "PERPTAPE_RATE_LIMITED"


def test_healthy_stream_real_429_stops_after_two_network_responses() -> None:
    stop = threading.Event()
    clock_value = NOW + timedelta(seconds=1)
    event_time = clock_value
    real_429_calls = 0

    def remote_rate_limit(deadline: datetime) -> Callable[[], dict[str, Any]]:
        def fail() -> dict[str, Any]:
            nonlocal real_429_calls
            real_429_calls += 1
            raise PerptapeRateLimited(deadline, is_remote=True)

        return fail

    def advance_to_second_window() -> str:
        nonlocal clock_value
        clock_value = NOW + timedelta(seconds=11)
        raise TimeoutError

    socket = FakeSocket(
        [
            message("hello", sequence=1, event_time=NOW),
            message(
                "alert",
                sequence=2,
                event_time=event_time,
                payload=short_alert("alert-rate-1", "ETHUSDT", event_time),
            ),
            message(
                "heartbeat",
                sequence=3,
                event_time=NOW + timedelta(seconds=2),
            ),
            advance_to_second_window,
        ]
    )
    connector = RecordingConnector([socket])
    store = SnapshotStore()
    stream, https_calls = worker(
        responses=[
            response(),
            remote_rate_limit(NOW + timedelta(seconds=10)),
            remote_rate_limit(NOW + timedelta(seconds=20)),
        ],
        connector=connector,
        store=store,
        clock=lambda: clock_value,
        cache_ttl_seconds=0,
        max_reconnect_attempts=2,
    )

    stream.run_forever(stop)

    assert len(https_calls) == 3
    assert real_429_calls == 2
    assert stream.fatal_error_code == "PERPTAPE_RATE_LIMITED"
    assert stop.is_set() is True
    assert store.current is not None
    assert all(candidate.readiness != "READY" for candidate in store.current.candidates)


def test_local_cooldown_does_not_call_https_or_consume_attempts() -> None:
    stop = threading.Event()
    event_time = NOW + timedelta(seconds=1)

    def stop_receiving() -> str:
        stop.set()
        raise TimeoutError

    socket = FakeSocket(
        [
            message("hello", sequence=1, event_time=NOW),
            message(
                "alert",
                sequence=2,
                event_time=event_time,
                payload=short_alert("alert-local-window", "ETHUSDT", event_time),
            ),
            message(
                "heartbeat",
                sequence=4,
                event_time=event_time + timedelta(seconds=1),
            ),
            stop_receiving,
        ]
    )
    connector = RecordingConnector([socket])
    store = SnapshotStore()
    store.current = PerptapeFeedSnapshot(
        contract_version="breakouts-v1",
        generated_at=NOW,
        fetched_at=NOW,
        next_allowed_at=NOW + timedelta(minutes=2),
        candidates=(),
    )
    stream, https_calls = worker(
        responses=[],
        connector=connector,
        store=store,
        clock=lambda: event_time,
        max_reconnect_attempts=2,
    )

    stream.run_forever(stop)

    assert https_calls == []
    assert stream._consecutive_remote_rate_limits == 0
    assert stream.fatal_error_code is None
    assert store.current is not None
    assert all(candidate.readiness != "READY" for candidate in store.current.candidates)


def test_successful_backfill_resets_rate_limit_series_before_new_failures() -> None:
    stop = threading.Event()
    clock_value = NOW + timedelta(seconds=1)
    first_event_time = clock_value
    second_event_time = NOW + timedelta(seconds=12)
    real_429_calls = 0

    def remote_rate_limit(deadline: datetime) -> Callable[[], dict[str, Any]]:
        def fail() -> dict[str, Any]:
            nonlocal real_429_calls
            real_429_calls += 1
            raise PerptapeRateLimited(deadline, is_remote=True)

        return fail

    completed = candidate_payload(
        triggered_at=first_event_time,
        updated_at=first_event_time,
        symbol="ETHUSDT",
    )
    completed["canonicalSymbol"] = "ETH"

    def advance_to_success_window() -> str:
        nonlocal clock_value
        clock_value = NOW + timedelta(seconds=11)
        raise TimeoutError

    def advance_to_final_window() -> str:
        nonlocal clock_value
        clock_value = NOW + timedelta(seconds=21)
        raise TimeoutError

    socket = FakeSocket(
        [
            message("hello", sequence=1, event_time=NOW),
            message(
                "alert",
                sequence=2,
                event_time=first_event_time,
                payload=short_alert(
                    "alert-reset-1",
                    "ETHUSDT",
                    first_event_time,
                ),
            ),
            advance_to_success_window,
            message(
                "alert",
                sequence=3,
                event_time=second_event_time,
                payload=short_alert(
                    "alert-reset-2",
                    "SOLUSDT",
                    second_event_time,
                ),
            ),
            message(
                "heartbeat",
                sequence=4,
                event_time=second_event_time + timedelta(seconds=1),
            ),
            advance_to_final_window,
        ]
    )
    connector = RecordingConnector([socket])
    store = SnapshotStore()
    stream, https_calls = worker(
        responses=[
            response(),
            remote_rate_limit(NOW + timedelta(seconds=10)),
            response(completed, generated_at=first_event_time),
            remote_rate_limit(NOW + timedelta(seconds=20)),
            remote_rate_limit(NOW + timedelta(seconds=30)),
        ],
        connector=connector,
        store=store,
        clock=lambda: clock_value,
        cache_ttl_seconds=0,
        max_reconnect_attempts=2,
    )

    stream.run_forever(stop)

    assert len(https_calls) == 5
    assert real_429_calls == 3
    assert stream.stats.https_backfills == 1
    assert stream._consecutive_remote_rate_limits == 2
    assert stream.fatal_error_code == "PERPTAPE_RATE_LIMITED"
    assert stop.is_set() is True


def test_invalid_messages_stop_after_bounded_failures_and_never_log_secret(
    caplog: Any,
) -> None:
    stop = RecordingStopEvent()
    sockets = [
        FakeSocket(
            [
                message("hello", sequence=1, event_time=NOW),
                "not-json",
            ]
        ),
        FakeSocket(
            [
                message("hello", sequence=1, event_time=NOW),
                json.dumps({"e": "unknown", "seq": 2, "E": int(NOW.timestamp() * 1_000)}),
            ]
        ),
    ]
    connector = RecordingConnector(sockets)
    store = SnapshotStore()
    stream, _https_calls = worker(
        responses=[
            response(candidate_payload()),
            response(candidate_payload()),
            response(candidate_payload()),
        ],
        connector=connector,
        store=store,
        max_reconnect_attempts=2,
    )

    with caplog.at_level(logging.WARNING):
        stream.run_forever(stop)

    assert stream.fatal_error_code == "PERPTAPE_STREAM_MESSAGE_INVALID"
    assert stop.waits == [1]
    assert API_KEY not in caplog.text
    assert store.current is not None
    assert all(item.readiness != "READY" for item in store.current.candidates)


def test_pre_stopped_worker_does_not_fetch_or_connect() -> None:
    stop = threading.Event()
    stop.set()
    connector = RecordingConnector([])
    store = SnapshotStore()
    stream, https_calls = worker(
        responses=[],
        connector=connector,
        store=store,
    )

    stream.run_forever(stop)

    assert https_calls == []
    assert connector.calls == []


def test_stop_during_startup_snapshot_returns_without_opening_websocket() -> None:
    stop = threading.Event()
    fetch_started = threading.Event()
    release_fetch = threading.Event()

    def delayed_startup_snapshot() -> dict[str, Any]:
        fetch_started.set()
        assert release_fetch.wait(timeout=1)
        return response(candidate_payload())

    connector = RecordingConnector([])
    store = SnapshotStore()
    stream, https_calls = worker(
        responses=[delayed_startup_snapshot],
        connector=connector,
        store=store,
    )
    stream_thread = threading.Thread(target=stream.run_forever, args=(stop,))
    stream_thread.start()
    assert fetch_started.wait(timeout=1)

    stop.set()
    release_fetch.set()
    stream_thread.join(timeout=1)

    assert not stream_thread.is_alive()
    assert len(https_calls) == 1
    assert connector.calls == []


def test_default_connector_applies_bounded_transport_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = FakeSocket([])
    captured: dict[str, Any] = {}

    @contextmanager
    def fake_connect(uri: str, **kwargs: Any) -> Iterator[PerptapeSocket]:
        captured["uri"] = uri
        captured.update(kwargs)
        yield socket

    monkeypatch.setattr(perptape_stream_module, "connect", fake_connect)
    headers = {"authorization": f"Bearer {API_KEY}", "x-api-key": API_KEY}

    with perptape_stream_module._default_connector(
        "wss://perptape.com/ws/v1/alerts",
        headers,
        5,
    ) as connected:
        assert connected is socket

    assert captured["uri"] == "wss://perptape.com/ws/v1/alerts"
    assert captured["additional_headers"] == headers
    assert captured["open_timeout"] == 5
    assert captured["close_timeout"] == 5
    assert captured["ping_interval"] == 15
    assert captured["ping_timeout"] == 5
    assert captured["max_size"] == 256 * 1024
    assert captured["max_queue"] == 32


def test_default_connector_maps_transport_failure_to_secret_free_domain_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(_uri: str, **_kwargs: Any) -> Any:
        raise OSError("transport detail")

    monkeypatch.setattr(perptape_stream_module, "connect", unavailable)

    with pytest.raises(DomainRejected) as rejected:
        with perptape_stream_module._default_connector(
            "wss://perptape.com/ws/v1/alerts",
            {"x-api-key": API_KEY},
            5,
        ):
            raise AssertionError("connection must fail before entering the context")

    assert rejected.value.code == "PERPTAPE_STREAM_UNAVAILABLE"
    assert API_KEY not in rejected.value.detail
