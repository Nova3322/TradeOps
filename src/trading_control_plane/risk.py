from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_control_plane.authorization import RiskTier, SystemRiskState
from trading_control_plane.commands import (
    CommandChannel,
    CommandEnvelope,
    CommandOutcome,
    CommandRejected,
    CommandStatus,
    DomainEvent,
    hash_json,
)
from trading_control_plane.metrics import (
    RISK_DECISION_DURATION,
    RISK_DECISIONS,
    RISK_STALE_INPUTS,
    SYSTEM_RISK_STATE_TRANSITIONS,
)
from trading_control_plane.proposal_models import SystemRiskStateRecord
from trading_control_plane.risk_models import (
    RiskDecisionSnapshot,
    RiskPolicyRecord,
)

ZERO = Decimal("0")
ONE = Decimal("1")
ONE_R_FRACTION = Decimal("0.005")


class RiskDecisionResult(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    ALLOW_WITH_CAP = "ALLOW_WITH_CAP"


class FactType(StrEnum):
    MARKET = "MARKET"
    ACCOUNT = "ACCOUNT"
    VAULT = "VAULT"
    POSITIONS = "POSITIONS"
    ORDERS = "ORDERS"
    LEDGER = "LEDGER"
    CATALOG = "CATALOG"
    VENUE_CAPABILITY = "VENUE_CAPABILITY"
    PROTECTION = "PROTECTION"


class FactStatus(StrEnum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"


class ScopeType(StrEnum):
    UNDERLYING = "UNDERLYING"
    RISK_CLUSTER = "RISK_CLUSTER"
    SECTOR = "SECTOR"
    EXECUTION_DOMAIN = "EXECUTION_DOMAIN"
    VENUE = "VENUE"
    COLLATERAL_POOL = "COLLATERAL_POOL"
    PORTFOLIO = "PORTFOLIO"


class RiskInclusionMode(StrEnum):
    EXCHANGE_ONLY = "EXCHANGE_ONLY"
    EXCHANGE_PLUS_VAULT = "EXCHANGE_PLUS_VAULT"


class PositionDirection(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


REQUIRED_FACT_TYPES = frozenset(FactType)
REQUIRED_SCOPE_TYPES = frozenset(ScopeType)

RISK_STATE_RANK = {
    SystemRiskState.NORMAL: 0,
    SystemRiskState.NO_PYRAMID: 1,
    SystemRiskState.NO_NEW_POSITION: 2,
    SystemRiskState.REDUCE_ONLY: 3,
    SystemRiskState.KILL_SWITCH: 4,
    SystemRiskState.UNKNOWN: 5,
}

REASON_PRIORITY = (
    "RISK_POLICY_OUTSIDE_VALID_WINDOW",
    "FACTS_UNKNOWN",
    "FACT_TIMESTAMP_IN_FUTURE",
    "FACT_TIME_ORDER_INVALID",
    "FACTS_STALE",
    "FACTS_INCONSISTENT",
    "PORTFOLIO_EQUITY_NON_POSITIVE",
    "INSTRUMENT_UNCLASSIFIED",
    "CAPABILITY_CERTIFICATE_INVALID",
    "PROTECTION_UNAVAILABLE",
    "SYSTEM_RISK_STATE_DENY",
    "SCOPE_POLICY_MISSING",
    "PROPOSAL_RISK_CAP_INVALID",
    "INVALIDATION_PRICE_INVALID",
    "TRADING_RULE_VIOLATION",
    "QUANTITY_RULE_VIOLATION",
    "LEVERAGE_LIMIT_EXCEEDED",
    "FUNDING_ENVELOPE_EXCEEDED",
    "MARGIN_INSUFFICIENT",
    "TRADE_LOSS_LIMIT_EXCEEDED",
    "SCOPE_PLANNED_LIMIT_EXCEEDED",
    "SCOPE_STRESS_LIMIT_EXCEEDED",
)
REASON_RANK = {code: index for index, code in enumerate(REASON_PRIORITY)}


class FactFreshnessLimit(BaseModel):
    model_config = ConfigDict(frozen=True)

    fact_type: FactType
    max_age_ms: int = Field(gt=0)


class ScopeLimit(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope_type: ScopeType
    scope_id: str = Field(min_length=1, max_length=255)
    planned_loss_cap: Decimal = Field(ge=0)
    stress_loss_cap: Decimal = Field(ge=0)

    @property
    def key(self) -> tuple[ScopeType, str]:
        return (self.scope_type, self.scope_id)


class RiskPolicyParameters(BaseModel):
    """Every research-dependent value is explicit; there are no production defaults."""

    model_config = ConfigDict(frozen=True)

    one_r_fraction: Decimal
    low_loss_multiplier: Decimal
    medium_loss_multiplier: Decimal
    high_loss_multiplier: Decimal
    low_max_leverage: Decimal
    medium_max_leverage: Decimal
    high_max_leverage: Decimal
    trade_funding_pct: Decimal = Field(gt=0, le=1)
    absolute_trade_loss_cap: Decimal | None = Field(default=None, gt=0)
    consistency_window_ms: int = Field(gt=0)
    max_future_skew_ms: int = Field(ge=0)
    fact_freshness_limits: tuple[FactFreshnessLimit, ...]
    scope_limits: tuple[ScopeLimit, ...]

    @model_validator(mode="after")
    def fixed_business_contract_and_unique_limits(self) -> Self:
        if self.one_r_fraction != ONE_R_FRACTION:
            raise ValueError("one_r_fraction must be exactly 0.005")
        if (
            self.low_loss_multiplier,
            self.medium_loss_multiplier,
            self.high_loss_multiplier,
        ) != (Decimal("1"), Decimal("2"), Decimal("3")):
            raise ValueError("risk tier loss multipliers must be exactly 1/2/3")
        if (
            self.low_max_leverage,
            self.medium_max_leverage,
            self.high_max_leverage,
        ) != (Decimal("3"), Decimal("5"), Decimal("10")):
            raise ValueError("risk tier leverage caps must be exactly 3/5/10")
        freshness_types = [item.fact_type for item in self.fact_freshness_limits]
        if len(freshness_types) != len(set(freshness_types)):
            raise ValueError("fact freshness limits must be unique")
        if frozenset(freshness_types) != REQUIRED_FACT_TYPES:
            raise ValueError("fact freshness limits must cover every required fact type")
        scope_keys = [item.key for item in self.scope_limits]
        if len(scope_keys) != len(set(scope_keys)):
            raise ValueError("scope limits must be unique")
        return self

    def loss_multiplier(self, tier: RiskTier) -> Decimal:
        return {
            RiskTier.LOW: self.low_loss_multiplier,
            RiskTier.MEDIUM: self.medium_loss_multiplier,
            RiskTier.HIGH: self.high_loss_multiplier,
        }[tier]

    def leverage_cap(self, tier: RiskTier) -> Decimal:
        return {
            RiskTier.LOW: self.low_max_leverage,
            RiskTier.MEDIUM: self.medium_max_leverage,
            RiskTier.HIGH: self.high_max_leverage,
        }[tier]


class CertificationBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    strategy_id: str = Field(min_length=1, max_length=160)
    strategy_version: str = Field(min_length=1, max_length=120)
    strategy_parameter_version: str = Field(min_length=1, max_length=120)
    authorization_policy_version: str = Field(min_length=1, max_length=120)
    instrument_identity: str = Field(min_length=1, max_length=255)
    venue: str = Field(min_length=1, max_length=80)
    execution_domain: str = Field(min_length=1, max_length=120)
    account_id: str = Field(min_length=1, max_length=160)
    account_abstraction: str = Field(min_length=1, max_length=80)
    margin_mode: str = Field(min_length=1, max_length=80)
    collateral_scope: str = Field(min_length=1, max_length=120)
    collateral_pool_id: str = Field(min_length=1, max_length=160)
    adapter_version: str = Field(min_length=1, max_length=120)
    freqtrade_worker_version: str = Field(min_length=1, max_length=120)
    account_capability_version: str = Field(min_length=1, max_length=120)
    catalog_version: str = Field(min_length=1, max_length=120)
    capability_certificate_ref: str = Field(min_length=1, max_length=255)


class MarketRiskInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    direction: PositionDirection
    mark_price: Decimal = Field(gt=0)
    index_price: Decimal = Field(gt=0)
    executable_price: Decimal = Field(gt=0)
    initial_invalidation_price: Decimal = Field(gt=0)
    contract_multiplier: Decimal = Field(gt=0)
    tick_size: Decimal = Field(gt=0)
    minimum_quantity: Decimal = Field(gt=0)
    minimum_notional: Decimal = Field(ge=0)
    funding_rate: Decimal
    max_slippage_bps: Decimal = Field(ge=0)
    contract_rules_version: str = Field(min_length=1, max_length=120)
    loss_model_version: str = Field(min_length=1, max_length=120)
    loss_calculation_ref: str = Field(min_length=1, max_length=255)


class CapitalInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    risk_inclusion_mode: RiskInclusionMode
    exchange_settled_equity_ex_upnl: Decimal
    current_unrealized_pnl: Decimal
    eligible_vault_equity: Decimal = Field(ge=0)
    exchange_risk_equity: Decimal = Field(ge=0)
    total_capital_snapshot_0: Decimal = Field(gt=0)
    funding_used: Decimal = Field(ge=0)
    funding_reserved: Decimal = Field(ge=0)
    available_margin: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def vault_mode_is_explicit(self) -> Self:
        if (
            self.risk_inclusion_mode is RiskInclusionMode.EXCHANGE_ONLY
            and self.eligible_vault_equity != ZERO
        ):
            raise ValueError("eligible_vault_equity must be zero in EXCHANGE_ONLY mode")
        conservative_exchange_ceiling = max(
            ZERO,
            min(self.exchange_settled_equity_ex_upnl, self.exchange_margin_equity),
        )
        if self.exchange_risk_equity > conservative_exchange_ceiling:
            raise ValueError(
                "exchange_risk_equity cannot include positive UPNL or exceed MTM equity"
            )
        return self

    @property
    def exchange_margin_equity(self) -> Decimal:
        return self.exchange_settled_equity_ex_upnl + self.current_unrealized_pnl

    @property
    def current_portfolio_mtm_equity(self) -> Decimal:
        return self.exchange_margin_equity + self.eligible_vault_equity


class TradeLossComponents(BaseModel):
    model_config = ConfigDict(frozen=True)

    open_heat: Decimal = Field(ge=0)
    reserved_heat: Decimal = Field(ge=0)
    unknown_heat: Decimal = Field(ge=0)
    protected_profit_giveback: Decimal = Field(ge=0)
    cost_stress_add_on: Decimal = Field(ge=0)

    @property
    def total(self) -> Decimal:
        return (
            self.open_heat
            + self.reserved_heat
            + self.unknown_heat
            + self.protected_profit_giveback
            + self.cost_stress_add_on
        )


class RequestedRiskIncrease(BaseModel):
    model_config = ConfigDict(frozen=True)

    requested_quantity: Decimal = Field(gt=0)
    quantity_step: Decimal = Field(gt=0)
    requested_reserved_heat: Decimal = Field(gt=0)
    requested_protected_profit_giveback: Decimal = Field(ge=0)
    requested_cost_stress_add_on: Decimal = Field(ge=0)
    requested_funding: Decimal = Field(ge=0)
    requested_margin: Decimal = Field(ge=0)
    requested_effective_leverage: Decimal = Field(gt=0)
    venue_leverage_cap: Decimal = Field(gt=0)
    proposal_requested_loss_cap: Decimal = Field(gt=0)

    @property
    def incremental_worst_case_loss(self) -> Decimal:
        return (
            self.requested_reserved_heat
            + self.requested_protected_profit_giveback
            + self.requested_cost_stress_add_on
        )


class ScopeRiskInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope_type: ScopeType
    scope_id: str = Field(min_length=1, max_length=255)
    current_planned_loss: Decimal = Field(ge=0)
    requested_incremental_planned_loss: Decimal = Field(ge=0)
    current_stress_loss: Decimal = Field(ge=0)
    requested_incremental_stress_loss: Decimal = Field(ge=0)

    @property
    def key(self) -> tuple[ScopeType, str]:
        return (self.scope_type, self.scope_id)


class FactObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    fact_type: FactType
    status: FactStatus
    source_ref: str = Field(min_length=1, max_length=255)
    source_version: str = Field(min_length=1, max_length=120)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_time: datetime
    received_at: datetime

    @model_validator(mode="after")
    def timestamps_are_aware(self) -> Self:
        if self.event_time.tzinfo is None or self.received_at.tzinfo is None:
            raise ValueError("fact timestamps must be timezone-aware")
        return self


class RiskPrecheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    organization_id: str = Field(min_length=1, max_length=120)
    proposal_ref: str = Field(min_length=1, max_length=255)
    candidate_version: int = Field(ge=1)
    originating_caller_ref: str = Field(min_length=1, max_length=255)
    originating_channel: CommandChannel
    originating_auth_context_ref: str = Field(min_length=1, max_length=255)
    policy_version: str = Field(min_length=1, max_length=120)
    risk_tier: RiskTier
    binding: CertificationBinding
    market: MarketRiskInput
    capital: CapitalInput
    current_trade_loss: TradeLossComponents
    requested: RequestedRiskIncrease
    scope_risks: tuple[ScopeRiskInput, ...]
    facts: tuple[FactObservation, ...]
    instrument_classified: bool
    capability_certificate_valid: bool
    protection_available: bool

    @model_validator(mode="after")
    def exact_fact_and_scope_coverage(self) -> Self:
        fact_types = [item.fact_type for item in self.facts]
        if len(fact_types) != len(set(fact_types)):
            raise ValueError("fact observations must be unique")
        if frozenset(fact_types) != REQUIRED_FACT_TYPES:
            raise ValueError("fact observations must cover every required fact type")
        scope_keys = [item.key for item in self.scope_risks]
        if len(scope_keys) != len(set(scope_keys)):
            raise ValueError("scope risk inputs must be unique")
        if frozenset(item.scope_type for item in self.scope_risks) != REQUIRED_SCOPE_TYPES:
            raise ValueError("scope risk inputs must cover every required scope type")
        return self


class RiskEvaluationInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    request: RiskPrecheckRequest
    risk_policy_id: UUID
    policy: RiskPolicyParameters
    policy_valid_from: datetime
    policy_valid_until: datetime
    system_risk_state: SystemRiskState
    decision_time: datetime

    @model_validator(mode="after")
    def timestamps_are_aware(self) -> Self:
        if (
            self.policy_valid_from.tzinfo is None
            or self.policy_valid_until.tzinfo is None
            or self.decision_time.tzinfo is None
        ):
            raise ValueError("risk evaluation timestamps must be timezone-aware")
        return self


class ScopeRiskDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope_type: ScopeType
    scope_id: str
    planned_loss_after: Decimal
    planned_loss_cap: Decimal
    stress_loss_after: Decimal
    stress_loss_cap: Decimal
    planned_passed: bool
    stress_passed: bool


class RiskEvaluationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    result: RiskDecisionResult
    primary_reason_code: str
    reason_codes: tuple[str, ...]
    requested_quantity: Decimal
    max_safe_quantity: Decimal
    final_quantity: Decimal
    current_portfolio_mtm_equity: Decimal
    current_unrealized_pnl: Decimal
    one_r_0: Decimal
    frozen_trade_loss_cap: Decimal
    dynamic_trade_loss_cap: Decimal
    effective_trade_loss_cap: Decimal
    trade_worst_case_loss_before: Decimal
    trade_worst_case_loss_after: Decimal
    funding_envelope_0: Decimal
    funding_after: Decimal
    leverage_cap: Decimal
    scope_decisions: tuple[ScopeRiskDecision, ...]
    stale_fact_types: tuple[FactType, ...]
    unknown_fact_types: tuple[FactType, ...]
    valid_until: datetime
    execution_eligible: bool = False
    reservation_created: bool = False


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if value <= ZERO:
        return ZERO
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _capacity_ratio(limit: Decimal, current: Decimal, requested: Decimal) -> Decimal:
    remaining = max(ZERO, limit - current)
    if requested == ZERO:
        return ONE if current <= limit else ZERO
    return min(ONE, remaining / requested)


def _ordered_unique_reasons(reasons: list[str]) -> tuple[str, ...]:
    return tuple(sorted(set(reasons), key=lambda code: (REASON_RANK.get(code, 999), code)))


class RiskEvaluator:
    """Pure deterministic proposal-precheck math with no authorization or reservation effect."""

    def evaluate(self, evaluation: RiskEvaluationInput) -> RiskEvaluationResult:
        request = evaluation.request
        policy = evaluation.policy
        now = evaluation.decision_time
        reasons: list[str] = []
        hard_failure = False
        ratios: list[Decimal] = [ONE]

        if not (evaluation.policy_valid_from <= now < evaluation.policy_valid_until):
            reasons.append("RISK_POLICY_OUTSIDE_VALID_WINDOW")
            hard_failure = True

        freshness = {item.fact_type: item.max_age_ms for item in policy.fact_freshness_limits}
        unknown_facts = tuple(
            sorted(
                (item.fact_type for item in request.facts if item.status is FactStatus.UNKNOWN),
                key=lambda item: item.value,
            )
        )
        stale_facts: list[FactType] = []
        future_facts: list[FactType] = []
        invalid_order_facts: list[FactType] = []
        fact_expiries: list[datetime] = []
        for fact in request.facts:
            max_age = timedelta(milliseconds=freshness[fact.fact_type])
            max_skew = timedelta(milliseconds=policy.max_future_skew_ms)
            fact_expiries.append(fact.event_time + max_age)
            if now - fact.event_time > max_age:
                stale_facts.append(fact.fact_type)
            if fact.event_time - now > max_skew or fact.received_at - now > max_skew:
                future_facts.append(fact.fact_type)
            if fact.event_time - fact.received_at > max_skew:
                invalid_order_facts.append(fact.fact_type)

        if unknown_facts:
            reasons.append("FACTS_UNKNOWN")
            hard_failure = True
        if future_facts:
            reasons.append("FACT_TIMESTAMP_IN_FUTURE")
            hard_failure = True
        if invalid_order_facts:
            reasons.append("FACT_TIME_ORDER_INVALID")
            hard_failure = True
        if stale_facts:
            reasons.append("FACTS_STALE")
            hard_failure = True
        event_times = [item.event_time for item in request.facts]
        consistency_window = timedelta(milliseconds=policy.consistency_window_ms)
        if max(event_times) - min(event_times) > consistency_window:
            reasons.append("FACTS_INCONSISTENT")
            hard_failure = True

        current_mtm = request.capital.current_portfolio_mtm_equity
        if current_mtm <= ZERO:
            reasons.append("PORTFOLIO_EQUITY_NON_POSITIVE")
            hard_failure = True
        if not request.instrument_classified:
            reasons.append("INSTRUMENT_UNCLASSIFIED")
            hard_failure = True
        if not request.capability_certificate_valid:
            reasons.append("CAPABILITY_CERTIFICATE_INVALID")
            hard_failure = True
        if not request.protection_available:
            reasons.append("PROTECTION_UNAVAILABLE")
            hard_failure = True
        if (
            request.market.direction is PositionDirection.LONG
            and request.market.initial_invalidation_price >= request.market.executable_price
        ) or (
            request.market.direction is PositionDirection.SHORT
            and request.market.initial_invalidation_price <= request.market.executable_price
        ):
            reasons.append("INVALIDATION_PRICE_INVALID")
            hard_failure = True
        requested_notional = (
            request.requested.requested_quantity
            * request.market.executable_price
            * request.market.contract_multiplier
        )
        if (
            request.requested.requested_quantity < request.market.minimum_quantity
            or requested_notional < request.market.minimum_notional
        ):
            reasons.append("TRADING_RULE_VIOLATION")
            hard_failure = True
        if evaluation.system_risk_state not in {
            SystemRiskState.NORMAL,
            SystemRiskState.NO_PYRAMID,
        }:
            reasons.append("SYSTEM_RISK_STATE_DENY")
            hard_failure = True

        multiplier = policy.loss_multiplier(request.risk_tier)
        one_r = request.capital.total_capital_snapshot_0 * policy.one_r_fraction
        frozen_cap = one_r * multiplier
        dynamic_cap = max(ZERO, current_mtm * policy.one_r_fraction * multiplier)
        if request.requested.proposal_requested_loss_cap > frozen_cap:
            reasons.append("PROPOSAL_RISK_CAP_INVALID")
            hard_failure = True
        trade_caps = [
            frozen_cap,
            dynamic_cap,
            request.requested.proposal_requested_loss_cap,
        ]
        if policy.absolute_trade_loss_cap is not None:
            trade_caps.append(policy.absolute_trade_loss_cap)
        effective_trade_cap = min(trade_caps)

        current_trade_loss = request.current_trade_loss.total
        incremental_trade_loss = request.requested.incremental_worst_case_loss
        trade_loss_after = current_trade_loss + incremental_trade_loss
        trade_ratio = _capacity_ratio(
            effective_trade_cap,
            current_trade_loss,
            incremental_trade_loss,
        )
        ratios.append(trade_ratio)
        if trade_ratio < ONE:
            reasons.append("TRADE_LOSS_LIMIT_EXCEEDED")

        funding_envelope = request.capital.total_capital_snapshot_0 * policy.trade_funding_pct
        funding_before = request.capital.funding_used + request.capital.funding_reserved
        funding_after = funding_before + request.requested.requested_funding
        funding_ratio = _capacity_ratio(
            funding_envelope,
            funding_before,
            request.requested.requested_funding,
        )
        ratios.append(funding_ratio)
        if funding_ratio < ONE:
            reasons.append("FUNDING_ENVELOPE_EXCEEDED")

        margin_ratio = _capacity_ratio(
            request.capital.available_margin,
            ZERO,
            request.requested.requested_margin,
        )
        ratios.append(margin_ratio)
        if margin_ratio < ONE:
            reasons.append("MARGIN_INSUFFICIENT")

        leverage_cap = min(
            policy.leverage_cap(request.risk_tier),
            request.requested.venue_leverage_cap,
        )
        leverage_ratio = min(
            ONE,
            leverage_cap / request.requested.requested_effective_leverage,
        )
        ratios.append(leverage_ratio)
        if leverage_ratio < ONE:
            reasons.append("LEVERAGE_LIMIT_EXCEEDED")

        rounded_requested = _floor_to_step(
            request.requested.requested_quantity,
            request.requested.quantity_step,
        )
        quantity_ratio = rounded_requested / request.requested.requested_quantity
        ratios.append(quantity_ratio)
        if rounded_requested != request.requested.requested_quantity:
            reasons.append("QUANTITY_RULE_VIOLATION")

        limit_by_key = {item.key: item for item in policy.scope_limits}
        scope_decisions: list[ScopeRiskDecision] = []
        for scope in sorted(
            request.scope_risks,
            key=lambda item: (item.scope_type.value, item.scope_id),
        ):
            limit = limit_by_key.get(scope.key)
            if limit is None:
                reasons.append("SCOPE_POLICY_MISSING")
                hard_failure = True
                scope_decisions.append(
                    ScopeRiskDecision(
                        scope_type=scope.scope_type,
                        scope_id=scope.scope_id,
                        planned_loss_after=(
                            scope.current_planned_loss + scope.requested_incremental_planned_loss
                        ),
                        planned_loss_cap=ZERO,
                        stress_loss_after=(
                            scope.current_stress_loss + scope.requested_incremental_stress_loss
                        ),
                        stress_loss_cap=ZERO,
                        planned_passed=False,
                        stress_passed=False,
                    )
                )
                continue

            planned_after = scope.current_planned_loss + scope.requested_incremental_planned_loss
            stress_after = scope.current_stress_loss + scope.requested_incremental_stress_loss
            planned_ratio = _capacity_ratio(
                limit.planned_loss_cap,
                scope.current_planned_loss,
                scope.requested_incremental_planned_loss,
            )
            stress_ratio = _capacity_ratio(
                limit.stress_loss_cap,
                scope.current_stress_loss,
                scope.requested_incremental_stress_loss,
            )
            ratios.extend((planned_ratio, stress_ratio))
            if planned_ratio < ONE:
                reasons.append("SCOPE_PLANNED_LIMIT_EXCEEDED")
            if stress_ratio < ONE:
                reasons.append("SCOPE_STRESS_LIMIT_EXCEEDED")
            scope_decisions.append(
                ScopeRiskDecision(
                    scope_type=scope.scope_type,
                    scope_id=scope.scope_id,
                    planned_loss_after=planned_after,
                    planned_loss_cap=limit.planned_loss_cap,
                    stress_loss_after=stress_after,
                    stress_loss_cap=limit.stress_loss_cap,
                    planned_passed=planned_after <= limit.planned_loss_cap,
                    stress_passed=stress_after <= limit.stress_loss_cap,
                )
            )

        safe_ratio = ZERO if hard_failure else min(ratios)
        max_safe_quantity = _floor_to_step(
            request.requested.requested_quantity * safe_ratio,
            request.requested.quantity_step,
        )
        max_safe_quantity = min(request.requested.requested_quantity, max_safe_quantity)
        ordered_reasons = _ordered_unique_reasons(reasons)
        allowed = not ordered_reasons
        result = RiskDecisionResult.ALLOW if allowed else RiskDecisionResult.DENY
        valid_until_candidate = min([evaluation.policy_valid_until, *fact_expiries])
        valid_until = max(now, valid_until_candidate) if allowed else now

        return RiskEvaluationResult(
            result=result,
            primary_reason_code=("RISK_PRECHECK_PASSED" if allowed else ordered_reasons[0]),
            reason_codes=ordered_reasons,
            requested_quantity=request.requested.requested_quantity,
            max_safe_quantity=(
                request.requested.requested_quantity if allowed else max_safe_quantity
            ),
            final_quantity=(request.requested.requested_quantity if allowed else ZERO),
            current_portfolio_mtm_equity=current_mtm,
            current_unrealized_pnl=request.capital.current_unrealized_pnl,
            one_r_0=one_r,
            frozen_trade_loss_cap=frozen_cap,
            dynamic_trade_loss_cap=dynamic_cap,
            effective_trade_loss_cap=effective_trade_cap,
            trade_worst_case_loss_before=current_trade_loss,
            trade_worst_case_loss_after=trade_loss_after,
            funding_envelope_0=funding_envelope,
            funding_after=funding_after,
            leverage_cap=leverage_cap,
            scope_decisions=tuple(scope_decisions),
            stale_fact_types=tuple(sorted(set(stale_facts), key=lambda item: item.value)),
            unknown_fact_types=unknown_facts,
            valid_until=valid_until,
        )


class RiskPrecheckService:
    """Persists one immutable shadow precheck and emits audit/outbox evidence."""

    command_type = "risk.precheck.evaluate.v1"

    def __init__(self, evaluator: RiskEvaluator | None = None) -> None:
        self._evaluator = evaluator or RiskEvaluator()

    def evaluate(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        started = time.monotonic()
        if envelope.command_type != self.command_type:
            raise CommandRejected("COMMAND_TYPE_MISMATCH", "unexpected command type")
        if envelope.channel is not CommandChannel.INTERNAL or envelope.service_principal is None:
            raise CommandRejected(
                "INTERNAL_SERVICE_REQUIRED",
                "risk precheck is callable only from an internal Trading service",
            )
        if envelope.object_type != "ProposalCandidate" or envelope.object_id is None:
            raise CommandRejected(
                "OBJECT_BINDING_MISMATCH", "ProposalCandidate binding is required"
            )
        try:
            request = RiskPrecheckRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("RISK_INPUT_INVALID", "risk precheck input is invalid") from exc
        if envelope.object_id != request.proposal_ref:
            raise CommandRejected("OBJECT_BINDING_MISMATCH", "proposal reference changed")
        if envelope.expected_version != request.candidate_version:
            raise CommandRejected("VERSION_CONFLICT", "proposal candidate version changed")
        if envelope.scope.get("organization_id") != request.organization_id:
            raise CommandRejected("SCOPE_MISMATCH", "organization scope changed")

        policy_record = session.execute(
            select(RiskPolicyRecord).where(
                RiskPolicyRecord.organization_id == request.organization_id,
                RiskPolicyRecord.policy_version == request.policy_version,
            )
        ).scalar_one_or_none()
        if policy_record is None:
            raise CommandRejected("RISK_POLICY_UNAVAILABLE", "risk policy is unavailable")
        if (
            policy_record.policy_mode != "SHADOW"
            or hash_json(policy_record.parameters) != policy_record.policy_hash
            or not policy_record.evidence_refs
            or not all(
                isinstance(reference, str) and bool(reference)
                for reference in policy_record.evidence_refs
            )
        ):
            raise CommandRejected(
                "RISK_POLICY_INTEGRITY_FAILED", "risk policy integrity check failed"
            )
        try:
            policy = RiskPolicyParameters.model_validate(policy_record.parameters)
        except ValidationError as exc:
            raise CommandRejected(
                "RISK_POLICY_INVALID", "risk policy violates the fixed business contract"
            ) from exc

        state_record = session.execute(
            select(SystemRiskStateRecord)
            .where(SystemRiskStateRecord.organization_id == request.organization_id)
            .with_for_update()
        ).scalar_one_or_none()
        system_state = (
            SystemRiskState(state_record.status)
            if state_record is not None
            else SystemRiskState.UNKNOWN
        )
        decided_at = datetime.now(UTC)
        evaluation_input = RiskEvaluationInput(
            request=request,
            risk_policy_id=policy_record.risk_policy_id,
            policy=policy,
            policy_valid_from=policy_record.valid_from,
            policy_valid_until=policy_record.valid_until,
            system_risk_state=system_state,
            decision_time=decided_at,
        )
        result = self._evaluator.evaluate(evaluation_input)
        input_snapshot = evaluation_input.model_dump(mode="json")
        input_snapshot["policy_record"] = {
            "policy_mode": policy_record.policy_mode,
            "policy_hash": policy_record.policy_hash,
            "evidence_refs": policy_record.evidence_refs,
        }
        input_snapshot["decision_context"] = {
            "command_id": str(envelope.command_id),
            "correlation_id": str(envelope.correlation_id),
            "caller_id": envelope.caller_id,
            "channel": envelope.channel.value,
            "auth_context_ref": envelope.auth_context_ref,
        }
        input_snapshot["derived_capital"] = {
            "exchange_margin_equity": str(request.capital.exchange_margin_equity),
            "current_portfolio_mtm_equity": str(request.capital.current_portfolio_mtm_equity),
        }
        input_hash = hash_json(input_snapshot)
        decision = result.model_dump(mode="json")
        decision_hash = hash_json(decision)
        decision_id = uuid4()
        session.add(
            RiskDecisionSnapshot(
                risk_decision_id=decision_id,
                organization_id=request.organization_id,
                proposal_ref=request.proposal_ref,
                decision_stage="PROPOSAL_PRECHECK",
                result=result.result.value,
                primary_reason_code=result.primary_reason_code,
                risk_tier=request.risk_tier.value,
                system_risk_state=system_state.value,
                risk_policy_id=policy_record.risk_policy_id,
                risk_policy_version=policy_record.policy_version,
                requested_quantity=result.requested_quantity,
                max_safe_quantity=result.max_safe_quantity,
                final_quantity=result.final_quantity,
                current_unrealized_pnl=result.current_unrealized_pnl,
                current_portfolio_mtm_equity=result.current_portfolio_mtm_equity,
                total_capital_snapshot_0=request.capital.total_capital_snapshot_0,
                one_r_0=result.one_r_0,
                frozen_trade_loss_cap=result.frozen_trade_loss_cap,
                dynamic_trade_loss_cap=result.dynamic_trade_loss_cap,
                effective_trade_loss_cap=result.effective_trade_loss_cap,
                trade_worst_case_loss_before=result.trade_worst_case_loss_before,
                trade_worst_case_loss_after=result.trade_worst_case_loss_after,
                input_snapshot=input_snapshot,
                input_hash=input_hash,
                decision=decision,
                decision_hash=decision_hash,
                execution_eligible=False,
                reservation_created=False,
                decided_at=decided_at,
                valid_until=result.valid_until,
            )
        )
        session.flush()

        RISK_DECISIONS.labels(result.result.value, result.primary_reason_code).inc()
        if result.stale_fact_types:
            RISK_STALE_INPUTS.inc()
        RISK_DECISION_DURATION.observe(time.monotonic() - started)
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="RiskDecision",
            object_id=str(decision_id),
            object_version=1,
            data={
                "risk_decision_id": str(decision_id),
                "decision_stage": "PROPOSAL_PRECHECK",
                "result": result.result.value,
                "primary_reason_code": result.primary_reason_code,
                "reason_codes": list(result.reason_codes),
                "requested_quantity": str(result.requested_quantity),
                "max_safe_quantity": str(result.max_safe_quantity),
                "final_quantity": str(result.final_quantity),
                "input_hash": input_hash,
                "decision_hash": decision_hash,
                "valid_until": result.valid_until.isoformat(),
                "execution_eligible": False,
                "reservation_created": False,
            },
            events=(
                DomainEvent(
                    event_type="RiskPrecheckDecisionRecorded",
                    aggregate_type="RiskDecision",
                    aggregate_id=str(decision_id),
                    payload={
                        "proposal_ref": request.proposal_ref,
                        "result": result.result.value,
                        "primary_reason_code": result.primary_reason_code,
                        "input_hash": input_hash,
                        "decision_hash": decision_hash,
                        "execution_eligible": False,
                        "reservation_created": False,
                    },
                ),
            ),
        )


class RiskStateTighteningResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    organization_id: str
    previous_status: SystemRiskState
    current_status: SystemRiskState
    version: int
    changed: bool


class RiskStateTightenRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    organization_id: str = Field(min_length=1, max_length=120)
    target_status: SystemRiskState
    reason_code: str = Field(min_length=1, max_length=160)
    policy_version: str = Field(min_length=1, max_length=120)
    source_ref: str = Field(min_length=1, max_length=255)


class SystemRiskStateService:
    """Only automatic tightening is implemented; recovery remains a separate future path."""

    def tighten(
        self,
        session: Session,
        *,
        organization_id: str,
        target_status: SystemRiskState,
        reason_code: str,
        policy_version: str,
        source_ref: str,
        changed_at: datetime | None = None,
        expected_version: int | None = None,
    ) -> RiskStateTighteningResult:
        if not reason_code or not policy_version or not source_ref:
            raise CommandRejected("RISK_STATE_INPUT_INVALID", "state evidence is required")
        if target_status in {SystemRiskState.NORMAL, SystemRiskState.UNKNOWN}:
            raise CommandRejected(
                "RISK_STATE_TARGET_INVALID",
                "automatic transitions require a concrete tighter state",
            )
        state = session.execute(
            select(SystemRiskStateRecord)
            .where(SystemRiskStateRecord.organization_id == organization_id)
            .with_for_update()
        ).scalar_one_or_none()
        if state is None:
            raise CommandRejected(
                "SYSTEM_RISK_STATE_UNKNOWN", "missing state cannot be automatically relaxed"
            )
        if expected_version is not None and state.version != expected_version:
            raise CommandRejected("VERSION_CONFLICT", "system risk state version changed")
        current = SystemRiskState(state.status)
        if RISK_STATE_RANK[target_status] < RISK_STATE_RANK[current]:
            raise CommandRejected(
                "RISK_STATE_RELAXATION_REQUIRES_MANUAL_RECOVERY",
                "automatic risk state recovery is forbidden",
            )
        if target_status is current:
            return RiskStateTighteningResult(
                organization_id=organization_id,
                previous_status=current,
                current_status=current,
                version=state.version,
                changed=False,
            )

        previous = current
        state.status = target_status.value
        state.version += 1
        state.reason_code = reason_code
        state.policy_version = policy_version
        state.transition_source_ref = source_ref
        state.updated_at = changed_at or datetime.now(UTC)
        session.flush()
        SYSTEM_RISK_STATE_TRANSITIONS.labels(previous.value, target_status.value).inc()
        return RiskStateTighteningResult(
            organization_id=organization_id,
            previous_status=previous,
            current_status=target_status,
            version=state.version,
            changed=True,
        )


class SystemRiskStateCommandService:
    """Internal durable command wrapper for state history, audit, and outbox atomicity."""

    command_type = "risk.state.tighten.v1"

    def __init__(self, state_service: SystemRiskStateService | None = None) -> None:
        self._state_service = state_service or SystemRiskStateService()

    def tighten(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        if envelope.command_type != self.command_type:
            raise CommandRejected("COMMAND_TYPE_MISMATCH", "unexpected command type")
        if envelope.channel is not CommandChannel.INTERNAL or envelope.service_principal is None:
            raise CommandRejected(
                "INTERNAL_SERVICE_REQUIRED",
                "automatic risk-state tightening requires an internal service",
            )
        if envelope.object_type != "SystemRiskState" or envelope.object_id is None:
            raise CommandRejected("OBJECT_BINDING_MISMATCH", "SystemRiskState binding is required")
        try:
            request = RiskStateTightenRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("RISK_STATE_INPUT_INVALID", "state input is invalid") from exc
        if envelope.object_id != request.organization_id:
            raise CommandRejected("OBJECT_BINDING_MISMATCH", "organization binding changed")
        if envelope.scope.get("organization_id") != request.organization_id:
            raise CommandRejected("SCOPE_MISMATCH", "organization scope changed")
        if envelope.expected_version is None:
            raise CommandRejected("VERSION_REQUIRED", "current state version is required")

        result = self._state_service.tighten(
            session,
            organization_id=request.organization_id,
            target_status=request.target_status,
            reason_code=request.reason_code,
            policy_version=request.policy_version,
            source_ref=request.source_ref,
            expected_version=envelope.expected_version,
        )
        event_type = (
            "SystemRiskStateTightened"
            if result.changed
            else "SystemRiskStateTighteningAlreadyApplied"
        )
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="SystemRiskState",
            object_id=request.organization_id,
            object_version=result.version,
            data={
                "previous_status": result.previous_status.value,
                "current_status": result.current_status.value,
                "version": result.version,
                "changed": result.changed,
            },
            events=(
                DomainEvent(
                    event_type=event_type,
                    aggregate_type="SystemRiskState",
                    aggregate_id=request.organization_id,
                    payload={
                        "previous_status": result.previous_status.value,
                        "current_status": result.current_status.value,
                        "version": result.version,
                        "reason_code": request.reason_code,
                        "policy_version": request.policy_version,
                        "source_ref": request.source_ref,
                        "changed": result.changed,
                    },
                ),
            ),
        )
