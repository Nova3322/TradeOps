from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from trading_control_plane.domain import DomainRejected

JsonObject = dict[str, Any]

FREQTRADE_TRANSACTION_RPC_TYPES = (
    "entry",
    "entry_fill",
    "entry_cancel",
    "exit",
    "exit_fill",
    "exit_cancel",
    "protection_trigger",
    "protection_trigger_global",
    "strategy_msg",
    "status",
    "startup",
    "warning",
)

BINANCE_PERPETUAL_PATTERN = re.compile(r"^(?P<base>[^\s/:]{1,64})USDT$")
OKX_PERPETUAL_PATTERN = re.compile(r"^(?P<base>[A-Z0-9]{1,64})-USDT-SWAP$")
BYBIT_PERPETUAL_PATTERN = re.compile(r"^(?P<base>[A-Z0-9]{1,64})USDT$")
HYPERLIQUID_CORE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HYPERLIQUID_HIP3_PATTERN = re.compile(
    r"^(?P<dex>[a-z0-9][a-z0-9_-]{0,31}):(?P<coin>[A-Za-z0-9][A-Za-z0-9._-]{0,63})$"
)
HIP3_DEX_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
INTERNAL_FREQTRADE_HOST_PATTERN = re.compile(
    r"^freqtrade-[a-z0-9](?:[a-z0-9-]{0,51}[a-z0-9])?$"
)


def parse_hip3_dexes(value: str) -> tuple[str, ...]:
    """Parse an explicit, rate-limit-conscious HIP-3 allowlist."""

    values = tuple(item.strip().lower() for item in value.split(",") if item.strip())
    if len(values) != len(set(values)) or any(
        not HIP3_DEX_PATTERN.fullmatch(item) for item in values
    ):
        raise ValueError("Freqtrade HIP-3 DEX names must be unique lowercase identifiers")
    return values


def freqtrade_pair(venue: str, symbol: str, *, hip3_dexes: tuple[str, ...] = ()) -> str:
    """Map the exact Trading Catalog identity to Freqtrade/CCXT pair identity."""

    if venue == "BINANCE":
        match = BINANCE_PERPETUAL_PATTERN.fullmatch(symbol)
        if match is None or symbol != symbol.upper():
            raise DomainRejected(
                "FREQTRADE_INSTRUMENT_UNSUPPORTED",
                "Binance Freqtrade routing requires an exact USDⓈ-M USDT perpetual symbol",
            )
        return f"{match.group('base')}/USDT:USDT"
    if venue == "OKX":
        match = OKX_PERPETUAL_PATTERN.fullmatch(symbol)
        if match is None:
            raise DomainRejected(
                "FREQTRADE_INSTRUMENT_UNSUPPORTED",
                "OKX Freqtrade routing requires an exact USDT linear SWAP symbol",
            )
        return f"{match.group('base')}/USDT:USDT"
    if venue == "BYBIT":
        match = BYBIT_PERPETUAL_PATTERN.fullmatch(symbol)
        if match is None:
            raise DomainRejected(
                "FREQTRADE_INSTRUMENT_UNSUPPORTED",
                "Bybit Freqtrade routing requires an exact USDT linear perpetual symbol",
            )
        return f"{match.group('base')}/USDT:USDT"
    if venue != "HYPERLIQUID":
        raise DomainRejected(
            "FREQTRADE_VENUE_UNSUPPORTED",
            "Freqtrade execution is restricted to supported TradingOPS venues",
        )
    hip3 = HYPERLIQUID_HIP3_PATTERN.fullmatch(symbol)
    if hip3 is not None:
        dex = hip3.group("dex")
        if dex not in hip3_dexes:
            raise DomainRejected(
                "FREQTRADE_HIP3_DEX_NOT_ALLOWED",
                "the HIP-3 DEX is not in the configured Freqtrade worker allowlist",
            )
        return f"{dex.upper()}-{hip3.group('coin')}/USDC:USDC"
    if not HYPERLIQUID_CORE_PATTERN.fullmatch(symbol):
        raise DomainRejected(
            "FREQTRADE_INSTRUMENT_UNSUPPORTED",
            "Hyperliquid Freqtrade routing requires a Core or configured HIP-3 symbol",
        )
    return f"{symbol}/USDC:USDC"


