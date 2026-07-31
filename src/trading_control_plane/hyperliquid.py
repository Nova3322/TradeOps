from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_control_plane.domain import DomainRejected

JsonValue = dict[str, Any] | list[Any] | str
JsonFetcher = Callable[[str, dict[str, Any], float], JsonValue]
OFFICIAL_INFO_HOSTS = {
    "api.hyperliquid.xyz",
    "api.hyperliquid-testnet.xyz",
}
HISTORY_WINDOW = timedelta(days=30)
ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")


def resolve_hyperliquid_main_account(
    *,
    base_url: str,
    account_address: str | None,
    api_wallet_address: str | None,
    fetcher: JsonFetcher | None = None,
) -> str | None:
    """Resolve an API wallet's owning main account through the official userRole query."""

    if account_address is not None:
        if not ADDRESS_PATTERN.fullmatch(account_address):
            raise ValueError("Hyperliquid main account address is invalid")
        return account_address
    if api_wallet_address is None:
        return None
    if not ADDRESS_PATTERN.fullmatch(api_wallet_address):
        raise ValueError("Hyperliquid API wallet address is invalid")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_INFO_HOSTS:
        raise ValueError("Hyperliquid account resolution requires an official API host")
    resolved_fetcher = fetcher or _default_fetcher
    raw = resolved_fetcher(
        f"{base_url.rstrip('/')}/info",
        {"type": "userRole", "user": api_wallet_address},
        5.0,
    )
    if not isinstance(raw, dict) or raw.get("role") != "agent":
        raise DomainRejected(
            "HYPERLIQUID_ACCOUNT_UNRESOLVED",
            "configured API wallet is not an authorized Hyperliquid agent",
        )
    data = raw.get("data")
    user = data.get("user") if isinstance(data, dict) else None
    if not isinstance(user, str) or not ADDRESS_PATTERN.fullmatch(user):
        raise DomainRejected(
            "HYPERLIQUID_ACCOUNT_UNRESOLVED",
            "Hyperliquid agent role omitted its owning main account",
        )
    return user


