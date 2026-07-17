from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import CommandChannel, CommandEnvelope, CommandResult, hash_json
from trading_control_plane.database import Database
from trading_control_plane.reconciliation import (
    RECONCILIATION_SERVICE_PRINCIPAL,
    REQUIRED_RECONCILIATION_SOURCES,
    AdvanceReconciliationPhaseRequest,
    ExecutionReconciliationService,
    FinishExecutionReconciliationRequest,
    ReconciliationCollectionStatus,
    ReconciliationFindingCategory,
    ReconciliationFindingSeverity,
    ReconciliationPhase,
    ReconciliationResolutionType,
    ReconciliationSourceType,
    ReconciliationStatus,
    ReconciliationTriggerType,
    RecordReconciliationFindingRequest,
    RecordReconciliationInputRequest,
    ResolveReconciliationFindingRequest,
    StartExecutionReconciliationRequest,
)
from trading_control_plane.sender_fencing import SenderScopeBinding


def reconciliation_envelope(
    run_id: UUID,
    command_type: str,
    payload: dict[str, object],
    *,
    now: datetime,
    expected_version: int | None,
    idempotency_key: str | None = None,
) -> CommandEnvelope:
    return CommandEnvelope(
        idempotency_key=idempotency_key or f"reconciliation-{uuid4()}",
        command_type=command_type,
        object_type="ExecutionReconciliationRun",
        object_id=str(run_id),
        expected_version=expected_version,
        service_principal=RECONCILIATION_SERVICE_PRINCIPAL,
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": "org-1"},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref="test-only:reconciliation-service",
        payload_schema_version=1,
        reason="record durable shadow reconciliation evidence",
        payload=payload,
    )


def start_envelope(
    run_id: UUID,
    scope: SenderScopeBinding,
    lease_id: UUID,
    fencing_token: int,
    *,
    now: datetime,
    trigger_type: ReconciliationTriggerType = ReconciliationTriggerType.STARTUP,
    supersedes_run_id: UUID | None = None,
    idempotency_key: str | None = None,
) -> CommandEnvelope:
    request = StartExecutionReconciliationRequest(
        run_id=run_id,
        scope=scope,
        lease_id=lease_id,
        fencing_token=fencing_token,
        trigger_type=trigger_type,
        observation_window_start=now - timedelta(minutes=5),
        observation_window_end=now,
        deadline_at=now + timedelta(seconds=20),
        supersedes_run_id=supersedes_run_id,
        reason_code="TEST_RECONCILIATION_START",
        source_ref=f"test-only:reconciliation-start:{run_id}",
    )
    return reconciliation_envelope(
        run_id,
        ExecutionReconciliationService.start_command_type,
        request.model_dump(mode="json"),
        now=now,
        expected_version=None,
        idempotency_key=idempotency_key,
    )


def input_envelope(
    run_id: UUID,
    source_type: ReconciliationSourceType,
    *,
    now: datetime,
    expected_version: int,
    status: ReconciliationCollectionStatus = ReconciliationCollectionStatus.COMPLETE,
    observed_from: datetime | None = None,
    observed_through: datetime | None = None,
    item_count: int = 0,
) -> CommandEnvelope:
    input_id = uuid4()
    request = RecordReconciliationInputRequest(
        input_id=input_id,
        source_type=source_type,
        collection_status=status,
        source_version="test-adapter-v1",
        watermark_type="SOURCE_SEQUENCE",
        watermark_value=f"{source_type.value}:100",
        observed_from=observed_from or now - timedelta(minutes=5),
        observed_through=observed_through or now,
        observed_at=now,
        item_count=item_count,
        payload_ref=f"test-only:payload:{input_id}",
        payload_hash=hash_json({"source": source_type.value, "items": []}),
        evidence_ref=f"test-only:evidence:{input_id}",
        evidence_hash=hash_json({"source": source_type.value, "complete": status.value}),
        reason_code="TEST_RECONCILIATION_INPUT",
    )
    return reconciliation_envelope(
        run_id,
        ExecutionReconciliationService.input_command_type,
        request.model_dump(mode="json"),
        now=now,
        expected_version=expected_version,
    )


def phase_envelope(
    run_id: UUID,
    target_phase: ReconciliationPhase,
    *,
    now: datetime,
    expected_version: int,
) -> CommandEnvelope:
    request = AdvanceReconciliationPhaseRequest(
        target_phase=target_phase,
        reason_code=f"TEST_RECONCILIATION_{target_phase.value}",
        source_ref=f"test-only:phase:{target_phase.value.lower()}",
    )
    return reconciliation_envelope(
        run_id,
        ExecutionReconciliationService.phase_command_type,
        request.model_dump(mode="json"),
        now=now,
        expected_version=expected_version,
    )


