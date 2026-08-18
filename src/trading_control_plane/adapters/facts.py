from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from trading_control_plane.domain import DomainRejected
from trading_control_plane.service_core import ConnectionProbeResult

logger = logging.getLogger(__name__)

Venue = Literal["BINANCE", "HYPERLIQUID", "OKX", "BYBIT"]
Environment = Literal["TESTNET", "LIVE"]
DataStatus = Literal["CURRENT", "STALE", "UNKNOWN"]
EventKind = Literal["BALANCE", "POSITION", "ORDER", "FILL", "MARK", "STATUS", "SNAPSHOT"]
SnapshotReason = Literal[
    "INITIAL",
    "RECONNECT_COMPENSATION",
    "SEQUENCE_GAP_COMPENSATION",
    "PERIODIC_RECONCILIATION",
    "LIMITED_REST_FALLBACK",
    "WEBSOCKET_INCREMENT",
]

JsonObject = dict[str, Any]

_REQUIRED_REST_CAPABILITIES = (
    "fetchBalance",
    "fetchPositions",
    "fetchOpenOrders",
)
_OPTIONAL_REST_CAPABILITIES = (
    "fetchMyTrades",
    "fetchFundingHistory",
    "fetchFundingRates",
    "fetchStatus",
)
_WATCH_CAPABILITIES: dict[str, str] = {
    "BALANCE": "watchBalance",
    "POSITION": "watchPositions",
    "ORDER": "watchOrders",
    "FILL": "watchMyTrades",
    "MARK": "watchTickers",
}
_MAX_EVENT_FINGERPRINTS = 10_000


def _utc(value: datetime | None = None) -> datetime:
    resolved = datetime.now(UTC) if value is None else value
    if resolved.utcoffset() is None:
        raise ValueError("fact adapter timestamps must include a timezone")
    return resolved.astimezone(UTC)


def _decimal(value: object, *, default: Decimal | None = None) -> Decimal | None:
    if value is None or value == "":
        return default
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return result if result.is_finite() else default


def _decimal_text(value: object, *, default: str | None = None) -> str | None:
    parsed = _decimal(value)
    if parsed is None:
        return default
    return format(parsed, "f")


def _timestamp(value: object, *, fallback: datetime) -> str:
    if value is None:
        return fallback.isoformat()
    try:
        return datetime.fromtimestamp(int(str(value)) / 1_000, UTC).isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        return fallback.isoformat()


def _fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class FactAdapterScope:
    workspace_id: str
    team_id: str
    account_id: str
    venue: Venue
    environment: Environment
    symbols: tuple[str, ...]
    account_mode: str = "STANDARD"

    def __post_init__(self) -> None:
        for name, value in (
            ("workspace_id", self.workspace_id),
            ("team_id", self.team_id),
            ("account_id", self.account_id),
        ):
            if not value or value != value.strip() or len(value) > 160 or ":" in value:
                raise ValueError(f"fact adapter {name} is invalid")
        if not self.symbols or len(self.symbols) != len(set(self.symbols)):
            raise ValueError("fact adapter symbols must be a non-empty unique tuple")
        allowed_modes = (
            {"STANDARD", "PORTFOLIO_MARGIN"} if self.venue == "BINANCE" else {"STANDARD"}
        )
        if self.account_mode not in allowed_modes:
            raise DomainRejected(
                "FACT_ADAPTER_ACCOUNT_MODE_UNSUPPORTED",
                "the account mode is not supported for the selected exchange",
            )

    @property
    def key(self) -> str:
        return ":".join(
            (self.workspace_id, self.team_id, self.environment, self.account_id, self.venue)
        )

    def to_dict(self) -> JsonObject:
        return {
            "workspace_id": self.workspace_id,
            "team_id": self.team_id,
            "account_id": self.account_id,
            "venue": self.venue,
            "environment": self.environment,
            "symbols": list(self.symbols),
            "account_mode": self.account_mode,
        }


ExchangeFactory = Callable[[FactAdapterScope, Mapping[str, str], Mapping[str, Any]], Any]


@dataclass(frozen=True, slots=True)
class FactAdapterMetrics:
    rest_requests: Mapping[str, int]
    websocket_subscriptions: int
    websocket_reconnects: int
    rest_compensations: int
    periodic_reconciliations: int
    limited_rest_fallbacks: int
    duplicate_events: int
    out_of_order_events: int
    sequence_gaps: int

    def to_dict(self) -> JsonObject:
        return {
            "rest_requests": dict(sorted(self.rest_requests.items())),
            "websocket_subscriptions": self.websocket_subscriptions,
            "websocket_reconnects": self.websocket_reconnects,
            "rest_compensations": self.rest_compensations,
            "periodic_reconciliations": self.periodic_reconciliations,
            "limited_rest_fallbacks": self.limited_rest_fallbacks,
            "duplicate_events": self.duplicate_events,
            "out_of_order_events": self.out_of_order_events,
            "sequence_gaps": self.sequence_gaps,
        }


