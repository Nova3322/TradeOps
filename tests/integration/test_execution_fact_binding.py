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
from trading_control_plane.execution_models import ExecutionFact, OrderIntentState
from trading_control_plane.reconciliation import (
    ReconciliationSourceType,
    ReconciliationStatus,
)
from trading_control_plane.reconciliation_models import (
    ExecutionReconciliationRun,
    ExecutionReconciliationRunState,
)
from trading_control_plane.sender_fencing import SenderLeaseAction, sender_scope_id
from trading_control_plane.sender_fencing_models import (
    ExecutionSenderScopeState,
    ShadowDispatchClaim,
)
from trading_control_plane.venue_fact_models import (
    VenuePositionSnapshot,
    VenueProtectionSnapshot,
)
from trading_control_plane.venue_facts import (
    VenuePositionDirection,
    VenuePositionState,
    VenueProtectionState,
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
        venue_position_snapshot_id=request.venue_position_snapshot_id,
        venue_protection_snapshot_id=request.venue_protection_snapshot_id,
        venue_fact_input_link_id=request.venue_fact_input_link_id,
        venue_fact_hash=request.venue_fact_hash,
        canonical_venue_order_id=request.payload.get("canonical_venue_order_id"),
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
    if effective_draft.status in {"PARTIALLY_FILLED", "FILLED"}:
        source_type = ReconciliationSourceType.VENUE_FILLS
    elif effective_draft.status == "POSITION_RECONCILED":
        source_type = ReconciliationSourceType.VENUE_POSITIONS
    elif effective_draft.status in {"PROTECTION_CONFIRMED", "COMPLETED"}:
        source_type = ReconciliationSourceType.VENUE_PROTECTION
    elif effective_draft.status == "DISPATCHING":
        source_type = ReconciliationSourceType.WORKER_LOCAL
    else:
        source_type = ReconciliationSourceType.VENUE_ORDERS
    claim, run, reconciliation_input, event_time, canonical_context = prepare_active_fact_run(
        database,
        order_intent_id,
        source_type,
        draft=effective_draft,
    )
    request = bind_fact_request(
        effective_draft,
        claim,
        run,
        reconciliation_input,
        event_time=event_time,
        received_at=(
            canonical_context.input_link.received_at
            if canonical_context is not None
            else event_time + timedelta(milliseconds=1)
        ),
        canonical_context=canonical_context,
    )
    return request, run.run_id


def prepare_filled_intent(database: Database) -> tuple[UUID, RecordExecutionFactRequest]:
    order_intent_id = create_order_intent(database)
    fill_request, run_id = prepare_bound_request(
        database,
        order_intent_id,
        fact_request(
            sequence=1,
            status="FILLED",
            filled=Decimal("0.5"),
            remaining=Decimal("0"),
            terminal=True,
        ),
    )
    filled = execute_bound_fact(database, order_intent_id, fill_request)
    assert filled.status is CommandStatus.COMPLETED
    finish_run(database, run_id, now=fill_request.received_at + timedelta(milliseconds=1))
    return order_intent_id, fill_request


def prepare_position_request(
    database: Database,
    order_intent_id: UUID,
    fill_request: RecordExecutionFactRequest,
    *,
    position_snapshot_overrides: dict[str, Any] | None = None,
) -> tuple[RecordExecutionFactRequest, UUID]:
    draft = fact_request(
        sequence=2,
        status="POSITION_RECONCILED",
        filled=Decimal("0.5"),
        remaining=Decimal("0"),
        terminal=True,
        reconciled=True,
    )
    claim, run, reconciliation_input, event_time, canonical_context = prepare_active_fact_run(
        database,
        order_intent_id,
        ReconciliationSourceType.VENUE_POSITIONS,
        now=fill_request.received_at + timedelta(milliseconds=2),
        draft=draft,
        position_snapshot_overrides=position_snapshot_overrides,
    )
    assert canonical_context is not None
    request = bind_fact_request(
        draft,
        claim,
        run,
        reconciliation_input,
        event_time=event_time,
        received_at=canonical_context.input_link.received_at,
        canonical_context=canonical_context,
    )
    return request, run.run_id


def prepare_position_reconciled_intent(
    database: Database,
) -> tuple[UUID, RecordExecutionFactRequest]:
    order_intent_id, fill_request = prepare_filled_intent(database)
    position_request, run_id = prepare_position_request(database, order_intent_id, fill_request)
    reconciled = execute_bound_fact(database, order_intent_id, position_request)
    assert reconciled.status is CommandStatus.COMPLETED
    finish_run(
        database,
        run_id,
        now=position_request.received_at + timedelta(milliseconds=1),
    )
    return order_intent_id, position_request


def prepare_protection_request(
    database: Database,
    order_intent_id: UUID,
    position_request: RecordExecutionFactRequest,
    *,
    protection_snapshot_overrides: dict[str, Any] | None = None,
    protection_position_snapshot_id: UUID | None = None,
) -> tuple[RecordExecutionFactRequest, UUID]:
    draft = fact_request(
        sequence=3,
        status="PROTECTION_CONFIRMED",
        filled=Decimal("0.5"),
        remaining=Decimal("0"),
        terminal=True,
        reconciled=True,
        protected=True,
    )
    claim, run, reconciliation_input, event_time, canonical_context = prepare_active_fact_run(
        database,
        order_intent_id,
        ReconciliationSourceType.VENUE_PROTECTION,
        now=position_request.received_at + timedelta(milliseconds=2),
        draft=draft,
        protection_snapshot_overrides=protection_snapshot_overrides,
        protection_position_snapshot_id=protection_position_snapshot_id,
    )
    assert canonical_context is not None
    request = bind_fact_request(
        draft,
        claim,
        run,
        reconciliation_input,
        event_time=event_time,
        received_at=canonical_context.input_link.received_at,
        canonical_context=canonical_context,
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
    assert first.data["authority_mode"] == "ORIGINAL_LEASE"
    assert restarted_service_result.status is CommandStatus.COMPLETED
    assert restarted_service_result.data["already_recorded"] is True
    with database.session_factory.begin() as session:
        fact = session.execute(select(ExecutionFact)).scalar_one()
        assert fact.fact_contract_version == 5
        assert fact.fact_kind == "VENUE_ORDER"
        assert fact.shadow_dispatch_claim_id == request.shadow_dispatch_claim_id
        assert fact.reconciliation_run_id == request.reconciliation_run_id
        assert fact.reconciliation_input_id == request.reconciliation_input_id
        assert fact.reconciliation_source_type == "VENUE_ORDERS"
        assert fact.reconciliation_run_ref is None
        assert fact.venue_order_observation_id == request.venue_order_observation_id
        assert fact.venue_fill_id is None
        assert fact.venue_position_snapshot_id is None
        assert fact.venue_fact_input_link_id == request.venue_fact_input_link_id
        assert fact.venue_fact_hash == request.venue_fact_hash
        assert fact.canonical_venue_order_id == request.payload["canonical_venue_order_id"]
    finish_run(database, run_id, now=request.received_at + timedelta(milliseconds=1))


def test_historical_v1_v2_v3_v4_commands_and_database_inserts_are_closed(
    database: Database,
) -> None:
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
    old_v2_envelope = fact_envelope(order_intent_id, request).model_copy(
        update={"command_type": "execution.fact.record-reconciled.v2"}
    )
    old_v2_rejected = IdempotentCommandExecutor(database.session_factory).execute(
        old_v2_envelope,
        ExecutionReconciliationService(clock=lambda: request.received_at).record,
    )
    assert old_v2_rejected.status is CommandStatus.REJECTED
    assert old_v2_rejected.error_code == "COMMAND_TYPE_MISMATCH"
    old_v3_envelope = fact_envelope(order_intent_id, request).model_copy(
        update={"command_type": "execution.fact.record-reconciled.v3"}
    )
    old_v3_rejected = IdempotentCommandExecutor(database.session_factory).execute(
        old_v3_envelope,
        ExecutionReconciliationService(clock=lambda: request.received_at).record,
    )
    assert old_v3_rejected.status is CommandStatus.REJECTED
    assert old_v3_rejected.error_code == "COMMAND_TYPE_MISMATCH"
    old_v4_envelope = fact_envelope(order_intent_id, request).model_copy(
        update={"command_type": "execution.fact.record-reconciled.v4"}
    )
    old_v4_rejected = IdempotentCommandExecutor(database.session_factory).execute(
        old_v4_envelope,
        ExecutionReconciliationService(clock=lambda: request.received_at).record,
    )
    assert old_v4_rejected.status is CommandStatus.REJECTED
    assert old_v4_rejected.error_code == "COMMAND_TYPE_MISMATCH"

    old_v2_row = fact_row(order_intent_id, request, recorded_at=request.received_at)
    old_v2_row.fact_contract_version = 2
    old_v2_row.venue_order_observation_id = None
    old_v2_row.venue_fill_id = None
    old_v2_row.venue_position_snapshot_id = None
    old_v2_row.venue_protection_snapshot_id = None
    old_v2_row.venue_fact_input_link_id = None
    old_v2_row.venue_fact_hash = None
    old_v2_row.canonical_venue_order_id = None
    with pytest.raises(DBAPIError, match="reconciled v5 contract"):
        with database.session_factory.begin() as session:
            session.add(old_v2_row)
            session.flush()

    old_v3_row = fact_row(order_intent_id, request, recorded_at=request.received_at)
    old_v3_row.fact_contract_version = 3
    old_v3_row.external_fact_id = f"old-v3-{uuid4()}"
    with pytest.raises(DBAPIError, match="reconciled v5 contract"):
        with database.session_factory.begin() as session:
            session.add(old_v3_row)
            session.flush()

    old_v4_row = fact_row(order_intent_id, request, recorded_at=request.received_at)
    old_v4_row.fact_contract_version = 4
    old_v4_row.external_fact_id = f"old-v4-{uuid4()}"
    with pytest.raises(DBAPIError, match="reconciled v5 contract"):
        with database.session_factory.begin() as session:
            session.add(old_v4_row)
            session.flush()

    payload = {"legacy": True}
    with pytest.raises(DBAPIError, match="reconciled v5 contract"):
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
            {
                "fact_kind": ExecutionFactKind.WORKER_RECEIPT,
                "venue_order_observation_id": None,
                "venue_fact_input_link_id": None,
                "venue_fact_hash": None,
            },
            "EXECUTION_FACT_SOURCE_STATUS_MISMATCH",
        ),
        (
            {"venue_fact_hash": "0" * 64},
            "EXECUTION_FACT_CANONICAL_LINK_MISMATCH",
        ),
        (
            {"venue_fact_input_link_id": uuid4()},
            "EXECUTION_FACT_CANONICAL_REFERENCE_NOT_FOUND",
        ),
        (
            {
                "cumulative_filled_quantity": Decimal("0.1"),
                "known_remaining_quantity": Decimal("0.4"),
            },
            "EXECUTION_FACT_CANONICAL_SEMANTICS_MISMATCH",
        ),
        (
            {"external_fact_id": str(uuid4())},
            "EXECUTION_FACT_CANONICAL_SEMANTICS_MISMATCH",
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


def test_direct_database_insert_cannot_forge_canonical_quantities(
    database: Database,
) -> None:
    order_intent_id = create_order_intent(database)
    request, _ = prepare_bound_request(database, order_intent_id)
    forged = fact_row(order_intent_id, request, recorded_at=request.received_at)
    forged.cumulative_filled_quantity = Decimal("0.1")
    forged.known_remaining_quantity = Decimal("0.4")

    with pytest.raises(DBAPIError, match="canonical order semantics are invalid"):
        with database.session_factory.begin() as session:
            session.add(forged)
            session.flush()
    with database.session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(ExecutionFact)) == 0


def test_direct_valid_fact_without_atomic_state_and_exposure_is_rolled_back(
    database: Database,
) -> None:
    order_intent_id = create_order_intent(database)
    request, _ = prepare_bound_request(database, order_intent_id)
    direct_fact = fact_row(order_intent_id, request, recorded_at=request.received_at)

    with pytest.raises(DBAPIError, match="requires atomic intent-state and exposure application"):
        with database.session_factory.begin() as session:
            session.add(direct_fact)
    with database.session_factory.begin() as session:
        state = session.get(OrderIntentState, order_intent_id)
        assert state is not None and state.last_fact_sequence == 0
        assert session.scalar(select(func.count()).select_from(ExecutionFact)) == 0


def test_venue_transition_without_canonical_references_is_rejected(
    database: Database,
) -> None:
    order_intent_id = create_order_intent(database)
    request, _ = prepare_bound_request(database, order_intent_id)
    payload = request.model_dump(mode="json")
    payload.update(
        {
            "venue_order_observation_id": None,
            "venue_fact_input_link_id": None,
            "venue_fact_hash": None,
        }
    )
    envelope = fact_envelope(order_intent_id, request).model_copy(update={"payload": payload})

    result = IdempotentCommandExecutor(database.session_factory).execute(
        envelope,
        ExecutionReconciliationService(clock=lambda: request.received_at).record,
    )

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "EXECUTION_FACT_INVALID"
    with database.session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(ExecutionFact)) == 0


