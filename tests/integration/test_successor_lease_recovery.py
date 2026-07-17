from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from tests.integration.test_execution import (
    bind_fact_request,
    fact_request,
    prepare_active_fact_run,
)
from tests.integration.test_execution_fact_binding import (
    create_order_intent,
    execute_bound_fact,
    finish_run,
    prepare_bound_request,
)
from tests.sender_fencing_fixtures import (
    acquire_envelope,
    execute_acquire,
    execute_tighten,
    make_sender_scope,
    tighten_envelope,
)
from trading_control_plane.commands import CommandStatus
from trading_control_plane.database import Database
from trading_control_plane.execution_models import ExecutionFact
from trading_control_plane.reconciliation import ReconciliationSourceType
from trading_control_plane.sender_fencing import SenderLeaseAction
from trading_control_plane.sender_fencing_models import (
    ExecutionSenderScopeState,
    ShadowDispatchClaim,
)

pytestmark = pytest.mark.integration


def test_successor_lease_reconciles_late_fact_for_original_claim(database: Database) -> None:
    order_intent_id = create_order_intent(database)
    original_request, original_run_id = prepare_bound_request(database, order_intent_id)
    finish_run(
        database,
        original_run_id,
        now=original_request.received_at + timedelta(milliseconds=1),
    )
    with database.session_factory.begin() as session:
        original_claim = session.get(ShadowDispatchClaim, original_request.shadow_dispatch_claim_id)
        sender_state = session.execute(select(ExecutionSenderScopeState)).scalar_one()
        assert original_claim is not None
        original_lease_id = original_claim.lease_id
        original_token = original_claim.fencing_token
        state_version = sender_state.version

    scope = make_sender_scope()
    fenced_at = original_request.received_at + timedelta(milliseconds=2)
    fenced = execute_tighten(
        database,
        tighten_envelope(
            scope,
            action=SenderLeaseAction.FENCE,
            now=fenced_at,
            expected_version=state_version,
            lease_id=original_lease_id,
            fencing_token=original_token,
        ),
        now=fenced_at,
    )
    assert fenced.status is CommandStatus.COMPLETED

    successor_lease_id = uuid4()
    reacquired_at = fenced_at + timedelta(milliseconds=1)
    reacquired = execute_acquire(
        database,
        acquire_envelope(
            scope,
            now=reacquired_at,
            lease_id=successor_lease_id,
            expected_version=fenced.object_version,
            ttl_seconds=300,
            max_lifetime_seconds=600,
        ),
        now=reacquired_at,
    )
    assert reacquired.status is CommandStatus.COMPLETED
    assert int(reacquired.data["fencing_token"]) > original_token

    late_fill = fact_request(
        sequence=1,
        status="PARTIALLY_FILLED",
        filled=Decimal("0.2"),
        remaining=Decimal("0.3"),
    )
    claim, run, reconciliation_input, event_time, canonical_context = prepare_active_fact_run(
        database,
        order_intent_id,
        ReconciliationSourceType.VENUE_FILLS,
        now=reacquired_at + timedelta(milliseconds=1),
        draft=late_fill,
    )
    assert canonical_context is not None
    request = bind_fact_request(
        late_fill,
        claim,
        run,
        reconciliation_input,
        event_time=event_time,
        received_at=canonical_context.input_link.received_at,
        canonical_context=canonical_context,
    )

    result = execute_bound_fact(database, order_intent_id, request)
    replay = execute_bound_fact(database, order_intent_id, request)

    assert result.status is CommandStatus.COMPLETED
    assert result.data["authority_mode"] == "SUCCESSOR_LEASE"
    assert replay.status is CommandStatus.COMPLETED
    assert replay.data["already_recorded"] is True
    assert replay.data["authority_mode"] == "SUCCESSOR_LEASE"
    with database.session_factory.begin() as session:
        fact = session.execute(select(ExecutionFact)).scalar_one()
        current_state = session.execute(select(ExecutionSenderScopeState)).scalar_one()
        assert fact.shadow_dispatch_claim_id == original_claim.claim_id
        assert fact.reconciliation_run_id == run.run_id
        assert run.lease_id == successor_lease_id
        assert run.fencing_token == int(reacquired.data["fencing_token"])
        assert current_state.active_lease_id == successor_lease_id
        assert current_state.current_fencing_token == run.fencing_token
    finish_run(database, run.run_id, now=request.received_at + timedelta(milliseconds=1))


def test_original_terminal_run_cannot_be_reused_after_successor_takeover(
    database: Database,
) -> None:
    order_intent_id = create_order_intent(database)
    original_request, original_run_id = prepare_bound_request(database, order_intent_id)
    finish_run(
        database,
        original_run_id,
        now=original_request.received_at + timedelta(milliseconds=1),
    )
    with database.session_factory.begin() as session:
        sender_state = session.execute(select(ExecutionSenderScopeState)).scalar_one()
        original_claim = session.get(ShadowDispatchClaim, original_request.shadow_dispatch_claim_id)
        assert original_claim is not None

    scope = make_sender_scope()
    fenced_at = original_request.received_at + timedelta(milliseconds=2)
    fenced = execute_tighten(
        database,
        tighten_envelope(
            scope,
            action=SenderLeaseAction.FENCE,
            now=fenced_at,
            expected_version=sender_state.version,
            lease_id=original_claim.lease_id,
            fencing_token=original_claim.fencing_token,
        ),
        now=fenced_at,
    )
    assert fenced.status is CommandStatus.COMPLETED
    reacquired_at = fenced_at + timedelta(milliseconds=1)
    successor = execute_acquire(
        database,
        acquire_envelope(
            scope,
            now=reacquired_at,
            lease_id=uuid4(),
            expected_version=fenced.object_version,
            ttl_seconds=300,
            max_lifetime_seconds=600,
        ),
        now=reacquired_at,
    )
    assert successor.status is CommandStatus.COMPLETED

    stale = execute_bound_fact(database, order_intent_id, original_request)

    assert stale.status is CommandStatus.REJECTED
    assert stale.error_code == "EXECUTION_FACT_RECONCILIATION_RUN_NOT_ACTIVE"
    with database.session_factory.begin() as session:
        assert session.execute(select(ExecutionFact)).scalar_one_or_none() is None
