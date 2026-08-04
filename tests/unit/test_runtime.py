from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

import trading_control_plane.runtime as runtime_module
from trading_control_plane.config import Settings
from trading_control_plane.domain import DomainRejected, ReconciliationStatus
from trading_control_plane.perptape import PerptapeClient, PerptapeFeedSnapshot
from trading_control_plane.runtime import (
    RuntimeSyncReport,
    RuntimeSyncWorker,
    SourceSyncResult,
    build_runtime_worker,
    perptape_resonance_groups,
    perptape_resonance_signal_identity,
)


@pytest.mark.parametrize(
    "invalid_time",
    [
        datetime.max.replace(tzinfo=UTC),
        datetime(2026, 7, 31, 8),
        datetime.min.replace(tzinfo=timezone(timedelta(hours=23, minutes=59))),
        datetime.max.replace(tzinfo=timezone(-timedelta(hours=23, minutes=59))),
    ],
)
def test_runtime_rejects_invalid_cycle_clock_before_database_queries(
    invalid_time: datetime,
) -> None:
    queries = 0

    def principal(_username: str) -> None:
        nonlocal queries
        queries += 1

    worker: Any = object.__new__(RuntimeSyncWorker)
    worker.settings = Settings(
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        _env_file=None,
    )
    worker.clock = lambda: invalid_time
    worker.queries = SimpleNamespace(service_principal_by_username=principal)

    with pytest.raises(DomainRejected, match="PERPTAPE_DATETIME_INVALID"):
        worker.run_once()
    assert queries == 0


@pytest.mark.parametrize(
    "invalid_time",
    [
        datetime.max.replace(tzinfo=UTC),
        datetime(2026, 7, 31, 8),
        datetime.min.replace(tzinfo=timezone(timedelta(hours=23, minutes=59))),
    ],
)
def test_runtime_rejects_invalid_completion_clock_before_database_queries(
    invalid_time: datetime,
) -> None:
    queries = 0

    def principal(_username: str) -> None:
        nonlocal queries
        queries += 1

    values = iter([datetime(2026, 7, 31, tzinfo=UTC), invalid_time])
    worker: Any = object.__new__(RuntimeSyncWorker)
    worker.settings = Settings(
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        _env_file=None,
    )
    worker.clock = lambda: next(values)
    worker.queries = SimpleNamespace(service_principal_by_username=principal)

    with pytest.raises(DomainRejected, match="PERPTAPE_DATETIME_INVALID"):
        worker.run_once()
    assert queries == 0


def test_runtime_operational_headroom_accepts_exact_boundary_only() -> None:
    worker: Any = object.__new__(RuntimeSyncWorker)
    worker.settings = Settings(
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        perptape_api_key="fixture-key",
        perptape_cache_seconds=90,
        runtime_sync_interval_seconds=120,
        _env_file=None,
    )
    maximum = datetime.max.replace(tzinfo=UTC)
    once_boundary = maximum - timedelta(seconds=90)
    continuous_boundary = maximum - timedelta(seconds=120)

    assert worker._normalize_runtime_time(once_boundary, continuous=False) == once_boundary
    assert (
        worker._normalize_runtime_time(continuous_boundary, continuous=True) == continuous_boundary
    )
    with pytest.raises(DomainRejected, match="PERPTAPE_DATETIME_INVALID"):
        worker._normalize_runtime_time(
            once_boundary + timedelta(microseconds=1),
            continuous=False,
        )
    with pytest.raises(DomainRejected, match="PERPTAPE_DATETIME_INVALID"):
        worker._normalize_runtime_time(
            continuous_boundary + timedelta(microseconds=1),
            continuous=True,
        )


def test_runtime_readiness_requires_both_source_success_and_complete_capital() -> None:
    incomplete = RuntimeSyncReport(
        started_at="2026-07-31T00:00:00+00:00",
        completed_at="2026-07-31T00:00:01+00:00",
        sources={"PERPTAPE": SourceSyncResult("SUCCESS", items_observed=10)},
        net_worth={"complete": False, "issues": ["MISSING_LIVE_SOURCE:VAULT"]},
    )
    failed = RuntimeSyncReport(
        started_at="2026-07-31T00:00:00+00:00",
        completed_at="2026-07-31T00:00:01+00:00",
        sources={"PERPTAPE": SourceSyncResult("FAILED", error_code="PERPTAPE_UNAVAILABLE")},
        net_worth={"complete": True, "issues": []},
    )

    assert incomplete.successful is True
    assert incomplete.ready_for_new_risk is False
    assert incomplete.to_dict()["source_sync_successful"] is True
    assert incomplete.to_dict()["ready_for_new_risk"] is False
    assert failed.successful is False
    assert failed.ready_for_new_risk is False