def test_canonical_fact_with_different_client_order_identity_is_rejected(
    database: Database,
) -> None:
    order_intent_id = create_order_intent(database)
    draft = fact_request(
        sequence=1,
        status="VENUE_ACKNOWLEDGED",
        filled=Decimal("0"),
        remaining=Decimal("0.5"),
    )
    claim, run, reconciliation_input, event_time, canonical_context = prepare_active_fact_run(
        database,
        order_intent_id,
        ReconciliationSourceType.VENUE_ORDERS,
        draft=draft,
        canonical_client_order_id="different-client-order-id",
    )
    assert canonical_context is not None
    request = bind_fact_request(
        draft,
        claim,
        run,
        reconciliation_input,
        event_time=event_time,
        received_at=canonical_context.input_link.received_at,
        canonical_context=canonical_context,
    )

    result = execute_bound_fact(database, order_intent_id, request)

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "EXECUTION_FACT_CANONICAL_OWNERSHIP_MISMATCH"
    with database.session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(ExecutionFact)) == 0


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
        sender_state = session.get(
            ExecutionSenderScopeState,
            sender_scope_id(make_sender_scope()),
        )
        assert sender_state is not None
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
    assert rejected.error_code in {
        "EXECUTION_FACT_CANONICAL_SEMANTICS_MISMATCH",
        "EXECUTION_FACT_OUT_OF_ORDER",
        "VERSION_CONFLICT",
    }
    with database.session_factory.begin() as session:
        assert session.execute(select(func.count()).select_from(ExecutionFact)).scalar_one() == 1
    finish_run(
        database,
        run_id,
        now=first_request.received_at + timedelta(milliseconds=1),
    )


