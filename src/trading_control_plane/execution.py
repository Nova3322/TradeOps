from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Any, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from trading_control_plane.authorization import SystemRiskState
from trading_control_plane.capability_certificates import CapabilityCertificateValidator
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
from trading_control_plane.execution_models import (
    ORDER_INTENT_STATUSES,
    ExecutionFact,
    ExecutionRiskDecision,
    OrderIntent,
    OrderIntentState,
    RiskExposureState,
    RiskLedgerEntry,
    RiskReservation,
)
from trading_control_plane.metrics import (
    EXECUTION_CANONICAL_FACT_BINDINGS,
    EXECUTION_FACT_AUTHORITY_MODES,
    EXECUTION_FACT_BINDINGS,
    EXECUTION_FACT_RESULTS,
    EXECUTION_RISK_DECISIONS,
    RISK_RESERVATION_TRANSITIONS,
)
from trading_control_plane.projections import (
    CurrentProtectionScope,
    ProjectionQueryContext,
    ProjectionState,
    VenueCurrentProjectionService,
)
from trading_control_plane.proposal_models import FrozenProposalVersion, SystemRiskStateRecord
from trading_control_plane.reconciliation import ReconciliationSourceType
from trading_control_plane.reconciliation_models import (
    ExecutionReconciliationInput,
    ExecutionReconciliationRun,
    ExecutionReconciliationRunState,
)
from trading_control_plane.risk import (
    CapitalProjectionBinding,
    CapitalProjectionResolver,
    FactType,
    RiskDecisionResult,
    RiskEvaluationInput,
    RiskEvaluationResult,
    RiskEvaluator,
    RiskPolicyParameters,
    RiskPrecheckRequest,
    ScopeType,
    TradeLossComponents,
    VerifiedCapitalProjection,
    VerifiedProtectedPositionRisk,
    capability_validation_request,
)
from trading_control_plane.risk_models import RiskPolicyRecord
from trading_control_plane.sender_fencing_models import (
    ExecutionSenderScope,
    ExecutionSenderScopeState,
    ShadowDispatchClaim,
)
from trading_control_plane.trading_authorization import FrozenAuthorizationBinding
from trading_control_plane.trading_authorization_models import (
    AddAuthorizationPackage,
    AddAuthorizationPackageState,
    AddUnit,
    AddUnitState,
    Campaign,
    CampaignState,
    InitialAuthorizationState,
    InitialOrderAuthorization,
    TradingAuthorization,
)
from trading_control_plane.venue_fact_models import (
    VenueFactInputLink,
    VenueFill,
    VenueOrderObservation,
    VenuePositionSnapshot,
    VenueProtectionSnapshot,
)

ZERO = Decimal("0")
QUANTUM = Decimal("0.000000000000000001")
EXECUTION_INTENT_SERVICE_PRINCIPAL = "oms-risk-reservation-service"
EXECUTION_RECONCILIATION_SERVICE_PRINCIPAL = "execution-reconciliation-service"
ZERO_TERMINAL_STATUSES = frozenset({"CANCELLED_ZERO_FILL", "REJECTED_ZERO_FILL"})
POSITIVE_TERMINAL_STATUSES = frozenset(
    {
        "FILLED",
        "CANCELLED_PARTIAL",
        "POSITION_RECONCILED",
        "PROTECTION_CONFIRMED",
        "COMPLETED",
    }
)
BLOCKING_INITIAL_STATUSES = frozenset(
    {
        "INTENT_CREATED",
        "DISPATCHING",
        "VENUE_ACKNOWLEDGED",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCEL_PENDING",
        "CANCELLED_PARTIAL",
        "RESULT_UNKNOWN",
        "POSITION_RECONCILED",
        "PROTECTION_CONFIRMED",
        "COMPLETED",
        "FAILED_SAFE",
    }
)


class ExecutionFactKind(StrEnum):
    WORKER_RECEIPT = "WORKER_RECEIPT"
    VENUE_ORDER = "VENUE_ORDER"
    VENUE_FILL = "VENUE_FILL"
    VENUE_POSITION = "VENUE_POSITION"
    VENUE_PROTECTION = "VENUE_PROTECTION"


EXECUTION_FACT_SOURCE_STATUS_MATRIX: dict[
    str, frozenset[tuple[ExecutionFactKind, ReconciliationSourceType]]
] = {
    "DISPATCHING": frozenset(
        {(ExecutionFactKind.WORKER_RECEIPT, ReconciliationSourceType.WORKER_LOCAL)}
    ),
    "VENUE_ACKNOWLEDGED": frozenset(
        {(ExecutionFactKind.VENUE_ORDER, ReconciliationSourceType.VENUE_ORDERS)}
    ),
    "PARTIALLY_FILLED": frozenset(
        {(ExecutionFactKind.VENUE_FILL, ReconciliationSourceType.VENUE_FILLS)}
    ),
    "FILLED": frozenset({(ExecutionFactKind.VENUE_FILL, ReconciliationSourceType.VENUE_FILLS)}),
    "CANCEL_PENDING": frozenset(
        {(ExecutionFactKind.VENUE_ORDER, ReconciliationSourceType.VENUE_ORDERS)}
    ),
    "CANCELLED_ZERO_FILL": frozenset(
        {(ExecutionFactKind.VENUE_ORDER, ReconciliationSourceType.VENUE_ORDERS)}
    ),
    "CANCELLED_PARTIAL": frozenset(
        {(ExecutionFactKind.VENUE_ORDER, ReconciliationSourceType.VENUE_ORDERS)}
    ),
    "REJECTED_ZERO_FILL": frozenset(
        {(ExecutionFactKind.VENUE_ORDER, ReconciliationSourceType.VENUE_ORDERS)}
    ),
    "RESULT_UNKNOWN": frozenset(
        {
            (ExecutionFactKind.WORKER_RECEIPT, ReconciliationSourceType.WORKER_LOCAL),
            (ExecutionFactKind.VENUE_ORDER, ReconciliationSourceType.VENUE_ORDERS),
        }
    ),
    "POSITION_RECONCILED": frozenset(
        {(ExecutionFactKind.VENUE_POSITION, ReconciliationSourceType.VENUE_POSITIONS)}
    ),
    "PROTECTION_CONFIRMED": frozenset(
        {(ExecutionFactKind.VENUE_PROTECTION, ReconciliationSourceType.VENUE_PROTECTION)}
    ),
    "COMPLETED": frozenset(),
    "FAILED_SAFE": frozenset(
        {
            (ExecutionFactKind.WORKER_RECEIPT, ReconciliationSourceType.WORKER_LOCAL),
        }
    ),
}


class IntentKind(StrEnum):
    INITIAL = "INITIAL"
    ADD = "ADD"


class AddEligibilitySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    frozen_return_pct: Decimal
    trend_valid: bool
    protection_valid: bool
    authorization_valid: bool
    current_effective_leverage: Decimal = Field(ge=0)
    target_effective_leverage: Decimal = Field(gt=0)
    current_position_equity: Decimal = Field(gt=0)
    position_snapshot_ref: str = Field(min_length=1, max_length=255)
    position_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    protection_snapshot_ref: str = Field(min_length=1, max_length=255)
    protection_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class CreateExecutionIntentRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    intent_kind: IntentKind
    candidate_ref: str = Field(min_length=1, max_length=255)
    initial_authorization_id: UUID | None = None
    add_package_id: UUID | None = None
    add_unit_id: UUID | None = None
    current_position_quantity: Decimal = Field(ge=0)
    target_position_quantity: Decimal = Field(gt=0)
    order_type: str = Field(min_length=1, max_length=40)
    time_in_force: str = Field(min_length=1, max_length=40)
    risk_currency: str = Field(min_length=1, max_length=80)
    valuation_price_source_ref: str = Field(min_length=1, max_length=255)
    risk_request: RiskPrecheckRequest
    add_eligibility: AddEligibilitySnapshot | None = None

    @model_validator(mode="after")
    def authorization_kind_matches(self) -> Self:
        if self.intent_kind is IntentKind.INITIAL:
            if (
                self.initial_authorization_id is None
                or self.add_package_id is not None
                or self.add_unit_id is not None
                or self.add_eligibility is not None
            ):
                raise ValueError("INITIAL requires only initial_authorization_id")
        elif (
            self.initial_authorization_id is not None
            or self.add_package_id is None
            or self.add_unit_id is None
            or self.add_eligibility is None
        ):
            raise ValueError("ADD requires package, unit, and eligibility evidence")
        if (
            self.target_position_quantity - self.current_position_quantity
            != self.risk_request.requested.requested_quantity
        ):
            raise ValueError("target delta must equal the exact risk-request quantity")
        return self


class VerifiedDurableExposure(BaseModel):
    model_config = ConfigDict(frozen=True)

    risk_request: RiskPrecheckRequest
    snapshot: DurableExposureSnapshot
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    protected_position_risk: VerifiedProtectedPositionRisk | None = None


class RecordExecutionFactRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_sequence: int = Field(ge=1)
    fact_kind: ExecutionFactKind
    target_status: str = Field(min_length=3, max_length=32)
    venue: str = Field(min_length=1, max_length=80)
    execution_domain: str = Field(min_length=1, max_length=120)
    account_id: str = Field(min_length=1, max_length=160)
    external_fact_id: str = Field(min_length=1, max_length=255)
    cumulative_filled_quantity: Decimal = Field(ge=0)
    known_remaining_quantity: Decimal = Field(ge=0)
    zero_fill_confirmed: bool
    venue_order_terminal: bool
    position_reconciled: bool
    protection_confirmed: bool
    shadow_dispatch_claim_id: UUID
    reconciliation_run_id: UUID
    reconciliation_input_id: UUID
    reconciliation_source_type: ReconciliationSourceType
    reconciliation_run_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reconciliation_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dispatch_claim_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    venue_order_observation_id: UUID | None = None
    venue_fill_id: UUID | None = None
    venue_position_snapshot_id: UUID | None = None
    venue_protection_snapshot_id: UUID | None = None
    venue_fact_input_link_id: UUID | None = None
    venue_fact_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_ref: str = Field(min_length=1, max_length=255)
    source_version: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any]
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ref: str = Field(min_length=1, max_length=255)
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_time: datetime
    received_at: datetime

    @model_validator(mode="after")
    def evidence_is_self_consistent(self) -> Self:
        if self.fact_kind is ExecutionFactKind.VENUE_ORDER:
            exact_venue_binding = (
                self.venue_order_observation_id is not None
                and self.venue_fill_id is None
                and self.venue_position_snapshot_id is None
                and self.venue_protection_snapshot_id is None
                and self.venue_fact_input_link_id is not None
                and self.venue_fact_hash is not None
            )
        elif self.fact_kind is ExecutionFactKind.VENUE_FILL:
            exact_venue_binding = (
                self.venue_order_observation_id is None
                and self.venue_fill_id is not None
                and self.venue_position_snapshot_id is None
                and self.venue_protection_snapshot_id is None
                and self.venue_fact_input_link_id is not None
                and self.venue_fact_hash is not None
            )
        elif self.fact_kind is ExecutionFactKind.VENUE_POSITION:
            exact_venue_binding = (
                self.venue_order_observation_id is None
                and self.venue_fill_id is None
                and self.venue_position_snapshot_id is not None
                and self.venue_protection_snapshot_id is None
                and self.venue_fact_input_link_id is not None
                and self.venue_fact_hash is not None
            )
        elif self.fact_kind is ExecutionFactKind.VENUE_PROTECTION:
            exact_venue_binding = (
                self.venue_order_observation_id is None
                and self.venue_fill_id is None
                and self.venue_position_snapshot_id is None
                and self.venue_protection_snapshot_id is not None
                and self.venue_fact_input_link_id is not None
                and self.venue_fact_hash is not None
            )
        else:
            exact_venue_binding = (
                self.venue_order_observation_id is None
                and self.venue_fill_id is None
                and self.venue_position_snapshot_id is None
                and self.venue_protection_snapshot_id is None
                and self.venue_fact_input_link_id is None
                and self.venue_fact_hash is None
            )
        if not exact_venue_binding:
            raise ValueError("execution fact canonical venue binding is invalid")
        if self.event_time.tzinfo is None or self.received_at.tzinfo is None:
            raise ValueError("execution fact timestamps must be timezone-aware")
        if self.event_time > self.received_at:
            raise ValueError("execution fact event_time cannot follow received_at")
        if hash_json(self.payload) != self.payload_hash:
            raise ValueError("execution fact payload hash mismatch")
        material = self.model_dump(mode="json", exclude={"evidence_hash"})
        if hash_json(material) != self.evidence_hash:
            raise ValueError("execution fact evidence hash mismatch")
        return self


class CurrentProtectedPositionRiskResolver:
    """Binds ADD risk to fresh canonical position and native protection facts."""

    @staticmethod
    def resolve(
        session: Session,
        request: CreateExecutionIntentRequest,
        campaign: Campaign,
        policy: RiskPolicyParameters,
        as_of: datetime,
    ) -> VerifiedProtectedPositionRisk:
        if request.intent_kind is not IntentKind.ADD or request.add_eligibility is None:
            raise CommandRejected(
                "CURRENT_PROTECTED_POSITION_RISK_NOT_APPLICABLE",
                "current protected-position risk is required only for ADD",
            )
        freshness_limits = {
            item.fact_type: item.max_age_ms for item in policy.fact_freshness_limits
        }
        max_age_ms = min(
            freshness_limits[FactType.POSITIONS],
            freshness_limits[FactType.PROTECTION],
        )
        binding = request.risk_request.binding
        scope = CurrentProtectionScope(
            organization_id=campaign.organization_id,
            venue=campaign.venue,
            execution_domain=campaign.execution_domain,
            account_id=campaign.account_id,
            instrument_id=campaign.instrument_id,
            position_mode=binding.position_mode,
            position_side="BOTH" if binding.position_mode == "ONE_WAY" else campaign.direction,
            margin_mode=binding.margin_mode,
            collateral_pool_id=binding.collateral_pool_id,
            settlement_currency=request.risk_currency,
        )
        projection = VenueCurrentProjectionService.current_protected_position_risk(
            session,
            scope,
            ProjectionQueryContext(as_of=as_of, max_age_ms=max_age_ms),
        )
        if projection.projection_state is not ProjectionState.CONFIRMED:
            raise CommandRejected(
                "CURRENT_PROTECTED_POSITION_RISK_UNAVAILABLE",
                f"current protected-position risk unavailable: {projection.reason_code}",
            )
        if (
            projection.quantity != request.current_position_quantity
            or projection.direction.value != campaign.direction
            or projection.mark_price != request.risk_request.market.mark_price
            or projection.contract_multiplier != binding.contract_multiplier
            or projection.scope.settlement_currency != request.risk_currency
        ):
            raise CommandRejected(
                "CURRENT_PROTECTED_POSITION_RISK_BINDING_MISMATCH",
                "current position quantity, direction, Mark, multiplier, or currency changed",
            )
        eligibility = request.add_eligibility
        expected_position_ref = f"venue-position-snapshot:{projection.position_snapshot_id}"
        expected_protection_ref = f"venue-protection-snapshot:{projection.protection_snapshot_id}"
        if (
            eligibility.position_snapshot_ref != expected_position_ref
            or eligibility.position_snapshot_hash != projection.position_snapshot_hash
            or eligibility.protection_snapshot_ref != expected_protection_ref
            or eligibility.protection_snapshot_hash != projection.protection_snapshot_hash
        ):
            raise CommandRejected(
                "ADD_ELIGIBILITY_CANONICAL_FACT_MISMATCH",
                "Add eligibility does not reference the current canonical protection facts",
            )
        if projection.facts_as_of is None:  # pragma: no cover - confirmed model enforces
            raise RuntimeError("confirmed protected-position risk lacks facts_as_of")
        valid_until = projection.facts_as_of + timedelta(milliseconds=max_age_ms)
        if valid_until <= as_of:
            raise CommandRejected(
                "CURRENT_PROTECTED_POSITION_RISK_UNAVAILABLE",
                "current protected-position risk has no remaining validity",
            )
        return VerifiedProtectedPositionRisk(
            projection=projection,
            max_age_ms=max_age_ms,
            valid_until=valid_until,
        )


