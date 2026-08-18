from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import partial
from typing import Any, cast

from trading_control_plane.binance_errors import (
    BinanceApiRejected,
    BinanceRequestState,
    classify_binance_rate_limit,
)
from trading_control_plane.domain import DomainRejected

JsonValue = dict[str, Any] | list[dict[str, Any]]
JsonFetcher = Callable[[str, dict[str, str], float], JsonValue]
ServerTimeFetcher = Callable[[float], int]


def _binance_http_error(exc: urllib.error.HTTPError) -> tuple[int | None, str | None]:
    try:
        raw = json.loads(exc.read())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, None
    if not isinstance(raw, dict):
        return None, None
    raw_code = raw.get("code")
    try:
        code = (
            int(raw_code)
            if isinstance(raw_code, (int, str)) and not isinstance(raw_code, bool)
            else None
        )
    except (TypeError, ValueError):
        code = None
    message = None if raw.get("msg") is None else str(raw["msg"])
    return code, message


def _raise_rate_limit(
    exc: urllib.error.HTTPError,
    *,
    exchange_code: int | None,
    exchange_message: str | None,
    state: BinanceRequestState | None = None,
    host: str | None = None,
) -> None:
    diagnostic = classify_binance_rate_limit(
        http_status=exc.code,
        binance_error_code=exchange_code,
        binance_error_message=exchange_message,
        headers=cast(dict[str, str], exc.headers or {}),
    )
    if state is not None:
        state.record_rate_limit(diagnostic, host=host)
    label = {
        "ORDINARY_RATE_LIMIT": "ordinary request rate limit",
        "REQUEST_WEIGHT_EXCEEDED": "request weight limit",
        "IP_TEMPORARILY_BANNED": "temporary IP ban",
    }[diagnostic.category]
    raise BinanceApiRejected(
        "BINANCE_RATE_LIMITED",
        f"Binance {label}; retry after next_retry_at",
        diagnostic,
    ) from exc


def _enforce_request_state(state: BinanceRequestState | None) -> None:
    if state is None:
        return
    diagnostic = state.blocked_diagnostic()
    if diagnostic is None:
        return
    raise BinanceApiRejected(
        "BINANCE_RATE_LIMITED",
        "Binance deployment-wide cooldown is active until next_retry_at",
        diagnostic,
    )


def _server_time_from(
    base_url: str,
    timeout: float,
    *,
    state: BinanceRequestState | None = None,
) -> int:
    _enforce_request_state(state)
    host = urllib.parse.urlparse(base_url).hostname
    request = urllib.request.Request(  # noqa: S310 - caller pins the official Binance host
        f"{base_url.rstrip('/')}/fapi/v1/time",
        method="GET",
    )
    last_error: BaseException | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                raw = json.loads(response.read())
                if state is not None:
                    state.record_success(getattr(response, "headers", {}), host=host)
            return int(raw["serverTime"])
        except urllib.error.HTTPError as exc:
            exchange_code, exchange_message = _binance_http_error(exc)
            if exc.code in {418, 429} or exchange_code == -1003:
                _raise_rate_limit(
                    exc,
                    exchange_code=exchange_code,
                    exchange_message=exchange_message,
                    state=state,
                    host=host,
                )
            last_error = exc
            if attempt < 2 and exc.code >= 500:
                time.sleep(0.25 * 2**attempt)
                continue
            break
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.25 * 2**attempt)
                continue
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            last_error = exc
        break
    raise DomainRejected(
        "BINANCE_READ_ONLY_UNAVAILABLE", "Binance server time is unavailable"
    ) from last_error


def _default_server_time_fetcher(timeout: float) -> int:
    return _server_time_from("https://fapi.binance.com", timeout)


def _default_fetcher(
    url: str,
    headers: dict[str, str],
    timeout: float,
    *,
    state: BinanceRequestState | None = None,
) -> JsonValue:
    _enforce_request_state(state)
    host = urllib.parse.urlparse(url).hostname
    request = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
    body: bytes | None = None
    last_error: BaseException | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                body = response.read()
                if state is not None:
                    state.record_success(getattr(response, "headers", {}), host=host)
            break
        except urllib.error.HTTPError as exc:
            exchange_code, exchange_message = _binance_http_error(exc)
            if exc.code in {401, 403} or exchange_code in {-2014, -2015, -1022}:
                code = "BINANCE_AUTHENTICATION_FAILED"
            elif exchange_code == -1021:
                code = "BINANCE_TIMESTAMP_REJECTED"
            elif exc.code in {418, 429} or exchange_code == -1003:
                _raise_rate_limit(
                    exc,
                    exchange_code=exchange_code,
                    exchange_message=exchange_message,
                    state=state,
                    host=host,
                )
            else:
                code = "BINANCE_READ_ONLY_UNAVAILABLE"
            if exc.code >= 500 and attempt < 2:
                last_error = exc
                time.sleep(0.25 * 2**attempt)
                continue
            raise DomainRejected(code, "Binance read-only API rejected the request") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.25 * 2**attempt)
                continue
    if body is None:
        raise DomainRejected(
            "BINANCE_READ_ONLY_UNAVAILABLE", "Binance read-only API could not be reached"
        ) from last_error
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DomainRejected("BINANCE_RESPONSE_INVALID", "Binance returned invalid JSON") from exc
    if not isinstance(value, (dict, list)):
        raise DomainRejected("BINANCE_RESPONSE_INVALID", "Binance response shape is invalid")
    return value


@dataclass(frozen=True, slots=True)
class BinanceInstrument:
    symbol: str
    tick_size: Decimal
    lot_size: Decimal
    minimum_notional: Decimal
    quote_currency: str
    collateral_currency: str
    active: bool


@dataclass(frozen=True, slots=True)
class BinanceOrder:
    order_id: str
    client_order_id: str
    status: str
    side: str
    order_type: str
    ordered_quantity: Decimal
    filled_quantity: Decimal
    stop_price: Decimal
    reduce_only: bool
    close_position: bool
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class BinanceFill:
    fill_id: str
    order_id: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_currency: str
    executed_at: datetime


