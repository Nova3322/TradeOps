from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from trading_control_plane.config import Settings
from trading_control_plane.domain import (
    CapitalDirection,
    CapitalTreasuryProvider,
    DirectCapitalPath,
    DomainRejected,
    ExecutionEnvironment,
)


@dataclass(frozen=True)
class CapitalTransferCommand:
    capital_transfer_id: UUID
    environment: ExecutionEnvironment
    direction: CapitalDirection
    source_id: str
    destination_id: str
    asset: str
    network: str
    destination_reference: str
    gross_amount: Decimal
    max_fee: Decimal
    min_received: Decimal


@dataclass(frozen=True)
class CapitalTransferSubmission:
    external_transfer_id: str
    status: str
    observed_at: datetime


@dataclass(frozen=True)
class CapitalAutomationDecision:
    purpose: str
    amount: Decimal | None
    reason: str


@dataclass(frozen=True)
class DirectCapitalPlan:
    path: DirectCapitalPath
    treasury_provider: CapitalTreasuryProvider
    venue: str
    account_id: str | None
    vault_id: str | None
    asset: str
    network: str
    amount: Decimal
    max_fee: Decimal | None
    min_received: Decimal | None
    status: str
    receipt_status: str
    source_reference: str | None
    destination_reference: str | None
    stages: tuple[dict[str, Any], ...]
    blockers: tuple[str, ...]
    execute_after: datetime | None
    expires_at: datetime


