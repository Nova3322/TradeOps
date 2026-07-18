from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from tests.integration.test_sender_fencing import prepare_shadow_intent
from tests.reconciliation_fixtures import (
    collect_complete_inputs,
    complete_successful_reconciliation,
    execute_reconciliation,
    finding_envelope,
    finish_envelope,
    input_envelope,
    phase_envelope,
    resolution_envelope,
    start_envelope,
)
from tests.sender_fencing_fixtures import (
    WORKER_ID,
    acquire_envelope,
    claim_envelope,
    execute_acquire,
    execute_claim,
    execute_tighten,
    make_sender_scope,
    tighten_envelope,
)
from trading_control_plane.capability_certificate_models import CapabilityCertificate
from trading_control_plane.commands import CommandStatus
from trading_control_plane.database import Database
from trading_control_plane.reconciliation import (
    ReconciliationCollectionStatus,
    ReconciliationPhase,
    ReconciliationSourceType,
    ReconciliationStatus,
)
from trading_control_plane.reconciliation_models import (
    ExecutionReconciliationFinding,
    ExecutionReconciliationFindingResolution,
    ExecutionReconciliationInput,
    ExecutionReconciliationRun,
    ExecutionReconciliationRunState,
    ExecutionReconciliationRunStateHistory,
)
from trading_control_plane.sender_fencing import SenderLeaseAction, sender_scope_id
from trading_control_plane.sender_fencing_models import (
    ExecutionSenderLease,
    ExecutionSenderScope,
    ExecutionSenderScopeState,
    ShadowDispatchClaim,
)

pytestmark = pytest.mark.integration


