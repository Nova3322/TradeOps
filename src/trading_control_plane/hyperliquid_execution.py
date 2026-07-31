from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from threading import Lock
from typing import Any, Literal, cast

from eth_account import Account
from hyperliquid.utils.signing import sign_l1_action  # type: ignore[import-untyped]

from trading_control_plane.binance_execution import ProtectionCancelCommand
from trading_control_plane.domain import DomainRejected
from trading_control_plane.hyperliquid import ADDRESS_PATTERN

JsonObject = dict[str, Any]
JsonValue = JsonObject | list[Any]
JsonRequester = Callable[[str, JsonObject, float], JsonValue]
ActionSigner = Callable[[JsonObject, int], JsonObject]
OFFICIAL_TESTNET_HOST = "api.hyperliquid-testnet.xyz"
OFFICIAL_LIVE_HOST = "api.hyperliquid.xyz"
CLOID_PATTERN = re.compile(r"^0x[0-9a-fA-F]{32}$")


def build_hyperliquid_signer(
    private_key: str | None,
    *,
    api_wallet_address: str | None,
    active_pool: str | None,
    is_mainnet: bool,
) -> ActionSigner | None:
    """Build the official SDK signer without exposing key material to application logs."""

    if private_key is None:
        return None
    try:
        wallet = Account.from_key(private_key)
    except (TypeError, ValueError) as exc:
        raise ValueError("Hyperliquid API wallet private key is invalid") from exc
    if api_wallet_address is not None:
        if not ADDRESS_PATTERN.fullmatch(api_wallet_address):
            raise ValueError("Hyperliquid API wallet address is invalid")
        if wallet.address.lower() != api_wallet_address.lower():
            raise ValueError(
                "Hyperliquid API wallet private key does not match its configured address"
            )

    def signer(action: JsonObject, nonce: int) -> JsonObject:
        return cast(
            JsonObject,
            sign_l1_action(
                wallet,
                action,
                active_pool.lower() if active_pool else None,
                nonce,
                None,
                is_mainnet,
            ),
        )

    return signer