def build_direct_capital_plan(
    *,
    path: DirectCapitalPath,
    treasury_provider: CapitalTreasuryProvider = CapitalTreasuryProvider.NOTILT_VAULT,
    amount: Decimal,
    settings: Settings,
    capital_transfer_gate: str | None,
    binance_capital_credentials_configured: bool | None = None,
    now: datetime,
) -> DirectCapitalPlan:
    """Build a fully explicit, non-broadcasting capital path plan."""

    if amount <= 0:
        raise DomainRejected("CAPITAL_AMOUNT_INVALID", "capital amount must be positive")
    venue = "BINANCE" if "BINANCE" in path.value else "HYPERLIQUID"
    account_id = (
        settings.capital_direct_binance_account_id
        if venue == "BINANCE"
        else settings.capital_direct_hyperliquid_account_id
    )
    is_safe = treasury_provider is CapitalTreasuryProvider.SAFE_SPENDING_LIMIT
    vault_id = "SAFE_SPENDING_LIMIT" if is_safe else settings.capital_direct_vault_id
    owned_address = settings.capital_direct_owned_arbitrum_address
    vault_address = (
        settings.capital_direct_safe_address if is_safe else settings.capital_direct_vault_address
    )
    venue_reference = (
        settings.capital_direct_binance_deposit_address
        if venue == "BINANCE"
        else settings.capital_direct_hyperliquid_bridge_address
    )
    blockers: list[str] = []
    required = {
        ("SAFE_ADDRESS_MISSING" if is_safe else "CAPITAL_VAULT_ID_MISSING"): (
            vault_address if is_safe else vault_id
        ),
        ("SAFE_ADDRESS_MISSING" if is_safe else "CAPITAL_VAULT_ADDRESS_MISSING"): vault_address,
        "CAPITAL_VENUE_ACCOUNT_MISSING": account_id,
    }
    if is_safe:
        required["SAFE_DELEGATE_ADDRESS_MISSING"] = settings.capital_direct_safe_delegate_address
        if not settings.safe_spending_enabled or not settings.safe_spending_arbitrum_rpc_url:
            blockers.append("SAFE_SPENDING_LIMIT_NOT_CONFIGURED")
    if path in {
        DirectCapitalPath.VAULT_TO_HYPERLIQUID,
        DirectCapitalPath.HYPERLIQUID_TO_VAULT,
    }:
        required["CAPITAL_OWNED_ARBITRUM_ADDRESS_MISSING"] = owned_address
    if path in {
        DirectCapitalPath.VAULT_TO_HYPERLIQUID,
        DirectCapitalPath.HYPERLIQUID_TO_VAULT,
    }:
        required["CAPITAL_HYPERLIQUID_CONTRACT_MISSING"] = venue_reference
    elif path is DirectCapitalPath.VAULT_TO_BINANCE:
        required["CAPITAL_BINANCE_WHITELIST_ADDRESS_MISSING"] = venue_reference
    elif path is DirectCapitalPath.BINANCE_TO_VAULT:
        withdrawal_address = settings.capital_direct_binance_withdrawal_address
        required["CAPITAL_BINANCE_WITHDRAWAL_ADDRESS_MISSING"] = withdrawal_address
        if (
            withdrawal_address
            and vault_address
            and withdrawal_address.lower() != vault_address.lower()
        ):
            blockers.append("CAPITAL_BINANCE_WITHDRAWAL_ADDRESS_SCOPE_MISMATCH")
    binance_credentials_ready = (
        bool(settings.binance_capital_api_key and settings.binance_capital_api_secret)
        if binance_capital_credentials_configured is None
        else binance_capital_credentials_configured
    )
    if venue == "BINANCE" and not binance_credentials_ready:
        blockers.append("BINANCE_CAPITAL_CREDENTIALS_MISSING")
    for code, value in required.items():
        if not value:
            blockers.append(code)
    if settings.capital_direct_max_amount is None:
        blockers.append("CAPITAL_AMOUNT_LIMIT_MISSING")
    elif amount > settings.capital_direct_max_amount:
        blockers.append("CAPITAL_AMOUNT_LIMIT_EXCEEDED")
    if settings.capital_direct_max_fee is None:
        blockers.append("CAPITAL_FEE_LIMIT_MISSING")
    if capital_transfer_gate != "ENABLED":
        blockers.append("CAPITAL_TRANSFER_GATE_DISABLED")

    max_fee = settings.capital_direct_max_fee
    fee_is_deducted_from_usdc = path in {
        DirectCapitalPath.BINANCE_TO_VAULT,
        DirectCapitalPath.HYPERLIQUID_TO_VAULT,
    }
    invalid_min_received = (
        fee_is_deducted_from_usdc and max_fee is not None and amount <= max_fee
    )
    min_received = (
        None
        if max_fee is None or invalid_min_received
        else amount - max_fee
        if fee_is_deducted_from_usdc
        else amount
    )
    if invalid_min_received:
        blockers.append("CAPITAL_MIN_RECEIVED_INVALID")

    execute_after: datetime | None = None
    stages: tuple[dict[str, str], ...]
    if is_safe and path is DirectCapitalPath.VAULT_TO_BINANCE:
        stages = (
            {"code": "READ_SAFE_SPENDING_LIMIT", "status": "BLOCKED"},
            {"code": "VERIFY_SAFE_MODULE_DELEGATE_TOKEN_NONCE", "status": "BLOCKED"},
            {"code": "BUILD_SAFE_ALLOWANCE_SIGNATURE_REQUEST", "status": "BLOCKED"},
            {"code": "HUMAN_DELEGATE_SIGNATURE_AND_SUBMISSION", "status": "BLOCKED"},
            {"code": "VERIFY_SAFE_TRANSFER_RECEIPT", "status": "BLOCKED"},
            {"code": "TRANSFER_BINANCE_SPOT_TO_USDM", "status": "BLOCKED"},
        )
        blockers.extend(
            (
                "SAFE_ALLOWANCE_PREFLIGHT_REQUIRED",
                "BINANCE_DEPOSIT_PREFLIGHT_REQUIRED",
            )
        )
    elif is_safe and path is DirectCapitalPath.VAULT_TO_HYPERLIQUID:
        stages = (
            {"code": "READ_SAFE_SPENDING_LIMIT", "status": "BLOCKED"},
            {"code": "SAFE_TRANSFER_TO_AUTHORIZED_OWNED_ADDRESS", "status": "BLOCKED"},
            {"code": "DEPOSIT_TO_HYPERLIQUID_CONTRACT", "status": "BLOCKED"},
            {"code": "HUMAN_WALLET_CONFIRMATION", "status": "BLOCKED"},
            {"code": "VERIFY_DESTINATION_RECEIPTS", "status": "BLOCKED"},
        )
        blockers.extend(
            (
                "SAFE_ALLOWANCE_PREFLIGHT_REQUIRED",
                "HYPERLIQUID_HUMAN_WALLET_CONFIRMATION_REQUIRED",
            )
        )
    elif is_safe and path is DirectCapitalPath.HYPERLIQUID_TO_VAULT:
        stages = (
            {"code": "WITHDRAW_FROM_HYPERLIQUID_CONTRACT", "status": "BLOCKED"},
            {"code": "WITHDRAW_DIRECTLY_TO_SAFE", "status": "BLOCKED"},
            {"code": "HUMAN_WALLET_CONFIRMATION", "status": "BLOCKED"},
            {"code": "VERIFY_SAFE_BALANCE_RECEIPT", "status": "BLOCKED"},
        )
        blockers.append("HYPERLIQUID_HUMAN_WALLET_CONFIRMATION_REQUIRED")
    elif is_safe:
        stages = (
            {
                "code": "RESTRICTED_BINANCE_WITHDRAWAL_TO_SELECTED_TREASURY",
                "status": "BLOCKED",
            },
            {"code": "TRANSFER_BINANCE_USDM_TO_SPOT", "status": "BLOCKED"},
            {"code": "VERIFY_BINANCE_WITHDRAWAL_RECEIPT", "status": "BLOCKED"},
            {"code": "VERIFY_SELECTED_TREASURY_CREDIT", "status": "BLOCKED"},
        )
        blockers.append("BINANCE_RESTRICTED_WITHDRAWAL_PREFLIGHT_REQUIRED")
    elif path is DirectCapitalPath.VAULT_TO_BINANCE:
        execute_after = now + timedelta(minutes=10)
        stages = (
            {"code": "VAULT_RELEASE_REQUEST", "status": "BLOCKED"},
            {
                "code": "WAIT_10_MINUTES",
                "status": "BLOCKED",
                "execute_after": execute_after.isoformat(),
            },
            {"code": "REVALIDATE_RELEASE", "status": "BLOCKED"},
            {"code": "TRANSFER_TO_AUTHORIZED_BINANCE_ADDRESS", "status": "BLOCKED"},
            {"code": "TRANSFER_BINANCE_SPOT_TO_USDM", "status": "BLOCKED"},
        )
        blockers.append("BINANCE_DEPOSIT_PREFLIGHT_REQUIRED")
    elif path is DirectCapitalPath.VAULT_TO_HYPERLIQUID:
        execute_after = now + timedelta(minutes=10)
        stages = (
            {"code": "VAULT_RELEASE_TO_AUTHORIZED_OWNED_ADDRESS", "status": "BLOCKED"},
            {
                "code": "WAIT_10_MINUTES",
                "status": "BLOCKED",
                "execute_after": execute_after.isoformat(),
            },
            {"code": "REVALIDATE_RELEASE", "status": "BLOCKED"},
            {"code": "DEPOSIT_TO_HYPERLIQUID_CONTRACT", "status": "BLOCKED"},
        )
        blockers.append("HYPERLIQUID_HUMAN_WALLET_CONFIRMATION_REQUIRED")
    elif path is DirectCapitalPath.HYPERLIQUID_TO_VAULT:
        stages = (
            {"code": "WITHDRAW_FROM_HYPERLIQUID_CONTRACT", "status": "BLOCKED"},
            {"code": "RECEIVE_AT_AUTHORIZED_OWNED_ADDRESS", "status": "BLOCKED"},
            {"code": "PREPARE_NOTILT_SDK_DEPOSIT", "status": "BLOCKED"},
            {"code": "HUMAN_WALLET_CONFIRMATION", "status": "BLOCKED"},
            {"code": "VERIFY_NOTILT_DEPOSIT_RECEIPT", "status": "BLOCKED"},
        )
        blockers.append("HYPERLIQUID_HUMAN_WALLET_CONFIRMATION_REQUIRED")
    else:
        stages = (
            {
                "code": "RESTRICTED_BINANCE_WITHDRAWAL_TO_SELECTED_TREASURY",
                "status": "BLOCKED",
            },
            {"code": "TRANSFER_BINANCE_USDM_TO_SPOT", "status": "BLOCKED"},
            {"code": "VERIFY_BINANCE_WITHDRAWAL_RECEIPT", "status": "BLOCKED"},
            {"code": "VERIFY_SELECTED_TREASURY_CREDIT", "status": "BLOCKED"},
        )
        blockers.append("BINANCE_RESTRICTED_WITHDRAWAL_PREFLIGHT_REQUIRED")

    source_reference = (
        vault_address
        if path in {DirectCapitalPath.VAULT_TO_BINANCE, DirectCapitalPath.VAULT_TO_HYPERLIQUID}
        else account_id
        if path is DirectCapitalPath.BINANCE_TO_VAULT
        else owned_address
    )
    destination_reference = (
        venue_reference
        if path is DirectCapitalPath.VAULT_TO_BINANCE
        else owned_address
        if path is DirectCapitalPath.VAULT_TO_HYPERLIQUID
        else vault_address
    )
    return DirectCapitalPlan(
        path=path,
        treasury_provider=treasury_provider,
        venue=venue,
        account_id=account_id,
        vault_id=vault_id,
        asset=settings.capital_direct_asset,
        network=settings.capital_direct_network,
        amount=amount,
        max_fee=max_fee,
        min_received=min_received,
        status="BLOCKED",
        receipt_status="NOT_SUBMITTED",
        source_reference=source_reference,
        destination_reference=destination_reference,
        stages=stages,
        blockers=tuple(dict.fromkeys(blockers)),
        execute_after=execute_after,
        expires_at=now + timedelta(hours=24),
    )