def test_runtime_rate_limit_cooldown_skips_only_until_retry_deadline() -> None:
    now = datetime(2026, 8, 5, 1, 0, tzinfo=UTC)
    worker: Any = object.__new__(RuntimeSyncWorker)
    worker.queries = SimpleNamespace(
        runtime_source_health=lambda _source: {
            "error_code": "HYPERLIQUID_RATE_LIMITED",
            "retry_at": (now + timedelta(minutes=2)).isoformat(),
        }
    )

    cooldown = worker._rate_limit_cooldown("HYPERLIQUID", now=now)

    assert cooldown == SourceSyncResult(
        "SKIPPED",
        error_code="HYPERLIQUID_RATE_LIMITED_COOLDOWN",
    )
    assert worker._rate_limit_cooldown(
        "HYPERLIQUID", now=now + timedelta(minutes=2)
    ) is None


def test_runtime_rate_limit_cooldown_fails_open_to_a_real_probe_on_bad_metadata() -> None:
    worker: Any = object.__new__(RuntimeSyncWorker)
    worker.queries = SimpleNamespace(
        runtime_source_health=lambda _source: {
            "error_code": "HYPERLIQUID_RATE_LIMITED",
            "retry_at": "not-a-time",
        }
    )

    assert worker._rate_limit_cooldown(
        "HYPERLIQUID", now=datetime(2026, 8, 5, tzinfo=UTC)
    ) is None


def test_skipped_capital_sources_cannot_be_hidden_by_a_fresh_complete_snapshot() -> None:
    skipped = RuntimeSyncReport(
        started_at="2026-07-31T00:00:00+00:00",
        completed_at="2026-07-31T00:00:01+00:00",
        sources={
            "PERPTAPE": SourceSyncResult("SUCCESS", items_observed=10),
            "BINANCE": SourceSyncResult("SKIPPED"),
            "HYPERLIQUID": SourceSyncResult("SUCCESS", items_observed=1),
            "NOTILT": SourceSyncResult("SKIPPED"),
        },
        net_worth={"complete": True, "issues": [], "total": "100"},
    )

    assert skipped.successful is False
    assert skipped.capital_sources_successful is False
    assert skipped.ready_for_new_risk is False
    assert skipped.to_dict()["capital_sources_successful"] is False


def test_perptape_skip_is_separate_from_current_cycle_capital_readiness() -> None:
    report = RuntimeSyncReport(
        started_at="2026-07-31T00:00:00+00:00",
        completed_at="2026-07-31T00:00:01+00:00",
        sources={
            "PERPTAPE": SourceSyncResult("SKIPPED"),
            "BINANCE": SourceSyncResult("SUCCESS", items_observed=1),
            "HYPERLIQUID": SourceSyncResult("SUCCESS", items_observed=1),
            "NOTILT:42161": SourceSyncResult("SUCCESS", items_observed=1),
        },
        net_worth={"complete": True, "issues": [], "total": "100"},
    )

    assert report.successful is False
    assert report.capital_sources_successful is True
    assert report.ready_for_new_risk is True


def test_runtime_source_failure_isolated_as_a_safe_error_code() -> None:
    results: dict[str, SourceSyncResult] = {}

    def unavailable() -> int:
        raise DomainRejected("PERPTAPE_UNAVAILABLE", "secret-free public detail")

    RuntimeSyncWorker._attempt("PERPTAPE", unavailable, results)

    assert results == {
        "PERPTAPE": SourceSyncResult(
            "FAILED",
            error_code="PERPTAPE_UNAVAILABLE",
        )
    }