def _default_requester(url: str, payload: JsonObject, timeout: float) -> JsonValue:
    request = urllib.request.Request(  # noqa: S310
        url,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    write = urllib.parse.urlparse(url).path == "/exchange"
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        code = "HYPERLIQUID_TESTNET_OUTCOME_UNKNOWN" if write else "HYPERLIQUID_TESTNET_UNAVAILABLE"
        raise DomainRejected(code, "Hyperliquid testnet request outcome is unavailable") from exc
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DomainRejected(
            "HYPERLIQUID_TESTNET_RESPONSE_INVALID",
            "Hyperliquid testnet returned invalid JSON",
        ) from exc
    if not isinstance(value, (dict, list)):
        raise DomainRejected(
            "HYPERLIQUID_TESTNET_RESPONSE_INVALID",
            "Hyperliquid testnet response shape is invalid",
        )
    return value


def _decimal(raw: Any, field: str, *, minimum: Decimal = Decimal(0)) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DomainRejected(
            "HYPERLIQUID_TESTNET_RESPONSE_INVALID",
            f"Hyperliquid field {field} is not numeric",
        ) from exc
    if not value.is_finite() or value < minimum:
        raise DomainRejected(
            "HYPERLIQUID_TESTNET_RESPONSE_INVALID",
            f"Hyperliquid field {field} is outside its valid range",
        )
    return value


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _time(raw: Any, fallback: datetime) -> datetime:
    try:
        milliseconds = int(raw)
        return fallback if milliseconds <= 0 else datetime.fromtimestamp(milliseconds / 1000, UTC)
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        raise DomainRejected(
            "HYPERLIQUID_TESTNET_RESPONSE_INVALID",
            "Hyperliquid order timestamp is invalid",
        ) from exc


@dataclass(frozen=True, slots=True)
class HyperliquidTestnetOrderCommand:
    symbol: str
    side: str
    quantity: Decimal
    limit_price: Decimal
    reduce_only: bool
    client_order_id: str


@dataclass(frozen=True, slots=True)
class HyperliquidTestnetProtectionCommand:
    symbol: str
    side: str
    quantity: Decimal
    trigger_price: Decimal
    limit_price: Decimal
    client_order_id: str


@dataclass(frozen=True, slots=True)
class HyperliquidTestnetOrder:
    order_id: str
    client_order_id: str
    status: str
    side: str
    order_type: str
    ordered_quantity: Decimal
    filled_quantity: Decimal
    limit_price: Decimal
    stop_price: Decimal
    reduce_only: bool
    close_position: bool
    observed_at: datetime


class HyperliquidTestnetClient:
    """Narrow Core TESTNET client using an injected official-compatible signer."""

    def __init__(
        self,
        *,
        base_url: str,
        account_address: str | None,
        signer: ActionSigner | None,
        subaccount_address: str | None = None,
        dex: str = "",
        requester: JsonRequester = _default_requester,
        environment: Literal["TESTNET", "LIVE"] = "TESTNET",
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        expected_host = OFFICIAL_TESTNET_HOST if environment == "TESTNET" else OFFICIAL_LIVE_HOST
        if parsed.scheme != "https" or parsed.hostname != expected_host:
            raise ValueError(
                f"Hyperliquid execution base URL must be the official {environment.lower()} API"
            )
        if dex:
            raise ValueError("this execution adapter supports Hyperliquid Core only")
        if subaccount_address is not None and not ADDRESS_PATTERN.fullmatch(subaccount_address):
            raise ValueError("Hyperliquid subaccount address is invalid")
        self._base_url = base_url.rstrip("/")
        self._account_address = account_address
        self._signer = signer
        self._subaccount_address = subaccount_address
        self._requester = requester
        self._nonce_lock = Lock()
        self._last_nonce = 0

    @property
    def configured(self) -> bool:
        return bool(
            self._account_address
            and ADDRESS_PATTERN.fullmatch(self._account_address)
            and self._signer is not None
        )

    def _require_configured(self) -> str:
        if not self.configured:
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_NOT_CONFIGURED",
                "Hyperliquid testnet requires a main account address and injected signer",
            )
        assert self._account_address is not None
        return self._subaccount_address or self._account_address

    @property
    def account_scope(self) -> str:
        return "SUBACCOUNT" if self._subaccount_address else "MAIN_ACCOUNT"

    def _info(self, payload: JsonObject) -> JsonValue:
        return self._requester(f"{self._base_url}/info", payload, 5.0)

    def _exchange(self, action: JsonObject, *, now: datetime) -> JsonObject:
        self._require_configured()
        assert self._signer is not None
        with self._nonce_lock:
            nonce = max(int(now.timestamp() * 1_000), self._last_nonce + 1)
            self._last_nonce = nonce
        signature = self._signer(action, nonce)
        if not isinstance(signature, dict) or not {"r", "s", "v"}.issubset(signature):
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_SIGNER_INVALID",
                "injected signer did not return the official r/s/v signature shape",
            )
        body: JsonObject = {"action": action, "nonce": nonce, "signature": signature}
        if self._subaccount_address is not None:
            body["vaultAddress"] = self._subaccount_address
        raw = self._requester(f"{self._base_url}/exchange", body, 5.0)
        if not isinstance(raw, dict):
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_RESPONSE_INVALID",
                "Hyperliquid Exchange response is invalid",
            )
        return raw

    def _asset_index(self, symbol: str) -> tuple[int, int]:
        raw = self._info({"type": "metaAndAssetCtxs", "dex": ""})
        if not isinstance(raw, list) or len(raw) != 2 or not isinstance(raw[0], dict):
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_RESPONSE_INVALID",
                "Hyperliquid Core metadata is invalid",
            )
        universe = raw[0].get("universe")
        if not isinstance(universe, list):
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_RESPONSE_INVALID",
                "Hyperliquid Core universe is invalid",
            )
        for index, item in enumerate(universe):
            if isinstance(item, dict) and item.get("name") == symbol:
                try:
                    size_decimals = int(item["szDecimals"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise DomainRejected(
                        "HYPERLIQUID_TESTNET_RESPONSE_INVALID",
                        "Hyperliquid size precision is invalid",
                    ) from exc
                if size_decimals < 0 or size_decimals > 6:
                    raise DomainRejected(
                        "HYPERLIQUID_TESTNET_RESPONSE_INVALID",
                        "Hyperliquid size precision is invalid",
                    )
                return index, size_decimals
        raise DomainRejected(
            "HYPERLIQUID_INSTRUMENT_UNAVAILABLE",
            "Hyperliquid Core instrument is unavailable",
        )

    @staticmethod
    def _validate_precision(quantity: Decimal, price: Decimal, size_decimals: int) -> None:
        if not quantity.is_finite() or not price.is_finite() or quantity <= 0 or price <= 0:
            raise DomainRejected(
                "HYPERLIQUID_ORDER_PRECISION_INVALID",
                "Hyperliquid quantity and explicit price must be positive",
            )
        lot = Decimal(1).scaleb(-size_decimals)
        if quantity % lot != 0:
            raise DomainRejected(
                "HYPERLIQUID_ORDER_PRECISION_INVALID",
                "Hyperliquid quantity is not aligned to szDecimals",
            )
        normalized = price.normalize()
        exponent = normalized.as_tuple().exponent
        if not isinstance(exponent, int):
            raise DomainRejected(
                "HYPERLIQUID_ORDER_PRECISION_INVALID", "Hyperliquid price must be finite"
            )
        decimals = max(0, -exponent)
        significant = len(normalized.as_tuple().digits)
        if decimals > 6 - size_decimals or significant > 5:
            raise DomainRejected(
                "HYPERLIQUID_ORDER_PRECISION_INVALID",
                "Hyperliquid price exceeds the Core perpetual precision rule",
            )

    @staticmethod
    def _status(value: str) -> str:
        if value == "open":
            return "SENT"
        if value == "filled":
            return "FILLED"
        if value in {
            "canceled",
            "marginCanceled",
            "vaultWithdrawalCanceled",
            "openInterestCapCanceled",
            "selfTradeCanceled",
            "reduceOnlyCanceled",
            "siblingFilledCanceled",
            "delistedCanceled",
            "liquidatedCanceled",
            "scheduledCancel",
        }:
            return "CANCELLED"
        if value == "rejected" or value.endswith("Rejected"):
            return "REJECTED"
        return "UNKNOWN"

    def query_order(
        self,
        symbol: str,
        client_order_id: str,
        *,
        expected_order_type: str,
        now: datetime,
    ) -> HyperliquidTestnetOrder | None:
        account = self._require_configured()
        if not CLOID_PATTERN.fullmatch(client_order_id):
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_IDENTITY_INVALID", "client order id must be a 128-bit cloid"
            )
        raw = self._info({"type": "orderStatus", "user": account, "oid": client_order_id})
        if not isinstance(raw, dict):
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_RESPONSE_INVALID", "orderStatus response is invalid"
            )
        if raw.get("status") == "unknownOid":
            return None
        wrapper = raw.get("order")
        if raw.get("status") != "order" or not isinstance(wrapper, dict):
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_RESPONSE_INVALID", "orderStatus response is invalid"
            )
        order = wrapper.get("order")
        if not isinstance(order, dict):
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_RESPONSE_INVALID", "orderStatus order is invalid"
            )
        if order.get("coin") != symbol or order.get("cloid") != client_order_id:
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_IDENTITY_CONFLICT",
                "stable cloid refers to a different Hyperliquid instrument or identity",
            )
        side_code = str(order.get("side"))
        if side_code not in {"A", "B"}:
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_RESPONSE_INVALID", "Hyperliquid order side is invalid"
            )
        is_trigger = bool(order.get("isTrigger", False))
        if is_trigger != (expected_order_type == "TRIGGER_MARKET"):
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_IDENTITY_CONFLICT",
                "stable cloid refers to a different Hyperliquid order type",
            )
        ordered = _decimal(order.get("origSz"), "origSz")
        remaining = _decimal(order.get("sz"), "sz")
        if ordered <= 0 or remaining > ordered:
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_RESPONSE_INVALID", "Hyperliquid order size is invalid"
            )
        status_value = self._status(str(wrapper.get("status")))
        filled = ordered if status_value == "FILLED" else ordered - remaining
        return HyperliquidTestnetOrder(
            order_id=str(order.get("oid")),
            client_order_id=client_order_id,
            status=status_value,
            side="BUY" if side_code == "B" else "SELL",
            order_type=expected_order_type,
            ordered_quantity=ordered,
            filled_quantity=filled,
            limit_price=_decimal(order.get("limitPx"), "limitPx"),
            stop_price=_decimal(order.get("triggerPx", 0), "triggerPx"),
            reduce_only=bool(order.get("reduceOnly", False)),
            close_position=False,
            observed_at=_time(wrapper.get("statusTimestamp", order.get("timestamp", 0)), now),
        )

    @staticmethod
    def _order_response(
        raw: JsonObject,
        command: HyperliquidTestnetOrderCommand | HyperliquidTestnetProtectionCommand,
        order_type: str,
        *,
        now: datetime,
    ) -> HyperliquidTestnetOrder:
        response = raw.get("response")
        if raw.get("status") != "ok" or not isinstance(response, dict):
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_REJECTED", "Hyperliquid testnet rejected the action"
            )
        data = response.get("data")
        statuses = data.get("statuses") if isinstance(data, dict) else None
        if response.get("type") != "order" or not isinstance(statuses, list) or len(statuses) != 1:
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_RESPONSE_INVALID", "order action response is invalid"
            )
        item = statuses[0]
        if not isinstance(item, dict):
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_RESPONSE_INVALID", "order status response is invalid"
            )
        if "error" in item:
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_REJECTED", f"Hyperliquid rejected order: {item['error']}"
            )
        filled = item.get("filled")
        resting = item.get("resting")
        if isinstance(filled, dict):
            order_id = str(filled.get("oid"))
            filled_quantity = _decimal(filled.get("totalSz"), "totalSz")
            status_value = "FILLED" if filled_quantity == command.quantity else "CANCELLED"
        elif isinstance(resting, dict):
            order_id = str(resting.get("oid"))
            filled_quantity = Decimal(0)
            status_value = "SENT"
        else:
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_RESPONSE_INVALID", "order acknowledgement is invalid"
            )
        trigger_price = (
            command.trigger_price
            if isinstance(command, HyperliquidTestnetProtectionCommand)
            else Decimal(0)
        )
        return HyperliquidTestnetOrder(
            order_id=order_id,
            client_order_id=command.client_order_id,
            status=status_value,
            side=command.side,
            order_type=order_type,
            ordered_quantity=command.quantity,
            filled_quantity=filled_quantity,
            limit_price=command.limit_price,
            stop_price=trigger_price,
            reduce_only=command.reduce_only
            if isinstance(command, HyperliquidTestnetOrderCommand)
            else True,
            close_position=False,
            observed_at=now,
        )

    def ensure_order(
        self, command: HyperliquidTestnetOrderCommand, *, now: datetime
    ) -> HyperliquidTestnetOrder:
        existing = self.query_order(
            command.symbol,
            command.client_order_id,
            expected_order_type="IOC_LIMIT",
            now=now,
        )
        if existing is not None:
            self._validate_order(existing, command)
            return existing
        asset, size_decimals = self._asset_index(command.symbol)
        self._validate_precision(command.quantity, command.limit_price, size_decimals)
        action: JsonObject = {
            "type": "order",
            "orders": [
                {
                    "a": asset,
                    "b": command.side == "BUY",
                    "p": _decimal_text(command.limit_price),
                    "s": _decimal_text(command.quantity),
                    "r": command.reduce_only,
                    "t": {"limit": {"tif": "Ioc"}},
                    "c": command.client_order_id,
                }
            ],
            "grouping": "na",
        }
        try:
            created = self._order_response(
                self._exchange(action, now=now), command, "IOC_LIMIT", now=now
            )
        except DomainRejected as exc:
            if exc.code != "HYPERLIQUID_TESTNET_REJECTED":
                raise
            try:
                recovered = self.query_order(
                    command.symbol,
                    command.client_order_id,
                    expected_order_type="IOC_LIMIT",
                    now=now,
                )
            except DomainRejected as query_error:
                raise DomainRejected(
                    "HYPERLIQUID_TESTNET_OUTCOME_UNKNOWN",
                    "rejected response could not be reconciled by stable cloid",
                ) from query_error
            if recovered is None:
                raise
            self._validate_order(recovered, command)
            return recovered
        self._validate_order(created, command)
        return created

    def cancel_order(
        self, command: HyperliquidTestnetOrderCommand, *, now: datetime
    ) -> HyperliquidTestnetOrder | None:
        existing = self.query_order(
            command.symbol,
            command.client_order_id,
            expected_order_type="IOC_LIMIT",
            now=now,
        )
        if existing is None:
            return None
        self._validate_order(existing, command)
        if existing.status in {"CANCELLED", "REJECTED", "FILLED"}:
            return existing
        asset, _ = self._asset_index(command.symbol)
        raw = self._exchange(
            {
                "type": "cancelByCloid",
                "cancels": [{"asset": asset, "cloid": command.client_order_id}],
            },
            now=now,
        )
        response = raw.get("response")
        data = response.get("data") if isinstance(response, dict) else None
        statuses = data.get("statuses") if isinstance(data, dict) else None
        if raw.get("status") != "ok" or not isinstance(statuses, list) or statuses != ["success"]:
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_REJECTED", "Hyperliquid cancel was not acknowledged"
            )
        return replace(existing, status="CANCELLED", observed_at=now)

    def recover_order(
        self, command: HyperliquidTestnetOrderCommand, *, now: datetime
    ) -> HyperliquidTestnetOrder | None:
        existing = self.query_order(
            command.symbol,
            command.client_order_id,
            expected_order_type="IOC_LIMIT",
            now=now,
        )
        if existing is not None:
            self._validate_order(existing, command)
        return existing

    def ensure_protection(
        self, command: HyperliquidTestnetProtectionCommand, *, now: datetime
    ) -> HyperliquidTestnetOrder:
        existing = self.query_order(
            command.symbol,
            command.client_order_id,
            expected_order_type="TRIGGER_MARKET",
            now=now,
        )
        if existing is not None:
            self._validate_protection(existing, command)
            return existing
        asset, size_decimals = self._asset_index(command.symbol)
        self._validate_precision(command.quantity, command.limit_price, size_decimals)
        self._validate_precision(command.quantity, command.trigger_price, size_decimals)
        action: JsonObject = {
            "type": "order",
            "orders": [
                {
                    "a": asset,
                    "b": command.side == "BUY",
                    "p": _decimal_text(command.limit_price),
                    "s": _decimal_text(command.quantity),
                    "r": True,
                    "t": {
                        "trigger": {
                            "isMarket": True,
                            "triggerPx": _decimal_text(command.trigger_price),
                            "tpsl": "sl",
                        }
                    },
                    "c": command.client_order_id,
                }
            ],
            "grouping": "na",
        }
        try:
            created = self._order_response(
                self._exchange(action, now=now), command, "TRIGGER_MARKET", now=now
            )
        except DomainRejected as exc:
            if exc.code != "HYPERLIQUID_TESTNET_REJECTED":
                raise
            try:
                recovered = self.query_order(
                    command.symbol,
                    command.client_order_id,
                    expected_order_type="TRIGGER_MARKET",
                    now=now,
                )
            except DomainRejected as query_error:
                raise DomainRejected(
                    "HYPERLIQUID_TESTNET_OUTCOME_UNKNOWN",
                    "rejected protection response could not be reconciled by stable cloid",
                ) from query_error
            if recovered is None:
                raise
            self._validate_protection(recovered, command)
            return recovered
        self._validate_protection(created, command)
        return created

    def cancel_protection(
        self, command: ProtectionCancelCommand, *, now: datetime
    ) -> HyperliquidTestnetOrder | None:
        existing = self.query_order(
            command.symbol,
            command.client_order_id,
            expected_order_type="TRIGGER_MARKET",
            now=now,
        )
        if existing is None:
            return None
        if not existing.reduce_only:
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_IDENTITY_CONFLICT",
                "stable cloid does not refer to reduce-only protection",
            )
        if existing.status in {"CANCELLED", "REJECTED", "FILLED"}:
            return existing
        asset, _ = self._asset_index(command.symbol)
        raw = self._exchange(
            {
                "type": "cancelByCloid",
                "cancels": [{"asset": asset, "cloid": command.client_order_id}],
            },
            now=now,
        )
        response = raw.get("response")
        data = response.get("data") if isinstance(response, dict) else None
        statuses = data.get("statuses") if isinstance(data, dict) else None
        if raw.get("status") != "ok" or not isinstance(statuses, list) or statuses != ["success"]:
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_REJECTED",
                "Hyperliquid protection cancellation was not acknowledged",
            )
        return replace(existing, status="CANCELLED", observed_at=now)

    @staticmethod
    def _validate_order(
        order: HyperliquidTestnetOrder, command: HyperliquidTestnetOrderCommand
    ) -> None:
        if (
            order.client_order_id != command.client_order_id
            or order.side != command.side
            or order.order_type != "IOC_LIMIT"
            or order.ordered_quantity != command.quantity
            or order.limit_price != command.limit_price
            or order.reduce_only != command.reduce_only
            or order.stop_price != 0
            or order.close_position
            or order.filled_quantity > command.quantity
        ):
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_IDENTITY_CONFLICT",
                "stable cloid refers to different IOC order semantics",
            )

    @staticmethod
    def _validate_protection(
        order: HyperliquidTestnetOrder, command: HyperliquidTestnetProtectionCommand
    ) -> None:
        if (
            order.client_order_id != command.client_order_id
            or order.side != command.side
            or order.order_type != "TRIGGER_MARKET"
            or order.ordered_quantity != command.quantity
            or order.limit_price != command.limit_price
            or order.stop_price != command.trigger_price
            or not order.reduce_only
            or order.close_position
        ):
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_IDENTITY_CONFLICT",
                "stable cloid refers to different protection semantics",
            )