def evaluate_capital_automation(
    *,
    purpose: str,
    venue_available: Decimal,
    venue_withdrawable: Decimal,
    vault_available: Decimal,
    confirmed_realized_pnl: Decimal,
    operating_low: Decimal,
    operating_target: Decimal,
    operating_high: Decimal,
    vault_minimum_reserve: Decimal,
    minimum_transfer: Decimal,
    maximum_transfer: Decimal,
    max_fee: Decimal,
) -> CapitalAutomationDecision:
    """Compute one safe capital candidate from confirmed facts only."""

    if not (
        Decimal(0) <= operating_low <= operating_target <= operating_high
        and vault_minimum_reserve >= 0
        and minimum_transfer > 0
        and maximum_transfer >= minimum_transfer
        and Decimal(0) <= max_fee < minimum_transfer
    ):
        raise DomainRejected("CAPITAL_AUTOMATION_POLICY_INVALID", "capital thresholds are invalid")
    if min(venue_available, venue_withdrawable, vault_available) < 0:
        raise DomainRejected("CAPITAL_AUTOMATION_FACT_INVALID", "capital facts cannot be negative")

    if purpose == "AUTO_PROFIT_SWEEP":
        if venue_available <= operating_high:
            return CapitalAutomationDecision(purpose, None, "VENUE_NOT_ABOVE_HIGH")
        if confirmed_realized_pnl <= 0:
            return CapitalAutomationDecision(purpose, None, "NO_CONFIRMED_REALIZED_PROFIT")
        amount = min(
            venue_available - operating_target,
            venue_withdrawable - operating_target,
            confirmed_realized_pnl,
            maximum_transfer,
        )
    elif purpose == "AUTO_OPERATING_REFILL":
        if venue_available >= operating_low:
            return CapitalAutomationDecision(purpose, None, "VENUE_NOT_BELOW_LOW")
        if confirmed_realized_pnl < 0:
            return CapitalAutomationDecision(purpose, None, "REALIZED_LOSS_REFILL_BLOCKED")
        amount = min(
            operating_target - venue_available,
            vault_available - vault_minimum_reserve,
            maximum_transfer,
        )
    else:
        raise DomainRejected("CAPITAL_AUTOMATION_PURPOSE_INVALID", "unknown capital automation")

    if amount < minimum_transfer or amount <= max_fee:
        return CapitalAutomationDecision(purpose, None, "TRANSFER_BELOW_SAFE_MINIMUM")
    return CapitalAutomationDecision(purpose, amount, "CANDIDATE_READY")


class MockCapitalTransferAdapter:
    """Deterministic test adapter. It has no network, signer, wallet, or venue credentials."""

    def submit(
        self, command: CapitalTransferCommand, *, now: datetime
    ) -> CapitalTransferSubmission:
        if command.environment is ExecutionEnvironment.LIVE:
            raise DomainRejected(
                "CAPITAL_TRANSFER_LIVE_DISABLED",
                "the mock adapter can never submit a LIVE capital transfer",
            )
        identity = hashlib.sha256(
            f"{command.capital_transfer_id}:{command.source_id}:{command.destination_id}:"
            f"{command.asset}:{command.network}:{command.gross_amount}".encode()
        ).hexdigest()[:24]
        return CapitalTransferSubmission(
            external_transfer_id=f"mock-capital-{identity}",
            status="SUBMITTED",
            observed_at=now,
        )
