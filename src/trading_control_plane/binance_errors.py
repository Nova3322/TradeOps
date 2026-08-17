from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from trading_control_plane.domain import DomainRejected

RATE_LIMIT_HEADER_NAMES = (
    "Retry-After",
    "X-MBX-USED-WEIGHT",
    "X-MBX-USED-WEIGHT-1M",
    "X-MBX-ORDER-COUNT-10S",
    "X-MBX-ORDER-COUNT-1M",
    "X-SAPI-USED-IP-WEIGHT-1M",
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


class BinanceApiRejected(DomainRejected):
    def __init__(self, code: str, detail: str, diagnostic: BinanceApiDiagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(code, detail, metadata=diagnostic.as_dict())


def _retry_after_seconds(
    headers: Mapping[str, str], message: str | None, *, now: datetime, category: str
) -> int:
    raw = headers.get("Retry-After")
    if raw:
        try:
            return max(1, min(86_400, math.ceil(float(raw))))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(raw)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                return max(1, min(86_400, math.ceil((retry_at - now).total_seconds())))
            except (TypeError, ValueError, OverflowError):
                pass
    banned_until = re.search(r"banned\s+until\s+(\d{10,13})", message or "", re.IGNORECASE)
    if banned_until:
        raw_timestamp = int(banned_until.group(1))
        timestamp = raw_timestamp / 1000 if raw_timestamp >= 10**12 else raw_timestamp
        return max(
            1,
            min(86_400, math.ceil((datetime.fromtimestamp(timestamp, UTC) - now).total_seconds())),
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
    safe_headers = {
        name: str(headers[name])[:120]
        for name in RATE_LIMIT_HEADER_NAMES
        if headers.get(name) is not None
    }
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
