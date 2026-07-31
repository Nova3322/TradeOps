from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

import trading_control_plane.runtime as runtime_module
from trading_control_plane.config import Settings
from trading_control_plane.domain import DomainRejected, ReconciliationStatus
from trading_control_plane.runtime import (
    RuntimeSyncReport,
    RuntimeSyncWorker,
    SourceSyncResult,
    build_runtime_worker,
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

    worker.service.status = ReconciliationStatus.MATCH
    worker._require_scope_match(
        "LIVE:account:BINANCE",
        UUID("22222222-2222-2222-2222-222222222222"),
        runtime_module.datetime.fromisoformat("2026-07-31T00:00:00+00:00"),
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
        lambda: cycles.append("completed") or report,
    )

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

    assert cycles == ["completed"]
    assert stop_event.stopped is True


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
    worker.run_once = lambda: stop_event.set() or report

    worker.run_forever(stop_event)

    assert len(created) == 1
    assert created[0].kwargs["websocket_url"] == "wss://perptape.com/ws/v1/alerts"
    assert stop_event.is_set() is True


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
