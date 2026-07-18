from __future__ import annotations

from datetime import datetime
from typing import NoReturn, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_control_plane.campaign_reduction_models import (
    CampaignReductionPlanSnapshotRecord,
)
from trading_control_plane.campaign_reduction_plan import (
    CampaignReductionExecutionPlan,
    CampaignReductionExecutionPlanService,
)
from trading_control_plane.campaign_reduction_preparation import (
    reduction_plan_snapshot_from_record,
)
from trading_control_plane.campaign_target_models import CampaignTargetPositionFactRecord
from trading_control_plane.commands import CommandRejected, hash_json
from trading_control_plane.metrics import CAMPAIGN_REDUCTION_PLAN_VALIDITY_EVALUATIONS
from trading_control_plane.projections import ProjectionQueryContext

VALIDITY_VERSION = "campaign-reduction-plan-validity-v1"


class CampaignReductionPlanValidity(BaseModel):
    """Current-use evaluation; explicitly never a dispatch permit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_reduction_plan_snapshot_id: UUID
    campaign_id: UUID
    stored_target_fact_id: UUID
    stored_target_version: int = Field(gt=0)
    stored_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_target_fact_id: UUID | None
    current_target_version: int | None = Field(default=None, gt=0)
    status: str = Field(
        pattern=(
            r"^(CURRENT|SUPERSEDED|EXPIRED|POSITION_CHANGED|POSITION_UNAVAILABLE|"
            r"INTENT_OCCUPIED|CAMPAIGN_STATE_INVALID|TARGET_NOT_ACTIONABLE)$"
        )
    )
    reason_code: str = Field(min_length=1, max_length=160)
    reprepare_required: bool
    order_type_status: str = Field(pattern=r"^UNAVAILABLE$")
    venue_execution_terms_status: str = Field(pattern=r"^UNAVAILABLE$")
    dispatch_eligible: bool
    evaluated_at: datetime
    stored_valid_until: datetime
    validity_version: str = Field(pattern=r"^campaign-reduction-plan-validity-v[0-9]+$")
    validity_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validity_is_self_consistent(self) -> Self:
        if self.dispatch_eligible:
            raise ValueError("Campaign reduction validity cannot grant dispatch authority")
        if self.status == "CURRENT":
            if (
                self.reprepare_required
                or self.current_target_fact_id != self.stored_target_fact_id
                or self.current_target_version != self.stored_target_version
                or self.reason_code != "EXECUTION_TERMS_UNAVAILABLE"
                or self.evaluated_at >= self.stored_valid_until
            ):
                raise ValueError("current Campaign reduction validity is inconsistent")
        elif not self.reprepare_required:
            raise ValueError("non-current Campaign reduction plan must require re-preparation")
        material = self.model_dump(mode="json", exclude={"validity_hash"})
        if self.validity_hash != hash_json(material):
            raise ValueError("Campaign reduction validity hash mismatch")
        return self


class CampaignReductionPlanValidityService:
    @staticmethod
    def evaluate(
        session: Session,
        snapshot_id: UUID,
        context: ProjectionQueryContext,
    ) -> CampaignReductionPlanValidity:
        record = session.get(CampaignReductionPlanSnapshotRecord, snapshot_id)
        if record is None:
            _reject(
                "SNAPSHOT_MISSING",
                "CAMPAIGN_REDUCTION_PLAN_SNAPSHOT_MISSING",
                "Campaign reduction plan snapshot is unavailable",
            )
        snapshot = reduction_plan_snapshot_from_record(record)
        stored_plan = snapshot.plan()
        latest_target = session.execute(
            select(CampaignTargetPositionFactRecord)
            .where(CampaignTargetPositionFactRecord.campaign_id == snapshot.campaign_id)
            .order_by(CampaignTargetPositionFactRecord.target_version.desc())
            .limit(1)
        ).scalar_one_or_none()
        current_target_fact_id = (
            latest_target.campaign_target_position_fact_id if latest_target is not None else None
        )
        current_target_version = latest_target.target_version if latest_target is not None else None
        if (
            current_target_fact_id != snapshot.campaign_target_position_fact_id
            or current_target_version != snapshot.target_version
        ):
            return _result(
                snapshot_id=snapshot_id,
                stored_plan=stored_plan,
                current_target_fact_id=current_target_fact_id,
                current_target_version=current_target_version,
                status="SUPERSEDED",
                reason_code="CAMPAIGN_REDUCTION_TARGET_SUPERSEDED",
                evaluated_at=context.as_of,
            )
        if context.as_of >= stored_plan.valid_until:
            return _result(
                snapshot_id=snapshot_id,
                stored_plan=stored_plan,
                current_target_fact_id=current_target_fact_id,
                current_target_version=current_target_version,
                status="EXPIRED",
                reason_code="CAMPAIGN_REDUCTION_PLAN_EXPIRED",
                evaluated_at=context.as_of,
            )
        try:
            current_plan = CampaignReductionExecutionPlanService.resolve(
                session,
                snapshot.campaign_id,
                context,
            )
        except CommandRejected as exc:
            status = _status_for_rejection(exc.error_code)
            if status is None:
                raise
            return _result(
                snapshot_id=snapshot_id,
                stored_plan=stored_plan,
                current_target_fact_id=current_target_fact_id,
                current_target_version=current_target_version,
                status=status,
                reason_code=exc.error_code,
                evaluated_at=context.as_of,
            )
        if _stable_plan_material(current_plan) != _stable_plan_material(stored_plan):
            return _result(
                snapshot_id=snapshot_id,
                stored_plan=stored_plan,
                current_target_fact_id=current_target_fact_id,
                current_target_version=current_target_version,
                status="POSITION_CHANGED",
                reason_code="CAMPAIGN_REDUCTION_PLAN_SOURCE_CHANGED",
                evaluated_at=context.as_of,
            )
        return _result(
            snapshot_id=snapshot_id,
            stored_plan=stored_plan,
            current_target_fact_id=current_target_fact_id,
            current_target_version=current_target_version,
            status="CURRENT",
            reason_code="EXECUTION_TERMS_UNAVAILABLE",
            evaluated_at=context.as_of,
        )


def _stable_plan_material(plan: CampaignReductionExecutionPlan) -> dict[str, object]:
    return plan.model_dump(mode="json", exclude={"planned_at", "plan_hash"})


def _status_for_rejection(error_code: str) -> str | None:
    if error_code == "CAMPAIGN_REDUCTION_INTENT_OCCUPIED":
        return "INTENT_OCCUPIED"
    if error_code == "CAMPAIGN_REDUCTION_STATE_INVALID":
        return "CAMPAIGN_STATE_INVALID"
    if error_code == "CAMPAIGN_REDUCTION_TARGET_NOT_ACTIONABLE":
        return "TARGET_NOT_ACTIONABLE"
    if error_code in {
        "CAMPAIGN_REDUCTION_TARGET_REQUIRES_REFRESH",
        "CAMPAIGN_REDUCTION_POSITION_EXPIRED",
    }:
        return "POSITION_CHANGED"
    if error_code.startswith("CAMPAIGN_CURRENT_POSITION_"):
        return "POSITION_UNAVAILABLE"
    return None


def _result(
    *,
    snapshot_id: UUID,
    stored_plan: CampaignReductionExecutionPlan,
    current_target_fact_id: UUID | None,
    current_target_version: int | None,
    status: str,
    reason_code: str,
    evaluated_at: datetime,
) -> CampaignReductionPlanValidity:
    draft = CampaignReductionPlanValidity.model_construct(
        campaign_reduction_plan_snapshot_id=snapshot_id,
        campaign_id=stored_plan.campaign_id,
        stored_target_fact_id=stored_plan.target_fact_id,
        stored_target_version=stored_plan.target_version,
        stored_plan_hash=stored_plan.plan_hash,
        current_target_fact_id=current_target_fact_id,
        current_target_version=current_target_version,
        status=status,
        reason_code=reason_code,
        reprepare_required=status != "CURRENT",
        order_type_status="UNAVAILABLE",
        venue_execution_terms_status="UNAVAILABLE",
        dispatch_eligible=False,
        evaluated_at=evaluated_at,
        stored_valid_until=stored_plan.valid_until,
        validity_version=VALIDITY_VERSION,
        validity_hash="0" * 64,
    )
    result = CampaignReductionPlanValidity.model_validate(
        {
            **draft.model_dump(mode="python"),
            "validity_hash": hash_json(draft.model_dump(mode="json", exclude={"validity_hash"})),
        }
    )
    CAMPAIGN_REDUCTION_PLAN_VALIDITY_EVALUATIONS.labels(result.status).inc()
    return result


def _reject(metric_result: str, error_code: str, detail: str) -> NoReturn:
    CAMPAIGN_REDUCTION_PLAN_VALIDITY_EVALUATIONS.labels(metric_result).inc()
    raise CommandRejected(error_code, detail)
