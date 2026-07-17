from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from tests.integration.test_execution import (
    ExecutionFactDraft,
    bind_fact_request,
    create_intent_envelope,
    execute_create,
    fact_envelope,
    fact_request,
    prepare_active_fact_run,
    prepare_authorization,
    seed_execution_policy,
)
from tests.reconciliation_fixtures import (
    execute_reconciliation,
    finish_envelope,
)
from tests.sender_fencing_fixtures import (
    execute_tighten,
    make_sender_scope,
    tighten_envelope,
)
from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import CommandStatus, hash_json
from trading_control_plane.database import Database
from trading_control_plane.execution import (
    ExecutionFactKind,
    ExecutionReconciliationService,
    RecordExecutionFactRequest,
)
from trading_control_plane.execution_models import ExecutionFact
from trading_control_plane.reconciliation import (
    ReconciliationSourceType,
    ReconciliationStatus,
)
from trading_control_plane.reconciliation_models import (
    ExecutionReconciliationRun,
    ExecutionReconciliationRunState,
)
from trading_control_plane.sender_fencing import SenderLeaseAction
from trading_control_plane.sender_fencing_models import (
    ExecutionSenderScopeState,
    ShadowDispatchClaim,
)

pytestmark = pytest.mark.integration


def create_order_intent(database: Database) -> UUID:
    now = datetime.now(UTC)
    proposal, campaign, initial = prepare_authorization(database)
    seed_execution_policy(database, now)
    created = execute_create(
        database,
        create_intent_envelope(proposal, campaign, initial, now=now),
    )
    assert created.status is CommandStatus.COMPLETED
    return UUID(str(created.data["order_intent_id"]))


def execute_bound_fact(
    database: Database,
    order_intent_id: UUID,
    request: RecordExecutionFactRequest,
):
    return IdempotentCommandExecutor(database.session_factory).execute(
        fact_envelope(order_intent_id, request),
        ExecutionReconciliationService(clock=lambda: request.received_at).record,
    )


def mutate_request(
    request: RecordExecutionFactRequest, **updates: Any
) -> RecordExecutionFactRequest:
    values = request.model_dump(mode="python")
    values.update(updates)
    values.pop("evidence_hash", None)
    provisional = RecordExecutionFactRequest.model_construct(**values, evidence_hash="0" * 64)
    values["evidence_hash"] = hash_json(
        provisional.model_dump(mode="json", exclude={"evidence_hash"})
    )
    return RecordExecutionFactRequest.model_validate(values)


def fact_row(
    order_intent_id: UUID,
    request: RecordExecutionFactRequest,
    *,
    recorded_at: datetime,
) -> ExecutionFact:
    return ExecutionFact(
        execution_fact_id=uuid4(),
        order_intent_id=order_intent_id,
        fact_sequence=request.fact_sequence,
        fact_contract_version=2,
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
        source_ref=request.source_ref,
        source_version=request.source_version,
        payload=request.payload,
        payload_hash=request.payload_hash,
        evidence_ref=request.evidence_ref,
        evidence_hash=request.evidence_hash,
        event_time=request.event_time,
        received_at=request.received_at,
        recorded_at=recorded_at,
    )


def prepare_bound_request(
    database: Database,
    order_intent_id: UUID,
    draft: ExecutionFactDraft | None = None,
) -> tuple[RecordExecutionFactRequest, UUID]:
    effective_draft = draft or fact_request(
        sequence=1,
        status="VENUE_ACKNOWLEDGED",
        filled=Decimal("0"),
        remaining=Decimal("0.5"),
    )
    if effective_draft.status in {"PARTIALLY_FILLED", "FILLED", "CANCELLED_PARTIAL"}:
        source_type = ReconciliationSourceType.VENUE_FILLS
    elif effective_draft.status == "POSITION_RECONCILED":
        source_type = ReconciliationSourceType.VENUE_POSITIONS
    elif effective_draft.status in {"PROTECTION_CONFIRMED", "COMPLETED"}:
        source_type = ReconciliationSourceType.VENUE_PROTECTION
    elif effective_draft.status == "DISPATCHING":
        source_type = ReconciliationSourceType.WORKER_LOCAL
    else:
        source_type = ReconciliationSourceType.VENUE_ORDERS
    claim, run, reconciliation_input, event_time = prepare_active_fact_run(
        database,
        order_intent_id,
        source_type,
    )
    request = bind_fact_request(
        effective_draft,
        claim,
        run,
        reconciliation_input,
        event_time=event_time,
        received_at=event_time + timedelta(milliseconds=1),
    )
    return request, run.run_id


