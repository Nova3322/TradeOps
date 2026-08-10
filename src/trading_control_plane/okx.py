from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
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
HISTORY_LIMIT = 100
MAX_HISTORY_PAGES = 5


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
                code = "OKX_AUTHENTICATION_FAILED"
            elif exc.code == 429:
                code = "OKX_RATE_LIMITED"
            else:
                code = "OKX_READ_ONLY_UNAVAILABLE"
            if exc.code >= 500 and attempt < 2:
                last_error = exc
                time.sleep(0.25 * 2**attempt)
                continue
            raise DomainRejected(code, "OKX read-only API rejected the request") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.25 * 2**attempt)
                continue
    if body is None:
        raise DomainRejected(
            "OKX_READ_ONLY_UNAVAILABLE", "OKX read-only API could not be reached"
        ) from last_error
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DomainRejected("OKX_RESPONSE_INVALID", "OKX returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise DomainRejected("OKX_RESPONSE_INVALID", "OKX response shape is invalid")
    return value


def _decimal(raw: Any, field: str, *, minimum: Decimal | None = None) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DomainRejected("OKX_RESPONSE_INVALID", f"OKX field {field} is not numeric") from exc
    if not value.is_finite() or (minimum is not None and value < minimum):
        raise DomainRejected(
            "OKX_RESPONSE_INVALID", f"OKX field {field} is outside its valid range"
        )
    return value


def _positive_decimal(raw: Any, field: str) -> Decimal:
    value = _decimal(raw, field)
    if value <= 0:
        raise DomainRejected("OKX_RESPONSE_INVALID", f"OKX field {field} must be positive")
    return value


def _time(raw: Any, fallback: datetime) -> datetime:
    try:
        milliseconds = int(raw)
        return fallback if milliseconds <= 0 else datetime.fromtimestamp(milliseconds / 1000, UTC)
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        raise DomainRejected("OKX_RESPONSE_INVALID", "OKX timestamp is invalid") from exc


def _truthy(raw: Any) -> bool:
    return raw is True or str(raw).lower() == "true"


class OkxReadOnlyClient:
    """OKX V5 USDT linear SWAP fact reader with no write or trading methods."""

    def __init__(
        self,
        *,
        base_url: str = "https://www.okx.com",
        api_key: str,
        api_secret: str,
        passphrase: str,
        fetcher: JsonFetcher = _default_fetcher,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname != "www.okx.com":
            raise ValueError("OKX read-only base URL must use the official API host")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self._fetcher = fetcher
        self._catalog: tuple[VenueInstrument, ...] | None = None
        self._contract_multipliers: dict[str, Decimal] = {}

    @staticmethod
    def _timestamp(now: datetime) -> str:
        return now.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @staticmethod
    def _data(raw: dict[str, Any], name: str) -> list[dict[str, Any]]:
        code = raw.get("code")
        if code != "0":
            normalized = str(code or "")
            if normalized in {"50011", "50040", "50061"}:
                error_code = "OKX_RATE_LIMITED"
            elif normalized.startswith("501"):
                error_code = "OKX_AUTHENTICATION_FAILED"
            else:
                error_code = "OKX_READ_ONLY_REJECTED"
            raise DomainRejected(error_code, f"OKX rejected the read-only {name} request")
        data = raw.get("data")
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise DomainRejected("OKX_RESPONSE_INVALID", f"OKX {name} response is invalid")
        return data

    @staticmethod
    def _query(params: dict[str, str]) -> str:
        return urllib.parse.urlencode(sorted(params.items()))

    def _public_get(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        query = self._query(params)
        suffix = f"?{query}" if query else ""
        return self._data(
            self._fetcher(f"{self._base_url}{path}{suffix}", {}, 5.0),
            path,
        )

    def _private_get(
        self,
        path: str,
        params: dict[str, str],
        *,
        now: datetime,
    ) -> list[dict[str, Any]]:
        query = self._query(params)
        request_path = f"{path}?{query}" if query else path
        timestamp = self._timestamp(now)
        signature = base64.b64encode(
            hmac.new(
                self._api_secret.encode(),
                f"{timestamp}GET{request_path}".encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        return self._data(
            self._fetcher(
                f"{self._base_url}{request_path}",
                {
                    "OK-ACCESS-KEY": self._api_key,
                    "OK-ACCESS-SIGN": signature,
                    "OK-ACCESS-TIMESTAMP": timestamp,
                    "OK-ACCESS-PASSPHRASE": self._passphrase,
                },
                5.0,
            ),
            path,
        )

    def _private_pages(
        self,
        path: str,
        params: dict[str, str],
        *,
        cursor_field: str,
        now: datetime,
    ) -> tuple[list[dict[str, Any]], bool]:
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_HISTORY_PAGES):
            scoped = dict(params)
            if cursor is not None:
                scoped["after"] = cursor
            page = self._private_get(path, scoped, now=now)
            rows.extend(page)
            if len(page) < HISTORY_LIMIT:
                return rows, False
            value = page[-1].get(cursor_field)
            cursor = value if isinstance(value, str) and value else None
            if cursor is None or cursor in seen:
                raise DomainRejected(
                    "OKX_RESPONSE_INVALID", "OKX history pagination identity is invalid"
                )
            seen.add(cursor)
        return rows, True

    def verify_connection(self, *, now: datetime) -> None:
        self._private_get("/api/v5/account/balance", {}, now=now)

    def read_active_instruments(self) -> tuple[VenueInstrument, ...]:
        if self._catalog is not None:
            return self._catalog
        rows = self._public_get("/api/v5/public/instruments", {"instType": "SWAP"})
        instruments: list[VenueInstrument] = []
        multipliers: dict[str, Decimal] = {}
        for item in rows:
            if (
                item.get("instType") != "SWAP"
                or item.get("ctType") != "linear"
                or item.get("settleCcy") != "USDT"
                or item.get("state") != "live"
            ):
                continue
            symbol = item.get("instId")
            if not isinstance(symbol, str) or not symbol.endswith("-USDT-SWAP"):
                raise DomainRejected("OKX_RESPONSE_INVALID", "OKX instrument identity is invalid")
            multiplier = _positive_decimal(item.get("ctVal"), "ctVal")
            if item.get("ctValCcy") not in {symbol.split("-", 1)[0], ""}:
                raise DomainRejected(
                    "OKX_INSTRUMENT_UNSUPPORTED",
                    "OKX contract value is not denominated in the base asset",
                )
            instruments.append(
                VenueInstrument(
                    symbol=symbol,
                    tick_size=_positive_decimal(item.get("tickSz"), "tickSz"),
                    lot_size=_positive_decimal(item.get("lotSz"), "lotSz") * multiplier,
                    minimum_notional=Decimal(0),
                    quote_currency="USDT",
                    collateral_currency="USDT",
                    active=True,
                )
            )
            multipliers[symbol] = multiplier
        if not instruments or len(instruments) != len({item.symbol for item in instruments}):
            raise DomainRejected("OKX_RESPONSE_INVALID", "OKX active SWAP catalog is invalid")
        self._contract_multipliers = multipliers
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
                "OKX_INSTRUMENT_UNAVAILABLE", "configured OKX instrument is unavailable"
            )
        marks = self._mark_prices()
        balance = self._private_get("/api/v5/account/balance", {}, now=now)
        positions = self._private_get(
            "/api/v5/account/positions", {"instType": "SWAP"}, now=now
        )
        orders = self._private_get(
            "/api/v5/trade/orders-pending", {"instType": "SWAP"}, now=now
        )
        algo_orders = tuple(
            item
            for order_type in ("conditional", "oco", "trigger", "move_order_stop")
            for item in self._private_get(
                "/api/v5/trade/orders-algo-pending",
                {"ordType": order_type},
                now=now,
            )
            if item.get("instType") in {None, "SWAP"}
        )
        fills, fills_incomplete = self._private_pages(
            "/api/v5/trade/fills-history",
            {"instType": "SWAP", "limit": str(HISTORY_LIMIT)},
            cursor_field="billId",
            now=now,
        )
        funding, funding_incomplete = self._private_pages(
            "/api/v5/account/bills",
            {"instType": "SWAP", "type": "8", "limit": str(HISTORY_LIMIT)},
            cursor_field="billId",
            now=now,
        )
        self._reject_unsupported_exposure(positions, orders, algo_orders, catalog)
        target_symbols = (
            configured
            | self._active_position_symbols(positions, catalog)
            | self._symbols(orders, catalog)
            | self._symbols(algo_orders, catalog)
            | self._symbols(fills, catalog)
            | self._symbols(funding, catalog, ignore_blank=True)
        )
        if not target_symbols:
            target_symbols.add(next(iter(sorted(catalog))))
        equity = self._parse_equity(balance, now)
        history_error = (
            "OKX_RESPONSE_INCOMPLETE"
            if fills_incomplete or funding_incomplete
            else None
        )
        snapshots: list[VenueReadOnlySnapshot] = []
        for symbol in sorted(target_symbols):
            position = self._parse_position(positions, symbol, marks[symbol], now)
            parsed_orders = (
                *self._parse_orders(orders, symbol, now),
                *self._parse_algo_orders(algo_orders, symbol, position, now),
            )
            snapshots.append(
                VenueReadOnlySnapshot(
                    symbol=symbol,
                    observed_at=now,
                    instrument=catalog[symbol],
                    orders=tuple(parsed_orders),
                    fills=(
                        () if history_error else self._parse_fills(fills, symbol, now)
                    ),
                    position=position,
                    equity=equity,
                    funding=(
                        () if history_error else self._parse_funding(funding, symbol, now)
                    ),
                    protection=self._select_protection(tuple(parsed_orders), position),
                    history_error_code=history_error,
                )
            )
        return tuple(snapshots)

    def _mark_prices(self) -> dict[str, Decimal]:
        rows = self._public_get("/api/v5/public/mark-price", {"instType": "SWAP"})
        result = {
            str(item.get("instId")): _positive_decimal(item.get("markPx"), "markPx")
            for item in rows
            if item.get("instId") in self._contract_multipliers
        }
        if result.keys() != self._contract_multipliers.keys():
            raise DomainRejected("OKX_RESPONSE_INCOMPLETE", "OKX mark price coverage is incomplete")
        return result

    @staticmethod
    def _symbols(
        rows: tuple[dict[str, Any], ...] | list[dict[str, Any]],
        catalog: dict[str, VenueInstrument],
        *,
        ignore_blank: bool = False,
    ) -> set[str]:
        result: set[str] = set()
        for item in rows:
            symbol = item.get("instId")
            if ignore_blank and not symbol:
                continue
            if not isinstance(symbol, str):
                raise DomainRejected("OKX_RESPONSE_INVALID", "OKX instrument identity is invalid")
            if symbol in catalog:
                result.add(symbol)
        return result

    def _reject_unsupported_exposure(
        self,
        positions: list[dict[str, Any]],
        orders: list[dict[str, Any]],
        algo_orders: tuple[dict[str, Any], ...],
        catalog: dict[str, VenueInstrument],
    ) -> None:
        unsupported_positions = [
            item
            for item in positions
            if item.get("instId") not in catalog and _decimal(item.get("pos", 0), "pos") != 0
        ]
        unsupported_orders = [
            item
            for item in (*orders, *algo_orders)
            if item.get("instId") not in catalog
        ]
        if unsupported_positions or unsupported_orders:
            raise DomainRejected(
                "OKX_ACCOUNT_SCOPE_UNSUPPORTED",
                "OKX continuous facts require an account without unsupported SWAP exposure",
            )

    @staticmethod
    def _active_position_symbols(
        rows: list[dict[str, Any]], catalog: dict[str, VenueInstrument]
    ) -> set[str]:
        return {
            str(item["instId"])
            for item in rows
            if item.get("instId") in catalog and _decimal(item.get("pos", 0), "pos") != 0
        }

    @staticmethod
    def _parse_equity(rows: list[dict[str, Any]], now: datetime) -> VenueEquity:
        if len(rows) != 1:
            raise DomainRejected("OKX_RESPONSE_INVALID", "OKX account balance is ambiguous")
        item = rows[0]
        return VenueEquity(
            equity=_decimal(item.get("totalEq"), "totalEq", minimum=Decimal(0)),
            available_balance=_decimal(item.get("availEq"), "availEq", minimum=Decimal(0)),
            currency="USD",
            observed_at=_time(item.get("uTime", 0), now),
        )

    def _parse_position(
        self,
        rows: list[dict[str, Any]],
        symbol: str,
        mark_price: Decimal,
        now: datetime,
    ) -> VenuePosition:
        relevant = [item for item in rows if item.get("instId") == symbol]
        active = [item for item in relevant if _decimal(item.get("pos", 0), "pos") != 0]
        if len(active) > 1:
            raise DomainRejected(
                "OKX_HEDGE_MODE_UNSUPPORTED",
                "OKX continuous facts cannot collapse simultaneous long and short positions",
            )
        if not active:
            return VenuePosition(Decimal(0), Decimal(0), mark_price, now)
        item = active[0]
        side = str(item.get("posSide", "net"))
        raw_quantity = _decimal(item.get("pos"), "pos")
        if side == "short":
            quantity = -abs(raw_quantity) * self._contract_multipliers[symbol]
        elif side == "long":
            quantity = abs(raw_quantity) * self._contract_multipliers[symbol]
        elif side == "net":
            quantity = raw_quantity * self._contract_multipliers[symbol]
        else:
            raise DomainRejected("OKX_RESPONSE_INVALID", "OKX position side is invalid")
        return VenuePosition(
            quantity=quantity,
            average_entry_price=_positive_decimal(item.get("avgPx"), "avgPx"),
            mark_price=_positive_decimal(item.get("markPx", mark_price), "markPx"),
            observed_at=_time(item.get("uTime", 0), now),
        )

    def _parse_orders(
        self, rows: list[dict[str, Any]], symbol: str, now: datetime
    ) -> tuple[VenueOrder, ...]:
        status_map = {"live": "SENT", "partially_filled": "PARTIALLY_FILLED"}
        result: list[VenueOrder] = []
        multiplier = self._contract_multipliers[symbol]
        for item in rows:
            if item.get("instId") != symbol:
                continue
            order_id = str(item.get("ordId") or "")
            if not order_id:
                raise DomainRejected("OKX_RESPONSE_INVALID", "OKX order identity is incomplete")
            side = str(item.get("side", "")).upper()
            if side not in {"BUY", "SELL"}:
                raise DomainRejected("OKX_RESPONSE_INVALID", "OKX order side is invalid")
            ordered = _positive_decimal(item.get("sz"), "sz") * multiplier
            filled = (
                _decimal(item.get("accFillSz", 0), "accFillSz", minimum=Decimal(0))
                * multiplier
            )
            if filled > ordered:
                raise DomainRejected("OKX_RESPONSE_INVALID", "OKX filled size exceeds order size")
            result.append(
                VenueOrder(
                    order_id=order_id,
                    client_order_id=str(item.get("clOrdId") or f"okx-ord-{order_id}"),
                    status=status_map.get(str(item.get("state")), "UNKNOWN"),
                    side=side,
                    order_type=str(item.get("ordType") or "UNKNOWN").upper(),
                    ordered_quantity=ordered,
                    filled_quantity=filled,
                    stop_price=_decimal(
                        item.get("slTriggerPx") or item.get("triggerPx") or 0,
                        "triggerPx",
                        minimum=Decimal(0),
                    ),
                    reduce_only=_truthy(item.get("reduceOnly")),
                    close_position=False,
                    observed_at=_time(item.get("uTime", 0), now),
                )
            )
        return tuple(result)

    def _parse_algo_orders(
        self,
        rows: tuple[dict[str, Any], ...],
        symbol: str,
        position: VenuePosition,
        now: datetime,
    ) -> tuple[VenueOrder, ...]:
        result: list[VenueOrder] = []
        multiplier = self._contract_multipliers[symbol]
        for item in rows:
            if item.get("instId") != symbol:
                continue
            order_id = str(item.get("algoId") or "")
            if not order_id:
                raise DomainRejected("OKX_RESPONSE_INVALID", "OKX algo identity is incomplete")
            side = str(item.get("side", "")).upper()
            if side not in {"BUY", "SELL"}:
                raise DomainRejected("OKX_RESPONSE_INVALID", "OKX algo side is invalid")
            close_position = str(item.get("closeFraction", "")) == "1"
            size_raw = item.get("sz")
            size = (
                abs(position.quantity) / multiplier
                if close_position and not size_raw
                else _positive_decimal(size_raw, "sz")
            )
            trigger = item.get("slTriggerPx") or item.get("triggerPx") or item.get("tpTriggerPx")
            result.append(
                VenueOrder(
                    order_id=order_id,
                    client_order_id=str(item.get("algoClOrdId") or f"okx-algo-{order_id}"),
                    status="SENT",
                    side=side,
                    order_type=str(item.get("ordType") or "ALGO").upper(),
                    ordered_quantity=size * multiplier,
                    filled_quantity=Decimal(0),
                    stop_price=_decimal(trigger or 0, "triggerPx", minimum=Decimal(0)),
                    reduce_only=_truthy(item.get("reduceOnly")) or close_position,
                    close_position=close_position,
                    observed_at=_time(item.get("cTime", 0), now),
                )
            )
        return tuple(result)

    def _parse_fills(
        self, rows: list[dict[str, Any]], symbol: str, now: datetime
    ) -> tuple[VenueFill, ...]:
        multiplier = self._contract_multipliers[symbol]
        result: list[VenueFill] = []
        for item in rows:
            if item.get("instId") != symbol:
                continue
            side = str(item.get("side", "")).upper()
            if side not in {"BUY", "SELL"}:
                raise DomainRejected("OKX_RESPONSE_INVALID", "OKX fill side is invalid")
            result.append(
                VenueFill(
                    fill_id=str(item.get("tradeId") or ""),
                    order_id=str(item.get("ordId") or ""),
                    side=side,
                    quantity=_positive_decimal(item.get("fillSz"), "fillSz") * multiplier,
                    price=_positive_decimal(item.get("fillPx"), "fillPx"),
                    fee=abs(_decimal(item.get("fee", 0), "fee")),
                    fee_currency=str(item.get("feeCcy") or "USDT"),
                    executed_at=_time(item.get("fillTime", 0), now),
                )
            )
        if any(not item.fill_id or not item.order_id for item in result):
            raise DomainRejected("OKX_RESPONSE_INVALID", "OKX fill identity is incomplete")
        return tuple(result)

    @staticmethod
    def _parse_funding(
        rows: list[dict[str, Any]], symbol: str, now: datetime
    ) -> tuple[VenueFunding, ...]:
        result: list[VenueFunding] = []
        for item in rows:
            if item.get("instId") != symbol or str(item.get("subType")) not in {"173", "174"}:
                continue
            payment_id = str(item.get("billId") or "")
            if not payment_id:
                raise DomainRejected(
                    "OKX_RESPONSE_INVALID", "OKX funding identity is incomplete"
                )
            result.append(
                VenueFunding(
                    payment_id=payment_id,
                    amount=_decimal(item.get("pnl"), "pnl"),
                    currency=str(item.get("ccy") or "USDT"),
                    paid_at=_time(item.get("ts", 0), now),
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
