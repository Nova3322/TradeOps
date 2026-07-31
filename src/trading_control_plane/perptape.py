from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from trading_control_plane.domain import Direction, DomainRejected

PERPTAPE_OFFICIAL_HOST = "perptape.com"
PERPTAPE_WEBSOCKET_PATH = "/ws/v1/alerts"
# A normal full 2,048-candidate window is about 1.2-1.6 MiB; 4 MiB leaves
# reconciliation headroom while making the authoritative JSONB payload finite.
PERPTAPE_PAYLOAD_MAX_BYTES = 4 * 1024 * 1024
PERPTAPE_STRING_FIELD_MAX_BYTES = {
    "candidate_id": 200,
    "source": 16,
    "source_contract_version": 64,
    "venue": 32,
    "source_exchange": 16,
    "symbol": 64,
    "canonical_symbol": 64,
    "source_direction": 8,
    "timeframe": 16,
    "rationale": 512,
    "data_health": 32,
    "readiness": 32,
    "detail_url": 2_048,
    "chart_url": 2_048,
}


def validate_perptape_http_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Perptape base URL must use the official HTTPS host") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != PERPTAPE_OFFICIAL_HOST
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Perptape base URL must use the official HTTPS host")
    return value.rstrip("/")


def validate_perptape_websocket_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Perptape WebSocket URL must use the official WSS endpoint") from exc
    if (
        parsed.scheme != "wss"
        or parsed.hostname != PERPTAPE_OFFICIAL_HOST
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != PERPTAPE_WEBSOCKET_PATH
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Perptape WebSocket URL must use the official WSS endpoint")
    return value


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
    quote_volume: Decimal | None = None
    open_interest: Decimal | None = None
    chart_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["direction"] = self.direction.value
        value["observed_at"] = self.observed_at.isoformat()
        value["triggered_at"] = None if self.triggered_at is None else self.triggered_at.isoformat()
        value["reference_price"] = str(self.reference_price)
        value["threshold"] = None if self.threshold is None else str(self.threshold)
        value["quote_volume"] = None if self.quote_volume is None else str(self.quote_volume)
        value["open_interest"] = None if self.open_interest is None else str(self.open_interest)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PerptapeCandidate:
        try:
            triggered_at = value.get("triggered_at")
            threshold = value.get("threshold")
            candidate = cls(
                candidate_id=str(value["candidate_id"]),
                source=str(value["source"]),
                source_contract_version=str(value["source_contract_version"]),
                venue=str(value["venue"]),
                source_exchange=str(value["source_exchange"]),
                symbol=str(value["symbol"]),
                canonical_symbol=str(value["canonical_symbol"]),
                direction=Direction(str(value["direction"])),
                source_direction=str(value["source_direction"]),
                timeframe=str(value["timeframe"]),
                observed_at=datetime.fromisoformat(str(value["observed_at"])),
                triggered_at=(
                    None if triggered_at is None else datetime.fromisoformat(str(triggered_at))
                ),
                reference_price=Decimal(str(value["reference_price"])),
                threshold=None if threshold is None else Decimal(str(threshold)),
                rationale=str(value["rationale"]),
                data_health=str(value["data_health"]),
                readiness=str(value["readiness"]),
                detail_url=str(value["detail_url"]),
                quote_volume=(
                    None
                    if value.get("quote_volume") is None
                    else Decimal(str(value["quote_volume"]))
                ),
                open_interest=(
                    None
                    if value.get("open_interest") is None
                    else Decimal(str(value["open_interest"]))
                ),
                chart_url=str(value.get("chart_url", "")),
            )
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise DomainRejected(
                "PERPTAPE_CACHE_INVALID",
                "persisted Perptape candidate is invalid",
            ) from exc
        validate_perptape_candidate(candidate, error_code="PERPTAPE_CACHE_INVALID")
        return candidate


@dataclass(frozen=True)
class PerptapeFeedSnapshot:
    contract_version: str
    generated_at: datetime
    fetched_at: datetime
    next_allowed_at: datetime
    candidates: tuple[PerptapeCandidate, ...]


PerptapeEventKey = tuple[str, str, str, str, datetime | None]
PERPTAPE_CANDIDATE_WINDOW = 2_048


def _validate_string_field(
    *,
    field: str,
    value: str,
    error_code: str,
    allow_empty: bool = False,
) -> None:
    if not value and not allow_empty:
        raise DomainRejected(
            error_code,
            f"Perptape candidate {field} is outside the supported contract",
        )
    if len(value.encode("utf-8")) > PERPTAPE_STRING_FIELD_MAX_BYTES[field]:
        raise DomainRejected(
            "PERPTAPE_FIELD_TOO_LARGE",
            f"Perptape candidate {field} exceeds the supported byte ceiling",
        )


def validate_perptape_candidate(
    candidate: PerptapeCandidate,
    *,
    error_code: str = "PERPTAPE_RESPONSE_INVALID",
) -> None:
    for field in PERPTAPE_STRING_FIELD_MAX_BYTES:
        _validate_string_field(
            field=field,
            value=getattr(candidate, field),
            error_code=error_code,
            allow_empty=field == "chart_url",
        )
    if (
        candidate.source != "PERPTAPE"
        or candidate.venue not in {"BINANCE", "HYPERLIQUID"}
        or candidate.source_direction not in {"HH", "LL"}
        or candidate.direction
        is not (Direction.LONG if candidate.source_direction == "HH" else Direction.SHORT)
        or not candidate.reference_price.is_finite()
        or candidate.reference_price <= 0
        or (
            candidate.threshold is not None
            and (not candidate.threshold.is_finite() or candidate.threshold <= 0)
        )
        or (
            candidate.quote_volume is not None
            and (not candidate.quote_volume.is_finite() or candidate.quote_volume < 0)
        )
        or (
            candidate.open_interest is not None
            and (not candidate.open_interest.is_finite() or candidate.open_interest < 0)
        )
        or candidate.observed_at.tzinfo is None
        or (candidate.triggered_at is not None and candidate.triggered_at.tzinfo is None)
    ):
        raise DomainRejected(
            error_code,
            "Perptape candidate is outside the supported contract",
        )


def perptape_event_key(candidate: PerptapeCandidate) -> PerptapeEventKey:
    return (
        candidate.source_exchange,
        candidate.symbol,
        candidate.source_direction,
        candidate.timeframe,
        candidate.triggered_at,
    )


def _canonical_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise DomainRejected(
            "PERPTAPE_CACHE_INVALID",
            "Perptape snapshot timestamps must be timezone-aware",
        )
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _canonical_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if not value.is_finite():
        raise DomainRejected(
            "PERPTAPE_CACHE_INVALID",
            "Perptape snapshot decimals must be finite",
        )
    if value == 0:
        return "0"
    sign, digits_tuple, exponent = value.as_tuple()
    assert isinstance(exponent, int)
    digits = list(digits_tuple)
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    coefficient = "".join(str(digit) for digit in digits)
    if exponent >= 0:
        rendered = coefficient + ("0" * exponent)
    else:
        point = len(coefficient) + exponent
        rendered = (
            coefficient[:point] + "." + coefficient[point:]
            if point > 0
            else "0." + ("0" * -point) + coefficient
        )
    return ("-" if sign else "") + rendered


def _canonical_candidate(candidate: PerptapeCandidate) -> tuple[Any, ...]:
    return (
        candidate.candidate_id,
        candidate.source,
        candidate.source_contract_version,
        candidate.venue,
        candidate.source_exchange,
        candidate.symbol,
        candidate.canonical_symbol,
        candidate.direction.value,
        candidate.source_direction,
        candidate.timeframe,
        _canonical_datetime(candidate.observed_at),
        _canonical_datetime(candidate.triggered_at),
        _canonical_decimal(candidate.reference_price),
        _canonical_decimal(candidate.threshold),
        candidate.rationale,
        candidate.data_health,
        candidate.readiness,
        candidate.detail_url,
        _canonical_decimal(candidate.quote_volume),
        _canonical_decimal(candidate.open_interest),
        candidate.chart_url,
    )


def _normalized_candidate_dict(candidate: PerptapeCandidate) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "source": candidate.source,
        "source_contract_version": candidate.source_contract_version,
        "venue": candidate.venue,
        "source_exchange": candidate.source_exchange,
        "symbol": candidate.symbol,
        "canonical_symbol": candidate.canonical_symbol,
        "direction": candidate.direction.value,
        "source_direction": candidate.source_direction,
        "timeframe": candidate.timeframe,
        "observed_at": _canonical_datetime(candidate.observed_at),
        "triggered_at": _canonical_datetime(candidate.triggered_at),
        "reference_price": _canonical_decimal(candidate.reference_price),
        "threshold": _canonical_decimal(candidate.threshold),
        "rationale": candidate.rationale,
        "data_health": candidate.data_health,
        "readiness": candidate.readiness,
        "detail_url": candidate.detail_url,
        "quote_volume": _canonical_decimal(candidate.quote_volume),
        "open_interest": _canonical_decimal(candidate.open_interest),
        "chart_url": candidate.chart_url,
    }