class DurableExposureResolver:
    """Rebuilds current funding, Heat, margin, and scope usage from durable state."""

    @staticmethod
    def resolve(
        session: Session,
        request: CreateExecutionIntentRequest,
        campaign: Campaign,
        protected_position_risk: VerifiedProtectedPositionRisk | None = None,
    ) -> VerifiedDurableExposure:
        snapshot = DurableExposureSnapshotService.query(
            session,
            organization_id=campaign.organization_id,
            campaign_id=campaign.campaign_id,
            scope_keys=tuple(
                (item.scope_type.value, item.scope_id) for item in request.risk_request.scope_risks
            ),
            raw_available_margin=request.risk_request.capital.available_margin,
        )
        if snapshot.has_unknown_exposure:
            raise CommandRejected(
                "ORDER_RESULT_UNKNOWN",
                "organization has unresolved durable order exposure",
            )
        if (
            request.risk_request.requested.requested_margin
            > snapshot.available_margin_after_internal_reservations
        ):
            raise CommandRejected(
                "DURABLE_MARGIN_CAPACITY_EXCEEDED",
                "durable margin reservations leave insufficient available margin",
            )

        if request.intent_kind is IntentKind.ADD and protected_position_risk is None:
            raise CommandRejected(
                "CURRENT_PROTECTED_POSITION_RISK_REQUIRED",
                "ADD durable exposure requires canonical protected-position risk",
            )
        projection = (
            protected_position_risk.projection if protected_position_risk is not None else None
        )
        replacement_delta = ZERO
        canonical_open_heat = snapshot.campaign_open_heat
        canonical_giveback = snapshot.campaign_protected_profit_giveback
        if projection is not None:
            if projection.open_heat is None or projection.protected_profit_giveback is None:
                raise CommandRejected(
                    "CURRENT_PROTECTED_POSITION_RISK_UNAVAILABLE",
                    "confirmed protected-position risk lacks loss components",
                )
            canonical_open_heat = projection.open_heat
            canonical_giveback = projection.protected_profit_giveback
            replacement_delta = (
                canonical_open_heat
                + canonical_giveback
                - snapshot.campaign_open_heat
                - snapshot.campaign_protected_profit_giveback
            )
        derived_trade_loss = TradeLossComponents(
            open_heat=canonical_open_heat,
            reserved_heat=snapshot.campaign_reserved_heat,
            unknown_heat=snapshot.campaign_unknown_heat,
            protected_profit_giveback=canonical_giveback,
            cost_stress_add_on=snapshot.campaign_cost_stress_add_on,
        )
        derived_scope_by_key = {item.key: item for item in snapshot.scope_exposures}
        derived_scope_risks = tuple(
            item.model_copy(
                update={
                    "current_planned_loss": (
                        derived_scope_by_key[
                            item.scope_type.value, item.scope_id
                        ].current_planned_loss
                        + replacement_delta
                    ),
                    "current_stress_loss": (
                        derived_scope_by_key[
                            item.scope_type.value, item.scope_id
                        ].current_stress_loss
                        + replacement_delta
                    ),
                }
            )
            for item in request.risk_request.scope_risks
        )
        derived_capital = request.risk_request.capital.model_copy(
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
            for item in request.risk_request.scope_risks
        }
        derived_scope_usage = {
            item.key: (item.current_planned_loss, item.current_stress_loss)
            for item in derived_scope_risks
        }
        if any(
            item.current_planned_loss < ZERO or item.current_stress_loss < ZERO
            for item in derived_scope_risks
        ):
            raise CommandRejected(
                "DURABLE_EXPOSURE_INTEGRITY_FAILED",
                "canonical protected risk adjustment made scope exposure negative",
            )
        if (
            request.risk_request.capital.funding_used != snapshot.global_funding_used
            or request.risk_request.capital.funding_reserved
            != snapshot.global_funding_reserved + snapshot.global_funding_unknown
            or request.risk_request.current_trade_loss != derived_trade_loss
            or submitted_scope_usage != derived_scope_usage
        ):
            raise CommandRejected(
                "DURABLE_EXPOSURE_INPUT_MISMATCH",
                "caller funding, Heat, or scope usage differs from durable risk state",
            )

        snapshot_hash = hash_json(snapshot.model_dump(mode="json"))
        return VerifiedDurableExposure(
            risk_request=request.risk_request.model_copy(
                update={
                    "capital": derived_capital,
                    "current_trade_loss": derived_trade_loss,
                    "scope_risks": derived_scope_risks,
                }
            ),
            snapshot=snapshot,
            snapshot_hash=snapshot_hash,
            protected_position_risk=protected_position_risk,
        )