def validate_worker_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Freqtrade worker URL must not embed credentials, query, or fragment")
    http_host_allowed = parsed.hostname in {"127.0.0.1", "localhost"} or bool(
        parsed.hostname and INTERNAL_FREQTRADE_HOST_PATTERN.fullmatch(parsed.hostname)
    )
    if parsed.scheme == "http" and not http_host_allowed:
        raise ValueError("non-loopback/internal Freqtrade workers require HTTPS")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
        raise ValueError("Freqtrade worker URL must include an explicit HTTP(S) host and port")
    return value.rstrip("/")


@dataclass(frozen=True, slots=True)
class FreqtradeEntryCommand:
    pair: str
    side: Literal["long", "short"]
    stake_amount: Decimal
    max_quantity: Decimal
    leverage: Decimal
    enter_tag: str
    client_order_id: str
    position_adjustment: bool = False


@dataclass(frozen=True, slots=True)
class FreqtradeExitCommand:
    pair: str
    max_quantity: Decimal
    client_order_id: str
    close_all: bool = False


@dataclass(frozen=True, slots=True)
class FreqtradeOrder:
    order_id: str
    side: str
    amount: Decimal
    filled: Decimal
    price: Decimal
    is_open: bool
    status: str
    tag: str | None
    filled_at: datetime | None


@dataclass(frozen=True, slots=True)
class FreqtradeTrade:
    trade_id: str
    pair: str
    side: Literal["long", "short"]
    amount: Decimal
    stake_amount: Decimal
    open_rate: Decimal
    current_rate: Decimal
    close_rate: Decimal | None
    is_open: bool
    enter_tag: str
    leverage: Decimal
    stop_loss_abs: Decimal | None
    stoploss_order_id: str | None
    entry_order_id: str | None
    exit_order_id: str | None
    observed_at: datetime
    orders: tuple[FreqtradeOrder, ...] = ()


