from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

MONEY_QUANTUM: Final = Decimal("0.000000000000000001")


class DomainRejected(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class IdempotencyConflict(DomainRejected):
    def __init__(self) -> None:
        super().__init__(
            "IDEMPOTENCY_CONFLICT",
            "the idempotency key was already used for different request semantics",
        )


class Role(StrEnum):
    OBSERVER = "OBSERVER"
    PROPOSER = "PROPOSER"
    REVIEWER = "REVIEWER"
    OPERATOR = "OPERATOR"
    TREASURY_ADMIN = "TREASURY_ADMIN"
    SYSTEM_ADMIN = "SYSTEM_ADMIN"


class WorkspaceRole(StrEnum):
    MEMBER = "MEMBER"
    ADMIN = "ADMIN"


class PrincipalType(StrEnum):
    HUMAN = "HUMAN"
    SERVICE = "SERVICE"


class ExecutionEnvironment(StrEnum):
    SHADOW = "SHADOW"
    TESTNET = "TESTNET"
    LIVE = "LIVE"


class ProposalSource(StrEnum):
    SYSTEM = "SYSTEM"
    MANUAL = "MANUAL"


class ProposalStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class SignalSourceMode(StrEnum):
    PERPTAPE = "PERPTAPE"
    WEBHOOK = "WEBHOOK"


class SignalProvider(StrEnum):
    TRADINGVIEW = "TRADINGVIEW"
    MODEL = "MODEL"


class SignalEventStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PROPOSAL_CREATED = "PROPOSAL_CREATED"


class ReviewDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class RiskPolicyChangeStatus(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXECUTED = "EXECUTED"


class CapitalDirection(StrEnum):
    VAULT_TO_VENUE = "VAULT_TO_VENUE"
    VENUE_TO_VAULT = "VENUE_TO_VAULT"


class DirectCapitalPath(StrEnum):
    VAULT_TO_BINANCE = "VAULT_TO_BINANCE"
    VAULT_TO_HYPERLIQUID = "VAULT_TO_HYPERLIQUID"
    BINANCE_TO_VAULT = "BINANCE_TO_VAULT"
    HYPERLIQUID_TO_VAULT = "HYPERLIQUID_TO_VAULT"


class CapitalTreasuryProvider(StrEnum):
    NOTILT_VAULT = "NOTILT_VAULT"
    SAFE_SPENDING_LIMIT = "SAFE_SPENDING_LIMIT"


class CapitalTransferStatus(StrEnum):
    SOURCE_RESERVED = "SOURCE_RESERVED"
    SUBMITTED = "SUBMITTED"
    IN_FLIGHT = "IN_FLIGHT"
    DESTINATION_CONFIRMED = "DESTINATION_CONFIRMED"
    SETTLED = "SETTLED"
    UNKNOWN = "UNKNOWN"
    FAILED_SOURCE_RESTORED = "FAILED_SOURCE_RESTORED"
    MANUAL_REQUIRED = "MANUAL_REQUIRED"


class RiskTier(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class RiskResult(StrEnum):
    ALLOW = "ALLOW"
    SCALE = "SCALE"
    DENY = "DENY"


class SystemRiskState(StrEnum):
    NORMAL = "NORMAL"
    NO_PYRAMID = "NO_PYRAMID"
    REDUCE_ONLY = "REDUCE_ONLY"
    KILL_SWITCH = "KILL_SWITCH"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class IntentKind(StrEnum):
    INITIAL = "INITIAL"
    ADD = "ADD"
    REDUCE = "REDUCE"
    EXIT = "EXIT"


class ReservationStatus(StrEnum):
    RESERVED = "RESERVED"
    OPEN = "OPEN"
    UNKNOWN = "UNKNOWN"
    RELEASED = "RELEASED"


class OrderIntentStatus(StrEnum):
    PENDING = "PENDING"
    RESERVED = "RESERVED"
    READY = "READY"
    SENT = "SENT"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class CampaignStatus(StrEnum):
    OPENING = "OPENING"
    OPEN = "OPEN"
    REDUCING = "REDUCING"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


class CapabilityStatus(StrEnum):
    DISABLED = "DISABLED"
    ENABLED = "ENABLED"


class FactStatus(StrEnum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"


class ProtectionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"


class VenueOrderStatus(StrEnum):
    SENT = "SENT"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class ReconciliationStatus(StrEnum):
    MATCH = "MATCH"
    DIFFERENCE = "DIFFERENCE"
    UNKNOWN = "UNKNOWN"
    MANUAL_REQUIRED = "MANUAL_REQUIRED"
    RESOLVED = "RESOLVED"


class TargetUrgency(StrEnum):
    NORMAL = "NORMAL"
    URGENT = "URGENT"
    IMMEDIATE = "IMMEDIATE"


@dataclass(frozen=True, slots=True)
class RiskPolicyInput:
    version: str
    system_state: SystemRiskState
    max_total_risk: Decimal | None
    max_account_risk: Decimal | None
    max_single_loss: Decimal | None
    max_consecutive_losses: int | None
    loss_cooldown: timedelta | None
    max_fact_age: timedelta | None


@dataclass(frozen=True, slots=True)
class RiskEvaluationInput:
    kind: IntentKind
    requested_quantity: Decimal
    requested_risk: Decimal
    current_risk: Decimal
    current_account_risk: Decimal
    team_consecutive_losses: int
    account_consecutive_losses: int
    loss_cooldown_remaining: timedelta
    fact_age: timedelta
    position_known: bool
    equity_known: bool
    protection_known: bool
    source_current: bool = True


@dataclass(frozen=True, slots=True)
class RiskOutcome:
    result: RiskResult
    allowed_quantity: Decimal
    allowed_risk: Decimal
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TargetCandidate:
    target_quantity: Decimal
    urgency: TargetUrgency
    reason: str


@dataclass(frozen=True, slots=True)
class TargetDecision:
    target_quantity: Decimal
    urgency: TargetUrgency
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EconomicFill:
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    slippage: Decimal


@dataclass(frozen=True, slots=True)
class PnlBreakdown:
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees: Decimal
    funding: Decimal
    slippage: Decimal
    total_pnl: Decimal
    open_quantity: Decimal
    average_entry_price: Decimal


@dataclass(frozen=True, slots=True)
class IntentCreation:
    campaign_id: UUID
    reservation_id: UUID
    intent_id: UUID


@dataclass(frozen=True)
class AddCandidateFacts:
    """Narrow, immutable facts accepted from the Perptape adapter for one Add decision."""

    candidate_id: str
    contract_version: str
    venue: str
    symbol: str
    direction: Direction
    observed_at: datetime
    reference_price: Decimal
    readiness: str
    legacy_candidate_id: str | None = None


def _amount(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM)


def _deny(reason: str) -> RiskOutcome:
    return RiskOutcome(RiskResult.DENY, Decimal(0), Decimal(0), (reason,))


def evaluate_risk(policy: RiskPolicyInput, inputs: RiskEvaluationInput) -> RiskOutcome:
    """Deterministically apply the current policy to current facts without persistence."""

    if any(
        value is None
        for value in (
            policy.max_total_risk,
            policy.max_account_risk,
            policy.max_single_loss,
            policy.max_consecutive_losses,
            policy.loss_cooldown,
            policy.max_fact_age,
        )
    ):
        return _deny("RISK_LIMITS_UNCONFIGURED")
    assert policy.max_total_risk is not None
    assert policy.max_account_risk is not None
    assert policy.max_single_loss is not None
    assert policy.max_consecutive_losses is not None
    assert policy.loss_cooldown is not None
    assert policy.max_fact_age is not None
    if (
        policy.max_total_risk <= 0
        or policy.max_account_risk <= 0
        or policy.max_single_loss <= 0
        or policy.max_consecutive_losses <= 0
        or policy.loss_cooldown <= timedelta(0)
        or policy.max_fact_age <= timedelta(0)
        or inputs.current_risk < 0
        or inputs.current_account_risk < 0
        or inputs.team_consecutive_losses < 0
        or inputs.account_consecutive_losses < 0
        or inputs.loss_cooldown_remaining < timedelta(0)
        or inputs.fact_age < timedelta(0)
        or inputs.requested_quantity <= 0
        or inputs.requested_risk <= 0
    ):
        return _deny("INVALID_INPUT")
    if not inputs.source_current:
        return _deny("READ_ONLY_SOURCE_UNAVAILABLE")
    if inputs.fact_age > policy.max_fact_age:
        return _deny("STALE_FACTS")
    if not inputs.position_known:
        return _deny("POSITION_UNKNOWN")
    if not inputs.equity_known:
        return _deny("EQUITY_UNKNOWN")
    if not inputs.protection_known:
        return _deny("PROTECTION_UNKNOWN")
    if policy.system_state is SystemRiskState.KILL_SWITCH:
        return _deny("KILL_SWITCH")
    if policy.system_state is SystemRiskState.REDUCE_ONLY and inputs.kind in {
        IntentKind.INITIAL,
        IntentKind.ADD,
    }:
        return _deny("REDUCE_ONLY")
    if policy.system_state is SystemRiskState.NO_PYRAMID and inputs.kind is IntentKind.ADD:
        return _deny("PYRAMID_DISABLED")
    if inputs.requested_risk > policy.max_single_loss:
        return _deny("SINGLE_LOSS_LIMIT_EXCEEDED")
    if (
        max(inputs.team_consecutive_losses, inputs.account_consecutive_losses)
        >= policy.max_consecutive_losses
        and inputs.loss_cooldown_remaining > timedelta(0)
    ):
        return _deny("LOSS_COOLDOWN_ACTIVE")
    available = min(
        max(Decimal(0), policy.max_total_risk - inputs.current_risk),
        max(Decimal(0), policy.max_account_risk - inputs.current_account_risk),
    )
    if available <= 0:
        return _deny("RISK_CAPACITY_EXHAUSTED")
    if inputs.requested_risk <= available:
        return RiskOutcome(
            RiskResult.ALLOW,
            _amount(inputs.requested_quantity),
            _amount(inputs.requested_risk),
            (),
        )

    ratio = available / inputs.requested_risk
    allowed_quantity = (inputs.requested_quantity * ratio).quantize(
        MONEY_QUANTUM, rounding=ROUND_DOWN
    )
    if allowed_quantity <= 0:
        return _deny("RISK_CAPACITY_EXHAUSTED")
    return RiskOutcome(
        RiskResult.SCALE,
        allowed_quantity,
        _amount(available),
        ("RISK_CAPACITY_SCALED",),
    )


def select_target_position(candidates: tuple[TargetCandidate, ...]) -> TargetDecision:
    if not candidates:
        raise DomainRejected("TARGET_CANDIDATES_REQUIRED", "at least one target is required")
    if any(candidate.target_quantity < 0 for candidate in candidates):
        raise DomainRejected("INVALID_TARGET", "target quantity cannot be negative")

    urgency_order = {
        TargetUrgency.NORMAL: 0,
        TargetUrgency.URGENT: 1,
        TargetUrgency.IMMEDIATE: 2,
    }
    return TargetDecision(
        target_quantity=min(candidate.target_quantity for candidate in candidates),
        urgency=max(candidates, key=lambda candidate: urgency_order[candidate.urgency]).urgency,
        reasons=tuple(sorted({candidate.reason for candidate in candidates})),
    )


def compute_pnl(
    *, fills: tuple[EconomicFill, ...], mark_price: Decimal, funding: Decimal
) -> PnlBreakdown:
    """Compute position economics from fills; positive funding is a receipt."""

    position = Decimal(0)
    average_entry = Decimal(0)
    realized_before_costs = Decimal(0)
    fees = Decimal(0)
    slippage = Decimal(0)

    for fill in fills:
        if fill.quantity <= 0 or fill.price <= 0:
            raise DomainRejected("INVALID_FILL", "fill quantity and price must be positive")
        signed_quantity = fill.quantity if fill.side == "BUY" else -fill.quantity
        if fill.side not in {"BUY", "SELL"}:
            raise DomainRejected("INVALID_FILL_SIDE", "fill side must be BUY or SELL")

        if position == 0 or (position > 0) == (signed_quantity > 0):
            new_position = position + signed_quantity
            average_entry = (
                abs(position) * average_entry + abs(signed_quantity) * fill.price
            ) / abs(new_position)
            position = new_position
        else:
            closing_quantity = min(abs(position), abs(signed_quantity))
            if position > 0:
                realized_before_costs += (fill.price - average_entry) * closing_quantity
            else:
                realized_before_costs += (average_entry - fill.price) * closing_quantity
            new_position = position + signed_quantity
            if new_position == 0:
                average_entry = Decimal(0)
            elif (new_position > 0) != (position > 0):
                average_entry = fill.price
            position = new_position
        fees += fill.fee
        slippage += fill.slippage

    unrealized = (mark_price - average_entry) * position if position else Decimal(0)
    realized = realized_before_costs - fees - slippage + funding
    return PnlBreakdown(
        realized_pnl=_amount(realized),
        unrealized_pnl=_amount(unrealized),
        fees=_amount(fees),
        funding=_amount(funding),
        slippage=_amount(slippage),
        total_pnl=_amount(realized + unrealized),
        open_quantity=_amount(position),
        average_entry_price=_amount(average_entry),
    )
