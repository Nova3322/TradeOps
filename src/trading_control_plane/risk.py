from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_control_plane.authorization import RiskTier, SystemRiskState
from trading_control_plane.capability_certificates import (
    CapabilityCertificateValidator,
    CapabilityPolicyVersions,
    CapabilityScope,
    CapabilityValidationRequest,
    CapabilityValidationResult,
)
from trading_control_plane.capital_scope import (
    PortfolioMtmProjection,
    PortfolioMtmProjectionService,
    PortfolioMtmQuery,
    PortfolioMtmState,
)
from trading_control_plane.commands import (
    CommandChannel,
    CommandEnvelope,
    CommandOutcome,
    CommandRejected,
    CommandStatus,
    DomainEvent,
    hash_json,
)
from trading_control_plane.durable_exposure import (
    DurableExposureSnapshot,
    DurableExposureSnapshotService,
)
from trading_control_plane.instrument_catalog import (
    InstrumentCatalogValidator,
    InstrumentClassificationValidationRequest,
    InstrumentClassificationValidationResult,
)
from trading_control_plane.metrics import (
    RISK_DECISION_DURATION,
    RISK_DECISIONS,
    RISK_STALE_INPUTS,
    SYSTEM_RISK_STATE_TRANSITIONS,
)
from trading_control_plane.projections import (
    CurrentProtectedPositionRiskProjection,
    ProjectionQueryContext,
    ProjectionState,
)
from trading_control_plane.proposal_models import SystemRiskStateRecord
from trading_control_plane.protection_capability import (
    ProtectionCapabilityValidationRequest,
    ProtectionCapabilityValidationResult,
    ProtectionCapabilityValidator,
)
from trading_control_plane.risk_fact_sets import (
    RiskFactSetValidationRequest,
    RiskFactSetValidationResult,
    RiskFactSetValidator,
)
from trading_control_plane.risk_facts import (
    REQUIRED_FACT_TYPES,
    FactStatus,
    FactType,
)
from trading_control_plane.risk_models import (
    RiskDecisionSnapshot,
    RiskPolicyRecord,
)

ZERO = Decimal("0")
ONE = Decimal("1")
ONE_R_FRACTION = Decimal("0.005")
RISK_AMOUNT_QUANTUM = Decimal("0.000000000000000001")
CANONICAL_LOSS_MODEL_VERSION = "directional-entry-to-invalidation-v1"
CANONICAL_COST_STRESS_MODEL_VERSION = "fee-stop-funding-stress-v1"
CANONICAL_SCOPE_STRESS_MODEL_VERSION = "planned-loss-plus-scope-shocks-v1"


