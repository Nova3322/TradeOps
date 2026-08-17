from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn, cast

import requests
from hyperliquid.utils.signing import (  # type: ignore[import-untyped]
    WITHDRAW_SIGN_TYPES,
    user_signed_payload,
)

from trading_control_plane.domain import DomainRejected

JsonObject = dict[str, Any]
InfoFetcher = Callable[[str, JsonObject, float], Any]
RpcFetcher = Callable[[str, str, list[object], float], Any]

ARBITRUM_CHAIN_ID = 42161
ARBITRUM_SIGNATURE_CHAIN_ID = "0xa4b1"
HYPERLIQUID_SIGNATURE_CHAIN_ID = "0x66eee"
HYPERLIQUID_BRIDGE2_ADDRESS = "0x2df1c51e09aecf9cacb7bc98cb1742757f163df7"
ARBITRUM_NATIVE_USDC_ADDRESS = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
USDC_DECIMALS = 6
ERC20_TRANSFER_SELECTOR = "a9059cbb"
ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
OFFICIAL_API_HOSTS = frozenset({"api.hyperliquid.xyz", "api.hyperliquid-testnet.xyz"})
ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")
HASH_PATTERN = re.compile(r"^0x[0-9a-fA-F]{64}$")
MINIMUM_DEPOSIT = Decimal("5")
CURRENT_WITHDRAWAL_FEE = Decimal("1")


def _reject(code: str, detail: str) -> NoReturn:
    raise DomainRejected(code, detail)


def _address(value: str, name: str) -> str:
    if ADDRESS_PATTERN.fullmatch(value) is None:
        _reject("HYPERLIQUID_CAPITAL_SCOPE_INVALID", f"{name} is not a valid EVM address")
    return value.lower()


def _amount(value: str | Decimal) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DomainRejected(
            "HYPERLIQUID_CAPITAL_AMOUNT_INVALID", "amount is not a decimal value"
        ) from exc
    if not amount.is_finite() or amount <= 0:
        _reject("HYPERLIQUID_CAPITAL_AMOUNT_INVALID", "amount must be positive")
    raw = amount * (Decimal(10) ** USDC_DECIMALS)
    if raw != raw.to_integral_value():
        _reject(
            "HYPERLIQUID_CAPITAL_AMOUNT_PRECISION_INVALID",
            "Arbitrum native USDC supports at most 6 decimal places",
        )
    return amount


def _raw_usdc(value: Decimal) -> int:
    return int(value * (Decimal(10) ** USDC_DECIMALS))


def _erc20_transfer_data(destination: str, raw_amount: int) -> str:
    encoded_address = destination[2:].lower().rjust(64, "0")
    encoded_amount = hex(raw_amount)[2:].rjust(64, "0")
    return f"0x{ERC20_TRANSFER_SELECTOR}{encoded_address}{encoded_amount}"


def _require_official_api(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_API_HOSTS:
        _reject(
            "HYPERLIQUID_CAPITAL_API_UNTRUSTED",
            "capital preflight requires an official Hyperliquid API host",
        )
    return base_url.rstrip("/")


def _require_rpc_url(rpc_url: str) -> str:
    parsed = urllib.parse.urlparse(rpc_url)
    if parsed.scheme != "https" or not parsed.hostname:
        _reject(
            "ARBITRUM_RPC_UNTRUSTED",
            "receipt verification requires an explicit trusted HTTPS Arbitrum RPC",
        )
    return rpc_url


def _default_info_fetcher(url: str, payload: JsonObject, timeout: float) -> Any:
    request = urllib.request.Request(  # noqa: S310
        url,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read())
    except (json.JSONDecodeError, urllib.error.URLError, TimeoutError) as exc:
        raise DomainRejected(
            "HYPERLIQUID_CAPITAL_PREFLIGHT_UNAVAILABLE",
            "Hyperliquid capital preflight did not return a valid bounded response",
        ) from exc


def _default_rpc_fetcher(rpc_url: str, method: str, params: list[object], timeout: float) -> Any:
    try:
        response = requests.post(
            rpc_url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": cast(Any, list(params)),
            },
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, requests.JSONDecodeError) as exc:
        raise DomainRejected(
            "ARBITRUM_RECEIPT_UNAVAILABLE",
            "trusted Arbitrum RPC did not return a valid bounded response",
        ) from exc
    if not isinstance(body, dict) or body.get("error") is not None:
        _reject("ARBITRUM_RECEIPT_UNAVAILABLE", "trusted Arbitrum RPC rejected the query")
    return body.get("result")