def count_rows(session: Session, model: type[object]) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def test_successful_run_persists_exact_manifest_terminal_evidence_and_replays(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    scope = make_sender_scope()
    lease_id = uuid4()
    acquired = execute_acquire(
        database,
        acquire_envelope(scope, now=now, lease_id=lease_id),
        now=now,
    )
    assert acquired.status is CommandStatus.COMPLETED

    run_id = uuid4()
    started_at = now + timedelta(seconds=1)
    start = start_envelope(
        run_id,
        scope,
        lease_id,
        1,
        now=started_at,
        idempotency_key="reconciliation-start-replay",
    )
    first = execute_reconciliation(database, start, now=started_at)
    replay = execute_reconciliation(database, start, now=started_at)
    assert first.status is CommandStatus.COMPLETED
    assert replay.status is CommandStatus.ALREADY_PROCESSED
    assert first.data["live_dispatch_eligible"] is False

    version = collect_complete_inputs(database, run_id, now=started_at)
    compared = execute_reconciliation(
        database,
        phase_envelope(
            run_id,
            ReconciliationPhase.COMPARING,
            now=started_at,
            expected_version=version,
        ),
        now=started_at,
    )
    finished = execute_reconciliation(
        database,
        finish_envelope(
            run_id,
            ReconciliationStatus.SUCCEEDED,
            now=started_at,
            expected_version=version + 1,
        ),
        now=started_at,
    )
    assert compared.status is CommandStatus.COMPLETED
    assert finished.status is CommandStatus.COMPLETED
    assert finished.object_version == 10
    assert finished.data["status"] == "SUCCEEDED"
    assert finished.data["external_send_permitted"] is False

    with database.session_factory.begin() as session:
        run = session.get(ExecutionReconciliationRun, run_id)
        state = session.get(ExecutionReconciliationRunState, run_id)
        assert run is not None and state is not None
        assert run.required_source_types == [source.value for source in ReconciliationSourceType]
        assert state.status == "SUCCEEDED"
        assert state.collected_source_count == 7
        assert state.result_snapshot is not None
        assert state.result_snapshot["no_historical_replay"] is True
        assert state.result_snapshot["external_send_permitted"] is False
        assert count_rows(session, ExecutionReconciliationInput) == 7
        assert count_rows(session, ExecutionReconciliationRunStateHistory) == 10

    with pytest.raises(DBAPIError, match="execution_reconciliation_runs is immutable"):
        with database.session_factory.begin() as session:
            session.execute(
                update(ExecutionReconciliationRun)
                .where(ExecutionReconciliationRun.run_id == run_id)
                .values(reason_code="TAMPERED")
            )


def test_unknown_input_finishes_unknown_and_terminal_run_cannot_resume(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    scope = make_sender_scope()
    lease_id = uuid4()
    execute_acquire(database, acquire_envelope(scope, now=now, lease_id=lease_id), now=now)
    run_id = uuid4()
    run_time = now + timedelta(seconds=1)
    execute_reconciliation(
        database,
        start_envelope(run_id, scope, lease_id, 1, now=run_time),
        now=run_time,
    )
    unknown_input = execute_reconciliation(
        database,
        input_envelope(
            run_id,
            ReconciliationSourceType.VENUE_ORDERS,
            now=run_time,
            expected_version=1,
            status=ReconciliationCollectionStatus.UNKNOWN,
        ),
        now=run_time,
    )
    assert unknown_input.status is CommandStatus.COMPLETED
    finish = execute_reconciliation(
        database,
        finish_envelope(
            run_id,
            ReconciliationStatus.UNKNOWN,
            now=run_time,
            expected_version=2,
        ),
        now=run_time,
    )
    assert finish.status is CommandStatus.COMPLETED
    assert finish.data["status"] == "UNKNOWN"
    assert finish.data["external_send_permitted"] is False

    late = execute_reconciliation(
        database,
        input_envelope(
            run_id,
            ReconciliationSourceType.VENUE_FILLS,
            now=run_time,
            expected_version=3,
        ),
        now=run_time,
    )
    assert late.status is CommandStatus.REJECTED
    assert late.error_code == "RECONCILIATION_RUN_TERMINAL"


def test_inputs_are_ordered_by_version_and_frozen_watermark_contract(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    scope = make_sender_scope()
    lease_id = uuid4()
    execute_acquire(database, acquire_envelope(scope, now=now, lease_id=lease_id), now=now)
    run_id = uuid4()
    run_time = now + timedelta(seconds=1)
    execute_reconciliation(
        database,
        start_envelope(run_id, scope, lease_id, 1, now=run_time),
        now=run_time,
    )
    premature = execute_reconciliation(
        database,
        phase_envelope(run_id, ReconciliationPhase.COMPARING, now=run_time, expected_version=1),
        now=run_time,
    )
    assert premature.status is CommandStatus.REJECTED
    assert premature.error_code == "RECONCILIATION_INPUTS_INCOMPLETE"

    incomplete_watermark = execute_reconciliation(
        database,
        input_envelope(
            run_id,
            ReconciliationSourceType.TRADING_LEDGER,
            now=run_time,
            expected_version=1,
            observed_from=run_time - timedelta(minutes=4),
        ),
        now=run_time,
    )
    assert incomplete_watermark.status is CommandStatus.REJECTED
    assert incomplete_watermark.error_code == "RECONCILIATION_WATERMARK_INCOMPLETE"

    accepted = execute_reconciliation(
        database,
        input_envelope(
            run_id,
            ReconciliationSourceType.TRADING_LEDGER,
            now=run_time,
            expected_version=1,
        ),
        now=run_time,
    )
    assert accepted.status is CommandStatus.COMPLETED
    duplicate = execute_reconciliation(
        database,
        input_envelope(
            run_id,
            ReconciliationSourceType.TRADING_LEDGER,
            now=run_time,
            expected_version=2,
        ),
        now=run_time,
    )
    assert duplicate.status is CommandStatus.REJECTED
    assert duplicate.error_code == "RECONCILIATION_INPUT_ALREADY_RECORDED"
    stale_version = execute_reconciliation(
        database,
        input_envelope(
            run_id,
            ReconciliationSourceType.VENUE_ORDERS,
            now=run_time,
            expected_version=1,
        ),
        now=run_time,
    )
    assert stale_version.status is CommandStatus.REJECTED
    assert stale_version.error_code == "VERSION_CONFLICT"


def test_blocking_finding_requires_append_only_resolution_before_success(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    scope = make_sender_scope()
    lease_id = uuid4()
    execute_acquire(database, acquire_envelope(scope, now=now, lease_id=lease_id), now=now)
    run_id = uuid4()
    run_time = now + timedelta(seconds=1)
    execute_reconciliation(
        database,
        start_envelope(run_id, scope, lease_id, 1, now=run_time),
        now=run_time,
    )
    version = collect_complete_inputs(database, run_id, now=run_time)
    execute_reconciliation(
        database,
        phase_envelope(
            run_id, ReconciliationPhase.COMPARING, now=run_time, expected_version=version
        ),
        now=run_time,
    )
    finding_command, finding_id = finding_envelope(
        run_id, now=run_time, expected_version=version + 1
    )
    finding = execute_reconciliation(database, finding_command, now=run_time)
    assert finding.status is CommandStatus.COMPLETED
    blocked = execute_reconciliation(
        database,
        finish_envelope(
            run_id,
            ReconciliationStatus.SUCCEEDED,
            now=run_time,
            expected_version=version + 2,
        ),
        now=run_time,
    )
    assert blocked.status is CommandStatus.REJECTED
    assert blocked.error_code == "RECONCILIATION_BLOCKING_FINDINGS_OPEN"

    early_resolution = execute_reconciliation(
        database,
        resolution_envelope(run_id, finding_id, now=run_time, expected_version=version + 2),
        now=run_time,
    )
    assert early_resolution.status is CommandStatus.REJECTED
    assert early_resolution.error_code == "RECONCILIATION_PHASE_INVALID"
    adjusted = execute_reconciliation(
        database,
        phase_envelope(
            run_id,
            ReconciliationPhase.ADJUSTING,
            now=run_time,
            expected_version=version + 2,
        ),
        now=run_time,
    )
    assert adjusted.status is CommandStatus.COMPLETED
    resolved = execute_reconciliation(
        database,
        resolution_envelope(run_id, finding_id, now=run_time, expected_version=version + 3),
        now=run_time,
    )
    assert resolved.status is CommandStatus.COMPLETED
    succeeded = execute_reconciliation(
        database,
        finish_envelope(
            run_id,
            ReconciliationStatus.SUCCEEDED,
            now=run_time,
            expected_version=version + 4,
        ),
        now=run_time,
    )
    assert succeeded.status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        assert count_rows(session, ExecutionReconciliationFinding) == 1
        assert count_rows(session, ExecutionReconciliationFindingResolution) == 1


def test_only_one_running_run_per_scope_under_concurrency(database: Database) -> None:
    now = datetime.now(UTC)
    scope = make_sender_scope()
    lease_id = uuid4()
    execute_acquire(database, acquire_envelope(scope, now=now, lease_id=lease_id), now=now)
    run_time = now + timedelta(seconds=1)
    envelopes = tuple(start_envelope(uuid4(), scope, lease_id, 1, now=run_time) for _ in range(2))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                lambda envelope: execute_reconciliation(database, envelope, now=run_time),
                envelopes,
            )
        )
    assert [result.status for result in results].count(CommandStatus.COMPLETED) == 1
    assert [result.status for result in results].count(CommandStatus.REJECTED) == 1
    rejected = next(result for result in results if result.status is CommandStatus.REJECTED)
    assert rejected.error_code == "RECONCILIATION_ALREADY_RUNNING"


def test_fenced_old_lease_run_cannot_authorize_new_sender_claim(database: Database) -> None:
    now = datetime.now(UTC)
    intent, scope = prepare_shadow_intent(database, now)
    first_lease_id = uuid4()
    execute_acquire(
        database,
        acquire_envelope(scope, now=now, lease_id=first_lease_id),
        now=now,
    )
    first_run_id = complete_successful_reconciliation(
        database,
        scope,
        first_lease_id,
        1,
        now=now + timedelta(seconds=1),
    )
    fenced = execute_tighten(
        database,
        tighten_envelope(
            scope,
            action=SenderLeaseAction.FENCE,
            now=now + timedelta(seconds=2),
            expected_version=1,
            lease_id=first_lease_id,
            fencing_token=1,
        ),
        now=now + timedelta(seconds=2),
    )
    assert fenced.status is CommandStatus.COMPLETED
    second_lease_id = uuid4()
    reacquired = execute_acquire(
        database,
        acquire_envelope(
            scope,
            now=now + timedelta(seconds=3),
            lease_id=second_lease_id,
            expected_version=2,
        ),
        now=now + timedelta(seconds=3),
    )
    assert reacquired.data["fencing_token"] == 3

    stale_run_claim = execute_claim(
        database,
        claim_envelope(
            scope,
            intent.order_intent_id,
            second_lease_id,
            3,
            first_run_id,
            now=now + timedelta(seconds=4),
        ),
        now=now + timedelta(seconds=4),
    )
    assert stale_run_claim.status is CommandStatus.REJECTED
    assert stale_run_claim.error_code == "RECONCILIATION_SUCCESS_REQUIRED"
    stale_start = execute_reconciliation(
        database,
        start_envelope(
            uuid4(),
            scope,
            first_lease_id,
            1,
            now=now + timedelta(seconds=4),
        ),
        now=now + timedelta(seconds=4),
    )
    assert stale_start.status is CommandStatus.REJECTED
    assert stale_start.error_code == "SENDER_LEASE_INACTIVE"


def test_newer_run_invalidates_old_success_and_requires_supersedes_chain(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    intent, scope = prepare_shadow_intent(database, now)
    lease_id = uuid4()
    execute_acquire(
        database,
        acquire_envelope(scope, now=now + timedelta(seconds=1), lease_id=lease_id),
        now=now + timedelta(seconds=1),
    )
    first_run_id = complete_successful_reconciliation(
        database,
        scope,
        lease_id,
        1,
        now=now + timedelta(seconds=2),
    )
    second_run_id = uuid4()
    second_time = now + timedelta(seconds=3)
    second = execute_reconciliation(
        database,
        start_envelope(
            second_run_id,
            scope,
            lease_id,
            1,
            now=second_time,
            supersedes_run_id=first_run_id,
        ),
        now=second_time,
    )
    assert second.status is CommandStatus.COMPLETED
    unknown = execute_reconciliation(
        database,
        finish_envelope(
            second_run_id,
            ReconciliationStatus.UNKNOWN,
            now=second_time,
            expected_version=1,
        ),
        now=second_time,
    )
    assert unknown.status is CommandStatus.COMPLETED
    old_success = execute_claim(
        database,
        claim_envelope(
            scope,
            intent.order_intent_id,
            lease_id,
            1,
            first_run_id,
            now=now + timedelta(seconds=4),
        ),
        now=now + timedelta(seconds=4),
    )
    assert old_success.status is CommandStatus.REJECTED
    assert old_success.error_code == "RECONCILIATION_SUCCESS_REQUIRED"

    with pytest.raises(
        DBAPIError, match="shadow dispatch claim violates current fenced non-dispatch contract"
    ):
        with database.session_factory.begin() as session:
            lease = session.get(ExecutionSenderLease, lease_id)
            scope_id = sender_scope_id(scope)
            sender_scope = session.get(ExecutionSenderScope, scope_id)
            sender_state = session.get(ExecutionSenderScopeState, scope_id)
            certificate = session.get(CapabilityCertificate, intent.capability_certificate_ref)
            first_state = session.get(ExecutionReconciliationRunState, first_run_id)
            assert lease is not None
            assert sender_scope is not None
            assert sender_state is not None
            assert sender_state.lease_expires_at is not None
            assert certificate is not None
            assert first_state is not None and first_state.result_hash is not None
            session.add(
                ShadowDispatchClaim(
                    claim_id=uuid4(),
                    organization_id="org-1",
                    order_intent_id=intent.order_intent_id,
                    scope_id=sender_scope.scope_id,
                    lease_id=lease.lease_id,
                    fencing_token=lease.fencing_token,
                    client_order_id=f"shadow-old-reconciliation-{uuid4()}",
                    owner_worker_id=WORKER_ID,
                    worker_config_hash=lease.worker_config_hash,
                    credential_fingerprint=lease.credential_fingerprint,
                    capability_certificate_ref=certificate.certificate_id,
                    reconciliation_run_id=first_run_id,
                    reconciliation_result_hash=first_state.result_hash,
                    execution_mode="SHADOW",
                    external_send_permitted=False,
                    live_gate_status="DISABLED",
                    intent_snapshot_hash=intent.intent_snapshot_hash,
                    capability_certificate_hash=certificate.certificate_hash,
                    scope_hash=sender_scope.scope_hash,
                    lease_hash=lease.lease_hash,
                    lease_expires_at=sender_state.lease_expires_at,
                    worker_observed_at=now + timedelta(seconds=4),
                    claimed_at=now + timedelta(seconds=4),
                    reason_code="FORGED_OLD_RECONCILIATION",
                    claim_hash="0" * 64,
                )
            )

    missing_chain = execute_reconciliation(
        database,
        start_envelope(
            uuid4(),
            scope,
            lease_id,
            1,
            now=now + timedelta(seconds=5),
        ),
        now=now + timedelta(seconds=5),
    )
    assert missing_chain.status is CommandStatus.REJECTED
    assert missing_chain.error_code == "RECONCILIATION_SUPERSEDES_REQUIRED"
    with pytest.raises(DBAPIError, match="reconciliation must supersede the latest scope run"):
        with database.session_factory.begin() as session:
            session.add(
                ExecutionReconciliationRun(
                    run_id=uuid4(),
                    schema_version=1,
                    organization_id="org-1",
                    scope_id=sender_scope_id(scope),
                    lease_id=lease_id,
                    fencing_token=1,
                    trigger_type="STARTUP",
                    environment="SHADOW",
                    live_dispatch_eligible=False,
                    required_source_types=[source.value for source in ReconciliationSourceType],
                    observation_window_start=now,
                    observation_window_end=now + timedelta(seconds=1),
                    supersedes_run_id=None,
                    initiated_by="execution-reconciliation-service",
                    reason_code="FORGED_MISSING_SUPERSEDES",
                    source_ref="test-only:forged-missing-supersedes",
                    started_at=now + timedelta(seconds=5),
                    deadline_at=now + timedelta(seconds=20),
                    run_hash="0" * 64,
                )
            )


def test_expired_run_cannot_succeed_but_can_end_failed_safe(database: Database) -> None:
    now = datetime.now(UTC)
    scope = make_sender_scope()
    lease_id = uuid4()
    execute_acquire(
        database,
        acquire_envelope(
            scope,
            now=now,
            lease_id=lease_id,
            ttl_seconds=60,
            max_lifetime_seconds=60,
        ),
        now=now,
    )
    run_id = uuid4()
    run_time = now + timedelta(seconds=1)
    execute_reconciliation(
        database,
        start_envelope(run_id, scope, lease_id, 1, now=run_time),
        now=run_time,
    )
    version = collect_complete_inputs(database, run_id, now=run_time)
    execute_reconciliation(
        database,
        phase_envelope(
            run_id, ReconciliationPhase.COMPARING, now=run_time, expected_version=version
        ),
        now=run_time,
    )
    expired_at = run_time + timedelta(seconds=21)
    expired_success = execute_reconciliation(
        database,
        finish_envelope(
            run_id,
            ReconciliationStatus.SUCCEEDED,
            now=expired_at,
            expected_version=version + 1,
        ),
        now=expired_at,
    )
    assert expired_success.status is CommandStatus.REJECTED
    assert expired_success.error_code == "RECONCILIATION_DEADLINE_EXPIRED"
    failed = execute_reconciliation(
        database,
        finish_envelope(
            run_id,
            ReconciliationStatus.FAILED,
            now=expired_at,
            expected_version=version + 1,
        ),
        now=expired_at,
    )
    assert failed.status is CommandStatus.COMPLETED
    assert failed.data["status"] == "FAILED"
    assert failed.data["external_send_permitted"] is False


def test_database_graph_guard_rejects_forged_progress_counts(database: Database) -> None:
    now = datetime.now(UTC)
    scope = make_sender_scope()
    lease_id = uuid4()
    execute_acquire(database, acquire_envelope(scope, now=now, lease_id=lease_id), now=now)
    run_id = uuid4()
    run_time = now + timedelta(seconds=1)
    execute_reconciliation(
        database,
        start_envelope(run_id, scope, lease_id, 1, now=run_time),
        now=run_time,
    )
    with pytest.raises(
        DBAPIError, match="reconciliation state counts or binding disagree with facts"
    ):
        with database.session_factory.begin() as session:
            state = session.get(ExecutionReconciliationRunState, run_id)
            assert state is not None
            state.version = 2
            state.collected_source_count = 1
            state.reason_code = "FORGED_PROGRESS"
            state.source_ref = "test-only:forged-progress"
            state.updated_at = run_time + timedelta(milliseconds=1)
