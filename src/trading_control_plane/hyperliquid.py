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
from typing import Any, TypeGuard

from trading_control_plane.domain import DomainRejected
from trading_control_plane.freqtrade import parse_hip3_dexes

JsonValue = dict[str, Any] | list[Any] | str
JsonFetcher = Callable[[str, dict[str, Any], float], JsonValue]
OFFICIAL_INFO_HOSTS = {
    "api.hyperliquid.xyz",
    "api.hyperliquid-testnet.xyz",
}
HISTORY_WINDOW = timedelta(days=30)
ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")
CORE_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9]{1,32}$")
HIP3_SYMBOL_PATTERN = re.compile(
    r"^(?P<dex>[a-z0-9][a-z0-9_-]{0,31}):(?P<coin>[A-Za-z0-9]{1,32})$"
)


def _is_core_symbol(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and CORE_SYMBOL_PATTERN.fullmatch(value) is not None


def _normalize_market_symbol(raw: object, *, dex: str) -> str:
    if not isinstance(raw, str):
        raise DomainRejected(
            "HYPERLIQUID_RESPONSE_INVALID",
            "Hyperliquid market symbol is not a string",
        )
    if not dex:
        if not _is_core_symbol(raw):
            raise DomainRejected(
                "HYPERLIQUID_RESPONSE_INVALID",
                "Hyperliquid Core market symbol is invalid",
            )
        return raw
    prefix = f"{dex}:"
    coin = raw[len(prefix) :] if raw.startswith(prefix) else raw
    if not _is_core_symbol(coin):
        raise DomainRejected(
            "HYPERLIQUID_RESPONSE_INVALID",
            "Hyperliquid HIP-3 market symbol is invalid",
        )
    return f"{dex}:{coin}"


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
    # Four bounded attempts keep a full probe below roughly 25 seconds while
    # still tolerating Hyperliquid's short 429 bursts.
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                body = response.read()
            break
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code != 429 or attempt == 3:
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
            if attempt < 2:
                time.sleep(0.25 * 2**attempt)
                continue
            break
    if body is None:
        if isinstance(last_error, urllib.error.HTTPError) and last_error.code == 429:
            raise DomainRejected(
                "HYPERLIQUID_RATE_LIMITED",
                "Hyperliquid Info API remained rate limited after bounded retries",
            ) from last_error
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
    """Official Info API reader for Core facts and configured HIP-3 market catalogs."""

    def __init__(
        self,
        *,
        base_url: str,
        account_address: str | None,
        api_wallet_address: str | None = None,
        dex: str = "",
        hip3_dexes: tuple[str, ...] = (),
        fetcher: JsonFetcher = _default_fetcher,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_INFO_HOSTS:
            raise ValueError("Hyperliquid read-only base URL must use an official API host")
        if dex:
            raise ValueError("the Core reader requires dex to remain empty")
        self._base_url = base_url.rstrip("/")
        self._host = parsed.hostname
        self._account_address = account_address
        self._api_wallet_address = api_wallet_address
        self._hip3_dexes = parse_hip3_dexes(",".join(hip3_dexes))
        self._fetcher = fetcher
        self._account_abstraction: str | None = None

    @property
    def configured(self) -> bool:
        return bool(
            (self._account_address and ADDRESS_PATTERN.fullmatch(self._account_address))
            or (
                self._api_wallet_address
                and ADDRESS_PATTERN.fullmatch(self._api_wallet_address)
            )
        )

    @property
    def fact_environment(self) -> str:
        return "TESTNET" if self._host == "api.hyperliquid-testnet.xyz" else "LIVE"

    def read_instrument(self, symbol: str) -> HyperliquidInstrument:
        dex = self._symbol_dex(symbol)
        meta_contexts = self._info({"type": "metaAndAssetCtxs", "dex": dex})
        instrument, _mark_price = self._parse_instrument(meta_contexts, symbol, dex=dex)
        return instrument

    def _symbol_dex(self, symbol: str) -> str:
        if _is_core_symbol(symbol):
            return ""
        match = HIP3_SYMBOL_PATTERN.fullmatch(symbol)
        if match is None or match.group("dex") not in self._hip3_dexes:
            raise DomainRejected(
                "HYPERLIQUID_SYMBOL_INVALID",
                "Hyperliquid symbol must be Core or belong to an explicitly configured HIP-3 DEX",
            )
        return match.group("dex")

    def read_active_instruments(self) -> tuple[HyperliquidInstrument, ...]:
        """Read Core plus explicitly allowed HIP-3 catalogs from the official Info API."""

        instruments: list[HyperliquidInstrument] = []
        for dex in ("", *self._hip3_dexes):
            instruments.extend(self._read_active_instruments_for_dex(dex))
        symbols = [instrument.symbol for instrument in instruments]
        if len(symbols) != len(set(symbols)):
            raise DomainRejected(
                "HYPERLIQUID_RESPONSE_INVALID",
                "Core and HIP-3 active catalogs contain duplicate instrument identities",
            )
        return tuple(sorted(instruments, key=lambda item: item.symbol))

    def _read_active_instruments_for_dex(self, dex: str) -> list[HyperliquidInstrument]:
        meta_contexts = self._info({"type": "metaAndAssetCtxs", "dex": dex})
        if not isinstance(meta_contexts, list) or len(meta_contexts) != 2:
            raise DomainRejected(
                "HYPERLIQUID_RESPONSE_INVALID", "metaAndAssetCtxs response is invalid"
            )
        meta = _require_dict(meta_contexts[0], "meta")
        collateral_token = meta.get("collateralToken")
        if dex and collateral_token not in {None, 0}:
            raise DomainRejected(
                "HYPERLIQUID_HIP3_COLLATERAL_UNSUPPORTED",
                "configured HIP-3 DEX does not use the worker's USDC collateral scope",
            )
        universe = _require_dict_list(meta.get("universe"), "meta universe")
        contexts = _require_dict_list(meta_contexts[1], "asset contexts")
        if not universe or len(universe) != len(contexts):
            raise DomainRejected(
                "HYPERLIQUID_RESPONSE_INVALID", "active perpetual catalog is invalid"
            )
        result: list[HyperliquidInstrument] = []
        for index, item in enumerate(universe):
            if bool(item.get("isDelisted", False)):
                continue
            raw_symbol = item.get("name")
            if not isinstance(raw_symbol, str):
                raise DomainRejected(
                    "HYPERLIQUID_RESPONSE_INVALID",
                    "active perpetual catalog symbols are invalid",
                )
            symbol = _normalize_market_symbol(raw_symbol, dex=dex)
            result.append(self._parse_instrument_at(meta_contexts, index, symbol)[0])
        return result

    def _info(self, payload: dict[str, Any]) -> JsonValue:
        return self._fetcher(f"{self._base_url}/info", payload, 5.0)

    def _resolved_account(self) -> str:
        if self._account_address and ADDRESS_PATTERN.fullmatch(self._account_address):
            return self._account_address
        if not self.configured:
            raise DomainRejected(
                "HYPERLIQUID_READ_ONLY_NOT_CONFIGURED",
                "a valid selected Hyperliquid account or API wallet address is required",
            )
        resolved = resolve_hyperliquid_main_account(
            base_url=self._base_url,
            account_address=None,
            api_wallet_address=self._api_wallet_address,
            fetcher=self._fetcher,
        )
        if resolved is None:
            raise DomainRejected(
                "HYPERLIQUID_ACCOUNT_UNRESOLVED",
                "Hyperliquid API wallet did not resolve to its owning main account",
            )
        self._account_address = resolved
        return resolved

    def _abstraction(self, account_address: str) -> str:
        if self._account_abstraction is not None:
            return self._account_abstraction
        raw = self._info({"type": "userAbstraction", "user": account_address})
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
        account_address = self._resolved_account()
        dex = self._symbol_dex(symbol)
        meta_contexts = self._info({"type": "metaAndAssetCtxs", "dex": dex})
        clearinghouse = self._info(
            {"type": "clearinghouseState", "user": account_address, "dex": dex}
        )
        abstraction = self._abstraction(account_address)
        spot_state = (
            self._info({"type": "spotClearinghouseState", "user": account_address})
            if abstraction in {"unifiedAccount", "portfolioMargin"}
            else None
        )

        orders = self._info(
            {"type": "frontendOpenOrders", "user": account_address, "dex": dex}
        )
        start_time_ms = int((now - HISTORY_WINDOW).timestamp() * 1_000)
        fills = self._info(
            {
                "type": "userFillsByTime",
                "user": account_address,
                "startTime": start_time_ms,
                "aggregateByTime": True,
            }
        )
        funding = self._info(
            {
                "type": "userFunding",
                "user": account_address,
                "startTime": start_time_ms,
            }
        )
        instrument, mark_price = self._parse_instrument(meta_contexts, symbol, dex=dex)
        position, equity = self._parse_account(clearinghouse, symbol, mark_price, now, dex=dex)
        if spot_state is not None:
            equity = self._parse_unified_equity(spot_state, now)
        parsed_orders = self._parse_orders(orders, symbol, now, dex=dex)
        return HyperliquidReadOnlySnapshot(
            symbol=symbol,
            observed_at=now,
            instrument=instrument,
            orders=parsed_orders,
            fills=self._parse_fills(fills, symbol, now, dex=dex),
            position=position,
            equity=equity,
            funding=self._parse_funding(funding, symbol, now, dex=dex),
            protection=self._select_protection(parsed_orders, position),
        )

    def read_account_snapshots(
        self, symbols: tuple[str, ...], *, now: datetime
    ) -> tuple[HyperliquidReadOnlySnapshot, ...]:
        """Project configured Core and HIP-3 symbols from their venue-scoped responses."""

        configured = self._validated_symbols(symbols)
        account_address = self._resolved_account()
        abstraction = self._abstraction(account_address)
        spot_state = (
            self._info({"type": "spotClearinghouseState", "user": account_address})
            if abstraction in {"unifiedAccount", "portfolioMargin"}
            else None
        )
        current_snapshots: list[HyperliquidReadOnlySnapshot] = []
        configured_by_dex: dict[str, set[str]] = {}
        for symbol in configured:
            configured_by_dex.setdefault(self._symbol_dex(symbol), set()).add(symbol)
        for dex, scoped_symbols in sorted(configured_by_dex.items()):
            meta_contexts = self._info({"type": "metaAndAssetCtxs", "dex": dex})
            clearinghouse = self._info(
                {"type": "clearinghouseState", "user": account_address, "dex": dex}
            )
            orders = self._info(
                {"type": "frontendOpenOrders", "user": account_address, "dex": dex}
            )
            target_symbols = (
                scoped_symbols
                | self._active_position_symbols(clearinghouse, dex=dex)
                | self._order_symbols(orders, dex=dex)
            )
            for symbol in sorted(target_symbols):
                instrument, mark_price = self._parse_instrument(
                    meta_contexts, symbol, dex=dex
                )
                position, equity = self._parse_account(
                    clearinghouse, symbol, mark_price, now, dex=dex
                )
                if spot_state is not None:
                    equity = self._parse_unified_equity(spot_state, now)
                parsed_orders = self._parse_orders(orders, symbol, now, dex=dex)
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
                    "user": account_address,
                    "startTime": start_time_ms,
                    "aggregateByTime": True,
                }
            )
            funding = self._info(
                {
                    "type": "userFunding",
                    "user": account_address,
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
                    fills=self._parse_fills(
                        fill_rows,
                        snapshot.symbol,
                        now,
                        dex=self._symbol_dex(snapshot.symbol),
                    ),
                    funding=self._parse_funding(
                        funding_rows,
                        snapshot.symbol,
                        now,
                        dex=self._symbol_dex(snapshot.symbol),
                    ),
                )
                for snapshot in current_snapshots
            ]
        except DomainRejected as exc:
            return tuple(
                replace(snapshot, history_error_code=exc.code) for snapshot in current_snapshots
            )
        return tuple(snapshots)

    def _validated_symbols(self, symbols: tuple[str, ...]) -> set[str]:
        result = set(symbols)
        if not result:
            raise DomainRejected(
                "HYPERLIQUID_SYMBOL_INVALID",
                "at least one configured Hyperliquid symbol is required",
            )
        for symbol in result:
            self._symbol_dex(symbol)
        return result

    @staticmethod
    def _active_position_symbols(raw: JsonValue, *, dex: str = "") -> set[str]:
        state = _require_dict(raw, "clearinghouseState")
        wrappers = _require_dict_list(state.get("assetPositions", []), "assetPositions")
        active: set[str] = set()
        seen: set[str] = set()
        for wrapper in wrappers:
            if wrapper.get("type", "oneWay") != "oneWay":
                raise DomainRejected("HYPERLIQUID_RESPONSE_INVALID", "position type is unsupported")
            position = _require_dict(wrapper.get("position"), "position")
            symbol = _normalize_market_symbol(position.get("coin"), dex=dex)
            if symbol in seen:
                raise DomainRejected(
                    "HYPERLIQUID_RESPONSE_INVALID", "position symbol is invalid or duplicated"
                )
            seen.add(symbol)
            if _decimal(position.get("szi"), "szi") != 0:
                active.add(symbol)
        return active

    @staticmethod
    def _order_symbols(raw: JsonValue, *, dex: str = "") -> set[str]:
        result: set[str] = set()
        for order in _require_dict_list(raw, "frontendOpenOrders"):
            symbol = _normalize_market_symbol(order.get("coin"), dex=dex)
            result.add(symbol)
        return result

    @staticmethod
    def _parse_instrument(
        raw: JsonValue, symbol: str, *, dex: str = ""
    ) -> tuple[HyperliquidInstrument, Decimal]:
        if not isinstance(raw, list) or len(raw) != 2:
            raise DomainRejected(
                "HYPERLIQUID_RESPONSE_INVALID", "metaAndAssetCtxs response is invalid"
            )
        meta = _require_dict(raw[0], "meta")
        universe = _require_dict_list(meta.get("universe"), "meta universe")
        contexts = _require_dict_list(raw[1], "asset contexts")
        index = next(
            (
                i
                for i, item in enumerate(universe)
                if _normalize_market_symbol(item.get("name"), dex=dex) == symbol
            ),
            None,
        )
        if index is None or index >= len(contexts):
            raise DomainRejected(
                "HYPERLIQUID_INSTRUMENT_UNAVAILABLE",
                "Hyperliquid perpetual instrument is unavailable in the selected market scope",
            )
        return HyperliquidReadOnlyClient._parse_instrument_at(raw, index, symbol)

    @staticmethod
    def _parse_instrument_at(
        raw: JsonValue, index: int, symbol: str
    ) -> tuple[HyperliquidInstrument, Decimal]:
        if not isinstance(raw, list) or len(raw) != 2:
            raise DomainRejected(
                "HYPERLIQUID_RESPONSE_INVALID", "metaAndAssetCtxs response is invalid"
            )
        meta = _require_dict(raw[0], "meta")
        universe = _require_dict_list(meta.get("universe"), "meta universe")
        contexts = _require_dict_list(raw[1], "asset contexts")
        if index < 0 or index >= len(universe) or index >= len(contexts):
            raise DomainRejected(
                "HYPERLIQUID_RESPONSE_INVALID", "instrument metadata index is invalid"
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
        raw: JsonValue, symbol: str, mark_price: Decimal, now: datetime, *, dex: str = ""
    ) -> tuple[HyperliquidPosition, HyperliquidEquity]:
        state = _require_dict(raw, "clearinghouseState")
        summary = _require_dict(state.get("marginSummary"), "marginSummary")
        observed_at = _time(state.get("time", 0), now)
        positions = _require_dict_list(state.get("assetPositions", []), "assetPositions")
        position_data: dict[str, Any] | None = None
        for wrapper in positions:
            candidate = wrapper.get("position")
            if (
                isinstance(candidate, dict)
                and _normalize_market_symbol(candidate.get("coin"), dex=dex) == symbol
            ):
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
    def _parse_orders(
        raw: JsonValue, symbol: str, now: datetime, *, dex: str = ""
    ) -> tuple[HyperliquidOrder, ...]:
        result: list[HyperliquidOrder] = []
        for item in _require_dict_list(raw, "frontendOpenOrders"):
            if _normalize_market_symbol(item.get("coin"), dex=dex) != symbol:
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
    def _parse_fills(
        raw: JsonValue, symbol: str, now: datetime, *, dex: str = ""
    ) -> tuple[HyperliquidFill, ...]:
        del dex  # History is global; HIP-3 identities must remain fully qualified.
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
        raw: JsonValue, symbol: str, now: datetime, *, dex: str = ""
    ) -> tuple[HyperliquidFunding, ...]:
        del dex  # History is global; HIP-3 identities must remain fully qualified.
        result: list[HyperliquidFunding] = []
        for item in _require_dict_list(raw, "userFunding"):
            delta = item.get("delta")
            if not isinstance(delta, dict):
                raise DomainRejected(
                    "HYPERLIQUID_RESPONSE_INVALID", "funding delta is invalid"
                )
            if delta.get("coin") != symbol:
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
