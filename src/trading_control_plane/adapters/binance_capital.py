from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn, cast

from trading_control_plane.binance_errors import (
    BinanceApiDiagnostic,
    BinanceApiRejected,
    BinanceRequestState,
    classify_binance_rate_limit,
)
from trading_control_plane.domain import DomainRejected

JsonValue = Any
Transport = Callable[[str, str, dict[str, str], float], JsonValue]

OFFICIAL_BINANCE_BASE_URLS = (
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api-gcp.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api4.binance.com",
)
OFFICIAL_BINANCE_HOSTS = frozenset(
    urllib.parse.urlparse(base_url).hostname for base_url in OFFICIAL_BINANCE_BASE_URLS
)
EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
SUPPORTED_ASSET = "USDC"
SUPPORTED_NETWORK = "ARBITRUM"
SUCCESSFUL_WITHDRAWAL_STATUS = 6
SUCCESSFUL_DEPOSIT_STATUS = 1
SPOT_TO_USDM = "MAIN_UMFUTURE"
USDM_TO_SPOT = "UMFUTURE_MAIN"


def _reject(code: str, detail: str) -> NoReturn:
    raise DomainRejected(code, detail)


def _decimal(value: object, *, code: str, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DomainRejected(code, f"Binance returned an invalid {field}") from exc
    if not result.is_finite() or result < 0:
        _reject(code, f"Binance returned an invalid {field}")
    return result


def _official_base_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_BINANCE_HOSTS:
        _reject(
            "BINANCE_CAPITAL_API_UNTRUSTED",
            "capital operations require an official Binance HTTPS API host",
        )
    return value.rstrip("/")


def _ordered_official_base_urls(preferred: str) -> tuple[str, ...]:
    return preferred, *(item for item in OFFICIAL_BINANCE_BASE_URLS if item != preferred)


def _evm_address(value: str, *, field: str) -> str:
    if EVM_ADDRESS.fullmatch(value) is None:
        _reject("BINANCE_CAPITAL_ADDRESS_INVALID", f"{field} is not a valid EVM address")
    return value.lower()


def _binance_http_error(exc: urllib.error.HTTPError) -> tuple[int | None, str | None]:
    try:
        raw = json.loads(exc.read())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, None
    if not isinstance(raw, dict):
        return None, None
    raw_code = raw.get("code")
    try:
        code = (
            int(raw_code)
            if isinstance(raw_code, (int, str)) and not isinstance(raw_code, bool)
            else None
        )
    except (TypeError, ValueError):
        code = None
    message = None if raw.get("msg") is None else str(raw["msg"])
    return code, message


class BinanceCapitalGateway:
    """Restricted Binance Wallet API boundary.

    Credentials remain in process memory, are never included in returned artifacts,
    and only the explicit withdrawal method can produce an external side effect.
    All other methods are bounded read-only probes or public receipt verification.
    """

    def __init__(
        self,
        *,
        base_url: str = "https://api.binance.com",
        api_key: str | None = None,
        api_secret: str | None = None,
        recv_window_ms: int = 5_000,
        timeout_seconds: float = 8,
        transport: Transport | None = None,
        clock_ms: Callable[[], int] | None = None,
        rate_limit_state: dict[str, BinanceApiDiagnostic] | None = None,
        request_state: BinanceRequestState | None = None,
    ) -> None:
        self._base_url = _official_base_url(base_url)
        self._active_base_url = self._base_url
        self._api_key = api_key
        self._api_secret = api_secret
        self._recv_window_ms = min(60_000, max(1_000, recv_window_ms))
        self._timeout_seconds = min(15.0, max(1.0, timeout_seconds))
        self._transport = transport
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._clock_offset_ms = 0
        self._clock_synchronized_at = 0.0
        self._rate_limit_state = rate_limit_state if rate_limit_state is not None else {}
        self._request_state = request_state or BinanceRequestState()

    def __repr__(self) -> str:
        return (
            f"BinanceCapitalGateway(base_url='{self._base_url}', "
            f"configured={self.configured}, timeout_seconds={self._timeout_seconds})"
        )

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self._api_secret)

    def attach_request_state(self, state: BinanceRequestState) -> None:
        """Attach the process/database shared policy without changing credentials."""

        self._request_state = state

    def with_credentials(self, *, api_key: str, api_secret: str) -> BinanceCapitalGateway:
        """Bind a short-lived exact-account credential without changing transport policy."""
        return BinanceCapitalGateway(
            base_url=self._base_url,
            api_key=api_key,
            api_secret=api_secret,
            recv_window_ms=self._recv_window_ms,
            timeout_seconds=self._timeout_seconds,
            transport=self._transport,
            clock_ms=self._clock_ms,
            rate_limit_state=self._rate_limit_state,
            request_state=self._request_state,
        )

    def _rate_limit_key(self) -> str:
        return "BINANCE_DEPLOYMENT_IP"

    def _enforce_rate_limit_backoff(self) -> None:
        diagnostic = self._rate_limit_state.get(self._rate_limit_key())
        shared = self._request_state.blocked_diagnostic()
        if shared is not None and (
            diagnostic is None or shared.next_retry_at > diagnostic.next_retry_at
        ):
            diagnostic = shared
        if diagnostic is None or datetime.now(UTC) >= diagnostic.next_retry_at:
            return
        labels = {
            "ORDINARY_RATE_LIMIT": "ordinary request rate limit",
            "REQUEST_WEIGHT_EXCEEDED": "request weight limit",
            "IP_TEMPORARILY_BANNED": "temporary IP ban",
        }
        raise BinanceApiRejected(
            "BINANCE_CAPITAL_RATE_LIMITED",
            f"Binance {labels[diagnostic.category]}; retry after next_retry_at",
            diagnostic,
        )

    def _rate_limit_rejection(
        self,
        exc: urllib.error.HTTPError,
        *,
        binance_error_code: int | None,
        binance_error_message: str | None,
    ) -> BinanceApiRejected:
        diagnostic = classify_binance_rate_limit(
            http_status=exc.code,
            binance_error_code=binance_error_code,
            binance_error_message=binance_error_message,
            headers=cast(dict[str, str], exc.headers or {}),
        )
        self._rate_limit_state[self._rate_limit_key()] = diagnostic
        self._request_state.record_rate_limit(
            diagnostic, host=urllib.parse.urlparse(self._active_base_url).hostname
        )
        label = {
            "ORDINARY_RATE_LIMIT": "ordinary request rate limit",
            "REQUEST_WEIGHT_EXCEEDED": "request weight limit",
            "IP_TEMPORARILY_BANNED": "temporary IP ban",
        }[diagnostic.category]
        return BinanceApiRejected(
            "BINANCE_CAPITAL_RATE_LIMITED",
            f"Binance {label}; retry after next_retry_at",
            diagnostic,
        )

    def _timestamp_ms(self) -> int:
        self._enforce_rate_limit_backoff()
        if self._transport is not None:
            return self._clock_ms()
        monotonic_now = time.monotonic()
        wall_now = datetime.now(UTC)
        shared_offset = self._request_state.current_time_offset()
        if (
            monotonic_now - self._clock_synchronized_at > 30
            and shared_offset is not None
            and wall_now - shared_offset[1] <= timedelta(seconds=30)
        ):
            self._clock_offset_ms = shared_offset[0]
            self._clock_synchronized_at = monotonic_now
        if monotonic_now - self._clock_synchronized_at > 30:
            server_time: int | None = None
            last_error: BaseException | None = None
            time_timeout = min(4.0, self._timeout_seconds)
            for base_url in _ordered_official_base_urls(self._active_base_url):
                request = urllib.request.Request(  # noqa: S310
                    f"{base_url}/api/v3/time",
                    method="GET",
                )
                try:
                    with urllib.request.urlopen(  # noqa: S310
                        request, timeout=time_timeout
                    ) as response:
                        raw = json.loads(response.read())
                        self._request_state.record_success(
                            getattr(response, "headers", {}),
                            host=urllib.parse.urlparse(base_url).hostname,
                        )
                    server_time = int(raw["serverTime"])
                    break
                except urllib.error.HTTPError as exc:
                    code, message = _binance_http_error(exc)
                    if exc.code in {418, 429} or code == -1003:
                        raise self._rate_limit_rejection(
                            exc,
                            binance_error_code=code,
                            binance_error_message=message,
                        ) from exc
                    last_error = exc
                except (
                    KeyError,
                    TypeError,
                    ValueError,
                    urllib.error.URLError,
                    TimeoutError,
                    json.JSONDecodeError,
                ) as exc:
                    last_error = exc
            if server_time is None:
                raise DomainRejected(
                    "BINANCE_CAPITAL_TIME_SYNC_FAILED",
                    "Binance server time could not be synchronized before a signed request",
                ) from last_error
            self._clock_offset_ms = server_time - self._clock_ms()
            self._clock_synchronized_at = monotonic_now
            self._request_state.record_time_offset(self._clock_offset_ms, synchronized_at=wall_now)
        return self._clock_ms() + self._clock_offset_ms

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, object] | None = None,
        *,
        priority: str = "CORE",
    ) -> Any:
        if not self.configured:
            _reject(
                "BINANCE_CAPITAL_CREDENTIALS_MISSING",
                "dedicated Binance capital API key and secret are not configured",
            )
        assert self._api_key is not None
        assert self._api_secret is not None
        self._enforce_rate_limit_backoff()
        if priority == "LOW":
            retry_at = self._request_state.low_priority_retry_at()
            if retry_at is not None and retry_at > datetime.now(UTC):
                raise DomainRejected(
                    "BINANCE_CAPITAL_WEIGHT_HEADROOM_DEFERRED",
                    "Binance non-urgent receipt read is deferred to preserve weight headroom",
                    metadata={"next_retry_at": retry_at.isoformat()},
                )
        semantic_params = {
            key: str(value) for key, value in (params or {}).items() if value is not None
        }
        if self._transport is not None:
            # Tests receive the unsigned semantic parameters only. Secret, API key,
            # signature and headers never cross this injection boundary.
            prepared = dict(semantic_params)
            prepared["recvWindow"] = str(self._recv_window_ms)
            prepared["timestamp"] = str(self._timestamp_ms())
            result = self._transport(method, path, dict(prepared), self._timeout_seconds)
            if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
                self._request_state.record_success(result[1], host="api.binance.com")
                return result[0]
            self._request_state.record_success(host="api.binance.com")
            return result
        base_urls = (
            _ordered_official_base_urls(self._active_base_url)
            if method == "GET"
            else (self._active_base_url,)
        )
        last_error: BaseException | None = None
        last_exchange_rejection: DomainRejected | None = None
        for attempt, base_url in enumerate(base_urls):
            prepared = dict(semantic_params)
            prepared["recvWindow"] = str(self._recv_window_ms)
            prepared["timestamp"] = str(self._timestamp_ms())
            query = urllib.parse.urlencode(prepared)
            signature = hmac.new(
                self._api_secret.encode(), query.encode(), hashlib.sha256
            ).hexdigest()
            signed_query = f"{query}&signature={signature}"
            url = f"{base_url}{path}"
            body: bytes | None = None
            if method == "GET":
                url = f"{url}?{signed_query}"
            else:
                body = signed_query.encode()
            request = urllib.request.Request(  # noqa: S310
                url,
                data=body,
                method=method,
                headers={
                    "X-MBX-APIKEY": self._api_key,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            try:
                with urllib.request.urlopen(  # noqa: S310
                    request, timeout=self._timeout_seconds
                ) as response:
                    result = json.loads(response.read())
                    self._request_state.record_success(
                        getattr(response, "headers", {}),
                        host=urllib.parse.urlparse(base_url).hostname,
                    )
                self._active_base_url = base_url
                return result
            except urllib.error.HTTPError as exc:
                code, message = _binance_http_error(exc)
                if code in {-2014, -2015} or exc.code in {401, 403}:
                    error_code = "BINANCE_CAPITAL_AUTHORIZATION_REJECTED"
                    detail = (
                        "Binance rejected the capital API key, source IP, or endpoint permission"
                    )
                elif code == -1021:
                    error_code = "BINANCE_CAPITAL_TIMESTAMP_REJECTED"
                    detail = "Binance rejected the signed request timestamp"
                elif code == -1003 or exc.code in {418, 429}:
                    raise self._rate_limit_rejection(
                        exc,
                        binance_error_code=code,
                        binance_error_message=message,
                    ) from exc
                elif exc.code >= 500:
                    error_code = "BINANCE_CAPITAL_API_UNAVAILABLE"
                    detail = "Binance Wallet API returned a server error"
                else:
                    error_code = "BINANCE_CAPITAL_API_REJECTED"
                    detail = f"Binance Wallet API rejected the request (code={code or 'UNKNOWN'})"
                last_error = exc
                rejection = DomainRejected(error_code, detail)
                if (
                    method == "GET"
                    and attempt + 1 < len(base_urls)
                    and (code in {-2014, -2015, -1021} or exc.code >= 500)
                ):
                    # Binance occasionally rejects a signed USER_DATA read on one
                    # documented edge while the same freshly signed request succeeds
                    # on another official edge.  GET probes are side-effect free, so
                    # fail over without weakening any permission, IP, address, balance,
                    # fee, quota, Travel Rule, or network check.  Withdrawal POSTs are
                    # still single-attempt because their outcome could be unknown.
                    last_exchange_rejection = rejection
                    if code == -1021:
                        self._clock_synchronized_at = 0.0
                    continue
                raise rejection from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt + 1 < len(base_urls):
                    # GET probes are side-effect free. Re-sign against the next bounded
                    # Binance official API host while retaining the fresh clock offset.
                    # POST withdrawal submission is never retried because its outcome
                    # could be unknown after a transport failure.
                    continue
                if last_exchange_rejection is not None:
                    raise last_exchange_rejection from exc
                raise DomainRejected(
                    "BINANCE_CAPITAL_API_UNAVAILABLE",
                    "Binance Wallet API did not return a valid bounded response",
                ) from exc
        if last_exchange_rejection is not None:
            raise last_exchange_rejection from last_error
        raise DomainRejected(
            "BINANCE_CAPITAL_API_UNAVAILABLE",
            "Binance Wallet API did not return a valid bounded response",
        ) from last_error

    def _permissions(self) -> dict[str, Any]:
        raw = self._request("GET", "/sapi/v1/account/apiRestrictions")
        if not isinstance(raw, dict):
            _reject("BINANCE_CAPITAL_RESPONSE_INVALID", "API permissions response is invalid")
        if raw.get("enableReading") is not True:
            _reject("BINANCE_CAPITAL_READING_DISABLED", "API key read permission is disabled")
        return raw

    def _spot_available(self) -> Decimal:
        raw = self._request(
            "POST",
            "/sapi/v3/asset/getUserAsset",
            {"asset": SUPPORTED_ASSET, "needBtcValuation": "false"},
        )
        if not isinstance(raw, list):
            _reject("BINANCE_CAPITAL_RESPONSE_INVALID", "spot asset response is invalid")
        item = next(
            (
                candidate
                for candidate in raw
                if isinstance(candidate, dict) and candidate.get("asset") == SUPPORTED_ASSET
            ),
            None,
        )
        if item is None:
            return Decimal(0)
        return _decimal(
            item.get("free"), code="BINANCE_CAPITAL_RESPONSE_INVALID", field="spot balance"
        )

    def _find_universal_transfer(
        self,
        *,
        transfer_type: str,
        amount: Decimal,
        prepared_at: datetime,
        now: datetime,
    ) -> dict[str, Any] | None:
        raw = self._request(
            "GET",
            "/sapi/v1/asset/transfer",
            {
                "type": transfer_type,
                "startTime": max(0, int(prepared_at.timestamp() * 1000) - 1_000),
                "endTime": int(now.timestamp() * 1000) + 1_000,
                "current": 1,
                "size": 100,
            },
        )
        rows = raw.get("rows") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            _reject(
                "BINANCE_CAPITAL_RESPONSE_INVALID",
                "universal transfer history response is invalid",
            )
        matches = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                observed_amount = Decimal(str(row.get("amount")))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if (
                row.get("asset") == SUPPORTED_ASSET
                and row.get("type") == transfer_type
                and observed_amount == amount
            ):
                matches.append(row)
        if len(matches) > 1:
            _reject(
                "BINANCE_INTERNAL_TRANSFER_AMBIGUOUS",
                "more than one matching Binance internal transfer was found in the frozen window",
            )
        if not matches:
            return None
        row = matches[0]
        return {
            "type": transfer_type,
            "asset": SUPPORTED_ASSET,
            "amount": str(amount),
            "status": str(row.get("status", "UNKNOWN")).upper(),
            "tranId": row.get("tranId"),
            "timestamp": row.get("timestamp"),
        }

    def _probe_universal_transfer_access(self, *, transfer_type: str, now: datetime) -> None:
        """Probe the exact read-only Universal Transfer endpoint used by this path.

        ``enableInternalTransfer`` is not a reliable capability signal across
        Binance account/API-key variants.  A bounded history read is the
        authoritative, side-effect-free permission check for the exact transfer
        direction that will later be submitted.
        """

        try:
            raw = self._request(
                "GET",
                "/sapi/v1/asset/transfer",
                {
                    "type": transfer_type,
                    "startTime": max(0, int(now.timestamp() * 1000) - 60_000),
                    "endTime": int(now.timestamp() * 1000) + 1_000,
                    "current": 1,
                    "size": 1,
                },
            )
        except DomainRejected as exc:
            if exc.code in {
                "BINANCE_CAPITAL_AUTHORIZATION_REJECTED",
                "BINANCE_CAPITAL_API_REJECTED",
            }:
                raise DomainRejected(
                    "BINANCE_INTERNAL_TRANSFER_PERMISSION_DISABLED",
                    "Binance rejected the Universal Transfer endpoint for this API key",
                ) from exc
            raise
        rows = raw.get("rows") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            _reject(
                "BINANCE_CAPITAL_RESPONSE_INVALID",
                "universal transfer capability probe returned an invalid response",
            )

    def _ensure_universal_transfer(
        self,
        *,
        transfer_type: str,
        amount: Decimal,
        prepared_at: datetime,
        now: datetime,
    ) -> dict[str, Any]:
        value = _decimal(amount, code="BINANCE_CAPITAL_AMOUNT_INVALID", field="amount")
        if value <= 0:
            _reject("BINANCE_CAPITAL_AMOUNT_INVALID", "internal transfer amount must be positive")
        existing = self._find_universal_transfer(
            transfer_type=transfer_type,
            amount=value,
            prepared_at=prepared_at,
            now=now,
        )
        if existing is not None:
            if existing["status"] == "CONFIRMED":
                return existing
            raise DomainRejected(
                "BINANCE_INTERNAL_TRANSFER_PENDING",
                "A matching Binance internal transfer already exists and must be reconciled "
                "from read-only history before any new POST",
            )
        try:
            raw = self._request(
                "POST",
                "/sapi/v1/asset/transfer",
                {"type": transfer_type, "asset": SUPPORTED_ASSET, "amount": str(value)},
            )
        except DomainRejected as exc:
            if exc.code == "BINANCE_CAPITAL_API_UNAVAILABLE":
                raise DomainRejected(
                    "BINANCE_INTERNAL_TRANSFER_SUBMISSION_UNKNOWN",
                    "Binance internal transfer response was unavailable; reconcile history "
                    "before retrying",
                ) from exc
            raise
        if not isinstance(raw, dict) or raw.get("tranId") is None:
            _reject(
                "BINANCE_INTERNAL_TRANSFER_SUBMISSION_UNKNOWN",
                "Binance did not return an internal transfer id; reconcile history before retrying",
            )
        confirmed = self._find_universal_transfer(
            transfer_type=transfer_type,
            amount=value,
            prepared_at=prepared_at,
            now=now,
        )
        if confirmed is None or confirmed["status"] != "CONFIRMED":
            raise DomainRejected(
                "BINANCE_INTERNAL_TRANSFER_PENDING",
                "Binance accepted the internal transfer but its confirmed history is not "
                "visible yet",
            )
        return confirmed

    def complete_deposit_to_usdm(
        self, *, amount: Decimal, prepared_at: datetime, now: datetime
    ) -> dict[str, Any]:
        self._permissions()
        return self._ensure_universal_transfer(
            transfer_type=SPOT_TO_USDM,
            amount=amount,
            prepared_at=prepared_at,
            now=now,
        )

    def _network(self) -> tuple[dict[str, Any], dict[str, Any]]:
        raw = self._request("GET", "/sapi/v1/capital/config/getall")
        if not isinstance(raw, list):
            _reject("BINANCE_CAPITAL_RESPONSE_INVALID", "coin configuration response is invalid")
        coin = next(
            (
                item
                for item in raw
                if isinstance(item, dict) and item.get("coin") == SUPPORTED_ASSET
            ),
            None,
        )
        if coin is None:
            _reject(
                "BINANCE_CAPITAL_ASSET_UNAVAILABLE", "USDC is missing from Binance capital scope"
            )
        networks = coin.get("networkList")
        if not isinstance(networks, list):
            _reject("BINANCE_CAPITAL_RESPONSE_INVALID", "USDC network configuration is invalid")
        network = next(
            (
                item
                for item in networks
                if isinstance(item, dict) and item.get("network") == SUPPORTED_NETWORK
            ),
            None,
        )
        if network is None:
            _reject(
                "BINANCE_CAPITAL_NETWORK_UNAVAILABLE",
                "USDC on Arbitrum is missing from Binance capital scope",
            )
        if network.get("busy") is True:
            _reject("BINANCE_CAPITAL_NETWORK_BUSY", "Binance reports USDC Arbitrum as busy")
        return coin, network

    def prepare_deposit(
        self,
        *,
        expected_address: str,
        amount: Decimal,
        source_address: str,
        now: datetime,
    ) -> dict[str, Any]:
        destination = _evm_address(expected_address, field="configured Binance deposit address")
        source = _evm_address(source_address, field="authorized source wallet")
        permissions = self._permissions()
        self._probe_universal_transfer_access(transfer_type=SPOT_TO_USDM, now=now)
        _, network = self._network()
        if network.get("depositEnable") is not True:
            _reject("BINANCE_CAPITAL_DEPOSIT_DISABLED", "USDC Arbitrum deposits are disabled")
        raw = self._request(
            "GET",
            "/sapi/v1/capital/deposit/address",
            {"coin": SUPPORTED_ASSET, "network": SUPPORTED_NETWORK},
        )
        if not isinstance(raw, dict) or not isinstance(raw.get("address"), str):
            _reject("BINANCE_CAPITAL_RESPONSE_INVALID", "deposit address response is invalid")
        observed = _evm_address(raw["address"], field="Binance deposit address")
        if observed != destination:
            _reject(
                "BINANCE_CAPITAL_DEPOSIT_ADDRESS_MISMATCH",
                "live Binance deposit address does not match the frozen configured address",
            )
        value = _decimal(amount, code="BINANCE_CAPITAL_AMOUNT_INVALID", field="amount")
        if value <= 0:
            _reject("BINANCE_CAPITAL_AMOUNT_INVALID", "deposit amount must be positive")
        return {
            "kind": "BINANCE_ARBITRUM_DEPOSIT_PREFLIGHT",
            "asset": SUPPORTED_ASSET,
            "network": SUPPORTED_NETWORK,
            "from": source,
            "destination": destination,
            "amount": str(value),
            "tag": raw.get("tag") or None,
            "readPermission": permissions.get("enableReading") is True,
            "depositEnabled": True,
            "preparedAt": now.astimezone(UTC).isoformat(),
            "expiresAt": (now + timedelta(minutes=5)).astimezone(UTC).isoformat(),
            "signing": False,
            "broadcast": False,
            "walletBoundary": "HUMAN_CONTROLLED_WALLET_OR_MULTISIG",
        }

    def prepare_withdrawal(
        self,
        *,
        destination: str,
        amount: Decimal,
        max_fee: Decimal,
        operation_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        target = _evm_address(destination, field="withdrawal destination")
        permissions = self._permissions()
        if permissions.get("ipRestrict") is not True:
            _reject(
                "BINANCE_CAPITAL_IP_RESTRICTION_REQUIRED",
                "dedicated withdrawal API key must be restricted to approved IPs",
            )
        if permissions.get("enableWithdrawals") is not True:
            _reject(
                "BINANCE_CAPITAL_WITHDRAW_PERMISSION_DISABLED",
                "dedicated Binance API key does not have withdrawal permission",
            )
        self._probe_universal_transfer_access(transfer_type=USDM_TO_SPOT, now=now)
        coin, network = self._network()
        if network.get("withdrawEnable") is not True:
            _reject("BINANCE_CAPITAL_WITHDRAW_DISABLED", "USDC Arbitrum withdrawals are disabled")
        if network.get("withdrawTag") is True:
            _reject("BINANCE_CAPITAL_TAG_UNSUPPORTED", "fixed EVM path cannot require a tag")
        addresses = self._request("GET", "/sapi/v1/capital/withdraw/address/list")
        if not isinstance(addresses, list):
            _reject("BINANCE_CAPITAL_RESPONSE_INVALID", "withdraw address response is invalid")
        allowlisted = any(
            isinstance(item, dict)
            and str(item.get("address", "")).lower() == target
            and item.get("coin") == SUPPORTED_ASSET
            and item.get("network") == SUPPORTED_NETWORK
            and item.get("whiteStatus") is True
            for item in addresses
        )
        if not allowlisted:
            _reject(
                "BINANCE_CAPITAL_DESTINATION_NOT_ALLOWLISTED",
                "destination is not an active USDC Arbitrum withdrawal allowlist entry",
            )
        questionnaire = self._request("GET", "/sapi/v1/localentity/questionnaire-requirements")
        if questionnaire not in (None, "NIL", {}, {"questionnaireCountryCode": "NIL"}):
            _reject(
                "BINANCE_CAPITAL_TRAVEL_RULE_REQUIRED",
                "this Binance account requires a Travel Rule workflow and cannot use "
                "the fixed endpoint",
            )
        value = _decimal(amount, code="BINANCE_CAPITAL_AMOUNT_INVALID", field="amount")
        fee = _decimal(
            network.get("withdrawFee"), code="BINANCE_CAPITAL_RESPONSE_INVALID", field="fee"
        )
        minimum = _decimal(
            network.get("withdrawMin"), code="BINANCE_CAPITAL_RESPONSE_INVALID", field="minimum"
        )
        maximum = _decimal(
            network.get("withdrawMax"), code="BINANCE_CAPITAL_RESPONSE_INVALID", field="maximum"
        )
        available = _decimal(
            coin.get("free"), code="BINANCE_CAPITAL_RESPONSE_INVALID", field="balance"
        )
        fee_limit = _decimal(max_fee, code="BINANCE_CAPITAL_FEE_LIMIT_INVALID", field="fee limit")
        if value < minimum or value > maximum:
            _reject(
                "BINANCE_CAPITAL_AMOUNT_OUT_OF_RANGE", "amount is outside current network limits"
            )
        if fee > fee_limit or value <= fee:
            _reject(
                "BINANCE_CAPITAL_FEE_LIMIT_EXCEEDED", "current fee exceeds the frozen fee limit"
            )
        # Binance withdrawals are funded from Spot.  This product's trading capital
        # lives in USD-M Futures, so every frozen withdrawal first moves the exact
        # requested amount to Spot instead of silently consuming an unrelated Spot
        # balance.
        internal_transfer_amount = value
        quota = self._request("GET", "/sapi/v1/capital/withdraw/quota")
        if not isinstance(quota, dict):
            _reject("BINANCE_CAPITAL_RESPONSE_INVALID", "withdrawal quota response is invalid")
        remaining_quota = _decimal(
            quota.get("wdQuota"), code="BINANCE_CAPITAL_RESPONSE_INVALID", field="withdrawal quota"
        ) - _decimal(
            quota.get("usedWdQuota"),
            code="BINANCE_CAPITAL_RESPONSE_INVALID",
            field="used withdrawal quota",
        )
        if value > remaining_quota:
            _reject("BINANCE_CAPITAL_QUOTA_EXCEEDED", "current withdrawal quota is insufficient")
        return {
            "kind": "BINANCE_RESTRICTED_WITHDRAWAL_PREFLIGHT",
            "asset": SUPPORTED_ASSET,
            "network": SUPPORTED_NETWORK,
            "destination": target,
            "amount": str(value),
            "fee": str(fee),
            "minReceived": str(value - fee),
            "withdrawOrderId": operation_id,
            "ipRestricted": True,
            "withdrawPermission": True,
            "allowlisted": True,
            "travelRuleRequired": False,
            "spotAvailableObserved": str(available),
            "internalTransferType": (USDM_TO_SPOT if internal_transfer_amount > 0 else None),
            "internalTransferAmount": str(internal_transfer_amount),
            "preparedAt": now.astimezone(UTC).isoformat(),
            "expiresAt": (now + timedelta(minutes=2)).astimezone(UTC).isoformat(),
            "submission": False,
            "credentialMaterialIncluded": False,
        }

    def submit_withdrawal(self, artifact: dict[str, Any], *, now: datetime) -> dict[str, Any]:
        try:
            expires_at = datetime.fromisoformat(str(artifact["expiresAt"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainRejected(
                "BINANCE_CAPITAL_PREFLIGHT_INVALID", "stored Binance preflight is invalid"
            ) from exc
        if expires_at <= now:
            _reject(
                "BINANCE_CAPITAL_PREFLIGHT_EXPIRED",
                "Binance withdrawal preflight expired; re-read all live controls",
            )
        order_id = str(artifact.get("withdrawOrderId", ""))
        if not order_id:
            _reject("BINANCE_CAPITAL_PREFLIGHT_INVALID", "withdrawal idempotency key is missing")
        existing = self._request(
            "GET",
            "/sapi/v1/capital/withdraw/history",
            {"coin": SUPPORTED_ASSET, "withdrawOrderId": order_id},
        )
        if isinstance(existing, list) and existing:
            return self._normalize_withdrawal(existing[0], expected_order_id=order_id)
        try:
            prepared_at = datetime.fromisoformat(str(artifact["preparedAt"]))
            transfer_amount = Decimal(str(artifact.get("internalTransferAmount", "0")))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise DomainRejected(
                "BINANCE_CAPITAL_PREFLIGHT_INVALID", "stored internal transfer plan is invalid"
            ) from exc
        internal_transfer = None
        if transfer_amount > 0:
            internal_transfer = self._ensure_universal_transfer(
                transfer_type=USDM_TO_SPOT,
                amount=transfer_amount,
                prepared_at=prepared_at,
                now=now,
            )
            expected_spot = Decimal(str(artifact.get("spotAvailableObserved", "0"))) + Decimal(
                str(artifact["amount"])
            )
            if self._spot_available() < expected_spot:
                raise DomainRejected(
                    "BINANCE_INTERNAL_TRANSFER_PENDING",
                    "USD-M Futures to Spot transfer is not reflected in the withdrawable "
                    "balance yet",
                )
        raw = self._request(
            "POST",
            "/sapi/v1/capital/withdraw/apply",
            {
                "coin": SUPPORTED_ASSET,
                "network": SUPPORTED_NETWORK,
                "address": artifact["destination"],
                "amount": artifact["amount"],
                "withdrawOrderId": order_id,
                "walletType": 0,
            },
        )
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            _reject(
                "BINANCE_CAPITAL_SUBMISSION_UNKNOWN",
                "Binance did not return a withdrawal id; query the fixed order id before retrying",
            )
        return {
            "withdrawalId": raw["id"],
            "withdrawOrderId": order_id,
            "status": "SUBMITTED",
            "submittedAt": now.astimezone(UTC).isoformat(),
            "internalTransfer": internal_transfer,
        }

    def _normalize_withdrawal(
        self, raw: object, *, expected_order_id: str, expected_destination: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(raw, dict) or raw.get("withdrawOrderId") != expected_order_id:
            _reject("BINANCE_CAPITAL_RECEIPT_MISMATCH", "withdrawal order id does not match")
        destination = str(raw.get("address", "")).lower()
        if expected_destination is not None and destination != expected_destination.lower():
            _reject("BINANCE_CAPITAL_RECEIPT_MISMATCH", "withdrawal destination does not match")
        if raw.get("coin") != SUPPORTED_ASSET or raw.get("network") != SUPPORTED_NETWORK:
            _reject(
                "BINANCE_CAPITAL_RECEIPT_MISMATCH", "withdrawal asset or network does not match"
            )
        status = int(raw.get("status", -1))
        return {
            "withdrawalId": raw.get("id"),
            "withdrawOrderId": expected_order_id,
            "statusCode": status,
            "status": "CONFIRMED" if status == SUCCESSFUL_WITHDRAWAL_STATUS else "PENDING",
            "transactionHash": raw.get("txId") or None,
            "amount": str(raw.get("amount")),
            "fee": str(raw.get("transactionFee")),
            "destination": destination,
            "network": SUPPORTED_NETWORK,
            "asset": SUPPORTED_ASSET,
        }

    def verify_withdrawal(
        self, *, order_id: str, destination: str, amount: Decimal
    ) -> dict[str, Any]:
        raw = self._request(
            "GET",
            "/sapi/v1/capital/withdraw/history",
            {"coin": SUPPORTED_ASSET, "withdrawOrderId": order_id},
            priority="LOW",
        )
        if not isinstance(raw, list) or len(raw) != 1:
            _reject("BINANCE_CAPITAL_RECEIPT_NOT_FOUND", "exact Binance withdrawal was not found")
        result = self._normalize_withdrawal(
            raw[0], expected_order_id=order_id, expected_destination=destination
        )
        received_amount = _decimal(
            result["amount"], code="BINANCE_CAPITAL_RECEIPT_MISMATCH", field="withdrawal amount"
        )
        fee = _decimal(
            result["fee"], code="BINANCE_CAPITAL_RECEIPT_MISMATCH", field="withdrawal fee"
        )
        if received_amount <= 0 or received_amount + fee != Decimal(amount):
            _reject("BINANCE_CAPITAL_RECEIPT_MISMATCH", "withdrawal amount does not match")
        if result["status"] != "CONFIRMED" or not result["transactionHash"]:
            _reject("BINANCE_CAPITAL_WITHDRAWAL_PENDING", "withdrawal is not chain-confirmed")
        if TX_HASH.fullmatch(str(result["transactionHash"])) is None:
            _reject("BINANCE_CAPITAL_RECEIPT_MISMATCH", "withdrawal transaction hash is invalid")
        return result

    def verify_deposit(
        self, *, transaction_hash: str, destination: str, amount: Decimal
    ) -> dict[str, Any]:
        if TX_HASH.fullmatch(transaction_hash) is None:
            _reject("BINANCE_CAPITAL_RECEIPT_MISMATCH", "deposit transaction hash is invalid")
        target = _evm_address(destination, field="Binance deposit address")
        raw = self._request(
            "GET",
            "/sapi/v1/capital/deposit/hisrec",
            {"coin": SUPPORTED_ASSET, "txId": transaction_hash},
            priority="LOW",
        )
        if not isinstance(raw, list) or len(raw) != 1:
            _reject("BINANCE_CAPITAL_RECEIPT_NOT_FOUND", "exact Binance deposit was not found")
        item = raw[0]
        if (
            not isinstance(item, dict)
            or item.get("coin") != SUPPORTED_ASSET
            or item.get("network") != SUPPORTED_NETWORK
            or str(item.get("address", "")).lower() != target
            or str(item.get("txId", "")).lower() != transaction_hash.lower()
            or Decimal(str(item.get("amount"))) != Decimal(amount)
        ):
            _reject("BINANCE_CAPITAL_RECEIPT_MISMATCH", "Binance deposit receipt does not match")
        if int(item.get("status", -1)) != SUCCESSFUL_DEPOSIT_STATUS:
            _reject("BINANCE_CAPITAL_DEPOSIT_PENDING", "Binance deposit is not credited")
        return {
            "depositId": item.get("id"),
            "status": "CONFIRMED",
            "transactionHash": transaction_hash.lower(),
            "amount": str(amount),
            "destination": target,
            "network": SUPPORTED_NETWORK,
            "asset": SUPPORTED_ASSET,
            "confirmations": item.get("confirmTimes"),
            "completedAt": item.get("completeTime"),
        }