def test_same_canonical_fill_cannot_be_consumed_twice(database: Database) -> None:
    order_intent_id = create_order_intent(database)
    request, _ = prepare_bound_request(
        database,
        order_intent_id,
        fact_request(
            sequence=1,
            status="PARTIALLY_FILLED",
            filled=Decimal("0.2"),
            remaining=Decimal("0.3"),
        ),
    )
    first = execute_bound_fact(database, order_intent_id, request)
    duplicate = execute_bound_fact(
        database,
        order_intent_id,
        mutate_request(
            request,
            fact_sequence=2,
            cumulative_filled_quantity=Decimal("0.4"),
            known_remaining_quantity=Decimal("0.1"),
        ),
    )

    assert first.status is CommandStatus.COMPLETED
    assert duplicate.status is CommandStatus.REJECTED
    assert duplicate.error_code == "EXTERNAL_FACT_ID_CONFLICT"
    with database.session_factory.begin() as session:
        state = session.get(OrderIntentState, order_intent_id)
        assert state is not None
        assert state.cumulative_filled_quantity == Decimal("0.2")
        assert session.scalar(select(func.count()).select_from(ExecutionFact)) == 1


def test_claimed_intent_cannot_switch_canonical_venue_order_id(
    database: Database,
) -> None:
    order_intent_id = create_order_intent(database)
    partial_draft = fact_request(
        sequence=1,
        status="PARTIALLY_FILLED",
        filled=Decimal("0.2"),
        remaining=Decimal("0.3"),
    )
    partial_request, partial_run_id = prepare_bound_request(
        database, order_intent_id, partial_draft
    )
    partial = execute_bound_fact(database, order_intent_id, partial_request)
    assert partial.status is CommandStatus.COMPLETED
    finish_run(
        database,
        partial_run_id,
        now=partial_request.received_at + timedelta(milliseconds=1),
    )

    cancel_draft = fact_request(
        sequence=2,
        status="CANCELLED_PARTIAL",
        filled=Decimal("0.2"),
        remaining=Decimal("0"),
        terminal=True,
    )
    claim, run, reconciliation_input, event_time, canonical_context = prepare_active_fact_run(
        database,
        order_intent_id,
        ReconciliationSourceType.VENUE_ORDERS,
        draft=cancel_draft,
        canonical_venue_order_id="different-native-order-id",
    )
    assert canonical_context is not None
    cancel_request = bind_fact_request(
        cancel_draft,
        claim,
        run,
        reconciliation_input,
        event_time=event_time,
        received_at=canonical_context.input_link.received_at,
        canonical_context=canonical_context,
    )
    rejected = execute_bound_fact(database, order_intent_id, cancel_request)

    assert rejected.status is CommandStatus.REJECTED
    assert rejected.error_code == "EXECUTION_FACT_CANONICAL_ORDER_ID_MISMATCH"
    with database.session_factory.begin() as session:
        state = session.get(OrderIntentState, order_intent_id)
        assert state is not None and state.status == "PARTIALLY_FILLED"
        assert session.scalar(select(func.count()).select_from(ExecutionFact)) == 1