def test_perptape_resonance_requires_three_fresh_distinct_consistent_timeframes() -> None:
    now = datetime(2026, 8, 2, 12, tzinfo=UTC)
    client = PerptapeClient(
        base_url="https://perptape.com",
        api_key="fixture-key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(0),
        fetcher=lambda _url, _headers, _timeout: {},
    )

    def candidate(identifier: str, timeframe: str, observed_at: datetime = now):
        return client.parse_stream_alert(
            {
                "id": identifier,
                "ex": "BN",
                "s": "BTCUSDT",
                "cs": "BTCUSDT",
                "dir": "HH",
                "p": 100_000,
                "th": 99_000,
                "tf": timeframe,
                "t": int(observed_at.timestamp() * 1_000),
                "u": int(observed_at.timestamp() * 1_000),
                "kr": {"status": "ready"},
            },
            event_time=observed_at,
        )

    fresh = tuple(
        candidate(f"candidate-{timeframe}", timeframe) for timeframe in ("1h", "4h", "1d")
    )
    groups = perptape_resonance_groups(
        fresh,
        now=now,
        max_age=timedelta(minutes=2),
    )
    assert [[item.timeframe for item in group] for group in groups] == [["1h", "4h", "1d"]]
    refreshed = tuple(
        replace(
            item,
            candidate_id=f"{item.candidate_id}-refresh",
            observed_at=now + timedelta(minutes=1),
            triggered_at=now + timedelta(minutes=1),
        )
        for item in fresh
    )
    assert perptape_resonance_signal_identity(refreshed) == (
        perptape_resonance_signal_identity(fresh)
    )
    assert (
        perptape_resonance_signal_identity((replace(fresh[0], threshold=None), *fresh[1:])) is None
    )
    assert (
        perptape_resonance_groups(
            fresh[:2],
            now=now,
            max_age=timedelta(minutes=2),
        )
        == ()
    )
    assert (
        perptape_resonance_groups(
            (*fresh[:2], candidate("stale-1d", "1d", now - timedelta(minutes=3))),
            now=now,
            max_age=timedelta(minutes=2),
        )
        == ()
    )
    assert (
        perptape_resonance_groups(
            (*fresh, candidate("conflicting-1h", "1h", now + timedelta(seconds=1))),
            now=now,
            max_age=timedelta(minutes=2),
        )
        == ()
    )


def test_runtime_https_snapshot_preserves_persisted_incomplete_stream_target() -> None:
    now = datetime(2026, 7, 31, 8, tzinfo=UTC)
    client = PerptapeClient(
        base_url="https://perptape.com",
        api_key="fixture-key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(0),
        fetcher=lambda _url, _headers, _timeout: {},
    )
    target = replace(
        client.parse_stream_alert(
            {
                "id": "persisted-target",
                "ex": "BN",
                "s": "ETHUSDT",
                "dir": "HH",
                "p": 4_000,
                "tf": "1h",
                "t": int(now.timestamp() * 1_000),
                "u": int(now.timestamp() * 1_000),
                "kr": {"status": "ready"},
                "vq24": 20_000,
                "oi": 10_000,
            },
            event_time=now,
        ),
        data_health="DEGRADED",
        readiness="INCOMPLETE",
    )
    current = PerptapeFeedSnapshot(
        contract_version="breakouts-v1",
        generated_at=now,
        fetched_at=now,
        next_allowed_at=now,
        candidates=(target,),
    )
    incoming = PerptapeFeedSnapshot(
        contract_version="breakouts-v1",
        generated_at=now + timedelta(seconds=1),
        fetched_at=now + timedelta(seconds=1),
        next_allowed_at=now + timedelta(seconds=1),
        candidates=(),
    )
    recorded: list[PerptapeFeedSnapshot] = []
    worker: Any = object.__new__(RuntimeSyncWorker)
    worker.settings = SimpleNamespace(
        perptape_contract_version="breakouts-v1",
        perptape_api_key="fixture-key",
        perptape_cache_seconds=60,
        perptape_websocket_reconciliation_seconds=300,
    )
    worker.queries = SimpleNamespace(perptape_feed=lambda: current)
    worker.perptape = SimpleNamespace(refresh=lambda **_kwargs: incoming)
    worker.service = SimpleNamespace(
        record_perptape_feed=lambda _actor_id, feed, **_kwargs: recorded.append(feed),
        proposal_automation_config=lambda _actor_id: None,
    )

    observed = worker._record_perptape(
        UUID("11111111-1111-1111-1111-111111111111"),
        now + timedelta(seconds=1),
    )

    assert observed == 1
    assert len(recorded) == 1
    assert recorded[0].candidates == (target,)