class HyperliquidCapitalGateway:
    """Signing-free Hyperliquid/Arbitrum capital boundary.

    This component performs current read-only checks, builds fixed requests and
    verifies public receipts. It never accepts a private key or a wallet signature.
    """

    def __init__(
        self,
        *,
        info_fetcher: InfoFetcher | None = None,
        rpc_fetcher: RpcFetcher | None = None,
        timeout_seconds: float = 5,
    ) -> None:
        self._info_fetcher = info_fetcher or _default_info_fetcher
        self._rpc_fetcher = rpc_fetcher or _default_rpc_fetcher
        self._timeout_seconds = min(15.0, max(1.0, timeout_seconds))

    def _info(self, base_url: str, payload: JsonObject) -> Any:
        official = _require_official_api(base_url)
        return self._info_fetcher(f"{official}/info", payload, self._timeout_seconds)

    def _agent_relationship(
        self,
        *,
        base_url: str,
        main_account: str,
        api_wallet_address: str | None,
    ) -> JsonObject:
        if api_wallet_address is None:
            return {
                "configured": False,
                "authorized": False,
                "capability": "MAIN_OR_MULTISIG_WALLET_REQUIRED",
            }
        agent = _address(api_wallet_address, "Hyperliquid API wallet")
        raw = self._info(base_url, {"type": "userRole", "user": agent})
        if not isinstance(raw, dict) or raw.get("role") != "agent":
            _reject(
                "HYPERLIQUID_AGENT_NOT_AUTHORIZED",
                "configured API wallet is not currently authorized as an agent",
            )
        data = raw.get("data")
        owner = data.get("user") if isinstance(data, dict) else None
        if not isinstance(owner, str) or _address(owner, "agent owner") != main_account:
            _reject(
                "HYPERLIQUID_AGENT_SCOPE_MISMATCH",
                "configured API wallet belongs to a different Hyperliquid account",
            )
        return {
            "configured": True,
            "authorized": True,
            "agent_address": agent,
            # withdraw3 and bridge deposits are user/onchain-wallet signed actions.
            "capability": "TRADING_AGENT_ONLY_MANUAL_WALLET_FALLBACK",
        }

    def prepare_deposit(
        self,
        *,
        base_url: str,
        main_account: str,
        api_wallet_address: str | None,
        owned_arbitrum_address: str,
        bridge_address: str,
        amount: str | Decimal,
        now: datetime,
    ) -> JsonObject:
        _require_official_api(base_url)
        main = _address(main_account, "Hyperliquid main account")
        sender = _address(owned_arbitrum_address, "authorized Arbitrum wallet")
        bridge = _address(bridge_address, "Hyperliquid Bridge2")
        if bridge != HYPERLIQUID_BRIDGE2_ADDRESS:
            _reject(
                "HYPERLIQUID_BRIDGE_UNTRUSTED",
                "configured bridge does not match the official Arbitrum Bridge2 deployment",
            )
        if sender != main:
            _reject(
                "HYPERLIQUID_DEPOSIT_ACCOUNT_MISMATCH",
                "Bridge2 credits the sending wallet; the authorized wallet must be "
                "the Hyperliquid account",
            )
        value = _amount(amount)
        if value < MINIMUM_DEPOSIT:
            _reject(
                "HYPERLIQUID_DEPOSIT_BELOW_MINIMUM",
                "Hyperliquid Arbitrum deposits require at least 5 USDC",
            )
        relationship = self._agent_relationship(
            base_url=base_url,
            main_account=main,
            api_wallet_address=api_wallet_address,
        )
        raw = _raw_usdc(value)
        return {
            "kind": "HYPERLIQUID_ARBITRUM_DEPOSIT_UNSIGNED_TRANSACTION",
            "chainId": ARBITRUM_CHAIN_ID,
            "network": "ARBITRUM",
            "from": sender,
            "to": ARBITRUM_NATIVE_USDC_ADDRESS,
            "token": ARBITRUM_NATIVE_USDC_ADDRESS,
            "bridge": bridge,
            "method": "ERC20.transfer",
            "amount": str(value),
            "amountRaw": str(raw),
            "value": "0",
            "data": _erc20_transfer_data(bridge, raw),
            "expectedCreditAccount": main,
            "agentWallet": relationship,
            "walletBoundary": "MAIN_OR_VALID_MULTISIG",
            "fallbackReason": "ONCHAIN_DEPOSIT_REQUIRES_ACCOUNT_WALLET_SIGNATURE",
            "preparedAt": now.astimezone(UTC).isoformat(),
            "expiresAt": (now + timedelta(minutes=5)).astimezone(UTC).isoformat(),
            "signing": False,
            "broadcast": False,
        }

    def prepare_arbitrum_usdc_transfer(
        self,
        *,
        sender: str,
        destination: str,
        amount: str | Decimal,
        now: datetime,
    ) -> JsonObject:
        """Build one exact native-USDC transfer for an independent browser wallet."""

        source = _address(sender, "authorized Arbitrum sender")
        target = _address(destination, "authorized Arbitrum destination")
        value = _amount(amount)
        raw = _raw_usdc(value)
        return {
            "kind": "ARBITRUM_USDC_UNSIGNED_TRANSACTION",
            "chainId": ARBITRUM_CHAIN_ID,
            "network": "ARBITRUM",
            "from": source,
            "to": ARBITRUM_NATIVE_USDC_ADDRESS,
            "token": ARBITRUM_NATIVE_USDC_ADDRESS,
            "recipient": target,
            "method": "ERC20.transfer",
            "amount": str(value),
            "amountRaw": str(raw),
            "value": "0",
            "data": _erc20_transfer_data(target, raw),
            "preparedAt": now.astimezone(UTC).isoformat(),
            "expiresAt": (now + timedelta(minutes=5)).astimezone(UTC).isoformat(),
            "signing": False,
            "broadcast": False,
        }

    def prepare_withdrawal(
        self,
        *,
        base_url: str,
        main_account: str,
        api_wallet_address: str | None,
        destination: str,
        amount: str | Decimal,
        max_fee: str | Decimal | None,
        now: datetime,
    ) -> JsonObject:
        official = _require_official_api(base_url)
        main = _address(main_account, "Hyperliquid main account")
        target = _address(destination, "authorized Arbitrum destination")
        value = _amount(amount)
        try:
            fee_limit = None if max_fee is None else Decimal(str(max_fee))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise DomainRejected(
                "HYPERLIQUID_CAPITAL_AMOUNT_INVALID", "maximum fee is invalid"
            ) from exc
        if fee_limit is not None and (not fee_limit.is_finite() or fee_limit < 0):
            _reject("HYPERLIQUID_CAPITAL_AMOUNT_INVALID", "maximum fee cannot be negative")
        if fee_limit is not None and fee_limit < CURRENT_WITHDRAWAL_FEE:
            _reject(
                "HYPERLIQUID_WITHDRAWAL_FEE_LIMIT_TOO_LOW",
                "configured fee limit is below the current Hyperliquid withdrawal fee",
            )
        state = self._info(base_url, {"type": "clearinghouseState", "user": main})
        margin = state.get("withdrawable") if isinstance(state, dict) else None
        try:
            withdrawable = Decimal(str(margin))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise DomainRejected(
                "HYPERLIQUID_WITHDRAWABLE_UNKNOWN",
                "Hyperliquid did not return a valid current withdrawable balance",
            ) from exc
        if withdrawable < value + CURRENT_WITHDRAWAL_FEE:
            _reject(
                "HYPERLIQUID_WITHDRAWABLE_INSUFFICIENT",
                "current Hyperliquid withdrawable balance does not cover amount plus fee",
            )
        relationship = self._agent_relationship(
            base_url=base_url,
            main_account=main,
            api_wallet_address=api_wallet_address,
        )
        nonce = int(now.timestamp() * 1000)
        action = {
            "type": "withdraw3",
            "hyperliquidChain": "Mainnet" if official.endswith("hyperliquid.xyz") else "Testnet",
            "signatureChainId": HYPERLIQUID_SIGNATURE_CHAIN_ID,
            "amount": str(value),
            "time": nonce,
            "destination": target,
        }
        typed_data = user_signed_payload(
            "HyperliquidTransaction:Withdraw", WITHDRAW_SIGN_TYPES, action
        )
        return {
            "kind": "HYPERLIQUID_WITHDRAW3_TYPED_REQUEST",
            "account": main,
            "destination": target,
            "amount": str(value),
            "expectedFee": str(CURRENT_WITHDRAWAL_FEE),
            "maxFee": None if fee_limit is None else str(fee_limit),
            "withdrawableObserved": str(withdrawable),
            "nonce": nonce,
            "action": action,
            "typedData": typed_data,
            "exchangeEndpoint": f"{official}/exchange",
            "exchangeRequestTemplate": {
                "action": action,
                "nonce": nonce,
                "signature": None,
            },
            "agentWallet": relationship,
            "walletBoundary": "MAIN_OR_VALID_MULTISIG",
            "fallbackReason": "WITHDRAW3_REQUIRES_USER_SIGNED_ACTION",
            "preparedAt": now.astimezone(UTC).isoformat(),
            "expiresAt": (now + timedelta(minutes=5)).astimezone(UTC).isoformat(),
            "signing": False,
            "broadcast": False,
        }

    def verify_hyperliquid_ledger(
        self,
        *,
        base_url: str,
        main_account: str,
        receipt_kind: str,
        amount: str | Decimal,
        prepared_at: datetime,
        nonce: int | None = None,
        action_hash: str | None = None,
        now: datetime,
    ) -> JsonObject:
        main = _address(main_account, "Hyperliquid main account")
        value = _amount(amount)
        if action_hash is not None and HASH_PATTERN.fullmatch(action_hash) is None:
            _reject("HYPERLIQUID_RECEIPT_INVALID", "action hash is invalid")
        raw = self._info(
            base_url,
            {
                "type": "userNonFundingLedgerUpdates",
                "user": main,
                "startTime": max(0, int(prepared_at.timestamp() * 1000) - 60_000),
                "endTime": int(now.timestamp() * 1000),
            },
        )
        if not isinstance(raw, list):
            _reject("HYPERLIQUID_RECEIPT_INVALID", "ledger response is invalid")
        expected_type = {
            "DEPOSIT": "deposit",
            "WITHDRAWAL": "withdraw",
            "CLASS_TRANSFER": "accountClassTransfer",
        }.get(receipt_kind)
        if expected_type is None:
            _reject("HYPERLIQUID_RECEIPT_INVALID", "receipt kind is unsupported")
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            delta = entry.get("delta")
            if not isinstance(delta, dict) or delta.get("type") != expected_type:
                continue
            if (
                action_hash is not None
                and str(entry.get("hash", "")).lower() != action_hash.lower()
            ):
                continue
            try:
                observed_amount = Decimal(str(delta.get("usdc")))
                raw_nonce = delta.get("nonce")
                raw_time = entry.get("time")
                observed_nonce = int(str(raw_nonce)) if raw_nonce is not None else None
                observed_time = int(str(raw_time))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if observed_amount != value:
                continue
            if nonce is not None and observed_nonce != nonce:
                continue
            return {
                "kind": f"HYPERLIQUID_{expected_type.upper()}_LEDGER_RECEIPT",
                "hash": str(entry.get("hash", "")).lower(),
                "time": observed_time,
                "amount": str(observed_amount),
                "nonce": observed_nonce,
                "fee": None if delta.get("fee") is None else str(delta.get("fee")),
                "verifiedAt": now.astimezone(UTC).isoformat(),
            }
        _reject(
            "HYPERLIQUID_RECEIPT_NOT_CONFIRMED",
            "no matching current Hyperliquid ledger receipt was found",
        )

    def verify_arbitrum_usdc_transfer(
        self,
        *,
        rpc_url: str,
        transaction_hash: str,
        sender: str,
        recipient: str,
        amount: str | Decimal,
        min_confirmations: int,
        expected_token: str = ARBITRUM_NATIVE_USDC_ADDRESS,
    ) -> JsonObject:
        trusted_rpc = _require_rpc_url(rpc_url)
        if HASH_PATTERN.fullmatch(transaction_hash) is None:
            _reject("ARBITRUM_RECEIPT_INVALID", "transaction hash is invalid")
        source = _address(sender, "transaction sender")
        target = _address(recipient, "USDC recipient")
        token = _address(expected_token, "USDC token")
        value = _amount(amount)
        receipt = self._rpc_fetcher(
            trusted_rpc,
            "eth_getTransactionReceipt",
            [transaction_hash],
            self._timeout_seconds,
        )
        transaction = self._rpc_fetcher(
            trusted_rpc,
            "eth_getTransactionByHash",
            [transaction_hash],
            self._timeout_seconds,
        )
        latest = self._rpc_fetcher(trusted_rpc, "eth_blockNumber", [], self._timeout_seconds)
        if not isinstance(receipt, dict) or not isinstance(transaction, dict):
            _reject("ARBITRUM_RECEIPT_NOT_CONFIRMED", "transaction is not yet confirmed")
        try:
            status = int(str(receipt.get("status")), 16)
            block_number = int(str(receipt.get("blockNumber")), 16)
            latest_block = int(str(latest), 16)
        except (TypeError, ValueError) as exc:
            raise DomainRejected(
                "ARBITRUM_RECEIPT_INVALID", "receipt block metadata is invalid"
            ) from exc
        confirmations = latest_block - block_number + 1
        expected_data = _erc20_transfer_data(target, _raw_usdc(value)).lower()
        if (
            status != 1
            or str(transaction.get("from", "")).lower() != source
            or str(transaction.get("to", "")).lower() != token
            or str(transaction.get("input", "")).lower() != expected_data
            or confirmations < min_confirmations
        ):
            _reject(
                "ARBITRUM_RECEIPT_MISMATCH",
                "Arbitrum receipt does not match sender, token, recipient, amount or confirmations",
            )
        return {
            "kind": "ARBITRUM_USDC_TRANSFER_RECEIPT",
            "transactionHash": transaction_hash.lower(),
            "blockNumber": block_number,
            "confirmations": confirmations,
            "sender": source,
            "recipient": target,
            "token": token,
            "amount": str(value),
        }

    def verify_arbitrum_usdc_credit(
        self,
        *,
        rpc_url: str,
        transaction_hash: str,
        sender: str,
        recipient: str,
        amount: str | Decimal,
        min_confirmations: int,
        expected_token: str = ARBITRUM_NATIVE_USDC_ADDRESS,
    ) -> JsonObject:
        trusted_rpc = _require_rpc_url(rpc_url)
        if HASH_PATTERN.fullmatch(transaction_hash) is None:
            _reject("ARBITRUM_RECEIPT_INVALID", "transaction hash is invalid")
        source = _address(sender, "USDC source")
        target = _address(recipient, "USDC recipient")
        token = _address(expected_token, "USDC token")
        value = _amount(amount)
        receipt = self._rpc_fetcher(
            trusted_rpc,
            "eth_getTransactionReceipt",
            [transaction_hash],
            self._timeout_seconds,
        )
        latest = self._rpc_fetcher(trusted_rpc, "eth_blockNumber", [], self._timeout_seconds)
        if not isinstance(receipt, dict):
            _reject("ARBITRUM_RECEIPT_NOT_CONFIRMED", "transaction is not yet confirmed")
        try:
            status = int(str(receipt.get("status")), 16)
            block_number = int(str(receipt.get("blockNumber")), 16)
            latest_block = int(str(latest), 16)
        except (TypeError, ValueError) as exc:
            raise DomainRejected(
                "ARBITRUM_RECEIPT_INVALID", "receipt block metadata is invalid"
            ) from exc
        expected_source_topic = f"0x{source[2:].rjust(64, '0')}"
        expected_target_topic = f"0x{target[2:].rjust(64, '0')}"
        expected_raw = _raw_usdc(value)
        matched = False
        for log in receipt.get("logs", []):
            if not isinstance(log, dict):
                continue
            topics = log.get("topics")
            if not isinstance(topics, list) or len(topics) < 3:
                continue
            try:
                raw_value = int(str(log.get("data")), 16)
            except (TypeError, ValueError):
                continue
            if (
                str(log.get("address", "")).lower() == token
                and str(topics[0]).lower() == ERC20_TRANSFER_TOPIC
                and str(topics[1]).lower() == expected_source_topic
                and str(topics[2]).lower() == expected_target_topic
                and raw_value == expected_raw
            ):
                matched = True
                break
        confirmations = latest_block - block_number + 1
        if status != 1 or not matched or confirmations < min_confirmations:
            _reject(
                "ARBITRUM_RECEIPT_MISMATCH",
                "Arbitrum receipt does not contain the expected confirmed USDC credit",
            )
        return {
            "kind": "ARBITRUM_USDC_CREDIT_RECEIPT",
            "transactionHash": transaction_hash.lower(),
            "blockNumber": block_number,
            "confirmations": confirmations,
            "sender": source,
            "recipient": target,
            "token": token,
            "amount": str(value),
        }

    def find_arbitrum_usdc_credit(
        self,
        *,
        rpc_url: str,
        sender: str,
        recipient: str,
        amount: str | Decimal,
        prepared_at: datetime,
        min_confirmations: int,
        expected_token: str = ARBITRUM_NATIVE_USDC_ADDRESS,
    ) -> JsonObject:
        """Discover an exact Bridge2 credit when Hyperliquid does not expose its EVM tx hash."""

        trusted_rpc = _require_rpc_url(rpc_url)
        source = _address(sender, "USDC source")
        target = _address(recipient, "USDC recipient")
        token = _address(expected_token, "USDC token")
        value = _amount(amount)
        latest_raw = self._rpc_fetcher(
            trusted_rpc, "eth_blockNumber", [], self._timeout_seconds
        )
        latest_block_raw = self._rpc_fetcher(
            trusted_rpc, "eth_getBlockByNumber", ["latest", False], self._timeout_seconds
        )
        if not isinstance(latest_block_raw, dict):
            _reject("ARBITRUM_RECEIPT_UNAVAILABLE", "latest Arbitrum block is unavailable")
        try:
            latest = int(str(latest_raw), 16)
            latest_timestamp = int(str(latest_block_raw.get("timestamp")), 16)
        except (TypeError, ValueError) as exc:
            raise DomainRejected(
                "ARBITRUM_RECEIPT_INVALID", "latest Arbitrum block metadata is invalid"
            ) from exc
        elapsed_seconds = max(0, latest_timestamp - int(prepared_at.timestamp()))
        # Arbitrum blocks are sub-second.  Keep the query bounded to the operation
        # window while leaving ample margin for provider clock skew and finalization.
        lookback = min(60_000, max(4_000, elapsed_seconds * 8 + 2_000))
        expected_source_topic = f"0x{source[2:].rjust(64, '0')}"
        expected_target_topic = f"0x{target[2:].rjust(64, '0')}"
        logs = self._rpc_fetcher(
            trusted_rpc,
            "eth_getLogs",
            [
                {
                    "fromBlock": hex(max(0, latest - lookback)),
                    "toBlock": "latest",
                    "address": token,
                    "topics": [
                        ERC20_TRANSFER_TOPIC,
                        expected_source_topic,
                        expected_target_topic,
                    ],
                }
            ],
            self._timeout_seconds,
        )
        if not isinstance(logs, list):
            _reject("ARBITRUM_RECEIPT_UNAVAILABLE", "Arbitrum log query is unavailable")
        matches: list[str] = []
        expected_raw = _raw_usdc(value)
        for log in logs:
            if not isinstance(log, dict):
                continue
            try:
                observed_raw = int(str(log.get("data")), 16)
                block_number = int(str(log.get("blockNumber")), 16)
            except (TypeError, ValueError):
                continue
            tx_hash = str(log.get("transactionHash", "")).lower()
            if (
                observed_raw == expected_raw
                and HASH_PATTERN.fullmatch(tx_hash) is not None
                and latest - block_number + 1 >= min_confirmations
            ):
                matches.append(tx_hash)
        unique = list(dict.fromkeys(matches))
        if not unique:
            _reject("ARBITRUM_RECEIPT_NOT_CONFIRMED", "withdrawal is not yet chain-confirmed")
        if len(unique) > 1:
            _reject(
                "ARBITRUM_RECEIPT_MISMATCH",
                "multiple exact Bridge2 credits matched the frozen operation window",
            )
        return self.verify_arbitrum_usdc_credit(
            rpc_url=trusted_rpc,
            transaction_hash=unique[0],
            sender=source,
            recipient=target,
            amount=value,
            min_confirmations=min_confirmations,
            expected_token=token,
        )

    def verify_arbitrum_usdc_credit_from_any_sender(
        self,
        *,
        rpc_url: str,
        transaction_hash: str,
        recipient: str,
        amount: str | Decimal,
        min_confirmations: int,
        expected_token: str = ARBITRUM_NATIVE_USDC_ADDRESS,
    ) -> JsonObject:
        """Verify exchange-originated credit without trusting an unknown hot-wallet sender."""

        trusted_rpc = _require_rpc_url(rpc_url)
        if HASH_PATTERN.fullmatch(transaction_hash) is None:
            _reject("ARBITRUM_RECEIPT_INVALID", "transaction hash is invalid")
        target = _address(recipient, "USDC recipient")
        token = _address(expected_token, "USDC token")
        value = _amount(amount)
        receipt = self._rpc_fetcher(
            trusted_rpc,
            "eth_getTransactionReceipt",
            [transaction_hash],
            self._timeout_seconds,
        )
        latest = self._rpc_fetcher(trusted_rpc, "eth_blockNumber", [], self._timeout_seconds)
        if not isinstance(receipt, dict):
            _reject("ARBITRUM_RECEIPT_NOT_CONFIRMED", "transaction is not yet confirmed")
        try:
            status = int(str(receipt.get("status")), 16)
            block_number = int(str(receipt.get("blockNumber")), 16)
            latest_block = int(str(latest), 16)
        except (TypeError, ValueError) as exc:
            raise DomainRejected(
                "ARBITRUM_RECEIPT_INVALID", "receipt block metadata is invalid"
            ) from exc
        expected_target_topic = f"0x{target[2:].rjust(64, '0')}"
        expected_raw = _raw_usdc(value)
        sender: str | None = None
        for log in receipt.get("logs", []):
            if not isinstance(log, dict):
                continue
            topics = log.get("topics")
            if not isinstance(topics, list) or len(topics) < 3:
                continue
            try:
                raw_value = int(str(log.get("data")), 16)
            except (TypeError, ValueError):
                continue
            if (
                str(log.get("address", "")).lower() == token
                and str(topics[0]).lower() == ERC20_TRANSFER_TOPIC
                and str(topics[2]).lower() == expected_target_topic
                and raw_value == expected_raw
            ):
                source_topic = str(topics[1]).lower()
                sender = f"0x{source_topic[-40:]}"
                break
        confirmations = latest_block - block_number + 1
        if status != 1 or sender is None or confirmations < min_confirmations:
            _reject(
                "ARBITRUM_RECEIPT_MISMATCH",
                "Arbitrum receipt does not contain the expected confirmed USDC credit",
            )
        return {
            "kind": "ARBITRUM_USDC_EXCHANGE_CREDIT_RECEIPT",
            "transactionHash": transaction_hash.lower(),
            "blockNumber": block_number,
            "confirmations": confirmations,
            "sender": sender,
            "recipient": target,
            "token": token,
            "amount": str(value),
        }
