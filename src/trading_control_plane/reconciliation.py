from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

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
    RECONCILIATION_FINDINGS,
    RECONCILIATION_INPUTS,
    RECONCILIATION_RUN_TRANSITIONS,
)
from trading_control_plane.reconciliation_models import (
    ExecutionReconciliationFinding,
    ExecutionReconciliationFindingResolution,
    ExecutionReconciliationInput,
    ExecutionReconciliationRun,
    ExecutionReconciliationRunState,
)
from trading_control_plane.sender_fencing import (
    SenderLeaseValidationRequest,
    SenderLeaseValidationResult,
    SenderLeaseValidator,
    SenderScopeBinding,
    sender_scope_id,
)
from trading_control_plane.sender_fencing_models import ExecutionSenderLease, ExecutionSenderScope
from trading_control_plane.venue_fact_models import VenueFactInputLink

RECONCILIATION_SERVICE_PRINCIPAL = "execution-reconciliation-service"


class ReconciliationSourceType(StrEnum):
    TRADING_LEDGER = "TRADING_LEDGER"
    VENUE_ORDERS = "VENUE_ORDERS"
    VENUE_FILLS = "VENUE_FILLS"
    VENUE_POSITIONS = "VENUE_POSITIONS"
    VENUE_BALANCES = "VENUE_BALANCES"
    VENUE_PROTECTION = "VENUE_PROTECTION"
    WORKER_LOCAL = "WORKER_LOCAL"


REQUIRED_RECONCILIATION_SOURCES: tuple[ReconciliationSourceType, ...] = tuple(
    ReconciliationSourceType
)


class ReconciliationTriggerType(StrEnum):
    STARTUP = "STARTUP"
    PRIVATE_STREAM_RECONNECT = "PRIVATE_STREAM_RECONNECT"
    ORDER_UNKNOWN = "ORDER_UNKNOWN"
    PARTIAL_FILL = "PARTIAL_FILL"
    CAMPAIGN_CLOSE = "CAMPAIGN_CLOSE"
    MANUAL_RECOVERY = "MANUAL_RECOVERY"


class ReconciliationCollectionStatus(StrEnum):
    COMPLETE = "COMPLETE"
    UNKNOWN = "UNKNOWN"


class ReconciliationPhase(StrEnum):
    COLLECTING = "COLLECTING"
    COMPARING = "COMPARING"
    ADJUSTING = "ADJUSTING"


class ReconciliationStatus(StrEnum):
    RUNNING = "RUNNING"
    UNKNOWN = "UNKNOWN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ReconciliationFindingCategory(StrEnum):
    MISSING_FACT = "MISSING_FACT"
    UNEXPLAINED_ORDER = "UNEXPLAINED_ORDER"
    POSITION_MISMATCH = "POSITION_MISMATCH"
    BALANCE_MISMATCH = "BALANCE_MISMATCH"
    PROTECTION_GAP = "PROTECTION_GAP"
    HEAT_MISMATCH = "HEAT_MISMATCH"
    WORKER_DRIFT = "WORKER_DRIFT"
    STALE_WATERMARK = "STALE_WATERMARK"
    OTHER = "OTHER"


class ReconciliationFindingSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    BLOCKING = "BLOCKING"
    UNKNOWN = "UNKNOWN"


class ReconciliationResolutionType(StrEnum):
    VENUE_FACT_CONFIRMED = "VENUE_FACT_CONFIRMED"
    TRADING_PROJECTION_CORRECTED = "TRADING_PROJECTION_CORRECTED"
    RISK_HELD = "RISK_HELD"
    NO_EXTERNAL_EFFECT_PROVED = "NO_EXTERNAL_EFFECT_PROVED"


class StartExecutionReconciliationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    scope: SenderScopeBinding
    lease_id: UUID
    fencing_token: int = Field(gt=0)
    trigger_type: ReconciliationTriggerType
    observation_window_start: datetime
    observation_window_end: datetime
    deadline_at: datetime
    supersedes_run_id: UUID | None = None
    reason_code: str = Field(min_length=3, max_length=160)
    source_ref: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_times(self) -> StartExecutionReconciliationRequest:
        times = (
            self.observation_window_start,
            self.observation_window_end,
            self.deadline_at,
        )
        if any(value.tzinfo is None for value in times):
            raise ValueError("reconciliation timestamps must be timezone-aware")
        if self.observation_window_start >= self.observation_window_end:
            raise ValueError("observation window must move forward")
        return self


class RecordReconciliationInputRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_id: UUID
    source_type: ReconciliationSourceType
    collection_status: ReconciliationCollectionStatus
    source_version: str = Field(min_length=1, max_length=160)
    watermark_type: str = Field(min_length=1, max_length=80)
    watermark_value: str = Field(min_length=1, max_length=255)
    observed_from: datetime
    observed_through: datetime
    observed_at: datetime
    item_count: int = Field(ge=0)
    payload_ref: str = Field(min_length=1, max_length=255)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ref: str = Field(min_length=1, max_length=255)
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason_code: str = Field(min_length=3, max_length=160)

    @model_validator(mode="after")
    def validate_times(self) -> RecordReconciliationInputRequest:
        times = (self.observed_from, self.observed_through, self.observed_at)
        if any(value.tzinfo is None for value in times):
            raise ValueError("input timestamps must be timezone-aware")
        if self.observed_from > self.observed_through:
            raise ValueError("input observation window cannot move backward")
        if self.observed_through > self.observed_at:
            raise ValueError("input watermark cannot be later than its observation")
        return self


class AdvanceReconciliationPhaseRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    target_phase: ReconciliationPhase
    reason_code: str = Field(min_length=3, max_length=160)
    source_ref: str = Field(min_length=1, max_length=255)


class RecordReconciliationFindingRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: UUID
    category: ReconciliationFindingCategory
    severity: ReconciliationFindingSeverity
    subject_type: str = Field(min_length=1, max_length=80)
    subject_id: str = Field(min_length=1, max_length=255)
    expected_snapshot: dict[str, Any]
    observed_snapshot: dict[str, Any]
    evidence_ref: str = Field(min_length=1, max_length=255)
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason_code: str = Field(min_length=3, max_length=160)


class ResolveReconciliationFindingRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    resolution_id: UUID
    finding_id: UUID
    resolution_type: ReconciliationResolutionType
    corrective_action_ref: str = Field(min_length=1, max_length=255)
    corrective_action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ref: str = Field(min_length=1, max_length=255)
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason_code: str = Field(min_length=3, max_length=160)


class FinishExecutionReconciliationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    target_status: ReconciliationStatus
    reason_code: str = Field(min_length=3, max_length=160)
    result_evidence_ref: str = Field(min_length=1, max_length=255)
    result_evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def terminal_only(self) -> FinishExecutionReconciliationRequest:
        if self.target_status is ReconciliationStatus.RUNNING:
            raise ValueError("finish requires a terminal status")
        return self


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _run_contract(run: ExecutionReconciliationRun) -> dict[str, Any]:
    return {
        "run_id": str(run.run_id),
        "organization_id": run.organization_id,
        "scope_id": run.scope_id,
        "lease_id": str(run.lease_id),
        "fencing_token": run.fencing_token,
        "trigger_type": run.trigger_type,
        "environment": run.environment,
        "live_dispatch_eligible": run.live_dispatch_eligible,
        "required_source_types": run.required_source_types,
        "observation_window_start": _iso(run.observation_window_start),
        "observation_window_end": _iso(run.observation_window_end),
        "supersedes_run_id": (
            str(run.supersedes_run_id) if run.supersedes_run_id is not None else None
        ),
        "initiated_by": run.initiated_by,
        "reason_code": run.reason_code,
        "source_ref": run.source_ref,
        "started_at": _iso(run.started_at),
        "deadline_at": _iso(run.deadline_at),
    }


