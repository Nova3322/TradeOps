from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from trading_control_plane.domain import CapitalDirection, DomainRejected, ExecutionEnvironment


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
