from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import trading_control_plane.notification_runtime as runtime_module
from trading_control_plane.config import Settings
from trading_control_plane.notification_runtime import NotificationWorker


def test_notification_worker_uses_bounded_runtime_settings() -> None:
    now = datetime(2026, 8, 10, tzinfo=UTC)
    worker: Any = object.__new__(NotificationWorker)
    worker.settings = SimpleNamespace(notification_worker_batch_size=17)
    worker.clock = lambda: now
    observed: dict[str, object] = {}

    def dispatch_due(*, now: datetime, limit: int) -> dict[str, object]:
        observed.update(now=now, limit=limit)
        return {"selected": 0, "results": {}, "recovered_unknown": 0}

    worker.dispatcher = SimpleNamespace(dispatch_due=dispatch_due)

    assert worker.run_once() == {
        "selected": 0,
        "results": {},
        "recovered_unknown": 0,
    }
    assert observed == {"now": now, "limit": 17}


def test_notification_worker_builds_dispatcher_with_only_notification_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        credential_encryption_key="G4dAqHdhSHI_KptQdXKVIgF_eVXWYFW3viBTPWLSBEs",
        notification_email_smtp_allowed_hosts="smtp.example.com",
        _env_file=None,
    )
    database = object()
    observed: dict[str, object] = {}

    def dispatcher(
        candidate: object,
        *,
        credential_encryption_key: str | None,
        sender: object,
    ) -> object:
        observed.update(
            database=candidate,
            credential_encryption_key=credential_encryption_key,
            sender=sender,
        )
        return object()

    monkeypatch.setattr(runtime_module, "NotificationDispatcher", dispatcher)

    worker = NotificationWorker(settings=settings, database=database)  # type: ignore[arg-type]

    assert worker.database is database
    assert observed == {
        "database": database,
        "credential_encryption_key": settings.credential_encryption_key,
        "sender": observed["sender"],
    }
    assert observed["sender"].email_smtp_allowed_hosts == {"smtp.example.com"}  # type: ignore[union-attr]


def test_notification_worker_continuous_loop_waits_between_cycles() -> None:
    worker: Any = object.__new__(NotificationWorker)
    worker.settings = SimpleNamespace(notification_worker_interval_seconds=15)
    worker.run_once = lambda: {"selected": 1, "results": {"SENT": 1}, "recovered_unknown": 0}
    waits: list[int] = []

    class StopAfterFirstWait:
        def is_set(self) -> bool:
            return False

        def wait(self, seconds: int) -> bool:
            waits.append(seconds)
            return True

    worker.run_forever(StopAfterFirstWait())

    assert waits == [15]


def test_notification_once_cli_checks_readiness_runs_and_disposes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        credential_encryption_key="G4dAqHdhSHI_KptQdXKVIgF_eVXWYFW3viBTPWLSBEs",
        _env_file=None,
    )

    class FakeDatabase:
        disposed = False

        def is_ready(self) -> tuple[bool, str | None]:
            return True, None

        def dispose(self) -> None:
            self.disposed = True

    class FakeWorker:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def run_once(self) -> dict[str, object]:
            return {"selected": 1, "results": {"SENT": 1}, "recovered_unknown": 0}

    database = FakeDatabase()
    monkeypatch.setattr(runtime_module, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime_module, "Database", lambda _url: database)
    monkeypatch.setattr(runtime_module, "NotificationWorker", FakeWorker)
    monkeypatch.setattr(runtime_module, "configure_logging", lambda _level: None)

    assert runtime_module.main(["--once"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "recovered_unknown": 0,
        "results": {"SENT": 1},
        "selected": 1,
    }
    assert database.disposed is True


def test_notification_continuous_cli_installs_shutdown_handlers_and_disposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        credential_encryption_key="G4dAqHdhSHI_KptQdXKVIgF_eVXWYFW3viBTPWLSBEs",
        notification_worker_enabled=True,
        _env_file=None,
    )

    class FakeDatabase:
        disposed = False

        def is_ready(self) -> tuple[bool, str | None]:
            return True, None

        def dispose(self) -> None:
            self.disposed = True

    class FakeWorker:
        stop_event: object | None = None

        def __init__(self, **_kwargs: object) -> None:
            pass

        def run_forever(self, stop_event: object) -> None:
            self.stop_event = stop_event

    database = FakeDatabase()
    worker = FakeWorker()
    installed_signals: list[int] = []
    monkeypatch.setattr(runtime_module, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime_module, "Database", lambda _url: database)
    monkeypatch.setattr(runtime_module, "NotificationWorker", lambda **_kwargs: worker)
    monkeypatch.setattr(runtime_module, "configure_logging", lambda _level: None)
    monkeypatch.setattr(
        runtime_module.signal,
        "signal",
        lambda signal_number, _handler: installed_signals.append(signal_number),
    )

    assert runtime_module.main([]) == 0
    assert worker.stop_event is not None
    assert installed_signals == [runtime_module.signal.SIGINT, runtime_module.signal.SIGTERM]
    assert database.disposed is True


