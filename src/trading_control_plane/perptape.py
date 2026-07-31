from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_control_plane.domain import Direction, DomainRejected

PERPTAPE_OFFICIAL_HOST = "perptape.com"
PERPTAPE_WEBSOCKET_PATH = "/ws/v1/alerts"


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
        if (
            candidate.source != "PERPTAPE"
            or candidate.venue not in {"BINANCE", "HYPERLIQUID"}
            or candidate.source_direction not in {"HH", "LL"}
            or candidate.direction
            is not (Direction.LONG if candidate.source_direction == "HH" else Direction.SHORT)
            or candidate.reference_price <= 0
            or candidate.observed_at.tzinfo is None
        ):
            raise DomainRejected(
                "PERPTAPE_CACHE_INVALID",
                "persisted Perptape candidate is outside the supported contract",
            )
        return candidate


@dataclass(frozen=True)
class PerptapeFeedSnapshot:
    contract_version: str
    generated_at: datetime
    fetched_at: datetime
    next_allowed_at: datetime
    candidates: tuple[PerptapeCandidate, ...]


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