def test_exact_open_position_snapshot_reconciles_and_replays(database: Database) -> None:
    order_intent_id, fill_request = prepare_filled_intent(database)
    request, run_id = prepare_position_request(database, order_intent_id, fill_request)

    reconciled = execute_bound_fact(database, order_intent_id, request)
    replay = execute_bound_fact(database, order_intent_id, request)

    assert reconciled.status is CommandStatus.COMPLETED
    assert replay.status is CommandStatus.COMPLETED
    assert replay.data["already_recorded"] is True
    with database.session_factory.begin() as session:
        state = session.get(OrderIntentState, order_intent_id)
        fact = session.execute(
            select(ExecutionFact).where(ExecutionFact.fact_kind == "VENUE_POSITION")
        ).scalar_one()
        assert state is not None and state.status == "POSITION_RECONCILED"
        assert state.position_reconciled is True
        assert fact.fact_contract_version == 5
        assert fact.venue_position_snapshot_id == request.venue_position_snapshot_id
        assert fact.venue_fact_input_link_id == request.venue_fact_input_link_id
        assert fact.canonical_venue_order_id is None
    finish_run(database, run_id, now=request.received_at + timedelta(milliseconds=1))


@pytest.mark.parametrize(
    "position_snapshot_overrides",
    [
        {
            "position_state": VenuePositionState.FLAT,
            "direction": VenuePositionDirection.FLAT,
        },
        {
            "position_state": VenuePositionState.UNKNOWN,
            "direction": VenuePositionDirection.UNKNOWN,
        },
    ],
)
def test_flat_or_unknown_position_cannot_advance_reconciliation(
    database: Database,
    position_snapshot_overrides: dict[str, Any],
) -> None:
    order_intent_id, fill_request = prepare_filled_intent(database)
    request, _ = prepare_position_request(
        database,
        order_intent_id,
        fill_request,
        position_snapshot_overrides=position_snapshot_overrides,
    )

    result = execute_bound_fact(database, order_intent_id, request)

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "EXECUTION_FACT_CANONICAL_POSITION_MISMATCH"
    with database.session_factory.begin() as session:
        state = session.get(OrderIntentState, order_intent_id)
        assert state is not None and state.status == "FILLED"
        assert (
            session.scalar(
                select(func.count())
                .select_from(ExecutionFact)
                .where(ExecutionFact.fact_kind == "VENUE_POSITION")
            )
            == 0
        )