@dataclass(frozen=True, slots=True)
class BinancePosition:
    quantity: Decimal
    average_entry_price: Decimal
    mark_price: Decimal
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class BinanceEquity:
    equity: Decimal
    available_balance: Decimal
    currency: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class BinanceFunding:
    payment_id: str
    amount: Decimal
    currency: str
    paid_at: datetime


@dataclass(frozen=True, slots=True)
class BinanceProtection:
    order_id: str
    quantity: Decimal
    trigger_price: Decimal
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class BinanceReadOnlySnapshot:
    symbol: str
    observed_at: datetime
    instrument: BinanceInstrument
    orders: tuple[BinanceOrder, ...]
    fills: tuple[BinanceFill, ...]
    position: BinancePosition
    equity: BinanceEquity
    funding: tuple[BinanceFunding, ...]
    protection: BinanceProtection | None
    history_error_code: str | None = None


def _decimal(raw: Any, field: str, *, minimum: Decimal | None = None) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DomainRejected(
            "BINANCE_RESPONSE_INVALID", f"Binance field {field} is not numeric"
        ) from exc
    if minimum is not None and value < minimum:
        raise DomainRejected(
            "BINANCE_RESPONSE_INVALID", f"Binance field {field} is below its minimum"
        )
    return value


def _positive_decimal(raw: Any, field: str) -> Decimal:
    value = _decimal(raw, field)
    if value <= 0:
        raise DomainRejected("BINANCE_RESPONSE_INVALID", f"Binance field {field} must be positive")
    return value


def _time(raw: Any, fallback: datetime) -> datetime:
    try:
        milliseconds = int(raw)
        return fallback if milliseconds <= 0 else datetime.fromtimestamp(milliseconds / 1000, UTC)
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        raise DomainRejected("BINANCE_RESPONSE_INVALID", "Binance timestamp is invalid") from exc