@dataclass(frozen=True, slots=True)
class ExchangeFactSnapshot:
    scope: FactAdapterScope
    snapshot_version: int
    observed_at: datetime
    data_status: DataStatus
    reason: SnapshotReason
    positions: tuple[JsonObject, ...]
    orders: tuple[JsonObject, ...]
    fills: tuple[JsonObject, ...]
    balances: tuple[JsonObject, ...]
    instruments: tuple[JsonObject, ...]
    marks: tuple[JsonObject, ...]
    funding: tuple[JsonObject, ...]
    account_status: JsonObject | None
    unknown_fields: tuple[str, ...]
    metrics: FactAdapterMetrics

    def to_dict(self) -> JsonObject:
        return {
            "scope": self.scope.to_dict(),
            "source": "CCXT_PRO",
            "snapshot_version": self.snapshot_version,
            "observed_at": self.observed_at.isoformat(),
            "data_status": self.data_status,
            "reason": self.reason,
            "positions": list(self.positions),
            "orders": list(self.orders),
            "fills": list(self.fills),
            "balances": list(self.balances),
            "instruments": list(self.instruments),
            "marks": list(self.marks),
            "funding": list(self.funding),
            "account_status": self.account_status,
            "unknown_fields": list(self.unknown_fields),
            "metrics": self.metrics.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ExchangeFactEvent:
    scope: FactAdapterScope
    stream_id: str
    sequence: int
    kind: EventKind
    observed_at: datetime
    payload: JsonObject
    snapshot_version: int

    @property
    def fingerprint(self) -> str:
        return _fingerprint(
            {
                "scope": self.scope.key,
                "kind": self.kind,
                "payload": self.payload,
            }
        )

    def to_dict(self) -> JsonObject:
        return {
            "scope": self.scope.to_dict(),
            "stream_id": self.stream_id,
            "sequence": self.sequence,
            "kind": self.kind,
            "observed_at": self.observed_at.isoformat(),
            "payload": self.payload,
            "snapshot_version": self.snapshot_version,
        }


class ExchangeFactAdapter(Protocol):
    scope: FactAdapterScope

    @property
    def watch_channels(self) -> tuple[EventKind, ...]: ...

    async def snapshot(
        self,
        *,
        reason: SnapshotReason,
        observed_at: datetime | None = None,
    ) -> ExchangeFactSnapshot: ...

    async def watch(self, kind: EventKind) -> JsonObject: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class _MutableMetrics:
    rest_requests: dict[str, int] = field(default_factory=dict)
    websocket_subscriptions: int = 0
    websocket_reconnects: int = 0
    rest_compensations: int = 0
    periodic_reconciliations: int = 0
    limited_rest_fallbacks: int = 0
    duplicate_events: int = 0
    out_of_order_events: int = 0
    sequence_gaps: int = 0

    def freeze(self) -> FactAdapterMetrics:
        return FactAdapterMetrics(
            rest_requests=dict(self.rest_requests),
            websocket_subscriptions=self.websocket_subscriptions,
            websocket_reconnects=self.websocket_reconnects,
            rest_compensations=self.rest_compensations,
            periodic_reconciliations=self.periodic_reconciliations,
            limited_rest_fallbacks=self.limited_rest_fallbacks,
            duplicate_events=self.duplicate_events,
            out_of_order_events=self.out_of_order_events,
            sequence_gaps=self.sequence_gaps,
        )


def _default_exchange_factory(
    scope: FactAdapterScope,
    credentials: Mapping[str, str],
    options: Mapping[str, Any],
) -> Any:
    # This is the only production import of CCXT Pro.  Domain, API, page and
    # risk modules never import or depend on an exchange implementation.
    import ccxt.pro as ccxtpro  # type: ignore[import-untyped]

    configuration: dict[str, Any] = {
        "enableRateLimit": True,
        "newUpdates": True,
        "options": dict(options),
    }
    if scope.venue in {"BINANCE", "OKX", "BYBIT"}:
        configuration["apiKey"] = credentials.get("api_key", "")
        configuration["secret"] = credentials.get("api_secret", "")
    if scope.venue == "OKX":
        configuration["password"] = credentials.get("passphrase", "")
    if scope.venue == "HYPERLIQUID":
        configuration["walletAddress"] = credentials.get("account_address", "")
        # Read-only fact adapters intentionally do not receive an API-wallet
        # private key.  Hyperliquid account facts are public by account address.
    constructors: dict[Venue, Callable[[dict[str, Any]], Any]] = {
        "BINANCE": ccxtpro.binanceusdm,
        "HYPERLIQUID": ccxtpro.hyperliquid,
        "OKX": ccxtpro.okx,
        "BYBIT": ccxtpro.bybit,
    }
    exchange = constructors[scope.venue](configuration)
    if scope.environment == "TESTNET":
        exchange.set_sandbox_mode(True)
    return exchange


class CcxtProFactAdapter:
    """One exact account-scoped CCXT/CCXT Pro fact connection.

    The adapter exposes no order-creation, cancellation, transfer or withdrawal
    methods.  Its credential envelope is never included in errors, reprs or
    serialized facts.
    """

    def __init__(
        self,
        scope: FactAdapterScope,
        *,
        credentials: Mapping[str, str],
        exchange_factory: ExchangeFactory = _default_exchange_factory,
        history_window: timedelta = timedelta(hours=24),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if history_window < timedelta(minutes=5) or history_window > timedelta(days=7):
            raise ValueError("fact adapter history window is outside its bounded range")
        normalized = {key: value for key, value in credentials.items() if value}
        required = {
            "BINANCE": {"api_key", "api_secret"},
            "HYPERLIQUID": {"account_address"},
            "OKX": {"api_key", "api_secret", "passphrase"},
            "BYBIT": {"api_key", "api_secret"},
        }[scope.venue]
        if not required.issubset(normalized):
            raise DomainRejected(
                "FACT_ADAPTER_CREDENTIALS_NOT_CONFIGURED",
                "the fact adapter credential scope is incomplete",
            )
        if "api_wallet_private_key" in normalized:
            raise DomainRejected(
                "FACT_ADAPTER_CREDENTIAL_SCOPE_INVALID",
                "read-only fact adapters must not receive trading signing material",
            )
        options: dict[str, Any] = {"defaultType": "swap"}
        if scope.venue == "BINANCE" and scope.account_mode == "PORTFOLIO_MARGIN":
            options.update({"papi": True, "portfolioMargin": True})
        if scope.venue == "HYPERLIQUID":
            hip3_dexes = sorted(
                {
                    symbol.split("-", 1)[0].lower()
                    for symbol in scope.symbols
                    if "-" in symbol.split("/", 1)[0]
                }
            )
            options["fetchMarkets"] = {
                "types": ["swap", "hip3"] if hip3_dexes else ["swap"],
                "hip3": {"dexes": hip3_dexes},
            }
        self.scope = scope
        self._exchange = exchange_factory(scope, normalized, options)
        self._history_window = history_window
        self._clock = clock
        self._snapshot_version = 0
        self._metrics = _MutableMetrics()
        self._markets_loaded = False
        self._closed = False
        self._tracked_symbols = set(scope.symbols)

    def __repr__(self) -> str:
        return f"CcxtProFactAdapter(scope={self.scope.key!r})"

    @property
    def metrics(self) -> FactAdapterMetrics:
        return self._metrics.freeze()

    def _capability(self, name: str) -> bool:
        capabilities = getattr(self._exchange, "has", {})
        return isinstance(capabilities, Mapping) and capabilities.get(name) is True

    def validate_capabilities(self) -> None:
        missing = [name for name in _REQUIRED_REST_CAPABILITIES if not self._capability(name)]
        if missing:
            raise DomainRejected(
                "FACT_ADAPTER_CAPABILITY_UNSUPPORTED",
                "the selected exchange lacks required fact capabilities",
                metadata={"missing": missing},
            )

    def validate_exchange_mapping(self) -> None:
        expected = {
            "BINANCE": "binanceusdm",
            "HYPERLIQUID": "hyperliquid",
            "OKX": "okx",
            "BYBIT": "bybit",
        }[self.scope.venue]
        exchange_id = getattr(self._exchange, "id", None)
        if exchange_id != expected:
            raise DomainRejected(
                "FACT_ADAPTER_EXCHANGE_MAPPING_INVALID",
                "the CCXT exchange does not match the exact account venue",
            )

    @property
    def watch_channels(self) -> tuple[EventKind, ...]:
        return tuple(
            cast(EventKind, kind)
            for kind, capability in _WATCH_CAPABILITIES.items()
            if self._capability(capability)
        )

    async def _rest(self, capability: str, *args: object) -> Any:
        if not self._capability(capability):
            raise DomainRejected(
                "FACT_ADAPTER_CAPABILITY_UNSUPPORTED",
                f"the selected exchange does not support {capability}",
            )
        method_name = {
            "fetchBalance": "fetch_balance",
            "fetchPositions": "fetch_positions",
            "fetchOpenOrders": "fetch_open_orders",
            "fetchMyTrades": "fetch_my_trades",
            "fetchFundingHistory": "fetch_funding_history",
            "fetchFundingRates": "fetch_funding_rates",
            "fetchStatus": "fetch_status",
        }[capability]
        method = getattr(self._exchange, method_name, None)
        if method is None:
            raise DomainRejected(
                "FACT_ADAPTER_CAPABILITY_CONTRACT_INVALID",
                f"CCXT advertised {capability} without its callable contract",
            )
        self._metrics.rest_requests[capability] = self._metrics.rest_requests.get(capability, 0) + 1
        return await cast(Callable[..., Awaitable[Any]], method)(*args)

    async def _load_markets(self) -> Mapping[str, Any]:
        if self._markets_loaded:
            markets = getattr(self._exchange, "markets", None)
            if isinstance(markets, Mapping):
                return cast(Mapping[str, Any], markets)
        method = getattr(self._exchange, "load_markets", None)
        if method is None:
            raise DomainRejected(
                "FACT_ADAPTER_CAPABILITY_CONTRACT_INVALID",
                "CCXT exchange has no load_markets contract",
            )
        self._metrics.rest_requests["loadMarkets"] = (
            self._metrics.rest_requests.get("loadMarkets", 0) + 1
        )
        raw = await cast(Callable[[], Awaitable[Any]], method)()
        if not isinstance(raw, Mapping):
            raise DomainRejected(
                "FACT_ADAPTER_RESPONSE_INVALID", "CCXT markets response is invalid"
            )
        self._markets_loaded = True
        return cast(Mapping[str, Any], raw)

    async def _optional_rest(
        self,
        capability: str,
        *args: object,
    ) -> tuple[Any, str | None]:
        if not self._capability(capability):
            return None, capability
        try:
            return await self._rest(capability, *args), None
        except Exception as exc:
            logger.warning(
                "Optional exchange fact capability failed",
                extra={
                    "event": "fact_adapter_optional_read_failed",
                    "component": "fact-adapter",
                    "venue": self.scope.venue,
                    "account_id": self.scope.account_id,
                    "capability": capability,
                    "error_type": type(exc).__name__,
                },
            )
            return None, capability

    @staticmethod
    def _market_id(markets: Mapping[str, Any], symbol: str) -> str:
        market = markets.get(symbol)
        if not isinstance(market, Mapping):
            return symbol
        native = market.get("id")
        return str(native) if native else symbol

    @staticmethod
    def _contract_size(markets: Mapping[str, Any], symbol: str) -> Decimal:
        market = markets.get(symbol)
        if not isinstance(market, Mapping):
            return Decimal(1)
        return _decimal(market.get("contractSize"), default=Decimal(1)) or Decimal(1)

    def _instruments(
        self,
        markets: Mapping[str, Any],
        symbols: Sequence[str],
    ) -> tuple[JsonObject, ...]:
        rows: list[JsonObject] = []
        for symbol in symbols:
            market = markets.get(symbol)
            if not isinstance(market, Mapping):
                raise DomainRejected(
                    "FACT_ADAPTER_INSTRUMENT_UNAVAILABLE",
                    "a configured instrument is absent from the CCXT market catalog",
                    metadata={"symbol": symbol},
                )
            precision = market.get("precision")
            limits = market.get("limits")
            amount_limits = limits.get("amount") if isinstance(limits, Mapping) else None
            cost_limits = limits.get("cost") if isinstance(limits, Mapping) else None
            rows.append(
                {
                    "symbol": symbol,
                    "native_symbol": self._market_id(markets, symbol),
                    "active": market.get("active") is not False,
                    "contract": bool(market.get("contract")),
                    "linear": bool(market.get("linear")),
                    "contract_size": _decimal_text(market.get("contractSize"), default="1"),
                    "amount_precision": (
                        _decimal_text(precision.get("amount"))
                        if isinstance(precision, Mapping)
                        else None
                    ),
                    "price_precision": (
                        _decimal_text(precision.get("price"))
                        if isinstance(precision, Mapping)
                        else None
                    ),
                    "minimum_amount": (
                        _decimal_text(amount_limits.get("min"))
                        if isinstance(amount_limits, Mapping)
                        else None
                    ),
                    "minimum_notional": (
                        _decimal_text(cost_limits.get("min"))
                        if isinstance(cost_limits, Mapping)
                        else None
                    ),
                    "quote": market.get("quote"),
                    "settle": market.get("settle"),
                }
            )
        return tuple(rows)

    def _positions(
        self,
        raw: object,
        markets: Mapping[str, Any],
        observed_at: datetime,
    ) -> tuple[JsonObject, ...]:
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise DomainRejected(
                "FACT_ADAPTER_RESPONSE_INVALID", "CCXT positions response is invalid"
            )
        rows: list[JsonObject] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise DomainRejected(
                    "FACT_ADAPTER_RESPONSE_INVALID", "CCXT position row is invalid"
                )
            symbol = item.get("symbol")
            if not isinstance(symbol, str):
                raise DomainRejected(
                    "FACT_ADAPTER_RESPONSE_INVALID", "CCXT position symbol is invalid"
                )
            contracts = _decimal(item.get("contracts"))
            if contracts is None:
                raise DomainRejected(
                    "FACT_ADAPTER_RESPONSE_INCOMPLETE",
                    "CCXT position quantity is unknown",
                )
            size = contracts * self._contract_size(markets, symbol)
            side = str(item.get("side") or "").lower()
            if side == "short":
                size = -abs(size)
            elif side == "long":
                size = abs(size)
            elif size != 0:
                # A non-zero position without direction is unknown, never assumed long.
                raise DomainRejected(
                    "FACT_ADAPTER_RESPONSE_INCOMPLETE",
                    "CCXT position direction is missing",
                )
            rows.append(
                {
                    "symbol": symbol,
                    "native_symbol": self._market_id(markets, symbol),
                    "side": side or None,
                    "quantity": format(size, "f"),
                    "entry_price": _decimal_text(item.get("entryPrice"), default="0"),
                    "mark_price": _decimal_text(item.get("markPrice")),
                    "unrealized_pnl": _decimal_text(item.get("unrealizedPnl")),
                    "liquidation_price": _decimal_text(item.get("liquidationPrice")),
                    "margin_mode": item.get("marginMode"),
                    "hedged": item.get("hedged"),
                    "observed_at": _timestamp(item.get("timestamp"), fallback=observed_at),
                }
            )
        return tuple(sorted(rows, key=lambda item: (str(item["symbol"]), str(item["side"]))))

    def _orders(
        self,
        raw: object,
        markets: Mapping[str, Any],
        observed_at: datetime,
    ) -> tuple[JsonObject, ...]:
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise DomainRejected(
                "FACT_ADAPTER_RESPONSE_INVALID", "CCXT open-orders response is invalid"
            )
        rows: list[JsonObject] = []
        for item in raw:
            if not isinstance(item, Mapping) or item.get("id") is None:
                raise DomainRejected(
                    "FACT_ADAPTER_RESPONSE_INVALID", "CCXT open-order row is invalid"
                )
            symbol = str(item.get("symbol") or "")
            amount = _decimal(item.get("amount"))
            filled = _decimal(item.get("filled"))
            if not symbol or amount is None or filled is None:
                raise DomainRejected(
                    "FACT_ADAPTER_RESPONSE_INCOMPLETE",
                    "CCXT open-order quantity is unknown",
                )
            size = self._contract_size(markets, symbol)
            rows.append(
                {
                    "order_id": str(item["id"]),
                    "client_order_id": str(item.get("clientOrderId") or ""),
                    "symbol": symbol,
                    "native_symbol": self._market_id(markets, symbol),
                    "status": item.get("status"),
                    "side": item.get("side"),
                    "type": item.get("type"),
                    "quantity": _decimal_text(
                        amount * size,
                        default="0",
                    ),
                    "filled_quantity": _decimal_text(
                        filled * size,
                        default="0",
                    ),
                    "trigger_price": _decimal_text(item.get("triggerPrice", item.get("stopPrice"))),
                    "reduce_only": bool(item.get("reduceOnly")),
                    "observed_at": _timestamp(
                        item.get("lastTradeTimestamp", item.get("timestamp")),
                        fallback=observed_at,
                    ),
                }
            )
        return tuple(sorted(rows, key=lambda item: str(item["order_id"])))

    def _fills(
        self,
        raw: object,
        markets: Mapping[str, Any],
        observed_at: datetime,
    ) -> tuple[JsonObject, ...]:
        if raw is None:
            return ()
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise DomainRejected("FACT_ADAPTER_RESPONSE_INVALID", "CCXT fills response is invalid")
        rows: list[JsonObject] = []
        for item in raw:
            if not isinstance(item, Mapping) or item.get("id") is None:
                raise DomainRejected("FACT_ADAPTER_RESPONSE_INVALID", "CCXT fill row is invalid")
            symbol = str(item.get("symbol") or "")
            amount = _decimal(item.get("amount"))
            price = _decimal(item.get("price"))
            if not symbol or amount is None or price is None:
                raise DomainRejected(
                    "FACT_ADAPTER_RESPONSE_INCOMPLETE",
                    "CCXT fill quantity or price is unknown",
                )
            fee = item.get("fee")
            fee_mapping = fee if isinstance(fee, Mapping) else {}
            size = self._contract_size(markets, symbol)
            rows.append(
                {
                    "fill_id": str(item["id"]),
                    "order_id": None if item.get("order") is None else str(item["order"]),
                    "symbol": symbol,
                    "native_symbol": self._market_id(markets, symbol),
                    "side": item.get("side"),
                    "quantity": _decimal_text(
                        amount * size,
                        default="0",
                    ),
                    "price": format(price, "f"),
                    "fee": _decimal_text(fee_mapping.get("cost"), default="0"),
                    "fee_currency": fee_mapping.get("currency"),
                    "executed_at": _timestamp(item.get("timestamp"), fallback=observed_at),
                }
            )
        return tuple(sorted(rows, key=lambda item: str(item["fill_id"])))

    @staticmethod
    def _balances(raw: object, observed_at: datetime) -> tuple[JsonObject, ...]:
        if not isinstance(raw, Mapping):
            raise DomainRejected(
                "FACT_ADAPTER_RESPONSE_INVALID", "CCXT balance response is invalid"
            )
        free: Mapping[str, object] = (
            cast(Mapping[str, object], raw.get("free"))
            if isinstance(raw.get("free"), Mapping)
            else {}
        )
        used: Mapping[str, object] = (
            cast(Mapping[str, object], raw.get("used"))
            if isinstance(raw.get("used"), Mapping)
            else {}
        )
        total: Mapping[str, object] = (
            cast(Mapping[str, object], raw.get("total"))
            if isinstance(raw.get("total"), Mapping)
            else {}
        )
        currencies = sorted(set(free) | set(used) | set(total))
        return tuple(
            {
                "currency": str(currency),
                "free": _decimal_text(free.get(currency)),
                "used": _decimal_text(used.get(currency)),
                "total": _decimal_text(total.get(currency)),
                "observed_at": observed_at.isoformat(),
            }
            for currency in currencies
            if any(source.get(currency) is not None for source in (free, used, total))
        )

    def _marks(
        self,
        raw: object,
        markets: Mapping[str, Any],
        observed_at: datetime,
        symbols: Sequence[str],
    ) -> tuple[JsonObject, ...]:
        if raw is None:
            return ()
        if not isinstance(raw, Mapping):
            raise DomainRejected(
                "FACT_ADAPTER_RESPONSE_INVALID", "CCXT tickers response is invalid"
            )
        rows: list[JsonObject] = []
        for symbol in symbols:
            value = raw.get(symbol)
            if not isinstance(value, Mapping):
                continue
            mark = value.get("markPrice")
            if mark is None:
                info = value.get("info")
                mark = info.get("markPrice") if isinstance(info, Mapping) else None
            rows.append(
                {
                    "symbol": symbol,
                    "native_symbol": self._market_id(markets, symbol),
                    "mark_price": _decimal_text(mark),
                    "observed_at": _timestamp(value.get("timestamp"), fallback=observed_at),
                }
            )
        return tuple(rows)

    def _funding(
        self,
        history: object,
        rates: object,
        markets: Mapping[str, Any],
        observed_at: datetime,
        symbols: Sequence[str],
    ) -> tuple[JsonObject, ...]:
        rows: list[JsonObject] = []
        if isinstance(history, Sequence) and not isinstance(history, (str, bytes)):
            for item in history:
                if not isinstance(item, Mapping):
                    continue
                symbol = str(item.get("symbol") or "")
                rows.append(
                    {
                        "kind": "PAYMENT",
                        "payment_id": str(item.get("id") or _fingerprint(dict(item))),
                        "symbol": symbol,
                        "native_symbol": self._market_id(markets, symbol),
                        "amount": _decimal_text(item.get("amount")),
                        "currency": item.get("code"),
                        "observed_at": _timestamp(item.get("timestamp"), fallback=observed_at),
                    }
                )
        if isinstance(rates, Mapping):
            for symbol in symbols:
                value = rates.get(symbol)
                if not isinstance(value, Mapping):
                    continue
                rows.append(
                    {
                        "kind": "RATE",
                        "symbol": symbol,
                        "native_symbol": self._market_id(markets, symbol),
                        "funding_rate": _decimal_text(value.get("fundingRate")),
                        "next_funding_at": _timestamp(
                            value.get("fundingTimestamp", value.get("nextFundingTimestamp")),
                            fallback=observed_at,
                        ),
                        "observed_at": _timestamp(value.get("timestamp"), fallback=observed_at),
                    }
                )
        return tuple(rows)

    async def snapshot(
        self,
        *,
        reason: SnapshotReason,
        observed_at: datetime | None = None,
    ) -> ExchangeFactSnapshot:
        if self._closed:
            raise DomainRejected("FACT_ADAPTER_CLOSED", "the fact adapter is closed")
        self.validate_capabilities()
        now = _utc(self._clock() if observed_at is None else observed_at)
        markets = await self._load_markets()
        since = int((now - self._history_window).timestamp() * 1_000)
        balance_task = asyncio.create_task(self._rest("fetchBalance"))
        # Account facts deliberately use the account-wide unified calls.  Passing
        # the Freqtrade pair allowlist here would hide manual/non-Freqtrade
        # positions and orders from reconciliation.
        positions_task = asyncio.create_task(self._rest("fetchPositions"))
        orders_task = asyncio.create_task(self._rest("fetchOpenOrders"))
        optional_tasks = {
            "fills": asyncio.create_task(self._optional_rest("fetchMyTrades", None, since, 1_000)),
            "funding_history": asyncio.create_task(
                self._optional_rest("fetchFundingHistory", None, since, 1_000)
            ),
            "status": asyncio.create_task(self._optional_rest("fetchStatus")),
        }
        try:
            balance, positions, orders = await asyncio.gather(
                balance_task, positions_task, orders_task
            )
            optional = {name: await task for name, task in optional_tasks.items()}
        except Exception as exc:
            for task in (*optional_tasks.values(),):
                task.cancel()
            raise DomainRejected(
                "FACT_ADAPTER_SNAPSHOT_UNAVAILABLE",
                "the complete account snapshot could not be confirmed",
                metadata={"error_type": type(exc).__name__},
            ) from exc
        position_rows = self._positions(positions, markets, now)
        order_rows = self._orders(orders, markets, now)
        for row in position_rows:
            symbol = row.get("symbol")
            quantity = _decimal(row.get("quantity"), default=Decimal(0)) or Decimal(0)
            if isinstance(symbol, str) and symbol and quantity != 0:
                self._tracked_symbols.add(symbol)
        self._tracked_symbols.update(
            str(row["symbol"])
            for row in order_rows
            if isinstance(row.get("symbol"), str) and row["symbol"]
        )
        tracked_symbols = tuple(sorted(self._tracked_symbols))
        funding_rates, funding_rates_error = await self._optional_rest(
            "fetchFundingRates", list(tracked_symbols)
        )
        tickers_method = getattr(self._exchange, "fetch_tickers", None)
        tickers_capability = self._capability("fetchTickers") and tickers_method is not None
        tickers = None
        if tickers_capability:
            self._metrics.rest_requests["fetchTickers"] = (
                self._metrics.rest_requests.get("fetchTickers", 0) + 1
            )
            try:
                tickers = await cast(Callable[..., Awaitable[Any]], tickers_method)(
                    list(tracked_symbols)
                )
            except Exception as exc:
                logger.warning(
                    "Optional exchange ticker snapshot failed",
                    extra={
                        "event": "fact_adapter_optional_read_failed",
                        "component": "fact-adapter",
                        "venue": self.scope.venue,
                        "account_id": self.scope.account_id,
                        "capability": "fetchTickers",
                        "error_type": type(exc).__name__,
                    },
                )
        unknown = [
            capability
            for capability in _OPTIONAL_REST_CAPABILITIES
            if not self._capability(capability)
        ]
        for _name, (_value, failed_capability) in optional.items():
            if failed_capability is not None and failed_capability not in unknown:
                unknown.append(failed_capability)
        if funding_rates_error is not None and funding_rates_error not in unknown:
            unknown.append(funding_rates_error)
        if tickers is None:
            unknown.append("fetchTickers")
        self._snapshot_version += 1
        if reason in {
            "RECONNECT_COMPENSATION",
            "SEQUENCE_GAP_COMPENSATION",
        }:
            self._metrics.rest_compensations += 1
        elif reason == "PERIODIC_RECONCILIATION":
            self._metrics.periodic_reconciliations += 1
        elif reason == "LIMITED_REST_FALLBACK":
            self._metrics.limited_rest_fallbacks += 1
        fills_raw = optional["fills"][0]
        funding_history = optional["funding_history"][0]
        account_status = optional["status"][0]
        return ExchangeFactSnapshot(
            scope=self.scope,
            snapshot_version=self._snapshot_version,
            observed_at=now,
            data_status="CURRENT",
            reason=reason,
            positions=position_rows,
            orders=order_rows,
            fills=self._fills(fills_raw, markets, now),
            balances=self._balances(balance, now),
            instruments=self._instruments(markets, tracked_symbols),
            marks=self._marks(tickers, markets, now, tracked_symbols),
            funding=self._funding(
                funding_history,
                funding_rates,
                markets,
                now,
                tracked_symbols,
            ),
            account_status=(dict(account_status) if isinstance(account_status, Mapping) else None),
            unknown_fields=tuple(sorted(set(unknown))),
            metrics=self._metrics.freeze(),
        )

    async def watch(self, kind: EventKind) -> JsonObject:
        if kind not in self.watch_channels:
            raise DomainRejected(
                "FACT_ADAPTER_WEBSOCKET_UNSUPPORTED",
                "the selected exchange does not support the requested fact stream",
            )
        capability = _WATCH_CAPABILITIES[kind]
        method_name = {
            "BALANCE": "watch_balance",
            "POSITION": "watch_positions",
            "ORDER": "watch_orders",
            "FILL": "watch_my_trades",
            "MARK": "watch_tickers",
        }[kind]
        method = getattr(self._exchange, method_name, None)
        if method is None:
            raise DomainRejected(
                "FACT_ADAPTER_CAPABILITY_CONTRACT_INVALID",
                f"CCXT advertised {capability} without its callable contract",
            )
        args: tuple[object, ...]
        if kind == "MARK":
            args = (sorted(self._tracked_symbols),)
        else:
            args = ()
        raw = await cast(Callable[..., Awaitable[Any]], method)(*args)
        observed_at = _utc(self._clock())
        markets = getattr(self._exchange, "markets", {})
        if not isinstance(markets, Mapping):
            markets = {}
        if kind == "POSITION":
            position_rows = self._positions(raw, markets, observed_at)
            self._tracked_symbols.update(
                str(row["symbol"])
                for row in position_rows
                if (_decimal(row.get("quantity"), default=Decimal(0)) or Decimal(0)) != 0
            )
            payload: JsonObject = {"positions": list(position_rows)}
        elif kind == "ORDER":
            order_rows = self._orders(raw, markets, observed_at)
            self._tracked_symbols.update(str(row["symbol"]) for row in order_rows)
            payload = {"orders": list(order_rows)}
        elif kind == "FILL":
            payload = {"fills": list(self._fills(raw, markets, observed_at))}
        elif kind == "BALANCE":
            payload = {"balances": list(self._balances(raw, observed_at))}
        elif kind == "MARK":
            payload = {
                "marks": list(self._marks(raw, markets, observed_at, sorted(self._tracked_symbols)))
            }
        else:
            payload = {"status": raw}
        return payload

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._exchange, "close", None)
        if close is not None:
            await cast(Callable[[], Awaitable[Any]], close)()


class FactAdapterConnectionProbe:
    """One-shot account-scoped read-only verification using the production adapter."""

    def __init__(
        self,
        *,
        bootstrap_symbols: Mapping[str, str],
        exchange_factory: ExchangeFactory = _default_exchange_factory,
    ) -> None:
        self._bootstrap_symbols = dict(bootstrap_symbols)
        self._exchange_factory = exchange_factory

    async def _verify(
        self,
        *,
        workspace_id: str,
        team_id: str,
        account_id: str,
        venue: str,
        environment: str,
        account_mode: str,
        credentials: Mapping[str, str],
    ) -> ConnectionProbeResult:
        adapter: CcxtProFactAdapter | None = None
        try:
            symbol = self._bootstrap_symbols.get(venue)
            if symbol is None:
                raise DomainRejected(
                    "EXCHANGE_VENUE_UNSUPPORTED",
                    "the exchange venue is unsupported",
                )
            scope = FactAdapterScope(
                workspace_id=workspace_id,
                team_id=team_id,
                account_id=account_id,
                venue=cast(Venue, venue),
                environment=cast(Environment, environment),
                symbols=(symbol,),
                account_mode=account_mode,
            )
            adapter = CcxtProFactAdapter(
                scope,
                credentials=credentials,
                exchange_factory=self._exchange_factory,
            )
            adapter.validate_exchange_mapping()
            adapter.validate_capabilities()
            snapshot = await adapter.snapshot(reason="INITIAL")
            if snapshot.scope != scope or snapshot.data_status != "CURRENT":
                raise DomainRejected(
                    "FACT_ADAPTER_PROBE_SCOPE_MISMATCH",
                    "the read-only snapshot did not confirm the exact account scope",
                )
            return ConnectionProbeResult(success=True, error_code=None)
        except DomainRejected as exc:
            return ConnectionProbeResult(
                success=False,
                error_code=exc.code,
                diagnostics=None if exc.metadata is None else dict(exc.metadata),
            )
        except (KeyError, TypeError, ValueError):
            return ConnectionProbeResult(
                success=False,
                error_code="FACT_ADAPTER_CONNECTION_CONFIGURATION_INVALID",
            )
        except Exception as exc:
            return ConnectionProbeResult(
                success=False,
                error_code="FACT_ADAPTER_CONNECTION_FAILED",
                diagnostics={"error_type": type(exc).__name__},
            )
        finally:
            if adapter is not None:
                await adapter.close()

    def verify(
        self,
        *,
        workspace_id: str,
        team_id: str,
        account_id: str,
        venue: str,
        environment: str,
        account_mode: str,
        credentials: Mapping[str, str],
        now: datetime,
    ) -> ConnectionProbeResult:
        del now
        return asyncio.run(
            self._verify(
                workspace_id=workspace_id,
                team_id=team_id,
                account_id=account_id,
                venue=venue,
                environment=environment,
                account_mode=account_mode,
                credentials=credentials,
            )
        )


@dataclass(slots=True)
class _RegistryState:
    adapter: ExchangeFactAdapter
    snapshot: ExchangeFactSnapshot | None = None
    stream_id: str = field(default_factory=lambda: str(uuid4()))
    sequence: int = 0
    fingerprints: deque[str] = field(default_factory=deque)
    fingerprint_set: set[str] = field(default_factory=set)
    subscribers: set[asyncio.Queue[ExchangeFactEvent]] = field(default_factory=set)
    task: asyncio.Task[None] | None = None


class FactAdapterRegistry:
    """Account-isolated latest snapshot and monotonic event registry."""

    def __init__(self, *, subscriber_queue_size: int = 256) -> None:
        if subscriber_queue_size < 8 or subscriber_queue_size > 10_000:
            raise ValueError("fact subscriber queue size is outside its bounded range")
        self._subscriber_queue_size = subscriber_queue_size
        self._states: dict[str, _RegistryState] = {}
        self._lock = asyncio.Lock()

    async def register(self, adapter: ExchangeFactAdapter) -> None:
        async with self._lock:
            if adapter.scope.key in self._states:
                raise DomainRejected(
                    "FACT_ADAPTER_SCOPE_CONFLICT",
                    "only one fact adapter may own an exact account scope",
                )
            self._states[adapter.scope.key] = _RegistryState(adapter=adapter)

    async def unregister(self, scope_key: str) -> None:
        async with self._lock:
            state = self._states.pop(scope_key, None)
        if state is None:
            return
        if state.task is not None:
            state.task.cancel()
            await asyncio.gather(state.task, return_exceptions=True)
        await state.adapter.close()

    async def attach_task(self, scope_key: str, task: asyncio.Task[None]) -> None:
        async with self._lock:
            state = self._states.get(scope_key)
            if state is None:
                task.cancel()
                raise DomainRejected(
                    "FACT_ADAPTER_SCOPE_NOT_FOUND", "the fact adapter scope is not registered"
                )
            if state.task is not None:
                task.cancel()
                raise DomainRejected(
                    "FACT_ADAPTER_TASK_CONFLICT",
                    "the fact adapter scope already has a stream task",
                )
            state.task = task

    async def scope_keys(self) -> tuple[str, ...]:
        async with self._lock:
            return tuple(sorted(self._states))

    async def scope(self, key: str) -> FactAdapterScope:
        async with self._lock:
            state = self._states.get(key)
            if state is None:
                raise DomainRejected(
                    "FACT_ADAPTER_SCOPE_NOT_FOUND", "the fact adapter scope is not registered"
                )
            return state.adapter.scope

    async def publish_snapshot(self, snapshot: ExchangeFactSnapshot) -> ExchangeFactEvent:
        async with self._lock:
            state = self._states.get(snapshot.scope.key)
            if state is None or state.adapter.scope != snapshot.scope:
                raise DomainRejected(
                    "FACT_ADAPTER_SCOPE_NOT_FOUND", "the fact adapter scope is not registered"
                )
            if state.snapshot is not None:
                if snapshot.observed_at < state.snapshot.observed_at:
                    raise DomainRejected(
                        "FACT_ADAPTER_SNAPSHOT_SUPERSEDED",
                        "an older fact snapshot cannot replace the current snapshot",
                    )
                snapshot = replace(
                    snapshot,
                    snapshot_version=state.snapshot.snapshot_version + 1,
                )
            state.snapshot = snapshot
            return self._publish_locked(state, "SNAPSHOT", {"snapshot": snapshot.to_dict()})

    def _remember_fingerprint(self, state: _RegistryState, fingerprint: str) -> bool:
        if fingerprint in state.fingerprint_set:
            metrics = getattr(state.adapter, "_metrics", None)
            if isinstance(metrics, _MutableMetrics):
                metrics.duplicate_events += 1
            return False
        state.fingerprints.append(fingerprint)
        state.fingerprint_set.add(fingerprint)
        while len(state.fingerprints) > _MAX_EVENT_FINGERPRINTS:
            state.fingerprint_set.discard(state.fingerprints.popleft())
        return True

    def _publish_locked(
        self,
        state: _RegistryState,
        kind: EventKind,
        payload: JsonObject,
    ) -> ExchangeFactEvent:
        fingerprint = _fingerprint(
            {"scope": state.adapter.scope.key, "kind": kind, "payload": payload}
        )
        if kind != "SNAPSHOT" and not self._remember_fingerprint(state, fingerprint):
            raise DomainRejected("FACT_EVENT_DUPLICATE", "duplicate fact event was ignored")
        self._merge_event_snapshot(state, kind, payload)
        state.sequence += 1
        event = ExchangeFactEvent(
            scope=state.adapter.scope,
            stream_id=state.stream_id,
            sequence=state.sequence,
            kind=kind,
            observed_at=_utc(),
            payload=payload,
            snapshot_version=0 if state.snapshot is None else state.snapshot.snapshot_version,
        )
        overflowed = False
        for queue in tuple(state.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                overflowed = True
                state.subscribers.discard(queue)
        if overflowed:
            metrics = getattr(state.adapter, "_metrics", None)
            if isinstance(metrics, _MutableMetrics):
                metrics.sequence_gaps += 1
        return event

    @staticmethod
    def _merge_rows(
        existing: tuple[JsonObject, ...],
        incoming: object,
        *,
        identity: tuple[str, ...],
        append_only: bool = False,
        metrics: _MutableMetrics | None = None,
    ) -> tuple[JsonObject, ...]:
        if not isinstance(incoming, list) or any(not isinstance(row, dict) for row in incoming):
            raise DomainRejected(
                "FACT_EVENT_INVALID",
                "fact WebSocket event rows are invalid",
            )

        def key(row: Mapping[str, Any]) -> tuple[str, ...]:
            values = tuple(str(row.get(field) or "") for field in identity)
            if any(not value for value in values):
                raise DomainRejected(
                    "FACT_EVENT_INVALID",
                    "fact WebSocket event identity is incomplete",
                )
            return values

        merged = {key(row): dict(row) for row in existing}
        for row in incoming:
            row_key = key(row)
            if append_only and row_key in merged:
                continue
            current = merged.get(row_key)
            if current is not None:
                current_time = str(current.get("observed_at") or "")
                incoming_time = str(row.get("observed_at") or "")
                if current_time and incoming_time and incoming_time < current_time:
                    if metrics is not None:
                        metrics.out_of_order_events += 1
                    continue
            merged[row_key] = dict(row)
        return tuple(merged[item] for item in sorted(merged))

    def _merge_event_snapshot(
        self,
        state: _RegistryState,
        kind: EventKind,
        payload: JsonObject,
    ) -> None:
        snapshot = state.snapshot
        if snapshot is None or kind in {"SNAPSHOT", "STATUS"}:
            return
        positions = snapshot.positions
        orders = snapshot.orders
        fills = snapshot.fills
        balances = snapshot.balances
        marks = snapshot.marks
        adapter_metrics = getattr(state.adapter, "_metrics", None)
        mutable_metrics = (
            adapter_metrics if isinstance(adapter_metrics, _MutableMetrics) else None
        )
        if kind == "POSITION":
            positions = self._merge_rows(
                positions,
                payload.get("positions"),
                identity=("native_symbol", "side"),
                metrics=mutable_metrics,
            )
        elif kind == "ORDER":
            orders = self._merge_rows(
                orders,
                payload.get("orders"),
                identity=("order_id",),
                metrics=mutable_metrics,
            )
        elif kind == "FILL":
            fills = self._merge_rows(
                fills,
                payload.get("fills"),
                identity=("fill_id",),
                append_only=True,
                metrics=mutable_metrics,
            )
        elif kind == "BALANCE":
            balances = self._merge_rows(
                balances,
                payload.get("balances"),
                identity=("currency",),
                metrics=mutable_metrics,
            )
        elif kind == "MARK":
            marks = self._merge_rows(
                marks,
                payload.get("marks"),
                identity=("native_symbol",),
                metrics=mutable_metrics,
            )
        else:
            return
        state.snapshot = ExchangeFactSnapshot(
            scope=snapshot.scope,
            snapshot_version=snapshot.snapshot_version + 1,
            observed_at=_utc(),
            data_status="CURRENT",
            reason="WEBSOCKET_INCREMENT",
            positions=positions,
            orders=orders,
            fills=fills,
            balances=balances,
            instruments=snapshot.instruments,
            marks=marks,
            funding=snapshot.funding,
            account_status=snapshot.account_status,
            unknown_fields=snapshot.unknown_fields,
            metrics=(
                snapshot.metrics if mutable_metrics is None else mutable_metrics.freeze()
            ),
        )

    async def publish(
        self,
        scope_key: str,
        kind: EventKind,
        payload: JsonObject,
    ) -> ExchangeFactEvent | None:
        async with self._lock:
            state = self._states.get(scope_key)
            if state is None:
                raise DomainRejected(
                    "FACT_ADAPTER_SCOPE_NOT_FOUND", "the fact adapter scope is not registered"
                )
            try:
                event = self._publish_locked(state, kind, payload)
            except DomainRejected as exc:
                if exc.code == "FACT_EVENT_DUPLICATE":
                    return None
                raise
            return event

    async def latest(self, scope_key: str, *, stale_after: timedelta) -> ExchangeFactSnapshot:
        async with self._lock:
            state = self._states.get(scope_key)
            if state is None or state.snapshot is None:
                raise DomainRejected(
                    "FACT_SNAPSHOT_UNKNOWN", "the account fact snapshot is not yet known"
                )
            snapshot = state.snapshot
        if _utc() - snapshot.observed_at <= stale_after:
            return snapshot
        return ExchangeFactSnapshot(
            scope=snapshot.scope,
            snapshot_version=snapshot.snapshot_version,
            observed_at=snapshot.observed_at,
            data_status="STALE",
            reason=snapshot.reason,
            positions=snapshot.positions,
            orders=snapshot.orders,
            fills=snapshot.fills,
            balances=snapshot.balances,
            instruments=snapshot.instruments,
            marks=snapshot.marks,
            funding=snapshot.funding,
            account_status=snapshot.account_status,
            unknown_fields=snapshot.unknown_fields,
            metrics=snapshot.metrics,
        )

    async def subscribe(self, scope_key: str) -> AsyncGenerator[ExchangeFactEvent, None]:
        queue: asyncio.Queue[ExchangeFactEvent] = asyncio.Queue(maxsize=self._subscriber_queue_size)
        async with self._lock:
            state = self._states.get(scope_key)
            if state is None:
                raise DomainRejected(
                    "FACT_ADAPTER_SCOPE_NOT_FOUND", "the fact adapter scope is not registered"
                )
            state.subscribers.add(queue)
            snapshot = state.snapshot
            if snapshot is not None:
                queue.put_nowait(
                    ExchangeFactEvent(
                        scope=state.adapter.scope,
                        stream_id=state.stream_id,
                        sequence=state.sequence,
                        kind="SNAPSHOT",
                        observed_at=_utc(),
                        payload={"snapshot": snapshot.to_dict()},
                        snapshot_version=snapshot.snapshot_version,
                    )
                )
        try:
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                current = self._states.get(scope_key)
                if current is not None:
                    current.subscribers.discard(queue)

    async def state(self, scope_key: str) -> _RegistryState:
        async with self._lock:
            state = self._states.get(scope_key)
            if state is None:
                raise DomainRejected(
                    "FACT_ADAPTER_SCOPE_NOT_FOUND", "the fact adapter scope is not registered"
                )
            return state

    async def health(self, *, stale_after: timedelta) -> JsonObject:
        now = _utc()
        async with self._lock:
            states = tuple(self._states.items())
        unknown = sorted(key for key, state in states if state.snapshot is None)
        stale = sorted(
            key
            for key, state in states
            if state.snapshot is not None and now - state.snapshot.observed_at > stale_after
        )
        stopped = sorted(
            key for key, state in states if state.task is not None and state.task.done()
        )
        return {
            "status": "ready" if not (unknown or stale or stopped) else "not_ready",
            "scope_count": len(states),
            "unknown_scope_count": len(unknown),
            "stale_scope_count": len(stale),
            "stopped_scope_count": len(stopped),
        }

    async def close(self) -> None:
        async with self._lock:
            states = tuple(self._states.values())
            self._states.clear()
        for state in states:
            if state.task is not None:
                state.task.cancel()
        await asyncio.gather(*(state.adapter.close() for state in states), return_exceptions=True)


class FactStreamSupervisor:
    """WebSocket-first fact loop with bounded recovery and REST compensation."""

    def __init__(
        self,
        registry: FactAdapterRegistry,
        adapter: ExchangeFactAdapter,
        *,
        reconciliation_seconds: int = 300,
        fallback_seconds: int = 60,
        reconnect_initial_seconds: float = 1,
        reconnect_max_seconds: float = 30,
        max_reconnect_attempts: int = 8,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        snapshot_callback: Callable[[ExchangeFactSnapshot], Awaitable[None]] | None = None,
        persistence_coalesce_seconds: float = 1.0,
    ) -> None:
        if not 30 <= reconciliation_seconds <= 3_600:
            raise ValueError("fact reconciliation interval is outside its bounded range")
        if not 30 <= fallback_seconds <= 900:
            raise ValueError("fact REST fallback interval is outside its bounded range")
        if reconnect_initial_seconds <= 0 or reconnect_max_seconds < reconnect_initial_seconds:
            raise ValueError("fact reconnect bounds are invalid")
        if not 1 <= max_reconnect_attempts <= 20:
            raise ValueError("fact reconnect attempts are outside their bounded range")
        if not 0 <= persistence_coalesce_seconds <= 5:
            raise ValueError("fact persistence coalescing is outside its bounded range")
        self.registry = registry
        self.adapter = adapter
        self.reconciliation_seconds = reconciliation_seconds
        self.fallback_seconds = fallback_seconds
        self.reconnect_initial_seconds = reconnect_initial_seconds
        self.reconnect_max_seconds = reconnect_max_seconds
        self.max_reconnect_attempts = max_reconnect_attempts
        self.sleeper = sleeper
        self.snapshot_callback = snapshot_callback
        self.persistence_coalesce_seconds = persistence_coalesce_seconds
        self._stop = asyncio.Event()
        self._callback_task: asyncio.Task[None] | None = None
        self._last_callback_at = 0.0

    async def _persist_current(self) -> None:
        if self.snapshot_callback is None:
            return
        current = await self.registry.latest(
            self.adapter.scope.key,
            stale_after=timedelta(days=1),
        )
        await self.snapshot_callback(current)
        self._last_callback_at = asyncio.get_running_loop().time()

    async def _flush_callback(self) -> None:
        if self._callback_task is not None:
            self._callback_task.cancel()
            await asyncio.gather(self._callback_task, return_exceptions=True)
            self._callback_task = None
        await self._persist_current()

    async def _schedule_callback(self) -> None:
        if self.snapshot_callback is None:
            return
        if self._callback_task is not None:
            if not self._callback_task.done():
                return
            await self._callback_task
            self._callback_task = None
        loop = asyncio.get_running_loop()
        delay = max(
            0.0,
            self.persistence_coalesce_seconds - (loop.time() - self._last_callback_at),
        )
        if delay == 0:
            await self._persist_current()
            return

        async def persist_later() -> None:
            await asyncio.sleep(delay)
            await self._persist_current()

        self._callback_task = asyncio.create_task(
            persist_later(),
            name=f"fact-persist:{self.adapter.scope.key}",
        )

    async def _snapshot(self, reason: SnapshotReason) -> None:
        snapshot = await self.adapter.snapshot(reason=reason)
        await self.registry.publish_snapshot(snapshot)
        if self.snapshot_callback is not None:
            await self._flush_callback()

    async def _watch_channel(
        self,
        kind: EventKind,
        queue: asyncio.Queue[tuple[EventKind, JsonObject]],
    ) -> None:
        while not self._stop.is_set():
            payload = await self.adapter.watch(kind)
            await queue.put((kind, payload))

    async def _requires_scope_compensation(
        self,
        kind: EventKind,
        payload: JsonObject,
    ) -> bool:
        if kind not in {"POSITION", "ORDER"}:
            return False
        current = await self.registry.latest(
            self.adapter.scope.key,
            stale_after=timedelta(days=1),
        )
        known = {str(row.get("native_symbol") or "") for row in current.instruments}
        field = "positions" if kind == "POSITION" else "orders"
        rows = payload.get(field)
        if not isinstance(rows, list):
            return True
        return any(
            not isinstance(row, Mapping)
            or not row.get("native_symbol")
            or str(row["native_symbol"]) not in known
            for row in rows
        )

    async def _run_connected(self) -> None:
        channels = self.adapter.watch_channels
        if not channels:
            while not self._stop.is_set():
                await self.sleeper(float(self.fallback_seconds))
                if self._stop.is_set():
                    return
                await self._snapshot("LIMITED_REST_FALLBACK")
            return
        metrics = getattr(self.adapter, "_metrics", None)
        if isinstance(metrics, _MutableMetrics):
            metrics.websocket_subscriptions = len(channels)
        queue: asyncio.Queue[tuple[EventKind, JsonObject]] = asyncio.Queue(maxsize=256)
        tasks = [asyncio.create_task(self._watch_channel(kind, queue)) for kind in channels]
        queue_task: asyncio.Task[tuple[EventKind, JsonObject]] | None = None
        loop = asyncio.get_running_loop()
        next_reconciliation = loop.time() + self.reconciliation_seconds
        try:
            while not self._stop.is_set():
                queue_task = asyncio.create_task(queue.get())
                done, _pending = await asyncio.wait(
                    (queue_task, *tasks),
                    timeout=max(0, next_reconciliation - loop.time()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                failed = next((task for task in tasks if task in done), None)
                if failed is not None:
                    error = failed.exception()
                    if error is None:
                        raise DomainRejected(
                            "FACT_ADAPTER_WEBSOCKET_UNAVAILABLE",
                            "a fact WebSocket channel stopped unexpectedly",
                        )
                    raise error
                if queue_task not in done:
                    queue_task.cancel()
                    await asyncio.gather(queue_task, return_exceptions=True)
                    queue_task = None
                    await self._snapshot("PERIODIC_RECONCILIATION")
                    next_reconciliation = loop.time() + self.reconciliation_seconds
                    continue
                kind, payload = queue_task.result()
                queue_task = None
                if await self._requires_scope_compensation(kind, payload):
                    # A position/order opened outside Freqtrade can introduce a
                    # market not present in the previous snapshot.  Rebuild the
                    # account-wide snapshot before exposing the increment so
                    # instruments, marks and balances remain internally complete.
                    await self._snapshot("SEQUENCE_GAP_COMPENSATION")
                    next_reconciliation = loop.time() + self.reconciliation_seconds
                    continue
                event = await self.registry.publish(self.adapter.scope.key, kind, payload)
                if event is not None and self.snapshot_callback is not None:
                    await self._schedule_callback()
                if loop.time() >= next_reconciliation:
                    await self._snapshot("PERIODIC_RECONCILIATION")
                    next_reconciliation = loop.time() + self.reconciliation_seconds
        finally:
            if queue_task is not None:
                queue_task.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(
                *(tasks + ([] if queue_task is None else [queue_task])),
                return_exceptions=True,
            )
            if self._callback_task is not None:
                await self._callback_task
                self._callback_task = None

    async def run(self) -> None:
        await self._snapshot("INITIAL")
        attempts = 0
        while not self._stop.is_set():
            try:
                await self._run_connected()
                attempts = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempts += 1
                metrics = getattr(self.adapter, "_metrics", None)
                if isinstance(metrics, _MutableMetrics):
                    metrics.websocket_reconnects += 1
                logger.warning(
                    "Fact WebSocket connection interrupted",
                    extra={
                        "event": "fact_adapter_websocket_reconnect",
                        "component": "fact-adapter",
                        "venue": self.adapter.scope.venue,
                        "account_id": self.adapter.scope.account_id,
                        "attempt": attempts,
                        "error_type": type(exc).__name__,
                    },
                )
                if attempts > self.max_reconnect_attempts:
                    raise DomainRejected(
                        "FACT_ADAPTER_WEBSOCKET_UNAVAILABLE",
                        "the fact stream exceeded its bounded reconnect attempts",
                    ) from exc
                delay = min(
                    self.reconnect_max_seconds,
                    self.reconnect_initial_seconds * (2 ** (attempts - 1)),
                )
                await self.sleeper(delay)
                await self._snapshot("RECONNECT_COMPENSATION")

    def stop(self) -> None:
        self._stop.set()


__all__ = [
    "CcxtProFactAdapter",
    "Environment",
    "EventKind",
    "ExchangeFactAdapter",
    "ExchangeFactEvent",
    "ExchangeFactSnapshot",
    "FactAdapterConnectionProbe",
    "FactAdapterMetrics",
    "FactAdapterRegistry",
    "FactAdapterScope",
    "FactStreamSupervisor",
    "Venue",
]