def perptape_payload_size_bytes(feed: PerptapeFeedSnapshot) -> int:
    normalized = [
        _normalized_candidate_dict(candidate)
        for candidate in sorted(feed.candidates, key=_candidate_window_sort_key)
    ]
    return len(
        json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def validate_perptape_feed_payload(feed: PerptapeFeedSnapshot) -> None:
    _validate_string_field(
        field="source_contract_version",
        value=feed.contract_version,
        error_code="PERPTAPE_RESPONSE_INVALID",
    )
    for candidate in feed.candidates:
        validate_perptape_candidate(candidate)
    if perptape_payload_size_bytes(feed) > PERPTAPE_PAYLOAD_MAX_BYTES:
        raise DomainRejected(
            "PERPTAPE_PAYLOAD_TOO_LARGE",
            "Perptape candidate payload exceeds the supported byte ceiling",
        )


def _canonical_candidate_json(candidate: PerptapeCandidate) -> str:
    return json.dumps(
        _canonical_candidate(candidate),
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _candidate_semantically_equal(
    left: PerptapeCandidate,
    right: PerptapeCandidate,
) -> bool:
    return _canonical_candidate(left) == _canonical_candidate(right)


def _candidate_window_sort_key(candidate: PerptapeCandidate) -> tuple[Any, ...]:
    return (
        _canonical_datetime(candidate.observed_at),
        _canonical_datetime(candidate.triggered_at) or "",
        candidate.source_exchange,
        candidate.symbol,
        candidate.source_direction,
        candidate.timeframe,
        candidate.candidate_id,
        _canonical_candidate_json(candidate),
    )


def _event_key_sort_key(key: PerptapeEventKey) -> tuple[str, ...]:
    return (
        key[0],
        key[1],
        key[2],
        key[3],
        _canonical_datetime(key[4]) or "",
    )


def perptape_snapshot_identity(feed: PerptapeFeedSnapshot) -> str:
    candidates = sorted(
        (_canonical_candidate(candidate) for candidate in feed.candidates),
        key=lambda value: json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
        ),
    )
    payload = {
        "contract_version": feed.contract_version,
        "generated_at": _canonical_datetime(feed.generated_at),
        "fetched_at": _canonical_datetime(feed.fetched_at),
        "next_allowed_at": _canonical_datetime(feed.next_allowed_at),
        "candidates": candidates,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _concurrent_candidate_choice(
    current: PerptapeCandidate,
    incoming: PerptapeCandidate,
) -> PerptapeCandidate:
    current_incomplete = current.readiness == "INCOMPLETE"
    incoming_incomplete = incoming.readiness == "INCOMPLETE"
    if current_incomplete != incoming_incomplete:
        incomplete = current if current_incomplete else incoming
        completed = incoming if current_incomplete else current
        if (
            completed.readiness == "READY"
            and completed.data_health == "CURRENT"
            and completed.observed_at >= incomplete.observed_at
        ):
            return completed
        return incomplete
    return max(
        (current, incoming),
        key=lambda candidate: (
            candidate.observed_at,
            candidate.readiness == "READY",
            candidate.data_health == "CURRENT",
            candidate.candidate_id,
            _canonical_candidate_json(candidate),
        ),
    )


def bound_perptape_feed_snapshot(
    feed: PerptapeFeedSnapshot,
) -> PerptapeFeedSnapshot:
    """Bound the authoritative candidate payload without evicting unresolved targets."""

    unique: dict[PerptapeEventKey, PerptapeCandidate] = {}
    for candidate in feed.candidates:
        key = perptape_event_key(candidate)
        existing = unique.get(key)
        unique[key] = (
            candidate if existing is None else _concurrent_candidate_choice(existing, candidate)
        )
    candidates = tuple(sorted(unique.values(), key=_candidate_window_sort_key))
    incomplete_count = sum(candidate.readiness == "INCOMPLETE" for candidate in candidates)
    if incomplete_count > PERPTAPE_CANDIDATE_WINDOW:
        raise DomainRejected(
            "PERPTAPE_STREAM_PENDING_LIMIT",
            "Perptape unresolved alert window is full",
        )
    completed_slots = PERPTAPE_CANDIDATE_WINDOW - incomplete_count
    completed = tuple(candidate for candidate in candidates if candidate.readiness != "INCOMPLETE")
    selected_completed = {
        perptape_event_key(candidate)
        for candidate in (completed[-completed_slots:] if completed_slots > 0 else ())
    }
    bounded = tuple(
        candidate
        for candidate in candidates
        if candidate.readiness == "INCOMPLETE"
        or perptape_event_key(candidate) in selected_completed
    )
    return replace(feed, candidates=bounded)


def _metadata_changed(
    base: PerptapeFeedSnapshot | None,
    incoming: PerptapeFeedSnapshot,
    field: Literal["generated_at", "fetched_at", "next_allowed_at"],
) -> bool:
    if base is None:
        return True
    if field == "generated_at":
        return _canonical_datetime(base.generated_at) != _canonical_datetime(incoming.generated_at)
    if field == "fetched_at":
        return _canonical_datetime(base.fetched_at) != _canonical_datetime(incoming.fetched_at)
    return _canonical_datetime(base.next_allowed_at) != _canonical_datetime(
        incoming.next_allowed_at
    )


def _merge_next_allowed_at(
    base: PerptapeFeedSnapshot | None,
    current: PerptapeFeedSnapshot,
    incoming: PerptapeFeedSnapshot,
) -> datetime:
    if not _metadata_changed(base, incoming, "next_allowed_at"):
        return current.next_allowed_at
    if base is None:
        if incoming.fetched_at > current.fetched_at:
            return incoming.next_allowed_at
        if incoming.fetched_at < current.fetched_at:
            return current.next_allowed_at
        return max(current.next_allowed_at, incoming.next_allowed_at)
    if _canonical_datetime(current.next_allowed_at) == _canonical_datetime(base.next_allowed_at):
        return incoming.next_allowed_at
    if incoming.fetched_at > current.fetched_at:
        return incoming.next_allowed_at
    if incoming.fetched_at < current.fetched_at:
        return current.next_allowed_at
    return max(current.next_allowed_at, incoming.next_allowed_at)


def apply_perptape_feed_delta(
    *,
    base: PerptapeFeedSnapshot | None,
    current: PerptapeFeedSnapshot | None,
    incoming: PerptapeFeedSnapshot,
) -> PerptapeFeedSnapshot:
    """Apply the caller's explicit base-to-desired changes to the locked row."""

    incoming = bound_perptape_feed_snapshot(incoming)
    if base is not None:
        base = bound_perptape_feed_snapshot(base)
    if current is not None:
        current = bound_perptape_feed_snapshot(current)
    contracts = {
        snapshot.contract_version for snapshot in (base, current, incoming) if snapshot is not None
    }
    if len(contracts) != 1:
        raise DomainRejected(
            "PERPTAPE_FEED_CONFLICT",
            "Perptape snapshot delta uses different contracts",
        )
    if current is None:
        return incoming

    base_candidates = {
        perptape_event_key(candidate): candidate
        for candidate in (() if base is None else base.candidates)
    }
    incoming_candidates = {
        perptape_event_key(candidate): candidate for candidate in incoming.candidates
    }
    merged = {perptape_event_key(candidate): candidate for candidate in current.candidates}

    for key in sorted(base_candidates.keys() - incoming_candidates.keys(), key=_event_key_sort_key):
        merged.pop(key, None)
    for key in sorted(incoming_candidates, key=_event_key_sort_key):
        desired = incoming_candidates[key]
        base_candidate = base_candidates.get(key)
        if base_candidate is not None and _candidate_semantically_equal(
            base_candidate,
            desired,
        ):
            continue
        current_candidate = merged.get(key)
        if base_candidate is not None and current_candidate is None:
            continue
        if current_candidate is None or (
            base_candidate is not None
            and _candidate_semantically_equal(current_candidate, base_candidate)
        ):
            merged[key] = desired
        elif not _candidate_semantically_equal(current_candidate, desired):
            merged[key] = _concurrent_candidate_choice(current_candidate, desired)

    generated_at = current.generated_at
    if _metadata_changed(base, incoming, "generated_at"):
        generated_at = max(current.generated_at, incoming.generated_at)
    fetched_at = current.fetched_at
    if _metadata_changed(base, incoming, "fetched_at"):
        fetched_at = max(current.fetched_at, incoming.fetched_at)
    next_allowed_at = _merge_next_allowed_at(base, current, incoming)
    return bound_perptape_feed_snapshot(
        PerptapeFeedSnapshot(
            contract_version=current.contract_version,
            generated_at=generated_at,
            fetched_at=fetched_at,
            next_allowed_at=max(generated_at, next_allowed_at),
            candidates=tuple(merged.values()),
        )
    )


def merge_incomplete_perptape_candidates(
    feed: PerptapeFeedSnapshot,
    preserved: Iterable[PerptapeCandidate],
) -> PerptapeFeedSnapshot:
    """Keep only unresolved persisted targets alongside a new full snapshot."""

    pending: dict[PerptapeEventKey, PerptapeCandidate] = {}
    for candidate in preserved:
        pending.setdefault(perptape_event_key(candidate), candidate)
    if not pending:
        return bound_perptape_feed_snapshot(feed)
    completed = {
        perptape_event_key(candidate)
        for candidate in feed.candidates
        if perptape_event_key(candidate) in pending
        and candidate.readiness == "READY"
        and candidate.data_health == "CURRENT"
        and candidate.observed_at >= pending[perptape_event_key(candidate)].observed_at
    }
    merged: list[PerptapeCandidate] = []
    included: set[PerptapeEventKey] = set()
    for candidate in feed.candidates:
        key = perptape_event_key(candidate)
        if key not in pending:
            merged.append(candidate)
        elif key in completed:
            if (
                key not in included
                and candidate.readiness == "READY"
                and candidate.data_health == "CURRENT"
                and candidate.observed_at >= pending[key].observed_at
            ):
                merged.append(candidate)
                included.add(key)
        elif key not in included:
            preserved_candidate = pending[key]
            candidate = (
                candidate
                if candidate.observed_at >= preserved_candidate.observed_at
                else preserved_candidate
            )
            merged.append(
                replace(
                    candidate,
                    data_health="DEGRADED",
                    readiness="INCOMPLETE",
                )
            )
            included.add(key)
    for key, candidate in pending.items():
        if key not in completed and key not in included:
            merged.append(
                replace(
                    candidate,
                    data_health="DEGRADED",
                    readiness="INCOMPLETE",
                )
            )
    return bound_perptape_feed_snapshot(replace(feed, candidates=tuple(merged)))


JsonFetcher = Callable[[str, dict[str, str], float], dict[str, Any]]


class PerptapeRateLimited(DomainRejected):
    def __init__(
        self,
        next_allowed_at: datetime | None = None,
        *,
        is_remote: bool = False,
    ) -> None:
        self.next_allowed_at = next_allowed_at
        self.is_remote = is_remote
        super().__init__(
            "PERPTAPE_RATE_LIMITED",
            "Perptape breakout request is rate limited",
        )


def _parse_rate_limit_deadline(body: bytes) -> datetime | None:
    try:
        value = json.loads(body)
        rate_limit = value.get("rateLimit")
        raw_deadline = (
            rate_limit.get("nextAllowedAt")
            if isinstance(rate_limit, dict)
            else value.get("nextAllowedAt")
        )
        if raw_deadline is None:
            return None
        return datetime.fromtimestamp(int(raw_deadline) / 1_000, UTC)
    except (
        AttributeError,
        json.JSONDecodeError,
        OSError,
        OverflowError,
        TypeError,
        ValueError,
    ):
        return None


def _default_fetcher(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            try:
                rate_limit_body = exc.read()
            except (AttributeError, OSError):
                rate_limit_body = b""
            raise PerptapeRateLimited(
                _parse_rate_limit_deadline(rate_limit_body),
                is_remote=True,
            ) from exc
        code = {
            401: "PERPTAPE_AUTH_FAILED",
            403: "PERPTAPE_PLAN_DENIED",
        }.get(exc.code, "PERPTAPE_UNAVAILABLE")
        detail = {
            401: "Perptape rejected the configured API key",
            403: "Perptape denied this API plan or account",
        }.get(exc.code, "Perptape could not be reached")
        raise DomainRejected(code, detail) from exc
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
        timeout_seconds: float = 15,
        fetcher: JsonFetcher = _default_fetcher,
    ) -> None:
        self._base_url = validate_perptape_http_url(base_url)
        self._api_key = api_key
        self._contract_version = contract_version
        self._cache_ttl = cache_ttl
        self._timeout_seconds = timeout_seconds
        self._fetcher = fetcher
        self._lock = threading.Lock()
        self._cached_at: datetime | None = None
        self._cached: tuple[PerptapeCandidate, ...] = ()
        self._feed: PerptapeFeedSnapshot | None = None
        self._rate_limit_not_before: datetime | None = None
        self._server_not_before: datetime | None = None
        self._remote_result_generation = 0
        self._remote_success_generation = 0
        self._remote_rate_limit_count = 0
        self._consecutive_remote_rate_limits = 0

    def remote_request_state(self) -> tuple[int, int, int, int]:
        """Return shared real-request generations and the current 429 sequence."""

        with self._lock:
            return (
                self._remote_result_generation,
                self._remote_success_generation,
                self._remote_rate_limit_count,
                self._consecutive_remote_rate_limits,
            )

    def list_candidates(self, *, now: datetime) -> list[PerptapeCandidate]:
        return list(self.refresh(now=now).candidates)

    def refresh(self, *, now: datetime, force: bool = False) -> PerptapeFeedSnapshot:
        if self._api_key is None:
            raise DomainRejected("PERPTAPE_NOT_CONFIGURED", "Perptape API key is not configured")
        with self._lock:
            if self._rate_limit_not_before is not None and now < self._rate_limit_not_before:
                raise PerptapeRateLimited(self._rate_limit_not_before)
            if (
                not force
                and self._feed is not None
                and self._cached_at is not None
                and now < self._feed.next_allowed_at
            ):
                return self._feed
            if self._server_not_before is not None and now < self._server_not_before:
                raise PerptapeRateLimited(self._server_not_before)
            query = urllib.parse.urlencode(
                {
                    "tf": "1h,4h,1d,1w",
                    "dir": "ALL",
                    "ex": "ALL",
                    "limit": "200",
                    "sort": "triggeredAt",
                }
            )
            try:
                value = self._fetcher(
                    f"{self._base_url}/api/v1/breakouts?{query}",
                    {
                        "authorization": f"Bearer {self._api_key}",
                        "x-api-key": self._api_key,
                        "user-agent": "trading-control-plane/1.0",
                    },
                    self._timeout_seconds,
                )
            except PerptapeRateLimited as exc:
                if exc.is_remote:
                    self._remote_result_generation += 1
                    self._remote_rate_limit_count += 1
                    self._consecutive_remote_rate_limits += 1
                if exc.next_allowed_at is not None:
                    self._rate_limit_not_before = max(
                        self._rate_limit_not_before or exc.next_allowed_at,
                        exc.next_allowed_at,
                    )
                    if self._feed is not None:
                        self._feed = replace(
                            self._feed,
                            next_allowed_at=max(
                                self._feed.next_allowed_at,
                                exc.next_allowed_at,
                            ),
                        )
                raise
            candidates = self._parse_response(value)
            generated_at, next_allowed_at = self._parse_feed_times(value, now=now)
            self._remote_result_generation += 1
            self._remote_success_generation = self._remote_result_generation
            self._consecutive_remote_rate_limits = 0
            self._rate_limit_not_before = None
            self._server_not_before = next_allowed_at
            self._cached_at = now
            self._cached = tuple(candidates)
            self._feed = PerptapeFeedSnapshot(
                contract_version=self._contract_version,
                generated_at=generated_at,
                fetched_at=now,
                next_allowed_at=max(now + self._cache_ttl, next_allowed_at),
                candidates=self._cached,
            )
            return self._feed

    def get_candidate(self, candidate_id: str, *, now: datetime) -> PerptapeCandidate:
        for candidate in self.list_candidates(now=now):
            if candidate.candidate_id == candidate_id:
                return candidate
        raise DomainRejected("PERPTAPE_CANDIDATE_NOT_FOUND", "candidate is no longer available")

    @staticmethod
    def _parse_feed_times(value: dict[str, Any], *, now: datetime) -> tuple[datetime, datetime]:
        try:
            generated_at = datetime.fromtimestamp(int(value["generatedAt"]) / 1000, UTC)
            rate_limit = value.get("rateLimit")
            next_allowed_at = (
                datetime.fromtimestamp(int(rate_limit["nextAllowedAt"]) / 1000, UTC)
                if isinstance(rate_limit, dict) and rate_limit.get("nextAllowedAt") is not None
                else now
            )
        except (KeyError, OSError, OverflowError, TypeError, ValueError) as exc:
            raise DomainRejected(
                "PERPTAPE_RESPONSE_INVALID",
                "Perptape feed timing metadata is invalid",
            ) from exc
        if generated_at > now + timedelta(seconds=30) or next_allowed_at < generated_at:
            raise DomainRejected(
                "PERPTAPE_RESPONSE_INVALID",
                "Perptape feed timing metadata is inconsistent",
            )
        return generated_at, next_allowed_at

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
            exchange_value = raw["exchange"]
            symbol_value = raw["symbol"]
            canonical_symbol_value = raw["canonicalSymbol"]
            source_direction_value = raw["direction"]
            timeframe_value = raw["timeframe"]
            if not all(
                isinstance(item, str) and item
                for item in (
                    exchange_value,
                    symbol_value,
                    canonical_symbol_value,
                    source_direction_value,
                    timeframe_value,
                )
            ):
                raise ValueError("candidate identity fields must be non-empty strings")
            exchange = exchange_value
            symbol = symbol_value
            canonical_symbol = canonical_symbol_value
            source_direction = source_direction_value
            timeframe = timeframe_value
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
        if (
            source_direction not in {"HH", "LL"}
            or timeframe not in {"1h", "4h", "1d", "1w"}
            or not price.is_finite()
            or price <= 0
        ):
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
            volume_raw = next(
                (
                    raw[key]
                    for key in ("volume24hQuote", "quoteVolume", "volume24h", "volume")
                    if raw.get(key) is not None
                ),
                None,
            )
            open_interest_raw = next(
                (
                    raw[key]
                    for key in ("openInterestQuote", "openInterest", "openInterestUsd", "oi")
                    if raw.get(key) is not None
                ),
                None,
            )
            quote_volume = None if volume_raw is None else Decimal(str(volume_raw))
            open_interest = None if open_interest_raw is None else Decimal(str(open_interest_raw))
        except (OSError, OverflowError, InvalidOperation, ValueError) as exc:
            raise DomainRejected(
                "PERPTAPE_RESPONSE_INVALID", "Perptape candidate contains invalid facts"
            ) from exc
        if (
            (threshold is not None and (not threshold.is_finite() or threshold <= 0))
            or (quote_volume is not None and (not quote_volume.is_finite() or quote_volume < 0))
            or (open_interest is not None and (not open_interest.is_finite() or open_interest < 0))
        ):
            raise DomainRejected(
                "PERPTAPE_RESPONSE_INVALID", "Perptape candidate contains invalid facts"
            )
        identity = ":".join(
            [exchange, canonical_symbol, timeframe, source_direction, str(triggered_at_ms or 0)]
        )
        candidate_id = "pt_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        candidate = PerptapeCandidate(
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
            detail_url=(
                f"{self._base_url}/breakouts?"
                + urllib.parse.urlencode(
                    {
                        "ex": exchange,
                        "q": f"{exchange}:{canonical_symbol}:{symbol}",
                        "utm_source": "trading_console",
                        "utm_medium": "opportunity",
                        "utm_campaign": "breakout_symbol",
                        "lang": "zh-CN",
                    }
                )
            ),
            quote_volume=quote_volume,
            open_interest=open_interest,
            chart_url=(
                f"https://www.binance.com/zh-CN/futures/{urllib.parse.quote(symbol)}"
                if venue == "BINANCE"
                else "https://app.hyperliquid.xyz/trade/" + urllib.parse.quote(canonical_symbol)
            ),
        )
        validate_perptape_candidate(candidate)
        return candidate

    def parse_stream_alert(
        self,
        payload: dict[str, Any],
        *,
        event_time: datetime,
        existing: PerptapeCandidate | None = None,
    ) -> PerptapeCandidate:
        """Map Perptape's documented short-field alert without inventing missing facts."""

        if payload.get("t") is None:
            raise DomainRejected(
                "PERPTAPE_STREAM_MESSAGE_INVALID",
                "Perptape alert trigger time is missing",
            )
        readiness = "unknown" if existing is None else existing.readiness.lower()
        kline_readiness = payload.get("kr")
        if isinstance(kline_readiness, dict):
            readiness = str(kline_readiness.get("status", "unknown"))
        raw = {
            "exchange": payload.get("ex"),
            "symbol": payload.get("s"),
            "canonicalSymbol": (
                payload.get("cs")
                if payload.get("cs") is not None
                else (payload.get("s") if existing is None else existing.canonical_symbol)
            ),
            "direction": payload.get("dir"),
            "timeframe": payload.get("tf"),
            "price": payload.get("p"),
            "threshold": (
                payload.get("th")
                if payload.get("th") is not None
                else (None if existing is None else existing.threshold)
            ),
            "triggeredAt": payload.get("t"),
            "updatedAt": payload.get("u", int(event_time.timestamp() * 1_000)),
            "klineReadiness": {"status": readiness},
            "volume24hQuote": (
                payload.get("vq24")
                if payload.get("vq24") is not None
                else (None if existing is None else existing.quote_volume)
            ),
            "openInterestQuote": (
                payload.get("oi")
                if payload.get("oi") is not None
                else (None if existing is None else existing.open_interest)
            ),
        }
        return self._parse_candidate(raw)