class ExecutionIntentService:
    command_type = "execution.intent.create.v4"
    payload_schema_version = 4

    def __init__(
        self,
        evaluator: RiskEvaluator | None = None,
        certificate_validator: CapabilityCertificateValidator | None = None,
        capital_projection_resolver: CapitalProjectionResolver | None = None,
        durable_exposure_resolver: DurableExposureResolver | None = None,
        protected_position_risk_resolver: CurrentProtectedPositionRiskResolver | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._evaluator = evaluator or RiskEvaluator()
        self._certificate_validator = certificate_validator or CapabilityCertificateValidator()
        self._capital_projection_resolver = (
            capital_projection_resolver or CapitalProjectionResolver()
        )
        self._durable_exposure_resolver = durable_exposure_resolver or DurableExposureResolver()
        self._protected_position_risk_resolver = (
            protected_position_risk_resolver or CurrentProtectedPositionRiskResolver()
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def create(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        self._require_internal(envelope)
        if envelope.object_id is None:  # pragma: no cover - validated above
            raise RuntimeError("missing campaign id")
        try:
            campaign_id = UUID(envelope.object_id)
            request = CreateExecutionIntentRequest.model_validate(envelope.payload)
        except (ValueError, ValidationError) as exc:
            raise CommandRejected("EXECUTION_INPUT_INVALID", "execution input is invalid") from exc

        campaign = session.execute(
            select(Campaign).where(Campaign.campaign_id == campaign_id).with_for_update()
        ).scalar_one_or_none()
        if campaign is None:
            raise CommandRejected("CAMPAIGN_NOT_FOUND", "campaign is unavailable")
        if envelope.scope.get("organization_id") != campaign.organization_id:
            raise CommandRejected("SCOPE_MISMATCH", "organization scope changed")

        authorization = session.execute(
            select(TradingAuthorization)
            .where(TradingAuthorization.authorization_id == campaign.authorization_id)
            .with_for_update()
        ).scalar_one()
        proposal = session.execute(
            select(FrozenProposalVersion).where(
                FrozenProposalVersion.proposal_version_id == campaign.proposal_version_id
            )
        ).scalar_one()
        campaign_state = session.execute(
            select(CampaignState).where(CampaignState.campaign_id == campaign_id).with_for_update()
        ).scalar_one()
        system_state_record = session.execute(
            select(SystemRiskStateRecord)
            .where(SystemRiskStateRecord.organization_id == campaign.organization_id)
            .with_for_update()
        ).scalar_one_or_none()
        system_state = (
            SystemRiskState(system_state_record.status)
            if system_state_record is not None
            else SystemRiskState.UNKNOWN
        )
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise CommandRejected(
                "EXECUTION_CLOCK_INVALID",
                "final order precheck clock must be timezone-aware",
            )
        binding = self._validate_root_integrity(authorization, campaign, proposal, request, now)
        frozen_capital_binding = self._frozen_capital_projection_binding(authorization)
        authorization_rows = self._lock_and_validate_authorization_kind(
            session,
            request,
            campaign,
            campaign_state,
            authorization,
            system_state,
            now,
        )
        self._lock_competing_scopes(session, request, campaign, proposal, binding)

        candidate_hash = hash_json(request.model_dump(mode="json"))
        existing = session.execute(
            select(OrderIntent).where(
                OrderIntent.campaign_id == campaign_id,
                OrderIntent.candidate_ref == request.candidate_ref,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.candidate_hash != candidate_hash:
                raise CommandRejected(
                    "CANDIDATE_BINDING_MISMATCH",
                    "candidate reference already belongs to different semantics",
                )
            return self._existing_outcome(session, existing)

        policy_record, policy = self._load_policy(
            session, campaign.organization_id, request.risk_request.policy_version
        )
        self._validate_risk_bindings(
            request,
            authorization,
            campaign,
            proposal,
            binding,
            frozen_capital_binding,
            system_state,
            authorization_rows,
        )
        verified_capital = self._capital_projection_resolver.resolve(
            session,
            request.risk_request,
            policy,
            now,
            frozen_total_capital_snapshot_0=authorization.total_capital_snapshot_0,
        )
        request = request.model_copy(
            update={
                "risk_request": request.risk_request.model_copy(
                    update={"capital": verified_capital.capital}
                )
            }
        )
        protected_position_risk = (
            self._protected_position_risk_resolver.resolve(
                session,
                request,
                campaign,
                policy,
                now,
            )
            if request.intent_kind is IntentKind.ADD
            else None
        )
        verified_exposure = self._durable_exposure_resolver.resolve(
            session,
            request,
            campaign,
            protected_position_risk,
        )
        request = request.model_copy(update={"risk_request": verified_exposure.risk_request})

        capability_validation = self._certificate_validator.validate(
            session,
            capability_validation_request(request.risk_request, now),
            lock=True,
        )
        evaluation_input = RiskEvaluationInput(
            request=request.risk_request,
            risk_policy_id=policy_record.risk_policy_id,
            policy=policy,
            policy_valid_from=policy_record.valid_from,
            policy_valid_until=policy_record.valid_until,
            system_risk_state=system_state,
            capability_validation=capability_validation,
            decision_time=now,
            protected_position_risk=verified_exposure.protected_position_risk,
        )
        evaluation = self._evaluator.evaluate(evaluation_input)
        if request.intent_kind is IntentKind.ADD and system_state is not SystemRiskState.NORMAL:
            return self._record_denial(
                session,
                request,
                campaign,
                authorization,
                policy_record,
                evaluation_input,
                evaluation,
                verified_capital,
                verified_exposure,
                now,
                "ADD_REQUIRES_NORMAL_SYSTEM_STATE",
            )
        if evaluation.result is not RiskDecisionResult.ALLOW:
            return self._record_denial(
                session,
                request,
                campaign,
                authorization,
                policy_record,
                evaluation_input,
                evaluation,
                verified_capital,
                verified_exposure,
                now,
                evaluation.primary_reason_code,
            )

        funding_after = (
            request.risk_request.capital.funding_used
            + request.risk_request.capital.funding_reserved
            + request.risk_request.requested.requested_funding
        )
        if funding_after > authorization.funding_envelope_0:
            return self._record_denial(
                session,
                request,
                campaign,
                authorization,
                policy_record,
                evaluation_input,
                evaluation,
                verified_capital,
                verified_exposure,
                now,
                "FROZEN_FUNDING_ENVELOPE_EXCEEDED",
            )

        reserved_heat = evaluation.requested_incremental_worst_case_loss
        if (
            request.risk_request.current_trade_loss.total + reserved_heat
            > authorization.authorized_loss_capacity
        ):
            return self._record_denial(
                session,
                request,
                campaign,
                authorization,
                policy_record,
                evaluation_input,
                evaluation,
                verified_capital,
                verified_exposure,
                now,
                "FROZEN_AUTHORIZATION_CAPACITY_EXCEEDED",
            )

        decision_id = uuid4()
        order_intent_id = uuid4()
        risk_reservation_id = uuid4()
        valid_until = min(evaluation.valid_until, authorization.valid_until)
        if valid_until <= now:
            return self._record_denial(
                session,
                request,
                campaign,
                authorization,
                policy_record,
                evaluation_input,
                evaluation,
                verified_capital,
                verified_exposure,
                now,
                "EXECUTION_VALIDITY_EXPIRED",
            )
        input_snapshot = self._input_snapshot(
            envelope,
            request,
            evaluation_input,
            authorization,
            authorization_rows,
            verified_capital,
            verified_exposure,
        )
        decision_payload = evaluation.model_dump(mode="json")
        decision_payload["approved_reserved_heat"] = str(reserved_heat)
        decision_payload["execution_eligible"] = False
        decision_payload["reservation_created"] = True
        decision_payload["order_intent_created"] = True
        initial_id, add_package_id, add_unit_id = self._authorization_ids(request)
        session.add(
            ExecutionRiskDecision(
                execution_risk_decision_id=decision_id,
                decision_stage="ORDER_PRECHECK",
                intent_kind=request.intent_kind.value,
                organization_id=campaign.organization_id,
                authorization_id=authorization.authorization_id,
                campaign_id=campaign.campaign_id,
                initial_authorization_id=initial_id,
                add_package_id=add_package_id,
                add_unit_id=add_unit_id,
                risk_policy_id=policy_record.risk_policy_id,
                risk_policy_version=policy_record.policy_version,
                capital_scope_manifest_id=verified_capital.projection.manifest_id,
                capital_scope_manifest_version=verified_capital.projection.manifest_version,
                capital_scope_manifest_hash=verified_capital.projection.manifest_hash,
                capital_projection_version=verified_capital.projection.projection_version,
                capital_projection_hash=verified_capital.projection_hash,
                durable_exposure_snapshot_hash=verified_exposure.snapshot_hash,
                system_risk_state=system_state.value,
                result="ALLOW",
                primary_reason_code="ORDER_PRECHECK_PASSED",
                requested_quantity=evaluation.requested_quantity,
                max_safe_quantity=evaluation.requested_quantity,
                final_quantity=evaluation.requested_quantity,
                approved_reserved_heat=reserved_heat,
                approved_funding=request.risk_request.requested.requested_funding,
                approved_margin=request.risk_request.requested.requested_margin,
                current_portfolio_mtm_equity=evaluation.current_portfolio_mtm_equity,
                current_unrealized_pnl=evaluation.current_unrealized_pnl,
                input_snapshot=input_snapshot,
                input_hash=hash_json(input_snapshot),
                decision=decision_payload,
                decision_hash=hash_json(decision_payload),
                execution_eligible=False,
                reservation_created=True,
                order_intent_created=True,
                decided_at=now,
                valid_until=valid_until,
            )
        )
        session.flush()

        price_reference, price_lower, price_upper = self._price_contract(
            request, authorization_rows
        )
        intent_snapshot = {
            "request": request.model_dump(mode="json"),
            "decision_id": str(decision_id),
            "reservation_id": str(risk_reservation_id),
            "execution_mode": "SHADOW",
            "dispatch_eligible": False,
        }
        intent = OrderIntent(
            order_intent_id=order_intent_id,
            execution_risk_decision_id=decision_id,
            proposal_id=campaign.proposal_id,
            proposal_version_id=campaign.proposal_version_id,
            authorization_id=authorization.authorization_id,
            campaign_id=campaign.campaign_id,
            initial_authorization_id=initial_id,
            add_package_id=add_package_id,
            add_unit_id=add_unit_id,
            intent_kind=request.intent_kind.value,
            candidate_ref=request.candidate_ref,
            candidate_hash=candidate_hash,
            strategy_owner=campaign.strategy_id,
            venue=campaign.venue,
            execution_domain=campaign.execution_domain,
            account_id=campaign.account_id,
            worker_id=binding.worker_id,
            instrument_id=campaign.instrument_id,
            side="BUY" if campaign.direction == "LONG" else "SELL",
            position_side=campaign.direction,
            reduce_only=False,
            current_position_quantity=request.current_position_quantity,
            target_position_quantity=request.target_position_quantity,
            expected_quantity=evaluation.requested_quantity,
            max_quantity=(
                authorization_rows["initial"].max_quantity
                if request.intent_kind is IntentKind.INITIAL
                else evaluation.requested_quantity
            ),
            quantity_source=(
                "INITIAL_RISK_APPROVED"
                if request.intent_kind is IntentKind.INITIAL
                else "TARGET_LEVERAGE_DELTA"
            ),
            order_type=request.order_type,
            time_in_force=request.time_in_force,
            trigger_price=proposal.trigger_price,
            limit_price=proposal.limit_price,
            price_reference=price_reference,
            price_lower_bound=price_lower,
            price_upper_bound=price_upper,
            max_slippage_bps=proposal.max_slippage_bps,
            risk_currency=request.risk_currency,
            margin_mode=binding.margin_mode,
            collateral_scope=binding.collateral_scope,
            collateral_pool_id=binding.collateral_pool_id,
            capability_certificate_ref=authorization.capability_certificate_ref,
            execution_mode="SHADOW",
            dispatch_eligible=False,
            intent_snapshot=intent_snapshot,
            intent_snapshot_hash=hash_json(intent_snapshot),
            valid_from=now,
            valid_until=valid_until,
            created_at=now,
        )
        session.add(intent)
        session.flush()

        scope_allocations = [
            {
                "scope_type": scope.scope_type.value,
                "scope_id": scope.scope_id,
                "planned_loss": str(evaluation.requested_incremental_worst_case_loss),
                "stress_loss": str(scope.requested_incremental_stress_loss),
            }
            for scope in sorted(
                request.risk_request.scope_risks,
                key=lambda item: (item.scope_type.value, item.scope_id),
            )
        ]
        funding = request.risk_request.requested.requested_funding
        margin = request.risk_request.requested.requested_margin
        base_heat = request.risk_request.requested_base_heat
        protected_profit_giveback = ZERO
        cost_stress_add_on = evaluation.requested_cost_stress_add_on
        reservation = RiskReservation(
            risk_reservation_id=risk_reservation_id,
            execution_risk_decision_id=decision_id,
            order_intent_id=order_intent_id,
            organization_id=campaign.organization_id,
            authorization_id=authorization.authorization_id,
            campaign_id=campaign.campaign_id,
            initial_authorization_id=initial_id,
            add_package_id=add_package_id,
            add_unit_id=add_unit_id,
            intent_kind=request.intent_kind.value,
            account_id=campaign.account_id,
            instrument_id=campaign.instrument_id,
            collateral_pool_id=binding.collateral_pool_id,
            risk_currency=request.risk_currency,
            valuation_price=request.risk_request.market.executable_price,
            valuation_price_source_ref=request.valuation_price_source_ref,
            reserved_quantity=evaluation.requested_quantity,
            reserved_heat=reserved_heat,
            base_heat_reserved=base_heat,
            protected_profit_giveback_reserved=protected_profit_giveback,
            cost_stress_add_on_reserved=cost_stress_add_on,
            funding_reserved=funding,
            margin_reserved=margin,
            scope_allocations=scope_allocations,
            valid_until=valid_until,
            created_at=now,
        )
        session.add(reservation)
        session.flush()

        reserve_evidence_ref = f"execution-risk-decision:{decision_id}"
        reserve_evidence_hash = hash_json(decision_payload)
        session.add(
            RiskLedgerEntry(
                risk_ledger_entry_id=uuid4(),
                risk_reservation_id=risk_reservation_id,
                order_intent_id=order_intent_id,
                execution_fact_id=None,
                entry_sequence=1,
                entry_type="RESERVE",
                from_bucket="AUTHORIZED",
                to_bucket="RESERVED",
                quantity=evaluation.requested_quantity,
                heat=reserved_heat,
                funding=funding,
                margin=margin,
                evidence_ref=reserve_evidence_ref,
                evidence_hash=reserve_evidence_hash,
                occurred_at=now,
            )
        )
        session.flush()
        session.add_all(
            (
                OrderIntentState(
                    order_intent_id=order_intent_id,
                    status="INTENT_CREATED",
                    version=1,
                    intent_quantity=evaluation.requested_quantity,
                    cumulative_filled_quantity=ZERO,
                    known_remaining_quantity=evaluation.requested_quantity,
                    zero_fill_confirmed=False,
                    venue_order_terminal=False,
                    position_reconciled=False,
                    protection_confirmed=False,
                    last_fact_sequence=0,
                    last_fact_hash=None,
                    reason_code="ATOMIC_RISK_RESERVATION_CREATED",
                    updated_at=now,
                ),
                RiskExposureState(
                    risk_reservation_id=risk_reservation_id,
                    status="RESERVED",
                    version=1,
                    ledger_sequence=1,
                    total_quantity=evaluation.requested_quantity,
                    reserved_quantity=evaluation.requested_quantity,
                    open_quantity=ZERO,
                    unknown_quantity=ZERO,
                    released_quantity=ZERO,
                    total_heat=reserved_heat,
                    reserved_heat=reserved_heat,
                    open_heat=ZERO,
                    unknown_heat=ZERO,
                    released_heat=ZERO,
                    total_funding=funding,
                    funding_reserved=funding,
                    funding_used=ZERO,
                    funding_unknown=ZERO,
                    funding_released=ZERO,
                    total_margin=margin,
                    margin_reserved=margin,
                    margin_used=ZERO,
                    margin_unknown=ZERO,
                    margin_released=ZERO,
                    last_evidence_ref=reserve_evidence_ref,
                    last_evidence_hash=reserve_evidence_hash,
                    reason_code="ATOMIC_RISK_RESERVATION_CREATED",
                    updated_at=now,
                ),
            )
        )
        if request.intent_kind is IntentKind.ADD:
            add_state = authorization_rows["add_unit_state"]
            add_state.status = "CLAIMED"
            add_state.version += 1
            add_state.reason_code = "ORDER_INTENT_RISK_RESERVED"
            add_state.updated_at = now
        session.flush()
        EXECUTION_RISK_DECISIONS.labels(
            request.intent_kind.value, "ALLOW", "ORDER_PRECHECK_PASSED"
        ).inc()
        RISK_RESERVATION_TRANSITIONS.labels("AUTHORIZED_TO_RESERVED").inc()
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="OrderIntent",
            object_id=str(order_intent_id),
            object_version=1,
            data={
                "execution_risk_decision_id": str(decision_id),
                "order_intent_id": str(order_intent_id),
                "risk_reservation_id": str(risk_reservation_id),
                "intent_status": "INTENT_CREATED",
                "risk_exposure_status": "RESERVED",
                "capital_projection_hash": verified_capital.projection_hash,
                "durable_exposure_snapshot_hash": verified_exposure.snapshot_hash,
                "execution_mode": "SHADOW",
                "dispatch_eligible": False,
                "reservation_created": True,
            },
            events=(
                DomainEvent(
                    event_type="ShadowOrderIntentRiskReserved",
                    aggregate_type="OrderIntent",
                    aggregate_id=str(order_intent_id),
                    payload={
                        "campaign_id": str(campaign_id),
                        "intent_kind": request.intent_kind.value,
                        "execution_risk_decision_id": str(decision_id),
                        "risk_reservation_id": str(risk_reservation_id),
                        "capital_projection_hash": verified_capital.projection_hash,
                        "durable_exposure_snapshot_hash": verified_exposure.snapshot_hash,
                        "dispatch_eligible": False,
                    },
                ),
            ),
        )

    @staticmethod
    def _require_internal(envelope: CommandEnvelope) -> None:
        if envelope.command_type != ExecutionIntentService.command_type:
            raise CommandRejected("COMMAND_TYPE_MISMATCH", "unexpected command type")
        if envelope.payload_schema_version != ExecutionIntentService.payload_schema_version:
            raise CommandRejected(
                "PAYLOAD_SCHEMA_VERSION_MISMATCH",
                "execution intent payload schema version is unsupported",
            )
        if (
            envelope.channel is not CommandChannel.INTERNAL
            or envelope.service_principal != EXECUTION_INTENT_SERVICE_PRINCIPAL
        ):
            raise CommandRejected(
                "EXECUTION_SERVICE_REQUIRED",
                "only the Trading OMS risk-reservation service may create intents",
            )
        if envelope.object_type != "Campaign" or envelope.object_id is None:
            raise CommandRejected("OBJECT_BINDING_MISMATCH", "Campaign binding is required")

    @staticmethod
    def _authorization_ids(
        request: CreateExecutionIntentRequest,
    ) -> tuple[UUID | None, UUID | None, UUID | None]:
        return (
            request.initial_authorization_id,
            request.add_package_id,
            request.add_unit_id,
        )

    @staticmethod
    def _validate_root_integrity(
        authorization: TradingAuthorization,
        campaign: Campaign,
        proposal: FrozenProposalVersion,
        request: CreateExecutionIntentRequest,
        now: datetime,
    ) -> FrozenAuthorizationBinding:
        if (
            authorization.authorization_mode != "SHADOW"
            or authorization.execution_eligible
            or hash_json(authorization.issuance_snapshot) != authorization.issuance_snapshot_hash
        ):
            raise CommandRejected(
                "AUTHORIZATION_INTEGRITY_FAILED", "authorization root integrity failed"
            )
        if (
            authorization.proposal_version_id != proposal.proposal_version_id
            or authorization.authorization_id != campaign.authorization_id
            or authorization.proposal_spec_hash != proposal.spec_hash
            or authorization.risk_summary_hash != proposal.risk_summary_hash
        ):
            raise CommandRejected(
                "AUTHORIZATION_BINDING_MISMATCH", "frozen authorization bindings changed"
            )
        if now >= authorization.valid_until or now >= proposal.valid_until:
            raise CommandRejected("AUTHORIZATION_EXPIRED", "frozen authorization expired")
        if request.order_type != proposal.order_type:
            raise CommandRejected("ORDER_TYPE_CHANGED", "frozen order type changed")
        try:
            return FrozenAuthorizationBinding.model_validate(
                authorization.issuance_snapshot.get("binding")
            )
        except ValidationError as exc:
            raise CommandRejected(
                "AUTHORIZATION_BINDING_INVALID", "authorization binding is incomplete"
            ) from exc

    @staticmethod
    def _frozen_capital_projection_binding(
        authorization: TradingAuthorization,
    ) -> CapitalProjectionBinding:
        try:
            return CapitalProjectionBinding.model_validate(
                authorization.issuance_snapshot.get("capital_projection_binding")
            )
        except ValidationError as exc:
            raise CommandRejected(
                "CAPITAL_PROJECTION_BINDING_INVALID",
                "authorization lacks the frozen capital projection binding",
            ) from exc

    @staticmethod
    def _lock_and_validate_authorization_kind(
        session: Session,
        request: CreateExecutionIntentRequest,
        campaign: Campaign,
        campaign_state: CampaignState,
        authorization: TradingAuthorization,
        system_state: SystemRiskState,
        now: datetime,
    ) -> dict[str, Any]:
        initial = session.execute(
            select(InitialOrderAuthorization)
            .where(InitialOrderAuthorization.campaign_id == campaign.campaign_id)
            .with_for_update()
        ).scalar_one()
        initial_state = session.execute(
            select(InitialAuthorizationState)
            .where(
                InitialAuthorizationState.initial_authorization_id
                == initial.initial_authorization_id
            )
            .with_for_update()
        ).scalar_one()
        rows: dict[str, Any] = {
            "initial": initial,
            "initial_state": initial_state,
        }
        if request.intent_kind is IntentKind.INITIAL:
            if request.initial_authorization_id != initial.initial_authorization_id:
                raise CommandRejected(
                    "INITIAL_AUTHORIZATION_MISMATCH", "initial authorization binding changed"
                )
            if initial_state.status != "ACTIVE" or campaign_state.status != "PENDING_ENTRY":
                raise CommandRejected(
                    "INITIAL_AUTHORIZATION_UNAVAILABLE", "initial authorization is unavailable"
                )
            if now < initial.valid_from or now >= initial.valid_until:
                raise CommandRejected(
                    "INITIAL_AUTHORIZATION_EXPIRED", "initial authorization expired"
                )
            if request.risk_request.requested.requested_quantity > initial.max_quantity:
                raise CommandRejected(
                    "INITIAL_QUANTITY_EXCEEDS_AUTHORIZATION",
                    "initial quantity exceeds frozen authorization",
                )
            blocking = session.execute(
                select(OrderIntentState.status)
                .join(OrderIntent, OrderIntent.order_intent_id == OrderIntentState.order_intent_id)
                .where(
                    OrderIntent.initial_authorization_id == initial.initial_authorization_id,
                    OrderIntentState.status.in_(BLOCKING_INITIAL_STATUSES),
                )
                .with_for_update()
            ).first()
            if blocking is not None:
                raise CommandRejected(
                    "INITIAL_INTENT_ALREADY_ACTIVE",
                    "an unresolved or positively filled initial intent already exists",
                )
            return rows

        if not authorization.auto_add_enabled:
            raise CommandRejected("AUTO_ADD_DISABLED", "auto-add was not frozen as enabled")
        package = session.execute(
            select(AddAuthorizationPackage)
            .where(AddAuthorizationPackage.add_package_id == request.add_package_id)
            .with_for_update()
        ).scalar_one_or_none()
        unit = session.execute(
            select(AddUnit).where(AddUnit.add_unit_id == request.add_unit_id).with_for_update()
        ).scalar_one_or_none()
        if (
            package is None
            or unit is None
            or package.authorization_id != authorization.authorization_id
            or package.campaign_id != campaign.campaign_id
            or unit.add_package_id != package.add_package_id
        ):
            raise CommandRejected("ADD_AUTHORIZATION_MISMATCH", "add authorization changed")
        package_state = session.execute(
            select(AddAuthorizationPackageState)
            .where(AddAuthorizationPackageState.add_package_id == package.add_package_id)
            .with_for_update()
        ).scalar_one()
        unit_states = tuple(
            session.execute(
                select(AddUnit, AddUnitState)
                .join(AddUnitState, AddUnitState.add_unit_id == AddUnit.add_unit_id)
                .where(AddUnit.add_package_id == package.add_package_id)
                .order_by(AddUnit.ordinal)
                .with_for_update()
            ).all()
        )
        selected_state = next(state for candidate, state in unit_states if candidate == unit)
        eligibility = request.add_eligibility
        if eligibility is None:  # pragma: no cover - Pydantic enforces
            raise RuntimeError("missing add eligibility")
        if (
            package_state.status != "ACTIVE"
            or selected_state.status != "AVAILABLE"
            or campaign_state.status != "OPEN"
            or initial_state.status != "CONSUMED"
            or system_state is not SystemRiskState.NORMAL
        ):
            raise CommandRejected("ADD_AUTHORIZATION_UNAVAILABLE", "add authorization unavailable")
        if now < package.valid_from or now >= package.valid_until:
            raise CommandRejected("ADD_AUTHORIZATION_EXPIRED", "add authorization expired")
        if any(
            state.status != "CONSUMED"
            for candidate, state in unit_states
            if candidate.ordinal < unit.ordinal
        ):
            raise CommandRejected("ADD_SEQUENCE_NOT_READY", "earlier AddUnit is not consumed")
        if any(
            state.status not in {"AVAILABLE", "INVALIDATED", "EXPIRED"}
            for candidate, state in unit_states
            if candidate.ordinal > unit.ordinal
        ):
            raise CommandRejected("LATER_ADD_UNIT_LOCKED", "later AddUnit state is inconsistent")
        if eligibility.frozen_return_pct < Decimal(unit.unlock_milestone_pct):
            raise CommandRejected("ADD_MILESTONE_NOT_MET", "frozen-return milestone is not met")
        if not (
            eligibility.trend_valid
            and eligibility.protection_valid
            and eligibility.authorization_valid
        ):
            raise CommandRejected("ADD_ELIGIBILITY_FAILED", "add hard gate failed")
        if eligibility.current_effective_leverage >= package.target_leverage_min:
            raise CommandRejected(
                "ADD_LEVERAGE_NOT_BELOW_MINIMUM",
                "effective leverage is not below the frozen minimum",
            )
        if not (
            package.target_leverage_min
            <= eligibility.target_effective_leverage
            <= package.target_leverage_max
        ):
            raise CommandRejected("ADD_TARGET_LEVERAGE_INVALID", "target leverage changed")
        rows.update(
            {
                "add_package": package,
                "add_package_state": package_state,
                "add_unit": unit,
                "add_unit_state": selected_state,
                "add_unit_states": unit_states,
            }
        )
        return rows

    @staticmethod
    def _lock_competing_scopes(
        session: Session,
        request: CreateExecutionIntentRequest,
        campaign: Campaign,
        proposal: FrozenProposalVersion,
        binding: FrozenAuthorizationBinding,
    ) -> None:
        expected = {
            ScopeType.UNDERLYING: binding.underlying_id,
            ScopeType.RISK_CLUSTER: binding.risk_cluster_id,
            ScopeType.SECTOR: proposal.sector,
            ScopeType.EXECUTION_DOMAIN: campaign.execution_domain,
            ScopeType.VENUE: campaign.venue,
            ScopeType.COLLATERAL_POOL: binding.collateral_pool_id,
            ScopeType.PORTFOLIO: campaign.organization_id,
        }
        actual = {item.scope_type: item.scope_id for item in request.risk_request.scope_risks}
        if actual != expected:
            raise CommandRejected("RISK_SCOPE_BINDING_MISMATCH", "risk scopes changed")
        keys = {f"risk-scope:{scope.value}:{scope_id}" for scope, scope_id in expected.items()}
        keys.update(
            {
                f"campaign:{campaign.campaign_id}",
                f"account:{campaign.venue}:{campaign.execution_domain}:{campaign.account_id}",
                f"instrument:{campaign.venue}:{campaign.instrument_id}",
                f"collateral:{binding.collateral_pool_id}",
            }
        )
        for key in sorted(keys):
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": key},
            )

    @staticmethod
    def _load_policy(
        session: Session, organization_id: str, policy_version: str
    ) -> tuple[RiskPolicyRecord, RiskPolicyParameters]:
        record = session.execute(
            select(RiskPolicyRecord).where(
                RiskPolicyRecord.organization_id == organization_id,
                RiskPolicyRecord.policy_version == policy_version,
            )
        ).scalar_one_or_none()
        if record is None:
            raise CommandRejected("RISK_POLICY_UNAVAILABLE", "risk policy is unavailable")
        if (
            record.policy_mode != "SHADOW"
            or hash_json(record.parameters) != record.policy_hash
            or not record.evidence_refs
        ):
            raise CommandRejected("RISK_POLICY_INTEGRITY_FAILED", "risk policy integrity failed")
        try:
            return record, RiskPolicyParameters.model_validate(record.parameters)
        except ValidationError as exc:
            raise CommandRejected("RISK_POLICY_INVALID", "risk policy is invalid") from exc

    @staticmethod
    def _validate_risk_bindings(
        request: CreateExecutionIntentRequest,
        authorization: TradingAuthorization,
        campaign: Campaign,
        proposal: FrozenProposalVersion,
        binding: FrozenAuthorizationBinding,
        frozen_capital_binding: CapitalProjectionBinding,
        system_state: SystemRiskState,
        rows: dict[str, Any],
    ) -> None:
        risk = request.risk_request
        expected_binding = {
            "proposal_source": authorization.source,
            "strategy_id": campaign.strategy_id,
            "strategy_version": campaign.strategy_version,
            "strategy_parameter_version": binding.strategy_parameter_version,
            "authorization_policy_version": authorization.authorization_policy_version,
            "instrument_identity": campaign.instrument_id,
            "contract_multiplier": binding.contract_multiplier,
            "underlying_id": binding.underlying_id,
            "sector_id": proposal.sector,
            "risk_cluster_id": binding.risk_cluster_id,
            "venue": campaign.venue,
            "execution_domain": campaign.execution_domain,
            "account_id": campaign.account_id,
            "account_abstraction": binding.account_abstraction,
            "position_mode": binding.position_mode,
            "margin_mode": binding.margin_mode,
            "collateral_scope": binding.collateral_scope,
            "collateral_pool_id": binding.collateral_pool_id,
            "settlement_asset": binding.settlement_asset,
            "adapter_version": binding.adapter_version,
            "worker_id": binding.worker_id,
            "worker_config_hash": binding.worker_config_hash,
            "credential_fingerprint": binding.credential_fingerprint,
            "freqtrade_worker_version": binding.freqtrade_worker_version,
            "account_capability_version": binding.account_capability_version,
            "credential_permission_profile_version": (
                binding.credential_permission_profile_version
            ),
            "venue_client_version": binding.venue_client_version,
            "instrument_scope_version": binding.instrument_scope_version,
            "catalog_version": authorization.catalog_version,
            "execution_capability_version": authorization.execution_capability_version,
            "position_management_template_version": (binding.position_management_template_version),
            "add_milestone_policy_version": binding.add_milestone_policy_version,
            "requested_add_count": authorization.requested_add_count,
            "capability_certificate_ref": authorization.capability_certificate_ref,
        }
        if risk.binding.model_dump(mode="python") != expected_binding:
            raise CommandRejected("CERTIFICATION_BINDING_MISMATCH", "certification binding changed")
        if risk.capital_projection_binding != frozen_capital_binding:
            raise CommandRejected(
                "FROZEN_CAPITAL_SCOPE_BINDING_MISMATCH",
                "final precheck capital scope differs from the frozen authorization",
            )
        if (
            risk.organization_id != campaign.organization_id
            or risk.proposal_ref != str(proposal.proposal_version_id)
            or risk.candidate_version != proposal.version
            or risk.policy_version != authorization.risk_policy_version
            or risk.risk_tier.value != authorization.risk_tier
            or risk.capital.total_capital_snapshot_0 != authorization.total_capital_snapshot_0
            or risk.market.direction.value != campaign.direction
            or risk.market.initial_invalidation_price != proposal.initial_invalidation_price
            or risk.market.contract_multiplier != binding.contract_multiplier
            or risk.market.max_slippage_bps != proposal.max_slippage_bps
            or request.order_type != proposal.order_type
        ):
            raise CommandRejected("FROZEN_RISK_BINDING_MISMATCH", "frozen risk input changed")
        if request.intent_kind is IntentKind.INITIAL:
            initial = rows["initial"]
            if not (
                initial.price_lower_bound
                <= risk.market.executable_price
                <= initial.price_upper_bound
            ):
                raise CommandRejected(
                    "INITIAL_PRICE_OUTSIDE_AUTHORIZATION",
                    "execution price left the frozen authorization boundary",
                )
        elif system_state is not SystemRiskState.NORMAL:
            raise CommandRejected("ADD_REQUIRES_NORMAL_SYSTEM_STATE", "add requires NORMAL")
        else:
            eligibility = request.add_eligibility
            if eligibility is None:  # pragma: no cover - Pydantic enforces
                raise RuntimeError("missing add eligibility")
            price_per_unit = risk.market.executable_price * risk.market.contract_multiplier
            raw_delta = (
                eligibility.current_position_equity * eligibility.target_effective_leverage
                - request.current_position_quantity * price_per_unit
            ) / price_per_unit
            expected_delta = ExecutionIntentService._floor_to_step(
                raw_delta, risk.requested.quantity_step
            )
            if expected_delta <= ZERO or expected_delta != risk.requested.requested_quantity:
                raise CommandRejected(
                    "ADD_TARGET_DELTA_MISMATCH",
                    "add quantity is not the equity-based target-leverage delta",
                )
        if request.risk_currency != binding.settlement_asset:
            raise CommandRejected("RISK_CURRENCY_MISMATCH", "settlement currency changed")

    @staticmethod
    def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
        if value <= ZERO:
            return ZERO
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    @staticmethod
    def _input_snapshot(
        envelope: CommandEnvelope,
        request: CreateExecutionIntentRequest,
        evaluation_input: RiskEvaluationInput,
        authorization: TradingAuthorization,
        rows: dict[str, Any],
        verified_capital: VerifiedCapitalProjection,
        verified_exposure: VerifiedDurableExposure,
    ) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "request": request.model_dump(mode="json"),
            "evaluation": evaluation_input.model_dump(mode="json"),
            "authorization": {
                "authorization_id": str(authorization.authorization_id),
                "issuance_snapshot_hash": authorization.issuance_snapshot_hash,
                "valid_until": authorization.valid_until.isoformat(),
            },
            "capital_projection": verified_capital.projection.model_dump(mode="json"),
            "capital_projection_hash": verified_capital.projection_hash,
            "durable_exposure_snapshot": verified_exposure.snapshot.model_dump(mode="json"),
            "durable_exposure_snapshot_hash": verified_exposure.snapshot_hash,
            "protected_position_risk": (
                verified_exposure.protected_position_risk.projection.model_dump(mode="json")
                if verified_exposure.protected_position_risk is not None
                else None
            ),
            "protected_position_risk_valid_until": (
                verified_exposure.protected_position_risk.valid_until.isoformat()
                if verified_exposure.protected_position_risk is not None
                else None
            ),
            "command_context": {
                "command_id": str(envelope.command_id),
                "correlation_id": str(envelope.correlation_id),
                "caller_id": envelope.caller_id,
                "auth_context_ref": envelope.auth_context_ref,
            },
        }
        if request.intent_kind is IntentKind.ADD:
            snapshot["add_state_versions"] = {
                "package": rows["add_package_state"].version,
                "unit": rows["add_unit_state"].version,
            }
        else:
            snapshot["initial_state_version"] = rows["initial_state"].version
        return snapshot

    @staticmethod
    def _price_contract(
        request: CreateExecutionIntentRequest, rows: dict[str, Any]
    ) -> tuple[Decimal, Decimal, Decimal]:
        if request.intent_kind is IntentKind.INITIAL:
            initial = rows["initial"]
            return (
                initial.price_reference,
                initial.price_lower_bound,
                initial.price_upper_bound,
            )
        reference = request.risk_request.market.executable_price
        slippage = request.risk_request.market.max_slippage_bps / Decimal("10000")
        lower = reference * (Decimal("1") - slippage)
        upper = reference * (Decimal("1") + slippage)
        if lower <= ZERO:
            raise CommandRejected("PRICE_BOUNDARY_INVALID", "add price boundary is invalid")
        return reference, lower, upper

    @staticmethod
    def _record_denial(
        session: Session,
        request: CreateExecutionIntentRequest,
        campaign: Campaign,
        authorization: TradingAuthorization,
        policy_record: RiskPolicyRecord,
        evaluation_input: RiskEvaluationInput,
        evaluation: RiskEvaluationResult,
        verified_capital: VerifiedCapitalProjection,
        verified_exposure: VerifiedDurableExposure,
        now: datetime,
        reason_code: str,
    ) -> CommandOutcome:
        decision_id = uuid4()
        initial_id, add_package_id, add_unit_id = ExecutionIntentService._authorization_ids(request)
        input_snapshot = {
            "request": request.model_dump(mode="json"),
            "evaluation": evaluation_input.model_dump(mode="json"),
            "authorization_id": str(authorization.authorization_id),
            "authorization_snapshot_hash": authorization.issuance_snapshot_hash,
            "capital_projection": verified_capital.projection.model_dump(mode="json"),
            "capital_projection_hash": verified_capital.projection_hash,
            "durable_exposure_snapshot": verified_exposure.snapshot.model_dump(mode="json"),
            "durable_exposure_snapshot_hash": verified_exposure.snapshot_hash,
        }
        decision_payload = evaluation.model_dump(mode="json")
        decision_payload["result"] = "DENY"
        decision_payload["primary_reason_code"] = reason_code
        decision_payload["final_quantity"] = "0"
        decision_payload["execution_eligible"] = False
        decision_payload["reservation_created"] = False
        decision_payload["order_intent_created"] = False
        session.add(
            ExecutionRiskDecision(
                execution_risk_decision_id=decision_id,
                decision_stage="ORDER_PRECHECK",
                intent_kind=request.intent_kind.value,
                organization_id=campaign.organization_id,
                authorization_id=authorization.authorization_id,
                campaign_id=campaign.campaign_id,
                initial_authorization_id=initial_id,
                add_package_id=add_package_id,
                add_unit_id=add_unit_id,
                risk_policy_id=policy_record.risk_policy_id,
                risk_policy_version=policy_record.policy_version,
                capital_scope_manifest_id=verified_capital.projection.manifest_id,
                capital_scope_manifest_version=verified_capital.projection.manifest_version,
                capital_scope_manifest_hash=verified_capital.projection.manifest_hash,
                capital_projection_version=verified_capital.projection.projection_version,
                capital_projection_hash=verified_capital.projection_hash,
                durable_exposure_snapshot_hash=verified_exposure.snapshot_hash,
                system_risk_state=evaluation_input.system_risk_state.value,
                result="DENY",
                primary_reason_code=reason_code,
                requested_quantity=request.risk_request.requested.requested_quantity,
                max_safe_quantity=ZERO,
                final_quantity=ZERO,
                approved_reserved_heat=ZERO,
                approved_funding=ZERO,
                approved_margin=ZERO,
                current_portfolio_mtm_equity=evaluation.current_portfolio_mtm_equity,
                current_unrealized_pnl=evaluation.current_unrealized_pnl,
                input_snapshot=input_snapshot,
                input_hash=hash_json(input_snapshot),
                decision=decision_payload,
                decision_hash=hash_json(decision_payload),
                execution_eligible=False,
                reservation_created=False,
                order_intent_created=False,
                decided_at=now,
                valid_until=now,
            )
        )
        session.flush()
        EXECUTION_RISK_DECISIONS.labels(request.intent_kind.value, "DENY", reason_code).inc()
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="ExecutionRiskDecision",
            object_id=str(decision_id),
            object_version=1,
            data={
                "execution_risk_decision_id": str(decision_id),
                "result": "DENY",
                "primary_reason_code": reason_code,
                "capital_projection_hash": verified_capital.projection_hash,
                "durable_exposure_snapshot_hash": verified_exposure.snapshot_hash,
                "dispatch_eligible": False,
                "reservation_created": False,
                "order_intent_created": False,
            },
            events=(
                DomainEvent(
                    event_type="ExecutionRiskIncreaseDenied",
                    aggregate_type="ExecutionRiskDecision",
                    aggregate_id=str(decision_id),
                    payload={
                        "campaign_id": str(campaign.campaign_id),
                        "intent_kind": request.intent_kind.value,
                        "primary_reason_code": reason_code,
                        "capital_projection_hash": verified_capital.projection_hash,
                        "durable_exposure_snapshot_hash": verified_exposure.snapshot_hash,
                    },
                ),
            ),
        )

    @staticmethod
    def _existing_outcome(session: Session, intent: OrderIntent) -> CommandOutcome:
        reservation = session.execute(
            select(RiskReservation).where(RiskReservation.order_intent_id == intent.order_intent_id)
        ).scalar_one()
        state = session.get(OrderIntentState, intent.order_intent_id)
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="OrderIntent",
            object_id=str(intent.order_intent_id),
            object_version=state.version if state is not None else 1,
            data={
                "order_intent_id": str(intent.order_intent_id),
                "execution_risk_decision_id": str(intent.execution_risk_decision_id),
                "risk_reservation_id": str(reservation.risk_reservation_id),
                "intent_status": state.status if state is not None else "INTENT_CREATED",
                "already_created": True,
                "execution_mode": "SHADOW",
                "dispatch_eligible": False,
                "reservation_created": True,
            },
            events=(
                DomainEvent(
                    event_type="ShadowOrderIntentAlreadyExists",
                    aggregate_type="OrderIntent",
                    aggregate_id=str(intent.order_intent_id),
                    payload={"candidate_ref": intent.candidate_ref},
                ),
            ),
        )