def test_runtime_venue_success_requires_this_cycle_reconciliation_match() -> None:
    reconciliation_id = UUID("11111111-1111-1111-1111-111111111111")

    class Service:
        status = ReconciliationStatus.DIFFERENCE

        def reconcile_scope(self, scope: str, actor_id: UUID, *, now: Any) -> UUID:
            assert scope == "LIVE:account:BINANCE"
            return reconciliation_id

        def reconciliation_status(self, value: UUID) -> ReconciliationStatus:
            assert value == reconciliation_id
            return self.status

    worker: Any = object.__new__(RuntimeSyncWorker)
    worker.service = Service()

    with pytest.raises(DomainRejected, match="RUNTIME_RECONCILIATION_NOT_MATCH"):
        worker._require_scope_match(
            "LIVE:account:BINANCE",
            UUID("22222222-2222-2222-2222-222222222222"),
            runtime_module.datetime.fromisoformat("2026-07-31T00:00:00+00:00"),
        )

    worker.service.status = ReconciliationStatus.UNKNOWN
    with pytest.raises(DomainRejected, match="RUNTIME_RECONCILIATION_NOT_MATCH"):
        worker._require_scope_match(
            "LIVE:account:BINANCE",
            UUID("22222222-2222-2222-2222-222222222222"),
            runtime_module.datetime.fromisoformat("2026-07-31T00:00:00+00:00"),
        )

    worker.service.status = ReconciliationStatus.MATCH
    worker._require_scope_match(
        "LIVE:account:BINANCE",
        UUID("22222222-2222-2222-2222-222222222222"),
        runtime_module.datetime.fromisoformat("2026-07-31T00:00:00+00:00"),
    )

    with pytest.raises(DomainRejected, match="BINANCE_READ_ONLY_UNAVAILABLE"):
        worker._require_scope_match(
            "LIVE:account:BINANCE",
            UUID("22222222-2222-2222-2222-222222222222"),
            runtime_module.datetime.fromisoformat("2026-07-31T00:00:00+00:00"),
            source_error_code="BINANCE_READ_ONLY_UNAVAILABLE",
        )


def test_runtime_worker_factory_constructs_read_only_clients() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        _env_file=None,
    )
    database = object()

    worker = build_runtime_worker(settings, database)  # type: ignore[arg-type]

    assert worker.database is database
    assert worker.binance.configured is False
    assert worker.hyperliquid.configured is False
    assert worker.notilt.available is True


def test_runtime_once_cli_reports_readiness_and_disposes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        _env_file=None,
    )

    class FakeDatabase:
        disposed = False

        def is_ready(self) -> tuple[bool, str | None]:
            return True, None

        def dispose(self) -> None:
            self.disposed = True

    class FakeWorker:
        def run_once(self) -> RuntimeSyncReport:
            return RuntimeSyncReport(
                started_at="2026-07-31T00:00:00+00:00",
                completed_at="2026-07-31T00:00:01+00:00",
                sources={
                    "PERPTAPE": SourceSyncResult("SUCCESS", items_observed=10),
                    "BINANCE": SourceSyncResult("SUCCESS", items_observed=1),
                    "HYPERLIQUID": SourceSyncResult("SUCCESS", items_observed=1),
                    "NOTILT:42161": SourceSyncResult("SUCCESS", items_observed=1),
                },
                net_worth={"complete": True, "issues": [], "total": "10"},
            )

    database = FakeDatabase()
    monkeypatch.setattr(runtime_module, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime_module, "Database", lambda _url: database)
    monkeypatch.setattr(
        runtime_module,
        "build_runtime_worker",
        lambda _settings, _database: FakeWorker(),
    )
    monkeypatch.setattr(runtime_module, "configure_logging", lambda _level: None)

    exit_code = runtime_module.main(["--once"])
    output: dict[str, Any] = runtime_module.json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["source_sync_successful"] is True
    assert output["ready_for_new_risk"] is True
    assert database.disposed is True


def test_runtime_continuous_loop_waits_between_degraded_cycles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        runtime_sync_interval_seconds=30,
        _env_file=None,
    )
    report = RuntimeSyncReport(
        started_at="2026-07-31T00:00:00+00:00",
        completed_at="2026-07-31T00:00:01+00:00",
        sources={"PERPTAPE": SourceSyncResult("SUCCESS", items_observed=10)},
        net_worth={"complete": False, "issues": ["MISSING_LIVE_SOURCE:VAULT"]},
    )
    worker: Any = object.__new__(RuntimeSyncWorker)
    worker.settings = settings
    cycles: list[str] = []
    monkeypatch.setattr(
        worker,
        "run_once",
        lambda *, started_at, completed_at: (
            cycles.append(f"{started_at.isoformat()}..{completed_at.isoformat()}") or report
        ),
    )
    worker.clock = lambda: datetime(2026, 7, 31, tzinfo=UTC)

    class StopAfterFirstWait:
        stopped = False

        def is_set(self) -> bool:
            return self.stopped

        def wait(self, timeout: float) -> bool:
            assert timeout == 30
            self.stopped = True
            return True

        def set(self) -> None:
            self.stopped = True

    stop_event = StopAfterFirstWait()
    worker.run_forever(stop_event)

    assert cycles == ["2026-07-31T00:00:00+00:00..2026-07-31T00:00:00+00:00"]
    assert stop_event.stopped is True