class RiskDecisionResult(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    ALLOW_WITH_CAP = "ALLOW_WITH_CAP"


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
    "RISK_FACT_SET_UNAVAILABLE",
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


class ScopeStressPolicyParameters(BaseModel):
    """Per-scope frozen stress scenario; intentionally has no production defaults."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_version: Literal["planned-loss-plus-scope-shocks-v1"]
    gap_bps: Decimal = Field(ge=0)
    liquidity_degradation_bps: Decimal = Field(ge=0)
    unprotected_window_bps: Decimal = Field(ge=0)
    source_ref: str = Field(min_length=1, max_length=255)


class ScopeLimit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope_type: ScopeType
    scope_id: str = Field(min_length=1, max_length=255)
    planned_loss_cap: Decimal = Field(ge=0)
    stress_loss_cap: Decimal = Field(ge=0)
    stress_scenario: ScopeStressPolicyParameters

    @property
    def key(self) -> tuple[ScopeType, str]:
        return (self.scope_type, self.scope_id)


class CostStressPolicyParameters(BaseModel):
    """Versioned research inputs; intentionally has no production defaults."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_version: Literal["fee-stop-funding-stress-v1"]
    round_trip_fee_bps: Decimal = Field(ge=0)
    stop_penetration_bps: Decimal = Field(ge=0)
    funding_interval_count: int = Field(ge=0)
    source_ref: str = Field(min_length=1, max_length=255)


class RiskPolicyParameters(BaseModel):
    """Every research-dependent value is explicit; there are no production defaults."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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
    cost_stress: CostStressPolicyParameters
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
    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_source: str = Field(pattern=r"^(SYSTEM|MANUAL)$")
    strategy_id: str = Field(min_length=1, max_length=160)
    strategy_version: str = Field(min_length=1, max_length=120)
    strategy_parameter_version: str = Field(min_length=1, max_length=120)
    authorization_policy_version: str = Field(min_length=1, max_length=120)
    instrument_identity: str = Field(min_length=1, max_length=255)
    contract_multiplier: Decimal = Field(gt=0)
    underlying_id: str = Field(min_length=1, max_length=160)
    sector_id: str = Field(min_length=1, max_length=160)
    risk_cluster_id: str = Field(min_length=1, max_length=160)
    venue: str = Field(min_length=1, max_length=80)
    execution_domain: str = Field(min_length=1, max_length=120)
    account_id: str = Field(min_length=1, max_length=160)
    account_abstraction: str = Field(min_length=1, max_length=80)
    position_mode: str = Field(min_length=1, max_length=80)
    margin_mode: str = Field(min_length=1, max_length=80)
    collateral_scope: str = Field(min_length=1, max_length=120)
    collateral_pool_id: str = Field(min_length=1, max_length=160)
    settlement_asset: str = Field(min_length=1, max_length=80)
    adapter_version: str = Field(min_length=1, max_length=120)
    worker_id: str = Field(min_length=1, max_length=160)
    worker_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    credential_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    freqtrade_worker_version: str = Field(min_length=1, max_length=120)
    account_capability_version: str = Field(min_length=1, max_length=120)
    credential_permission_profile_version: str = Field(min_length=1, max_length=120)
    venue_client_version: str = Field(min_length=1, max_length=120)
    instrument_scope_version: str = Field(min_length=1, max_length=120)
    catalog_version: str = Field(min_length=1, max_length=120)
    execution_capability_version: str = Field(min_length=1, max_length=120)
    position_management_template_version: str = Field(min_length=1, max_length=120)
    add_milestone_policy_version: str = Field(min_length=1, max_length=120)
    requested_add_count: int = Field(ge=0, le=3)
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
    loss_model_version: Literal["directional-entry-to-invalidation-v1"]
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


class CapitalProjectionBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_id: UUID
    manifest_version: int = Field(ge=1)
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    projection_version: str = Field(pattern=r"^portfolio-mtm-v[0-9]+$")


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
    model_config = ConfigDict(frozen=True, extra="forbid")

    requested_quantity: Decimal = Field(gt=0)
    quantity_step: Decimal = Field(gt=0)
    requested_funding: Decimal = Field(ge=0)
    requested_margin: Decimal = Field(ge=0)
    requested_effective_leverage: Decimal = Field(gt=0)
    venue_leverage_cap: Decimal = Field(gt=0)
    proposal_requested_loss_cap: Decimal = Field(gt=0)


class ScopeRiskInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope_type: ScopeType
    scope_id: str = Field(min_length=1, max_length=255)
    current_planned_loss: Decimal = Field(ge=0)
    current_stress_loss: Decimal = Field(ge=0)

    @property
    def key(self) -> tuple[ScopeType, str]:
        return (self.scope_type, self.scope_id)


class RiskPrecheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

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
    capital_projection_binding: CapitalProjectionBinding
    capital: CapitalInput
    current_trade_loss: TradeLossComponents
    requested: RequestedRiskIncrease
    scope_risks: tuple[ScopeRiskInput, ...]

    @model_validator(mode="after")
    def exact_scope_coverage(self) -> Self:
        scope_keys = [item.key for item in self.scope_risks]
        if len(scope_keys) != len(set(scope_keys)):
            raise ValueError("scope risk inputs must be unique")
        if frozenset(item.scope_type for item in self.scope_risks) != REQUIRED_SCOPE_TYPES:
            raise ValueError("scope risk inputs must cover every required scope type")
        if self.market.contract_multiplier != self.binding.contract_multiplier:
            raise ValueError("market contract multiplier must match the certified binding")
        return self

    @property
    def requested_base_heat(self) -> Decimal:
        """Canonical directional loss from executable entry to frozen invalidation."""

        return _quantize_risk_amount(
            abs(self.market.executable_price - self.market.initial_invalidation_price)
            * self.requested.requested_quantity
            * self.binding.contract_multiplier
        )


def instrument_classification_validation_request(
    request: RiskPrecheckRequest,
    validation_time: datetime,
) -> InstrumentClassificationValidationRequest:
    binding = request.binding
    return InstrumentClassificationValidationRequest(
        organization_id=request.organization_id,
        venue=binding.venue,
        execution_domain=binding.execution_domain,
        canonical_instrument_id=binding.instrument_identity,
        catalog_version=binding.catalog_version,
        classification_version=binding.instrument_scope_version,
        expected_underlying_id=binding.underlying_id,
        expected_sector=binding.sector_id,
        expected_risk_cluster_id=binding.risk_cluster_id,
        expected_settlement_asset=binding.settlement_asset,
        expected_contract_multiplier=binding.contract_multiplier,
        validation_time=validation_time,
    )


def protection_capability_validation_request(
    request: RiskPrecheckRequest,
    instrument_classification: InstrumentClassificationValidationResult,
    validation_time: datetime,
) -> ProtectionCapabilityValidationRequest:
    binding = request.binding
    catalog_valid = instrument_classification.valid
    return ProtectionCapabilityValidationRequest(
        organization_id=request.organization_id,
        expected_catalog_record_id=(
            instrument_classification.catalog_record_id if catalog_valid else None
        ),
        expected_catalog_record_hash=(
            instrument_classification.record_hash if catalog_valid else None
        ),
        catalog_version=binding.catalog_version,
        classification_version=binding.instrument_scope_version,
        venue=binding.venue,
        execution_domain=binding.execution_domain,
        canonical_instrument_id=binding.instrument_identity,
        account_id=binding.account_id,
        expected_account_abstraction=binding.account_abstraction,
        position_mode=binding.position_mode,
        margin_mode=binding.margin_mode,
        expected_collateral_scope=binding.collateral_scope,
        collateral_pool_id=binding.collateral_pool_id,
        position_management_template_version=(binding.position_management_template_version),
        expected_execution_capability_version=(binding.execution_capability_version),
        expected_adapter_version=binding.adapter_version,
        expected_worker_id=binding.worker_id,
        expected_worker_config_hash=binding.worker_config_hash,
        expected_credential_fingerprint=binding.credential_fingerprint,
        expected_freqtrade_worker_version=binding.freqtrade_worker_version,
        expected_account_capability_version=binding.account_capability_version,
        expected_credential_permission_profile_version=(
            binding.credential_permission_profile_version
        ),
        expected_venue_client_version=binding.venue_client_version,
        validation_time=validation_time,
    )


def risk_fact_set_validation_request(
    request: RiskPrecheckRequest,
    validation_time: datetime,
) -> RiskFactSetValidationRequest:
    binding = request.binding
    return RiskFactSetValidationRequest(
        organization_id=request.organization_id,
        venue=binding.venue,
        execution_domain=binding.execution_domain,
        account_id=binding.account_id,
        canonical_instrument_id=binding.instrument_identity,
        position_mode=binding.position_mode,
        margin_mode=binding.margin_mode,
        collateral_pool_id=binding.collateral_pool_id,
        validation_time=validation_time,
    )


class CostStressBreakdown(BaseModel):
    model_config = ConfigDict(frozen=True)

    fee_stress: Decimal = Field(ge=0)
    stop_penetration_stress: Decimal = Field(ge=0)
    adverse_funding_stress: Decimal = Field(ge=0)

    @property
    def total(self) -> Decimal:
        return self.fee_stress + self.stop_penetration_stress + self.adverse_funding_stress


class ScopeStressBreakdown(BaseModel):
    model_config = ConfigDict(frozen=True)

    gap_stress: Decimal = Field(ge=0)
    liquidity_degradation_stress: Decimal = Field(ge=0)
    unprotected_window_stress: Decimal = Field(ge=0)

    @property
    def scenario_add_on(self) -> Decimal:
        return self.gap_stress + self.liquidity_degradation_stress + self.unprotected_window_stress


def _quantize_risk_amount(value: Decimal) -> Decimal:
    if value == ZERO:
        return ZERO
    with localcontext() as context:
        context.prec = 80
        return value.quantize(RISK_AMOUNT_QUANTUM, rounding=ROUND_CEILING)


def derive_cost_stress(
    request: RiskPrecheckRequest,
    policy: RiskPolicyParameters,
) -> CostStressBreakdown:
    parameters = policy.cost_stress
    notional = (
        request.requested.requested_quantity
        * request.market.executable_price
        * request.binding.contract_multiplier
    )
    if request.market.direction is PositionDirection.LONG:
        adverse_funding_rate = max(ZERO, request.market.funding_rate)
    else:
        adverse_funding_rate = max(ZERO, -request.market.funding_rate)
    return CostStressBreakdown(
        fee_stress=_quantize_risk_amount(
            notional * parameters.round_trip_fee_bps / Decimal("10000")
        ),
        stop_penetration_stress=_quantize_risk_amount(
            notional * parameters.stop_penetration_bps / Decimal("10000")
        ),
        adverse_funding_stress=_quantize_risk_amount(
            notional * adverse_funding_rate * parameters.funding_interval_count
        ),
    )


def derive_scope_stress(
    request: RiskPrecheckRequest,
    parameters: ScopeStressPolicyParameters,
) -> ScopeStressBreakdown:
    """Derive scope-only shocks from the same requested notional as planned loss."""

    notional = (
        request.requested.requested_quantity
        * request.market.executable_price
        * request.binding.contract_multiplier
    )
    return ScopeStressBreakdown(
        gap_stress=_quantize_risk_amount(notional * parameters.gap_bps / Decimal("10000")),
        liquidity_degradation_stress=_quantize_risk_amount(
            notional * parameters.liquidity_degradation_bps / Decimal("10000")
        ),
        unprotected_window_stress=_quantize_risk_amount(
            notional * parameters.unprotected_window_bps / Decimal("10000")
        ),
    )


class VerifiedProtectedPositionRisk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    projection: CurrentProtectedPositionRiskProjection
    max_age_ms: int = Field(gt=0)
    valid_until: datetime

    @model_validator(mode="after")
    def projection_is_confirmed_and_time_bounded(self) -> Self:
        if self.valid_until.tzinfo is None or self.valid_until.utcoffset() is None:
            raise ValueError("protected-position risk expiry must be timezone-aware")
        if (
            self.projection.projection_state is not ProjectionState.CONFIRMED
            or self.projection.facts_as_of is None
            or self.valid_until
            != self.projection.facts_as_of + timedelta(milliseconds=self.max_age_ms)
        ):
            raise ValueError("protected-position risk must have a canonical expiry")
        return self


class RiskEvaluationInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    request: RiskPrecheckRequest
    risk_policy_id: UUID
    policy: RiskPolicyParameters
    policy_valid_from: datetime
    policy_valid_until: datetime
    system_risk_state: SystemRiskState
    capability_validation: CapabilityValidationResult
    instrument_classification: InstrumentClassificationValidationResult
    protection_capability: ProtectionCapabilityValidationResult
    risk_fact_set: RiskFactSetValidationResult
    decision_time: datetime
    protected_position_risk: VerifiedProtectedPositionRisk | None = None

    @model_validator(mode="after")
    def timestamps_are_aware(self) -> Self:
        if (
            self.policy_valid_from.tzinfo is None
            or self.policy_valid_until.tzinfo is None
            or self.decision_time.tzinfo is None
        ):
            raise ValueError("risk evaluation timestamps must be timezone-aware")
        protected = self.protected_position_risk
        if protected is not None:
            projection = protected.projection
            binding = self.request.binding
            if (
                projection.projection_state is not ProjectionState.CONFIRMED
                or projection.scope.organization_id != self.request.organization_id
                or projection.scope.venue != binding.venue
                or projection.scope.execution_domain != binding.execution_domain
                or projection.scope.account_id != binding.account_id
                or projection.scope.instrument_id != binding.instrument_identity
                or projection.scope.position_mode != binding.position_mode
                or projection.scope.margin_mode != binding.margin_mode
                or projection.scope.collateral_pool_id != binding.collateral_pool_id
                or projection.scope.settlement_currency != binding.settlement_asset
                or projection.direction.value != self.request.market.direction.value
                or projection.mark_price != self.request.market.mark_price
                or projection.contract_multiplier != binding.contract_multiplier
                or self.request.current_trade_loss.open_heat != projection.open_heat
                or self.request.current_trade_loss.protected_profit_giveback
                != projection.protected_profit_giveback
            ):
                raise ValueError("protected-position risk binding is inconsistent")
        return self


class ScopeRiskDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope_type: ScopeType
    scope_id: str
    incremental_planned_loss: Decimal = Field(ge=0)
    incremental_stress_loss: Decimal = Field(ge=0)
    gap_stress_add_on: Decimal = Field(ge=0)
    liquidity_degradation_stress_add_on: Decimal = Field(ge=0)
    unprotected_window_stress_add_on: Decimal = Field(ge=0)
    scope_stress_model_version: str | None
    scope_stress_source_ref: str | None
    planned_loss_after: Decimal
    planned_loss_cap: Decimal
    stress_loss_after: Decimal
    stress_loss_cap: Decimal
    planned_passed: bool
    stress_passed: bool

    @model_validator(mode="after")
    def derived_amounts_and_outcomes_are_consistent(self) -> Self:
        scenario_add_on = (
            self.gap_stress_add_on
            + self.liquidity_degradation_stress_add_on
            + self.unprotected_window_stress_add_on
        )
        if self.incremental_stress_loss != self.incremental_planned_loss + scenario_add_on:
            raise ValueError("scope stress loss must equal planned loss plus scenario add-ons")
        if (self.scope_stress_model_version is None) != (self.scope_stress_source_ref is None):
            raise ValueError("scope stress model and source must be present together")
        if self.scope_stress_model_version is None and scenario_add_on != ZERO:
            raise ValueError("scope stress add-ons require a versioned scenario")
        if self.planned_passed != (self.planned_loss_after <= self.planned_loss_cap):
            raise ValueError("scope planned outcome is inconsistent with its cap")
        if self.stress_passed != (self.stress_loss_after <= self.stress_loss_cap):
            raise ValueError("scope stress outcome is inconsistent with its cap")
        return self


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
    current_trade_loss: TradeLossComponents
    current_protected_position_risk_calculation_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    catalog_record_id: UUID | None
    catalog_record_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    catalog_evidence_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    catalog_validation_reason_codes: tuple[str, ...]
    protection_capability_record_id: UUID | None
    protection_capability_record_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    protection_capability_evidence_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    protection_capability_reason_codes: tuple[str, ...]
    risk_fact_set_id: UUID | None
    risk_fact_set_version: str | None
    risk_fact_set_record_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    risk_fact_set_evidence_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    risk_fact_set_reason_codes: tuple[str, ...]
    requested_base_heat: Decimal
    requested_fee_stress: Decimal
    requested_stop_penetration_stress: Decimal
    requested_adverse_funding_stress: Decimal
    requested_cost_stress_add_on: Decimal
    requested_incremental_worst_case_loss: Decimal
    cost_stress_model_version: str
    funding_envelope_0: Decimal
    funding_after: Decimal
    leverage_cap: Decimal
    scope_decisions: tuple[ScopeRiskDecision, ...]
    stale_fact_types: tuple[FactType, ...]
    unknown_fact_types: tuple[FactType, ...]
    valid_until: datetime
    execution_eligible: bool = False
    reservation_created: bool = False


class VerifiedCapitalProjection(BaseModel):
    model_config = ConfigDict(frozen=True)

    capital: CapitalInput
    projection: PortfolioMtmProjection
    projection_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class VerifiedProposalDurableExposure(BaseModel):
    model_config = ConfigDict(frozen=True)

    risk_request: RiskPrecheckRequest
    snapshot: DurableExposureSnapshot
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class CapitalProjectionResolver:
    """Recomputes current capital from exact canonical scope/facts inside the transaction."""

    @staticmethod
    def resolve(
        session: Session,
        request: RiskPrecheckRequest,
        policy: RiskPolicyParameters,
        as_of: datetime,
        *,
        frozen_total_capital_snapshot_0: Decimal | None = None,
    ) -> VerifiedCapitalProjection:
        account_max_age_ms = next(
            item.max_age_ms
            for item in policy.fact_freshness_limits
            if item.fact_type is FactType.ACCOUNT
        )
        binding = request.capital_projection_binding
        projection = PortfolioMtmProjectionService.query(
            session,
            PortfolioMtmQuery(
                manifest_id=binding.manifest_id,
                organization_id=request.organization_id,
                manifest_version=binding.manifest_version,
                context=ProjectionQueryContext(
                    as_of=as_of,
                    max_age_ms=account_max_age_ms,
                ),
            ),
        )
        if projection.projection_state is not PortfolioMtmState.CONFIRMED:
            error_code = {
                "FX_FACTS_REQUIRED": "CAPITAL_FX_FACTS_REQUIRED",
                "ACCOUNT_SCOPE_INCOMPLETE": "CAPITAL_ACCOUNT_SCOPE_INCOMPLETE",
            }.get(projection.reason_code or "UNKNOWN", "CAPITAL_PROJECTION_UNAVAILABLE")
            raise CommandRejected(
                error_code,
                f"capital projection unavailable: {projection.reason_code}",
            )
        if (
            projection.manifest_hash != binding.manifest_hash
            or projection.projection_version != binding.projection_version
        ):
            raise CommandRejected(
                "CAPITAL_PROJECTION_BINDING_MISMATCH",
                "capital manifest hash or projection version changed",
            )
        trade_binding = request.binding
        if not any(
            component.scope.venue == trade_binding.venue
            and component.scope.execution_domain == trade_binding.execution_domain
            and component.scope.account_id == trade_binding.account_id
            and component.scope.margin_mode == trade_binding.margin_mode
            and component.scope.collateral_pool_id == trade_binding.collateral_pool_id
            and component.scope.settlement_currency == trade_binding.settlement_asset
            for component in projection.account_components
        ):
            raise CommandRejected(
                "CAPITAL_TRADE_ACCOUNT_OUTSIDE_MANIFEST",
                "proposal trading account is not an exact member of the capital manifest",
            )
        if (
            projection.risk_inclusion_mode.value != RiskInclusionMode.EXCHANGE_ONLY.value
            or projection.report_currency != "USD"
            or projection.exchange_margin_equity is None
            or projection.current_unrealized_pnl is None
            or projection.available_margin is None
            or projection.eligible_vault_equity is None
            or projection.current_portfolio_mtm_equity is None
        ):
            raise CommandRejected(
                "CAPITAL_PROJECTION_INTEGRITY_FAILED",
                "confirmed capital projection violates risk input semantics",
            )

        exchange_settled = projection.exchange_margin_equity - projection.current_unrealized_pnl
        exchange_risk_equity = max(
            ZERO,
            min(exchange_settled, projection.exchange_margin_equity),
        )
        derived = CapitalInput(
            risk_inclusion_mode=RiskInclusionMode.EXCHANGE_ONLY,
            exchange_settled_equity_ex_upnl=exchange_settled,
            current_unrealized_pnl=projection.current_unrealized_pnl,
            eligible_vault_equity=projection.eligible_vault_equity,
            exchange_risk_equity=exchange_risk_equity,
            total_capital_snapshot_0=(
                projection.current_portfolio_mtm_equity
                if frozen_total_capital_snapshot_0 is None
                else frozen_total_capital_snapshot_0
            ),
            funding_used=request.capital.funding_used,
            funding_reserved=request.capital.funding_reserved,
            available_margin=projection.available_margin,
        )
        if derived != request.capital:
            raise CommandRejected(
                "CAPITAL_INPUT_MISMATCH",
                "caller capital values differ from the canonical portfolio projection",
            )
        projection_snapshot = projection.model_dump(mode="json")
        return VerifiedCapitalProjection(
            capital=derived,
            projection=projection,
            projection_hash=hash_json(projection_snapshot),
        )


class ProposalDurableExposureResolver:
    """Binds a new initial proposal to the organization risk ledger projection."""

    @staticmethod
    def resolve(
        session: Session,
        request: RiskPrecheckRequest,
    ) -> VerifiedProposalDurableExposure:
        snapshot = DurableExposureSnapshotService.query(
            session,
            organization_id=request.organization_id,
            campaign_id=None,
            scope_keys=tuple(
                (item.scope_type.value, item.scope_id) for item in request.scope_risks
            ),
            raw_available_margin=request.capital.available_margin,
        )
        if snapshot.has_unknown_exposure:
            raise CommandRejected(
                "ORDER_RESULT_UNKNOWN",
                "organization has unresolved durable order exposure",
            )
        if (
            request.requested.requested_margin
            > snapshot.available_margin_after_internal_reservations
        ):
            raise CommandRejected(
                "DURABLE_MARGIN_CAPACITY_EXCEEDED",
                "durable margin reservations leave insufficient available margin",
            )

        derived_trade_loss = TradeLossComponents(
            open_heat=ZERO,
            reserved_heat=ZERO,
            unknown_heat=ZERO,
            protected_profit_giveback=ZERO,
            cost_stress_add_on=ZERO,
        )
        scope_by_key = {item.key: item for item in snapshot.scope_exposures}
        derived_scope_risks = tuple(
            item.model_copy(
                update={
                    "current_planned_loss": scope_by_key[
                        item.scope_type.value, item.scope_id
                    ].current_planned_loss,
                    "current_stress_loss": scope_by_key[
                        item.scope_type.value, item.scope_id
                    ].current_stress_loss,
                }
            )
            for item in request.scope_risks
        )
        derived_capital = request.capital.model_copy(
            update={
                "funding_used": snapshot.global_funding_used,
                "funding_reserved": (
                    snapshot.global_funding_reserved + snapshot.global_funding_unknown
                ),
                "available_margin": snapshot.available_margin_after_internal_reservations,
            }
        )
        submitted_scope_usage = {
            item.key: (item.current_planned_loss, item.current_stress_loss)
            for item in request.scope_risks
        }
        derived_scope_usage = {
            item.key: (item.current_planned_loss, item.current_stress_loss)
            for item in derived_scope_risks
        }
        if (
            request.capital.funding_used != snapshot.global_funding_used
            or request.capital.funding_reserved
            != snapshot.global_funding_reserved + snapshot.global_funding_unknown
            or request.current_trade_loss != derived_trade_loss
            or submitted_scope_usage != derived_scope_usage
        ):
            raise CommandRejected(
                "DURABLE_EXPOSURE_INPUT_MISMATCH",
                "caller funding, initial Heat, or scope usage differs from durable risk state",
            )

        snapshot_hash = hash_json(snapshot.model_dump(mode="json"))
        return VerifiedProposalDurableExposure(
            risk_request=request.model_copy(
                update={
                    "capital": derived_capital,
                    "current_trade_loss": derived_trade_loss,
                    "scope_risks": derived_scope_risks,
                }
            ),
            snapshot=snapshot,
            snapshot_hash=snapshot_hash,
        )


def capability_validation_request(
    request: RiskPrecheckRequest,
    validation_time: datetime,
) -> CapabilityValidationRequest:
    """Build the exact durable certificate lookup from risk facts, not caller booleans."""

    binding = request.binding
    return CapabilityValidationRequest(
        organization_id=request.organization_id,
        certificate_id=binding.capability_certificate_ref,
        expected_scope=CapabilityScope(
            proposal_source=binding.proposal_source,
            strategy_id=binding.strategy_id,
            strategy_version=binding.strategy_version,
            venue=binding.venue,
            execution_domain=binding.execution_domain,
            account_id=binding.account_id,
            account_abstraction=binding.account_abstraction,
            position_mode=binding.position_mode,
            margin_mode=binding.margin_mode,
            collateral_scope=binding.collateral_scope,
            collateral_pool_id=binding.collateral_pool_id,
            instrument_id=binding.instrument_identity,
            contract_multiplier=binding.contract_multiplier,
            underlying_id=binding.underlying_id,
            sector_id=binding.sector_id,
            risk_cluster_id=binding.risk_cluster_id,
            direction=request.market.direction.value,
            risk_tier=request.risk_tier.value,
            max_add_count=binding.requested_add_count,
            settlement_asset=binding.settlement_asset,
            worker_id=binding.worker_id,
            worker_config_hash=binding.worker_config_hash,
            credential_fingerprint=binding.credential_fingerprint,
            capital_transfer_capability="NOT_APPLICABLE",
        ),
        expected_policy_versions=CapabilityPolicyVersions(
            strategy_parameter_version=binding.strategy_parameter_version,
            risk_policy_version=request.policy_version,
            authorization_policy_version=binding.authorization_policy_version,
            catalog_version=binding.catalog_version,
            execution_capability_version=binding.execution_capability_version,
            adapter_version=binding.adapter_version,
            freqtrade_worker_version=binding.freqtrade_worker_version,
            account_capability_version=binding.account_capability_version,
            credential_permission_profile_version=(binding.credential_permission_profile_version),
            venue_client_version=binding.venue_client_version,
            instrument_scope_version=binding.instrument_scope_version,
            position_management_template_version=(binding.position_management_template_version),
            add_milestone_policy_version=binding.add_milestone_policy_version,
        ),
        requested_order_notional=(
            request.requested.requested_quantity
            * request.market.executable_price
            * request.market.contract_multiplier
        ),
        requested_trade_loss=request.requested.proposal_requested_loss_cap,
        validation_time=validation_time,
    )


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

        facts = evaluation.risk_fact_set.observations
        freshness = {item.fact_type: item.max_age_ms for item in policy.fact_freshness_limits}
        unknown_facts = tuple(
            sorted(
                (item.fact_type for item in facts if item.status is FactStatus.UNKNOWN),
                key=lambda item: item.value,
            )
        )
        stale_facts: list[FactType] = []
        future_facts: list[FactType] = []
        invalid_order_facts: list[FactType] = []
        fact_expiries: list[datetime] = []
        if not evaluation.risk_fact_set.valid:
            reasons.append("RISK_FACT_SET_UNAVAILABLE")
            hard_failure = True
        for fact in facts:
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
        event_times = [item.event_time for item in facts]
        consistency_window = timedelta(milliseconds=policy.consistency_window_ms)
        if event_times and max(event_times) - min(event_times) > consistency_window:
            reasons.append("FACTS_INCONSISTENT")
            hard_failure = True

        current_mtm = request.capital.current_portfolio_mtm_equity
        if current_mtm <= ZERO:
            reasons.append("PORTFOLIO_EQUITY_NON_POSITIVE")
            hard_failure = True
        if not evaluation.instrument_classification.valid:
            reasons.append("INSTRUMENT_UNCLASSIFIED")
            hard_failure = True
        if not evaluation.capability_validation.valid:
            reasons.append("CAPABILITY_CERTIFICATE_INVALID")
            hard_failure = True
        if not evaluation.protection_capability.valid:
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
        cost_stress = derive_cost_stress(request, policy)
        incremental_trade_loss = request.requested_base_heat + cost_stress.total
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
                        incremental_planned_loss=incremental_trade_loss,
                        incremental_stress_loss=incremental_trade_loss,
                        gap_stress_add_on=ZERO,
                        liquidity_degradation_stress_add_on=ZERO,
                        unprotected_window_stress_add_on=ZERO,
                        scope_stress_model_version=None,
                        scope_stress_source_ref=None,
                        planned_loss_after=(scope.current_planned_loss + incremental_trade_loss),
                        planned_loss_cap=ZERO,
                        stress_loss_after=(scope.current_stress_loss + incremental_trade_loss),
                        stress_loss_cap=ZERO,
                        planned_passed=False,
                        stress_passed=False,
                    )
                )
                continue

            stress = derive_scope_stress(request, limit.stress_scenario)
            incremental_stress_loss = incremental_trade_loss + stress.scenario_add_on
            planned_after = scope.current_planned_loss + incremental_trade_loss
            stress_after = scope.current_stress_loss + incremental_stress_loss
            planned_ratio = _capacity_ratio(
                limit.planned_loss_cap,
                scope.current_planned_loss,
                incremental_trade_loss,
            )
            stress_ratio = _capacity_ratio(
                limit.stress_loss_cap,
                scope.current_stress_loss,
                incremental_stress_loss,
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
                    incremental_planned_loss=incremental_trade_loss,
                    incremental_stress_loss=incremental_stress_loss,
                    gap_stress_add_on=stress.gap_stress,
                    liquidity_degradation_stress_add_on=(stress.liquidity_degradation_stress),
                    unprotected_window_stress_add_on=stress.unprotected_window_stress,
                    scope_stress_model_version=limit.stress_scenario.model_version,
                    scope_stress_source_ref=limit.stress_scenario.source_ref,
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
        valid_until_candidate = min(
            [
                evaluation.policy_valid_until,
                evaluation.capability_validation.valid_until,
                evaluation.instrument_classification.valid_until,
                evaluation.protection_capability.valid_until,
                evaluation.risk_fact_set.valid_until,
                *fact_expiries,
                *(
                    [evaluation.protected_position_risk.valid_until]
                    if evaluation.protected_position_risk is not None
                    else []
                ),
            ]
        )
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
            current_trade_loss=request.current_trade_loss,
            current_protected_position_risk_calculation_hash=(
                evaluation.protected_position_risk.projection.calculation_hash
                if evaluation.protected_position_risk is not None
                else None
            ),
            catalog_record_id=evaluation.instrument_classification.catalog_record_id,
            catalog_record_hash=evaluation.instrument_classification.record_hash,
            catalog_evidence_hash=evaluation.instrument_classification.evidence_hash,
            catalog_validation_reason_codes=(evaluation.instrument_classification.reason_codes),
            protection_capability_record_id=(
                evaluation.protection_capability.protection_capability_record_id
            ),
            protection_capability_record_hash=(evaluation.protection_capability.record_hash),
            protection_capability_evidence_hash=(evaluation.protection_capability.evidence_hash),
            protection_capability_reason_codes=(evaluation.protection_capability.reason_codes),
            risk_fact_set_id=evaluation.risk_fact_set.risk_fact_set_id,
            risk_fact_set_version=evaluation.risk_fact_set.fact_set_version,
            risk_fact_set_record_hash=evaluation.risk_fact_set.record_hash,
            risk_fact_set_evidence_hash=evaluation.risk_fact_set.evidence_hash,
            risk_fact_set_reason_codes=evaluation.risk_fact_set.reason_codes,
            requested_base_heat=request.requested_base_heat,
            requested_fee_stress=cost_stress.fee_stress,
            requested_stop_penetration_stress=cost_stress.stop_penetration_stress,
            requested_adverse_funding_stress=cost_stress.adverse_funding_stress,
            requested_cost_stress_add_on=cost_stress.total,
            requested_incremental_worst_case_loss=incremental_trade_loss,
            cost_stress_model_version=policy.cost_stress.model_version,
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

    command_type = "risk.precheck.evaluate.v8"
    payload_schema_version = 8

    def __init__(
        self,
        evaluator: RiskEvaluator | None = None,
        certificate_validator: CapabilityCertificateValidator | None = None,
        instrument_catalog_validator: InstrumentCatalogValidator | None = None,
        protection_capability_validator: ProtectionCapabilityValidator | None = None,
        risk_fact_set_validator: RiskFactSetValidator | None = None,
        capital_projection_resolver: CapitalProjectionResolver | None = None,
        durable_exposure_resolver: ProposalDurableExposureResolver | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._evaluator = evaluator or RiskEvaluator()
        self._certificate_validator = certificate_validator or CapabilityCertificateValidator()
        self._instrument_catalog_validator = (
            instrument_catalog_validator or InstrumentCatalogValidator()
        )
        self._protection_capability_validator = (
            protection_capability_validator or ProtectionCapabilityValidator()
        )
        self._risk_fact_set_validator = risk_fact_set_validator or RiskFactSetValidator()
        self._capital_projection_resolver = (
            capital_projection_resolver or CapitalProjectionResolver()
        )
        self._durable_exposure_resolver = (
            durable_exposure_resolver or ProposalDurableExposureResolver()
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def evaluate(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        started = time.monotonic()
        if envelope.command_type != self.command_type:
            raise CommandRejected("COMMAND_TYPE_MISMATCH", "unexpected command type")
        if envelope.payload_schema_version != self.payload_schema_version:
            raise CommandRejected(
                "PAYLOAD_SCHEMA_VERSION_MISMATCH",
                "risk precheck payload schema version is unsupported",
            )
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

        decided_at = self._clock()
        if decided_at.tzinfo is None or decided_at.utcoffset() is None:
            raise CommandRejected(
                "RISK_CLOCK_INVALID", "risk precheck clock must be timezone-aware"
            )
        verified_capital = self._capital_projection_resolver.resolve(
            session,
            request,
            policy,
            decided_at,
        )
        verified_request = request.model_copy(update={"capital": verified_capital.capital})
        verified_exposure = self._durable_exposure_resolver.resolve(session, verified_request)
        verified_request = verified_exposure.risk_request
        capability_validation = self._certificate_validator.validate(
            session,
            capability_validation_request(verified_request, decided_at),
        )
        instrument_classification = self._instrument_catalog_validator.validate(
            session,
            instrument_classification_validation_request(verified_request, decided_at),
        )
        protection_capability = self._protection_capability_validator.validate(
            session,
            protection_capability_validation_request(
                verified_request,
                instrument_classification,
                decided_at,
            ),
        )
        risk_fact_set = self._risk_fact_set_validator.validate(
            session,
            risk_fact_set_validation_request(verified_request, decided_at),
            lock=True,
        )
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
        evaluation_input = RiskEvaluationInput(
            request=verified_request,
            risk_policy_id=policy_record.risk_policy_id,
            policy=policy,
            policy_valid_from=policy_record.valid_from,
            policy_valid_until=policy_record.valid_until,
            system_risk_state=system_state,
            capability_validation=capability_validation,
            instrument_classification=instrument_classification,
            protection_capability=protection_capability,
            risk_fact_set=risk_fact_set,
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
        input_snapshot["submitted_capital"] = request.capital.model_dump(mode="json")
        input_snapshot["capital_projection"] = verified_capital.projection.model_dump(mode="json")
        input_snapshot["capital_projection_hash"] = verified_capital.projection_hash
        input_snapshot["durable_exposure_snapshot"] = verified_exposure.snapshot.model_dump(
            mode="json"
        )
        input_snapshot["durable_exposure_snapshot_hash"] = verified_exposure.snapshot_hash
        input_snapshot["derived_capital"] = {
            "exchange_margin_equity": str(verified_request.capital.exchange_margin_equity),
            "current_portfolio_mtm_equity": str(
                verified_request.capital.current_portfolio_mtm_equity
            ),
            "funding_used": str(verified_request.capital.funding_used),
            "funding_reserved": str(verified_request.capital.funding_reserved),
            "available_margin": str(verified_request.capital.available_margin),
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
                capital_scope_manifest_id=verified_capital.projection.manifest_id,
                capital_scope_manifest_version=verified_capital.projection.manifest_version,
                capital_scope_manifest_hash=verified_capital.projection.manifest_hash,
                capital_projection_version=verified_capital.projection.projection_version,
                capital_projection_hash=verified_capital.projection_hash,
                durable_exposure_snapshot_hash=verified_exposure.snapshot_hash,
                catalog_record_id=instrument_classification.catalog_record_id,
                catalog_version=(
                    verified_request.binding.catalog_version
                    if instrument_classification.catalog_record_id is not None
                    else None
                ),
                catalog_classification_version=(
                    verified_request.binding.instrument_scope_version
                    if instrument_classification.catalog_record_id is not None
                    else None
                ),
                catalog_record_hash=instrument_classification.record_hash,
                protection_capability_record_id=(
                    protection_capability.protection_capability_record_id
                ),
                protection_capability_version=(
                    verified_request.binding.position_management_template_version
                    if protection_capability.protection_capability_record_id is not None
                    else None
                ),
                protection_capability_record_hash=(protection_capability.record_hash),
                risk_fact_set_id=risk_fact_set.risk_fact_set_id,
                risk_fact_set_version=(
                    risk_fact_set.fact_set_version
                    if risk_fact_set.risk_fact_set_id is not None
                    else None
                ),
                risk_fact_set_record_hash=risk_fact_set.record_hash,
                requested_quantity=result.requested_quantity,
                max_safe_quantity=result.max_safe_quantity,
                final_quantity=result.final_quantity,
                current_unrealized_pnl=result.current_unrealized_pnl,
                current_portfolio_mtm_equity=result.current_portfolio_mtm_equity,
                total_capital_snapshot_0=verified_request.capital.total_capital_snapshot_0,
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
                "capital_scope_manifest_id": str(verified_capital.projection.manifest_id),
                "capital_scope_manifest_version": verified_capital.projection.manifest_version,
                "capital_scope_manifest_hash": verified_capital.projection.manifest_hash,
                "capital_projection_version": verified_capital.projection.projection_version,
                "capital_projection_hash": verified_capital.projection_hash,
                "durable_exposure_snapshot_hash": verified_exposure.snapshot_hash,
                "catalog_record_id": (
                    str(instrument_classification.catalog_record_id)
                    if instrument_classification.catalog_record_id is not None
                    else None
                ),
                "catalog_record_hash": instrument_classification.record_hash,
                "catalog_validation_reason_codes": list(instrument_classification.reason_codes),
                "protection_capability_record_id": (
                    str(protection_capability.protection_capability_record_id)
                    if protection_capability.protection_capability_record_id is not None
                    else None
                ),
                "protection_capability_record_hash": (protection_capability.record_hash),
                "protection_capability_reason_codes": list(protection_capability.reason_codes),
                "risk_fact_set_id": (
                    str(risk_fact_set.risk_fact_set_id)
                    if risk_fact_set.risk_fact_set_id is not None
                    else None
                ),
                "risk_fact_set_version": risk_fact_set.fact_set_version,
                "risk_fact_set_record_hash": risk_fact_set.record_hash,
                "risk_fact_set_reason_codes": list(risk_fact_set.reason_codes),
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
                        "capital_scope_manifest_id": str(verified_capital.projection.manifest_id),
                        "capital_scope_manifest_version": (
                            verified_capital.projection.manifest_version
                        ),
                        "capital_scope_manifest_hash": verified_capital.projection.manifest_hash,
                        "capital_projection_version": (
                            verified_capital.projection.projection_version
                        ),
                        "capital_projection_hash": verified_capital.projection_hash,
                        "durable_exposure_snapshot_hash": verified_exposure.snapshot_hash,
                        "catalog_record_id": (
                            str(instrument_classification.catalog_record_id)
                            if instrument_classification.catalog_record_id is not None
                            else None
                        ),
                        "catalog_record_hash": instrument_classification.record_hash,
                        "catalog_validation_reason_codes": list(
                            instrument_classification.reason_codes
                        ),
                        "protection_capability_record_id": (
                            str(protection_capability.protection_capability_record_id)
                            if protection_capability.protection_capability_record_id is not None
                            else None
                        ),
                        "protection_capability_record_hash": (protection_capability.record_hash),
                        "risk_fact_set_id": (
                            str(risk_fact_set.risk_fact_set_id)
                            if risk_fact_set.risk_fact_set_id is not None
                            else None
                        ),
                        "risk_fact_set_version": risk_fact_set.fact_set_version,
                        "risk_fact_set_record_hash": risk_fact_set.record_hash,
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