def test_notification_healthcheck_validates_gates_without_building_worker(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        credential_encryption_key="G4dAqHdhSHI_KptQdXKVIgF_eVXWYFW3viBTPWLSBEs",
        notification_worker_enabled=True,
        _env_file=None,
    )

    class FakeDatabase:
        disposed = False

        def is_ready(self) -> tuple[bool, str | None]:
            return True, None

        def dispose(self) -> None:
            self.disposed = True

    database = FakeDatabase()
    monkeypatch.setattr(runtime_module, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime_module, "Database", lambda _url: database)
    monkeypatch.setattr(runtime_module, "configure_logging", lambda _level: None)
    monkeypatch.setattr(
        runtime_module,
        "NotificationWorker",
        lambda **_kwargs: pytest.fail("healthcheck must not build a delivery worker"),
    )

    assert runtime_module.main(["--healthcheck"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "component": "notification-worker",
        "database": "READY",
        "status": "READY",
    }
    assert database.disposed is True


@pytest.mark.parametrize(
    ("settings", "argv", "message"),
    [
        (
            Settings(
                database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
                _env_file=None,
            ),
            ["--once"],
            "TRADING_CREDENTIAL_ENCRYPTION_KEY",
        ),
        (
            Settings(
                database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
                credential_encryption_key="G4dAqHdhSHI_KptQdXKVIgF_eVXWYFW3viBTPWLSBEs",
                _env_file=None,
            ),
            [],
            "TRADING_NOTIFICATION_WORKER_ENABLED",
        ),
        (
            Settings(
                database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
                credential_encryption_key="G4dAqHdhSHI_KptQdXKVIgF_eVXWYFW3viBTPWLSBEs",
                _env_file=None,
            ),
            ["--healthcheck"],
            "TRADING_NOTIFICATION_WORKER_ENABLED",
        ),
    ],
)
def test_notification_cli_is_fail_closed_before_database_access(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    argv: list[str],
    message: str,
) -> None:
    monkeypatch.setattr(runtime_module, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime_module, "configure_logging", lambda _level: None)
    monkeypatch.setattr(
        runtime_module,
        "Database",
        lambda _url: pytest.fail("database must not be opened before runtime gates pass"),
    )

    with pytest.raises(SystemExit, match=message):
        runtime_module.main(argv)


def test_notification_cli_reports_database_readiness_reason_and_disposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        credential_encryption_key="G4dAqHdhSHI_KptQdXKVIgF_eVXWYFW3viBTPWLSBEs",
        _env_file=None,
    )

    class FakeDatabase:
        disposed = False

        def is_ready(self) -> tuple[bool, str | None]:
            return False, "schema revision mismatch"

        def dispose(self) -> None:
            self.disposed = True

    database = FakeDatabase()
    monkeypatch.setattr(runtime_module, "get_settings", lambda: settings)
    monkeypatch.setattr(runtime_module, "Database", lambda _url: database)
    monkeypatch.setattr(runtime_module, "configure_logging", lambda _level: None)

    with pytest.raises(SystemExit, match="schema revision mismatch"):
        runtime_module.main(["--once"])
    assert database.disposed is True
