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

JsonObject = dict[str, Any]
JsonRequester = Callable[[str, str, dict[str, str], float], JsonObject]
OFFICIAL_TESTNET_HOST = "testnet.binancefuture.com"


def _default_requester(
    method: str, url: str, headers: dict[str, str], timeout: float
) -> JsonObject:
    request = urllib.request.Request(url, headers=headers, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read()
    except urllib.error.HTTPError as exc:
        try:
            value = json.loads(exc.read())
        except json.JSONDecodeError as parse_error:
            raise DomainRejected(
                "BINANCE_TESTNET_RESPONSE_INVALID", "Binance testnet returned invalid JSON"
            ) from parse_error
        if isinstance(value, dict):
            return value
        raise DomainRejected(
            "BINANCE_TESTNET_RESPONSE_INVALID", "Binance testnet error response is invalid"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        code = (
            "BINANCE_TESTNET_OUTCOME_UNKNOWN"
            if method in {"POST", "DELETE"}
            else "BINANCE_TESTNET_UNAVAILABLE"
        )
        raise DomainRejected(
            code, "Binance testnet request outcome could not be confirmed"
        ) from exc
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DomainRejected(
            "BINANCE_TESTNET_RESPONSE_INVALID", "Binance testnet returned invalid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise DomainRejected(
            "BINANCE_TESTNET_RESPONSE_INVALID", "Binance testnet response shape is invalid"
        )
    return value


def _decimal(raw: Any, field: str, *, minimum: Decimal = Decimal(0)) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DomainRejected(
            "BINANCE_TESTNET_RESPONSE_INVALID", f"Binance field {field} is not numeric"
        ) from exc
    if value < minimum:
        raise DomainRejected(
            "BINANCE_TESTNET_RESPONSE_INVALID", f"Binance field {field} is below its minimum"
        )
    return value


def _observed_time(raw: Any, fallback: datetime) -> datetime:
    try:
        milliseconds = int(raw)
        return fallback if milliseconds <= 0 else datetime.fromtimestamp(milliseconds / 1000, UTC)
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        raise DomainRejected(
            "BINANCE_TESTNET_RESPONSE_INVALID", "Binance order timestamp is invalid"
        ) from exc


@dataclass(frozen=True, slots=True)
class BinanceTestnetOrderCommand:
    symbol: str
    side: str
    quantity: Decimal
    reduce_only: bool
    client_order_id: str


@dataclass(frozen=True, slots=True)
class BinanceTestnetProtectionCommand:
    symbol: str
    side: str
    trigger_price: Decimal
    client_order_id: str


@dataclass(frozen=True, slots=True)
class BinanceTestnetOrder:
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


class BinanceTestnetClient:
    """Narrow USDⓈ-M testnet order client; the official testnet host is mandatory."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        api_secret: str | None,
        recv_window_ms: int = 5_000,
        requester: JsonRequester = _default_requester,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname != OFFICIAL_TESTNET_HOST:
            raise ValueError("Binance execution base URL must be the official USDⓈ-M testnet")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._recv_window_ms = recv_window_ms
        self._requester = requester

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self._api_secret)

    def _signed(
        self,
        method: str,
        path: str,
        params: dict[str, str],
        *,
        now: datetime,
    ) -> JsonObject:
        if not self.configured:
            raise DomainRejected(
                "BINANCE_TESTNET_NOT_CONFIGURED", "Binance testnet credentials are not configured"
            )
        signed = {
            **params,
            "recvWindow": str(self._recv_window_ms),
            "timestamp": str(int(now.timestamp() * 1_000)),
        }
        query = urllib.parse.urlencode(signed)
        assert self._api_secret is not None
        signature = hmac.new(self._api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        assert self._api_key is not None
        return self._requester(
            method,
            f"{self._base_url}{path}?{query}&signature={signature}",
            {"X-MBX-APIKEY": self._api_key},
            5.0,
        )

    @staticmethod
    def _api_error(raw: JsonObject) -> int | None:
        try:
            code = int(raw.get("code", 0))
        except (TypeError, ValueError) as exc:
            raise DomainRejected(
                "BINANCE_TESTNET_RESPONSE_INVALID", "Binance testnet error code is invalid"
            ) from exc
        return code if code < 0 else None

    @classmethod
    def _parse_order(cls, raw: JsonObject, *, now: datetime) -> BinanceTestnetOrder:
        error = cls._api_error(raw)
        if error is not None:
            raise DomainRejected(
                "BINANCE_TESTNET_REJECTED", f"Binance testnet rejected order request ({error})"
            )
        status_map = {
            "NEW": "SENT",
            "PARTIALLY_FILLED": "PARTIALLY_FILLED",
            "FILLED": "FILLED",
            "CANCELED": "CANCELLED",
            "EXPIRED": "CANCELLED",
            "EXPIRED_IN_MATCH": "CANCELLED",
            "REJECTED": "REJECTED",
        }
        try:
            order_id = str(raw["orderId"])
            client_order_id = str(raw["clientOrderId"])
            side = str(raw["side"])
            order_type = str(raw["type"])
        except KeyError as exc:
            raise DomainRejected(
                "BINANCE_TESTNET_RESPONSE_INVALID", "Binance testnet order identity is incomplete"
            ) from exc
        if side not in {"BUY", "SELL"}:
            raise DomainRejected(
                "BINANCE_TESTNET_RESPONSE_INVALID", "Binance testnet order side is invalid"
            )
        return BinanceTestnetOrder(
            order_id=order_id,
            client_order_id=client_order_id,
            status=status_map.get(str(raw.get("status")), "UNKNOWN"),
            side=side,
            order_type=order_type,
            ordered_quantity=_decimal(raw.get("origQty", 0), "origQty"),
            filled_quantity=_decimal(raw.get("executedQty", 0), "executedQty"),
            stop_price=_decimal(raw.get("stopPrice", 0), "stopPrice"),
            reduce_only=bool(raw.get("reduceOnly", False)),
            close_position=bool(raw.get("closePosition", False)),
            observed_at=_observed_time(raw.get("updateTime", 0), now),
        )

    def query_order(
        self, symbol: str, client_order_id: str, *, now: datetime
    ) -> BinanceTestnetOrder | None:
        raw = self._signed(
            "GET",
            "/fapi/v1/order",
            {"symbol": symbol, "origClientOrderId": client_order_id},
            now=now,
        )
        if self._api_error(raw) == -2013:
            return None
        return self._parse_order(raw, now=now)

    def ensure_order(
        self, command: BinanceTestnetOrderCommand, *, now: datetime
    ) -> BinanceTestnetOrder:
        existing = self.query_order(command.symbol, command.client_order_id, now=now)
        if existing is not None:
            self._validate_command(existing, command)
            return existing
        params = {
            "symbol": command.symbol,
            "side": command.side,
            "type": "MARKET",
            "quantity": str(command.quantity),
            "newClientOrderId": command.client_order_id,
            "newOrderRespType": "RESULT",
        }
        if command.reduce_only:
            params["reduceOnly"] = "true"
        try:
            created = self._parse_order(
                self._signed("POST", "/fapi/v1/order", params, now=now), now=now
            )
        except DomainRejected as exc:
            if exc.code != "BINANCE_TESTNET_REJECTED":
                raise
            try:
                recovered = self.query_order(command.symbol, command.client_order_id, now=now)
            except DomainRejected as query_error:
                raise DomainRejected(
                    "BINANCE_TESTNET_OUTCOME_UNKNOWN",
                    "testnet rejected the create response but identity lookup is unavailable",
                ) from query_error
            if recovered is None:
                raise
            self._validate_command(recovered, command)
            return recovered
        self._validate_command(created, command)
        return created

    def cancel_order(
        self, command: BinanceTestnetOrderCommand, *, now: datetime
    ) -> BinanceTestnetOrder | None:
        existing = self.query_order(command.symbol, command.client_order_id, now=now)
        if existing is None:
            return None
        self._validate_command(existing, command)
        if existing.status in {"CANCELLED", "REJECTED", "FILLED"}:
            return existing
        return self._parse_order(
            self._signed(
                "DELETE",
                "/fapi/v1/order",
                {"symbol": command.symbol, "origClientOrderId": command.client_order_id},
                now=now,
            ),
            now=now,
        )

    def recover_order(
        self, command: BinanceTestnetOrderCommand, *, now: datetime
    ) -> BinanceTestnetOrder | None:
        existing = self.query_order(command.symbol, command.client_order_id, now=now)
        if existing is not None:
            self._validate_command(existing, command)
        return existing

    def ensure_protection(
        self, command: BinanceTestnetProtectionCommand, *, now: datetime
    ) -> BinanceTestnetOrder:
        existing = self.query_order(command.symbol, command.client_order_id, now=now)
        if existing is not None:
            self._validate_protection(existing, command)
            return existing
        created = self._parse_order(
            self._signed(
                "POST",
                "/fapi/v1/order",
                {
                    "symbol": command.symbol,
                    "side": command.side,
                    "type": "STOP_MARKET",
                    "stopPrice": str(command.trigger_price),
                    "closePosition": "true",
                    "workingType": "MARK_PRICE",
                    "newClientOrderId": command.client_order_id,
                },
                now=now,
            ),
            now=now,
        )
        self._validate_protection(created, command)
        return created

    @staticmethod
    def _validate_command(order: BinanceTestnetOrder, command: BinanceTestnetOrderCommand) -> None:
        if (
            order.client_order_id != command.client_order_id
            or order.side != command.side
            or order.order_type != "MARKET"
            or order.ordered_quantity != command.quantity
            or order.reduce_only != command.reduce_only
            or order.close_position
        ):
            raise DomainRejected(
                "BINANCE_TESTNET_IDENTITY_CONFLICT",
                "stable client order identity refers to different order semantics",
            )

    @staticmethod
    def _validate_protection(
        order: BinanceTestnetOrder, command: BinanceTestnetProtectionCommand
    ) -> None:
        if (
            order.client_order_id != command.client_order_id
            or order.side != command.side
            or order.order_type != "STOP_MARKET"
            or order.stop_price != command.trigger_price
            or not order.close_position
        ):
            raise DomainRejected(
                "BINANCE_TESTNET_IDENTITY_CONFLICT",
                "stable protection identity refers to different order semantics",
            )
