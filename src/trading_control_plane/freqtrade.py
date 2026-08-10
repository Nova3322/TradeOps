from __future__ import annotations

import base64
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from trading_control_plane.domain import DomainRejected

JsonObject = dict[str, Any]
JsonValue = JsonObject | list[Any]
JsonFetcher = Callable[[str, str, JsonObject | None, dict[str, str], float], JsonValue]

BINANCE_PERPETUAL_PATTERN = re.compile(r"^(?P<base>[^\s/:]{1,64})USDT$")
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
    if venue != "HYPERLIQUID":
        raise DomainRejected(
            "FREQTRADE_VENUE_UNSUPPORTED",
            "Freqtrade execution is restricted to Binance and Hyperliquid",
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


@dataclass(frozen=True, slots=True)
class FreqtradeWorkerSpec:
    name: str
    venue: Literal["BINANCE", "HYPERLIQUID"]
    base_url: str
    username: str | None
    password: str | None = field(repr=False)
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


@dataclass(frozen=True, slots=True)
class FreqtradeExitCommand:
    pair: str
    max_quantity: Decimal
    client_order_id: str


@dataclass(frozen=True, slots=True)
class FreqtradeTrade:
    trade_id: str
    pair: str
    side: Literal["long", "short"]
    amount: Decimal
    stake_amount: Decimal
    open_rate: Decimal
    close_rate: Decimal | None
    is_open: bool
    enter_tag: str
    leverage: Decimal
    stop_loss_abs: Decimal | None
    stoploss_order_id: str | None
    entry_order_id: str | None
    exit_order_id: str | None
    observed_at: datetime


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


def _stoploss_order_id(value: JsonObject) -> str | None:
    direct = value.get("stoploss_order_id")
    if direct is not None:
        return str(direct)
    orders = value.get("orders")
    if orders is None:
        return None
    if not isinstance(orders, list) or any(not isinstance(item, dict) for item in orders):
        raise DomainRejected(
            "FREQTRADE_WORKER_RESPONSE_INVALID",
            "Freqtrade trade orders are invalid",
        )
    active = [
        item
        for item in orders
        if item.get("ft_order_side") == "stoploss"
        and (item.get("is_open") is True or item.get("status") == "open")
    ]
    if len(active) > 1:
        raise DomainRejected(
            "FREQTRADE_WORKER_RESPONSE_INVALID",
            "Freqtrade trade exposes multiple active stop-loss orders",
        )
    if not active:
        return None
    order_id = active[0].get("order_id")
    if order_id is None:
        raise DomainRejected(
            "FREQTRADE_WORKER_RESPONSE_INVALID",
            "Freqtrade active stop-loss order identity is missing",
        )
    return str(order_id)


def _execution_order_id(
    value: JsonObject,
    *,
    is_short: bool,
    phase: Literal["entry", "exit"],
) -> str | None:
    orders = value.get("orders")
    if orders is None:
        return None
    if not isinstance(orders, list) or any(not isinstance(item, dict) for item in orders):
        raise DomainRejected(
            "FREQTRADE_WORKER_RESPONSE_INVALID",
            "Freqtrade trade orders are invalid",
        )
    entry_side = "sell" if is_short else "buy"
    expected_side = entry_side if phase == "entry" else ("buy" if is_short else "sell")
    matches = [
        str(item["order_id"])
        for item in orders
        if item.get("ft_order_side") == expected_side
        and item.get("order_id") is not None
        and (item.get("status") == "closed" or item.get("is_open") is False)
    ]
    if len(matches) > 1:
        raise DomainRejected(
            "FREQTRADE_WORKER_RESPONSE_INVALID",
            f"Freqtrade trade exposes multiple {phase} execution orders",
        )
    return None if not matches else matches[0]


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
    return FreqtradeTrade(
        trade_id=str(trade_id),
        pair=pair,
        side="short" if is_short else "long",
        amount=_decimal(value.get("amount"), "amount", positive=True),
        stake_amount=_decimal(value.get("stake_amount"), "stake_amount", positive=True),
        open_rate=_decimal(value.get("open_rate"), "open_rate", positive=True),
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
        stoploss_order_id=_stoploss_order_id(value),
        entry_order_id=_execution_order_id(value, is_short=is_short, phase="entry"),
        exit_order_id=_execution_order_id(value, is_short=is_short, phase="exit"),
        observed_at=_trade_timestamp(value),
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
    ) -> None:
        if timeout_seconds <= 0 or confirmation_timeout_seconds < 10:
            raise ValueError("Freqtrade timeouts are outside their bounded range")
        self.spec = FreqtradeWorkerSpec(
            name=spec.name,
            venue=spec.venue,
            base_url=validate_worker_url(spec.base_url),
            username=spec.username,
            password=spec.password,
            hip3_dexes=spec.hip3_dexes,
            exchange_account_id=spec.exchange_account_id,
            team_id=spec.team_id,
            account_id=spec.account_id,
        )
        self.timeout_seconds = timeout_seconds
        self.confirmation_timeout_seconds = confirmation_timeout_seconds
        self._fetcher = fetcher

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
        expected_exchange = "binance" if self.spec.venue == "BINANCE" else "hyperliquid"
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
        if expected_mode == "LIVE":
            if config.get("force_entry_enable") is not True:
                raise DomainRejected(
                    "FREQTRADE_FORCE_ENTRY_DISABLED",
                    "Freqtrade LIVE worker does not permit controlled force entry",
                )
            if str(config.get("state", "")).lower() != "running":
                raise DomainRejected(
                    "FREQTRADE_WORKER_NOT_RUNNING",
                    "Freqtrade LIVE worker is not running",
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
            "order_send": expected_mode == "LIVE",
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
        return tuple(parse_freqtrade_trade(item) for item in value)

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

    def _find_filled_entry(self, *, pair: str, enter_tag: str) -> FreqtradeTrade | None:
        """Read a force-entry result while tolerating Freqtrade's transient zero fill row."""

        value = self._authorized_request("status")
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise DomainRejected(
                "FREQTRADE_WORKER_RESPONSE_INVALID",
                "Freqtrade open-trade response is invalid",
            )
        scoped = [item for item in value if item.get("pair") == pair]
        matching = [item for item in scoped if item.get("enter_tag") == enter_tag]
        if len(scoped) > 1 or len(matching) > 1:
            raise DomainRejected(
                "FREQTRADE_SCOPE_AMBIGUOUS",
                "multiple live Freqtrade trades match the controlled entry scope",
            )
        if scoped and not matching:
            raise DomainRejected(
                "FREQTRADE_SCOPE_BUSY",
                "another Freqtrade trade appeared during controlled entry",
            )
        if not matching:
            return None
        try:
            amount = Decimal(str(matching[0].get("amount", 0)))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise DomainRejected(
                "FREQTRADE_WORKER_RESPONSE_INVALID",
                "Freqtrade pending entry amount is invalid",
            ) from exc
        if amount == 0:
            return None
        if amount < 0:
            raise DomainRejected(
                "FREQTRADE_WORKER_RESPONSE_INVALID",
                "Freqtrade pending entry amount cannot be negative",
            )
        return parse_freqtrade_trade(matching[0])

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

    def force_enter(self, command: FreqtradeEntryCommand) -> FreqtradeTrade:
        if command.stake_amount <= 0 or command.max_quantity <= 0 or command.leverage <= 0:
            raise DomainRejected(
                "FREQTRADE_ORDER_INVALID",
                "Freqtrade LIVE entry values must be positive",
            )
        self.probe(expected_mode="LIVE", required_pair=command.pair)
        existing = self.find_open_trade(pair=command.pair, enter_tag=command.enter_tag)
        if existing is not None:
            if existing.side != command.side or existing.amount > command.max_quantity:
                raise DomainRejected(
                    "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                    "existing Freqtrade trade changed frozen entry semantics",
                )
            return self.wait_for_protection(trade_id=existing.trade_id, pair=command.pair)
        if self.find_open_trade(pair=command.pair) is not None:
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
        deadline = time.monotonic() + self.confirmation_timeout_seconds
        while time.monotonic() <= deadline:
            found = self._find_filled_entry(pair=command.pair, enter_tag=command.enter_tag)
            if found is not None:
                if found.side != command.side or found.amount > command.max_quantity:
                    raise DomainRejected(
                        "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                        "Freqtrade filled more than the frozen maximum quantity",
                    )
                return self.wait_for_protection(trade_id=found.trade_id, pair=command.pair)
            time.sleep(0.25)
        raise DomainRejected(
            "FREQTRADE_LIVE_OUTCOME_UNKNOWN",
            "Freqtrade entry outcome could not be confirmed within the bounded timeout",
        )

    def force_exit(self, trade_id: str, *, pair: str) -> FreqtradeTrade:
        current = self.find_open_trade(pair=pair)
        if current is None or current.trade_id != trade_id:
            recovered = self.trade(trade_id)
            if recovered.pair != pair:
                raise DomainRejected(
                    "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                    "Freqtrade trade does not match the controlled exit scope",
                )
            if not recovered.is_open:
                return recovered
            raise DomainRejected(
                "FREQTRADE_SCOPE_AMBIGUOUS",
                "the requested Freqtrade exit trade is not the unique open scope",
            )
        try:
            self._authorized_request(
                "forceexit",
                method="POST",
                payload={"tradeid": trade_id, "ordertype": "market"},
            )
        except DomainRejected as exc:
            if exc.code != "FREQTRADE_WORKER_UNAVAILABLE":
                raise
        deadline = time.monotonic() + self.confirmation_timeout_seconds
        while time.monotonic() <= deadline:
            open_trade = self.find_open_trade(pair=pair)
            if open_trade is None:
                recovered = self.trade(trade_id)
                if recovered.pair != pair or recovered.is_open:
                    raise DomainRejected(
                        "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                        "Freqtrade exit result changed the controlled scope",
                    )
                return recovered
            if open_trade.trade_id != trade_id:
                raise DomainRejected(
                    "FREQTRADE_SCOPE_AMBIGUOUS",
                    "a different Freqtrade trade appeared during exit",
                )
            time.sleep(0.25)
        raise DomainRejected(
            "FREQTRADE_LIVE_OUTCOME_UNKNOWN",
            "Freqtrade exit outcome could not be confirmed within the bounded timeout",
        )
