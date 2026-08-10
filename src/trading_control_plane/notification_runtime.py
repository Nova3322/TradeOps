from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from trading_control_plane.config import Settings, get_settings
from trading_control_plane.database import Database
from trading_control_plane.logging import configure_logging
from trading_control_plane.notification import NotificationDispatcher

logger = logging.getLogger(__name__)


class NotificationWorker:
    """Runs the least-privilege notification outbox; it owns no trading adapters."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self.database = database
        self.clock = clock
        self.dispatcher = NotificationDispatcher(
            database,
            credential_encryption_key=settings.credential_encryption_key,
        )

    def run_once(self) -> dict[str, object]:
        return self.dispatcher.dispatch_due(
            now=self.clock(),
            limit=self.settings.notification_worker_batch_size,
        )

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            report = self.run_once()
            logger.info(
                "Notification delivery cycle completed",
                extra={
                    "event": "notification_delivery_cycle_completed",
                    "component": "notification-worker",
                    "selected": report["selected"],
                    "recovered_unknown": report["recovered_unknown"],
                },
            )
            if stop_event.wait(self.settings.notification_worker_interval_seconds):
                break


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run team notification delivery")
    parser.add_argument("--once", action="store_true", help="run one delivery cycle")
    args = parser.parse_args(argv)
    settings = get_settings()
    settings.validate_runtime_security()
    configure_logging(settings.log_level)
    if not settings.credential_encryption_key:
        raise SystemExit("TRADING_CREDENTIAL_ENCRYPTION_KEY is required for notification delivery")
    if not args.once and not settings.notification_worker_enabled:
        raise SystemExit("TRADING_NOTIFICATION_WORKER_ENABLED must be true for continuous mode")
    database = Database(settings.database_url)
    try:
        ready, reason = database.is_ready()
        if not ready:
            raise SystemExit(f"database is not ready: {reason}")
        worker = NotificationWorker(settings=settings, database=database)
        if args.once:
            print(json.dumps(worker.run_once(), separators=(",", ":"), sort_keys=True))
            return 0
        stop_event = threading.Event()
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signal_number, lambda _signal, _frame: stop_event.set())
        worker.run_forever(stop_event)
        return 0
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
