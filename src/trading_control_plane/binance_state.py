from __future__ import annotations

import math
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from trading_control_plane.binance_errors import (
    BinanceApiDiagnostic,
    BinanceRequestState,
    binance_rate_limit_headers,
)
from trading_control_plane.database import Database
from trading_control_plane.models import BinanceApiState

BINANCE_DEPLOYMENT_SCOPE = "BINANCE_DEPLOYMENT_IP"


class DatabaseBinanceRequestState(BinanceRequestState):
    """Persist deployment-wide Binance cooldown, weight and time-offset state."""

    def __init__(self, database: Database) -> None:
        super().__init__()
        self.database = database
        self._owner_prefix = uuid4().hex

    def _owner(self) -> str:
        return f"{self._owner_prefix}:{threading.get_ident()}"

    @staticmethod
    def _new(now: datetime) -> BinanceApiState:
        return BinanceApiState(
            scope_key=BINANCE_DEPLOYMENT_SCOPE,
            host=None,
            diagnostic=None,
            next_retry_at=None,
            rate_limit_headers={},
            headers_observed_at=None,
            clock_offset_ms=None,
            clock_synchronized_at=None,
            probe_owner=None,
            probe_started_at=None,
            updated_at=now,
        )

    def current_diagnostic(self) -> BinanceApiDiagnostic | None:
        with self.database.session_factory() as session:
            row = session.get(BinanceApiState, BINANCE_DEPLOYMENT_SCOPE)
            if row is None or not isinstance(row.diagnostic, dict):
                return None
            return BinanceApiDiagnostic.from_dict(row.diagnostic)

    def record_rate_limit(
        self, diagnostic: BinanceApiDiagnostic, *, host: str | None = None
    ) -> None:
        with self.database.session_factory.begin() as session:
            row = session.get(BinanceApiState, BINANCE_DEPLOYMENT_SCOPE, with_for_update=True)
            if row is None:
                row = self._new(diagnostic.failed_at)
                session.add(row)
            if row.next_retry_at is None or diagnostic.next_retry_at >= row.next_retry_at:
                row.diagnostic = diagnostic.as_dict()
                row.next_retry_at = diagnostic.next_retry_at
            row.host = host or row.host
            row.rate_limit_headers = dict(diagnostic.rate_limit_headers)
            row.headers_observed_at = diagnostic.failed_at
            row.probe_owner = None
            row.probe_started_at = None
            row.updated_at = diagnostic.failed_at

    def blocked_diagnostic(self, *, now: datetime | None = None) -> BinanceApiDiagnostic | None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        owner = self._owner()
        with self.database.session_factory.begin() as session:
            row = session.get(BinanceApiState, BINANCE_DEPLOYMENT_SCOPE, with_for_update=True)
            if row is None or not isinstance(row.diagnostic, dict):
                return None
            diagnostic = BinanceApiDiagnostic.from_dict(row.diagnostic)
            if diagnostic is None:
                return None
            if current < diagnostic.next_retry_at:
                return diagnostic
            probe_expired = (
                row.probe_started_at is None
                or current - row.probe_started_at >= timedelta(seconds=30)
            )
            if row.probe_owner is None or probe_expired:
                row.probe_owner = owner
                row.probe_started_at = current
                row.updated_at = current
                return None
            if row.probe_owner == owner:
                return None
            probe_started_at = row.probe_started_at
            if probe_started_at is None:
                return None
            retry_at = probe_started_at + timedelta(seconds=30)
            return BinanceApiDiagnostic(
                category=diagnostic.category,
                http_status=diagnostic.http_status,
                binance_error_code=diagnostic.binance_error_code,
                binance_error_message=diagnostic.binance_error_message,
                retry_after_seconds=max(1, math.ceil((retry_at - current).total_seconds())),
                rate_limit_headers=dict(diagnostic.rate_limit_headers),
                failed_at=current,
                next_retry_at=retry_at,
            )

    def record_success(
        self,
        headers: Mapping[str, object] | None = None,
        *,
        host: str | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        now = (observed_at or datetime.now(UTC)).astimezone(UTC)
        safe = binance_rate_limit_headers(headers or {})
        owner = self._owner()
        with self.database.session_factory.begin() as session:
            row = session.get(BinanceApiState, BINANCE_DEPLOYMENT_SCOPE, with_for_update=True)
            if row is None:
                row = self._new(now)
                session.add(row)
            may_close_probe = row.diagnostic is None or (
                row.next_retry_at is not None
                and now >= row.next_retry_at
                and row.probe_owner == owner
            )
            if may_close_probe:
                row.diagnostic = None
                row.next_retry_at = None
                row.probe_owner = None
                row.probe_started_at = None
            row.host = host or row.host
            if safe:
                row.rate_limit_headers = safe
                row.headers_observed_at = now
            row.updated_at = now

    def record_response_headers(
        self,
        headers: Mapping[str, object],
        *,
        host: str | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        safe = binance_rate_limit_headers(headers)
        if not safe:
            return
        now = (observed_at or datetime.now(UTC)).astimezone(UTC)
        with self.database.session_factory.begin() as session:
            row = session.get(BinanceApiState, BINANCE_DEPLOYMENT_SCOPE, with_for_update=True)
            if row is None:
                row = self._new(now)
                session.add(row)
            row.host = host or row.host
            row.rate_limit_headers = safe
            row.headers_observed_at = now
            row.updated_at = now

    def current_headers(self) -> tuple[dict[str, str], datetime | None]:
        with self.database.session_factory() as session:
            row = session.get(BinanceApiState, BINANCE_DEPLOYMENT_SCOPE)
            if row is None:
                return {}, None
            return dict(row.rate_limit_headers or {}), row.headers_observed_at

    def current_time_offset(self) -> tuple[int, datetime] | None:
        with self.database.session_factory() as session:
            row = session.get(BinanceApiState, BINANCE_DEPLOYMENT_SCOPE)
            if row is None or row.clock_offset_ms is None or row.clock_synchronized_at is None:
                return None
            return row.clock_offset_ms, row.clock_synchronized_at

    def record_time_offset(
        self, offset_ms: int, *, synchronized_at: datetime | None = None
    ) -> None:
        now = (synchronized_at or datetime.now(UTC)).astimezone(UTC)
        with self.database.session_factory.begin() as session:
            row = session.get(BinanceApiState, BINANCE_DEPLOYMENT_SCOPE, with_for_update=True)
            if row is None:
                row = self._new(now)
                session.add(row)
            row.clock_offset_ms = int(offset_ms)
            row.clock_synchronized_at = now
            row.updated_at = now

    def low_priority_retry_at(self, *, now: datetime | None = None) -> datetime | None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        headers, observed_at = self.current_headers()
        if observed_at is None or current - observed_at > timedelta(minutes=1):
            return None
        used = []
        for name, value in headers.items():
            lowered = name.lower()
            if (
                "used-weight" not in lowered and "used-ip-weight" not in lowered
            ) or not lowered.endswith("1m"):
                continue
            try:
                used.append(int(value))
            except ValueError:
                continue
        return observed_at + timedelta(minutes=1) if used and max(used) >= 1_920 else None