class ExecutionReconciliationService:
    start_command_type = "execution.reconciliation.start-shadow.v1"
    input_command_type = "execution.reconciliation.record-input.v1"
    phase_command_type = "execution.reconciliation.advance-phase.v1"
    finding_command_type = "execution.reconciliation.record-finding.v1"
    resolve_command_type = "execution.reconciliation.resolve-finding.v1"
    finish_command_type = "execution.reconciliation.finish.v1"

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock

    def start(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        self._require_service(envelope, self.start_command_type)
        try:
            request = StartExecutionReconciliationRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("RECONCILIATION_INPUT_INVALID", str(exc)) from exc
        if envelope.object_id != str(request.run_id):
            raise CommandRejected("OBJECT_BINDING_MISMATCH", "reconciliation identity changed")
        if envelope.expected_version is not None:
            raise CommandRejected("VERSION_CONFLICT", "new reconciliation has no prior version")
        self._require_organization(envelope, request.scope.organization_id)
        scope_id = sender_scope_id(request.scope)
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": scope_id},
        )
        if session.get(ExecutionReconciliationRun, request.run_id) is not None:
            raise CommandRejected(
                "RECONCILIATION_RUN_ALREADY_EXISTS", "reconciliation identity already exists"
            )
        now = self._now(session)
        if request.deadline_at <= now:
            raise CommandRejected(
                "RECONCILIATION_DEADLINE_EXPIRED", "reconciliation deadline already elapsed"
            )
        lease_validation, lease = self._validate_current_lease(
            session,
            request.scope,
            request.lease_id,
            request.fencing_token,
            now,
        )
        if not lease_validation.valid:
            raise CommandRejected(
                lease_validation.reason_codes[0], "reconciliation requires current sender lease"
            )
        active_run = session.execute(
            select(ExecutionReconciliationRunState).where(
                ExecutionReconciliationRunState.scope_id == scope_id,
                ExecutionReconciliationRunState.status == ReconciliationStatus.RUNNING.value,
            )
        ).scalar_one_or_none()
        if active_run is not None:
            raise CommandRejected(
                "RECONCILIATION_ALREADY_RUNNING",
                "one reconciliation run already owns this sender scope",
            )
        latest_run = session.execute(
            select(ExecutionReconciliationRun)
            .where(ExecutionReconciliationRun.scope_id == scope_id)
            .order_by(
                ExecutionReconciliationRun.started_at.desc(),
                ExecutionReconciliationRun.run_id.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()
        if latest_run is None and request.supersedes_run_id is not None:
            raise CommandRejected(
                "RECONCILIATION_SUPERSEDES_INVALID",
                "first reconciliation run cannot supersede another run",
            )
        if latest_run is not None and request.supersedes_run_id != latest_run.run_id:
            raise CommandRejected(
                "RECONCILIATION_SUPERSEDES_REQUIRED",
                "new reconciliation must supersede the latest exact-scope run",
            )
        if request.supersedes_run_id is not None:
            previous = session.get(ExecutionReconciliationRun, request.supersedes_run_id)
            previous_state = session.get(ExecutionReconciliationRunState, request.supersedes_run_id)
            if (
                previous is None
                or previous_state is None
                or previous.scope_id != scope_id
                or previous_state.status == ReconciliationStatus.RUNNING.value
            ):
                raise CommandRejected(
                    "RECONCILIATION_SUPERSEDES_INVALID",
                    "superseded run must be terminal and share the exact sender scope",
                )
        required_sources = [source.value for source in REQUIRED_RECONCILIATION_SOURCES]
        run = ExecutionReconciliationRun(
            run_id=request.run_id,
            schema_version=1,
            organization_id=request.scope.organization_id,
            scope_id=scope_id,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            trigger_type=request.trigger_type.value,
            environment="SHADOW",
            live_dispatch_eligible=False,
            required_source_types=required_sources,
            observation_window_start=request.observation_window_start,
            observation_window_end=request.observation_window_end,
            supersedes_run_id=request.supersedes_run_id,
            initiated_by=RECONCILIATION_SERVICE_PRINCIPAL,
            reason_code=request.reason_code,
            source_ref=request.source_ref,
            started_at=now,
            deadline_at=request.deadline_at,
            run_hash="0" * 64,
        )
        run.run_hash = hash_json(_run_contract(run))
        state = ExecutionReconciliationRunState(
            run_id=run.run_id,
            organization_id=run.organization_id,
            scope_id=run.scope_id,
            status=ReconciliationStatus.RUNNING.value,
            phase=ReconciliationPhase.COLLECTING.value,
            version=1,
            collected_source_count=0,
            finding_count=0,
            unresolved_blocking_count=0,
            reason_code=request.reason_code,
            source_ref=request.source_ref,
            result_snapshot=None,
            result_hash=None,
            updated_at=now,
            completed_at=None,
        )
        session.add_all((run, state))
        session.flush()
        RECONCILIATION_RUN_TRANSITIONS.labels("NONE", "RUNNING", "COLLECTING").inc()
        return self._outcome(
            run,
            state,
            "ExecutionReconciliationStarted",
            {"trigger_type": run.trigger_type, "required_source_count": len(required_sources)},
        )

    def record_input(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        run, state = self._load_running(session, envelope, self.input_command_type)
        try:
            request = RecordReconciliationInputRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("RECONCILIATION_INPUT_INVALID", str(exc)) from exc
        if state.phase != ReconciliationPhase.COLLECTING.value:
            raise CommandRejected(
                "RECONCILIATION_PHASE_INVALID", "inputs are frozen after collection"
            )
        if request.source_type.value not in run.required_source_types:
            raise CommandRejected(
                "RECONCILIATION_SOURCE_NOT_REQUIRED", "input source is outside frozen manifest"
            )
        if (
            request.observed_from > run.observation_window_start
            or request.observed_through < run.observation_window_end
        ):
            raise CommandRejected(
                "RECONCILIATION_WATERMARK_INCOMPLETE",
                "input watermark does not cover the frozen observation window",
            )
        now = self._now(session)
        if request.observed_at > now:
            raise CommandRejected(
                "RECONCILIATION_FUTURE_OBSERVATION", "input observation is in the future"
            )
        existing = session.execute(
            select(ExecutionReconciliationInput).where(
                ExecutionReconciliationInput.run_id == run.run_id,
                ExecutionReconciliationInput.source_type == request.source_type.value,
            )
        ).scalar_one_or_none()
        existing_identity = session.get(ExecutionReconciliationInput, request.input_id)
        if existing is not None or existing_identity is not None:
            raise CommandRejected(
                "RECONCILIATION_INPUT_ALREADY_RECORDED",
                "source snapshot or input identity already exists",
            )
        input_values = {
            "input_id": str(request.input_id),
            "run_id": str(run.run_id),
            "organization_id": run.organization_id,
            "source_type": request.source_type.value,
            "collection_status": request.collection_status.value,
            "source_version": request.source_version,
            "watermark_type": request.watermark_type,
            "watermark_value": request.watermark_value,
            "observed_from": _iso(request.observed_from),
            "observed_through": _iso(request.observed_through),
            "observed_at": _iso(request.observed_at),
            "received_at": _iso(now),
            "item_count": request.item_count,
            "payload_ref": request.payload_ref,
            "payload_hash": request.payload_hash,
            "evidence_ref": request.evidence_ref,
            "evidence_hash": request.evidence_hash,
        }
        snapshot = ExecutionReconciliationInput(
            input_id=request.input_id,
            run_id=run.run_id,
            organization_id=run.organization_id,
            source_type=request.source_type.value,
            collection_status=request.collection_status.value,
            source_version=request.source_version,
            watermark_type=request.watermark_type,
            watermark_value=request.watermark_value,
            observed_from=request.observed_from,
            observed_through=request.observed_through,
            observed_at=request.observed_at,
            received_at=now,
            item_count=request.item_count,
            payload_ref=request.payload_ref,
            payload_hash=request.payload_hash,
            evidence_ref=request.evidence_ref,
            evidence_hash=request.evidence_hash,
            input_hash=hash_json(input_values),
        )
        session.add(snapshot)
        state.version += 1
        state.collected_source_count += 1
        state.reason_code = request.reason_code
        state.source_ref = request.evidence_ref
        state.updated_at = now
        session.flush()
        RECONCILIATION_INPUTS.labels(
            request.source_type.value, request.collection_status.value
        ).inc()
        return self._outcome(
            run,
            state,
            "ExecutionReconciliationInputRecorded",
            {
                "input_id": str(snapshot.input_id),
                "source_type": snapshot.source_type,
                "collection_status": snapshot.collection_status,
                "input_hash": snapshot.input_hash,
            },
        )

    def advance_phase(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        run, state = self._load_running(session, envelope, self.phase_command_type)
        try:
            request = AdvanceReconciliationPhaseRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("RECONCILIATION_INPUT_INVALID", str(exc)) from exc
        current_phase = ReconciliationPhase(state.phase)
        allowed = {
            ReconciliationPhase.COLLECTING: ReconciliationPhase.COMPARING,
            ReconciliationPhase.COMPARING: ReconciliationPhase.ADJUSTING,
        }
        if allowed.get(current_phase) is not request.target_phase:
            raise CommandRejected(
                "RECONCILIATION_PHASE_INVALID", "phase must advance exactly one step"
            )
        inputs = self._inputs(session, run.run_id)
        if request.target_phase is ReconciliationPhase.COMPARING:
            self._require_complete_manifest(session, run, inputs)
        now = self._now(session)
        if now >= run.deadline_at:
            raise CommandRejected("RECONCILIATION_DEADLINE_EXPIRED", "expired run cannot advance")
        state.phase = request.target_phase.value
        state.version += 1
        state.reason_code = request.reason_code
        state.source_ref = request.source_ref
        state.updated_at = now
        session.flush()
        RECONCILIATION_RUN_TRANSITIONS.labels(
            "RUNNING", "RUNNING", request.target_phase.value
        ).inc()
        return self._outcome(
            run,
            state,
            "ExecutionReconciliationPhaseAdvanced",
            {"from_phase": current_phase.value, "to_phase": request.target_phase.value},
        )

    def record_finding(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        run, state = self._load_running(session, envelope, self.finding_command_type)
        try:
            request = RecordReconciliationFindingRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("RECONCILIATION_FINDING_INVALID", str(exc)) from exc
        if state.phase not in {
            ReconciliationPhase.COMPARING.value,
            ReconciliationPhase.ADJUSTING.value,
        }:
            raise CommandRejected(
                "RECONCILIATION_PHASE_INVALID", "findings require comparison to have started"
            )
        if session.get(ExecutionReconciliationFinding, request.finding_id) is not None:
            raise CommandRejected(
                "RECONCILIATION_FINDING_ALREADY_EXISTS", "finding identity already exists"
            )
        now = self._now(session)
        expected_hash = hash_json(request.expected_snapshot)
        observed_hash = hash_json(request.observed_snapshot)
        finding_sequence = state.finding_count + 1
        finding_values = {
            "finding_id": str(request.finding_id),
            "run_id": str(run.run_id),
            "organization_id": run.organization_id,
            "finding_sequence": finding_sequence,
            "category": request.category.value,
            "severity": request.severity.value,
            "subject_type": request.subject_type,
            "subject_id": request.subject_id,
            "expected_hash": expected_hash,
            "observed_hash": observed_hash,
            "evidence_ref": request.evidence_ref,
            "evidence_hash": request.evidence_hash,
            "created_at": _iso(now),
        }
        finding = ExecutionReconciliationFinding(
            finding_id=request.finding_id,
            run_id=run.run_id,
            organization_id=run.organization_id,
            finding_sequence=finding_sequence,
            category=request.category.value,
            severity=request.severity.value,
            subject_type=request.subject_type,
            subject_id=request.subject_id,
            expected_snapshot=request.expected_snapshot,
            expected_hash=expected_hash,
            observed_snapshot=request.observed_snapshot,
            observed_hash=observed_hash,
            evidence_ref=request.evidence_ref,
            evidence_hash=request.evidence_hash,
            finding_hash=hash_json(finding_values),
            created_at=now,
        )
        session.add(finding)
        state.version += 1
        state.finding_count += 1
        if request.severity in {
            ReconciliationFindingSeverity.BLOCKING,
            ReconciliationFindingSeverity.UNKNOWN,
        }:
            state.unresolved_blocking_count += 1
        state.reason_code = request.reason_code
        state.source_ref = request.evidence_ref
        state.updated_at = now
        session.flush()
        RECONCILIATION_FINDINGS.labels(request.severity.value, "OPEN").inc()
        return self._outcome(
            run,
            state,
            "ExecutionReconciliationFindingRecorded",
            {
                "finding_id": str(finding.finding_id),
                "finding_sequence": finding.finding_sequence,
                "severity": finding.severity,
                "finding_hash": finding.finding_hash,
            },
        )

    def resolve_finding(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        run, state = self._load_running(session, envelope, self.resolve_command_type)
        try:
            request = ResolveReconciliationFindingRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("RECONCILIATION_RESOLUTION_INVALID", str(exc)) from exc
        if state.phase != ReconciliationPhase.ADJUSTING.value:
            raise CommandRejected(
                "RECONCILIATION_PHASE_INVALID", "finding closure requires ADJUSTING phase"
            )
        finding = session.execute(
            select(ExecutionReconciliationFinding).where(
                ExecutionReconciliationFinding.finding_id == request.finding_id,
                ExecutionReconciliationFinding.run_id == run.run_id,
            )
        ).scalar_one_or_none()
        if finding is None:
            raise CommandRejected(
                "RECONCILIATION_FINDING_NOT_FOUND", "finding is unavailable in this run"
            )
        existing = session.execute(
            select(ExecutionReconciliationFindingResolution).where(
                ExecutionReconciliationFindingResolution.finding_id == finding.finding_id
            )
        ).scalar_one_or_none()
        existing_identity = session.get(
            ExecutionReconciliationFindingResolution, request.resolution_id
        )
        if existing is not None or existing_identity is not None:
            raise CommandRejected(
                "RECONCILIATION_FINDING_ALREADY_RESOLVED",
                "finding or resolution identity already has a closure",
            )
        now = self._now(session)
        resolution_values = {
            "resolution_id": str(request.resolution_id),
            "finding_id": str(finding.finding_id),
            "run_id": str(run.run_id),
            "organization_id": run.organization_id,
            "disposition": "RESOLVED_SAFE",
            "resolution_type": request.resolution_type.value,
            "corrective_action_ref": request.corrective_action_ref,
            "corrective_action_hash": request.corrective_action_hash,
            "evidence_ref": request.evidence_ref,
            "evidence_hash": request.evidence_hash,
            "resolved_by": RECONCILIATION_SERVICE_PRINCIPAL,
            "resolved_at": _iso(now),
        }
        resolution = ExecutionReconciliationFindingResolution(
            resolution_id=request.resolution_id,
            finding_id=finding.finding_id,
            run_id=run.run_id,
            organization_id=run.organization_id,
            disposition="RESOLVED_SAFE",
            resolution_type=request.resolution_type.value,
            corrective_action_ref=request.corrective_action_ref,
            corrective_action_hash=request.corrective_action_hash,
            evidence_ref=request.evidence_ref,
            evidence_hash=request.evidence_hash,
            resolved_by=RECONCILIATION_SERVICE_PRINCIPAL,
            resolved_at=now,
            resolution_hash=hash_json(resolution_values),
        )
        session.add(resolution)
        state.version += 1
        if finding.severity in {
            ReconciliationFindingSeverity.BLOCKING.value,
            ReconciliationFindingSeverity.UNKNOWN.value,
        }:
            state.unresolved_blocking_count -= 1
        state.reason_code = request.reason_code
        state.source_ref = request.evidence_ref
        state.updated_at = now
        session.flush()
        RECONCILIATION_FINDINGS.labels(finding.severity, "RESOLVED_SAFE").inc()
        return self._outcome(
            run,
            state,
            "ExecutionReconciliationFindingResolved",
            {
                "finding_id": str(finding.finding_id),
                "resolution_id": str(resolution.resolution_id),
                "resolution_hash": resolution.resolution_hash,
            },
        )

    def finish(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        run, state = self._load_running(session, envelope, self.finish_command_type)
        try:
            request = FinishExecutionReconciliationRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("RECONCILIATION_FINISH_INVALID", str(exc)) from exc
        now = self._now(session)
        inputs = self._inputs(session, run.run_id)
        findings = self._findings(session, run.run_id)
        resolutions = self._resolutions(session, run.run_id)
        if request.target_status is ReconciliationStatus.SUCCEEDED:
            if state.phase not in {
                ReconciliationPhase.COMPARING.value,
                ReconciliationPhase.ADJUSTING.value,
            }:
                raise CommandRejected(
                    "RECONCILIATION_PHASE_INVALID", "success requires completed comparison"
                )
            self._require_complete_manifest(session, run, inputs)
            if state.unresolved_blocking_count != 0:
                raise CommandRejected(
                    "RECONCILIATION_BLOCKING_FINDINGS_OPEN",
                    "blocking or unknown findings remain unresolved",
                )
            if now >= run.deadline_at:
                raise CommandRejected(
                    "RECONCILIATION_DEADLINE_EXPIRED", "expired run cannot succeed"
                )
            lease_validation, _ = self._validate_current_lease(
                session,
                self._scope_binding(session, run.scope_id),
                run.lease_id,
                run.fencing_token,
                now,
            )
            if not lease_validation.valid:
                raise CommandRejected(
                    lease_validation.reason_codes[0],
                    "successful reconciliation requires the same current sender lease",
                )
        result_snapshot = self._result_snapshot(
            run,
            state,
            request,
            inputs,
            findings,
            resolutions,
            now,
        )
        prior_status = state.status
        state.status = request.target_status.value
        state.version += 1
        state.reason_code = request.reason_code
        state.source_ref = request.result_evidence_ref
        state.result_snapshot = result_snapshot
        state.result_hash = hash_json(result_snapshot)
        state.updated_at = now
        state.completed_at = now
        session.flush()
        RECONCILIATION_RUN_TRANSITIONS.labels(prior_status, state.status, state.phase).inc()
        return self._outcome(
            run,
            state,
            "ExecutionReconciliationFinished",
            {
                "status": state.status,
                "result_hash": state.result_hash,
                "external_send_permitted": False,
            },
        )

    def _load_running(
        self,
        session: Session,
        envelope: CommandEnvelope,
        command_type: str,
    ) -> tuple[ExecutionReconciliationRun, ExecutionReconciliationRunState]:
        self._require_service(envelope, command_type)
        try:
            run_id = UUID(cast(str, envelope.object_id))
        except ValueError as exc:
            raise CommandRejected(
                "OBJECT_BINDING_MISMATCH", "reconciliation identity is invalid"
            ) from exc
        state = session.execute(
            select(ExecutionReconciliationRunState)
            .where(ExecutionReconciliationRunState.run_id == run_id)
            .with_for_update()
        ).scalar_one_or_none()
        run = session.get(ExecutionReconciliationRun, run_id)
        if run is None or state is None:
            raise CommandRejected(
                "RECONCILIATION_RUN_NOT_FOUND", "reconciliation run is unavailable"
            )
        self._require_organization(envelope, run.organization_id)
        if run.run_hash != hash_json(_run_contract(run)):
            raise CommandRejected(
                "RECONCILIATION_RUN_INTEGRITY_FAILED", "frozen run contract drifted"
            )
        if envelope.expected_version != state.version:
            raise CommandRejected("VERSION_CONFLICT", "reconciliation state version changed")
        if state.status != ReconciliationStatus.RUNNING.value:
            raise CommandRejected(
                "RECONCILIATION_RUN_TERMINAL", "terminal reconciliation cannot be changed"
            )
        return run, state

    @staticmethod
    def _inputs(session: Session, run_id: UUID) -> list[ExecutionReconciliationInput]:
        return list(
            session.scalars(
                select(ExecutionReconciliationInput)
                .where(ExecutionReconciliationInput.run_id == run_id)
                .order_by(ExecutionReconciliationInput.source_type)
            )
        )

    @staticmethod
    def _findings(session: Session, run_id: UUID) -> list[ExecutionReconciliationFinding]:
        return list(
            session.scalars(
                select(ExecutionReconciliationFinding)
                .where(ExecutionReconciliationFinding.run_id == run_id)
                .order_by(ExecutionReconciliationFinding.finding_sequence)
            )
        )

    @staticmethod
    def _resolutions(
        session: Session, run_id: UUID
    ) -> list[ExecutionReconciliationFindingResolution]:
        return list(
            session.scalars(
                select(ExecutionReconciliationFindingResolution).where(
                    ExecutionReconciliationFindingResolution.run_id == run_id
                )
            )
        )

    @staticmethod
    def _require_complete_manifest(
        session: Session,
        run: ExecutionReconciliationRun,
        inputs: list[ExecutionReconciliationInput],
    ) -> None:
        observed = {item.source_type for item in inputs}
        required = set(run.required_source_types)
        if observed != required or any(
            item.collection_status != ReconciliationCollectionStatus.COMPLETE.value
            for item in inputs
        ):
            raise CommandRejected(
                "RECONCILIATION_INPUTS_INCOMPLETE",
                "every frozen source requires one complete watermark snapshot",
            )
        normalized_sources = {
            ReconciliationSourceType.VENUE_ORDERS.value,
            ReconciliationSourceType.VENUE_FILLS.value,
            ReconciliationSourceType.VENUE_POSITIONS.value,
        }
        linked_counts: dict[str, int] = {
            source_type: count
            for source_type, count in session.execute(
                select(VenueFactInputLink.source_type, func.count())
                .where(VenueFactInputLink.run_id == run.run_id)
                .group_by(VenueFactInputLink.source_type)
            ).tuples()
        }
        if any(
            item.source_type in normalized_sources
            and linked_counts.get(item.source_type, 0) != item.item_count
            for item in inputs
        ):
            raise CommandRejected(
                "RECONCILIATION_NORMALIZED_FACT_COUNT_MISMATCH",
                "venue order, fill, and position inputs require exact immutable fact membership",
            )

    @staticmethod
    def _result_snapshot(
        run: ExecutionReconciliationRun,
        state: ExecutionReconciliationRunState,
        request: FinishExecutionReconciliationRequest,
        inputs: list[ExecutionReconciliationInput],
        findings: list[ExecutionReconciliationFinding],
        resolutions: list[ExecutionReconciliationFindingResolution],
        completed_at: datetime,
    ) -> dict[str, Any]:
        resolution_by_finding = {resolution.finding_id: resolution for resolution in resolutions}
        return {
            "run_id": str(run.run_id),
            "scope_id": run.scope_id,
            "lease_id": str(run.lease_id),
            "fencing_token": run.fencing_token,
            "status": request.target_status.value,
            "phase": state.phase,
            "input_count": len(inputs),
            "inputs": [
                {
                    "source_type": item.source_type,
                    "collection_status": item.collection_status,
                    "source_version": item.source_version,
                    "watermark_type": item.watermark_type,
                    "watermark_value": item.watermark_value,
                    "observed_through": _iso(item.observed_through),
                    "input_hash": item.input_hash,
                }
                for item in inputs
            ],
            "finding_count": len(findings),
            "unresolved_blocking_count": state.unresolved_blocking_count,
            "findings": [
                {
                    "finding_id": str(finding.finding_id),
                    "severity": finding.severity,
                    "finding_hash": finding.finding_hash,
                    "resolution_hash": (
                        resolution_by_finding[finding.finding_id].resolution_hash
                        if finding.finding_id in resolution_by_finding
                        else None
                    ),
                }
                for finding in findings
            ],
            "result_evidence_ref": request.result_evidence_ref,
            "result_evidence_hash": request.result_evidence_hash,
            "no_historical_replay": True,
            "external_send_permitted": False,
            "completed_at": _iso(completed_at),
        }

    @staticmethod
    def _scope_binding(session: Session, scope_id: str) -> SenderScopeBinding:
        scope = session.get(ExecutionSenderScope, scope_id)
        if scope is None:
            raise CommandRejected("SENDER_SCOPE_NOT_FOUND", "sender scope is unavailable")
        return SenderScopeBinding(
            organization_id=scope.organization_id,
            venue=scope.venue,
            execution_domain=scope.execution_domain,
            account_id=scope.account_id,
            account_abstraction=scope.account_abstraction,
            position_mode=scope.position_mode,
            margin_mode=scope.margin_mode,
            collateral_scope=scope.collateral_scope,
            collateral_pool_id=scope.collateral_pool_id,
        )

    @staticmethod
    def _validate_current_lease(
        session: Session,
        scope: SenderScopeBinding,
        lease_id: UUID,
        fencing_token: int,
        now: datetime,
    ) -> tuple[SenderLeaseValidationResult, ExecutionSenderLease]:
        lease = session.get(ExecutionSenderLease, lease_id)
        if lease is None:
            raise CommandRejected("SENDER_LEASE_NOT_FOUND", "sender lease is unavailable")
        validation = SenderLeaseValidator().validate(
            session,
            SenderLeaseValidationRequest(
                scope=scope,
                lease_id=lease_id,
                fencing_token=fencing_token,
                owner_worker_id=lease.owner_worker_id,
                worker_config_hash=lease.worker_config_hash,
                credential_fingerprint=lease.credential_fingerprint,
                validation_time=now,
            ),
        )
        return validation, lease

    @staticmethod
    def _outcome(
        run: ExecutionReconciliationRun,
        state: ExecutionReconciliationRunState,
        event_type: str,
        details: dict[str, Any],
    ) -> CommandOutcome:
        data = {
            "run_id": str(run.run_id),
            "scope_id": run.scope_id,
            "lease_id": str(run.lease_id),
            "fencing_token": run.fencing_token,
            "status": state.status,
            "phase": state.phase,
            "collected_source_count": state.collected_source_count,
            "finding_count": state.finding_count,
            "unresolved_blocking_count": state.unresolved_blocking_count,
            "result_hash": state.result_hash,
            "environment": "SHADOW",
            "live_dispatch_eligible": False,
            **details,
        }
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="ExecutionReconciliationRun",
            object_id=str(run.run_id),
            object_version=state.version,
            data=data,
            events=(
                DomainEvent(
                    event_type=event_type,
                    aggregate_type="ExecutionReconciliationRun",
                    aggregate_id=str(run.run_id),
                    payload=data,
                ),
            ),
        )

    @staticmethod
    def _require_service(envelope: CommandEnvelope, command_type: str) -> None:
        if envelope.command_type != command_type:
            raise CommandRejected("COMMAND_TYPE_MISMATCH", "unexpected command type")
        if envelope.object_type != "ExecutionReconciliationRun" or envelope.object_id is None:
            raise CommandRejected(
                "OBJECT_BINDING_MISMATCH", "ExecutionReconciliationRun binding is required"
            )
        if (
            envelope.channel is not CommandChannel.INTERNAL
            or envelope.service_principal != RECONCILIATION_SERVICE_PRINCIPAL
        ):
            raise CommandRejected(
                "RECONCILIATION_SERVICE_REQUIRED",
                "reconciliation service principal is required",
            )

    @staticmethod
    def _require_organization(envelope: CommandEnvelope, organization_id: str) -> None:
        if envelope.scope.get("organization_id") != organization_id:
            raise CommandRejected("SCOPE_MISMATCH", "organization scope changed")

    def _now(self, session: Session) -> datetime:
        if self._clock is not None:
            return self._clock()
        return cast(datetime, session.execute(select(func.clock_timestamp())).scalar_one())