class BinanceReadOnlyClient:
    """Narrow USDⓈ-M Futures USER_DATA reader; it exposes no write method."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        api_secret: str | None,
        recv_window_ms: int = 5_000,
        fetcher: JsonFetcher = _default_fetcher,
        server_time_fetcher: ServerTimeFetcher | None = None,
        request_state: BinanceRequestState | None = None,
        clock_ms: Callable[[], int] | None = None,
        exchange_info_cache_seconds: int = 1_800,
        history_refresh_seconds: int = 600,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._recv_window_ms = recv_window_ms
        self._request_state = request_state or BinanceRequestState()
        self._fetcher_records_state = fetcher is _default_fetcher
        self._fetcher = (
            partial(_default_fetcher, state=self._request_state)
            if fetcher is _default_fetcher
            else fetcher
        )
        self._server_time_records_state = server_time_fetcher is None
        self._server_time_fetcher = server_time_fetcher or partial(
            _server_time_from, self._base_url, state=self._request_state
        )
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._exchange_info_cache_seconds = max(1_800, exchange_info_cache_seconds)
        self._exchange_info_cache: JsonValue | None = None
        self._exchange_info_cached_at: datetime | None = None
        self._symbol_exchange_info_cache: dict[str, tuple[JsonValue, datetime]] = {}
        self._history_refresh_seconds = min(900, max(300, history_refresh_seconds))
        self._history_last_requested_at: datetime | None = None
        self._history_cursors: dict[str, dict[str, datetime | None]] = {}

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self._api_secret)

    def read_active_instruments(self) -> tuple[BinanceInstrument, ...]:
        """Read the complete official USDⓈ-M perpetual catalog without credentials."""

        exchange = self._exchange_info()
        return self._parse_active_instruments(exchange)

    def set_history_cursors(self, cursors: Mapping[str, Mapping[str, datetime | None]]) -> None:
        self._history_cursors = {
            symbol: {
                "fills": values.get("fills"),
                "funding": values.get("funding"),
            }
            for symbol, values in cursors.items()
        }

    def _exchange_info(self, *, now: datetime | None = None) -> JsonValue:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if (
            self._exchange_info_cache is not None
            and self._exchange_info_cached_at is not None
            and (current - self._exchange_info_cached_at).total_seconds()
            < self._exchange_info_cache_seconds
        ):
            return self._exchange_info_cache
        exchange = self._public_get("/fapi/v1/exchangeInfo", {})
        self._exchange_info_cache = exchange
        self._exchange_info_cached_at = current
        return exchange

    def _exchange_info_for_symbol(self, symbol: str, *, now: datetime) -> JsonValue:
        current = now.astimezone(UTC)
        if (
            self._exchange_info_cache is not None
            and self._exchange_info_cached_at is not None
            and (current - self._exchange_info_cached_at).total_seconds()
            < self._exchange_info_cache_seconds
        ):
            return self._exchange_info_cache
        cached = self._symbol_exchange_info_cache.get(symbol)
        if (
            cached is not None
            and (current - cached[1]).total_seconds() < self._exchange_info_cache_seconds
        ):
            return cached[0]
        exchange = self._public_get("/fapi/v1/exchangeInfo", {"symbol": symbol})
        self._symbol_exchange_info_cache[symbol] = (exchange, current)
        return exchange

    def _timestamp_ms(self) -> int:
        _enforce_request_state(self._request_state)
        now = datetime.now(UTC)
        cached = self._request_state.current_time_offset()
        if cached is not None and now - cached[1] <= timedelta(seconds=30):
            return self._clock_ms() + cached[0]
        try:
            server_time = self._server_time_fetcher(5.0)
        except BinanceApiRejected as exc:
            self._request_state.record_rate_limit(exc.diagnostic)
            raise
        if not self._server_time_records_state:
            self._request_state.record_success()
        self._request_state.record_time_offset(server_time - self._clock_ms(), synchronized_at=now)
        return server_time

    def _history_due(self, now: datetime) -> bool:
        return (
            self._history_last_requested_at is None
            or (now - self._history_last_requested_at).total_seconds()
            >= self._history_refresh_seconds
        )

    def _history_params(self, symbol: str, kind: str) -> dict[str, str]:
        params = {"symbol": symbol, "limit": "1000"}
        cursor = self._history_cursors.get(symbol, {}).get(kind)
        if cursor is not None:
            overlap = cursor.astimezone(UTC) - timedelta(minutes=5)
            params["startTime"] = str(max(0, int(overlap.timestamp() * 1000)))
        return params

    def _assert_low_priority_allowed(self, now: datetime) -> None:
        retry_at = self._request_state.low_priority_retry_at(now=now)
        if retry_at is not None and retry_at > now:
            raise DomainRejected(
                "BINANCE_HISTORY_DEFERRED_WEIGHT_HEADROOM",
                "Binance history reads are deferred to preserve request-weight headroom",
                metadata={"next_retry_at": retry_at.isoformat()},
            )

    def _public_get(self, path: str, params: dict[str, str]) -> JsonValue:
        _enforce_request_state(self._request_state)
        query = urllib.parse.urlencode(params)
        try:
            result = self._fetcher(f"{self._base_url}{path}?{query}", {}, 5.0)
        except BinanceApiRejected as exc:
            self._request_state.record_rate_limit(
                exc.diagnostic, host=urllib.parse.urlparse(self._base_url).hostname
            )
            raise
        if not self._fetcher_records_state:
            self._request_state.record_success()
        return result

    def _signed_get(self, path: str, params: dict[str, str], *, timestamp_ms: int) -> JsonValue:
        _enforce_request_state(self._request_state)
        if not self.configured:
            raise DomainRejected(
                "BINANCE_READ_ONLY_NOT_CONFIGURED",
                "Binance read-only credentials are not configured",
            )
        signed = {
            **params,
            "recvWindow": str(self._recv_window_ms),
            "timestamp": str(timestamp_ms),
        }
        query = urllib.parse.urlencode(signed)
        assert self._api_secret is not None
        signature = hmac.new(self._api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        assert self._api_key is not None
        try:
            result = self._fetcher(
                f"{self._base_url}{path}?{query}&signature={signature}",
                {"X-MBX-APIKEY": self._api_key},
                5.0,
            )
        except BinanceApiRejected as exc:
            self._request_state.record_rate_limit(
                exc.diagnostic, host=urllib.parse.urlparse(self._base_url).hostname
            )
            raise
        if not self._fetcher_records_state:
            self._request_state.record_success()
        return result

    def verify_connection(self, *, now: datetime) -> None:
        """Authenticate one USD-M account read without persisting account facts."""

        del now
        raw = self._signed_get(
            "/fapi/v3/balance",
            {},
            timestamp_ms=self._timestamp_ms(),
        )
        if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
            raise DomainRejected(
                "BINANCE_RESPONSE_INVALID", "Binance account balance response is invalid"
            )

    def read_snapshot(self, symbol: str, *, now: datetime) -> BinanceReadOnlySnapshot:
        if not symbol or symbol != symbol.upper():
            raise DomainRejected("BINANCE_SYMBOL_INVALID", "Binance symbol must be uppercase")
        timestamp_ms = int(now.timestamp() * 1000)
        exchange = self._exchange_info_for_symbol(symbol, now=now)
        position = self._signed_get(
            "/fapi/v3/positionRisk", {"symbol": symbol}, timestamp_ms=timestamp_ms
        )
        balance = self._signed_get("/fapi/v3/balance", {}, timestamp_ms=timestamp_ms)
        orders = self._signed_get(
            "/fapi/v1/openOrders", {"symbol": symbol}, timestamp_ms=timestamp_ms
        )
        fills = self._signed_get(
            "/fapi/v1/userTrades", {"symbol": symbol, "limit": "1000"}, timestamp_ms=timestamp_ms
        )
        funding = self._signed_get(
            "/fapi/v1/income",
            {"symbol": symbol, "incomeType": "FUNDING_FEE", "limit": "1000"},
            timestamp_ms=timestamp_ms,
        )
        instrument = self._parse_instrument(exchange, symbol)
        parsed_orders = self._parse_orders(orders, symbol, now)
        parsed_position = self._parse_position(position, symbol, now)
        return BinanceReadOnlySnapshot(
            symbol=symbol,
            observed_at=now,
            instrument=instrument,
            orders=parsed_orders,
            fills=self._parse_fills(fills, symbol, now),
            position=parsed_position,
            equity=self._parse_equity(balance, instrument.collateral_currency, now),
            funding=self._parse_funding(funding, symbol, now),
            protection=self._select_protection(parsed_orders, parsed_position),
        )

    def read_account_snapshots(
        self, symbols: tuple[str, ...], *, now: datetime
    ) -> tuple[BinanceReadOnlySnapshot, ...]:
        """Read one complete account position/open-order snapshot and project its symbols."""

        configured = self._validated_symbols(symbols)
        timestamp_ms = int(now.timestamp() * 1000)
        positions = self._signed_get("/fapi/v3/positionRisk", {}, timestamp_ms=timestamp_ms)
        balance = self._signed_get("/fapi/v3/balance", {}, timestamp_ms=timestamp_ms)
        orders = self._signed_get("/fapi/v1/openOrders", {}, timestamp_ms=timestamp_ms)
        active_symbols = self._active_position_symbols(positions, container="positionRisk")
        target_symbols = configured | active_symbols | self._order_symbols(orders, "openOrders")
        current_snapshots: list[BinanceReadOnlySnapshot] = []
        for symbol in sorted(target_symbols):
            exchange = self._exchange_info_for_symbol(symbol, now=now)
            instrument = self._parse_instrument(exchange, symbol)
            parsed_orders = self._parse_orders(orders, symbol, now)
            position = self._parse_position_or_flat(positions, symbol, now)
            current_snapshots.append(
                BinanceReadOnlySnapshot(
                    symbol=symbol,
                    observed_at=now,
                    instrument=instrument,
                    orders=parsed_orders,
                    fills=(),
                    position=position,
                    equity=self._parse_equity(balance, instrument.collateral_currency, now),
                    funding=(),
                    protection=self._select_protection(parsed_orders, position),
                )
            )
        if not self._history_due(now):
            return tuple(current_snapshots)
        self._history_last_requested_at = now
        try:
            self._assert_low_priority_allowed(now)
            snapshots: list[BinanceReadOnlySnapshot] = []
            for snapshot in current_snapshots:
                fills = self._signed_get(
                    "/fapi/v1/userTrades",
                    self._history_params(snapshot.symbol, "fills"),
                    timestamp_ms=timestamp_ms,
                )
                funding = self._signed_get(
                    "/fapi/v1/income",
                    {
                        **self._history_params(snapshot.symbol, "funding"),
                        "incomeType": "FUNDING_FEE",
                    },
                    timestamp_ms=timestamp_ms,
                )
                self._require_untruncated(fills, "userTrades", limit=1000)
                self._require_untruncated(funding, "income", limit=1000)
                snapshots.append(
                    replace(
                        snapshot,
                        fills=self._parse_fills(fills, snapshot.symbol, now),
                        funding=self._parse_funding(funding, snapshot.symbol, now),
                    )
                )
        except DomainRejected as exc:
            return tuple(
                replace(snapshot, history_error_code=exc.code) for snapshot in current_snapshots
            )
        return tuple(snapshots)

    @staticmethod
    def _validated_symbols(symbols: tuple[str, ...]) -> set[str]:
        result = set(symbols)
        if not result or any(not symbol or symbol != symbol.upper() for symbol in result):
            raise DomainRejected("BINANCE_SYMBOL_INVALID", "Binance symbol must be uppercase")
        return result

    @classmethod
    def _active_position_symbols(cls, raw: JsonValue, *, container: str) -> set[str]:
        rows = cls._require_list(raw, container)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in rows:
            symbol = item.get("symbol")
            if not isinstance(symbol, str) or not symbol or symbol != symbol.upper():
                raise DomainRejected(
                    "BINANCE_RESPONSE_INVALID", "Binance position symbol is invalid"
                )
            grouped.setdefault(symbol, []).append(item)
        active: set[str] = set()
        for symbol, symbol_rows in grouped.items():
            both = [item for item in symbol_rows if item.get("positionSide", "BOTH") == "BOTH"]
            if len(both) > 1:
                raise DomainRejected(
                    "BINANCE_RESPONSE_INVALID", "Binance position response contains duplicates"
                )
            nonzero_hedged = any(
                item.get("positionSide", "BOTH") != "BOTH"
                and _decimal(item.get("positionAmt"), "positionAmt") != 0
                for item in symbol_rows
            )
            if nonzero_hedged:
                raise DomainRejected(
                    "BINANCE_HEDGE_MODE_UNSUPPORTED",
                    "read-only account sync requires one-way positions",
                )
            if both and _decimal(both[0].get("positionAmt"), "positionAmt") != 0:
                active.add(symbol)
        return active

    @classmethod
    def _order_symbols(cls, raw: JsonValue, name: str) -> set[str]:
        result: set[str] = set()
        for item in cls._require_list(raw, name):
            symbol = item.get("symbol")
            if not isinstance(symbol, str) or not symbol or symbol != symbol.upper():
                raise DomainRejected("BINANCE_RESPONSE_INVALID", "Binance order symbol is invalid")
            result.add(symbol)
        return result

    @classmethod
    def _require_untruncated(cls, raw: JsonValue, name: str, *, limit: int) -> None:
        if len(cls._require_list(raw, name)) >= limit:
            raise DomainRejected(
                "BINANCE_RESPONSE_INCOMPLETE",
                f"Binance {name} reached its requested page limit",
            )

    def _parse_position_or_flat(
        self, raw: JsonValue, symbol: str, now: datetime
    ) -> BinancePosition:
        rows = [
            item for item in self._require_list(raw, "positionRisk") if item.get("symbol") == symbol
        ]
        if rows:
            return self._parse_position(rows, symbol, now)
        raw_mark = self._public_get("/fapi/v1/premiumIndex", {"symbol": symbol})
        if not isinstance(raw_mark, dict) or raw_mark.get("symbol") != symbol:
            raise DomainRejected("BINANCE_RESPONSE_INVALID", "Binance mark price is invalid")
        mark = _positive_decimal(raw_mark.get("markPrice"), "markPrice")
        return BinancePosition(Decimal(0), Decimal(0), mark, now)

    @staticmethod
    def _parse_instrument(raw: JsonValue, symbol: str) -> BinanceInstrument:
        if not isinstance(raw, dict) or not isinstance(raw.get("symbols"), list):
            raise DomainRejected("BINANCE_RESPONSE_INVALID", "exchangeInfo is invalid")
        item = next(
            (
                value
                for value in raw["symbols"]
                if isinstance(value, dict) and value.get("symbol") == symbol
            ),
            None,
        )
        if item is None or item.get("contractType") != "PERPETUAL":
            raise DomainRejected(
                "BINANCE_INSTRUMENT_UNAVAILABLE", "USDⓈ-M perpetual instrument is unavailable"
            )
        filters = {
            value.get("filterType"): value
            for value in item.get("filters", [])
            if isinstance(value, dict)
        }
        price_filter = filters.get("PRICE_FILTER", {})
        lot_filter = filters.get("LOT_SIZE", {})
        notional_filter = filters.get("MIN_NOTIONAL", {})
        return BinanceInstrument(
            symbol=symbol,
            tick_size=_positive_decimal(price_filter.get("tickSize"), "tickSize"),
            lot_size=_positive_decimal(lot_filter.get("stepSize"), "stepSize"),
            minimum_notional=_positive_decimal(notional_filter.get("notional"), "minimumNotional"),
            quote_currency=str(item.get("quoteAsset", "")),
            collateral_currency=str(item.get("marginAsset", "")),
            active=item.get("status") == "TRADING",
        )

    @classmethod
    def _parse_active_instruments(cls, raw: JsonValue) -> tuple[BinanceInstrument, ...]:
        if not isinstance(raw, dict) or not isinstance(raw.get("symbols"), list):
            raise DomainRejected("BINANCE_RESPONSE_INVALID", "exchangeInfo is invalid")
        symbols: list[str] = []
        for item in raw["symbols"]:
            if not isinstance(item, dict):
                raise DomainRejected("BINANCE_RESPONSE_INVALID", "exchangeInfo symbol is invalid")
            if item.get("contractType") != "PERPETUAL" or item.get("status") != "TRADING":
                continue
            symbol = item.get("symbol")
            if not isinstance(symbol, str) or not symbol or symbol != symbol.upper():
                raise DomainRejected("BINANCE_RESPONSE_INVALID", "exchangeInfo symbol is invalid")
            symbols.append(symbol)
        if not symbols or len(symbols) != len(set(symbols)):
            raise DomainRejected("BINANCE_RESPONSE_INVALID", "active perpetual catalog is invalid")
        return tuple(cls._parse_instrument(raw, symbol) for symbol in sorted(symbols))

    @staticmethod
    def _require_list(raw: JsonValue, name: str) -> list[dict[str, Any]]:
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise DomainRejected("BINANCE_RESPONSE_INVALID", f"{name} response is invalid")
        return raw

    @classmethod
    def _parse_orders(cls, raw: JsonValue, symbol: str, now: datetime) -> tuple[BinanceOrder, ...]:
        status_map = {
            "NEW": "SENT",
            "PARTIALLY_FILLED": "PARTIALLY_FILLED",
            "FILLED": "FILLED",
            "CANCELED": "CANCELLED",
            "EXPIRED": "CANCELLED",
            "EXPIRED_IN_MATCH": "CANCELLED",
            "REJECTED": "REJECTED",
        }
        result: list[BinanceOrder] = []
        for item in cls._require_list(raw, "openOrders"):
            if item.get("symbol") != symbol:
                continue
            try:
                order_id = str(item["orderId"])
                client_order_id = str(item["clientOrderId"])
                side = str(item["side"])
                order_type = str(item["type"])
            except KeyError as exc:
                raise DomainRejected(
                    "BINANCE_RESPONSE_INVALID", "Binance order identity is incomplete"
                ) from exc
            if side not in {"BUY", "SELL"}:
                raise DomainRejected("BINANCE_RESPONSE_INVALID", "Binance order side is invalid")
            result.append(
                BinanceOrder(
                    order_id=order_id,
                    client_order_id=client_order_id,
                    status=status_map.get(str(item.get("status")), "UNKNOWN"),
                    side=side,
                    order_type=order_type,
                    ordered_quantity=_decimal(item.get("origQty"), "origQty", minimum=Decimal(0)),
                    filled_quantity=_decimal(
                        item.get("executedQty"), "executedQty", minimum=Decimal(0)
                    ),
                    stop_price=_decimal(item.get("stopPrice", 0), "stopPrice", minimum=Decimal(0)),
                    reduce_only=bool(item.get("reduceOnly", False)),
                    close_position=bool(item.get("closePosition", False)),
                    observed_at=_time(item.get("updateTime", 0), now),
                )
            )
        return tuple(result)

    @classmethod
    def _parse_fills(cls, raw: JsonValue, symbol: str, now: datetime) -> tuple[BinanceFill, ...]:
        result: list[BinanceFill] = []
        for item in cls._require_list(raw, "userTrades"):
            if item.get("symbol") != symbol:
                continue
            side = str(item.get("side"))
            if side not in {"BUY", "SELL"}:
                raise DomainRejected("BINANCE_RESPONSE_INVALID", "Binance fill side is invalid")
            result.append(
                BinanceFill(
                    fill_id=str(item.get("id")),
                    order_id=str(item.get("orderId")),
                    side=side,
                    quantity=_positive_decimal(item.get("qty"), "qty"),
                    price=_positive_decimal(item.get("price"), "price"),
                    fee=_decimal(item.get("commission"), "commission", minimum=Decimal(0)),
                    fee_currency=str(item.get("commissionAsset", "")),
                    executed_at=_time(item.get("time", 0), now),
                )
            )
        return tuple(result)

    @classmethod
    def _parse_position(cls, raw: JsonValue, symbol: str, now: datetime) -> BinancePosition:
        rows = [
            item for item in cls._require_list(raw, "positionRisk") if item.get("symbol") == symbol
        ]
        both = next((item for item in rows if item.get("positionSide", "BOTH") == "BOTH"), None)
        if both is None:
            nonzero = [
                item for item in rows if _decimal(item.get("positionAmt"), "positionAmt") != 0
            ]
            if nonzero:
                raise DomainRejected(
                    "BINANCE_HEDGE_MODE_UNSUPPORTED",
                    "M3 read-only sync cannot collapse hedge-mode positions into one net fact",
                )
            both = rows[0] if rows else None
        if both is None:
            raise DomainRejected("BINANCE_POSITION_UNKNOWN", "position response omitted the symbol")
        return BinancePosition(
            quantity=_decimal(both.get("positionAmt"), "positionAmt"),
            average_entry_price=_decimal(both.get("entryPrice"), "entryPrice", minimum=Decimal(0)),
            mark_price=_positive_decimal(both.get("markPrice"), "markPrice"),
            observed_at=_time(both.get("updateTime", 0), now),
        )

    @classmethod
    def _parse_equity(cls, raw: JsonValue, currency: str, now: datetime) -> BinanceEquity:
        item = next(
            (
                value
                for value in cls._require_list(raw, "balance")
                if value.get("asset") == currency
            ),
            None,
        )
        if item is None:
            raise DomainRejected("BINANCE_EQUITY_UNKNOWN", "collateral balance is unavailable")
        wallet = _decimal(item.get("balance"), "balance", minimum=Decimal(0))
        unrealized = _decimal(item.get("crossUnPnl", 0), "crossUnPnl")
        return BinanceEquity(
            equity=wallet + unrealized,
            available_balance=_decimal(
                item.get("availableBalance"), "availableBalance", minimum=Decimal(0)
            ),
            currency=currency,
            observed_at=_time(item.get("updateTime", 0), now),
        )

    @classmethod
    def _parse_funding(
        cls, raw: JsonValue, symbol: str, now: datetime
    ) -> tuple[BinanceFunding, ...]:
        return tuple(
            BinanceFunding(
                payment_id=str(item.get("tranId")),
                amount=_decimal(item.get("income"), "income"),
                currency=str(item.get("asset", "")),
                paid_at=_time(item.get("time", 0), now),
            )
            for item in cls._require_list(raw, "income")
            if item.get("symbol") == symbol and item.get("incomeType") == "FUNDING_FEE"
        )

    @staticmethod
    def _select_protection(
        orders: tuple[BinanceOrder, ...], position: BinancePosition
    ) -> BinanceProtection | None:
        if position.quantity == 0:
            return None
        protective_side = "SELL" if position.quantity > 0 else "BUY"
        types = {
            "STOP",
            "STOP_MARKET",
            "TAKE_PROFIT",
            "TAKE_PROFIT_MARKET",
            "TRAILING_STOP_MARKET",
        }
        candidates = [
            order
            for order in orders
            if order.side == protective_side
            and order.order_type in types
            and (order.reduce_only or order.close_position)
            and order.status in {"SENT", "PARTIALLY_FILLED"}
        ]
        if not candidates:
            return None
        order = max(candidates, key=lambda item: item.observed_at)
        quantity = abs(position.quantity) if order.close_position else order.ordered_quantity
        return BinanceProtection(order.order_id, quantity, order.stop_price, order.observed_at)


class BinancePortfolioMarginReadOnlyClient:
    """Binance Unified Account UM reader using Portfolio Margin USER_DATA endpoints."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        api_secret: str | None,
        recv_window_ms: int = 10_000,
        fetcher: JsonFetcher = _default_fetcher,
        server_time_fetcher: ServerTimeFetcher = _default_server_time_fetcher,
        request_state: BinanceRequestState | None = None,
        clock_ms: Callable[[], int] | None = None,
        exchange_info_cache_seconds: int = 1_800,
        history_refresh_seconds: int = 600,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname != "papi.binance.com":
            raise ValueError(
                "Binance Portfolio Margin base URL must be the official LIVE PAPI host"
            )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._recv_window_ms = recv_window_ms
        self._request_state = request_state or BinanceRequestState()
        self._fetcher_records_state = fetcher is _default_fetcher
        self._fetcher = (
            partial(_default_fetcher, state=self._request_state)
            if fetcher is _default_fetcher
            else fetcher
        )
        self._server_time_records_state = server_time_fetcher is _default_server_time_fetcher
        self._server_time_fetcher = (
            partial(_server_time_from, "https://fapi.binance.com", state=self._request_state)
            if self._server_time_records_state
            else server_time_fetcher
        )
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._exchange_info_cache_seconds = max(1_800, exchange_info_cache_seconds)
        self._exchange_info_cache: JsonValue | None = None
        self._exchange_info_cached_at: datetime | None = None
        self._symbol_exchange_info_cache: dict[str, tuple[JsonValue, datetime]] = {}
        self._history_refresh_seconds = min(900, max(300, history_refresh_seconds))
        self._history_last_requested_at: datetime | None = None
        self._history_cursors: dict[str, dict[str, datetime | None]] = {}

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self._api_secret)

    def read_active_instruments(self) -> tuple[BinanceInstrument, ...]:
        exchange = self._exchange_info()
        return BinanceReadOnlyClient._parse_active_instruments(exchange)

    def set_history_cursors(self, cursors: Mapping[str, Mapping[str, datetime | None]]) -> None:
        self._history_cursors = {
            symbol: {
                "fills": values.get("fills"),
                "funding": values.get("funding"),
            }
            for symbol, values in cursors.items()
        }

    def _exchange_info(self, *, now: datetime | None = None) -> JsonValue:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if (
            self._exchange_info_cache is not None
            and self._exchange_info_cached_at is not None
            and (current - self._exchange_info_cached_at).total_seconds()
            < self._exchange_info_cache_seconds
        ):
            return self._exchange_info_cache
        exchange = self._market_get("/fapi/v1/exchangeInfo", {})
        self._exchange_info_cache = exchange
        self._exchange_info_cached_at = current
        return exchange

    def _exchange_info_for_symbol(self, symbol: str, *, now: datetime) -> JsonValue:
        current = now.astimezone(UTC)
        if (
            self._exchange_info_cache is not None
            and self._exchange_info_cached_at is not None
            and (current - self._exchange_info_cached_at).total_seconds()
            < self._exchange_info_cache_seconds
        ):
            return self._exchange_info_cache
        cached = self._symbol_exchange_info_cache.get(symbol)
        if (
            cached is not None
            and (current - cached[1]).total_seconds() < self._exchange_info_cache_seconds
        ):
            return cached[0]
        exchange = self._market_get("/fapi/v1/exchangeInfo", {"symbol": symbol})
        self._symbol_exchange_info_cache[symbol] = (exchange, current)
        return exchange

    def _timestamp_ms(self) -> int:
        _enforce_request_state(self._request_state)
        now = datetime.now(UTC)
        cached = self._request_state.current_time_offset()
        if cached is not None and now - cached[1] <= timedelta(seconds=30):
            return self._clock_ms() + cached[0]
        try:
            server_time = self._server_time_fetcher(5.0)
        except BinanceApiRejected as exc:
            self._request_state.record_rate_limit(exc.diagnostic)
            raise
        if not self._server_time_records_state:
            self._request_state.record_success()
        self._request_state.record_time_offset(server_time - self._clock_ms(), synchronized_at=now)
        return server_time

    def _history_due(self, now: datetime) -> bool:
        return (
            self._history_last_requested_at is None
            or (now - self._history_last_requested_at).total_seconds()
            >= self._history_refresh_seconds
        )

    def _history_params(self, symbol: str, kind: str) -> dict[str, str]:
        params = {"symbol": symbol, "limit": "1000"}
        cursor = self._history_cursors.get(symbol, {}).get(kind)
        if cursor is not None:
            overlap = cursor.astimezone(UTC) - timedelta(minutes=5)
            params["startTime"] = str(max(0, int(overlap.timestamp() * 1000)))
        return params

    def _assert_low_priority_allowed(self, now: datetime) -> None:
        retry_at = self._request_state.low_priority_retry_at(now=now)
        if retry_at is not None and retry_at > now:
            raise DomainRejected(
                "BINANCE_HISTORY_DEFERRED_WEIGHT_HEADROOM",
                "Binance history reads are deferred to preserve request-weight headroom",
                metadata={"next_retry_at": retry_at.isoformat()},
            )

    def _signed_get(self, path: str, params: dict[str, str], *, timestamp_ms: int) -> JsonValue:
        _enforce_request_state(self._request_state)
        if not self.configured:
            raise DomainRejected(
                "BINANCE_READ_ONLY_NOT_CONFIGURED",
                "Binance Portfolio Margin credentials are not configured",
            )
        signed = {
            **params,
            "recvWindow": str(self._recv_window_ms),
            "timestamp": str(timestamp_ms),
        }
        query = urllib.parse.urlencode(signed)
        assert self._api_secret is not None
        signature = hmac.new(self._api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        assert self._api_key is not None
        try:
            result = self._fetcher(
                f"{self._base_url}{path}?{query}&signature={signature}",
                {"X-MBX-APIKEY": self._api_key},
                5.0,
            )
        except BinanceApiRejected as exc:
            self._request_state.record_rate_limit(
                exc.diagnostic, host=urllib.parse.urlparse(self._base_url).hostname
            )
            raise
        if not self._fetcher_records_state:
            self._request_state.record_success()
        return result

    def _market_get(self, path: str, params: dict[str, str]) -> JsonValue:
        _enforce_request_state(self._request_state)
        query = urllib.parse.urlencode(params)
        try:
            result = self._fetcher(f"https://fapi.binance.com{path}?{query}", {}, 5.0)
        except BinanceApiRejected as exc:
            self._request_state.record_rate_limit(exc.diagnostic, host="fapi.binance.com")
            raise
        if not self._fetcher_records_state:
            self._request_state.record_success(host="fapi.binance.com")
        return result

    def verify_connection(self, *, now: datetime) -> None:
        """Authenticate one Portfolio Margin account read without ingesting facts."""

        del now
        timestamp_ms = self._timestamp_ms()
        raw = self._signed_get("/papi/v1/account", {}, timestamp_ms=timestamp_ms)
        if not isinstance(raw, dict) or not isinstance(raw.get("totalWalletBalance"), str):
            raise DomainRejected(
                "BINANCE_RESPONSE_INVALID",
                "Binance Portfolio Margin account response is invalid",
            )

    def read_snapshot(self, symbol: str, *, now: datetime) -> BinanceReadOnlySnapshot:
        del now
        if not symbol or symbol != symbol.upper():
            raise DomainRejected("BINANCE_SYMBOL_INVALID", "Binance symbol must be uppercase")
        timestamp_ms = self._timestamp_ms()
        observed_at = datetime.fromtimestamp(timestamp_ms / 1000, UTC)
        exchange = self._exchange_info_for_symbol(symbol, now=observed_at)
        mark = self._market_get("/fapi/v1/premiumIndex", {"symbol": symbol})
        um_account = self._signed_get("/papi/v1/um/account", {}, timestamp_ms=timestamp_ms)
        account = self._signed_get("/papi/v1/account", {}, timestamp_ms=timestamp_ms)
        orders = self._signed_get(
            "/papi/v1/um/openOrders", {"symbol": symbol}, timestamp_ms=timestamp_ms
        )
        algo_orders = self._signed_get(
            "/papi/v1/um/algo/openAlgoOrders",
            {"symbol": symbol, "algoType": "CONDITIONAL"},
            timestamp_ms=timestamp_ms,
        )
        fills = self._signed_get(
            "/papi/v1/um/userTrades",
            {"symbol": symbol, "limit": "1000"},
            timestamp_ms=timestamp_ms,
        )
        funding = self._signed_get(
            "/papi/v1/um/income",
            {"symbol": symbol, "incomeType": "FUNDING_FEE", "limit": "1000"},
            timestamp_ms=timestamp_ms,
        )
        instrument = BinanceReadOnlyClient._parse_instrument(exchange, symbol)
        position = self._parse_position(um_account, mark, symbol, observed_at)
        parsed_orders = BinanceReadOnlyClient._parse_orders(
            orders, symbol, observed_at
        ) + self._parse_algo_orders(algo_orders, symbol, observed_at)
        return BinanceReadOnlySnapshot(
            symbol=symbol,
            observed_at=observed_at,
            instrument=instrument,
            orders=parsed_orders,
            fills=BinanceReadOnlyClient._parse_fills(fills, symbol, observed_at),
            position=position,
            equity=self._parse_equity(account, instrument.collateral_currency, observed_at),
            funding=BinanceReadOnlyClient._parse_funding(funding, symbol, observed_at),
            protection=BinanceReadOnlyClient._select_protection(parsed_orders, position),
        )

    def read_account_snapshots(
        self, symbols: tuple[str, ...], *, now: datetime
    ) -> tuple[BinanceReadOnlySnapshot, ...]:
        """Read the authoritative UM account positions once, then enrich each covered symbol."""

        configured = BinanceReadOnlyClient._validated_symbols(symbols)
        timestamp_ms = self._timestamp_ms()
        observed_at = datetime.fromtimestamp(timestamp_ms / 1000, UTC)
        um_account = self._signed_get("/papi/v1/um/account", {}, timestamp_ms=timestamp_ms)
        account = self._signed_get("/papi/v1/account", {}, timestamp_ms=timestamp_ms)
        if not isinstance(um_account, dict) or not isinstance(um_account.get("positions"), list):
            raise DomainRejected(
                "BINANCE_RESPONSE_INVALID", "Portfolio Margin UM account is invalid"
            )
        position_rows: JsonValue = um_account["positions"]
        active_symbols = BinanceReadOnlyClient._active_position_symbols(
            position_rows, container="Portfolio Margin UM positions"
        )
        orders = self._signed_get("/papi/v1/um/openOrders", {}, timestamp_ms=timestamp_ms)
        algo_orders = self._signed_get(
            "/papi/v1/um/algo/openAlgoOrders",
            {"algoType": "CONDITIONAL"},
            timestamp_ms=timestamp_ms,
        )
        target_symbols = (
            configured
            | active_symbols
            | BinanceReadOnlyClient._order_symbols(orders, "openOrders")
            | BinanceReadOnlyClient._order_symbols(algo_orders, "openAlgoOrders")
        )
        market: dict[str, tuple[BinanceInstrument, JsonValue]] = {}
        for symbol in sorted(target_symbols):
            exchange = self._exchange_info_for_symbol(symbol, now=now)
            mark = self._market_get("/fapi/v1/premiumIndex", {"symbol": symbol})
            market[symbol] = (BinanceReadOnlyClient._parse_instrument(exchange, symbol), mark)
        collateral_currencies = {
            instrument.collateral_currency for instrument, _mark in market.values()
        }
        if len(collateral_currencies) != 1:
            raise DomainRejected(
                "BINANCE_ACCOUNT_EQUITY_AMBIGUOUS",
                "Portfolio Margin aggregate equity cannot cover multiple collateral currencies",
            )
        equity_currency = next(iter(collateral_currencies))

        current_snapshots: list[BinanceReadOnlySnapshot] = []
        for symbol in sorted(target_symbols):
            instrument, mark = market[symbol]
            position = self._parse_position(um_account, mark, symbol, observed_at)
            parsed_orders = BinanceReadOnlyClient._parse_orders(
                orders, symbol, observed_at
            ) + self._parse_algo_orders(algo_orders, symbol, observed_at)
            current_snapshots.append(
                BinanceReadOnlySnapshot(
                    symbol=symbol,
                    observed_at=observed_at,
                    instrument=instrument,
                    orders=parsed_orders,
                    fills=(),
                    position=position,
                    equity=self._parse_equity(account, equity_currency, observed_at),
                    funding=(),
                    protection=BinanceReadOnlyClient._select_protection(parsed_orders, position),
                )
            )
        if not self._history_due(now):
            return tuple(current_snapshots)
        self._history_last_requested_at = now
        try:
            self._assert_low_priority_allowed(now)
            snapshots: list[BinanceReadOnlySnapshot] = []
            for snapshot in current_snapshots:
                fills = self._signed_get(
                    "/papi/v1/um/userTrades",
                    self._history_params(snapshot.symbol, "fills"),
                    timestamp_ms=timestamp_ms,
                )
                funding = self._signed_get(
                    "/papi/v1/um/income",
                    {
                        **self._history_params(snapshot.symbol, "funding"),
                        "incomeType": "FUNDING_FEE",
                    },
                    timestamp_ms=timestamp_ms,
                )
                BinanceReadOnlyClient._require_untruncated(fills, "userTrades", limit=1000)
                BinanceReadOnlyClient._require_untruncated(funding, "income", limit=1000)
                snapshots.append(
                    replace(
                        snapshot,
                        fills=BinanceReadOnlyClient._parse_fills(
                            fills, snapshot.symbol, observed_at
                        ),
                        funding=BinanceReadOnlyClient._parse_funding(
                            funding, snapshot.symbol, observed_at
                        ),
                    )
                )
        except DomainRejected as exc:
            return tuple(
                replace(snapshot, history_error_code=exc.code) for snapshot in current_snapshots
            )
        return tuple(snapshots)

    @staticmethod
    def _parse_position(
        raw: JsonValue, mark: JsonValue, symbol: str, now: datetime
    ) -> BinancePosition:
        if not isinstance(raw, dict) or not isinstance(raw.get("positions"), list):
            raise DomainRejected(
                "BINANCE_RESPONSE_INVALID", "Portfolio Margin UM account is invalid"
            )
        if not isinstance(mark, dict) or mark.get("symbol") != symbol:
            raise DomainRejected("BINANCE_RESPONSE_INVALID", "Binance mark price is invalid")
        rows = [
            item
            for item in raw["positions"]
            if isinstance(item, dict) and item.get("symbol") == symbol
        ]
        both = next(
            (item for item in rows if item.get("positionSide", "BOTH") == "BOTH"),
            None,
        )
        if both is None and any(
            _decimal(item.get("positionAmt", 0), "positionAmt") != 0 for item in rows
        ):
            raise DomainRejected(
                "BINANCE_HEDGE_MODE_UNSUPPORTED",
                "Portfolio Margin reader requires one-way UM position mode",
            )
        return BinancePosition(
            quantity=(
                Decimal(0) if both is None else _decimal(both.get("positionAmt", 0), "positionAmt")
            ),
            average_entry_price=(
                Decimal(0)
                if both is None
                else _decimal(both.get("entryPrice", 0), "entryPrice", minimum=Decimal(0))
            ),
            mark_price=_positive_decimal(mark.get("markPrice"), "markPrice"),
            observed_at=now,
        )

    @staticmethod
    def _parse_equity(raw: JsonValue, currency: str, now: datetime) -> BinanceEquity:
        if not isinstance(raw, dict):
            raise DomainRejected("BINANCE_RESPONSE_INVALID", "Portfolio Margin account is invalid")
        return BinanceEquity(
            equity=_decimal(raw.get("accountEquity"), "accountEquity", minimum=Decimal(0)),
            available_balance=_decimal(
                raw.get("totalAvailableBalance"),
                "totalAvailableBalance",
                minimum=Decimal(0),
            ),
            currency=currency,
            observed_at=_time(raw.get("updateTime", 0), now),
        )

    @staticmethod
    def _parse_algo_orders(raw: JsonValue, symbol: str, now: datetime) -> tuple[BinanceOrder, ...]:
        status_map = {
            "NEW": "SENT",
            "ACTIVE": "SENT",
            "CANCELED": "CANCELLED",
            "EXPIRED": "CANCELLED",
            "REJECTED": "REJECTED",
            "TRIGGERED": "UNKNOWN",
            "FINISHED": "FILLED",
        }
        result: list[BinanceOrder] = []
        for item in BinanceReadOnlyClient._require_list(raw, "openAlgoOrders"):
            if item.get("symbol") != symbol:
                continue
            side = str(item.get("side"))
            if side not in {"BUY", "SELL"}:
                raise DomainRejected(
                    "BINANCE_RESPONSE_INVALID", "Binance algo order side is invalid"
                )
            result.append(
                BinanceOrder(
                    order_id=str(item.get("algoId")),
                    client_order_id=str(item.get("clientAlgoId")),
                    status=status_map.get(str(item.get("algoStatus")), "UNKNOWN"),
                    side=side,
                    order_type=str(item.get("orderType")),
                    ordered_quantity=_decimal(
                        item.get("quantity", 0), "quantity", minimum=Decimal(0)
                    ),
                    filled_quantity=Decimal(0),
                    stop_price=_decimal(
                        item.get("triggerPrice", 0),
                        "triggerPrice",
                        minimum=Decimal(0),
                    ),
                    reduce_only=bool(item.get("reduceOnly", False)),
                    close_position=False,
                    observed_at=_time(item.get("updateTime", item.get("createTime", 0)), now),
                )
            )
        return tuple(result)
