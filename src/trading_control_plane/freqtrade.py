from __future__ import annotations

import base64
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from trading_control_plane.domain import DomainRejected

JsonObject = dict[str, Any]
JsonValue = JsonObject | list[Any]
JsonFetcher = Callable[[str, str, JsonObject | None, dict[str, str], float], JsonValue]
WebSocketConnector = Callable[[str, float], Any]

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
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("non-loopback Freqtrade workers require HTTPS")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
        raise ValueError("Freqtrade worker URL must include an explicit HTTP(S) host and port")
    return value.rstrip("/")


def _default_fetcher(
    url: str,
    method: str,
    payload: JsonObject | None,
    headers: dict[str, str],
    timeout: float,
) -> JsonValue:
    request = urllib.request.Request(  # noqa: S310
        url,
        data=(None if payload is None else json.dumps(payload, separators=(",", ":")).encode()),
        headers={"Accept": "application/json", "Content-Type": "application/json", **headers},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise DomainRejected(
            "FREQTRADE_WORKER_REJECTED",
            f"Freqtrade worker rejected the request with HTTP {exc.code}",
        ) from exc
    except (
        urllib.error.URLError,
        http.client.RemoteDisconnected,
        ConnectionResetError,
        TimeoutError,
    ) as exc:
        raise DomainRejected(
            "FREQTRADE_WORKER_UNAVAILABLE",
            "Freqtrade worker could not be reached within the bounded timeout",
        ) from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DomainRejected(
            "FREQTRADE_WORKER_RESPONSE_INVALID",
            "Freqtrade worker returned invalid JSON",
        ) from exc
    if not isinstance(value, (dict, list)):
        raise DomainRejected(
            "FREQTRADE_WORKER_RESPONSE_INVALID",
            "Freqtrade worker response shape is invalid",
        )
    return value


def _default_websocket_connector(url: str, timeout: float) -> Any:
    from websockets.asyncio.client import connect

    return connect(
        url,
        open_timeout=timeout,
        close_timeout=timeout,
        max_size=1_000_000,
    )


@dataclass(frozen=True, slots=True)
class FreqtradeWorkerSpec:
    name: str
    venue: Literal["BINANCE", "HYPERLIQUID", "OKX", "BYBIT"]
    base_url: str
    username: str | None
    password: str | None = field(repr=False)
    ws_token: str | None = field(default=None, repr=False)
    hip3_dexes: tuple[str, ...] = ()
    exchange_account_id: str | None = None
    team_id: str | None = None
    account_id: str | None = None

    @property
    def credentials_configured(self) -> bool:
        return bool(self.username and self.password)

    def matches_scope(self, *, team_id: str, account_id: str, venue: str) -> bool:
        return (
            self.team_id == team_id
            and self.account_id == account_id
            and self.venue == venue
            and self.exchange_account_id is not None
        )


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


class FreqtradeWorkerClient:
    """Bounded, authenticated control client for a venue-scoped Freqtrade worker."""

    def __init__(
        self,
        spec: FreqtradeWorkerSpec,
        *,
        timeout_seconds: float = 5,
        confirmation_timeout_seconds: float = 90,
        fetcher: JsonFetcher = _default_fetcher,
        websocket_connector: WebSocketConnector = _default_websocket_connector,
    ) -> None:
        if timeout_seconds <= 0 or confirmation_timeout_seconds < 10:
            raise ValueError("Freqtrade timeouts are outside their bounded range")
        self.spec = FreqtradeWorkerSpec(
            name=spec.name,
            venue=spec.venue,
            base_url=validate_worker_url(spec.base_url),
            username=spec.username,
            password=spec.password,
            ws_token=spec.ws_token,
            hip3_dexes=spec.hip3_dexes,
            exchange_account_id=spec.exchange_account_id,
            team_id=spec.team_id,
            account_id=spec.account_id,
        )
        self.timeout_seconds = timeout_seconds
        self.confirmation_timeout_seconds = confirmation_timeout_seconds
        self._fetcher = fetcher
        self._websocket_connector = websocket_connector

    def rpc_websocket_url(self) -> str:
        if not self.spec.ws_token:
            raise DomainRejected(
                "FREQTRADE_RPC_AUTH_NOT_CONFIGURED",
                "Freqtrade RPC WebSocket token is not configured",
            )
        parsed = urllib.parse.urlparse(self.spec.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        query = urllib.parse.urlencode({"token": self.spec.ws_token})
        return urllib.parse.urlunparse(
            (scheme, parsed.netloc, "/api/v1/message/ws", "", query, "")
        )

    async def rpc_messages(
        self,
        event_types: tuple[str, ...] = FREQTRADE_TRANSACTION_RPC_TYPES,
    ) -> AsyncIterator[FreqtradeRpcMessage]:
        """Yield official Freqtrade RPC messages; account facts still come from CCXT Pro."""

        if (
            not event_types
            or len(event_types) != len(set(event_types))
            or any(not item or len(item) > 120 for item in event_types)
        ):
            raise ValueError("Freqtrade RPC subscriptions must be non-empty unique event types")
        url = self.rpc_websocket_url()
        try:
            async with self._websocket_connector(url, self.timeout_seconds) as websocket:
                await websocket.send(
                    json.dumps(
                        {"type": "subscribe", "data": list(event_types)},
                        separators=(",", ":"),
                    )
                )
                async for raw in websocket:
                    if not isinstance(raw, (str, bytes)):
                        raise DomainRejected(
                            "FREQTRADE_RPC_MESSAGE_INVALID",
                            "Freqtrade RPC WebSocket returned an invalid frame",
                        )
                    yield parse_freqtrade_rpc_message(raw)
        except DomainRejected:
            raise
        except Exception as exc:
            raise DomainRejected(
                "FREQTRADE_RPC_UNAVAILABLE",
                "Freqtrade RPC WebSocket could not be consumed within its bounded connection",
            ) from exc

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: JsonObject | None = None,
        headers: dict[str, str] | None = None,
    ) -> JsonValue:
        return self._fetcher(
            f"{self.spec.base_url}/api/v1/{path.lstrip('/')}",
            method,
            payload,
            headers or {},
            self.timeout_seconds,
        )

    def _access_token(self) -> str:
        if not self.spec.credentials_configured:
            raise DomainRejected(
                "FREQTRADE_WORKER_AUTH_NOT_CONFIGURED",
                "Freqtrade worker control credentials are not configured",
            )
        assert self.spec.username is not None and self.spec.password is not None
        encoded = base64.b64encode(f"{self.spec.username}:{self.spec.password}".encode()).decode()
        result = self._request(
            "token/login",
            method="POST",
            headers={"Authorization": f"Basic {encoded}"},
        )
        if not isinstance(result, dict):
            raise DomainRejected(
                "FREQTRADE_WORKER_AUTH_FAILED",
                "Freqtrade worker token response is invalid",
            )
        token = result.get("access_token")
        if not isinstance(token, str) or not token:
            raise DomainRejected(
                "FREQTRADE_WORKER_AUTH_FAILED",
                "Freqtrade worker did not issue a control token",
            )
        return token

    def _authorized_request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: JsonObject | None = None,
    ) -> JsonValue:
        token = self._access_token()
        return self._request(
            path,
            method=method,
            payload=payload,
            headers={"Authorization": f"Bearer {token}"},
        )

    def probe(
        self,
        *,
        expected_mode: Literal["DRY_RUN", "LIVE"] = "DRY_RUN",
        required_pair: str | None = None,
    ) -> JsonObject:
        """Verify worker identity, mode and exact tradable scope without sending an order."""

        ping = self._request("ping")
        if not isinstance(ping, dict) or ping.get("status") != "pong":
            raise DomainRejected(
                "FREQTRADE_WORKER_RESPONSE_INVALID",
                "Freqtrade worker ping response is invalid",
            )
        token = self._access_token()
        headers = {"Authorization": f"Bearer {token}"}
        config = self._request("show_config", headers=headers)
        version = self._request("version", headers=headers)
        whitelist_response = self._request("whitelist", headers=headers)
        if not all(isinstance(item, dict) for item in (config, version, whitelist_response)):
            raise DomainRejected(
                "FREQTRADE_WORKER_RESPONSE_INVALID",
                "Freqtrade worker configuration response is invalid",
            )
        assert isinstance(config, dict)
        assert isinstance(version, dict)
        assert isinstance(whitelist_response, dict)
        exchange = config.get("exchange")
        expected_exchange = {
            "BINANCE": "binance",
            "HYPERLIQUID": "hyperliquid",
            "OKX": "okx",
            "BYBIT": "bybit",
        }[self.spec.venue]
        if exchange != expected_exchange or config.get("trading_mode") != "futures":
            raise DomainRejected(
                "FREQTRADE_WORKER_SCOPE_MISMATCH",
                "Freqtrade worker exchange or trading mode does not match its bound scope",
            )
        expected_dry_run = expected_mode == "DRY_RUN"
        if config.get("dry_run") is not expected_dry_run:
            raise DomainRejected(
                (
                    "FREQTRADE_LIVE_MODE_REQUIRED"
                    if expected_mode == "LIVE"
                    else "FREQTRADE_LIVE_MODE_FORBIDDEN"
                ),
                "Freqtrade worker mode does not match the requested execution boundary",
            )
        whitelist = whitelist_response.get("whitelist")
        if not isinstance(whitelist, list) or any(not isinstance(pair, str) for pair in whitelist):
            raise DomainRejected(
                "FREQTRADE_WORKER_RESPONSE_INVALID",
                "Freqtrade worker whitelist response is invalid",
            )
        hip3_pairs = [
            pair
            for pair in whitelist
            if any(pair.startswith(f"{dex.upper()}-") for dex in self.spec.hip3_dexes)
        ]
        if self.spec.hip3_dexes and not hip3_pairs:
            raise DomainRejected(
                "FREQTRADE_HIP3_SCOPE_MISMATCH",
                "Freqtrade worker did not expose any pair from its configured HIP-3 DEX scope",
            )
        if required_pair is not None and required_pair not in whitelist:
            raise DomainRejected(
                "FREQTRADE_INSTRUMENT_NOT_ALLOWED",
                "the exact Freqtrade pair is not in the worker whitelist",
            )
        if config.get("force_entry_enable") is not True:
            raise DomainRejected(
                "FREQTRADE_FORCE_ENTRY_DISABLED",
                "Freqtrade worker does not permit controlled force entry",
            )
        if config.get("position_adjustment_enable") is not True:
            raise DomainRejected(
                "FREQTRADE_POSITION_ADJUSTMENT_DISABLED",
                "Freqtrade worker cannot execute approved Add or partial-reduction intents",
            )
        if str(config.get("state", "")).lower() != "running":
            raise DomainRejected(
                "FREQTRADE_WORKER_NOT_RUNNING",
                "Freqtrade worker is not running",
            )
        result: JsonObject = {
            "name": self.spec.name,
            "venue": self.spec.venue,
            "backend": "FREQTRADE",
            "status": "READY",
            "exchange": exchange,
            "trading_mode": "futures",
            "dry_run": expected_dry_run,
            "worker_state": config.get("state"),
            "version": version.get("version"),
            "hip3_dexes": list(self.spec.hip3_dexes),
            "active_pair_count": len(whitelist),
            "hip3_pair_count": len(hip3_pairs),
            "worker_command_available": True,
            "position_adjustment_enabled": True,
            "external_order_send": expected_mode == "LIVE",
        }
        if self.spec.exchange_account_id is not None:
            result["exchange_account_id"] = self.spec.exchange_account_id
            result["team_id"] = self.spec.team_id
            result["account_id"] = self.spec.account_id
        return result

    def _open_trades(self) -> tuple[FreqtradeTrade, ...]:
        value = self._authorized_request("status")
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise DomainRejected(
                "FREQTRADE_WORKER_RESPONSE_INVALID",
                "Freqtrade open-trade response is invalid",
            )
        parsed: list[FreqtradeTrade] = []
        for item in value:
            try:
                amount = Decimal(str(item.get("amount")))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise DomainRejected(
                    "FREQTRADE_WORKER_RESPONSE_INVALID",
                    "Freqtrade pending entry amount is invalid",
                ) from exc
            if amount < 0:
                raise DomainRejected(
                    "FREQTRADE_WORKER_RESPONSE_INVALID",
                    "Freqtrade pending entry amount cannot be negative",
                )
            if amount == 0:
                continue
            parsed.append(parse_freqtrade_trade(item))
        return tuple(parsed)

    def open_trades(self) -> tuple[FreqtradeTrade, ...]:
        """Query current worker trades without issuing an execution command."""

        return self._open_trades()

    def find_open_trade(
        self,
        *,
        pair: str,
        enter_tag: str | None = None,
    ) -> FreqtradeTrade | None:
        matches = tuple(
            item
            for item in self._open_trades()
            if item.pair == pair and (enter_tag is None or item.enter_tag == enter_tag)
        )
        if len(matches) > 1:
            raise DomainRejected(
                "FREQTRADE_SCOPE_AMBIGUOUS",
                "multiple live Freqtrade trades match the controlled scope",
            )
        return matches[0] if matches else None

    def _find_filled_entry(
        self,
        command: FreqtradeEntryCommand,
        *,
        trade_id: str | None,
        dispatch_started_at: datetime,
    ) -> FreqtradeTrade | None:
        """Read the order tagged by one intent, including position adjustments."""

        scoped = tuple(item for item in self._open_trades() if item.pair == command.pair)
        if len(scoped) > 1:
            raise DomainRejected(
                "FREQTRADE_SCOPE_AMBIGUOUS",
                "multiple live Freqtrade trades match the controlled entry scope",
            )
        if not scoped:
            return None
        trade = scoped[0]
        if trade_id is not None and trade.trade_id != trade_id:
            raise DomainRejected(
                "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                "Freqtrade position adjustment changed the bound trade identity",
            )
        if trade.side != command.side:
            raise DomainRejected(
                "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                "Freqtrade entry changed the frozen direction",
            )
        tagged = tuple(item for item in trade.orders if item.tag == command.enter_tag)
        if not tagged:
            if not command.position_adjustment and trade.enter_tag != command.enter_tag:
                raise DomainRejected(
                    "FREQTRADE_SCOPE_BUSY",
                    "another Freqtrade trade appeared during controlled entry",
                )
            return None
        freqtrade_execution_order(
            trade,
            command,
            dispatch_started_at=dispatch_started_at,
        )
        return trade

    def wait_for_protection(
        self,
        *,
        trade_id: str,
        pair: str,
    ) -> FreqtradeTrade:
        """Wait a bounded interval for the exchange-native stop to become observable."""

        deadline = time.monotonic() + self.confirmation_timeout_seconds
        while time.monotonic() <= deadline:
            current = self.find_open_trade(pair=pair)
            if current is None or current.trade_id != trade_id:
                raise DomainRejected(
                    "FREQTRADE_LIVE_OUTCOME_UNKNOWN",
                    "Freqtrade trade changed before native protection was confirmed",
                )
            if current.stop_loss_abs is not None and current.stoploss_order_id:
                try:
                    freqtrade_active_stop_order(current)
                except DomainRejected as exc:
                    if exc.code != "FREQTRADE_PROTECTION_UNCONFIRMED":
                        raise
                else:
                    return current
            time.sleep(0.25)
        raise DomainRejected(
            "FREQTRADE_PROTECTION_UNCONFIRMED",
            "Freqtrade did not expose an active exchange-native stop within the bounded timeout",
        )

    def trade(self, trade_id: str) -> FreqtradeTrade:
        value = self._authorized_request(f"trade/{urllib.parse.quote(trade_id, safe='')}")
        if not isinstance(value, dict):
            raise DomainRejected(
                "FREQTRADE_WORKER_RESPONSE_INVALID",
                "Freqtrade trade response is invalid",
            )
        return parse_freqtrade_trade(value)

    def force_enter(
        self,
        command: FreqtradeEntryCommand,
        *,
        expected_mode: Literal["DRY_RUN", "LIVE"] = "LIVE",
        trade_id: str | None = None,
        dispatch_started_at: datetime | None = None,
    ) -> FreqtradeTrade:
        if command.stake_amount <= 0 or command.max_quantity <= 0 or command.leverage <= 0:
            raise DomainRejected(
                "FREQTRADE_ORDER_INVALID",
                "Freqtrade entry values must be positive",
            )
        started_at = dispatch_started_at or datetime.now(UTC)
        self.probe(expected_mode=expected_mode, required_pair=command.pair)
        existing = self.find_open_trade(pair=command.pair)
        if command.position_adjustment:
            if existing is None or trade_id is None or existing.trade_id != trade_id:
                raise DomainRejected(
                    "FREQTRADE_POSITION_NOT_FOUND",
                    "approved Add requires the exact existing Freqtrade trade",
                )
            if existing.side != command.side:
                raise DomainRejected(
                    "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                    "approved Add direction differs from the existing Freqtrade trade",
                )
        elif trade_id is not None:
            raise DomainRejected(
                "FREQTRADE_DISPATCH_IDENTITY_INVALID",
                "initial entry must not bind an existing Freqtrade trade",
            )
        if existing is not None:
            recovered = self._find_filled_entry(
                command,
                trade_id=trade_id,
                dispatch_started_at=started_at,
            )
            if recovered is not None:
                return self.wait_for_protection(
                    trade_id=recovered.trade_id,
                    pair=command.pair,
                )
            if not command.position_adjustment:
                raise DomainRejected(
                    "FREQTRADE_SCOPE_BUSY",
                    "another Freqtrade trade is already open for this pair",
                )
        try:
            self._authorized_request(
                "forceenter",
                method="POST",
                payload={
                    "pair": command.pair,
                    "side": command.side,
                    "ordertype": "market",
                    "stakeamount": float(command.stake_amount),
                    "leverage": float(command.leverage),
                    "entry_tag": command.enter_tag,
                },
            )
        except DomainRejected as exc:
            if exc.code != "FREQTRADE_WORKER_UNAVAILABLE":
                raise
        return self.recover_entry(
            command,
            trade_id=trade_id,
            dispatch_started_at=started_at,
        )

    def recover_entry(
        self,
        command: FreqtradeEntryCommand,
        *,
        trade_id: str | None = None,
        dispatch_started_at: datetime | None = None,
    ) -> FreqtradeTrade:
        """Query a previously dispatched entry without issuing another write."""

        started_at = dispatch_started_at or datetime.now(UTC)
        deadline = time.monotonic() + self.confirmation_timeout_seconds
        while time.monotonic() <= deadline:
            found = self._find_filled_entry(
                command,
                trade_id=trade_id,
                dispatch_started_at=started_at,
            )
            if found is not None:
                return self.wait_for_protection(trade_id=found.trade_id, pair=command.pair)
            time.sleep(0.25)
        raise DomainRejected(
            "FREQTRADE_LIVE_OUTCOME_UNKNOWN",
            "Freqtrade entry outcome could not be confirmed by query within the bounded timeout",
        )

    def force_exit(
        self,
        trade_id: str,
        command: FreqtradeExitCommand,
        *,
        dispatch_started_at: datetime,
    ) -> FreqtradeTrade:
        current = self.find_open_trade(pair=command.pair)
        if current is None or current.trade_id != trade_id:
            return self.recover_exit(
                trade_id,
                command,
                dispatch_started_at=dispatch_started_at,
            )
        if command.close_all:
            if current.amount > command.max_quantity:
                raise DomainRejected(
                    "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                    "Freqtrade open amount exceeds the frozen full-exit boundary",
                )
        elif command.max_quantity >= current.amount:
            raise DomainRejected(
                "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                "partial reduction must remain below the exact open Freqtrade amount",
            )
        payload: JsonObject = {"tradeid": trade_id, "ordertype": "market"}
        if not command.close_all:
            payload["amount"] = float(command.max_quantity)
        try:
            self._authorized_request(
                "forceexit",
                method="POST",
                payload=payload,
            )
        except DomainRejected as exc:
            if exc.code != "FREQTRADE_WORKER_UNAVAILABLE":
                raise
        return self.recover_exit(
            trade_id,
            command,
            dispatch_started_at=dispatch_started_at,
        )

    def recover_exit(
        self,
        trade_id: str,
        command: FreqtradeExitCommand,
        *,
        dispatch_started_at: datetime,
    ) -> FreqtradeTrade:
        """Query a previously dispatched exit without issuing another write."""

        deadline = time.monotonic() + self.confirmation_timeout_seconds
        while time.monotonic() <= deadline:
            recovered = self.trade(trade_id)
            if recovered.pair != command.pair:
                raise DomainRejected(
                    "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                    "Freqtrade exit result changed the controlled scope",
                )
            candidates = [
                item
                for item in recovered.orders
                if item.side == ("buy" if recovered.side == "short" else "sell")
                and not item.is_open
                and item.status in {"closed", "filled"}
                and item.filled > 0
                and item.filled_at is not None
                and item.filled_at >= dispatch_started_at
            ]
            if len(candidates) > 1:
                raise DomainRejected(
                    "FREQTRADE_EXECUTION_ORDER_AMBIGUOUS",
                    "multiple Freqtrade exits appeared inside one durable dispatch window",
                )
            if candidates:
                freqtrade_execution_order(
                    recovered,
                    command,
                    dispatch_started_at=dispatch_started_at,
                )
                if command.close_all and recovered.is_open:
                    raise DomainRejected(
                        "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                        "full Freqtrade exit left the bound trade open",
                    )
                if not command.close_all and not recovered.is_open:
                    raise DomainRejected(
                        "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                        "partial Freqtrade reduction unexpectedly closed the trade",
                    )
                return recovered
            time.sleep(0.25)
        raise DomainRejected(
            "FREQTRADE_LIVE_OUTCOME_UNKNOWN",
            "Freqtrade exit outcome could not be confirmed by query within the bounded timeout",
        )
