from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from tests.integration.test_execution import (
    create_intent_envelope,
    execute_create,
    prepare_authorization,
    seed_execution_policy,
)
from tests.reconciliation_fixtures import complete_successful_reconciliation
from tests.sender_fencing_fixtures import (
    WORKER_ID,
    acquire_envelope,
    claim_envelope,
    execute_acquire,
    execute_claim,
    execute_renew,
    execute_tighten,
    make_sender_scope,
    renew_envelope,
    tighten_envelope,
)
from trading_control_plane.capability_certificate_models import CapabilityCertificate
from trading_control_plane.capability_certificates import CapabilityScope
from trading_control_plane.commands import CommandStatus
from trading_control_plane.database import Database
from trading_control_plane.execution_models import OrderIntent, OrderIntentState
from trading_control_plane.models import AuditEvent, CapabilityGate, OutboxMessage
from trading_control_plane.reconciliation_models import ExecutionReconciliationRunState
from trading_control_plane.sender_fencing import (
    SenderLeaseAction,
    SenderLeaseValidationRequest,
    SenderLeaseValidator,
    SenderScopeBinding,
    sender_scope_id,
)
from trading_control_plane.sender_fencing_models import (
    ExecutionSenderLease,
    ExecutionSenderScope,
    ExecutionSenderScopeState,
    ExecutionSenderScopeStateHistory,
    ShadowDispatchClaim,
)

pytestmark = pytest.mark.integration


def count_rows(session: Session, model: type[object]) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def prepare_shadow_intent(
    database: Database, now: datetime
) -> tuple[OrderIntent, SenderScopeBinding]:
    proposal, campaign, initial = prepare_authorization(database)
    seed_execution_policy(database, now)
    created = execute_create(database, create_intent_envelope(proposal, campaign, initial, now=now))
    assert created.status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        intent = session.execute(select(OrderIntent)).scalar_one()
        certificate = session.get(CapabilityCertificate, intent.capability_certificate_ref)
        assert certificate is not None
        certificate_scope = CapabilityScope.model_validate(certificate.scope)
    return intent, make_sender_scope(
        organization_id="org-1",
        venue=certificate_scope.venue,
        execution_domain=certificate_scope.execution_domain,
        account_id=certificate_scope.account_id,
        account_abstraction=certificate_scope.account_abstraction,
        position_mode=certificate_scope.position_mode,
        margin_mode=certificate_scope.margin_mode,
        collateral_scope=certificate_scope.collateral_scope,
        collateral_pool_id=certificate_scope.collateral_pool_id,
    )


def test_acquire_persists_one_immutable_shadow_authority_and_replays_idempotently(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    scope = make_sender_scope()
    envelope = acquire_envelope(scope, now=now, idempotency_key="sender-acquire-replay")

    first = execute_acquire(database, envelope, now=now)
    replay = execute_acquire(database, envelope, now=now)

    assert first.status is CommandStatus.COMPLETED
    assert replay.status is CommandStatus.ALREADY_PROCESSED
    assert first.data["environment"] == "SHADOW"
    assert first.data["live_dispatch_eligible"] is False
    assert first.data["fencing_token"] == 1
    with database.session_factory.begin() as session:
        sender_scope = session.execute(select(ExecutionSenderScope)).scalar_one()
        lease = session.execute(select(ExecutionSenderLease)).scalar_one()
        state = session.execute(select(ExecutionSenderScopeState)).scalar_one()
        assert sender_scope.live_dispatch_eligible is False
        assert lease.live_dispatch_eligible is False
        assert state.status == "LEASED"
        assert state.active_lease_id == lease.lease_id
        assert state.current_fencing_token == 1
        assert count_rows(session, ExecutionSenderScopeStateHistory) == 1
        assert count_rows(session, ShadowDispatchClaim) == 0
        gates = tuple(session.execute(select(CapabilityGate.status)).scalars())
        assert set(gates) == {"DISABLED"}
        assert count_rows(session, AuditEvent) == 1
        assert count_rows(session, OutboxMessage) == 1

    with pytest.raises(DBAPIError, match="execution_sender_scopes is immutable"):
        with database.session_factory.begin() as session:
            session.execute(update(ExecutionSenderScope).values(scope_hash="0" * 64))
    with pytest.raises(DBAPIError, match="execution_sender_leases is immutable"):
        with database.session_factory.begin() as session:
            session.execute(update(ExecutionSenderLease).values(owner_worker_id="tampered"))
    with pytest.raises(DBAPIError, match="execution_sender_scope_state_history is immutable"):
        with database.session_factory.begin() as session:
            session.execute(update(ExecutionSenderScopeStateHistory).values(reason_code="tampered"))


def test_concurrent_workers_cannot_both_acquire_same_exact_scope(database: Database) -> None:
    now = datetime.now(UTC)
    scope = make_sender_scope()
    envelopes = (
        acquire_envelope(scope, now=now, owner_worker_id="worker-a"),
        acquire_envelope(scope, now=now, owner_worker_id="worker-b"),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda item: execute_acquire(database, item, now=now), envelopes))

    assert [result.status for result in results].count(CommandStatus.COMPLETED) == 1
    assert [result.status for result in results].count(CommandStatus.REJECTED) == 1
    rejected = next(result for result in results if result.status is CommandStatus.REJECTED)
    assert rejected.error_code in {"SENDER_ALREADY_ACTIVE", "VERSION_CONFLICT"}
    with database.session_factory.begin() as session:
        assert count_rows(session, ExecutionSenderScope) == 1
        assert count_rows(session, ExecutionSenderLease) == 1
        state = session.execute(select(ExecutionSenderScopeState)).scalar_one()
        assert state.status == "LEASED"
        assert state.current_fencing_token == 1