@pytest.mark.parametrize(
    "invalid_time",
    [
        datetime.max.replace(tzinfo=UTC),
        datetime(2026, 7, 31, 8),
        datetime.min.replace(tzinfo=timezone(timedelta(hours=23, minutes=59))),
    ],
)
@pytest.mark.parametrize("failure_point", ["START", "COMPLETION"])
def test_runtime_continuous_rejects_initial_clock_before_queries_or_stream(
    monkeypatch: pytest.MonkeyPatch,
    invalid_time: datetime,
    failure_point: str,
) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        perptape_api_key="fixture-key",
        perptape_websocket_enabled=True,
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
    worker._perptape_stream_thread = None
    query_calls = 0
    cycle_calls = 0
    stream_calls = 0

    def principal(_username: str) -> None:
        nonlocal query_calls
        query_calls += 1

    def create_stream(**_kwargs: Any) -> None:
        nonlocal stream_calls
        stream_calls += 1

    def run_once(*, started_at: datetime, completed_at: datetime) -> RuntimeSyncReport:
        del started_at, completed_at
        nonlocal cycle_calls
        cycle_calls += 1
        raise AssertionError("invalid initial clock must not start a cycle")

    worker.queries = SimpleNamespace(service_principal_by_username=principal)
    worker.run_once = run_once
    monkeypatch.setattr(runtime_module, "PerptapeStreamWorker", create_stream)

    class StopSpy:
        stopped = False
        waits = 0

        def is_set(self) -> bool:
            return self.stopped

        def set(self) -> None:
            self.stopped = True

        def wait(self, _timeout: float) -> bool:
            self.waits += 1
            return False

    stop = StopSpy()
    with pytest.raises(DomainRejected, match="PERPTAPE_DATETIME_INVALID"):
        worker.run_forever(stop)  # type: ignore[arg-type]

    assert query_calls == 0
    assert cycle_calls == 0
    assert stream_calls == 0
    assert stop.waits == 0
    assert stop.stopped is True
    assert worker.dependencies_in_use is False


@pytest.mark.parametrize("failure_point", ["SCHEDULE", "NEXT_START", "NEXT_COMPLETION"])
def test_runtime_continuous_validates_before_wait_and_each_later_cycle(
    failure_point: str,
) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        runtime_sync_interval_seconds=30,
        _env_file=None,
    )
    invalid = datetime.max.replace(tzinfo=UTC)
    valid = datetime(2026, 7, 31, tzinfo=UTC)
    clock_values = [valid, valid]
    clock_values.append(invalid if failure_point == "SCHEDULE" else valid)
    if failure_point != "SCHEDULE":
        clock_values.append(invalid if failure_point == "NEXT_START" else valid)
    if failure_point == "NEXT_COMPLETION":
        clock_values.append(invalid)
    values = iter(clock_values)
    report = RuntimeSyncReport(
        started_at="2026-07-31T00:00:00+00:00",
        completed_at="2026-07-31T00:00:01+00:00",
        sources={"PERPTAPE": SourceSyncResult("SUCCESS")},
        net_worth={"complete": False},
    )
    worker: Any = object.__new__(RuntimeSyncWorker)
    worker.settings = settings
    worker.clock = lambda: next(values)
    worker._perptape_stream_thread = None
    cycles: list[tuple[datetime, datetime]] = []
    worker.run_once = lambda *, started_at, completed_at: (
        cycles.append((started_at, completed_at)) or report
    )

    class WaitSpy:
        def __init__(self) -> None:
            self.stopped = False
            self.waits: list[float] = []

        def is_set(self) -> bool:
            return self.stopped

        def set(self) -> None:
            self.stopped = True

        def wait(self, timeout: float) -> bool:
            self.waits.append(timeout)
            return False

    stop = WaitSpy()
    with pytest.raises(DomainRejected, match="PERPTAPE_DATETIME_INVALID"):
        worker.run_forever(stop)  # type: ignore[arg-type]

    assert cycles == [(valid, valid)]
    assert stop.waits == ([] if failure_point == "SCHEDULE" else [30])
    assert stop.stopped is True


