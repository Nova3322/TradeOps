from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from trading_control_plane.campaign_protection_exit import (
    CampaignProtectionExitCandidateService,
)
from trading_control_plane.campaign_target_models import CampaignTargetPositionFactRecord
from trading_control_plane.campaign_target_position import (
    CampaignTargetPositionEvaluationService,
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
from trading_control_plane.metrics import CAMPAIGN_TARGET_FACT_RECORDINGS
from trading_control_plane.projections import ProjectionQueryContext
from trading_control_plane.target_position_arbiter import TargetPositionDecision
from trading_control_plane.trading_authorization_models import Campaign, CampaignState

SERVICE_PRINCIPAL = "campaign-target-service"
RECORD_VERSION = "campaign-target-position-fact-v1"


class EvaluateRecordCampaignTargetRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_id: UUID
    organization_id: str = Field(min_length=1, max_length=120)
    max_age_ms: int = Field(gt=0, le=300_000)


class CampaignTargetPositionFactSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_target_position_fact_id: UUID
    campaign_id: UUID
    organization_id: str = Field(min_length=1, max_length=120)
    target_version: int = Field(gt=0)
    current_position_binding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_position_snapshot_id: UUID
    current_position_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_quantity: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    target_quantity: Decimal = Field(ge=0, max_digits=38, decimal_places=18)
    reduction_quantity: Decimal = Field(ge=0, max_digits=38, decimal_places=18)
    action: str = Field(pattern=r"^(HOLD|REDUCE|EXIT)$")
    requires_order: bool
    reduce_only_required: bool
    urgency: str = Field(pattern=r"^(NONE|ORDERLY|URGENT|IMMEDIATE)$")
    selected_target_source_refs: tuple[str, ...]
    selected_urgency_source_refs: tuple[str, ...]
    target_reason_codes: tuple[str, ...]
    urgency_reason_codes: tuple[str, ...]
    all_reason_codes: tuple[str, ...]
    input_candidate_hashes: tuple[str, ...]
    decision_payload: dict[str, JsonValue]
    decision_facts_as_of: datetime
    decision_evaluated_at: datetime
    decision_version: str = Field(pattern=r"^target-position-decision-v[0-9]+$")
    decision_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_semantic_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_version: str = Field(pattern=r"^campaign-target-position-fact-v[0-9]+$")
    record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment: str = Field(pattern=r"^SHADOW$")
    live_order_eligible: bool
    recorded_at: datetime

    @model_validator(mode="after")
    def fact_is_self_consistent(self) -> Self:
        if self.live_order_eligible:
            raise ValueError("Campaign target fact cannot be live-order eligible")
        if self.decision_facts_as_of > self.decision_evaluated_at:
            raise ValueError("Campaign target fact uses future decision facts")
        if self.decision_evaluated_at > self.recorded_at:
            raise ValueError("Campaign target fact predates its decision")
        decision = self.decision()
        if not _decision_matches_projection(self, decision):
            raise ValueError("Campaign target decision payload and columns diverged")
        if self.target_semantic_hash != target_semantic_hash(decision):
            raise ValueError("Campaign target semantic hash mismatch")
        if self.record_hash != hash_json(_record_hash_contract(self)):
            raise ValueError("Campaign target fact record hash mismatch")
        return self

    def decision(self) -> TargetPositionDecision:
        return TargetPositionDecision.model_validate(self.decision_payload)


def _decision_matches_projection(
    snapshot: CampaignTargetPositionFactSnapshot,
    decision: TargetPositionDecision,
) -> bool:
    return (
        snapshot.campaign_id == decision.campaign_id
        and snapshot.current_position_binding_hash == decision.current_position_binding_hash
        and snapshot.current_quantity == decision.current_quantity
        and snapshot.target_quantity == decision.target_quantity
        and snapshot.reduction_quantity == decision.reduction_quantity
        and snapshot.action == decision.action
        and snapshot.requires_order == decision.requires_order
        and snapshot.reduce_only_required == decision.reduce_only_required
        and snapshot.urgency == decision.urgency
        and snapshot.selected_target_source_refs == decision.selected_target_source_refs
        and snapshot.selected_urgency_source_refs == decision.selected_urgency_source_refs
        and snapshot.target_reason_codes == decision.target_reason_codes
        and snapshot.urgency_reason_codes == decision.urgency_reason_codes
        and snapshot.all_reason_codes == decision.all_reason_codes
        and snapshot.input_candidate_hashes == decision.input_candidate_hashes
        and snapshot.decision_facts_as_of == decision.facts_as_of
        and snapshot.decision_evaluated_at == decision.evaluated_at
        and snapshot.decision_version == decision.decision_version
        and snapshot.decision_hash == decision.decision_hash
    )


def _record_hash_contract(snapshot: CampaignTargetPositionFactSnapshot) -> dict[str, JsonValue]:
    return {
        "campaign_target_position_fact_id": str(snapshot.campaign_target_position_fact_id),
        "campaign_id": str(snapshot.campaign_id),
        "organization_id": snapshot.organization_id,
        "target_version": snapshot.target_version,
        "current_position_snapshot_id": str(snapshot.current_position_snapshot_id),
        "current_position_snapshot_hash": snapshot.current_position_snapshot_hash,
        "decision_payload": snapshot.decision_payload,
        "target_semantic_hash": snapshot.target_semantic_hash,
        "record_version": snapshot.record_version,
        "environment": snapshot.environment,
        "live_order_eligible": snapshot.live_order_eligible,
        "recorded_at": snapshot.recorded_at.astimezone(UTC).isoformat(),
    }


def target_semantic_hash(decision: TargetPositionDecision) -> str:
    return hash_json(
        {
            "campaign_id": str(decision.campaign_id),
            "current_position_binding_hash": decision.current_position_binding_hash,
            "current_quantity": str(decision.current_quantity),
            "target_quantity": str(decision.target_quantity),
            "reduction_quantity": str(decision.reduction_quantity),
            "action": decision.action,
            "requires_order": decision.requires_order,
            "reduce_only_required": decision.reduce_only_required,
            "urgency": decision.urgency,
            "selected_target_source_refs": list(decision.selected_target_source_refs),
            "selected_urgency_source_refs": list(decision.selected_urgency_source_refs),
            "target_reason_codes": list(decision.target_reason_codes),
            "urgency_reason_codes": list(decision.urgency_reason_codes),
            "all_reason_codes": list(decision.all_reason_codes),
        }
    )


def target_fact_from_record(
    record: CampaignTargetPositionFactRecord,
) -> CampaignTargetPositionFactSnapshot:
    return CampaignTargetPositionFactSnapshot.model_validate(record.contract())


class CampaignTargetPositionFactService:
    """Evaluates canonical Campaign target sources and appends a monotonic target fact."""

    command_type = "campaign.target-position.evaluate-record.v1"
    payload_schema_version = 1

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def evaluate_and_record(
        self,
        session: Session,
        envelope: CommandEnvelope,
    ) -> CommandOutcome:
        self._require_service(envelope)
        try:
            request = EvaluateRecordCampaignTargetRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected(
                "CAMPAIGN_TARGET_INPUT_INVALID",
                "Campaign target evaluation input is invalid",
            ) from exc
        if envelope.object_id != str(request.campaign_id):
            raise CommandRejected("OBJECT_BINDING_MISMATCH", "Campaign target identity changed")
        if envelope.scope.get("organization_id") != request.organization_id:
            raise CommandRejected("SCOPE_MISMATCH", "organization scope changed")

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
        campaign_state = session.execute(
            select(CampaignState)
            .where(CampaignState.campaign_id == request.campaign_id)
            .with_for_update()
        ).scalar_one()
        latest = session.execute(
            select(CampaignTargetPositionFactRecord)
            .where(CampaignTargetPositionFactRecord.campaign_id == request.campaign_id)
            .order_by(CampaignTargetPositionFactRecord.target_version.desc())
            .limit(1)
            .with_for_update()
        ).scalar_one_or_none()
        if latest is None:
            if envelope.expected_version is not None:
                raise CommandRejected("VERSION_CONFLICT", "Campaign target has no prior version")
            target_version = 1
            latest_snapshot = None
        else:
            latest_snapshot = target_fact_from_record(latest)
            if envelope.expected_version != latest_snapshot.target_version:
                raise CommandRejected("VERSION_CONFLICT", "Campaign target version changed")
            target_version = latest_snapshot.target_version + 1

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise CommandRejected(
                "CAMPAIGN_TARGET_CLOCK_INVALID",
                "Campaign target clock must be timezone-aware",
            )
        context = ProjectionQueryContext(as_of=now, max_age_ms=request.max_age_ms)
        protection = CampaignProtectionExitCandidateService.evaluate(
            session,
            request.campaign_id,
            context,
        )
        candidates = () if protection.candidate is None else (protection.candidate,)
        decision = CampaignTargetPositionEvaluationService.evaluate(
            session,
            request.campaign_id,
            candidates,
            context,
        )
        if (
            decision.current_position_binding_hash != protection.current_position_binding_hash
            or decision.current_quantity != protection.current_quantity
        ):
            raise RuntimeError("Campaign target sources changed inside one transaction")
        semantic_hash = target_semantic_hash(decision)
        if latest_snapshot is not None:
            if decision.target_quantity > latest_snapshot.target_quantity:
                CAMPAIGN_TARGET_FACT_RECORDINGS.labels("RELAXATION_REJECTED").inc()
                raise CommandRejected(
                    "CAMPAIGN_TARGET_POSITION_RELAXATION_FORBIDDEN",
                    "Campaign target cannot increase before the tighter target is reconciled",
                )
            if semantic_hash == latest_snapshot.target_semantic_hash:
                CAMPAIGN_TARGET_FACT_RECORDINGS.labels("NO_CHANGE").inc()
                return self._no_change_outcome(latest_snapshot)

        self._validate_campaign_state(campaign_state, decision)
        snapshot = self._snapshot(
            fact_id=envelope.command_id,
            organization_id=request.organization_id,
            target_version=target_version,
            position_snapshot_id=protection.current_position_snapshot_id,
            position_snapshot_hash=protection.current_position_snapshot_hash,
            decision=decision,
            semantic_hash=semantic_hash,
            recorded_at=now,
        )
        session.add(CampaignTargetPositionFactRecord(**snapshot.model_dump(mode="python")))
        if decision.action == "EXIT" and campaign_state.status == "OPEN":
            campaign_state.status = "CLOSING"
            campaign_state.version += 1
            campaign_state.reason_code = "TARGET_POSITION_ZERO_RECORDED"
            campaign_state.updated_at = now
        CAMPAIGN_TARGET_FACT_RECORDINGS.labels(decision.action).inc()
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="CampaignTargetPosition",
            object_id=str(request.campaign_id),
            object_version=target_version,
            data={
                "campaign_target_position_fact_id": str(snapshot.campaign_target_position_fact_id),
                "target_version": target_version,
                "action": decision.action,
                "target_quantity": str(decision.target_quantity),
                "target_semantic_hash": semantic_hash,
                "environment": "SHADOW",
                "live_order_eligible": False,
                "result": "RECORDED",
            },
            events=(
                DomainEvent(
                    event_type="CampaignTargetPositionRecorded",
                    aggregate_type="Campaign",
                    aggregate_id=str(request.campaign_id),
                    payload={
                        "campaign_target_position_fact_id": str(
                            snapshot.campaign_target_position_fact_id
                        ),
                        "target_version": target_version,
                        "action": decision.action,
                        "target_quantity": str(decision.target_quantity),
                        "target_semantic_hash": semantic_hash,
                        "environment": "SHADOW",
                        "live_order_eligible": False,
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
                "Campaign target payload schema version is unsupported",
            )
        if (
            envelope.channel is not CommandChannel.INTERNAL
            or envelope.service_principal != SERVICE_PRINCIPAL
            or envelope.object_type != "CampaignTargetPosition"
        ):
            raise CommandRejected(
                "CAMPAIGN_TARGET_SERVICE_REQUIRED",
                "only the Campaign target service may record target facts",
            )

    @staticmethod
    def _validate_campaign_state(
        campaign_state: CampaignState,
        decision: TargetPositionDecision,
    ) -> None:
        if decision.action == "EXIT":
            if campaign_state.status not in {"OPEN", "CLOSING"}:
                raise CommandRejected(
                    "CAMPAIGN_TARGET_STATE_INVALID",
                    "zero target requires an open or closing Campaign",
                )
        elif campaign_state.status != "OPEN":
            raise CommandRejected(
                "CAMPAIGN_TARGET_STATE_INVALID",
                "nonzero target requires an open Campaign",
            )

    @staticmethod
    def _snapshot(
        *,
        fact_id: UUID,
        organization_id: str,
        target_version: int,
        position_snapshot_id: UUID,
        position_snapshot_hash: str,
        decision: TargetPositionDecision,
        semantic_hash: str,
        recorded_at: datetime,
    ) -> CampaignTargetPositionFactSnapshot:
        draft = CampaignTargetPositionFactSnapshot.model_construct(
            campaign_target_position_fact_id=fact_id,
            campaign_id=decision.campaign_id,
            organization_id=organization_id,
            target_version=target_version,
            current_position_binding_hash=decision.current_position_binding_hash,
            current_position_snapshot_id=position_snapshot_id,
            current_position_snapshot_hash=position_snapshot_hash,
            current_quantity=decision.current_quantity,
            target_quantity=decision.target_quantity,
            reduction_quantity=decision.reduction_quantity,
            action=decision.action,
            requires_order=decision.requires_order,
            reduce_only_required=decision.reduce_only_required,
            urgency=decision.urgency,
            selected_target_source_refs=decision.selected_target_source_refs,
            selected_urgency_source_refs=decision.selected_urgency_source_refs,
            target_reason_codes=decision.target_reason_codes,
            urgency_reason_codes=decision.urgency_reason_codes,
            all_reason_codes=decision.all_reason_codes,
            input_candidate_hashes=decision.input_candidate_hashes,
            decision_payload=decision.model_dump(mode="json"),
            decision_facts_as_of=decision.facts_as_of,
            decision_evaluated_at=decision.evaluated_at,
            decision_version=decision.decision_version,
            decision_hash=decision.decision_hash,
            target_semantic_hash=semantic_hash,
            record_version=RECORD_VERSION,
            record_hash="0" * 64,
            environment="SHADOW",
            live_order_eligible=False,
            recorded_at=recorded_at,
        )
        return CampaignTargetPositionFactSnapshot.model_validate(
            {
                **draft.model_dump(mode="python"),
                "record_hash": hash_json(_record_hash_contract(draft)),
            }
        )

    @staticmethod
    def _no_change_outcome(
        latest: CampaignTargetPositionFactSnapshot,
    ) -> CommandOutcome:
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="CampaignTargetPosition",
            object_id=str(latest.campaign_id),
            object_version=latest.target_version,
            data={
                "campaign_target_position_fact_id": str(latest.campaign_target_position_fact_id),
                "target_version": latest.target_version,
                "action": latest.action,
                "target_quantity": str(latest.target_quantity),
                "target_semantic_hash": latest.target_semantic_hash,
                "environment": latest.environment,
                "live_order_eligible": latest.live_order_eligible,
                "result": "NO_CHANGE",
            },
        )
