from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_control_plane.domain import Direction, DomainRejected


@dataclass(frozen=True)
class PerptapeCandidate:
    candidate_id: str
    source: str
    source_contract_version: str
    venue: str
    source_exchange: str
    symbol: str
    canonical_symbol: str
    direction: Direction
    source_direction: str
    timeframe: str
    observed_at: datetime
    triggered_at: datetime | None
    reference_price: Decimal
    threshold: Decimal | None
    rationale: str
    data_health: str
    readiness: str
    detail_url: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["direction"] = self.direction.value
        value["observed_at"] = self.observed_at.isoformat()
        value["triggered_at"] = None if self.triggered_at is None else self.triggered_at.isoformat()
        value["reference_price"] = str(self.reference_price)
        value["threshold"] = None if self.threshold is None else str(self.threshold)
        return value


JsonFetcher = Callable[[str, dict[str, str], float], dict[str, Any]]


def _default_fetcher(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DomainRejected("PERPTAPE_UNAVAILABLE", "Perptape could not be reached") from exc
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DomainRejected("PERPTAPE_RESPONSE_INVALID", "Perptape returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise DomainRejected("PERPTAPE_RESPONSE_INVALID", "Perptape response must be an object")
    return value


class PerptapeClient:
    """Read-only adapter for Perptape's existing external breakout contract."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        contract_version: str,
        cache_ttl: timedelta,
        fetcher: JsonFetcher = _default_fetcher,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._contract_version = contract_version
        self._cache_ttl = cache_ttl
        self._fetcher = fetcher
        self._lock = threading.Lock()
        self._cached_at: datetime | None = None
        self._cached: tuple[PerptapeCandidate, ...] = ()

    def list_candidates(self, *, now: datetime) -> list[PerptapeCandidate]:
        if self._api_key is None:
            raise DomainRejected("PERPTAPE_NOT_CONFIGURED", "Perptape API key is not configured")
        with self._lock:
            if self._cached_at is not None and now - self._cached_at < self._cache_ttl:
                return list(self._cached)
            query = urllib.parse.urlencode(
                {
                    "tf": "1h,4h,1d,1w",
                    "dir": "ALL",
                    "ex": "ALL",
                    "limit": "200",
                    "sort": "triggeredAt",
                }
            )
            value = self._fetcher(
                f"{self._base_url}/api/v1/breakouts?{query}",
                {"authorization": f"Bearer {self._api_key}"},
                5.0,
            )
            candidates = self._parse_response(value)
            self._cached_at = now
            self._cached = tuple(candidates)
            return candidates

    def get_candidate(self, candidate_id: str, *, now: datetime) -> PerptapeCandidate:
        for candidate in self.list_candidates(now=now):
            if candidate.candidate_id == candidate_id:
                return candidate
        raise DomainRejected("PERPTAPE_CANDIDATE_NOT_FOUND", "candidate is no longer available")

    def _parse_response(self, value: dict[str, Any]) -> list[PerptapeCandidate]:
        if value.get("type") != "breakouts" or not isinstance(value.get("data"), list):
            raise DomainRejected(
                "PERPTAPE_RESPONSE_INVALID", "Perptape breakout response shape is invalid"
            )
        result: list[PerptapeCandidate] = []
        for raw in value["data"]:
            if not isinstance(raw, dict):
                continue
            result.append(self._parse_candidate(raw))
        return result

    def _parse_candidate(self, raw: dict[str, Any]) -> PerptapeCandidate:
        try:
            exchange = str(raw["exchange"])
            symbol = str(raw["symbol"])
            canonical_symbol = str(raw["canonicalSymbol"])
            source_direction = str(raw["direction"])
            timeframe = str(raw["timeframe"])
            price_value = raw["price"] if raw.get("price") is not None else raw["breakoutPrice"]
            price = Decimal(str(price_value))
            updated_at_ms = int(raw["updatedAt"])
            triggered_at_ms = None if raw.get("triggeredAt") is None else int(raw["triggeredAt"])
            readiness_value = raw.get("klineReadiness")
            readiness = (
                str(readiness_value.get("status", "unknown"))
                if isinstance(readiness_value, dict)
                else "unknown"
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise DomainRejected(
                "PERPTAPE_RESPONSE_INVALID", "Perptape candidate contains invalid fields"
            ) from exc
        if source_direction not in {"HH", "LL"} or price <= 0:
            raise DomainRejected(
                "PERPTAPE_RESPONSE_INVALID", "Perptape candidate direction or price is invalid"
            )
        venue = {"BN": "BINANCE", "HL": "HYPERLIQUID"}.get(exchange)
        if venue is None:
            raise DomainRejected("PERPTAPE_RESPONSE_INVALID", "Perptape venue is unsupported")
        try:
            observed_at = datetime.fromtimestamp(updated_at_ms / 1000, tz=UTC)
            triggered_at = (
                None
                if triggered_at_ms is None
                else datetime.fromtimestamp(triggered_at_ms / 1000, tz=UTC)
            )
            threshold_raw = raw.get("threshold")
            threshold = None if threshold_raw is None else Decimal(str(threshold_raw))
        except (OSError, OverflowError, InvalidOperation, ValueError) as exc:
            raise DomainRejected(
                "PERPTAPE_RESPONSE_INVALID", "Perptape candidate contains invalid facts"
            ) from exc
        identity = ":".join(
            [exchange, canonical_symbol, timeframe, source_direction, str(triggered_at_ms or 0)]
        )
        candidate_id = "pt_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        return PerptapeCandidate(
            candidate_id=candidate_id,
            source="PERPTAPE",
            source_contract_version=self._contract_version,
            venue=venue,
            source_exchange=exchange,
            symbol=symbol,
            canonical_symbol=canonical_symbol,
            direction=Direction.LONG if source_direction == "HH" else Direction.SHORT,
            source_direction=source_direction,
            timeframe=timeframe,
            observed_at=observed_at,
            triggered_at=triggered_at,
            reference_price=price,
            threshold=threshold,
            rationale=f"Perptape {source_direction} breakout on {timeframe}",
            data_health="CURRENT" if readiness == "ready" else "DEGRADED",
            readiness=readiness.upper(),
            detail_url=f"{self._base_url}/breakouts?ex={urllib.parse.quote(exchange)}",
        )