def test_renew_fence_and_reacquire_advance_monotonic_token_and_invalidate_old_owner(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    scope = make_sender_scope()
    first_lease_id = uuid4()
    acquired = execute_acquire(
        database,
        acquire_envelope(scope, now=now, lease_id=first_lease_id),
        now=now,
    )
    assert acquired.data["fencing_token"] == 1

    renewed_at = now + timedelta(seconds=2)
    renewed = execute_renew(
        database,
        renew_envelope(
            scope,
            first_lease_id,
            1,
            owner_worker_id=WORKER_ID,
            now=renewed_at,
            expected_version=1,
            ttl_seconds=30,
        ),
        now=renewed_at,
    )
    assert renewed.status is CommandStatus.COMPLETED
    assert renewed.object_version == 2
    assert renewed.data["fencing_token"] == 1

    fenced_at = now + timedelta(seconds=3)
    fenced = execute_tighten(
        database,
        tighten_envelope(
            scope,
            action=SenderLeaseAction.FENCE,
            now=fenced_at,
            expected_version=2,
            lease_id=first_lease_id,
            fencing_token=1,
        ),
        now=fenced_at,
    )
    assert fenced.status is CommandStatus.COMPLETED
    assert fenced.data["status"] == "FENCED"
    assert fenced.data["fencing_token"] == 2

    with database.session_factory.begin() as session:
        invalid = SenderLeaseValidator().validate(
            session,
            SenderLeaseValidationRequest(
                scope=scope,
                lease_id=first_lease_id,
                fencing_token=1,
                owner_worker_id=WORKER_ID,
                worker_config_hash="a" * 64,
                credential_fingerprint="b" * 64,
                validation_time=fenced_at,
            ),
        )
        assert invalid.valid is False
        assert "SENDER_LEASE_INACTIVE" in invalid.reason_codes

    second_lease_id = uuid4()
    reacquired_at = now + timedelta(seconds=4)
    reacquired = execute_acquire(
        database,
        acquire_envelope(
            scope,
            now=reacquired_at,
            lease_id=second_lease_id,
            expected_version=3,
        ),
        now=reacquired_at,
    )
    assert reacquired.status is CommandStatus.COMPLETED
    assert reacquired.object_version == 4
    assert reacquired.data["fencing_token"] == 3

    stale_renew = execute_renew(
        database,
        renew_envelope(
            scope,
            first_lease_id,
            1,
            owner_worker_id=WORKER_ID,
            now=now + timedelta(seconds=5),
            expected_version=4,
        ),
        now=now + timedelta(seconds=5),
    )
    assert stale_renew.status is CommandStatus.REJECTED
    assert stale_renew.error_code == "SENDER_LEASE_INACTIVE"
    released_at = now + timedelta(seconds=6)
    released = execute_tighten(
        database,
        tighten_envelope(
            scope,
            action=SenderLeaseAction.RELEASE,
            now=released_at,
            expected_version=4,
            lease_id=second_lease_id,
            fencing_token=3,
        ),
        now=released_at,
    )
    assert released.status is CommandStatus.COMPLETED
    assert released.data["status"] == "UNOWNED"
    assert released.data["fencing_token"] == 4
    with database.session_factory.begin() as session:
        state = session.execute(select(ExecutionSenderScopeState)).scalar_one()
        assert state.active_lease_id is None
        assert state.current_fencing_token == 4
        assert count_rows(session, ExecutionSenderScopeStateHistory) == 5


def test_clock_skew_and_early_expiry_fail_closed_then_expiry_fences_token(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    skewed_scope = make_sender_scope(account_id="clock-skew-account")
    skewed = execute_acquire(
        database,
        acquire_envelope(
            skewed_scope,
            now=now,
            worker_observed_at=now + timedelta(seconds=6),
        ),
        now=now,
    )
    assert skewed.status is CommandStatus.REJECTED
    assert skewed.error_code == "WORKER_CLOCK_SKEW_EXCEEDED"

    scope = make_sender_scope()
    lease_id = uuid4()
    acquired = execute_acquire(
        database,
        acquire_envelope(
            scope,
            now=now,
            lease_id=lease_id,
            ttl_seconds=5,
            max_lifetime_seconds=10,
        ),
        now=now,
    )
    assert acquired.status is CommandStatus.COMPLETED
    early = execute_tighten(
        database,
        tighten_envelope(
            scope,
            action=SenderLeaseAction.EXPIRE,
            now=now + timedelta(seconds=1),
            expected_version=1,
            lease_id=lease_id,
            fencing_token=1,
        ),
        now=now + timedelta(seconds=1),
    )
    assert early.status is CommandStatus.REJECTED
    assert early.error_code == "SENDER_LEASE_NOT_EXPIRED"

    expired = execute_tighten(
        database,
        tighten_envelope(
            scope,
            action=SenderLeaseAction.EXPIRE,
            now=now + timedelta(seconds=5),
            expected_version=1,
            lease_id=lease_id,
            fencing_token=1,
        ),
        now=now + timedelta(seconds=5),
    )
    assert expired.status is CommandStatus.COMPLETED
    assert expired.data["status"] == "UNOWNED"
    assert expired.data["fencing_token"] == 2
    with database.session_factory.begin() as session:
        assert count_rows(session, ExecutionSenderScope) == 1


def test_shadow_claim_requires_closed_live_gate_current_lease_and_active_certificate(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    intent, scope = prepare_shadow_intent(database, now)
    lease_id = uuid4()
    lease_time = now + timedelta(seconds=1)
    acquired = execute_acquire(
        database,
        acquire_envelope(scope, now=lease_time, lease_id=lease_id),
        now=lease_time,
    )
    assert acquired.status is CommandStatus.COMPLETED

    claim_time = now + timedelta(seconds=2)
    unreconciled = execute_claim(
        database,
        claim_envelope(
            scope,
            intent.order_intent_id,
            lease_id,
            1,
            uuid4(),
            now=claim_time,
        ),
        now=claim_time,
    )
    assert unreconciled.status is CommandStatus.REJECTED
    assert unreconciled.error_code == "RECONCILIATION_SUCCESS_REQUIRED"
    reconciliation_run_id = complete_successful_reconciliation(
        database,
        scope,
        lease_id,
        1,
        now=now + timedelta(milliseconds=1500),
    )
    with database.session_factory.begin() as session:
        gate = session.get(CapabilityGate, "LIVE_ORDER_SEND")
        assert gate is not None
        gate.status = "SHADOW"
        gate.version += 1
    blocked = execute_claim(
        database,
        claim_envelope(
            scope,
            intent.order_intent_id,
            lease_id,
            1,
            reconciliation_run_id,
            now=claim_time,
        ),
        now=claim_time,
    )
    assert blocked.status is CommandStatus.REJECTED
    assert blocked.error_code == "LIVE_ORDER_SEND_NOT_DISABLED"

    with database.session_factory.begin() as session:
        gate = session.get(CapabilityGate, "LIVE_ORDER_SEND")
        assert gate is not None
        gate.status = "DISABLED"
        gate.version += 1
    envelope = claim_envelope(
        scope,
        intent.order_intent_id,
        lease_id,
        1,
        reconciliation_run_id,
        now=claim_time,
        idempotency_key="shadow-claim-replay",
    )
    claimed = execute_claim(database, envelope, now=claim_time)
    replay = execute_claim(database, envelope, now=claim_time)
    assert claimed.status is CommandStatus.COMPLETED
    assert replay.status is CommandStatus.ALREADY_PROCESSED
    assert claimed.data["execution_mode"] == "SHADOW"
    assert claimed.data["external_send_permitted"] is False
    assert claimed.data["live_gate_status"] == "DISABLED"
    assert claimed.data["order_intent_status"] == "INTENT_CREATED"

    duplicate = execute_claim(
        database,
        claim_envelope(
            scope,
            intent.order_intent_id,
            lease_id,
            1,
            reconciliation_run_id,
            now=claim_time + timedelta(milliseconds=1),
        ),
        now=claim_time + timedelta(milliseconds=1),
    )
    assert duplicate.status is CommandStatus.REJECTED
    assert duplicate.error_code == "ORDER_INTENT_ALREADY_CLAIMED"
    with database.session_factory.begin() as session:
        claim = session.execute(select(ShadowDispatchClaim)).scalar_one()
        state = session.get(OrderIntentState, intent.order_intent_id)
        assert claim.external_send_permitted is False
        assert claim.live_gate_status == "DISABLED"
        assert claim.reason_code == "TEST_SHADOW_DISPATCH_CLAIM"
        assert state is not None and state.status == "INTENT_CREATED"
        assert count_rows(session, ShadowDispatchClaim) == 1

    with pytest.raises(DBAPIError, match="shadow_dispatch_claims is immutable"):
        with database.session_factory.begin() as session:
            session.execute(update(ShadowDispatchClaim).values(external_send_permitted=True))


def test_database_rejects_non_monotonic_sender_state_and_cross_scope_claim(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    intent, scope = prepare_shadow_intent(database, now)
    lease_id = uuid4()
    acquired_at = now + timedelta(seconds=1)
    execute_acquire(
        database,
        acquire_envelope(scope, now=acquired_at, lease_id=lease_id),
        now=acquired_at,
    )
    reconciliation_run_id = complete_successful_reconciliation(
        database,
        scope,
        lease_id,
        1,
        now=now + timedelta(milliseconds=1500),
    )
    with pytest.raises(DBAPIError, match="invalid sender scope identity, version, time, or token"):
        with database.session_factory.begin() as session:
            state = session.execute(select(ExecutionSenderScopeState)).scalar_one()
            state.version += 1
            state.current_fencing_token = 0
            state.updated_at = now + timedelta(seconds=2)

    with pytest.raises(
        DBAPIError, match="shadow dispatch claim violates current fenced non-dispatch contract"
    ):
        with database.session_factory.begin() as session:
            lease = session.get(ExecutionSenderLease, lease_id)
            state = session.execute(select(ExecutionSenderScopeState)).scalar_one()
            certificate = session.get(CapabilityCertificate, intent.capability_certificate_ref)
            reconciliation_state = session.get(
                ExecutionReconciliationRunState, reconciliation_run_id
            )
            sender_scope = session.get(ExecutionSenderScope, sender_scope_id(scope))
            assert lease is not None
            assert state.lease_expires_at is not None
            assert certificate is not None
            assert reconciliation_state is not None
            assert reconciliation_state.result_hash is not None
            assert sender_scope is not None
            session.add(
                ShadowDispatchClaim(
                    claim_id=uuid4(),
                    organization_id="org-1",
                    order_intent_id=intent.order_intent_id,
                    scope_id=sender_scope.scope_id,
                    lease_id=lease.lease_id,
                    fencing_token=lease.fencing_token,
                    client_order_id=f"shadow-forged-{uuid4()}",
                    owner_worker_id="forged-worker",
                    worker_config_hash=lease.worker_config_hash,
                    credential_fingerprint=lease.credential_fingerprint,
                    capability_certificate_ref=certificate.certificate_id,
                    reconciliation_run_id=reconciliation_run_id,
                    reconciliation_result_hash=reconciliation_state.result_hash,
                    execution_mode="SHADOW",
                    external_send_permitted=False,
                    live_gate_status="DISABLED",
                    intent_snapshot_hash=intent.intent_snapshot_hash,
                    capability_certificate_hash=certificate.certificate_hash,
                    scope_hash=sender_scope.scope_hash,
                    lease_hash=lease.lease_hash,
                    lease_expires_at=state.lease_expires_at,
                    worker_observed_at=now + timedelta(seconds=2),
                    claimed_at=now + timedelta(seconds=2),
                    reason_code="FORGED_CROSS_OWNER_CLAIM",
                    claim_hash="0" * 64,
                )
            )

    other_scope = make_sender_scope(account_id="other-account")
    wrong_claim = execute_claim(
        database,
        claim_envelope(
            other_scope,
            intent.order_intent_id,
            lease_id,
            1,
            reconciliation_run_id,
            now=now + timedelta(seconds=2),
        ),
        now=now + timedelta(seconds=2),
    )
    assert wrong_claim.status is CommandStatus.REJECTED
    assert wrong_claim.error_code in {"SENDER_SCOPE_NOT_FOUND", "SENDER_LEASE_INTEGRITY_FAILED"}
    with database.session_factory.begin() as session:
        assert count_rows(session, ShadowDispatchClaim) == 0