def finish_run(database: Database, run_id: UUID, *, now: datetime) -> None:
    with database.session_factory.begin() as session:
        state = session.get(ExecutionReconciliationRunState, run_id)
        assert state is not None
        version = state.version
    result = execute_reconciliation(
        database,
        finish_envelope(
            run_id,
            ReconciliationStatus.SUCCEEDED,
            now=now,
            expected_version=version,
        ),
        now=now,
    )
    assert result.status is CommandStatus.COMPLETED


def test_exact_claim_run_input_binding_is_persisted_and_replayable_after_restart(
    database: Database,
) -> None:
    order_intent_id = create_order_intent(database)
    request, run_id = prepare_bound_request(database, order_intent_id)

    first = execute_bound_fact(database, order_intent_id, request)
    restarted_service_result = execute_bound_fact(database, order_intent_id, request)

    assert first.status is CommandStatus.COMPLETED
    assert restarted_service_result.status is CommandStatus.COMPLETED
    assert restarted_service_result.data["already_recorded"] is True
    with database.session_factory.begin() as session:
        fact = session.execute(select(ExecutionFact)).scalar_one()
        assert fact.fact_contract_version == 2
        assert fact.fact_kind == "VENUE_ORDER"
        assert fact.shadow_dispatch_claim_id == request.shadow_dispatch_claim_id
        assert fact.reconciliation_run_id == request.reconciliation_run_id
        assert fact.reconciliation_input_id == request.reconciliation_input_id
        assert fact.reconciliation_source_type == "VENUE_ORDERS"
        assert fact.reconciliation_run_ref is None
    finish_run(database, run_id, now=request.received_at + timedelta(milliseconds=1))


def test_legacy_v1_command_and_database_insert_are_both_closed(database: Database) -> None:
    order_intent_id = create_order_intent(database)
    request, _ = prepare_bound_request(database, order_intent_id)
    legacy_envelope = fact_envelope(order_intent_id, request).model_copy(
        update={"command_type": "execution.fact.record.v1"}
    )

    rejected = IdempotentCommandExecutor(database.session_factory).execute(
        legacy_envelope,
        ExecutionReconciliationService(clock=lambda: request.received_at).record,
    )

    assert rejected.status is CommandStatus.REJECTED
    assert rejected.error_code == "COMMAND_TYPE_MISMATCH"
    payload = {"legacy": True}
    with pytest.raises(DBAPIError, match="reconciled v2 contract"):
        with database.session_factory.begin() as session:
            session.add(
                ExecutionFact(
                    execution_fact_id=uuid4(),
                    order_intent_id=order_intent_id,
                    fact_sequence=1,
                    fact_contract_version=1,
                    fact_kind=None,
                    target_status="VENUE_ACKNOWLEDGED",
                    venue="BINANCE",
                    execution_domain="BINANCE_USDM",
                    account_id="account-1",
                    external_fact_id=f"legacy-{uuid4()}",
                    cumulative_filled_quantity=Decimal("0"),
                    known_remaining_quantity=Decimal("0.5"),
                    zero_fill_confirmed=False,
                    venue_order_terminal=False,
                    position_reconciled=False,
                    protection_confirmed=False,
                    reconciliation_run_ref="legacy:unbound",
                    shadow_dispatch_claim_id=None,
                    reconciliation_run_id=None,
                    reconciliation_input_id=None,
                    reconciliation_source_type=None,
                    reconciliation_run_hash=None,
                    reconciliation_input_hash=None,
                    dispatch_claim_hash=None,
                    source_ref="legacy:source",
                    source_version="legacy-v1",
                    payload=payload,
                    payload_hash=hash_json(payload),
                    evidence_ref="legacy:evidence",
                    evidence_hash=hash_json({"legacy": "evidence"}),
                    event_time=request.event_time,
                    received_at=request.received_at,
                    recorded_at=request.received_at,
                )
            )
            session.flush()