def test_position_snapshot_with_wrong_instrument_ownership_is_rejected(
    database: Database,
) -> None:
    order_intent_id, fill_request = prepare_filled_intent(database)
    request, _ = prepare_position_request(
        database,
        order_intent_id,
        fill_request,
        position_snapshot_overrides={"instrument_id": "ETHUSDT-PERP"},
    )

    result = execute_bound_fact(database, order_intent_id, request)

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "EXECUTION_FACT_CANONICAL_OWNERSHIP_MISMATCH"


def test_position_snapshot_with_wrong_post_intent_quantity_is_rejected(
    database: Database,
) -> None:
    order_intent_id, fill_request = prepare_filled_intent(database)
    request, _ = prepare_position_request(
        database,
        order_intent_id,
        fill_request,
        position_snapshot_overrides={"quantity": Decimal("0.6")},
    )

    result = execute_bound_fact(database, order_intent_id, request)

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "EXECUTION_FACT_CANONICAL_POSITION_MISMATCH"


def test_position_snapshot_cannot_skip_terminal_fill_evidence(database: Database) -> None:
    order_intent_id = create_order_intent(database)
    draft = fact_request(
        sequence=1,
        status="POSITION_RECONCILED",
        filled=Decimal("0"),
        remaining=Decimal("0"),
        terminal=True,
        reconciled=True,
    )
    claim, run, reconciliation_input, event_time, canonical_context = prepare_active_fact_run(
        database,
        order_intent_id,
        ReconciliationSourceType.VENUE_POSITIONS,
        draft=draft,
        position_snapshot_overrides={"quantity": Decimal("0.5")},
    )
    assert canonical_context is not None
    request = bind_fact_request(
        draft,
        claim,
        run,
        reconciliation_input,
        event_time=event_time,
        received_at=canonical_context.input_link.received_at,
        canonical_context=canonical_context,
    )

    result = execute_bound_fact(database, order_intent_id, request)

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "EXECUTION_FACT_CANONICAL_POSITION_MISMATCH"
    with database.session_factory.begin() as session:
        state = session.get(OrderIntentState, order_intent_id)
        assert state is not None and state.status == "INTENT_CREATED"


