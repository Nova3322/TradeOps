from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from trading_control_plane.campaign_position_binding import (
    CampaignCurrentPositionBinding,
    CampaignCurrentPositionBindingService,
)
from trading_control_plane.commands import CommandRejected, hash_json
from trading_control_plane.metrics import CAMPAIGN_PROTECTION_EXIT_EVALUATIONS
from trading_control_plane.projections import (
    CurrentProtectionProjection,
    CurrentProtectionScope,
    ProjectionQueryContext,
    ProjectionState,
    VenueCurrentProjectionService,
)
from trading_control_plane.target_position_arbiter import (
    ReductionSourceType,
    ReductionUrgency,
    TargetPositionCandidate,
)

EVALUATION_VERSION = "campaign-protection-exit-evaluation-v1"
POLICY_VERSION = "canonical-protection-health-v1"
FAILURE_REASONS = (
    "PROTECTION_MISSING",
    "PROTECTION_STALE",
    "PROTECTION_FROM_FUTURE",
    "PROTECTION_UNKNOWN",
    "PROTECTION_BINDING_CONFLICT",
)


class ProtectionExitStatus(StrEnum):
    CLEAR = "CLEAR"
    EXIT_REQUIRED = "EXIT_REQUIRED"


class CampaignProtectionExitEvaluation(BaseModel):
    """Fresh Campaign position plus canonical protection health, without execution authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_id: UUID
    current_position_binding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_position_snapshot_id: UUID
    current_position_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    direction: str = Field(pattern=r"^(LONG|SHORT)$")
    current_quantity: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    current_facts_as_of: datetime
    protection_projection_state: str = Field(pattern=r"^(CONFIRMED|UNKNOWN)$")
    protection_snapshot_id: UUID | None
    protection_snapshot_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    protection_position_snapshot_id: UUID | None
    protected_direction: str = Field(pattern=r"^(LONG|SHORT|UNKNOWN)$")
    protected_quantity: Decimal | None = Field(
        default=None,
        gt=0,
        max_digits=38,
        decimal_places=18,
    )
    protection_order_set_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    protection_facts_as_of: datetime | None
    status: ProtectionExitStatus
    failure_reason: str | None
    candidate: TargetPositionCandidate | None
    evaluated_at: datetime
    valid_until: datetime
    evaluation_version: str = Field(pattern=r"^campaign-protection-exit-evaluation-v[0-9]+$")
    evaluation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def evaluation_is_self_consistent(self) -> Self:
        if self.valid_until <= self.evaluated_at:
            raise ValueError("protection-exit evaluation validity is empty")
        exact_binding = (
            self.protection_position_snapshot_id == self.current_position_snapshot_id
            and self.protected_direction == self.direction
            and self.protected_quantity == self.current_quantity
            and self.protection_facts_as_of is not None
            and self.protection_facts_as_of >= self.current_facts_as_of
        )
        if self.status is ProtectionExitStatus.CLEAR:
            if (
                self.protection_projection_state != ProjectionState.CONFIRMED.value
                or self.protection_snapshot_id is None
                or self.protection_snapshot_hash is None
                or self.protection_order_set_hash is None
                or not exact_binding
                or self.failure_reason is not None
                or self.candidate is not None
            ):
                raise ValueError("clear protection evaluation lacks confirmed exact protection")
        else:
            if self.failure_reason not in FAILURE_REASONS or self.candidate is None:
                raise ValueError("protection failure lacks an exit candidate and bounded reason")
            if (self.protection_projection_state == ProjectionState.CONFIRMED.value) != (
                self.failure_reason == "PROTECTION_BINDING_CONFLICT"
            ):
                raise ValueError("protection failure reason does not match projection state")
            if self.protection_projection_state == ProjectionState.CONFIRMED.value and (
                self.protection_snapshot_id is None
                or self.protection_snapshot_hash is None
                or self.protection_order_set_hash is None
                or exact_binding
            ):
                raise ValueError("confirmed protection failure lacks a real binding conflict")
            if self.protection_projection_state == ProjectionState.UNKNOWN.value and (
                self.protection_position_snapshot_id is not None
                or self.protected_direction != "UNKNOWN"
                or self.protected_quantity is not None
                or self.protection_order_set_hash is not None
            ):
                raise ValueError("unknown protection cannot expose protected-position semantics")
            expected_source_ref = (
                f"protection-health:{self.current_position_snapshot_id}:{self.failure_reason}"
            )
            if (
                self.candidate.source_type is not ReductionSourceType.SYSTEM_RISK_REDUCTION
                or self.candidate.source_ref != expected_source_ref
                or self.candidate.policy_version != POLICY_VERSION
                or self.candidate.reason_code != self.failure_reason
                or self.candidate.current_position_binding_hash
                != self.current_position_binding_hash
                or self.candidate.target_quantity != 0
                or self.candidate.urgency is not ReductionUrgency.IMMEDIATE
                or self.candidate.facts_as_of != self.evaluated_at
                or self.candidate.valid_until != self.valid_until
            ):
                raise ValueError("protection-failure candidate does not match its evaluation")
        material = self.model_dump(mode="json", exclude={"evaluation_hash"})
        if self.evaluation_hash != hash_json(material):
            raise ValueError("protection-exit evaluation hash mismatch")
        return self


class CampaignProtectionExitCandidateService:
    """Requires exit when canonical native protection is unavailable or not exact."""

    @staticmethod
    def evaluate(
        session: Session,
        campaign_id: UUID,
        context: ProjectionQueryContext,
    ) -> CampaignProtectionExitEvaluation:
        position = CampaignCurrentPositionBindingService.resolve(session, campaign_id, context)
        if context.as_of >= position.valid_until:
            CAMPAIGN_PROTECTION_EXIT_EVALUATIONS.labels("POSITION_EXPIRED").inc()
            raise CommandRejected(
                "CAMPAIGN_PROTECTION_EXIT_POSITION_EXPIRED",
                "canonical Campaign position has no remaining decision validity",
            )
        protection = VenueCurrentProjectionService.current_protection(
            session,
            _protection_scope(position),
            context,
        )
        exact = _protection_is_exact(position, protection)
        failure_reason = None if exact else _protection_failure_reason(protection)
        status = ProtectionExitStatus.CLEAR if exact else ProtectionExitStatus.EXIT_REQUIRED
        candidate = (
            None
            if failure_reason is None
            else _candidate(
                position=position,
                failure_reason=failure_reason,
                evaluated_at=context.as_of,
            )
        )
        draft = CampaignProtectionExitEvaluation.model_construct(
            campaign_id=campaign_id,
            current_position_binding_hash=position.binding_hash,
            current_position_snapshot_id=position.current_position_snapshot_id,
            current_position_snapshot_hash=position.current_position_snapshot_hash,
            direction=position.direction,
            current_quantity=position.current_quantity,
            current_facts_as_of=position.current_facts_as_of,
            protection_projection_state=protection.projection_state.value,
            protection_snapshot_id=protection.source_snapshot_id,
            protection_snapshot_hash=protection.source_snapshot_hash,
            protection_position_snapshot_id=protection.source_position_snapshot_id,
            protected_direction=protection.protected_direction.value,
            protected_quantity=protection.position_quantity,
            protection_order_set_hash=protection.order_set_hash,
            protection_facts_as_of=protection.facts_as_of,
            status=status,
            failure_reason=failure_reason,
            candidate=candidate,
            evaluated_at=context.as_of,
            valid_until=position.valid_until,
            evaluation_version=EVALUATION_VERSION,
            evaluation_hash="0" * 64,
        )
        evaluation = CampaignProtectionExitEvaluation.model_validate(
            {
                **draft.model_dump(mode="python"),
                "evaluation_hash": hash_json(
                    draft.model_dump(mode="json", exclude={"evaluation_hash"})
                ),
            }
        )
        CAMPAIGN_PROTECTION_EXIT_EVALUATIONS.labels(status.value).inc()
        return evaluation


def _protection_scope(position: CampaignCurrentPositionBinding) -> CurrentProtectionScope:
    return CurrentProtectionScope(
        organization_id=position.organization_id,
        venue=position.venue,
        execution_domain=position.execution_domain,
        account_id=position.account_id,
        instrument_id=position.instrument_id,
        position_mode=position.position_mode,
        position_side=position.position_side,
        margin_mode=position.margin_mode,
        collateral_pool_id=position.collateral_pool_id,
        settlement_currency=position.settlement_currency,
    )


def _protection_is_exact(
    position: CampaignCurrentPositionBinding,
    protection: CurrentProtectionProjection,
) -> bool:
    return (
        protection.projection_state is ProjectionState.CONFIRMED
        and protection.source_position_snapshot_id == position.current_position_snapshot_id
        and protection.protected_direction.value == position.direction
        and protection.position_quantity == position.current_quantity
        and protection.facts_as_of is not None
        and protection.facts_as_of >= position.current_facts_as_of
    )


def _protection_failure_reason(protection: CurrentProtectionProjection) -> str:
    if protection.projection_state is ProjectionState.CONFIRMED:
        return "PROTECTION_BINDING_CONFLICT"
    return {
        "SOURCE_MISSING": "PROTECTION_MISSING",
        "SOURCE_STALE": "PROTECTION_STALE",
        "SOURCE_FROM_FUTURE": "PROTECTION_FROM_FUTURE",
    }.get(protection.reason_code or "", "PROTECTION_UNKNOWN")


def _candidate(
    *,
    position: CampaignCurrentPositionBinding,
    failure_reason: str,
    evaluated_at: datetime,
) -> TargetPositionCandidate:
    draft = TargetPositionCandidate.model_construct(
        source_type=ReductionSourceType.SYSTEM_RISK_REDUCTION,
        source_ref=(f"protection-health:{position.current_position_snapshot_id}:{failure_reason}"),
        policy_version=POLICY_VERSION,
        reason_code=failure_reason,
        current_position_binding_hash=position.binding_hash,
        target_quantity=Decimal("0"),
        urgency=ReductionUrgency.IMMEDIATE,
        facts_as_of=evaluated_at,
        valid_until=position.valid_until,
        candidate_hash="0" * 64,
    )
    return TargetPositionCandidate.model_validate(
        {
            **draft.model_dump(mode="python"),
            "candidate_hash": hash_json(draft.model_dump(mode="json", exclude={"candidate_hash"})),
        }
    )