class HyperliquidLiveClient:
    """Production Core client backed by the official SDK signing implementation."""

    def __init__(
        self,
        *,
        base_url: str,
        account_address: str | None,
        signer: ActionSigner | None,
        subaccount_address: str | None = None,
        dex: str = "",
        requester: JsonRequester = _default_requester,
    ) -> None:
        self._client = HyperliquidTestnetClient(
            base_url=base_url,
            account_address=account_address,
            signer=signer,
            subaccount_address=subaccount_address,
            dex=dex,
            requester=requester,
            environment="LIVE",
        )

    @property
    def configured(self) -> bool:
        return self._client.configured

    @property
    def account_scope(self) -> str:
        return self._client.account_scope

    @staticmethod
    def _live_error(exc: DomainRejected) -> DomainRejected:
        return DomainRejected(
            exc.code.replace("HYPERLIQUID_TESTNET", "HYPERLIQUID_LIVE"),
            exc.detail.replace("Hyperliquid testnet", "Hyperliquid LIVE").replace(
                "testnet", "LIVE"
            ),
        )

    def query_order(
        self,
        symbol: str,
        client_order_id: str,
        *,
        expected_order_type: str,
        now: datetime,
    ) -> HyperliquidTestnetOrder | None:
        try:
            return self._client.query_order(
                symbol,
                client_order_id,
                expected_order_type=expected_order_type,
                now=now,
            )
        except DomainRejected as exc:
            raise self._live_error(exc) from exc

    def ensure_order(
        self, command: HyperliquidTestnetOrderCommand, *, now: datetime
    ) -> HyperliquidTestnetOrder:
        try:
            return self._client.ensure_order(command, now=now)
        except DomainRejected as exc:
            raise self._live_error(exc) from exc

    def cancel_order(
        self, command: HyperliquidTestnetOrderCommand, *, now: datetime
    ) -> HyperliquidTestnetOrder | None:
        try:
            return self._client.cancel_order(command, now=now)
        except DomainRejected as exc:
            raise self._live_error(exc) from exc

    def recover_order(
        self, command: HyperliquidTestnetOrderCommand, *, now: datetime
    ) -> HyperliquidTestnetOrder | None:
        try:
            return self._client.recover_order(command, now=now)
        except DomainRejected as exc:
            raise self._live_error(exc) from exc

    def ensure_protection(
        self, command: HyperliquidTestnetProtectionCommand, *, now: datetime
    ) -> HyperliquidTestnetOrder:
        try:
            return self._client.ensure_protection(command, now=now)
        except DomainRejected as exc:
            raise self._live_error(exc) from exc

    def cancel_protection(
        self, command: ProtectionCancelCommand, *, now: datetime
    ) -> HyperliquidTestnetOrder | None:
        try:
            return self._client.cancel_protection(command, now=now)
        except DomainRejected as exc:
            raise self._live_error(exc) from exc