def _default_fetcher(url: str, payload: dict[str, Any], timeout: float) -> JsonValue:
    request = urllib.request.Request(  # noqa: S310
        url,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    body: bytes | None = None
    last_error: BaseException | None = None
    for attempt in range(6):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                body = response.read()
            break
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code != 429 or attempt == 5:
                break
            retry_after = exc.headers.get("Retry-After")
            try:
                delay = (
                    min(10.0, max(0.5, float(retry_after)))
                    if retry_after
                    else min(10.0, 0.5 * 2**attempt)
                )
            except ValueError:
                delay = min(10.0, 0.5 * 2**attempt)
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            break
    if body is None:
        raise DomainRejected(
            "HYPERLIQUID_READ_ONLY_UNAVAILABLE",
            "Hyperliquid Info API is unavailable",
        ) from last_error
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DomainRejected(
            "HYPERLIQUID_RESPONSE_INVALID",
            "Hyperliquid Info API returned invalid JSON",
        ) from exc
    if not isinstance(value, (dict, list, str)):
        raise DomainRejected(
            "HYPERLIQUID_RESPONSE_INVALID",
            "Hyperliquid Info API response shape is invalid",
        )
    return value


def _decimal(raw: Any, field: str, *, minimum: Decimal | None = None) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DomainRejected(
            "HYPERLIQUID_RESPONSE_INVALID",
            f"Hyperliquid field {field} is not numeric",
        ) from exc
    if not value.is_finite() or (minimum is not None and value < minimum):
        raise DomainRejected(
            "HYPERLIQUID_RESPONSE_INVALID",
            f"Hyperliquid field {field} is outside its valid range",
        )
    return value


def _positive_decimal(raw: Any, field: str) -> Decimal:
    value = _decimal(raw, field)
    if value <= 0:
        raise DomainRejected(
            "HYPERLIQUID_RESPONSE_INVALID",
            f"Hyperliquid field {field} must be positive",
        )
    return value


def _time(raw: Any, fallback: datetime) -> datetime:
    try:
        milliseconds = int(raw)
        return fallback if milliseconds <= 0 else datetime.fromtimestamp(milliseconds / 1000, UTC)
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        raise DomainRejected(
            "HYPERLIQUID_RESPONSE_INVALID",
            "Hyperliquid timestamp is invalid",
        ) from exc


def _require_dict(raw: JsonValue | Any, name: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise DomainRejected(
            "HYPERLIQUID_RESPONSE_INVALID", f"Hyperliquid {name} response is invalid"
        )
    return raw


def _require_dict_list(raw: JsonValue | Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise DomainRejected(
            "HYPERLIQUID_RESPONSE_INVALID", f"Hyperliquid {name} response is invalid"
        )
    return raw


@dataclass(frozen=True, slots=True)
class HyperliquidInstrument:
    symbol: str
    tick_size: Decimal
    lot_size: Decimal
    minimum_notional: Decimal
    quote_currency: str
    collateral_currency: str
    active: bool


@dataclass(frozen=True, slots=True)
class HyperliquidOrder:
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
class HyperliquidFill:
    fill_id: str
    order_id: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_currency: str
    executed_at: datetime


@dataclass(frozen=True, slots=True)
class HyperliquidPosition:
    quantity: Decimal
    average_entry_price: Decimal
    mark_price: Decimal
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class HyperliquidEquity:
    equity: Decimal
    available_balance: Decimal
    currency: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class HyperliquidFunding:
    payment_id: str
    amount: Decimal
    currency: str
    paid_at: datetime


@dataclass(frozen=True, slots=True)
class HyperliquidProtection:
    order_id: str
    quantity: Decimal
    trigger_price: Decimal
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class HyperliquidReadOnlySnapshot:
    symbol: str
    observed_at: datetime
    instrument: HyperliquidInstrument
    orders: tuple[HyperliquidOrder, ...]
    fills: tuple[HyperliquidFill, ...]
    position: HyperliquidPosition
    equity: HyperliquidEquity
    funding: tuple[HyperliquidFunding, ...]
    protection: HyperliquidProtection | None
    history_error_code: str | None = None


class HyperliquidReadOnlyClient:
    """Narrow Hyperliquid Core reader; HIP-3 and Exchange actions are absent."""

    def __init__(
        self,
        *,
        base_url: str,
        account_address: str | None,
        dex: str = "",
        fetcher: JsonFetcher = _default_fetcher,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_INFO_HOSTS:
            raise ValueError("Hyperliquid read-only base URL must use an official API host")
        if dex:
            raise ValueError("this adapter supports Hyperliquid Core only; dex must be empty")
        self._base_url = base_url.rstrip("/")
        self._host = parsed.hostname
        self._account_address = account_address
        self._fetcher = fetcher
        self._account_abstraction: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self._account_address and ADDRESS_PATTERN.fullmatch(self._account_address))

    @property
    def fact_environment(self) -> str:
        return "TESTNET" if self._host == "api.hyperliquid-testnet.xyz" else "LIVE"

    def _info(self, payload: dict[str, Any]) -> JsonValue:
        return self._fetcher(f"{self._base_url}/info", payload, 5.0)

    def _abstraction(self) -> str:
        if self._account_abstraction is not None:
            return self._account_abstraction
        assert self._account_address is not None
        raw = self._info({"type": "userAbstraction", "user": self._account_address})
        if not isinstance(raw, str) or raw not in {
            "disabled",
            "default",
            "dexAbstraction",
            "unifiedAccount",
            "portfolioMargin",
        }:
            raise DomainRejected(
                "HYPERLIQUID_RESPONSE_INVALID",
                "Hyperliquid userAbstraction response is invalid",
            )
        self._account_abstraction = raw
        return raw

    def read_snapshot(self, symbol: str, *, now: datetime) -> HyperliquidReadOnlySnapshot:
        if not self.configured:
            raise DomainRejected(
                "HYPERLIQUID_READ_ONLY_NOT_CONFIGURED",
                "a valid selected Hyperliquid account address is required",
            )
        if not symbol or symbol != symbol.upper() or not re.fullmatch(r"[A-Z0-9]+", symbol):
            raise DomainRejected(
                "HYPERLIQUID_SYMBOL_INVALID",
                "Hyperliquid Core symbol must be uppercase alphanumeric",
            )
        assert self._account_address is not None
        meta_contexts = self._info({"type": "metaAndAssetCtxs", "dex": ""})
        clearinghouse = self._info(
            {"type": "clearinghouseState", "user": self._account_address, "dex": ""}
        )
        abstraction = self._abstraction()
        spot_state = (
            self._info({"type": "spotClearinghouseState", "user": self._account_address})
            if abstraction in {"unifiedAccount", "portfolioMargin"}
            else None
        )

        orders = self._info(
            {"type": "frontendOpenOrders", "user": self._account_address, "dex": ""}
        )
        start_time_ms = int((now - HISTORY_WINDOW).timestamp() * 1_000)
        fills = self._info(
            {
                "type": "userFillsByTime",
                "user": self._account_address,
                "startTime": start_time_ms,
                "aggregateByTime": True,
            }
        )
        funding = self._info(
            {
                "type": "userFunding",
                "user": self._account_address,
                "startTime": start_time_ms,
            }
        )
        instrument, mark_price = self._parse_instrument(meta_contexts, symbol)
        position, equity = self._parse_account(clearinghouse, symbol, mark_price, now)
        if spot_state is not None:
            equity = self._parse_unified_equity(spot_state, now)
        parsed_orders = self._parse_orders(orders, symbol, now)
        return HyperliquidReadOnlySnapshot(
            symbol=symbol,
            observed_at=now,
            instrument=instrument,
            orders=parsed_orders,
            fills=self._parse_fills(fills, symbol, now),
            position=position,
            equity=equity,
            funding=self._parse_funding(funding, symbol, now),
            protection=self._select_protection(parsed_orders, position),
        )

    def read_account_snapshots(
        self, symbols: tuple[str, ...], *, now: datetime
    ) -> tuple[HyperliquidReadOnlySnapshot, ...]:
        """Project every covered Core symbol from one complete clearinghouse response."""

        configured = self._validated_symbols(symbols)
        if not self.configured:
            raise DomainRejected(
                "HYPERLIQUID_READ_ONLY_NOT_CONFIGURED",
                "a valid selected Hyperliquid account address is required",
            )
        assert self._account_address is not None
        meta_contexts = self._info({"type": "metaAndAssetCtxs", "dex": ""})
        clearinghouse = self._info(
            {"type": "clearinghouseState", "user": self._account_address, "dex": ""}
        )
        abstraction = self._abstraction()
        spot_state = (
            self._info({"type": "spotClearinghouseState", "user": self._account_address})
            if abstraction in {"unifiedAccount", "portfolioMargin"}
            else None
        )
        orders = self._info(
            {"type": "frontendOpenOrders", "user": self._account_address, "dex": ""}
        )
        target_symbols = (
            configured | self._active_position_symbols(clearinghouse) | self._order_symbols(orders)
        )
        current_snapshots: list[HyperliquidReadOnlySnapshot] = []
        for symbol in sorted(target_symbols):
            instrument, mark_price = self._parse_instrument(meta_contexts, symbol)
            position, equity = self._parse_account(clearinghouse, symbol, mark_price, now)
            if spot_state is not None:
                equity = self._parse_unified_equity(spot_state, now)
            parsed_orders = self._parse_orders(orders, symbol, now)
            current_snapshots.append(
                HyperliquidReadOnlySnapshot(
                    symbol=symbol,
                    observed_at=now,
                    instrument=instrument,
                    orders=parsed_orders,
                    fills=(),
                    position=position,
                    equity=equity,
                    funding=(),
                    protection=self._select_protection(parsed_orders, position),
                )
            )
        start_time_ms = int((now - HISTORY_WINDOW).timestamp() * 1_000)
        try:
            fills = self._info(
                {
                    "type": "userFillsByTime",
                    "user": self._account_address,
                    "startTime": start_time_ms,
                    "aggregateByTime": True,
                }
            )
            funding = self._info(
                {
                    "type": "userFunding",
                    "user": self._account_address,
                    "startTime": start_time_ms,
                }
            )
            fill_rows = _require_dict_list(fills, "userFills")
            funding_rows = _require_dict_list(funding, "userFunding")
            if len(fill_rows) >= 500 or len(funding_rows) >= 500:
                raise DomainRejected(
                    "HYPERLIQUID_RESPONSE_INCOMPLETE",
                    "Hyperliquid account history reached an endpoint result limit",
                )
            snapshots = [
                replace(
                    snapshot,
                    fills=self._parse_fills(fill_rows, snapshot.symbol, now),
                    funding=self._parse_funding(funding_rows, snapshot.symbol, now),
                )
                for snapshot in current_snapshots
            ]
        except DomainRejected as exc:
            return tuple(
                replace(snapshot, history_error_code=exc.code) for snapshot in current_snapshots
            )
        return tuple(snapshots)

    @staticmethod
    def _validated_symbols(symbols: tuple[str, ...]) -> set[str]:
        result = set(symbols)
        if not result or any(
            not symbol or symbol != symbol.upper() or not re.fullmatch(r"[A-Z0-9]+", symbol)
            for symbol in result
        ):
            raise DomainRejected(
                "HYPERLIQUID_SYMBOL_INVALID",
                "Hyperliquid Core symbol must be uppercase alphanumeric",
            )
        return result

    @staticmethod
    def _active_position_symbols(raw: JsonValue) -> set[str]:
        state = _require_dict(raw, "clearinghouseState")
        wrappers = _require_dict_list(state.get("assetPositions", []), "assetPositions")
        active: set[str] = set()
        seen: set[str] = set()
        for wrapper in wrappers:
            if wrapper.get("type", "oneWay") != "oneWay":
                raise DomainRejected("HYPERLIQUID_RESPONSE_INVALID", "position type is unsupported")
            position = _require_dict(wrapper.get("position"), "position")
            symbol = position.get("coin")
            if (
                not isinstance(symbol, str)
                or not symbol
                or symbol != symbol.upper()
                or not re.fullmatch(r"[A-Z0-9]+", symbol)
                or symbol in seen
            ):
                raise DomainRejected(
                    "HYPERLIQUID_RESPONSE_INVALID", "position symbol is invalid or duplicated"
                )
            seen.add(symbol)
            if _decimal(position.get("szi"), "szi") != 0:
                active.add(symbol)
        return active

    @staticmethod
    def _order_symbols(raw: JsonValue) -> set[str]:
        result: set[str] = set()
        for order in _require_dict_list(raw, "frontendOpenOrders"):
            symbol = order.get("coin")
            if (
                not isinstance(symbol, str)
                or not symbol
                or symbol != symbol.upper()
                or not re.fullmatch(r"[A-Z0-9]+", symbol)
            ):
                raise DomainRejected("HYPERLIQUID_RESPONSE_INVALID", "order symbol is invalid")
            result.add(symbol)
        return result

    @staticmethod
    def _parse_instrument(raw: JsonValue, symbol: str) -> tuple[HyperliquidInstrument, Decimal]:
        if not isinstance(raw, list) or len(raw) != 2:
            raise DomainRejected(
                "HYPERLIQUID_RESPONSE_INVALID", "metaAndAssetCtxs response is invalid"
            )
        meta = _require_dict(raw[0], "meta")
        universe = _require_dict_list(meta.get("universe"), "meta universe")
        contexts = _require_dict_list(raw[1], "asset contexts")
        index = next((i for i, item in enumerate(universe) if item.get("name") == symbol), None)
        if index is None or index >= len(contexts):
            raise DomainRejected(
                "HYPERLIQUID_INSTRUMENT_UNAVAILABLE",
                "Hyperliquid Core perpetual instrument is unavailable",
            )
        item = universe[index]
        try:
            size_decimals = int(item["szDecimals"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainRejected(
                "HYPERLIQUID_RESPONSE_INVALID", "instrument size precision is invalid"
            ) from exc
        if size_decimals < 0 or size_decimals > 6:
            raise DomainRejected(
                "HYPERLIQUID_RESPONSE_INVALID", "instrument size precision is invalid"
            )
        mark_price = _positive_decimal(contexts[index].get("markPx"), "markPx")
        return (
            HyperliquidInstrument(
                symbol=symbol,
                tick_size=Decimal(1).scaleb(-(6 - size_decimals)),
                lot_size=Decimal(1).scaleb(-size_decimals),
                minimum_notional=Decimal(10),
                quote_currency="USDC",
                collateral_currency="USDC",
                active=not bool(item.get("isDelisted", False)),
            ),
            mark_price,
        )

    @staticmethod
    def _parse_account(
        raw: JsonValue, symbol: str, mark_price: Decimal, now: datetime
    ) -> tuple[HyperliquidPosition, HyperliquidEquity]:
        state = _require_dict(raw, "clearinghouseState")
        summary = _require_dict(state.get("marginSummary"), "marginSummary")
        observed_at = _time(state.get("time", 0), now)
        positions = _require_dict_list(state.get("assetPositions", []), "assetPositions")
        position_data: dict[str, Any] | None = None
        for wrapper in positions:
            candidate = wrapper.get("position")
            if isinstance(candidate, dict) and candidate.get("coin") == symbol:
                position_data = candidate
                break
        if position_data is None:
            quantity = Decimal(0)
            entry_price = Decimal(0)
        else:
            quantity = _decimal(position_data.get("szi"), "szi")
            entry_price = (
                Decimal(0)
                if quantity == 0
                else _positive_decimal(position_data.get("entryPx"), "entryPx")
            )
        return (
            HyperliquidPosition(
                quantity=quantity,
                average_entry_price=entry_price,
                mark_price=mark_price,
                observed_at=observed_at,
            ),
            HyperliquidEquity(
                equity=_decimal(summary.get("accountValue"), "accountValue", minimum=Decimal(0)),
                available_balance=_decimal(
                    state.get("withdrawable"), "withdrawable", minimum=Decimal(0)
                ),
                currency="USDC",
                observed_at=observed_at,
            ),
        )

    @staticmethod
    def _parse_unified_equity(raw: JsonValue, now: datetime) -> HyperliquidEquity:
        state = _require_dict(raw, "spotClearinghouseState")
        balances = _require_dict_list(state.get("balances", []), "spot balances")
        usdc = next((item for item in balances if item.get("coin") == "USDC"), None)
        if usdc is None:
            raise DomainRejected(
                "HYPERLIQUID_RESPONSE_INVALID",
                "unified Hyperliquid account omitted its USDC balance",
            )
        total = _decimal(usdc.get("total"), "USDC total", minimum=Decimal(0))
        hold = _decimal(usdc.get("hold", 0), "USDC hold", minimum=Decimal(0))
        if hold > total:
            raise DomainRejected(
                "HYPERLIQUID_RESPONSE_INVALID",
                "unified Hyperliquid USDC hold exceeds total balance",
            )
        return HyperliquidEquity(
            equity=total,
            available_balance=total - hold,
            currency="USDC",
            observed_at=now,
        )

    @staticmethod
    def _parse_orders(raw: JsonValue, symbol: str, now: datetime) -> tuple[HyperliquidOrder, ...]:
        result: list[HyperliquidOrder] = []
        for item in _require_dict_list(raw, "frontendOpenOrders"):
            if item.get("coin") != symbol:
                continue
            try:
                order_id = str(item["oid"])
                side_code = str(item["side"])
            except KeyError as exc:
                raise DomainRejected(
                    "HYPERLIQUID_RESPONSE_INVALID", "order identity is incomplete"
                ) from exc
            if side_code not in {"A", "B"}:
                raise DomainRejected("HYPERLIQUID_RESPONSE_INVALID", "order side is invalid")
            ordered = _positive_decimal(item.get("origSz"), "origSz")
            remaining = _decimal(item.get("sz"), "sz", minimum=Decimal(0))
            if remaining > ordered:
                raise DomainRejected(
                    "HYPERLIQUID_RESPONSE_INVALID", "order remaining size exceeds original size"
                )
            client_order_id = str(item.get("cloid") or f"hl-oid-{order_id}")
            trigger = _decimal(item.get("triggerPx", 0), "triggerPx", minimum=Decimal(0))
            is_trigger = bool(item.get("isTrigger", False))
            result.append(
                HyperliquidOrder(
                    order_id=order_id,
                    client_order_id=client_order_id,
                    status="PARTIALLY_FILLED" if remaining < ordered else "SENT",
                    side="BUY" if side_code == "B" else "SELL",
                    order_type=(
                        "TRIGGER_MARKET" if is_trigger else str(item.get("orderType", "LIMIT"))
                    ),
                    ordered_quantity=ordered,
                    filled_quantity=ordered - remaining,
                    stop_price=trigger,
                    reduce_only=bool(item.get("reduceOnly", False)),
                    close_position=False,
                    observed_at=_time(item.get("timestamp", 0), now),
                )
            )
        return tuple(result)

    @staticmethod
    def _parse_fills(raw: JsonValue, symbol: str, now: datetime) -> tuple[HyperliquidFill, ...]:
        result: list[HyperliquidFill] = []
        for item in _require_dict_list(raw, "userFills"):
            if item.get("coin") != symbol:
                continue
            try:
                fill_id = str(item["tid"])
                order_id = str(item["oid"])
                side_code = str(item["side"])
            except KeyError as exc:
                raise DomainRejected(
                    "HYPERLIQUID_RESPONSE_INVALID", "fill identity is incomplete"
                ) from exc
            if side_code not in {"A", "B"}:
                raise DomainRejected("HYPERLIQUID_RESPONSE_INVALID", "fill side is invalid")
            fee = _decimal(item.get("fee", 0), "fee")
            result.append(
                HyperliquidFill(
                    fill_id=fill_id,
                    order_id=order_id,
                    side="BUY" if side_code == "B" else "SELL",
                    quantity=_positive_decimal(item.get("sz"), "sz"),
                    price=_positive_decimal(item.get("px"), "px"),
                    fee=abs(fee),
                    fee_currency=str(item.get("feeToken") or "USDC"),
                    executed_at=_time(item.get("time", 0), now),
                )
            )
        return tuple(result)

    @staticmethod
    def _parse_funding(
        raw: JsonValue, symbol: str, now: datetime
    ) -> tuple[HyperliquidFunding, ...]:
        result: list[HyperliquidFunding] = []
        for item in _require_dict_list(raw, "userFunding"):
            delta = item.get("delta")
            if not isinstance(delta, dict) or delta.get("coin") != symbol:
                continue
            paid_at = _time(item.get("time", 0), now)
            payment_id = str(
                item.get("hash")
                or f"{symbol}:{int(paid_at.timestamp() * 1000)}:{delta.get('usdc')}"
            )
            result.append(
                HyperliquidFunding(
                    payment_id=payment_id,
                    amount=_decimal(delta.get("usdc"), "funding usdc"),
                    currency="USDC",
                    paid_at=paid_at,
                )
            )
        return tuple(result)

    @staticmethod
    def _select_protection(
        orders: tuple[HyperliquidOrder, ...], position: HyperliquidPosition
    ) -> HyperliquidProtection | None:
        if position.quantity == 0:
            return None
        required_side = "SELL" if position.quantity > 0 else "BUY"
        candidates = tuple(
            order
            for order in orders
            if order.side == required_side
            and order.reduce_only
            and order.order_type == "TRIGGER_MARKET"
            and order.stop_price > 0
            and order.status in {"SENT", "PARTIALLY_FILLED"}
        )
        if not candidates:
            return None
        selected = max(candidates, key=lambda item: item.ordered_quantity - item.filled_quantity)
        return HyperliquidProtection(
            order_id=selected.order_id,
            quantity=selected.ordered_quantity - selected.filled_quantity,
            trigger_price=selected.stop_price,
            observed_at=selected.observed_at,
        )