@dataclass(frozen=True, slots=True)
class FreqtradeRpcMessage:
    event_type: str
    payload: JsonObject
    observed_at: datetime

    @property
    def idempotency_key(self) -> str:
        encoded = json.dumps(
            {"event_type": self.event_type, "payload": self.payload},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        import hashlib

        return hashlib.sha256(encoded).hexdigest()


def parse_freqtrade_rpc_message(raw: str | bytes) -> FreqtradeRpcMessage:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DomainRejected(
            "FREQTRADE_RPC_MESSAGE_INVALID",
            "Freqtrade RPC WebSocket returned invalid JSON",
        ) from exc
    if not isinstance(value, dict):
        raise DomainRejected(
            "FREQTRADE_RPC_MESSAGE_INVALID",
            "Freqtrade RPC WebSocket message must be an object",
        )
    event_type = value.get("type")
    if not isinstance(event_type, str) or not event_type or len(event_type) > 120:
        raise DomainRejected(
            "FREQTRADE_RPC_MESSAGE_INVALID",
            "Freqtrade RPC WebSocket message type is invalid",
        )
    payload = value.get("data")
    if payload is None:
        payload = {key: item for key, item in value.items() if key != "type"}
    if not isinstance(payload, dict):
        raise DomainRejected(
            "FREQTRADE_RPC_MESSAGE_INVALID",
            "Freqtrade RPC WebSocket message data is invalid",
        )
    return FreqtradeRpcMessage(
        event_type=event_type,
        payload=payload,
        observed_at=datetime.now(UTC),
    )


def _decimal(raw: Any, field_name: str, *, positive: bool = False) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DomainRejected(
            "FREQTRADE_WORKER_RESPONSE_INVALID",
            f"Freqtrade field {field_name} is not numeric",
        ) from exc
    if positive and value <= 0:
        raise DomainRejected(
            "FREQTRADE_WORKER_RESPONSE_INVALID",
            f"Freqtrade field {field_name} must be positive",
        )
    return value


def _trade_timestamp(value: JsonObject) -> datetime:
    raw = value.get("close_timestamp") or value.get("open_timestamp")
    if raw is None:
        return datetime.now(UTC)
    try:
        return datetime.fromtimestamp(int(raw) / 1000, UTC)
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        raise DomainRejected(
            "FREQTRADE_WORKER_RESPONSE_INVALID",
            "Freqtrade trade timestamp is invalid",
        ) from exc


def _order_timestamp(raw: Any, field_name: str) -> datetime | None:
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(int(raw) / 1000, UTC)
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        raise DomainRejected(
            "FREQTRADE_WORKER_RESPONSE_INVALID",
            f"Freqtrade field {field_name} is invalid",
        ) from exc


def _orders(value: JsonObject) -> tuple[FreqtradeOrder, ...]:
    raw_orders = value.get("orders")
    if raw_orders is None:
        return ()
    if not isinstance(raw_orders, list) or any(not isinstance(item, dict) for item in raw_orders):
        raise DomainRejected(
            "FREQTRADE_WORKER_RESPONSE_INVALID",
            "Freqtrade trade orders are invalid",
        )
    parsed: list[FreqtradeOrder] = []
    for item in raw_orders:
        order_id = item.get("order_id")
        side = item.get("ft_order_side")
        status = item.get("status")
        if order_id is None or not isinstance(side, str) or not isinstance(status, str):
            raise DomainRejected(
                "FREQTRADE_WORKER_RESPONSE_INVALID",
                "Freqtrade order identity is incomplete",
            )
        amount = _decimal(item.get("amount"), "orders.amount", positive=True)
        filled = _decimal(item.get("filled", 0), "orders.filled")
        price = _decimal(
            item.get("average") or item.get("safe_price") or item.get("price") or 0,
            "orders.price",
        )
        if filled < 0 or filled > amount or price < 0:
            raise DomainRejected(
                "FREQTRADE_WORKER_RESPONSE_INVALID",
                "Freqtrade order amount, fill, or price is outside its valid boundary",
            )
        tag = item.get("ft_order_tag")
        if tag is not None and not isinstance(tag, str):
            raise DomainRejected(
                "FREQTRADE_WORKER_RESPONSE_INVALID",
                "Freqtrade order tag is invalid",
            )
        parsed.append(
            FreqtradeOrder(
                order_id=str(order_id),
                side=side,
                amount=amount,
                filled=filled,
                price=price,
                is_open=item.get("is_open") is True,
                status=status,
                tag=tag,
                filled_at=_order_timestamp(
                    item.get("order_filled_timestamp"),
                    "orders.order_filled_timestamp",
                ),
            )
        )
    return tuple(parsed)


def _stoploss_order_id(value: JsonObject, orders: tuple[FreqtradeOrder, ...]) -> str | None:
    direct = value.get("stoploss_order_id")
    if direct is not None:
        return str(direct)
    active = [
        item
        for item in orders
        if item.side == "stoploss" and (item.is_open or item.status == "open")
    ]
    if len(active) > 1:
        raise DomainRejected(
            "FREQTRADE_WORKER_RESPONSE_INVALID",
            "Freqtrade trade exposes multiple active stop-loss orders",
        )
    if not active:
        return None
    return active[0].order_id


def _execution_order_id(
    orders: tuple[FreqtradeOrder, ...],
    *,
    is_short: bool,
    phase: Literal["entry", "exit"],
) -> str | None:
    entry_side = "sell" if is_short else "buy"
    expected_side = entry_side if phase == "entry" else ("buy" if is_short else "sell")
    matches = [
        item
        for item in orders
        if item.side == expected_side
        and not item.is_open
        and item.status in {"closed", "filled"}
        and item.filled > 0
    ]
    return None if not matches else matches[-1].order_id


def freqtrade_execution_order(
    trade: FreqtradeTrade,
    command: FreqtradeEntryCommand | FreqtradeExitCommand,
    *,
    dispatch_started_at: datetime,
) -> FreqtradeOrder:
    """Select the one completed order owned by a durable dispatch."""

    expected_side = (
        ("sell" if command.side == "short" else "buy")
        if isinstance(command, FreqtradeEntryCommand)
        else ("buy" if trade.side == "short" else "sell")
    )
    if isinstance(command, FreqtradeEntryCommand):
        matches = [
            item
            for item in trade.orders
            if item.side == expected_side
            and item.tag == command.enter_tag
            and not item.is_open
            and item.status in {"closed", "filled"}
            and item.filled > 0
        ]
    else:
        matches = [
            item
            for item in trade.orders
            if item.side == expected_side
            and not item.is_open
            and item.status in {"closed", "filled"}
            and item.filled > 0
            and item.filled_at is not None
            and item.filled_at >= dispatch_started_at
        ]
    if len(matches) != 1:
        raise DomainRejected(
            "FREQTRADE_EXECUTION_ORDER_AMBIGUOUS",
            "Freqtrade did not expose one completed order owned by the durable dispatch",
        )
    order = matches[0]
    if order.filled > command.max_quantity or order.price <= 0:
        raise DomainRejected(
            "FREQTRADE_ORDER_IDENTITY_CONFLICT",
            "Freqtrade execution exceeded the frozen quantity or has no confirmed price",
        )
    return order


def freqtrade_active_stop_order(trade: FreqtradeTrade) -> FreqtradeOrder:
    matches = [
        item
        for item in trade.orders
        if item.side == "stoploss" and (item.is_open or item.status == "open")
    ]
    if (
        len(matches) != 1
        or trade.stoploss_order_id != matches[0].order_id
        or matches[0].amount < trade.amount
    ):
        raise DomainRejected(
            "FREQTRADE_PROTECTION_UNCONFIRMED",
            "Freqtrade did not expose one active stop covering the current trade amount",
        )
    return matches[0]


def parse_freqtrade_trade(value: JsonObject) -> FreqtradeTrade:
    trade_id = value.get("trade_id") or value.get("tradeid") or value.get("id")
    pair = value.get("pair")
    enter_tag = value.get("enter_tag") or value.get("buy_tag")
    if trade_id is None or not isinstance(pair, str) or not isinstance(enter_tag, str):
        raise DomainRejected(
            "FREQTRADE_WORKER_RESPONSE_INVALID",
            "Freqtrade trade identity is incomplete",
        )
    is_short = value.get("is_short")
    if not isinstance(is_short, bool):
        direction = value.get("trade_direction") or value.get("direction")
        if direction not in {"long", "short"}:
            raise DomainRejected(
                "FREQTRADE_WORKER_RESPONSE_INVALID",
                "Freqtrade trade direction is invalid",
            )
        is_short = direction == "short"
    close_rate_raw = value.get("close_rate")
    stop_loss_raw = value.get("stop_loss_abs")
    orders = _orders(value)
    return FreqtradeTrade(
        trade_id=str(trade_id),
        pair=pair,
        side="short" if is_short else "long",
        amount=_decimal(value.get("amount"), "amount", positive=True),
        stake_amount=_decimal(value.get("stake_amount"), "stake_amount", positive=True),
        open_rate=_decimal(value.get("open_rate"), "open_rate", positive=True),
        current_rate=_decimal(
            value.get("current_rate", value.get("open_rate")),
            "current_rate",
            positive=True,
        ),
        close_rate=(
            None
            if close_rate_raw in {None, 0, "0", "0.0"}
            else _decimal(close_rate_raw, "close_rate", positive=True)
        ),
        is_open=bool(value.get("is_open")),
        enter_tag=enter_tag,
        leverage=_decimal(value.get("leverage", 1), "leverage", positive=True),
        stop_loss_abs=(
            None
            if stop_loss_raw in {None, 0, "0", "0.0"}
            else _decimal(stop_loss_raw, "stop_loss_abs", positive=True)
        ),
        stoploss_order_id=_stoploss_order_id(value, orders),
        entry_order_id=_execution_order_id(orders, is_short=is_short, phase="entry"),
        exit_order_id=_execution_order_id(orders, is_short=is_short, phase="exit"),
        observed_at=_trade_timestamp(value),
        orders=orders,
    )
