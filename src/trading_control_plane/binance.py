from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_control_plane.domain import DomainRejected

JsonValue = dict[str, Any] | list[dict[str, Any]]
JsonFetcher = Callable[[str, dict[str, str], float], JsonValue]
ServerTimeFetcher = Callable[[float], int]


def _default_server_time_fetcher(timeout: float) -> int:
    request = urllib.request.Request("https://fapi.binance.com/fapi/v1/time", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = json.loads(response.read())
        return int(raw["serverTime"])
    except (
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        urllib.error.URLError,
        TimeoutError,
    ) as exc:
        raise DomainRejected(
            "BINANCE_READ_ONLY_UNAVAILABLE", "Binance server time is unavailable"
        ) from exc


def _default_fetcher(url: str, headers: dict[str, str], timeout: float) -> JsonValue:
    request = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DomainRejected(
            "BINANCE_READ_ONLY_UNAVAILABLE", "Binance read-only API could not be reached"
        ) from exc
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
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._recv_window_ms = recv_window_ms
        self._fetcher = fetcher

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self._api_secret)

    def _public_get(self, path: str, params: dict[str, str]) -> JsonValue:
        query = urllib.parse.urlencode(params)
        return self._fetcher(f"{self._base_url}{path}?{query}", {}, 5.0)

    def _signed_get(self, path: str, params: dict[str, str], *, timestamp_ms: int) -> JsonValue:
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
        return self._fetcher(
            f"{self._base_url}{path}?{query}&signature={signature}",
            {"X-MBX-APIKEY": self._api_key},
            5.0,
        )

    def read_snapshot(self, symbol: str, *, now: datetime) -> BinanceReadOnlySnapshot:
        if not symbol or symbol != symbol.upper():
            raise DomainRejected("BINANCE_SYMBOL_INVALID", "Binance symbol must be uppercase")
        timestamp_ms = int(now.timestamp() * 1000)
        exchange = self._public_get("/fapi/v1/exchangeInfo", {"symbol": symbol})
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
        self._fetcher = fetcher
        self._server_time_fetcher = server_time_fetcher

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self._api_secret)

    def _signed_get(self, path: str, params: dict[str, str], *, timestamp_ms: int) -> JsonValue:
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
        return self._fetcher(
            f"{self._base_url}{path}?{query}&signature={signature}",
            {"X-MBX-APIKEY": self._api_key},
            5.0,
        )

    def _market_get(self, path: str, params: dict[str, str]) -> JsonValue:
        query = urllib.parse.urlencode(params)
        return self._fetcher(f"https://fapi.binance.com{path}?{query}", {}, 5.0)

    def read_snapshot(self, symbol: str, *, now: datetime) -> BinanceReadOnlySnapshot:
        del now
        if not symbol or symbol != symbol.upper():
            raise DomainRejected("BINANCE_SYMBOL_INVALID", "Binance symbol must be uppercase")
        timestamp_ms = self._server_time_fetcher(5.0)
        observed_at = datetime.fromtimestamp(timestamp_ms / 1000, UTC)
        exchange = self._market_get("/fapi/v1/exchangeInfo", {"symbol": symbol})
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