@pytest.mark.parametrize(
    ("updates", "expected_error"),
    [
        ({"dispatch_claim_hash": "0" * 64}, "EXECUTION_FACT_CLAIM_MISMATCH"),
        (
            {"reconciliation_run_hash": "0" * 64},
            "EXECUTION_FACT_RECONCILIATION_RUN_MISMATCH",
        ),
        (
            {"reconciliation_input_id": uuid4()},
            "EXECUTION_FACT_RECONCILIATION_INPUT_NOT_FOUND",
        ),
        (
            {"fact_kind": ExecutionFactKind.WORKER_RECEIPT},
            "EXECUTION_FACT_SOURCE_STATUS_MISMATCH",
        ),
    ],
)
def test_forged_or_incompatible_bindings_fail_closed_with_stable_errors(
    database: Database,
    updates: dict[str, Any],
    expected_error: str,
) -> None:
    order_intent_id = create_order_intent(database)
    request, _ = prepare_bound_request(database, order_intent_id)

    result = execute_bound_fact(database, order_intent_id, mutate_request(request, **updates))

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == expected_error
    with database.session_factory.begin() as session:
        assert session.execute(select(func.count()).select_from(ExecutionFact)).scalar_one() == 0


def test_terminal_reconciliation_run_cannot_authorize_a_late_fact(database: Database) -> None:
    order_intent_id = create_order_intent(database)
    request, run_id = prepare_bound_request(database, order_intent_id)
    finish_run(database, run_id, now=request.received_at)

    result = execute_bound_fact(database, order_intent_id, request)

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "EXECUTION_FACT_RECONCILIATION_RUN_NOT_ACTIVE"


def test_expired_run_and_pre_claim_event_both_fail_closed(database: Database) -> None:
    order_intent_id = create_order_intent(database)
    request, run_id = prepare_bound_request(database, order_intent_id)
    with database.session_factory.begin() as session:
        run = session.get(ExecutionReconciliationRun, run_id)
        assert run is not None
        deadline_at = run.deadline_at

    expired = IdempotentCommandExecutor(database.session_factory).execute(
        fact_envelope(order_intent_id, request),
        ExecutionReconciliationService(clock=lambda: deadline_at).record,
    )

    assert expired.status is CommandStatus.REJECTED
    assert expired.error_code == "EXECUTION_FACT_RECONCILIATION_RUN_EXPIRED"
    with pytest.raises(DBAPIError, match="reconciliation run deadline elapsed"):
        with database.session_factory.begin() as session:
            session.add(fact_row(order_intent_id, request, recorded_at=deadline_at))
            session.flush()

    with database.session_factory.begin() as session:
        claim = session.get(ShadowDispatchClaim, request.shadow_dispatch_claim_id)
        assert claim is not None
        pre_claim_time = claim.claimed_at - timedelta(microseconds=1)
    pre_claim = execute_bound_fact(
        database,
        order_intent_id,
        mutate_request(request, event_time=pre_claim_time),
    )

    assert pre_claim.status is CommandStatus.REJECTED
    assert pre_claim.error_code == "EXECUTION_FACT_PRE_CLAIM_EVENT"


def test_fenced_sender_lease_cannot_authorize_a_fact(database: Database) -> None:
    order_intent_id = create_order_intent(database)
    request, _ = prepare_bound_request(database, order_intent_id)
    with database.session_factory.begin() as session:
        sender_state = session.execute(select(ExecutionSenderScopeState)).scalar_one()
        scope_id = sender_state.scope_id
        state_version = sender_state.version
    fenced_at = request.received_at
    fenced = execute_tighten(
        database,
        tighten_envelope(
            make_sender_scope(),
            action=SenderLeaseAction.FENCE,
            now=fenced_at,
            expected_version=state_version,
        ),
        now=fenced_at,
    )
    assert fenced.status is CommandStatus.COMPLETED
    assert fenced.object_id == scope_id

    result = execute_bound_fact(database, order_intent_id, request)

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "EXECUTION_FACT_SENDER_LEASE_STALE"


def test_concurrent_facts_for_one_sequence_apply_exactly_once(database: Database) -> None:
    order_intent_id = create_order_intent(database)
    first_request, run_id = prepare_bound_request(database, order_intent_id)
    second_request = mutate_request(
        first_request,
        external_fact_id=f"venue-fact-{uuid4()}",
        evidence_ref="test-only:concurrent-second",
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                lambda request: execute_bound_fact(database, order_intent_id, request),
                (first_request, second_request),
            )
        )

    assert [result.status for result in results].count(CommandStatus.COMPLETED) == 1
    assert [result.status for result in results].count(CommandStatus.REJECTED) == 1
    rejected = next(result for result in results if result.status is CommandStatus.REJECTED)
    assert rejected.error_code in {"EXECUTION_FACT_OUT_OF_ORDER", "VERSION_CONFLICT"}
    with database.session_factory.begin() as session:
        assert session.execute(select(func.count()).select_from(ExecutionFact)).scalar_one() == 1
    finish_run(
        database,
        run_id,
        now=first_request.received_at + timedelta(milliseconds=1),
    )