def test_position_snapshot_older_than_fill_evidence_is_rejected(database: Database) -> None:
    order_intent_id, fill_request = prepare_filled_intent(database)
    stale_at = fill_request.event_time - timedelta(microseconds=1)
    request, _ = prepare_position_request(
        database,
        order_intent_id,
        fill_request,
        position_snapshot_overrides={
            "event_time": stale_at,
            "venue_observed_at": stale_at,
        },
    )

    result = execute_bound_fact(database, order_intent_id, request)

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "EXECUTION_FACT_CANONICAL_POSITION_STALE"


def test_position_source_cannot_self_report_failed_safe(database: Database) -> None:
    order_intent_id, fill_request = prepare_filled_intent(database)
    request, _ = prepare_position_request(database, order_intent_id, fill_request)

    result = execute_bound_fact(
        database,
        order_intent_id,
        mutate_request(request, target_status="FAILED_SAFE"),
    )

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "EXECUTION_FACT_SOURCE_STATUS_MISMATCH"


def test_direct_database_insert_cannot_promote_flat_position_snapshot(
    database: Database,
) -> None:
    order_intent_id, fill_request = prepare_filled_intent(database)
    request, _ = prepare_position_request(
        database,
        order_intent_id,
        fill_request,
        position_snapshot_overrides={
            "position_state": VenuePositionState.FLAT,
            "direction": VenuePositionDirection.FLAT,
        },
    )
    forged = fact_row(order_intent_id, request, recorded_at=request.received_at)

    with pytest.raises(DBAPIError, match="canonical position quantity is invalid"):
        with database.session_factory.begin() as session:
            session.add(forged)
            session.flush()
    with database.session_factory.begin() as session:
        state = session.get(OrderIntentState, order_intent_id)
        assert state is not None and state.status == "FILLED"
        assert (
            session.scalar(
                select(func.count())
                .select_from(ExecutionFact)
                .where(ExecutionFact.fact_kind == "VENUE_POSITION")
            )
            == 0
        )


