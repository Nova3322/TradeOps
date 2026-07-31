from typing import Any

import pytest

import trading_control_plane.runtime as runtime_module
from trading_control_plane.config import Settings
from trading_control_plane.domain import DomainRejected
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
                sources={"PERPTAPE": SourceSyncResult("SUCCESS", items_observed=10)},
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

    stop_event = StopAfterFirstWait()
    worker.run_forever(stop_event)

    assert cycles == ["completed"]
    assert stop_event.stopped is True


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
