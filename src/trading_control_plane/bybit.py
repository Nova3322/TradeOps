from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_control_plane.domain import DomainRejected
from trading_control_plane.venue_read_only import (
    VenueEquity,
    VenueFill,
    VenueFunding,
    VenueInstrument,
    VenueOrder,
    VenuePosition,
    VenueProtection,
    VenueReadOnlySnapshot,
)

JsonFetcher = Callable[[str, dict[str, str], float], dict[str, Any]]
HISTORY_WINDOW = timedelta(days=7)
MAX_PAGES = 50


def _default_fetcher(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
    body: bytes | None = None
    last_error: BaseException | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                body = response.read()
            break
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                code = "BYBIT_AUTHENTICATION_FAILED"
            elif exc.code == 429:
                code = "BYBIT_RATE_LIMITED"
            else:
                code = "BYBIT_READ_ONLY_UNAVAILABLE"
            if exc.code >= 500 and attempt < 2:
                last_error = exc
                time.sleep(0.25 * 2**attempt)
                continue
            raise DomainRejected(code, "Bybit read-only API rejected the request") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.25 * 2**attempt)
                continue
    if body is None:
        raise DomainRejected(
            "BYBIT_READ_ONLY_UNAVAILABLE", "Bybit read-only API could not be reached"
        ) from last_error
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DomainRejected("BYBIT_RESPONSE_INVALID", "Bybit returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise DomainRejected("BYBIT_RESPONSE_INVALID", "Bybit response shape is invalid")
    return value


def _decimal(raw: Any, field: str, *, minimum: Decimal | None = None) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DomainRejected(
            "BYBIT_RESPONSE_INVALID", f"Bybit field {field} is not numeric"
        ) from exc
    if not value.is_finite() or (minimum is not None and value < minimum):
        raise DomainRejected(
            "BYBIT_RESPONSE_INVALID", f"Bybit field {field} is outside its valid range"
        )
    return value


def _positive_decimal(raw: Any, field: str) -> Decimal:
    value = _decimal(raw, field)
    if value <= 0:
        raise DomainRejected("BYBIT_RESPONSE_INVALID", f"Bybit field {field} must be positive")
    return value


def _time(raw: Any, fallback: datetime) -> datetime:
    try:
        milliseconds = int(raw)
        return fallback if milliseconds <= 0 else datetime.fromtimestamp(milliseconds / 1000, UTC)
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        raise DomainRejected("BYBIT_RESPONSE_INVALID", "Bybit timestamp is invalid") from exc


class BybitReadOnlyClient:
    """Bybit V5 Unified USDT linear-perpetual reader with no write methods."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.bybit.com",
        api_key: str,
        api_secret: str,
        recv_window_ms: int = 5_000,
        fetcher: JsonFetcher = _default_fetcher,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname != "api.bybit.com":
            raise ValueError("Bybit read-only base URL must use the official API host")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._recv_window_ms = recv_window_ms
        self._fetcher = fetcher
        self._catalog: tuple[VenueInstrument, ...] | None = None

    @staticmethod
    def _query(params: dict[str, str]) -> str:
        return urllib.parse.urlencode(sorted(params.items()))

    @staticmethod
    def _result(raw: dict[str, Any], name: str) -> dict[str, Any]:
        code = raw.get("retCode")
        if code != 0:
            if code == 10006:
                error_code = "BYBIT_RATE_LIMITED"
            elif code in {10003, 10004, 10005, 10007, 10010}:
                error_code = "BYBIT_AUTHENTICATION_FAILED"
            else:
                error_code = "BYBIT_READ_ONLY_REJECTED"
            raise DomainRejected(error_code, f"Bybit rejected the read-only {name} request")
        result = raw.get("result")
        if not isinstance(result, dict):
            raise DomainRejected("BYBIT_RESPONSE_INVALID", f"Bybit {name} response is invalid")
        return result

    def _public_result(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        query = self._query(params)
        return self._result(
            self._fetcher(f"{self._base_url}{path}?{query}", {}, 5.0),
            path,
        )

    def _private_result(
        self,
        path: str,
        params: dict[str, str],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        query = self._query(params)
        timestamp = str(int(now.timestamp() * 1000))
        recv_window = str(self._recv_window_ms)
        signature = hmac.new(
            self._api_secret.encode(),
            f"{timestamp}{self._api_key}{recv_window}{query}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return self._result(
            self._fetcher(
                f"{self._base_url}{path}?{query}",
                {
                    "X-BAPI-API-KEY": self._api_key,
                    "X-BAPI-TIMESTAMP": timestamp,
                    "X-BAPI-RECV-WINDOW": recv_window,
                    "X-BAPI-SIGN": signature,
                },
                5.0,
            ),
            path,
        )

    @staticmethod
    def _rows(result: dict[str, Any], name: str) -> list[dict[str, Any]]:
        rows = result.get("list")
        if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
            raise DomainRejected("BYBIT_RESPONSE_INVALID", f"Bybit {name} rows are invalid")
        return rows

    def _pages(
        self,
        path: str,
        params: dict[str, str],
        *,
        now: datetime | None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            scoped = dict(params)
            if cursor:
                scoped["cursor"] = cursor
            result = (
                self._public_result(path, scoped)
                if now is None
                else self._private_result(path, scoped, now=now)
            )
            rows.extend(self._rows(result, path))
            value = result.get("nextPageCursor")
            cursor = value if isinstance(value, str) and value else None
            if cursor is None:
                return rows
            if cursor in seen:
                raise DomainRejected(
                    "BYBIT_RESPONSE_INVALID", "Bybit pagination cursor repeated"
                )
            seen.add(cursor)
        raise DomainRejected(
            "BYBIT_RESPONSE_INCOMPLETE", "Bybit response exceeded the bounded page limit"
        )

    def verify_connection(self, *, now: datetime) -> None:
        result = self._private_result(
            "/v5/account/wallet-balance", {"accountType": "UNIFIED"}, now=now
        )
        self._rows(result, "wallet balance")

    def read_active_instruments(self) -> tuple[VenueInstrument, ...]:
        if self._catalog is not None:
            return self._catalog
        rows = self._pages(
            "/v5/market/instruments-info",
            {"category": "linear", "limit": "1000"},
            now=None,
        )
        instruments: list[VenueInstrument] = []
        for item in rows:
            if (
                item.get("contractType") != "LinearPerpetual"
                or item.get("settleCoin") != "USDT"
                or item.get("status") != "Trading"
            ):
                continue
            symbol = item.get("symbol")
            price_filter = item.get("priceFilter")
            size_filter = item.get("lotSizeFilter")
            if (
                not isinstance(symbol, str)
                or not symbol.endswith("USDT")
                or not isinstance(price_filter, dict)
                or not isinstance(size_filter, dict)
            ):
                raise DomainRejected(
                    "BYBIT_RESPONSE_INVALID", "Bybit instrument metadata is invalid"
                )
            instruments.append(
                VenueInstrument(
                    symbol=symbol,
                    tick_size=_positive_decimal(price_filter.get("tickSize"), "tickSize"),
                    lot_size=_positive_decimal(size_filter.get("qtyStep"), "qtyStep"),
                    minimum_notional=_decimal(
                        size_filter.get("minNotionalValue", 0),
                        "minNotionalValue",
                        minimum=Decimal(0),
                    ),
                    quote_currency="USDT",
                    collateral_currency="USDT",
                    active=True,
                )
            )
        if not instruments or len(instruments) != len({item.symbol for item in instruments}):
            raise DomainRejected("BYBIT_RESPONSE_INVALID", "Bybit active catalog is invalid")
        self._catalog = tuple(sorted(instruments, key=lambda item: item.symbol))
        return self._catalog

    def read_account_snapshots(
        self,
        symbols: tuple[str, ...],
        *,
        now: datetime,
    ) -> tuple[VenueReadOnlySnapshot, ...]:
        catalog = {item.symbol: item for item in self.read_active_instruments()}
        configured = set(symbols)
        if any(symbol not in catalog for symbol in configured):
            raise DomainRejected(
                "BYBIT_INSTRUMENT_UNAVAILABLE", "configured Bybit instrument is unavailable"
            )
        marks = self._mark_prices(catalog)
        wallet = self._private_result(
            "/v5/account/wallet-balance", {"accountType": "UNIFIED"}, now=now
        )
        supported_positions = self._pages(
            "/v5/position/list",
            {"category": "linear", "settleCoin": "USDT", "limit": "200"},
            now=now,
        )
        supported_orders = self._pages(
            "/v5/order/realtime",
            {"category": "linear", "settleCoin": "USDT", "limit": "50"},
            now=now,
        )
        self._reject_unsupported_exposure(now)
        start_ms = int((now - HISTORY_WINDOW).timestamp() * 1000)
        end_ms = int(now.timestamp() * 1000)
        fills = self._pages(
            "/v5/execution/list",
            {
                "category": "linear",
                "startTime": str(start_ms),
                "endTime": str(end_ms),
                "limit": "100",
            },
            now=now,
        )
        funding = self._pages(
            "/v5/account/transaction-log",
            {
                "accountType": "UNIFIED",
                "category": "linear",
                "currency": "USDT",
                "type": "SETTLEMENT",
                "startTime": str(start_ms),
                "endTime": str(end_ms),
                "limit": "50",
            },
            now=now,
        )
        target_symbols = (
            configured
            | self._active_position_symbols(supported_positions, catalog)
            | self._symbols(supported_orders, catalog)
            | self._symbols(fills, catalog)
            | self._symbols(funding, catalog, ignore_blank=True)
        )
        if not target_symbols:
            target_symbols.add(next(iter(sorted(catalog))))
        equity = self._parse_equity(wallet, now)
        snapshots: list[VenueReadOnlySnapshot] = []
        for symbol in sorted(target_symbols):
            position, position_order = self._parse_position(
                supported_positions, symbol, marks[symbol], now
            )
            orders = list(self._parse_orders(supported_orders, symbol, now))
            if position_order is not None:
                orders.append(position_order)
            parsed_orders = tuple(orders)
            snapshots.append(
                VenueReadOnlySnapshot(
                    symbol=symbol,
                    observed_at=now,
                    instrument=catalog[symbol],
                    orders=parsed_orders,
                    fills=self._parse_fills(fills, symbol, now),
                    position=position,
                    equity=equity,
                    funding=self._parse_funding(funding, symbol, now),
                    protection=self._select_protection(parsed_orders, position),
                )
            )
        return tuple(snapshots)

    def _reject_unsupported_exposure(self, now: datetime) -> None:
        unsupported_positions = [
            *self._pages(
                "/v5/position/list",
                {"category": "linear", "settleCoin": "USDC", "limit": "200"},
                now=now,
            ),
            *self._pages(
                "/v5/position/list", {"category": "inverse", "limit": "200"}, now=now
            ),
            *self._pages(
                "/v5/position/list", {"category": "option", "limit": "200"}, now=now
            ),
        ]
        unsupported_orders = [
            *self._pages(
                "/v5/order/realtime",
                {"category": "linear", "settleCoin": "USDC", "limit": "50"},
                now=now,
            ),
            *self._pages(
                "/v5/order/realtime", {"category": "inverse", "limit": "50"}, now=now
            ),
            *self._pages(
                "/v5/order/realtime", {"category": "option", "limit": "50"}, now=now
            ),
        ]
        if any(_decimal(item.get("size", 0), "size") != 0 for item in unsupported_positions):
            raise DomainRejected(
                "BYBIT_ACCOUNT_SCOPE_UNSUPPORTED",
                "Bybit continuous facts require no unsupported derivative positions",
            )
        if unsupported_orders:
            raise DomainRejected(
                "BYBIT_ACCOUNT_SCOPE_UNSUPPORTED",
                "Bybit continuous facts require no unsupported derivative orders",
            )

    def _mark_prices(
        self, catalog: dict[str, VenueInstrument]
    ) -> dict[str, Decimal]:
        result = self._public_result("/v5/market/tickers", {"category": "linear"})
        marks = {
            str(item.get("symbol")): _positive_decimal(item.get("markPrice"), "markPrice")
            for item in self._rows(result, "tickers")
            if item.get("symbol") in catalog
        }
        if marks.keys() != catalog.keys():
            raise DomainRejected(
                "BYBIT_RESPONSE_INCOMPLETE", "Bybit mark price coverage is incomplete"
            )
        return marks

    @staticmethod
    def _symbols(
        rows: list[dict[str, Any]],
        catalog: dict[str, VenueInstrument],
        *,
        ignore_blank: bool = False,
    ) -> set[str]:
        result: set[str] = set()
        for item in rows:
            symbol = item.get("symbol")
            if ignore_blank and not symbol:
                continue
            if not isinstance(symbol, str):
                raise DomainRejected(
                    "BYBIT_RESPONSE_INVALID", "Bybit instrument identity is invalid"
                )
            if symbol in catalog:
                result.add(symbol)
        return result

    @staticmethod
    def _active_position_symbols(
        rows: list[dict[str, Any]], catalog: dict[str, VenueInstrument]
    ) -> set[str]:
        return {
            str(item["symbol"])
            for item in rows
            if item.get("symbol") in catalog
            and _decimal(item.get("size", 0), "size") != 0
        }

    def _parse_equity(self, result: dict[str, Any], now: datetime) -> VenueEquity:
        rows = self._rows(result, "wallet balance")
        if len(rows) != 1 or rows[0].get("accountType") != "UNIFIED":
            raise DomainRejected(
                "BYBIT_RESPONSE_INVALID", "Bybit Unified equity is ambiguous"
            )
        item = rows[0]
        return VenueEquity(
            equity=_decimal(item.get("totalEquity"), "totalEquity", minimum=Decimal(0)),
            available_balance=_decimal(
                item.get("totalAvailableBalance"),
                "totalAvailableBalance",
                minimum=Decimal(0),
            ),
            currency="USD",
            observed_at=now,
        )

    @staticmethod
    def _parse_position(
        rows: list[dict[str, Any]],
        symbol: str,
        mark_price: Decimal,
        now: datetime,
    ) -> tuple[VenuePosition, VenueOrder | None]:
        active = [
            item
            for item in rows
            if item.get("symbol") == symbol and _decimal(item.get("size", 0), "size") != 0
        ]
        if len(active) > 1:
            raise DomainRejected(
                "BYBIT_HEDGE_MODE_UNSUPPORTED",
                "Bybit continuous facts cannot collapse simultaneous long and short positions",
            )
        if not active:
            return VenuePosition(Decimal(0), Decimal(0), mark_price, now), None
        item = active[0]
        side = item.get("side")
        if side not in {"Buy", "Sell"}:
            raise DomainRejected("BYBIT_RESPONSE_INVALID", "Bybit position side is invalid")
        quantity = _positive_decimal(item.get("size"), "size")
        if side == "Sell":
            quantity = -quantity
        observed_at = _time(item.get("updatedTime", 0), now)
        position = VenuePosition(
            quantity=quantity,
            average_entry_price=_positive_decimal(item.get("avgPrice"), "avgPrice"),
            mark_price=_positive_decimal(item.get("markPrice", mark_price), "markPrice"),
            observed_at=observed_at,
        )
        stop_price = _decimal(item.get("stopLoss") or 0, "stopLoss", minimum=Decimal(0))
        if stop_price == 0:
            return position, None
        position_index = str(item.get("positionIdx", "0"))
        order_id = f"bybit-position-sl:{symbol}:{position_index}"
        return position, VenueOrder(
            order_id=order_id,
            client_order_id=order_id,
            status="SENT",
            side="SELL" if quantity > 0 else "BUY",
            order_type="POSITION_STOP_LOSS",
            ordered_quantity=abs(quantity),
            filled_quantity=Decimal(0),
            stop_price=stop_price,
            reduce_only=True,
            close_position=True,
            observed_at=observed_at,
        )

    @staticmethod
    def _parse_orders(
        rows: list[dict[str, Any]], symbol: str, now: datetime
    ) -> tuple[VenueOrder, ...]:
        status_map = {"New": "SENT", "PartiallyFilled": "PARTIALLY_FILLED"}
        result: list[VenueOrder] = []
        for item in rows:
            if item.get("symbol") != symbol:
                continue
            order_id = str(item.get("orderId") or "")
            side = str(item.get("side", "")).upper()
            if not order_id or side not in {"BUY", "SELL"}:
                raise DomainRejected(
                    "BYBIT_RESPONSE_INVALID", "Bybit order identity is invalid"
                )
            ordered = _positive_decimal(item.get("qty"), "qty")
            filled = _decimal(item.get("cumExecQty", 0), "cumExecQty", minimum=Decimal(0))
            if filled > ordered:
                raise DomainRejected(
                    "BYBIT_RESPONSE_INVALID", "Bybit filled size exceeds order size"
                )
            result.append(
                VenueOrder(
                    order_id=order_id,
                    client_order_id=str(item.get("orderLinkId") or f"bybit-ord-{order_id}"),
                    status=status_map.get(str(item.get("orderStatus")), "UNKNOWN"),
                    side=side,
                    order_type=str(item.get("orderType") or "UNKNOWN").upper(),
                    ordered_quantity=ordered,
                    filled_quantity=filled,
                    stop_price=_decimal(
                        item.get("triggerPrice") or 0,
                        "triggerPrice",
                        minimum=Decimal(0),
                    ),
                    reduce_only=bool(item.get("reduceOnly")),
                    close_position=bool(item.get("closeOnTrigger")),
                    observed_at=_time(item.get("updatedTime", 0), now),
                )
            )
        return tuple(result)

    @staticmethod
    def _parse_fills(
        rows: list[dict[str, Any]], symbol: str, now: datetime
    ) -> tuple[VenueFill, ...]:
        result: list[VenueFill] = []
        for item in rows:
            if item.get("symbol") != symbol:
                continue
            side = str(item.get("side", "")).upper()
            if side not in {"BUY", "SELL"}:
                raise DomainRejected("BYBIT_RESPONSE_INVALID", "Bybit fill side is invalid")
            result.append(
                VenueFill(
                    fill_id=str(item.get("execId") or ""),
                    order_id=str(item.get("orderId") or ""),
                    side=side,
                    quantity=_positive_decimal(item.get("execQty"), "execQty"),
                    price=_positive_decimal(item.get("execPrice"), "execPrice"),
                    fee=abs(_decimal(item.get("execFee", 0), "execFee")),
                    fee_currency=str(item.get("feeCurrency") or "USDT"),
                    executed_at=_time(item.get("execTime", 0), now),
                )
            )
        if any(not item.fill_id or not item.order_id for item in result):
            raise DomainRejected(
                "BYBIT_RESPONSE_INVALID", "Bybit fill identity is incomplete"
            )
        return tuple(result)

    @staticmethod
    def _parse_funding(
        rows: list[dict[str, Any]], symbol: str, now: datetime
    ) -> tuple[VenueFunding, ...]:
        result: list[VenueFunding] = []
        for item in rows:
            if item.get("symbol") != symbol:
                continue
            amount = _decimal(item.get("funding", 0), "funding")
            if amount == 0:
                continue
            payment_id = str(item.get("id") or "")
            if not payment_id:
                raise DomainRejected(
                    "BYBIT_RESPONSE_INVALID", "Bybit funding identity is incomplete"
                )
            result.append(
                VenueFunding(
                    payment_id=payment_id,
                    amount=amount,
                    currency=str(item.get("currency") or "USDT"),
                    paid_at=_time(item.get("transactionTime", 0), now),
                )
            )
        return tuple(result)

    @staticmethod
    def _select_protection(
        orders: tuple[VenueOrder, ...], position: VenuePosition
    ) -> VenueProtection | None:
        if position.quantity == 0:
            return None
        side = "SELL" if position.quantity > 0 else "BUY"
        candidates = [
            item
            for item in orders
            if item.side == side
            and (item.reduce_only or item.close_position)
            and item.stop_price > 0
            and item.status in {"SENT", "PARTIALLY_FILLED"}
        ]
        if not candidates:
            return None
        selected = max(candidates, key=lambda item: item.observed_at)
        quantity = (
            abs(position.quantity)
            if selected.close_position
            else selected.ordered_quantity - selected.filled_quantity
        )
        return VenueProtection(
            selected.order_id,
            quantity,
            selected.stop_price,
            selected.observed_at,
        )