def test_confirmed_native_protection_advances_and_replays(database: Database) -> None:
    order_intent_id, position_request = prepare_position_reconciled_intent(database)
    request, run_id = prepare_protection_request(database, order_intent_id, position_request)

    confirmed = execute_bound_fact(database, order_intent_id, request)
    replay = execute_bound_fact(database, order_intent_id, request)

    assert confirmed.status is CommandStatus.COMPLETED
    assert replay.status is CommandStatus.COMPLETED
    assert replay.data["already_recorded"] is True
    with database.session_factory.begin() as session:
        state = session.get(OrderIntentState, order_intent_id)
        fact = session.execute(
            select(ExecutionFact).where(ExecutionFact.fact_kind == "VENUE_PROTECTION")
        ).scalar_one()
        protection = session.get(VenueProtectionSnapshot, request.venue_protection_snapshot_id)
        assert state is not None and state.status == "PROTECTION_CONFIRMED"
        assert state.position_reconciled is True
        assert state.protection_confirmed is True
        assert protection is not None
        assert protection.worst_active_trigger_price is not None
        assert protection.worst_active_trigger_price > 0
        assert fact.fact_contract_version == 5
        assert fact.venue_protection_snapshot_id == protection.venue_protection_snapshot_id
        assert fact.venue_position_snapshot_id is None
        assert protection.venue_position_snapshot_id == position_request.venue_position_snapshot_id
        assert fact.venue_fact_input_link_id == request.venue_fact_input_link_id
    finish_run(database, run_id, now=request.received_at + timedelta(milliseconds=1))


@pytest.mark.parametrize(
    "protection_state",
    [VenueProtectionState.DEGRADED, VenueProtectionState.UNKNOWN],
)
def test_degraded_or_unknown_protection_cannot_advance(
    database: Database,
    protection_state: VenueProtectionState,
) -> None:
    order_intent_id, position_request = prepare_position_reconciled_intent(database)
    request, _ = prepare_protection_request(
        database,
        order_intent_id,
        position_request,
        protection_snapshot_overrides={"protection_state": protection_state},
    )

    result = execute_bound_fact(database, order_intent_id, request)

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "EXECUTION_FACT_CANONICAL_PROTECTION_MISMATCH"
    with database.session_factory.begin() as session:
        state = session.get(OrderIntentState, order_intent_id)
        assert state is not None and state.status == "POSITION_RECONCILED"
        assert (
            session.scalar(
                select(func.count())
                .select_from(ExecutionFact)
                .where(ExecutionFact.fact_kind == "VENUE_PROTECTION")
            )
            == 0
        )


