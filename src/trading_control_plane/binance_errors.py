from __future__ import annotations

import math
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from trading_control_plane.domain import DomainRejected

RATE_LIMIT_HEADER_PREFIXES = (
    "x-mbx-used-weight",
    "x-mbx-order-count",
    "x-sapi-used-ip-weight",
)


@dataclass(frozen=True, slots=True)
class BinanceApiDiagnostic:
    category: str
    http_status: int
    binance_error_code: int | None
    binance_error_message: str | None
    retry_after_seconds: int
    rate_limit_headers: dict[str, str]
    failed_at: datetime
    next_retry_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "http_status": self.http_status,
            "binance_error_code": self.binance_error_code,
            "binance_error_message": self.binance_error_message,
            "retry_after_seconds": self.retry_after_seconds,
            "rate_limit_headers": dict(self.rate_limit_headers),
            "failed_at": self.failed_at.astimezone(UTC).isoformat(),
            "next_retry_at": self.next_retry_at.astimezone(UTC).isoformat(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> BinanceApiDiagnostic | None:
        try:
            failed_at = datetime.fromisoformat(str(value["failed_at"])).astimezone(UTC)
            next_retry_at = datetime.fromisoformat(str(value["next_retry_at"])).astimezone(UTC)
            raw_headers = value.get("rate_limit_headers")
            headers = (
                {str(key): str(item) for key, item in raw_headers.items()}
                if isinstance(raw_headers, Mapping)
                else {}
            )
            raw_code = value.get("binance_error_code")
            return cls(
                category=str(value["category"]),
                http_status=int(value["http_status"]),
                binance_error_code=None if raw_code is None else int(raw_code),
                binance_error_message=(
                    None
                    if value.get("binance_error_message") is None
                    else str(value["binance_error_message"])
                ),
                retry_after_seconds=int(value["retry_after_seconds"]),
                rate_limit_headers=headers,
                failed_at=failed_at,
                next_retry_at=next_retry_at,
            )
        except (KeyError, TypeError, ValueError):
            return None


def binance_rate_limit_headers(headers: Mapping[str, object]) -> dict[str, str]:
    """Return every Binance weight/order header without depending on header casing."""

    result: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        name = str(raw_name)
        lowered = name.lower()
        if lowered == "retry-after" or lowered.startswith(RATE_LIMIT_HEADER_PREFIXES):
            result[name] = str(raw_value)[:120]
    return result


class BinanceRequestState:
    """Process-shared request state; a DB-backed subclass may persist the same contract."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._diagnostic: BinanceApiDiagnostic | None = None
        self._headers: dict[str, str] = {}
        self._headers_observed_at: datetime | None = None
        self._clock_offset_ms: int | None = None
        self._clock_synchronized_at: datetime | None = None
        self._probe_owner: int | None = None
        self._probe_started_at: datetime | None = None

    def current_diagnostic(self) -> BinanceApiDiagnostic | None:
        with self._lock:
            return self._diagnostic

    def record_rate_limit(
        self, diagnostic: BinanceApiDiagnostic, *, host: str | None = None
    ) -> None:
        del host
        with self._lock:
            if (
                self._diagnostic is None
                or diagnostic.next_retry_at >= self._diagnostic.next_retry_at
            ):
                self._diagnostic = diagnostic
            self._headers = dict(diagnostic.rate_limit_headers)
            self._headers_observed_at = diagnostic.failed_at
            self._probe_owner = None
            self._probe_started_at = None

    def blocked_diagnostic(self, *, now: datetime | None = None) -> BinanceApiDiagnostic | None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        owner = threading.get_ident()
        with self._lock:
            diagnostic = self._diagnostic
            if diagnostic is None:
                return None
            if current < diagnostic.next_retry_at:
                return diagnostic
            probe_expired = (
                self._probe_started_at is None
                or current - self._probe_started_at >= timedelta(seconds=30)
            )
            if self._probe_owner is None or probe_expired:
                self._probe_owner = owner
                self._probe_started_at = current
                return None
            if self._probe_owner == owner:
                return None
            retry_at = self._probe_started_at + timedelta(seconds=30)
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
        del host
        current = (observed_at or datetime.now(UTC)).astimezone(UTC)
        safe = binance_rate_limit_headers(headers or {})
        owner = threading.get_ident()
        with self._lock:
            may_close_probe = self._diagnostic is None or (
                current >= self._diagnostic.next_retry_at and self._probe_owner == owner
            )
            if may_close_probe:
                self._diagnostic = None
                self._probe_owner = None
                self._probe_started_at = None
            if safe:
                self._headers = safe
                self._headers_observed_at = current

    def record_response_headers(
        self,
        headers: Mapping[str, object],
        *,
        host: str | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        del host
        safe = binance_rate_limit_headers(headers)
        if not safe:
            return
        with self._lock:
            self._headers = safe
            self._headers_observed_at = (observed_at or datetime.now(UTC)).astimezone(UTC)

    def current_headers(self) -> tuple[dict[str, str], datetime | None]:
        with self._lock:
            return dict(self._headers), self._headers_observed_at

    def current_time_offset(self) -> tuple[int, datetime] | None:
        with self._lock:
            if self._clock_offset_ms is None or self._clock_synchronized_at is None:
                return None
            return self._clock_offset_ms, self._clock_synchronized_at

    def record_time_offset(
        self, offset_ms: int, *, synchronized_at: datetime | None = None
    ) -> None:
        with self._lock:
            self._clock_offset_ms = int(offset_ms)
            self._clock_synchronized_at = (synchronized_at or datetime.now(UTC)).astimezone(UTC)

    def low_priority_retry_at(self, *, now: datetime | None = None) -> datetime | None:
        """Conservatively defer non-urgent reads near the documented 2400/min futures limit."""

        current = (now or datetime.now(UTC)).astimezone(UTC)
        headers, observed_at = self.current_headers()
        if observed_at is None or current - observed_at > timedelta(minutes=1):
            return None
        used_values: list[int] = []
        for name, value in headers.items():
            lowered = name.lower()
            if (
                "used-weight" not in lowered and "used-ip-weight" not in lowered
            ) or not lowered.endswith("1m"):
                continue
            try:
                used_values.append(int(value))
            except ValueError:
                continue
        if not used_values or max(used_values) < 1_920:
            return None
        return observed_at + timedelta(minutes=1)


class BinanceApiRejected(DomainRejected):
    def __init__(self, code: str, detail: str, diagnostic: BinanceApiDiagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(code, detail, metadata=diagnostic.as_dict())


def _retry_after_seconds(
    headers: Mapping[str, str], message: str | None, *, now: datetime, category: str
) -> int:
    raw = next(
        (str(value) for name, value in headers.items() if str(name).lower() == "retry-after"),
        None,
    )
    if raw:
        try:
            return max(1, math.ceil(float(raw)))
        except (ValueError, OverflowError):
            try:
                retry_at = parsedate_to_datetime(raw)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                return max(1, math.ceil((retry_at - now).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    banned_until = re.search(r"banned\s+until\s+(\d{10,13})", message or "", re.IGNORECASE)
    if banned_until:
        raw_timestamp = int(banned_until.group(1))
        timestamp = raw_timestamp / 1000 if raw_timestamp >= 10**12 else raw_timestamp
        return max(
            1,
            math.ceil((datetime.fromtimestamp(timestamp, UTC) - now).total_seconds()),
        )
    return {
        "IP_TEMPORARILY_BANNED": 120,
        "REQUEST_WEIGHT_EXCEEDED": 60,
        "ORDINARY_RATE_LIMIT": 30,
    }[category]


def classify_binance_rate_limit(
    *,
    http_status: int,
    binance_error_code: int | None,
    binance_error_message: str | None,
    headers: Mapping[str, str],
    failed_at: datetime | None = None,
) -> BinanceApiDiagnostic:
    now = (failed_at or datetime.now(UTC)).astimezone(UTC)
    message = None if binance_error_message is None else str(binance_error_message)[:500]
    if http_status == 418 or re.search(r"IP\s+banned|banned\s+until", message or "", re.I):
        category = "IP_TEMPORARILY_BANNED"
    elif binance_error_code == -1003:
        category = "REQUEST_WEIGHT_EXCEEDED"
    else:
        category = "ORDINARY_RATE_LIMIT"
    safe_headers = binance_rate_limit_headers(headers)
    retry_after = _retry_after_seconds(safe_headers, message, now=now, category=category)
    return BinanceApiDiagnostic(
        category=category,
        http_status=http_status,
        binance_error_code=binance_error_code,
        binance_error_message=message,
        retry_after_seconds=retry_after,
        rate_limit_headers=safe_headers,
        failed_at=now,
        next_retry_at=now + timedelta(seconds=retry_after),
    )
