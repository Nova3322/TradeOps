from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from trading_control_plane.domain import DomainRejected

SUPPORTED_NOTILT_CHAINS = {
    1: "ETHEREUM",
    56: "BSC",
    42161: "ARBITRUM",
}

JsonObject = dict[str, Any]
GatewayExecutor = Callable[[JsonObject], JsonObject]
PriceFetcher = Callable[[str, float], JsonObject]
USD_STABLE_ASSETS = frozenset({"USD", "USDC", "USDT", "USDT0"})
NATIVE_PRICE_SYMBOLS = {
    "ETH": "ETHUSDT",
    "BNB": "BNBUSDT",
}


@dataclass(frozen=True, slots=True)
class NoTiltAssetBudget:
    chain_id: int
    chain: str
    block_number: int
    block_timestamp: datetime
    vault: str
    agent: str
    owner: str
    asset_address: str
    asset: str
    decimals: int
    native: bool
    is_official_vault: bool
    is_active_whitelist: bool
    assigned_whitelist_vault: str
    balance: Decimal
    max_release_net: Decimal
    pending_net: Decimal
    panic_locked: bool
    daily_release_rate: Decimal
    daily_fee_rate: Decimal


@dataclass(frozen=True, slots=True)
class NoTiltVaultSnapshot:
    chain_id: int
    chain: str
    vault: str
    agent: str
    budgets: tuple[NoTiltAssetBudget, ...]


