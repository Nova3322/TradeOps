from __future__ import annotations

import base64
import http.client
import json
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
from trading_control_plane.freqtrade_contracts import (
    FREQTRADE_TRANSACTION_RPC_TYPES as FREQTRADE_TRANSACTION_RPC_TYPES,
)
from trading_control_plane.freqtrade_contracts import (
    FreqtradeEntryCommand as FreqtradeEntryCommand,
)
from trading_control_plane.freqtrade_contracts import (
    FreqtradeExitCommand as FreqtradeExitCommand,
)
from trading_control_plane.freqtrade_contracts import (
    FreqtradeOrder as FreqtradeOrder,
)
from trading_control_plane.freqtrade_contracts import (
    FreqtradeRpcMessage as FreqtradeRpcMessage,
)
from trading_control_plane.freqtrade_contracts import (
    FreqtradeTrade as FreqtradeTrade,
)
from trading_control_plane.freqtrade_contracts import (
    freqtrade_active_stop_order as freqtrade_active_stop_order,
)
from trading_control_plane.freqtrade_contracts import (
    freqtrade_execution_order as freqtrade_execution_order,
)
from trading_control_plane.freqtrade_contracts import (
    freqtrade_pair as freqtrade_pair,
)
from trading_control_plane.freqtrade_contracts import (
    parse_freqtrade_rpc_message as parse_freqtrade_rpc_message,
)
from trading_control_plane.freqtrade_contracts import (
    parse_freqtrade_trade as parse_freqtrade_trade,
)
from trading_control_plane.freqtrade_contracts import (
    parse_hip3_dexes as parse_hip3_dexes,
)
from trading_control_plane.freqtrade_contracts import (
    validate_worker_url as validate_worker_url,
)

JsonObject = dict[str, Any]
JsonValue = JsonObject | list[Any]
JsonFetcher = Callable[[str, str, JsonObject | None, dict[str, str], float], JsonValue]
WebSocketConnector = Callable[[str, float], Any]


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
        return urllib.parse.urlunparse((scheme, parsed.netloc, "/api/v1/message/ws", "", query, ""))

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
        expected_mode: Literal["DRY_RUN", "TESTNET", "LIVE"] = "DRY_RUN",
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
                    else (
                        "FREQTRADE_EXTERNAL_TESTNET_REQUIRED"
                        if expected_mode == "TESTNET"
                        else "FREQTRADE_LIVE_MODE_FORBIDDEN"
                    )
                ),
                "Freqtrade worker mode does not match the requested execution boundary",
            )
        if expected_mode == "TESTNET" and (
            config.get("demo_trading") is not False
            or config.get("bot_name") != f"tradeops-{self.spec.venue.lower()}-testnet"
        ):
            raise DomainRejected(
                "FREQTRADE_TESTNET_IDENTITY_MISMATCH",
                "Freqtrade TESTNET worker identity is not the pinned exchange sandbox",
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
            "external_order_send": expected_mode in {"TESTNET", "LIVE"},
            "network": expected_mode,
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
        expected_mode: Literal["DRY_RUN", "TESTNET", "LIVE"] = "LIVE",
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