class ExecutionReconciliationService:
    command_type = "execution.fact.record-reconciled.v5"

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def record(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        self._require_internal(envelope)
        if envelope.object_id is None:  # pragma: no cover - validated above
            raise RuntimeError("missing OrderIntent id")
        try:
            order_intent_id = UUID(envelope.object_id)
            request = RecordExecutionFactRequest.model_validate(envelope.payload)
        except (ValueError, ValidationError) as exc:
            raise CommandRejected("EXECUTION_FACT_INVALID", "execution fact is invalid") from exc
        if request.target_status not in ORDER_INTENT_STATUSES:
            raise CommandRejected("EXECUTION_STATUS_INVALID", "target status is unsupported")

        intent = session.execute(
            select(OrderIntent)
            .where(OrderIntent.order_intent_id == order_intent_id)
            .with_for_update()
        ).scalar_one_or_none()
        if intent is None:
            raise CommandRejected("ORDER_INTENT_NOT_FOUND", "OrderIntent is unavailable")
        if envelope.scope.get("organization_id") is None:
            raise CommandRejected("SCOPE_MISMATCH", "organization scope is required")
        reservation = session.execute(
            select(RiskReservation)
            .where(RiskReservation.order_intent_id == order_intent_id)
            .with_for_update()
        ).scalar_one()
        if envelope.scope.get("organization_id") != reservation.organization_id:
            raise CommandRejected("SCOPE_MISMATCH", "organization scope changed")
        state = session.execute(
            select(OrderIntentState)
            .where(OrderIntentState.order_intent_id == order_intent_id)
            .with_for_update()
        ).scalar_one()
        exposure = session.execute(
            select(RiskExposureState)
            .where(RiskExposureState.risk_reservation_id == reservation.risk_reservation_id)
            .with_for_update()
        ).scalar_one()
        self._lock_reconciliation_scope(session, intent, reservation)
        self._validate_route(intent, request)

        existing = session.execute(
            select(ExecutionFact).where(
                ExecutionFact.venue == request.venue,
                ExecutionFact.execution_domain == request.execution_domain,
                ExecutionFact.account_id == request.account_id,
                ExecutionFact.external_fact_id == request.external_fact_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if (
                existing.order_intent_id != order_intent_id
                or existing.payload_hash != request.payload_hash
                or existing.evidence_hash != request.evidence_hash
                or existing.fact_sequence != request.fact_sequence
                or existing.fact_contract_version != 5
                or existing.fact_kind != request.fact_kind.value
                or existing.shadow_dispatch_claim_id != request.shadow_dispatch_claim_id
                or existing.reconciliation_run_id != request.reconciliation_run_id
                or existing.reconciliation_input_id != request.reconciliation_input_id
                or existing.reconciliation_source_type != request.reconciliation_source_type.value
                or existing.reconciliation_run_hash != request.reconciliation_run_hash
                or existing.reconciliation_input_hash != request.reconciliation_input_hash
                or existing.dispatch_claim_hash != request.dispatch_claim_hash
                or existing.venue_order_observation_id != request.venue_order_observation_id
                or existing.venue_fill_id != request.venue_fill_id
                or existing.venue_position_snapshot_id != request.venue_position_snapshot_id
                or existing.venue_protection_snapshot_id != request.venue_protection_snapshot_id
                or existing.venue_fact_input_link_id != request.venue_fact_input_link_id
                or existing.venue_fact_hash != request.venue_fact_hash
            ):
                raise CommandRejected(
                    "EXTERNAL_FACT_ID_CONFLICT",
                    "external fact id belongs to different semantics",
                )
            if request.fact_kind in {
                ExecutionFactKind.VENUE_ORDER,
                ExecutionFactKind.VENUE_FILL,
                ExecutionFactKind.VENUE_POSITION,
                ExecutionFactKind.VENUE_PROTECTION,
            }:
                EXECUTION_CANONICAL_FACT_BINDINGS.labels(request.fact_kind.value, "REPLAYED").inc()
            return self._existing_fact_outcome(session, state, existing)

        now = self._clock()
        authority_mode = self._validate_reconciliation_binding(
            session, intent, reservation, request, now
        )
        try:
            (
                canonical_venue_order_id,
                venue_position_snapshot_id,
                venue_protection_snapshot_id,
            ) = self._validate_canonical_venue_fact(session, intent, state, request)
        except CommandRejected:
            if request.fact_kind in {
                ExecutionFactKind.VENUE_ORDER,
                ExecutionFactKind.VENUE_FILL,
                ExecutionFactKind.VENUE_POSITION,
                ExecutionFactKind.VENUE_PROTECTION,
            }:
                EXECUTION_CANONICAL_FACT_BINDINGS.labels(request.fact_kind.value, "REJECTED").inc()
            raise
        if any(
            value is not None
            for value in (
                canonical_venue_order_id,
                venue_position_snapshot_id,
                venue_protection_snapshot_id,
            )
        ):
            EXECUTION_CANONICAL_FACT_BINDINGS.labels(request.fact_kind.value, "APPLIED").inc()
        self._validate_progression(state, request, now)
        fact_id = uuid4()
        fact = ExecutionFact(
            execution_fact_id=fact_id,
            order_intent_id=order_intent_id,
            fact_sequence=request.fact_sequence,
            fact_contract_version=5,
            fact_kind=request.fact_kind.value,
            target_status=request.target_status,
            venue=request.venue,
            execution_domain=request.execution_domain,
            account_id=request.account_id,
            external_fact_id=request.external_fact_id,
            cumulative_filled_quantity=request.cumulative_filled_quantity,
            known_remaining_quantity=request.known_remaining_quantity,
            zero_fill_confirmed=request.zero_fill_confirmed,
            venue_order_terminal=request.venue_order_terminal,
            position_reconciled=request.position_reconciled,
            protection_confirmed=request.protection_confirmed,
            reconciliation_run_ref=None,
            shadow_dispatch_claim_id=request.shadow_dispatch_claim_id,
            reconciliation_run_id=request.reconciliation_run_id,
            reconciliation_input_id=request.reconciliation_input_id,
            reconciliation_source_type=request.reconciliation_source_type.value,
            reconciliation_run_hash=request.reconciliation_run_hash,
            reconciliation_input_hash=request.reconciliation_input_hash,
            dispatch_claim_hash=request.dispatch_claim_hash,
            venue_order_observation_id=request.venue_order_observation_id,
            venue_fill_id=request.venue_fill_id,
            venue_position_snapshot_id=venue_position_snapshot_id,
            venue_protection_snapshot_id=venue_protection_snapshot_id,
            venue_fact_input_link_id=request.venue_fact_input_link_id,
            venue_fact_hash=request.venue_fact_hash,
            canonical_venue_order_id=canonical_venue_order_id,
            source_ref=request.source_ref,
            source_version=request.source_version,
            payload=request.payload,
            payload_hash=request.payload_hash,
            evidence_ref=request.evidence_ref,
            evidence_hash=request.evidence_hash,
            event_time=request.event_time,
            received_at=request.received_at,
            recorded_at=now,
        )
        session.add(fact)
        session.flush()

        old_quantities = self._quantity_buckets(exposure)
        new_quantities = self._desired_quantity_buckets(state.intent_quantity, request)
        transfers = self._bucket_transfers(old_quantities, new_quantities)
        if transfers:
            self._append_ledger_transfers(
                session,
                intent,
                reservation,
                exposure,
                fact_id,
                request,
                transfers,
                now,
            )
            self._apply_exposure_state(exposure, new_quantities, request, len(transfers), now)

        state.status = request.target_status
        state.version += 1
        state.cumulative_filled_quantity = request.cumulative_filled_quantity
        state.known_remaining_quantity = request.known_remaining_quantity
        state.zero_fill_confirmed = request.zero_fill_confirmed
        state.venue_order_terminal = request.venue_order_terminal
        state.position_reconciled = request.position_reconciled
        state.protection_confirmed = request.protection_confirmed
        state.last_fact_sequence = request.fact_sequence
        state.last_fact_hash = request.evidence_hash
        state.reason_code = f"EXECUTION_FACT_{request.target_status}"
        state.updated_at = now
        self._apply_authorization_lifecycle(session, intent, reservation, request, now)
        session.flush()
        EXECUTION_FACT_BINDINGS.labels(
            request.fact_kind.value,
            request.reconciliation_source_type.value,
            "APPLIED",
        ).inc()
        EXECUTION_FACT_AUTHORITY_MODES.labels(authority_mode, "APPLIED").inc()
        EXECUTION_FACT_RESULTS.labels(request.target_status, "APPLIED").inc()
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="OrderIntent",
            object_id=str(order_intent_id),
            object_version=state.version,
            data={
                "execution_fact_id": str(fact_id),
                "order_intent_id": str(order_intent_id),
                "intent_status": state.status,
                "risk_exposure_status": exposure.status,
                "cumulative_filled_quantity": str(state.cumulative_filled_quantity),
                "known_remaining_quantity": str(state.known_remaining_quantity),
                "authority_mode": authority_mode,
                "dispatch_eligible": False,
            },
            events=(
                DomainEvent(
                    event_type="ExecutionFactReconciled",
                    aggregate_type="OrderIntent",
                    aggregate_id=str(order_intent_id),
                    payload={
                        "execution_fact_id": str(fact_id),
                        "fact_sequence": request.fact_sequence,
                        "fact_kind": request.fact_kind.value,
                        "target_status": request.target_status,
                        "shadow_dispatch_claim_id": str(request.shadow_dispatch_claim_id),
                        "reconciliation_run_id": str(request.reconciliation_run_id),
                        "reconciliation_input_id": str(request.reconciliation_input_id),
                        "reconciliation_source_type": request.reconciliation_source_type.value,
                        "authority_mode": authority_mode,
                        "risk_exposure_status": exposure.status,
                        "cumulative_filled_quantity": str(request.cumulative_filled_quantity),
                    },
                ),
            ),
        )

    @staticmethod
    def _require_internal(envelope: CommandEnvelope) -> None:
        if envelope.command_type != ExecutionReconciliationService.command_type:
            raise CommandRejected("COMMAND_TYPE_MISMATCH", "unexpected command type")
        if (
            envelope.channel is not CommandChannel.INTERNAL
            or envelope.service_principal != EXECUTION_RECONCILIATION_SERVICE_PRINCIPAL
        ):
            raise CommandRejected(
                "RECONCILIATION_SERVICE_REQUIRED",
                "only the Trading reconciliation service may record execution facts",
            )
        if envelope.object_type != "OrderIntent" or envelope.object_id is None:
            raise CommandRejected("OBJECT_BINDING_MISMATCH", "OrderIntent binding is required")

    @staticmethod
    def _lock_reconciliation_scope(
        session: Session, intent: OrderIntent, reservation: RiskReservation
    ) -> None:
        keys = sorted(
            {
                f"campaign:{intent.campaign_id}",
                f"order-intent:{intent.order_intent_id}",
                f"account:{intent.venue}:{intent.execution_domain}:{intent.account_id}",
                f"collateral:{reservation.collateral_pool_id}",
            }
        )
        for key in keys:
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": key},
            )

    @staticmethod
    def _validate_route(intent: OrderIntent, request: RecordExecutionFactRequest) -> None:
        if (
            request.venue != intent.venue
            or request.execution_domain != intent.execution_domain
            or request.account_id != intent.account_id
        ):
            raise CommandRejected("EXECUTION_ROUTE_MISMATCH", "execution route changed")

    @staticmethod
    def _validate_reconciliation_binding(
        session: Session,
        intent: OrderIntent,
        reservation: RiskReservation,
        request: RecordExecutionFactRequest,
        now: datetime,
    ) -> str:
        allowed_bindings = EXECUTION_FACT_SOURCE_STATUS_MATRIX.get(request.target_status)
        requested_binding = (request.fact_kind, request.reconciliation_source_type)
        if allowed_bindings is None or requested_binding not in allowed_bindings:
            raise CommandRejected(
                "EXECUTION_FACT_SOURCE_STATUS_MISMATCH",
                "fact kind and reconciliation source cannot prove the target status",
            )

        claim = session.execute(
            select(ShadowDispatchClaim).where(
                ShadowDispatchClaim.claim_id == request.shadow_dispatch_claim_id
            )
        ).scalar_one_or_none()
        if claim is None:
            raise CommandRejected(
                "EXECUTION_FACT_CLAIM_NOT_FOUND", "shadow dispatch claim is unavailable"
            )
        if (
            claim.order_intent_id != intent.order_intent_id
            or claim.organization_id != reservation.organization_id
            or claim.claim_hash != request.dispatch_claim_hash
            or claim.execution_mode != "SHADOW"
            or claim.external_send_permitted
            or claim.live_gate_status != "DISABLED"
            or claim.reconciliation_run_id is None
        ):
            raise CommandRejected(
                "EXECUTION_FACT_CLAIM_MISMATCH",
                "shadow dispatch claim does not match the immutable intent authority",
            )

        run = session.execute(
            select(ExecutionReconciliationRun).where(
                ExecutionReconciliationRun.run_id == request.reconciliation_run_id
            )
        ).scalar_one_or_none()
        if run is None:
            raise CommandRejected(
                "EXECUTION_FACT_RECONCILIATION_RUN_NOT_FOUND",
                "execution reconciliation run is unavailable",
            )
        run_state = session.execute(
            select(ExecutionReconciliationRunState)
            .where(ExecutionReconciliationRunState.run_id == request.reconciliation_run_id)
            .with_for_update()
        ).scalar_one()
        if (
            run.organization_id != claim.organization_id
            or run.scope_id != claim.scope_id
            or run.fencing_token < claim.fencing_token
            or (run.fencing_token == claim.fencing_token and run.lease_id != claim.lease_id)
            or run.environment != "SHADOW"
            or run.live_dispatch_eligible
            or run.run_hash != request.reconciliation_run_hash
            or run.started_at <= claim.claimed_at
        ):
            raise CommandRejected(
                "EXECUTION_FACT_RECONCILIATION_RUN_MISMATCH",
                "reconciliation run is not a valid post-claim original or successor authority",
            )
        if now >= run.deadline_at:
            raise CommandRejected(
                "EXECUTION_FACT_RECONCILIATION_RUN_EXPIRED",
                "execution reconciliation run deadline has elapsed",
            )
        if run_state.status != "RUNNING" or run_state.phase not in {"COMPARING", "ADJUSTING"}:
            raise CommandRejected(
                "EXECUTION_FACT_RECONCILIATION_RUN_NOT_ACTIVE",
                "execution facts require an active comparison or adjustment phase",
            )

        latest_run_id = session.execute(
            select(ExecutionReconciliationRun.run_id)
            .where(ExecutionReconciliationRun.scope_id == run.scope_id)
            .order_by(
                ExecutionReconciliationRun.started_at.desc(),
                ExecutionReconciliationRun.run_id.desc(),
            )
            .limit(1)
        ).scalar_one()
        if latest_run_id != run.run_id:
            raise CommandRejected(
                "EXECUTION_FACT_RECONCILIATION_RUN_NOT_LATEST",
                "execution facts require the latest exact-scope reconciliation run",
            )

        lineage_matches = bool(
            session.execute(
                text(
                    """
                    WITH RECURSIVE lineage(run_id, supersedes_run_id) AS (
                        SELECT run_id, supersedes_run_id
                        FROM execution_reconciliation_runs WHERE run_id = :run_id
                        UNION ALL
                        SELECT parent.run_id, parent.supersedes_run_id
                        FROM execution_reconciliation_runs parent
                        JOIN lineage child ON parent.run_id = child.supersedes_run_id
                    )
                    SELECT EXISTS(
                        SELECT 1 FROM lineage WHERE run_id = :claim_reconciliation_run_id
                    )
                    """
                ),
                {
                    "run_id": str(run.run_id),
                    "claim_reconciliation_run_id": str(claim.reconciliation_run_id),
                },
            ).scalar_one()
        )
        if not lineage_matches:
            raise CommandRejected(
                "EXECUTION_FACT_RECONCILIATION_LINEAGE_MISMATCH",
                "reconciliation run does not descend from the claim-authorizing result",
            )

        reconciliation_input = session.execute(
            select(ExecutionReconciliationInput).where(
                ExecutionReconciliationInput.input_id == request.reconciliation_input_id
            )
        ).scalar_one_or_none()
        if reconciliation_input is None:
            raise CommandRejected(
                "EXECUTION_FACT_RECONCILIATION_INPUT_NOT_FOUND",
                "reconciliation input is unavailable",
            )
        if (
            reconciliation_input.run_id != run.run_id
            or reconciliation_input.organization_id != claim.organization_id
            or reconciliation_input.source_type != request.reconciliation_source_type.value
            or reconciliation_input.collection_status != "COMPLETE"
            or reconciliation_input.source_version != request.source_version
            or reconciliation_input.input_hash != request.reconciliation_input_hash
        ):
            raise CommandRejected(
                "EXECUTION_FACT_RECONCILIATION_INPUT_MISMATCH",
                "fact is not bound to the exact complete source input",
            )
        if not (
            reconciliation_input.observed_from
            <= request.event_time
            <= reconciliation_input.observed_through
        ):
            raise CommandRejected(
                "EXECUTION_FACT_OUTSIDE_INPUT_WINDOW",
                "fact event is outside the bound source input watermark",
            )
        if request.event_time < claim.claimed_at:
            raise CommandRejected(
                "EXECUTION_FACT_PRE_CLAIM_EVENT",
                "pre-claim execution events cannot be replayed as new business facts",
            )

        sender_state = session.execute(
            select(ExecutionSenderScopeState)
            .where(ExecutionSenderScopeState.scope_id == claim.scope_id)
            .with_for_update()
        ).scalar_one()
        if (
            sender_state.status != "LEASED"
            or sender_state.active_lease_id != run.lease_id
            or sender_state.current_fencing_token != run.fencing_token
            or sender_state.lease_expires_at is None
            or now >= sender_state.lease_expires_at
        ):
            raise CommandRejected(
                "EXECUTION_FACT_SENDER_LEASE_STALE",
                "execution fact authority is fenced or expired",
            )
        if run.lease_id == claim.lease_id and run.fencing_token == claim.fencing_token:
            return "ORIGINAL_LEASE"
        return "SUCCESSOR_LEASE"

    @staticmethod
    def _validate_canonical_venue_fact(
        session: Session,
        intent: OrderIntent,
        state: OrderIntentState,
        request: RecordExecutionFactRequest,
    ) -> tuple[str | None, UUID | None, UUID | None]:
        if request.fact_kind not in {
            ExecutionFactKind.VENUE_ORDER,
            ExecutionFactKind.VENUE_FILL,
            ExecutionFactKind.VENUE_POSITION,
            ExecutionFactKind.VENUE_PROTECTION,
        }:
            return None, None, None
        claim = session.get(ShadowDispatchClaim, request.shadow_dispatch_claim_id)
        scope = session.get(ExecutionSenderScope, claim.scope_id) if claim is not None else None
        link = session.get(VenueFactInputLink, request.venue_fact_input_link_id)
        if claim is None or scope is None or link is None:
            raise CommandRejected(
                "EXECUTION_FACT_CANONICAL_REFERENCE_NOT_FOUND",
                "canonical venue fact authority is unavailable",
            )
        if (
            link.run_id != request.reconciliation_run_id
            or link.reconciliation_input_id != request.reconciliation_input_id
            or link.organization_id != claim.organization_id
            or link.source_type != request.reconciliation_source_type.value
            or link.input_hash != request.reconciliation_input_hash
            or link.fact_hash != request.venue_fact_hash
        ):
            raise CommandRejected(
                "EXECUTION_FACT_CANONICAL_LINK_MISMATCH",
                "canonical venue fact is not a member of the exact reconciliation input",
            )

        expected_position_side = (
            "BOTH" if scope.position_mode == "ONE_WAY" else intent.position_side
        )
        canonical_order_id: str
        fact_event_time: datetime
        if request.fact_kind is ExecutionFactKind.VENUE_ORDER:
            observation = session.get(VenueOrderObservation, request.venue_order_observation_id)
            if observation is None:
                raise CommandRejected(
                    "EXECUTION_FACT_CANONICAL_REFERENCE_NOT_FOUND",
                    "venue order observation is unavailable",
                )
            if (
                link.venue_order_observation_id != observation.venue_order_observation_id
                or link.venue_fill_id is not None
                or link.venue_position_snapshot_id is not None
                or link.venue_protection_snapshot_id is not None
                or observation.observation_hash != request.venue_fact_hash
            ):
                raise CommandRejected(
                    "EXECUTION_FACT_CANONICAL_LINK_MISMATCH",
                    "venue order observation and input membership disagree",
                )
            if (
                observation.organization_id != claim.organization_id
                or observation.venue != intent.venue
                or observation.execution_domain != intent.execution_domain
                or observation.account_id != intent.account_id
                or observation.instrument_id != intent.instrument_id
                or observation.observed_client_order_id != claim.client_order_id
                or observation.side != intent.side
                or observation.position_side != expected_position_side
                or observation.reduce_only != intent.reduce_only
                or observation.order_type != intent.order_type
                or observation.time_in_force != intent.time_in_force
            ):
                raise CommandRejected(
                    "EXECUTION_FACT_CANONICAL_OWNERSHIP_MISMATCH",
                    "venue order observation does not belong to the claimed intent",
                )
            canonical_order_id = observation.venue_order_id
            fact_event_time = observation.event_time
            if (
                observation.original_quantity != state.intent_quantity
                or observation.cumulative_filled_quantity != state.cumulative_filled_quantity
            ):
                raise CommandRejected(
                    "EXECUTION_FACT_CANONICAL_QUANTITY_MISMATCH",
                    "venue order observation cannot create unconfirmed fill quantity",
                )
            expected_target = ExecutionReconciliationService._order_observation_target(observation)
            expected_cumulative = observation.cumulative_filled_quantity
            expected_remaining = observation.known_remaining_quantity
            expected_zero = observation.zero_fill_confirmed
            expected_terminal = observation.terminal
            fact_id = observation.venue_order_observation_id
            fact_type = "VENUE_ORDER_OBSERVATION"
        elif request.fact_kind is ExecutionFactKind.VENUE_FILL:
            fill = session.get(VenueFill, request.venue_fill_id)
            if fill is None:
                raise CommandRejected(
                    "EXECUTION_FACT_CANONICAL_REFERENCE_NOT_FOUND",
                    "venue fill is unavailable",
                )
            if (
                link.venue_fill_id != fill.venue_fill_id
                or link.venue_order_observation_id is not None
                or link.venue_position_snapshot_id is not None
                or link.venue_protection_snapshot_id is not None
                or fill.fill_hash != request.venue_fact_hash
            ):
                raise CommandRejected(
                    "EXECUTION_FACT_CANONICAL_LINK_MISMATCH",
                    "venue fill and input membership disagree",
                )
            if (
                fill.organization_id != claim.organization_id
                or fill.venue != intent.venue
                or fill.execution_domain != intent.execution_domain
                or fill.account_id != intent.account_id
                or fill.instrument_id != intent.instrument_id
                or fill.observed_client_order_id != claim.client_order_id
                or fill.side != intent.side
                or fill.position_side != expected_position_side
                or fill.reduce_only != intent.reduce_only
            ):
                raise CommandRejected(
                    "EXECUTION_FACT_CANONICAL_OWNERSHIP_MISMATCH",
                    "venue fill does not belong to the claimed intent",
                )
            canonical_order_id = fill.venue_order_id
            fact_event_time = fill.event_time
            expected_cumulative = state.cumulative_filled_quantity + fill.quantity
            if expected_cumulative > state.intent_quantity:
                raise CommandRejected(
                    "EXECUTION_FACT_CANONICAL_QUANTITY_MISMATCH",
                    "venue fill exceeds the frozen intent quantity",
                )
            expected_remaining = state.intent_quantity - expected_cumulative
            expected_target = "FILLED" if expected_remaining == ZERO else "PARTIALLY_FILLED"
            expected_zero = False
            expected_terminal = expected_remaining == ZERO
            fact_id = fill.venue_fill_id
            fact_type = "VENUE_FILL"
        elif request.fact_kind is ExecutionFactKind.VENUE_POSITION:
            snapshot = session.get(VenuePositionSnapshot, request.venue_position_snapshot_id)
            if snapshot is None:
                raise CommandRejected(
                    "EXECUTION_FACT_CANONICAL_REFERENCE_NOT_FOUND",
                    "venue position snapshot is unavailable",
                )
            if (
                link.venue_position_snapshot_id != snapshot.venue_position_snapshot_id
                or link.venue_order_observation_id is not None
                or link.venue_fill_id is not None
                or link.venue_protection_snapshot_id is not None
                or snapshot.snapshot_hash != request.venue_fact_hash
            ):
                raise CommandRejected(
                    "EXECUTION_FACT_CANONICAL_LINK_MISMATCH",
                    "venue position snapshot and input membership disagree",
                )
            if (
                snapshot.organization_id != claim.organization_id
                or snapshot.venue != intent.venue
                or snapshot.execution_domain != intent.execution_domain
                or snapshot.account_id != intent.account_id
                or snapshot.instrument_id != intent.instrument_id
                or snapshot.position_mode != scope.position_mode
                or snapshot.position_side != expected_position_side
                or snapshot.margin_mode != scope.margin_mode
                or snapshot.collateral_pool_id != scope.collateral_pool_id
                or (
                    snapshot.position_state == "OPEN" and snapshot.direction != intent.position_side
                )
            ):
                raise CommandRejected(
                    "EXECUTION_FACT_CANONICAL_OWNERSHIP_MISMATCH",
                    "venue position snapshot does not belong to the claimed intent scope",
                )
            expected_position_quantity = (
                intent.current_position_quantity + state.cumulative_filled_quantity
            )
            if (
                state.status not in {"FILLED", "CANCELLED_PARTIAL"}
                or snapshot.position_state != "OPEN"
                or snapshot.quantity != expected_position_quantity
            ):
                raise CommandRejected(
                    "EXECUTION_FACT_CANONICAL_POSITION_MISMATCH",
                    "venue position does not prove the exact post-intent open quantity",
                )
            latest_order_or_fill_time = session.execute(
                select(ExecutionFact.event_time)
                .where(
                    ExecutionFact.order_intent_id == intent.order_intent_id,
                    ExecutionFact.fact_kind.in_(("VENUE_ORDER", "VENUE_FILL")),
                )
                .order_by(ExecutionFact.event_time.desc(), ExecutionFact.fact_sequence.desc())
                .limit(1)
            ).scalar_one_or_none()
            if latest_order_or_fill_time is None or snapshot.event_time < latest_order_or_fill_time:
                raise CommandRejected(
                    "EXECUTION_FACT_CANONICAL_POSITION_STALE",
                    "venue position snapshot predates the intent execution evidence",
                )
            expected_payload = {
                "venue_fact_type": "VENUE_POSITION_SNAPSHOT",
                "venue_fact_id": str(snapshot.venue_position_snapshot_id),
                "venue_fact_hash": request.venue_fact_hash,
                "venue_fact_input_link_id": str(link.venue_fact_input_link_id),
            }
            if (
                request.external_fact_id != str(snapshot.venue_position_snapshot_id)
                or request.target_status != "POSITION_RECONCILED"
                or request.cumulative_filled_quantity != state.cumulative_filled_quantity
                or request.known_remaining_quantity != state.known_remaining_quantity
                or request.zero_fill_confirmed
                or not request.venue_order_terminal
                or not state.venue_order_terminal
                or not request.position_reconciled
                or request.protection_confirmed
                or request.event_time != snapshot.event_time
                or request.received_at != link.received_at
                or request.source_ref != link.raw_payload_ref
                or request.evidence_ref != link.evidence_ref
                or request.payload != expected_payload
            ):
                raise CommandRejected(
                    "EXECUTION_FACT_CANONICAL_SEMANTICS_MISMATCH",
                    "position reconciliation is not an exact canonical snapshot projection",
                )
            return None, snapshot.venue_position_snapshot_id, None
        else:
            protection = session.get(VenueProtectionSnapshot, request.venue_protection_snapshot_id)
            if protection is None:
                raise CommandRejected(
                    "EXECUTION_FACT_CANONICAL_REFERENCE_NOT_FOUND",
                    "venue protection snapshot is unavailable",
                )
            if (
                link.venue_protection_snapshot_id != protection.venue_protection_snapshot_id
                or link.venue_order_observation_id is not None
                or link.venue_fill_id is not None
                or link.venue_position_snapshot_id is not None
                or protection.snapshot_hash != request.venue_fact_hash
            ):
                raise CommandRejected(
                    "EXECUTION_FACT_CANONICAL_LINK_MISMATCH",
                    "venue protection snapshot and input membership disagree",
                )
            if (
                protection.organization_id != claim.organization_id
                or protection.venue != intent.venue
                or protection.execution_domain != intent.execution_domain
                or protection.account_id != intent.account_id
                or protection.instrument_id != intent.instrument_id
                or protection.position_mode != scope.position_mode
                or protection.position_side != expected_position_side
                or protection.margin_mode != scope.margin_mode
                or protection.collateral_pool_id != scope.collateral_pool_id
            ):
                raise CommandRejected(
                    "EXECUTION_FACT_CANONICAL_OWNERSHIP_MISMATCH",
                    "venue protection snapshot does not belong to the claimed intent scope",
                )
            position_fact = session.execute(
                select(ExecutionFact)
                .where(
                    ExecutionFact.order_intent_id == intent.order_intent_id,
                    ExecutionFact.fact_kind == "VENUE_POSITION",
                )
                .order_by(ExecutionFact.fact_sequence.desc())
                .limit(1)
            ).scalar_one_or_none()
            expected_position_quantity = (
                intent.current_position_quantity + state.cumulative_filled_quantity
            )
            if (
                state.status != "POSITION_RECONCILED"
                or position_fact is None
                or position_fact.venue_position_snapshot_id != protection.venue_position_snapshot_id
                or protection.protection_state != "CONFIRMED"
                or protection.protected_direction != intent.position_side
                or protection.position_quantity != expected_position_quantity
                or protection.covered_quantity != expected_position_quantity
                or protection.uncovered_quantity != ZERO
                or protection.active_stop_order_count is None
                or protection.active_stop_order_count < 1
                or not protection.venue_native
                or not protection.reduce_only_confirmed
                or protection.replacement_in_progress
            ):
                raise CommandRejected(
                    "EXECUTION_FACT_CANONICAL_PROTECTION_MISMATCH",
                    "venue protection does not prove exact native coverage "
                    "of the reconciled position",
                )
            if protection.event_time < position_fact.event_time:
                raise CommandRejected(
                    "EXECUTION_FACT_CANONICAL_PROTECTION_STALE",
                    "venue protection snapshot predates position reconciliation evidence",
                )
            expected_payload = {
                "venue_fact_type": "VENUE_PROTECTION_SNAPSHOT",
                "venue_fact_id": str(protection.venue_protection_snapshot_id),
                "venue_fact_hash": request.venue_fact_hash,
                "venue_fact_input_link_id": str(link.venue_fact_input_link_id),
                "venue_position_snapshot_id": str(protection.venue_position_snapshot_id),
            }
            if (
                request.external_fact_id != str(protection.venue_protection_snapshot_id)
                or request.target_status != "PROTECTION_CONFIRMED"
                or request.cumulative_filled_quantity != state.cumulative_filled_quantity
                or request.known_remaining_quantity != state.known_remaining_quantity
                or request.zero_fill_confirmed != state.zero_fill_confirmed
                or request.venue_order_terminal != state.venue_order_terminal
                or not request.position_reconciled
                or not request.protection_confirmed
                or request.event_time != protection.event_time
                or request.received_at != link.received_at
                or request.source_ref != link.raw_payload_ref
                or request.evidence_ref != link.evidence_ref
                or request.payload != expected_payload
            ):
                raise CommandRejected(
                    "EXECUTION_FACT_CANONICAL_SEMANTICS_MISMATCH",
                    "protection confirmation is not an exact canonical snapshot projection",
                )
            return None, None, protection.venue_protection_snapshot_id

        existing_order_id = session.execute(
            select(ExecutionFact.canonical_venue_order_id)
            .where(
                ExecutionFact.order_intent_id == intent.order_intent_id,
                ExecutionFact.fact_contract_version.in_((3, 4, 5)),
                ExecutionFact.canonical_venue_order_id.is_not(None),
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing_order_id is not None and existing_order_id != canonical_order_id:
            raise CommandRejected(
                "EXECUTION_FACT_CANONICAL_ORDER_ID_MISMATCH",
                "claimed intent cannot change its canonical venue order identity",
            )
        expected_payload = {
            "venue_fact_type": fact_type,
            "venue_fact_id": str(fact_id),
            "venue_fact_hash": request.venue_fact_hash,
            "venue_fact_input_link_id": str(link.venue_fact_input_link_id),
            "canonical_venue_order_id": canonical_order_id,
        }
        if (
            request.external_fact_id != str(fact_id)
            or request.target_status != expected_target
            or request.cumulative_filled_quantity != expected_cumulative
            or request.known_remaining_quantity != expected_remaining
            or request.zero_fill_confirmed != expected_zero
            or request.venue_order_terminal != expected_terminal
            or request.position_reconciled
            or request.protection_confirmed
            or request.event_time != fact_event_time
            or request.received_at != link.received_at
            or request.source_ref != link.raw_payload_ref
            or request.evidence_ref != link.evidence_ref
            or request.payload != expected_payload
        ):
            raise CommandRejected(
                "EXECUTION_FACT_CANONICAL_SEMANTICS_MISMATCH",
                "execution transition is not an exact projection of the canonical venue fact",
            )
        return canonical_order_id, None, None

    @staticmethod
    def _order_observation_target(observation: VenueOrderObservation) -> str:
        if observation.status == "OPEN":
            return "VENUE_ACKNOWLEDGED"
        if observation.status == "CANCEL_PENDING":
            return "CANCEL_PENDING"
        if observation.status == "REJECTED":
            return "REJECTED_ZERO_FILL"
        if observation.status in {"CANCELLED", "EXPIRED"}:
            return (
                "CANCELLED_ZERO_FILL"
                if observation.cumulative_filled_quantity == ZERO
                else "CANCELLED_PARTIAL"
            )
        if observation.status == "UNKNOWN":
            return "RESULT_UNKNOWN"
        raise CommandRejected(
            "EXECUTION_FACT_CANONICAL_STATUS_UNSUPPORTED",
            "venue order status requires fill authority or has no execution transition",
        )

    @staticmethod
    def _validate_progression(
        state: OrderIntentState,
        request: RecordExecutionFactRequest,
        now: datetime,
    ) -> None:
        if request.received_at > now:
            raise CommandRejected("EXECUTION_FACT_FROM_FUTURE", "fact is from the future")
        if request.fact_sequence != state.last_fact_sequence + 1:
            raise CommandRejected("EXECUTION_FACT_OUT_OF_ORDER", "fact sequence is not next")
        if state.status in ZERO_TERMINAL_STATUSES or state.status == "COMPLETED":
            raise CommandRejected("ORDER_INTENT_ALREADY_TERMINAL", "intent is already terminal")
        if request.cumulative_filled_quantity < state.cumulative_filled_quantity:
            raise CommandRejected("FILLED_QUANTITY_REGRESSION", "filled quantity regressed")
        if request.cumulative_filled_quantity > state.intent_quantity:
            raise CommandRejected("FILLED_QUANTITY_EXCEEDED", "filled quantity exceeds intent")
        if (
            request.cumulative_filled_quantity + request.known_remaining_quantity
            > state.intent_quantity
        ):
            raise CommandRejected("EXECUTION_QUANTITY_OVERFLOW", "execution quantity overflows")
        if request.zero_fill_confirmed and request.target_status not in ZERO_TERMINAL_STATUSES:
            raise CommandRejected(
                "EXECUTION_FACT_SEMANTICS_INVALID",
                "zero-fill proof cannot accompany a non-zero-terminal status",
            )
        if request.target_status in ZERO_TERMINAL_STATUSES:
            valid = (
                request.zero_fill_confirmed
                and request.venue_order_terminal
                and request.cumulative_filled_quantity == ZERO
                and request.known_remaining_quantity == ZERO
            )
        elif request.target_status == "PARTIALLY_FILLED":
            valid = (
                request.cumulative_filled_quantity > ZERO
                and request.known_remaining_quantity > ZERO
                and not request.venue_order_terminal
                and request.cumulative_filled_quantity + request.known_remaining_quantity
                == state.intent_quantity
            )
        elif request.target_status == "FILLED":
            valid = (
                request.cumulative_filled_quantity == state.intent_quantity
                and request.known_remaining_quantity == ZERO
                and request.venue_order_terminal
            )
        elif request.target_status == "CANCELLED_PARTIAL":
            valid = (
                request.cumulative_filled_quantity > ZERO
                and request.known_remaining_quantity == ZERO
                and request.venue_order_terminal
            )
        elif request.target_status == "RESULT_UNKNOWN":
            valid = request.known_remaining_quantity == ZERO and not request.zero_fill_confirmed
        elif request.target_status in {
            "POSITION_RECONCILED",
            "PROTECTION_CONFIRMED",
            "COMPLETED",
        }:
            valid = request.position_reconciled
            if request.target_status in {"PROTECTION_CONFIRMED", "COMPLETED"}:
                valid = (
                    valid
                    and request.protection_confirmed
                    and request.cumulative_filled_quantity > ZERO
                    and request.known_remaining_quantity == ZERO
                    and request.venue_order_terminal
                )
        else:
            valid = (
                not request.venue_order_terminal
                and not request.zero_fill_confirmed
                and request.cumulative_filled_quantity + request.known_remaining_quantity
                == state.intent_quantity
            )
        if not valid:
            raise CommandRejected(
                "EXECUTION_FACT_SEMANTICS_INVALID", "fact does not prove target status"
            )

    @staticmethod
    def _quantity_buckets(exposure: RiskExposureState) -> dict[str, Decimal]:
        return {
            "RESERVED": exposure.reserved_quantity,
            "OPEN": exposure.open_quantity,
            "UNKNOWN": exposure.unknown_quantity,
            "RELEASED": exposure.released_quantity,
        }

    @staticmethod
    def _desired_quantity_buckets(
        total: Decimal, request: RecordExecutionFactRequest
    ) -> dict[str, Decimal]:
        filled = request.cumulative_filled_quantity
        if request.target_status in ZERO_TERMINAL_STATUSES:
            return {"RESERVED": ZERO, "OPEN": ZERO, "UNKNOWN": ZERO, "RELEASED": total}
        if request.target_status == "RESULT_UNKNOWN":
            return {
                "RESERVED": ZERO,
                "OPEN": filled,
                "UNKNOWN": total - filled,
                "RELEASED": ZERO,
            }
        released = total - filled if request.venue_order_terminal else ZERO
        reserved = ZERO if request.venue_order_terminal else request.known_remaining_quantity
        return {
            "RESERVED": reserved,
            "OPEN": filled,
            "UNKNOWN": ZERO,
            "RELEASED": released,
        }

    @staticmethod
    def _bucket_transfers(
        old: dict[str, Decimal], new: dict[str, Decimal]
    ) -> list[tuple[str, str, Decimal]]:
        sources: list[tuple[str, Decimal]] = [
            (bucket, old[bucket] - new[bucket])
            for bucket in ("RESERVED", "UNKNOWN", "OPEN", "RELEASED")
            if old[bucket] > new[bucket]
        ]
        targets: list[tuple[str, Decimal]] = [
            (bucket, new[bucket] - old[bucket])
            for bucket in ("OPEN", "RESERVED", "UNKNOWN", "RELEASED")
            if new[bucket] > old[bucket]
        ]
        transfers: list[tuple[str, str, Decimal]] = []
        source_index = 0
        target_index = 0
        while source_index < len(sources) and target_index < len(targets):
            source, source_quantity = sources[source_index]
            target, target_quantity = targets[target_index]
            quantity = min(source_quantity, target_quantity)
            if quantity > ZERO:
                transfers.append((str(source), str(target), quantity))
            source_remaining = source_quantity - quantity
            target_remaining = target_quantity - quantity
            sources[source_index] = (source, source_remaining)
            targets[target_index] = (target, target_remaining)
            if source_remaining == ZERO:
                source_index += 1
            if target_remaining == ZERO:
                target_index += 1
        if source_index != len(sources) or target_index != len(targets):
            raise CommandRejected(
                "RISK_BUCKET_CONSERVATION_FAILED", "risk bucket transfer does not conserve quantity"
            )
        return transfers

    @staticmethod
    def _proportional(total: Decimal, quantity: Decimal, total_quantity: Decimal) -> Decimal:
        if total == ZERO or quantity == ZERO:
            return ZERO
        if quantity == total_quantity:
            return total
        return (total * quantity / total_quantity).quantize(QUANTUM, rounding=ROUND_DOWN)

    def _append_ledger_transfers(
        self,
        session: Session,
        intent: OrderIntent,
        reservation: RiskReservation,
        exposure: RiskExposureState,
        fact_id: UUID,
        request: RecordExecutionFactRequest,
        transfers: list[tuple[str, str, Decimal]],
        now: datetime,
    ) -> None:
        for offset, (source, target, quantity) in enumerate(transfers, start=1):
            transition = f"{source}_TO_{target}"
            session.add(
                RiskLedgerEntry(
                    risk_ledger_entry_id=uuid4(),
                    risk_reservation_id=reservation.risk_reservation_id,
                    order_intent_id=intent.order_intent_id,
                    execution_fact_id=fact_id,
                    entry_sequence=exposure.ledger_sequence + offset,
                    entry_type="RELEASE" if target == "RELEASED" else "MIGRATE",
                    from_bucket=source,
                    to_bucket=target,
                    quantity=quantity,
                    heat=self._proportional(exposure.total_heat, quantity, exposure.total_quantity),
                    funding=self._proportional(
                        exposure.total_funding, quantity, exposure.total_quantity
                    ),
                    margin=self._proportional(
                        exposure.total_margin, quantity, exposure.total_quantity
                    ),
                    evidence_ref=request.evidence_ref,
                    evidence_hash=request.evidence_hash,
                    occurred_at=now,
                )
            )
            RISK_RESERVATION_TRANSITIONS.labels(transition).inc()
        session.flush()

    def _apply_exposure_state(
        self,
        exposure: RiskExposureState,
        quantities: dict[str, Decimal],
        request: RecordExecutionFactRequest,
        ledger_entries: int,
        now: datetime,
    ) -> None:
        dimensions = {
            "heat": exposure.total_heat,
            "funding": exposure.total_funding,
            "margin": exposure.total_margin,
        }
        allocated: dict[str, dict[str, Decimal]] = {}
        for name, total in dimensions.items():
            open_amount = self._proportional(total, quantities["OPEN"], exposure.total_quantity)
            unknown_amount = self._proportional(
                total, quantities["UNKNOWN"], exposure.total_quantity
            )
            released_amount = self._proportional(
                total, quantities["RELEASED"], exposure.total_quantity
            )
            reserved_amount = total - open_amount - unknown_amount - released_amount
            allocated[name] = {
                "RESERVED": reserved_amount,
                "OPEN": open_amount,
                "UNKNOWN": unknown_amount,
                "RELEASED": released_amount,
            }
        exposure.reserved_quantity = quantities["RESERVED"]
        exposure.open_quantity = quantities["OPEN"]
        exposure.unknown_quantity = quantities["UNKNOWN"]
        exposure.released_quantity = quantities["RELEASED"]
        exposure.reserved_heat = allocated["heat"]["RESERVED"]
        exposure.open_heat = allocated["heat"]["OPEN"]
        exposure.unknown_heat = allocated["heat"]["UNKNOWN"]
        exposure.released_heat = allocated["heat"]["RELEASED"]
        exposure.funding_reserved = allocated["funding"]["RESERVED"]
        exposure.funding_used = allocated["funding"]["OPEN"]
        exposure.funding_unknown = allocated["funding"]["UNKNOWN"]
        exposure.funding_released = allocated["funding"]["RELEASED"]
        exposure.margin_reserved = allocated["margin"]["RESERVED"]
        exposure.margin_used = allocated["margin"]["OPEN"]
        exposure.margin_unknown = allocated["margin"]["UNKNOWN"]
        exposure.margin_released = allocated["margin"]["RELEASED"]
        exposure.status = self._exposure_status(quantities)
        exposure.version += 1
        exposure.ledger_sequence += ledger_entries
        exposure.last_evidence_ref = request.evidence_ref
        exposure.last_evidence_hash = request.evidence_hash
        exposure.reason_code = f"EXECUTION_FACT_{request.target_status}"
        exposure.updated_at = now

    @staticmethod
    def _exposure_status(quantities: dict[str, Decimal]) -> str:
        if quantities["UNKNOWN"] > ZERO:
            return "UNKNOWN"
        if quantities["RESERVED"] > ZERO and quantities["OPEN"] > ZERO:
            return "PARTIAL"
        if quantities["RESERVED"] > ZERO:
            return "RESERVED"
        if quantities["OPEN"] > ZERO:
            return "OPEN"
        return "RELEASED"

    @staticmethod
    def _apply_authorization_lifecycle(
        session: Session,
        intent: OrderIntent,
        reservation: RiskReservation,
        request: RecordExecutionFactRequest,
        now: datetime,
    ) -> None:
        system_state = session.execute(
            select(SystemRiskStateRecord)
            .where(SystemRiskStateRecord.organization_id == reservation.organization_id)
            .with_for_update()
        ).scalar_one_or_none()
        system_status = system_state.status if system_state is not None else "UNKNOWN"
        campaign_state = session.execute(
            select(CampaignState)
            .where(CampaignState.campaign_id == intent.campaign_id)
            .with_for_update()
        ).scalar_one()
        if intent.intent_kind == "INITIAL":
            initial_state = session.execute(
                select(InitialAuthorizationState)
                .where(
                    InitialAuthorizationState.initial_authorization_id
                    == intent.initial_authorization_id
                )
                .with_for_update()
            ).scalar_one()
            if request.cumulative_filled_quantity > ZERO and initial_state.status == "ACTIVE":
                initial_state.status = "CONSUMED"
                initial_state.version += 1
                initial_state.reason_code = "POSITIVE_INITIAL_FILL_CONFIRMED"
                initial_state.updated_at = now
                if campaign_state.status == "PENDING_ENTRY":
                    campaign_state.status = "OPEN"
                    campaign_state.version += 1
                    campaign_state.reason_code = "POSITIVE_INITIAL_FILL_CONFIRMED"
                    campaign_state.updated_at = now
            if request.target_status in {"PROTECTION_CONFIRMED", "COMPLETED"}:
                ExecutionReconciliationService._activate_or_invalidate_add_package(
                    session, intent, system_status, now
                )
            return

        add_state = session.execute(
            select(AddUnitState)
            .where(AddUnitState.add_unit_id == intent.add_unit_id)
            .with_for_update()
        ).scalar_one()
        package = session.execute(
            select(AddAuthorizationPackage)
            .where(AddAuthorizationPackage.add_package_id == intent.add_package_id)
            .with_for_update()
        ).scalar_one()
        package_state = session.execute(
            select(AddAuthorizationPackageState)
            .where(AddAuthorizationPackageState.add_package_id == intent.add_package_id)
            .with_for_update()
        ).scalar_one()
        if request.cumulative_filled_quantity > ZERO and add_state.status == "CLAIMED":
            add_state.status = "CONSUMED"
            add_state.version += 1
            add_state.reason_code = "POSITIVE_ADD_FILL_CONFIRMED"
            add_state.updated_at = now
        elif request.target_status in ZERO_TERMINAL_STATUSES and add_state.status == "CLAIMED":
            reusable = (
                package_state.status == "ACTIVE"
                and package.valid_from <= now < package.valid_until
                and system_status == "NORMAL"
            )
            add_state.status = "AVAILABLE" if reusable else "INVALIDATED"
            add_state.version += 1
            add_state.reason_code = (
                "TERMINAL_ZERO_FILL_RELEASED"
                if reusable
                else "TERMINAL_ZERO_FILL_AUTHORIZATION_INVALID"
            )
            add_state.updated_at = now
        if add_state.status == "CONSUMED" and package_state.status == "ACTIVE":
            persisted_unit_statuses = tuple(
                session.execute(
                    select(AddUnit.add_unit_id, AddUnitState.status)
                    .join(AddUnit, AddUnit.add_unit_id == AddUnitState.add_unit_id)
                    .where(AddUnit.add_package_id == package.add_package_id)
                ).all()
            )
            unit_statuses = tuple(
                add_state.status if unit_id == intent.add_unit_id else status
                for unit_id, status in persisted_unit_statuses
            )
            if unit_statuses and all(status == "CONSUMED" for status in unit_statuses):
                package_state.status = "EXHAUSTED"
                package_state.version += 1
                package_state.reason_code = "ALL_ADD_UNITS_CONSUMED"
                package_state.updated_at = now

    @staticmethod
    def _activate_or_invalidate_add_package(
        session: Session,
        intent: OrderIntent,
        system_status: str,
        now: datetime,
    ) -> None:
        package = session.execute(
            select(AddAuthorizationPackage)
            .where(AddAuthorizationPackage.campaign_id == intent.campaign_id)
            .with_for_update()
        ).scalar_one_or_none()
        if package is None:
            return
        package_state = session.execute(
            select(AddAuthorizationPackageState)
            .where(AddAuthorizationPackageState.add_package_id == package.add_package_id)
            .with_for_update()
        ).scalar_one()
        if package_state.status != "DORMANT":
            return
        activate = system_status == "NORMAL" and package.valid_from <= now < package.valid_until
        package_state.status = "ACTIVE" if activate else "INVALIDATED"
        package_state.version += 1
        package_state.reason_code = (
            "INITIAL_POSITION_RECONCILED_AND_PROTECTED"
            if activate
            else "ADD_ACTIVATION_SAFETY_GATE_FAILED"
        )
        package_state.updated_at = now
        if not activate:
            unit_states = tuple(
                session.execute(
                    select(AddUnitState)
                    .join(AddUnit, AddUnit.add_unit_id == AddUnitState.add_unit_id)
                    .where(AddUnit.add_package_id == package.add_package_id)
                    .with_for_update()
                ).scalars()
            )
            for state in unit_states:
                if state.status == "AVAILABLE":
                    state.status = "INVALIDATED"
                    state.version += 1
                    state.reason_code = "ADD_ACTIVATION_SAFETY_GATE_FAILED"
                    state.updated_at = now

    @staticmethod
    def _existing_fact_outcome(
        session: Session, state: OrderIntentState, fact: ExecutionFact
    ) -> CommandOutcome:
        claim = session.get(ShadowDispatchClaim, fact.shadow_dispatch_claim_id)
        run = session.get(ExecutionReconciliationRun, fact.reconciliation_run_id)
        authority_mode = "LEGACY_UNBOUND"
        if claim is not None and run is not None:
            authority_mode = (
                "ORIGINAL_LEASE"
                if run.lease_id == claim.lease_id and run.fencing_token == claim.fencing_token
                else "SUCCESSOR_LEASE"
            )
        EXECUTION_FACT_BINDINGS.labels(
            fact.fact_kind or "LEGACY",
            fact.reconciliation_source_type or "UNBOUND",
            "ALREADY_RECORDED",
        ).inc()
        EXECUTION_FACT_AUTHORITY_MODES.labels(authority_mode, "ALREADY_RECORDED").inc()
        EXECUTION_FACT_RESULTS.labels(fact.target_status, "ALREADY_RECORDED").inc()
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="OrderIntent",
            object_id=str(fact.order_intent_id),
            object_version=state.version,
            data={
                "execution_fact_id": str(fact.execution_fact_id),
                "order_intent_id": str(fact.order_intent_id),
                "intent_status": state.status,
                "authority_mode": authority_mode,
                "already_recorded": True,
                "dispatch_eligible": False,
            },
            events=(
                DomainEvent(
                    event_type="ExecutionFactAlreadyRecorded",
                    aggregate_type="OrderIntent",
                    aggregate_id=str(fact.order_intent_id),
                    payload={"external_fact_id": fact.external_fact_id},
                ),
            ),
        )