def test_protection_request_must_preserve_exact_canonical_semantics(
    database: Database,
) -> None:
    order_intent_id, position_request = prepare_position_reconciled_intent(database)
    request, _ = prepare_protection_request(database, order_intent_id, position_request)

    result = execute_bound_fact(
        database,
        order_intent_id,
        mutate_request(
            request,
            cumulative_filled_quantity=Decimal("0.4"),
            known_remaining_quantity=Decimal("0.1"),
        ),
    )

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "EXECUTION_FACT_CANONICAL_SEMANTICS_MISMATCH"


def test_protection_for_a_different_position_snapshot_cannot_advance(
    database: Database,
) -> None:
    order_intent_id, position_request = prepare_position_reconciled_intent(database)
    alternate_draft = fact_request(
        sequence=3,
        status="POSITION_RECONCILED",
        filled=Decimal("0.5"),
        remaining=Decimal("0"),
        terminal=True,
        reconciled=True,
    )
    _, run, _, _, alternate_context = prepare_active_fact_run(
        database,
        order_intent_id,
        ReconciliationSourceType.VENUE_POSITIONS,
        now=position_request.received_at + timedelta(milliseconds=2),
        draft=alternate_draft,
    )
    assert alternate_context is not None
    assert isinstance(alternate_context.fact, VenuePositionSnapshot)
    alternate_position_id = alternate_context.fact.venue_position_snapshot_id
    finish_run(
        database,
        run.run_id,
        now=alternate_context.input_link.received_at + timedelta(milliseconds=1),
    )
    request, _ = prepare_protection_request(
        database,
        order_intent_id,
        position_request,
        protection_position_snapshot_id=alternate_position_id,
    )

    result = execute_bound_fact(database, order_intent_id, request)

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "EXECUTION_FACT_CANONICAL_PROTECTION_MISMATCH"


def test_protection_request_event_cannot_diverge_from_canonical_snapshot(
    database: Database,
) -> None:
    order_intent_id, position_request = prepare_position_reconciled_intent(database)
    request, _ = prepare_protection_request(database, order_intent_id, position_request)

    result = execute_bound_fact(
        database,
        order_intent_id,
        mutate_request(request, event_time=request.event_time - timedelta(microseconds=1)),
    )

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "EXECUTION_FACT_CANONICAL_SEMANTICS_MISMATCH"


@pytest.mark.parametrize("target_status", ["COMPLETED", "FAILED_SAFE"])
def test_protection_source_cannot_self_report_terminal_statuses(
    database: Database,
    target_status: str,
) -> None:
    order_intent_id, position_request = prepare_position_reconciled_intent(database)
    request, _ = prepare_protection_request(database, order_intent_id, position_request)

    result = execute_bound_fact(
        database,
        order_intent_id,
        mutate_request(request, target_status=target_status),
    )

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "EXECUTION_FACT_SOURCE_STATUS_MISMATCH"


def test_direct_database_insert_cannot_promote_degraded_protection(
    database: Database,
) -> None:
    order_intent_id, position_request = prepare_position_reconciled_intent(database)
    request, _ = prepare_protection_request(
        database,
        order_intent_id,
        position_request,
        protection_snapshot_overrides={"protection_state": VenueProtectionState.DEGRADED},
    )
    forged = fact_row(order_intent_id, request, recorded_at=request.received_at)

    with pytest.raises(DBAPIError, match="canonical protection coverage is invalid"):
        with database.session_factory.begin() as session:
            session.add(forged)
            session.flush()
    with database.session_factory.begin() as session:
        state = session.get(OrderIntentState, order_intent_id)
        assert state is not None and state.status == "POSITION_RECONCILED"