def finding_envelope(
    run_id: UUID,
    *,
    now: datetime,
    expected_version: int,
    severity: ReconciliationFindingSeverity = ReconciliationFindingSeverity.BLOCKING,
) -> tuple[CommandEnvelope, UUID]:
    finding_id = uuid4()
    request = RecordReconciliationFindingRequest(
        finding_id=finding_id,
        category=ReconciliationFindingCategory.POSITION_MISMATCH,
        severity=severity,
        subject_type="POSITION",
        subject_id="BTCUSDT:LONG",
        expected_snapshot={"quantity": "0"},
        observed_snapshot={"quantity": "1"},
        evidence_ref=f"test-only:finding:{finding_id}",
        evidence_hash=hash_json({"finding_id": str(finding_id)}),
        reason_code="TEST_RECONCILIATION_FINDING",
    )
    return (
        reconciliation_envelope(
            run_id,
            ExecutionReconciliationService.finding_command_type,
            request.model_dump(mode="json"),
            now=now,
            expected_version=expected_version,
        ),
        finding_id,
    )


def resolution_envelope(
    run_id: UUID,
    finding_id: UUID,
    *,
    now: datetime,
    expected_version: int,
) -> CommandEnvelope:
    resolution_id = uuid4()
    request = ResolveReconciliationFindingRequest(
        resolution_id=resolution_id,
        finding_id=finding_id,
        resolution_type=ReconciliationResolutionType.RISK_HELD,
        corrective_action_ref=f"test-only:risk-hold:{finding_id}",
        corrective_action_hash=hash_json({"risk_held": True}),
        evidence_ref=f"test-only:resolution:{resolution_id}",
        evidence_hash=hash_json({"resolution_id": str(resolution_id)}),
        reason_code="TEST_RECONCILIATION_RESOLUTION",
    )
    return reconciliation_envelope(
        run_id,
        ExecutionReconciliationService.resolve_command_type,
        request.model_dump(mode="json"),
        now=now,
        expected_version=expected_version,
    )


def finish_envelope(
    run_id: UUID,
    target_status: ReconciliationStatus,
    *,
    now: datetime,
    expected_version: int,
) -> CommandEnvelope:
    request = FinishExecutionReconciliationRequest(
        target_status=target_status,
        reason_code=f"TEST_RECONCILIATION_{target_status.value}",
        result_evidence_ref=f"test-only:result:{run_id}:{target_status.value}",
        result_evidence_hash=hash_json({"run_id": str(run_id), "status": target_status.value}),
    )
    return reconciliation_envelope(
        run_id,
        ExecutionReconciliationService.finish_command_type,
        request.model_dump(mode="json"),
        now=now,
        expected_version=expected_version,
    )


def execute_reconciliation(
    database: Database,
    envelope: CommandEnvelope,
    *,
    now: datetime,
) -> CommandResult:
    service = ExecutionReconciliationService(clock=lambda: now)
    handlers = {
        service.start_command_type: service.start,
        service.input_command_type: service.record_input,
        service.phase_command_type: service.advance_phase,
        service.finding_command_type: service.record_finding,
        service.resolve_command_type: service.resolve_finding,
        service.finish_command_type: service.finish,
    }
    return IdempotentCommandExecutor(database.session_factory).execute(
        envelope, handlers[envelope.command_type]
    )


def collect_complete_inputs(
    database: Database,
    run_id: UUID,
    *,
    now: datetime,
    first_version: int = 1,
    item_counts: dict[ReconciliationSourceType, int] | None = None,
) -> int:
    version = first_version
    for source_type in REQUIRED_RECONCILIATION_SOURCES:
        result = execute_reconciliation(
            database,
            input_envelope(
                run_id,
                source_type,
                now=now,
                expected_version=version,
                item_count=(item_counts or {}).get(source_type, 0),
            ),
            now=now,
        )
        assert result.object_version == version + 1
        version += 1
    return version


def complete_successful_reconciliation(
    database: Database,
    scope: SenderScopeBinding,
    lease_id: UUID,
    fencing_token: int,
    *,
    now: datetime,
    supersedes_run_id: UUID | None = None,
) -> UUID:
    run_id = uuid4()
    started = execute_reconciliation(
        database,
        start_envelope(
            run_id,
            scope,
            lease_id,
            fencing_token,
            now=now,
            supersedes_run_id=supersedes_run_id,
        ),
        now=now,
    )
    assert started.object_version == 1
    version = collect_complete_inputs(database, run_id, now=now)
    compared = execute_reconciliation(
        database,
        phase_envelope(run_id, ReconciliationPhase.COMPARING, now=now, expected_version=version),
        now=now,
    )
    assert compared.object_version == version + 1
    finished = execute_reconciliation(
        database,
        finish_envelope(
            run_id,
            ReconciliationStatus.SUCCEEDED,
            now=now,
            expected_version=version + 1,
        ),
        now=now,
    )
    assert finished.status.value == "COMPLETED"
    return run_id