def test_runtime_enabled_websocket_is_started_and_joined_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        runtime_sync_enabled=True,
        perptape_websocket_enabled=True,
        perptape_api_key="fixture-key",
        _env_file=None,
    )
    created: list[Any] = []

    class FakeStream:
        fatal_error_code = None

        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            created.append(self)

        def run_forever(self, stop_event: Any) -> None:
            stop_event.wait(1)

    monkeypatch.setattr(runtime_module, "PerptapeStreamWorker", FakeStream)
    worker: Any = object.__new__(RuntimeSyncWorker)
    worker.settings = settings
    worker.queries = SimpleNamespace(
        service_principal_by_username=lambda _username: SimpleNamespace(
            user_id=UUID("11111111-1111-1111-1111-111111111111")
        ),
        perptape_feed=lambda: None,
    )
    worker.service = SimpleNamespace(record_perptape_feed=lambda *_args, **_kwargs: 1)
    worker.perptape = object()
    worker.clock = lambda: runtime_module.datetime.fromisoformat("2026-07-31T00:00:00+00:00")
    stop_event = runtime_module.threading.Event()
    report = RuntimeSyncReport(
        started_at="2026-07-31T00:00:00+00:00",
        completed_at="2026-07-31T00:00:01+00:00",
        sources={"PERPTAPE": SourceSyncResult("SUCCESS")},
        net_worth={"complete": False},
    )
    worker.run_once = lambda *, started_at, completed_at: stop_event.set() or report

    worker.run_forever(stop_event)

    assert len(created) == 1
    assert created[0].kwargs["websocket_url"] == "wss://perptape.com/ws/v1/alerts"
    assert stop_event.is_set() is True
    assert worker.dependencies_in_use is False


def test_runtime_stream_stop_bound_covers_http_open_or_close() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        perptape_timeout_seconds=30,
        _env_file=None,
    )
    worker: Any = object.__new__(RuntimeSyncWorker)
    worker.settings = settings

    assert worker._perptape_stop_timeout_seconds() == 37


def test_runtime_continuous_cli_installs_stop_handlers_and_disposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        runtime_sync_enabled=True,
        _env_file=None,
    )

    class FakeDatabase:
        disposed = False

        def is_ready(self) -> tuple[bool, str | None]:
            return True, None

        def dispose(self) -> None:
            self.disposed = True

    class FakeWorker:
        cycles = 0

        def run_forever(self, stop_event: Any) -> None:
            self.cycles += 1
            stop_event.set()

    database = FakeDatabase()
    worker = FakeWorker()
    installed_signals: list[int] = []
    monkeypatch.setattr(runtime_module, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime_module, "Database", lambda _url: database)
    monkeypatch.setattr(
        runtime_module,
        "build_runtime_worker",
        lambda _settings, _database: worker,
    )
    monkeypatch.setattr(runtime_module, "configure_logging", lambda _level: None)
    monkeypatch.setattr(
        runtime_module.signal,
        "signal",
        lambda signal_number, _handler: installed_signals.append(signal_number),
    )

    assert runtime_module.main([]) == 0
    assert installed_signals == [runtime_module.signal.SIGINT, runtime_module.signal.SIGTERM]
    assert worker.cycles == 1
    assert database.disposed is True


def test_runtime_does_not_dispose_database_while_stream_thread_is_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        runtime_sync_enabled=True,
        _env_file=None,
    )

    class FakeDatabase:
        disposed = False

        def is_ready(self) -> tuple[bool, str | None]:
            return True, None

        def dispose(self) -> None:
            self.disposed = True

    class FakeWorker:
        dependencies_in_use = True

        def run_forever(self, _stop_event: Any) -> None:
            raise DomainRejected(
                "PERPTAPE_STREAM_STOP_TIMEOUT",
                "background stream is still active",
            )

    database = FakeDatabase()
    monkeypatch.setattr(runtime_module, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime_module, "Database", lambda _url: database)
    monkeypatch.setattr(
        runtime_module,
        "build_runtime_worker",
        lambda _settings, _database: FakeWorker(),
    )
    monkeypatch.setattr(runtime_module, "configure_logging", lambda _level: None)
    monkeypatch.setattr(runtime_module.signal, "signal", lambda *_args: None)

    assert runtime_module.main([]) == 1
    assert database.disposed is False