@dataclass(frozen=True, slots=True)
class NoTiltUnsignedTransaction:
    chain_id: int
    to: str
    data: str
    value: int
    contract: str
    function_name: str
    summary: str

    @classmethod
    def from_json(cls, value: JsonObject) -> NoTiltUnsignedTransaction:
        try:
            chain_id = int(value["chainId"])
            target = str(value["to"])
            data = str(value["data"])
            native_value = int(value["value"])
            contract = str(value["contract"])
            function_name = str(value["functionName"])
            summary = str(value["summary"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainRejected(
                "NOTILT_RESPONSE_INVALID",
                "NoTilt gateway returned an invalid unsigned transaction",
            ) from exc
        if (
            chain_id not in SUPPORTED_NOTILT_CHAINS
            or not target.startswith("0x")
            or len(target) != 42
            or not data.startswith("0x")
            or native_value < 0
            or contract not in {"vault", "erc20"}
            or function_name
            not in {
                "approve",
                "deposit",
                "requestWhitelistRelease",
                "executeWhitelistRelease",
                "cancelWhitelistRelease",
            }
        ):
            raise DomainRejected(
                "NOTILT_RESPONSE_INVALID",
                "NoTilt gateway returned an unsupported unsigned transaction",
            )
        return cls(
            chain_id,
            target,
            data,
            native_value,
            contract,
            function_name,
            summary,
        )

    def to_dict(self) -> JsonObject:
        return {
            "chain_id": self.chain_id,
            "to": self.to,
            "data": self.data,
            "value": str(self.value),
            "contract": self.contract,
            "function_name": self.function_name,
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class UsdValuation:
    price: Decimal
    value: Decimal
    observed_at: datetime


def _default_price_fetcher(url: str, timeout: float) -> JsonObject:
    request = urllib.request.Request(  # noqa: S310
        url,
        headers={"User-Agent": "trading-control-plane/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise DomainRejected(
            "NOTILT_VALUATION_UNAVAILABLE",
            "USD valuation price is unavailable",
        ) from exc
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DomainRejected(
            "NOTILT_VALUATION_INVALID",
            "USD valuation source returned invalid JSON",
        ) from exc
    if not isinstance(value, dict):
        raise DomainRejected(
            "NOTILT_VALUATION_INVALID",
            "USD valuation source returned an invalid response",
        )
    return value


class NoTiltUsdValuator:
    """Value the fixed NoTilt catalog without accepting caller-selected price endpoints."""

    def __init__(self, fetcher: PriceFetcher = _default_price_fetcher) -> None:
        self._fetcher = fetcher

    def value(self, asset: str, amount: Decimal, *, now: datetime) -> UsdValuation:
        if amount < 0:
            raise DomainRejected(
                "NOTILT_VALUATION_INVALID",
                "NoTilt balance cannot be negative",
            )
        normalized = asset.upper()
        if normalized in USD_STABLE_ASSETS:
            return UsdValuation(Decimal(1), amount, now)
        if amount == 0:
            return UsdValuation(Decimal(1), Decimal(0), now)
        symbol = NATIVE_PRICE_SYMBOLS.get(normalized)
        if symbol is None:
            raise DomainRejected(
                "NOTILT_VALUATION_UNSUPPORTED",
                "NoTilt asset has no approved USD valuation source",
            )
        query = urllib.parse.urlencode({"symbol": symbol})
        raw = self._fetcher(f"https://fapi.binance.com/fapi/v1/premiumIndex?{query}", 5.0)
        try:
            if raw.get("symbol") != symbol:
                raise ValueError
            price = Decimal(str(raw["markPrice"]))
            observed_at = datetime.fromtimestamp(int(raw["time"]) / 1_000, UTC)
        except (InvalidOperation, KeyError, OSError, OverflowError, TypeError, ValueError) as exc:
            raise DomainRejected(
                "NOTILT_VALUATION_INVALID",
                "USD valuation source returned invalid price facts",
            ) from exc
        if price <= 0 or observed_at > now + timedelta(seconds=30):
            raise DomainRejected(
                "NOTILT_VALUATION_INVALID",
                "USD valuation price or timestamp is invalid",
            )
        return UsdValuation(price, amount * price, observed_at)


def _amount(value: Any, decimals: int, field: str) -> Decimal:
    try:
        integer = int(value)
        amount = Decimal(integer) / (Decimal(10) ** decimals)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DomainRejected(
            "NOTILT_RESPONSE_INVALID",
            f"NoTilt gateway returned an invalid {field}",
        ) from exc
    if integer < 0:
        raise DomainRejected(
            "NOTILT_RESPONSE_INVALID",
            f"NoTilt gateway returned a negative {field}",
        )
    return amount


class NoTiltGateway:
    """Constrained, signing-free Python boundary around the official NoTilt SDK."""

    def __init__(
        self,
        *,
        executor: GatewayExecutor | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        self._executor = executor or self._execute_subprocess
        self._timeout_seconds = timeout_seconds
        self._gateway_path = Path(__file__).with_name("notilt_gateway") / "index.mjs"

    @property
    def available(self) -> bool:
        return self._gateway_path.is_file() and shutil.which("node") is not None

    def _execute_subprocess(self, payload: JsonObject) -> JsonObject:
        node = shutil.which("node")
        if node is None or not self._gateway_path.is_file():
            raise DomainRejected(
                "NOTILT_GATEWAY_UNAVAILABLE",
                "NoTilt gateway runtime is not installed",
            )
        try:
            completed = subprocess.run(  # noqa: S603
                [node, str(self._gateway_path)],
                input=json.dumps(payload, separators=(",", ":")),
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
                env={"PATH": os.environ.get("PATH", "")},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DomainRejected(
                "NOTILT_GATEWAY_UNAVAILABLE",
                "NoTilt gateway did not complete",
            ) from exc
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise DomainRejected(
                "NOTILT_RESPONSE_INVALID",
                "NoTilt gateway returned invalid JSON",
            ) from exc
        if not isinstance(response, dict):
            raise DomainRejected(
                "NOTILT_RESPONSE_INVALID",
                "NoTilt gateway response must be an object",
            )
        if completed.returncode != 0 or response.get("ok") is not True:
            error = response.get("error")
            detail = (
                str(error.get("message"))
                if isinstance(error, dict) and error.get("message")
                else "NoTilt rejected the requested operation"
            )
            raise DomainRejected("NOTILT_OPERATION_REJECTED", detail[:400])
        data = response.get("data")
        if not isinstance(data, dict):
            raise DomainRejected(
                "NOTILT_RESPONSE_INVALID",
                "NoTilt gateway omitted its result",
            )
        return data

    def _call(self, operation: str, chain_id: int, **values: Any) -> JsonObject:
        if chain_id not in SUPPORTED_NOTILT_CHAINS:
            raise DomainRejected(
                "NOTILT_CHAIN_UNSUPPORTED",
                "NoTilt only supports Ethereum, BNB Smart Chain, and Arbitrum One",
            )
        return self._executor({"operation": operation, "chainId": chain_id, **values})

    def resolve_assignment(self, chain_id: int, agent: str) -> tuple[str, bool]:
        value = self._call("resolve-assignment", chain_id, agent=agent)
        vault = str(value.get("assignedVault", ""))
        active = value.get("active")
        if len(vault) != 42 or not vault.startswith("0x") or not isinstance(active, bool):
            raise DomainRejected(
                "NOTILT_RESPONSE_INVALID",
                "NoTilt gateway returned an invalid Registry assignment",
            )
        return vault, active

    def verify_deployment(self, chain_id: int) -> JsonObject:
        return self._call("verify-deployment", chain_id)

    def read_vault(self, chain_id: int, vault: str, agent: str) -> NoTiltVaultSnapshot:
        value = self._call("read-vault", chain_id, vault=vault, agent=agent)
        raw_budgets = value.get("budgets")
        if not isinstance(raw_budgets, list) or not raw_budgets:
            raise DomainRejected(
                "NOTILT_RESPONSE_INVALID",
                "NoTilt gateway returned no Vault budgets",
            )
        budgets: list[NoTiltAssetBudget] = []
        for raw in raw_budgets:
            if not isinstance(raw, dict) or not isinstance(raw.get("asset"), dict):
                raise DomainRejected(
                    "NOTILT_RESPONSE_INVALID",
                    "NoTilt gateway returned an invalid Vault budget",
                )
            asset = raw["asset"]
            try:
                decimals = int(asset["decimals"])
                block_timestamp = datetime.fromtimestamp(int(raw["blockTimestamp"]), UTC)
                budget = NoTiltAssetBudget(
                    chain_id=chain_id,
                    chain=str(value["chain"]).upper(),
                    block_number=int(raw["blockNumber"]),
                    block_timestamp=block_timestamp,
                    vault=str(raw["vault"]),
                    agent=str(raw["agent"]),
                    owner=str(raw["owner"]),
                    asset_address=str(asset["address"]),
                    asset=str(asset["symbol"]).upper(),
                    decimals=decimals,
                    native=bool(asset["native"]),
                    is_official_vault=bool(raw["isOfficialVault"]),
                    is_active_whitelist=bool(raw["isActiveWhitelist"]),
                    assigned_whitelist_vault=str(raw["assignedWhitelistVault"]),
                    balance=_amount(raw["balance"], decimals, "balance"),
                    max_release_net=_amount(raw["maxReleaseNet"], decimals, "maxReleaseNet"),
                    pending_net=_amount(raw["pendingNet"], decimals, "pendingNet"),
                    panic_locked=bool(raw["panicLocked"]),
                    daily_release_rate=_amount(raw["dailyReleaseRate"], 18, "dailyReleaseRate"),
                    daily_fee_rate=_amount(raw["dailyFeeRate"], 18, "dailyFeeRate"),
                )
            except (KeyError, OSError, OverflowError, TypeError, ValueError) as exc:
                raise DomainRejected(
                    "NOTILT_RESPONSE_INVALID",
                    "NoTilt gateway returned an invalid Vault budget",
                ) from exc
            budgets.append(budget)
        return NoTiltVaultSnapshot(
            chain_id=chain_id,
            chain=str(value.get("chain", "")).upper(),
            vault=str(value.get("vault", "")),
            agent=str(value.get("agent", "")),
            budgets=tuple(budgets),
        )

    def prepare_deposit(
        self,
        *,
        chain_id: int,
        vault: str,
        agent: str,
        asset: str,
        amount: str,
    ) -> tuple[NoTiltUnsignedTransaction, ...]:
        value = self._call(
            "prepare-deposit",
            chain_id,
            vault=vault,
            agent=agent,
            asset=asset,
            amount=amount,
        )
        transactions = value.get("transactions")
        if not isinstance(transactions, list) or not transactions:
            raise DomainRejected(
                "NOTILT_RESPONSE_INVALID",
                "NoTilt gateway returned no deposit transactions",
            )
        parsed = tuple(
            NoTiltUnsignedTransaction.from_json(item)
            for item in transactions
            if isinstance(item, dict)
        )
        if len(parsed) != len(transactions):
            raise DomainRejected(
                "NOTILT_RESPONSE_INVALID",
                "NoTilt gateway returned an invalid deposit transaction",
            )
        return parsed

    def prepare_release_request(
        self,
        *,
        chain_id: int,
        vault: str,
        agent: str,
        asset: str,
        amount: str,
    ) -> NoTiltUnsignedTransaction:
        value = self._call(
            "prepare-release-request",
            chain_id,
            vault=vault,
            agent=agent,
            asset=asset,
            amount=amount,
        )
        transaction = value.get("transaction")
        if not isinstance(transaction, dict):
            raise DomainRejected(
                "NOTILT_RESPONSE_INVALID",
                "NoTilt gateway returned no release request transaction",
            )
        return NoTiltUnsignedTransaction.from_json(transaction)

    def prepare_release_execution(
        self, *, chain_id: int, vault: str, agent: str, request_id: str
    ) -> NoTiltUnsignedTransaction:
        return NoTiltUnsignedTransaction.from_json(
            self._call(
                "prepare-release-execution",
                chain_id,
                vault=vault,
                agent=agent,
                requestId=request_id,
            )
        )

    def prepare_release_cancellation(
        self, *, chain_id: int, vault: str, agent: str, request_id: str
    ) -> NoTiltUnsignedTransaction:
        return NoTiltUnsignedTransaction.from_json(
            self._call(
                "prepare-release-cancellation",
                chain_id,
                vault=vault,
                agent=agent,
                requestId=request_id,
            )
        )
