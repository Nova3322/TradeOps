from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from trading_control_plane.campaign_reduction_models import (
    CampaignReductionPlanSnapshotRecord,
)
from trading_control_plane.campaign_reduction_plan import (
    CampaignReductionExecutionPlan,
    CampaignReductionExecutionPlanService,
)
from trading_control_plane.campaign_target_facts import target_fact_from_record
from trading_control_plane.campaign_target_models import CampaignTargetPositionFactRecord
from trading_control_plane.commands import (
    CommandChannel,
    CommandEnvelope,
    CommandOutcome,
    CommandRejected,
    CommandStatus,
    DomainEvent,
    hash_json,
)
from trading_control_plane.metrics import CAMPAIGN_REDUCTION_PLAN_PREPARATIONS
from trading_control_plane.projections import ProjectionQueryContext
from trading_control_plane.trading_authorization_models import Campaign, CampaignState

SERVICE_PRINCIPAL = "campaign-reduction-preparation-service"
RECORD_VERSION = "campaign-reduction-plan-snapshot-v1"


class PrepareCampaignReductionPlanRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_id: UUID
    organization_id: str = Field(min_length=1, max_length=120)
    max_age_ms: int = Field(gt=0, le=300_000)


class CampaignReductionPlanSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_reduction_plan_snapshot_id: UUID
    campaign_id: UUID
    organization_id: str = Field(min_length=1, max_length=120)
    campaign_target_position_fact_id: UUID
    target_version: int = Field(gt=0)
    target_semantic_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_position_binding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_idempotency_ref: str = Field(min_length=1, max_length=255)
    plan_payload: dict[str, JsonValue]
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_version: str = Field(pattern=r"^campaign-reduction-plan-snapshot-v[0-9]+$")
    record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment: str = Field(pattern=r"^SHADOW$")
    dispatch_eligible: bool
    recorded_at: datetime

    @model_validator(mode="after")
    def snapshot_is_self_consistent(self) -> Self:
        plan = self.plan()
        if (
            self.campaign_id != plan.campaign_id
            or self.campaign_target_position_fact_id != plan.target_fact_id
            or self.target_version != plan.target_version
            or self.target_semantic_hash != plan.target_semantic_hash
            or self.current_position_binding_hash != plan.current_position_binding_hash
            or self.plan_idempotency_ref != plan.plan_idempotency_ref
            or self.plan_hash != plan.plan_hash
            or self.environment != plan.environment
        ):
            raise ValueError("Campaign reduction plan payload and columns diverged")
        if self.dispatch_eligible or plan.live_order_eligible:
            raise ValueError("Campaign reduction plan snapshot cannot be dispatch eligible")
        if self.recorded_at < plan.planned_at:
            raise ValueError("Campaign reduction plan snapshot predates its plan")
        if self.record_hash != hash_json(_record_hash_contract(self)):
            raise ValueError("Campaign reduction plan snapshot hash mismatch")
        return self

    def plan(self) -> CampaignReductionExecutionPlan:
        return CampaignReductionExecutionPlan.model_validate(self.plan_payload)


def _record_hash_contract(snapshot: CampaignReductionPlanSnapshot) -> dict[str, JsonValue]:
    return {
        "campaign_reduction_plan_snapshot_id": str(snapshot.campaign_reduction_plan_snapshot_id),
        "campaign_id": str(snapshot.campaign_id),
        "organization_id": snapshot.organization_id,
        "campaign_target_position_fact_id": str(snapshot.campaign_target_position_fact_id),
        "target_version": snapshot.target_version,
        "current_position_binding_hash": snapshot.current_position_binding_hash,
        "plan_payload": snapshot.plan_payload,
        "record_version": snapshot.record_version,
        "environment": snapshot.environment,
        "dispatch_eligible": snapshot.dispatch_eligible,
        "recorded_at": snapshot.recorded_at.astimezone(UTC).isoformat(),
    }


def reduction_plan_snapshot_from_record(
    record: CampaignReductionPlanSnapshotRecord,
) -> CampaignReductionPlanSnapshot:
    return CampaignReductionPlanSnapshot.model_validate(record.contract())


