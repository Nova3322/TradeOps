from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import NoReturn, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_control_plane.campaign_intent_occupancy import (
    CampaignOrderIntentOccupancyService,
)
from trading_control_plane.campaign_position_binding import (
    CampaignCurrentPositionBindingService,
)
from trading_control_plane.campaign_target_facts import target_fact_from_record
from trading_control_plane.campaign_target_models import CampaignTargetPositionFactRecord
from trading_control_plane.commands import CommandRejected, hash_json
from trading_control_plane.metrics import CAMPAIGN_REDUCTION_PLAN_EVALUATIONS
from trading_control_plane.projections import ProjectionQueryContext
from trading_control_plane.trading_authorization_models import CampaignState

PLAN_VERSION = "campaign-reduction-execution-plan-v1"


class CampaignReductionExecutionPlan(BaseModel):
    """Read-only OMS input; it has no OrderIntent or dispatch authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_id: UUID
    target_fact_id: UUID
    target_version: int = Field(gt=0)
    target_semantic_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_position_binding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_position_snapshot_id: UUID
    current_position_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    direction: str = Field(pattern=r"^(LONG|SHORT)$")
    side: str = Field(pattern=r"^(BUY|SELL)$")
    position_side: str = Field(min_length=1, max_length=20)
    current_quantity: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    target_quantity: Decimal = Field(ge=0, max_digits=38, decimal_places=18)
    order_quantity: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    action: str = Field(pattern=r"^(REDUCE|EXIT)$")
    urgency: str = Field(pattern=r"^(ORDERLY|URGENT|IMMEDIATE)$")
    reason_codes: tuple[str, ...] = Field(min_length=1)
    reduce_only: bool
    plan_idempotency_ref: str = Field(min_length=1, max_length=255)
    order_type_status: str = Field(pattern=r"^UNAVAILABLE$")
    venue_execution_terms_status: str = Field(pattern=r"^UNAVAILABLE$")
    planned_at: datetime
    valid_until: datetime
    plan_version: str = Field(pattern=r"^campaign-reduction-execution-plan-v[0-9]+$")
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment: str = Field(pattern=r"^SHADOW$")
    live_order_eligible: bool

    @model_validator(mode="after")
    def plan_is_self_consistent(self) -> Self:
        if self.target_quantity >= self.current_quantity:
            raise ValueError("reduction plan must strictly reduce the current position")
        if self.order_quantity != self.current_quantity - self.target_quantity:
            raise ValueError("reduction plan quantity is inconsistent")
        expected_action = "EXIT" if self.target_quantity == 0 else "REDUCE"
        if self.action != expected_action:
            raise ValueError("reduction plan action is inconsistent")
        if self.side != _reduction_side(self.direction):
            raise ValueError("reduction plan side would not reduce the position")
        if not self.reduce_only or self.live_order_eligible:
            raise ValueError("reduction plan must remain non-dispatchable and reduce-only")
        if self.valid_until <= self.planned_at:
            raise ValueError("reduction plan validity is empty")
        material = self.model_dump(mode="json", exclude={"plan_hash"})
        if self.plan_hash != hash_json(material):
            raise ValueError("reduction plan hash mismatch")
        return self


class CampaignReductionExecutionPlanService:
    @staticmethod
    def resolve(
        session: Session,
        campaign_id: UUID,
        context: ProjectionQueryContext,
        *,
        lock_intents: bool = False,
    ) -> CampaignReductionExecutionPlan:
        record = session.execute(
            select(CampaignTargetPositionFactRecord)
            .where(CampaignTargetPositionFactRecord.campaign_id == campaign_id)
            .order_by(CampaignTargetPositionFactRecord.target_version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if record is None:
            _reject(
                "TARGET_MISSING",
                "CAMPAIGN_REDUCTION_TARGET_MISSING",
                "Campaign has no durable target-position fact",
            )
        target = target_fact_from_record(record)
        if target.action not in {"REDUCE", "EXIT"}:
            _reject(
                "TARGET_NOT_ACTIONABLE",
                "CAMPAIGN_REDUCTION_TARGET_NOT_ACTIONABLE",
                "latest Campaign target does not require a reduction",
            )
        position = CampaignCurrentPositionBindingService.resolve(session, campaign_id, context)
        if context.as_of >= position.valid_until:
            _reject(
                "POSITION_EXPIRED",
                "CAMPAIGN_REDUCTION_POSITION_EXPIRED",
                "canonical current position has no remaining plan validity",
            )
        if (
            target.current_position_binding_hash != position.binding_hash
            or target.current_position_snapshot_id != position.current_position_snapshot_id
            or target.current_position_snapshot_hash != position.current_position_snapshot_hash
            or target.current_quantity != position.current_quantity
        ):
            _reject(
                "TARGET_REQUIRES_REFRESH",
                "CAMPAIGN_REDUCTION_TARGET_REQUIRES_REFRESH",
                "durable target is not bound to the fresh current position",
            )
        state = session.get(CampaignState, campaign_id)
        expected_state = "CLOSING" if target.action == "EXIT" else "OPEN"
        if state is None or state.status != expected_state:
            _reject(
                "CAMPAIGN_STATE_INVALID",
                "CAMPAIGN_REDUCTION_STATE_INVALID",
                "Campaign state does not match its actionable target",
            )
        occupancy = CampaignOrderIntentOccupancyService.resolve(
            session,
            campaign_id,
            lock=lock_intents,
        )
        if occupancy.status != "CLEAR":
            _reject(
                "INTENT_OCCUPIED",
                "CAMPAIGN_REDUCTION_INTENT_OCCUPIED",
                "an unresolved OrderIntent can still change the Campaign position",
            )

        draft = CampaignReductionExecutionPlan.model_construct(
            campaign_id=campaign_id,
            target_fact_id=target.campaign_target_position_fact_id,
            target_version=target.target_version,
            target_semantic_hash=target.target_semantic_hash,
            current_position_binding_hash=position.binding_hash,
            current_position_snapshot_id=position.current_position_snapshot_id,
            current_position_snapshot_hash=position.current_position_snapshot_hash,
            direction=position.direction,
            side=_reduction_side(position.direction),
            position_side=position.position_side,
            current_quantity=position.current_quantity,
            target_quantity=target.target_quantity,
            order_quantity=position.current_quantity - target.target_quantity,
            action=target.action,
            urgency=target.urgency,
            reason_codes=target.all_reason_codes,
            reduce_only=True,
            plan_idempotency_ref=(
                f"campaign-reduction:{campaign_id}:{target.target_version}:{position.binding_hash}"
            ),
            order_type_status="UNAVAILABLE",
            venue_execution_terms_status="UNAVAILABLE",
            planned_at=context.as_of,
            valid_until=position.valid_until,
            plan_version=PLAN_VERSION,
            plan_hash="0" * 64,
            environment="SHADOW",
            live_order_eligible=False,
        )
        plan = CampaignReductionExecutionPlan.model_validate(
            {
                **draft.model_dump(mode="python"),
                "plan_hash": hash_json(draft.model_dump(mode="json", exclude={"plan_hash"})),
            }
        )
        CAMPAIGN_REDUCTION_PLAN_EVALUATIONS.labels(plan.action).inc()
        return plan


def _reduction_side(direction: str) -> str:
    if direction == "LONG":
        return "SELL"
    if direction == "SHORT":
        return "BUY"
    raise ValueError("reduction direction must be LONG or SHORT")


def _reject(metric_result: str, error_code: str, detail: str) -> NoReturn:
    CAMPAIGN_REDUCTION_PLAN_EVALUATIONS.labels(metric_result).inc()
    raise CommandRejected(error_code, detail)