class CampaignReductionPlanPreparationService:
    """Persists one immutable, non-dispatchable preparation per target fact."""

    command_type = "campaign.reduction-plan.prepare.v1"
    payload_schema_version = 1

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def prepare(
        self,
        session: Session,
        envelope: CommandEnvelope,
    ) -> CommandOutcome:
        self._require_service(envelope)
        try:
            request = PrepareCampaignReductionPlanRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected(
                "CAMPAIGN_REDUCTION_PREPARATION_INPUT_INVALID",
                "Campaign reduction preparation input is invalid",
            ) from exc
        if envelope.object_id != str(request.campaign_id):
            raise CommandRejected("OBJECT_BINDING_MISMATCH", "Campaign identity changed")
        if envelope.scope.get("organization_id") != request.organization_id:
            raise CommandRejected("SCOPE_MISMATCH", "organization scope changed")

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise CommandRejected(
                "CAMPAIGN_REDUCTION_PREPARATION_CLOCK_INVALID",
                "Campaign reduction preparation clock must be timezone-aware",
            )
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"campaign-target:{request.campaign_id}"},
        )
        campaign = session.execute(
            select(Campaign).where(Campaign.campaign_id == request.campaign_id).with_for_update()
        ).scalar_one_or_none()
        if campaign is None:
            raise CommandRejected("CAMPAIGN_NOT_FOUND", "Campaign is unavailable")
        if campaign.organization_id != request.organization_id:
            raise CommandRejected("SCOPE_MISMATCH", "Campaign organization changed")
        session.execute(
            select(CampaignState)
            .where(CampaignState.campaign_id == request.campaign_id)
            .with_for_update()
        ).scalar_one()
        target_record = session.execute(
            select(CampaignTargetPositionFactRecord)
            .where(CampaignTargetPositionFactRecord.campaign_id == request.campaign_id)
            .order_by(CampaignTargetPositionFactRecord.target_version.desc())
            .limit(1)
            .with_for_update()
        ).scalar_one_or_none()
        if target_record is None:
            raise CommandRejected(
                "CAMPAIGN_REDUCTION_TARGET_MISSING",
                "Campaign has no durable target-position fact",
            )
        target = target_fact_from_record(target_record)
        if envelope.expected_version != target.target_version:
            raise CommandRejected("VERSION_CONFLICT", "Campaign target version changed")

        existing = session.execute(
            select(CampaignReductionPlanSnapshotRecord)
            .where(
                CampaignReductionPlanSnapshotRecord.campaign_target_position_fact_id
                == target.campaign_target_position_fact_id
            )
            .with_for_update()
        ).scalar_one_or_none()
        if existing is not None:
            snapshot = reduction_plan_snapshot_from_record(existing)
            if now >= snapshot.plan().valid_until:
                CAMPAIGN_REDUCTION_PLAN_PREPARATIONS.labels("EXPIRED").inc()
                raise CommandRejected(
                    "CAMPAIGN_REDUCTION_PREPARATION_EXPIRED",
                    "existing reduction preparation expired; refresh the target binding",
                )
            CAMPAIGN_REDUCTION_PLAN_PREPARATIONS.labels("ALREADY_PREPARED").inc()
            return self._existing_outcome(snapshot)

        plan = CampaignReductionExecutionPlanService.resolve(
            session,
            request.campaign_id,
            ProjectionQueryContext(as_of=now, max_age_ms=request.max_age_ms),
            lock_intents=True,
        )
        if plan.target_fact_id != target.campaign_target_position_fact_id:
            raise RuntimeError("Campaign reduction target changed inside one transaction")
        snapshot = self._snapshot(
            snapshot_id=envelope.command_id,
            organization_id=request.organization_id,
            plan=plan,
            recorded_at=now,
        )
        session.add(CampaignReductionPlanSnapshotRecord(**snapshot.model_dump(mode="python")))
        CAMPAIGN_REDUCTION_PLAN_PREPARATIONS.labels(plan.action).inc()
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="CampaignReductionPlan",
            object_id=str(request.campaign_id),
            object_version=plan.target_version,
            data={
                "campaign_reduction_plan_snapshot_id": str(
                    snapshot.campaign_reduction_plan_snapshot_id
                ),
                "target_version": plan.target_version,
                "action": plan.action,
                "order_quantity": str(plan.order_quantity),
                "plan_hash": plan.plan_hash,
                "environment": "SHADOW",
                "dispatch_eligible": False,
                "result": "PREPARED",
            },
            events=(
                DomainEvent(
                    event_type="CampaignReductionPlanPrepared",
                    aggregate_type="Campaign",
                    aggregate_id=str(request.campaign_id),
                    payload={
                        "campaign_reduction_plan_snapshot_id": str(
                            snapshot.campaign_reduction_plan_snapshot_id
                        ),
                        "campaign_target_position_fact_id": str(plan.target_fact_id),
                        "target_version": plan.target_version,
                        "action": plan.action,
                        "order_quantity": str(plan.order_quantity),
                        "plan_hash": plan.plan_hash,
                        "environment": "SHADOW",
                        "dispatch_eligible": False,
                    },
                ),
            ),
        )

    def _require_service(self, envelope: CommandEnvelope) -> None:
        if envelope.command_type != self.command_type:
            raise CommandRejected("COMMAND_TYPE_MISMATCH", "unexpected command type")
        if envelope.payload_schema_version != self.payload_schema_version:
            raise CommandRejected(
                "PAYLOAD_SCHEMA_VERSION_MISMATCH",
                "Campaign reduction preparation payload version is unsupported",
            )
        if (
            envelope.channel is not CommandChannel.INTERNAL
            or envelope.service_principal != SERVICE_PRINCIPAL
            or envelope.object_type != "CampaignReductionPlan"
        ):
            raise CommandRejected(
                "CAMPAIGN_REDUCTION_PREPARATION_SERVICE_REQUIRED",
                "only the Campaign reduction preparation service may persist plans",
            )

    @staticmethod
    def _snapshot(
        *,
        snapshot_id: UUID,
        organization_id: str,
        plan: CampaignReductionExecutionPlan,
        recorded_at: datetime,
    ) -> CampaignReductionPlanSnapshot:
        draft = CampaignReductionPlanSnapshot.model_construct(
            campaign_reduction_plan_snapshot_id=snapshot_id,
            campaign_id=plan.campaign_id,
            organization_id=organization_id,
            campaign_target_position_fact_id=plan.target_fact_id,
            target_version=plan.target_version,
            target_semantic_hash=plan.target_semantic_hash,
            current_position_binding_hash=plan.current_position_binding_hash,
            plan_idempotency_ref=plan.plan_idempotency_ref,
            plan_payload=plan.model_dump(mode="json"),
            plan_hash=plan.plan_hash,
            record_version=RECORD_VERSION,
            record_hash="0" * 64,
            environment="SHADOW",
            dispatch_eligible=False,
            recorded_at=recorded_at,
        )
        return CampaignReductionPlanSnapshot.model_validate(
            {
                **draft.model_dump(mode="python"),
                "record_hash": hash_json(_record_hash_contract(draft)),
            }
        )

    @staticmethod
    def _existing_outcome(snapshot: CampaignReductionPlanSnapshot) -> CommandOutcome:
        plan = snapshot.plan()
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="CampaignReductionPlan",
            object_id=str(snapshot.campaign_id),
            object_version=snapshot.target_version,
            data={
                "campaign_reduction_plan_snapshot_id": str(
                    snapshot.campaign_reduction_plan_snapshot_id
                ),
                "target_version": snapshot.target_version,
                "action": plan.action,
                "order_quantity": str(plan.order_quantity),
                "plan_hash": snapshot.plan_hash,
                "environment": snapshot.environment,
                "dispatch_eligible": snapshot.dispatch_eligible,
                "result": "ALREADY_PREPARED",
            },
        )
