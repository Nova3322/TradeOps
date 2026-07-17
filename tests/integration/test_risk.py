from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from tests.risk_fixtures import make_policy, make_request
from trading_control_plane.authorization import SystemRiskState
from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import (
    CommandChannel,
    CommandEnvelope,
    CommandRejected,
    CommandStatus,
    hash_json,
)
from trading_control_plane.database import Database
from trading_control_plane.models import AuditEvent, OutboxMessage
from trading_control_plane.proposal_models import SystemRiskStateRecord
from trading_control_plane.risk import (
    RiskPrecheckRequest,
    RiskPrecheckService,
    SystemRiskStateCommandService,
    SystemRiskStateService,
)
from trading_control_plane.risk_models import (
    RiskDecisionSnapshot,
    RiskPolicyRecord,
    SystemRiskStateTransition,
)

pytestmark = pytest.mark.integration


def count_rows(session: Session, model: type[object]) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def seed_policy(
    database: Database,
    *,
    now: datetime | None = None,
    tampered_hash: bool = False,
) -> UUID:
    created_at = now or datetime.now(UTC)
    policy_id = uuid4()
    parameters = make_policy().model_dump(mode="json")
    with database.session_factory.begin() as session:
        session.add(
            RiskPolicyRecord(
                risk_policy_id=policy_id,
                organization_id="org-1",
                policy_version="risk-shadow-test-v1",
                policy_mode="SHADOW",
                parameters=parameters,
                policy_hash="0" * 64 if tampered_hash else hash_json(parameters),
                evidence_refs=["test-only:risk-policy-fixture"],
                valid_from=created_at - timedelta(days=1),
                valid_until=created_at + timedelta(days=1),
                created_at=created_at,
            )
        )
    return policy_id


def seed_state(
    database: Database,
    *,
    status: SystemRiskState = SystemRiskState.NORMAL,
) -> None:
    with database.session_factory.begin() as session:
        session.add(
            SystemRiskStateRecord(
                organization_id="org-1",
                status=status.value,
                version=1,
                reason_code="INTEGRATION_INITIAL_STATE",
                policy_version="risk-state-test-v1",
                transition_source_ref="test-only:initial-state",
                updated_at=datetime.now(UTC),
            )
        )


def risk_envelope(request: RiskPrecheckRequest) -> CommandEnvelope:
    now = datetime.now(UTC)
    return CommandEnvelope(
        idempotency_key=f"risk-precheck-{uuid4()}",
        command_type="risk.precheck.evaluate.v1",
        object_type="ProposalCandidate",
        object_id=request.proposal_ref,
        expected_version=request.candidate_version,
        service_principal="trading:proposal-test",
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": request.organization_id},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref="test-only:risk-precheck-auth",
        payload_schema_version=1,
        reason="evaluate proposal candidate",
        payload=request.model_dump(mode="json"),
    )


def execute_precheck(database: Database, envelope: CommandEnvelope):
    return IdempotentCommandExecutor(database.session_factory).execute(
        envelope,
        RiskPrecheckService().evaluate,
    )


def state_envelope(
    *,
    target: SystemRiskState,
    expected_version: int = 1,
) -> CommandEnvelope:
    now = datetime.now(UTC)
    return CommandEnvelope(
        idempotency_key=f"risk-state-{uuid4()}",
        command_type="risk.state.tighten.v1",
        object_type="SystemRiskState",
        object_id="org-1",
        expected_version=expected_version,
        service_principal="risk-monitor:test",
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": "org-1"},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref="test-only:risk-monitor-auth",
        payload_schema_version=1,
        reason="tighten automatic risk state",
        payload={
            "organization_id": "org-1",
            "target_status": target.value,
            "reason_code": f"TEST_{target.value}",
            "policy_version": "risk-state-test-v1",
            "source_ref": f"monitor:{target.value.lower()}",
        },
    )


def test_allow_precheck_persists_immutable_snapshot_audit_and_outbox(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    seed_policy(database, now=now)
    seed_state(database)

    result = execute_precheck(database, risk_envelope(make_request(now=now)))

    assert result.status is CommandStatus.COMPLETED
    assert result.data["result"] == "ALLOW"
    assert result.data["execution_eligible"] is False
    assert result.data["reservation_created"] is False
    with database.session_factory.begin() as session:
        snapshot = session.execute(select(RiskDecisionSnapshot)).scalar_one()
        assert snapshot.current_portfolio_mtm_equity == Decimal("100000")
        assert snapshot.current_unrealized_pnl == 0
        assert snapshot.one_r_0 == Decimal("500")
        assert snapshot.frozen_trade_loss_cap == Decimal("500")
        assert snapshot.dynamic_trade_loss_cap == Decimal("500")
        assert snapshot.execution_eligible is False
        assert snapshot.reservation_created is False
        assert hash_json(snapshot.input_snapshot) == snapshot.input_hash
        assert hash_json(snapshot.decision) == snapshot.decision_hash
        assert count_rows(session, AuditEvent) == 1
        assert count_rows(session, OutboxMessage) == 1
        assert (
            session.execute(text("SELECT to_regclass('public.risk_reservations')")).scalar_one()
            is None
        )
        assert (
            session.execute(text("SELECT to_regclass('public.order_intents')")).scalar_one() is None
        )


def test_stale_precheck_is_durable_deny_not_silent_failure(database: Database) -> None:
    now = datetime.now(UTC)
    seed_policy(database, now=now)
    seed_state(database)

    result = execute_precheck(
        database,
        risk_envelope(make_request(now=now, fact_age=timedelta(seconds=6))),
    )

    assert result.status is CommandStatus.COMPLETED
    assert result.data["result"] == "DENY"
    assert result.data["primary_reason_code"] == "FACTS_STALE"
    assert result.data["max_safe_quantity"] == "0"
    with database.session_factory.begin() as session:
        snapshot = session.execute(select(RiskDecisionSnapshot)).scalar_one()
        assert snapshot.result == "DENY"
        assert snapshot.valid_until == snapshot.decided_at


def test_missing_system_state_is_persisted_as_fail_closed_unknown(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    seed_policy(database, now=now)

    result = execute_precheck(database, risk_envelope(make_request(now=now)))

    assert result.data["result"] == "DENY"
    assert result.data["primary_reason_code"] == "SYSTEM_RISK_STATE_DENY"
    with database.session_factory.begin() as session:
        snapshot = session.execute(select(RiskDecisionSnapshot)).scalar_one()
        assert snapshot.system_risk_state == "UNKNOWN"


def test_idempotent_replay_never_creates_second_risk_snapshot(database: Database) -> None:
    now = datetime.now(UTC)
    seed_policy(database, now=now)
    seed_state(database)
    envelope = risk_envelope(make_request(now=now))

    first = execute_precheck(database, envelope)
    replay = execute_precheck(database, envelope)

    assert first.status is CommandStatus.COMPLETED
    assert replay.status is CommandStatus.ALREADY_PROCESSED
    assert replay.replayed is True
    with database.session_factory.begin() as session:
        assert count_rows(session, RiskDecisionSnapshot) == 1
        assert count_rows(session, AuditEvent) == 1
        assert count_rows(session, OutboxMessage) == 1


def test_missing_or_tampered_policy_fails_before_any_allow_snapshot(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    seed_state(database)
    request = make_request(now=now)

    missing = execute_precheck(database, risk_envelope(request))
    seed_policy(database, now=now, tampered_hash=True)
    tampered = execute_precheck(database, risk_envelope(request))

    assert missing.status is CommandStatus.REJECTED
    assert missing.error_code == "RISK_POLICY_UNAVAILABLE"
    assert tampered.status is CommandStatus.REJECTED
    assert tampered.error_code == "RISK_POLICY_INTEGRITY_FAILED"
    with database.session_factory.begin() as session:
        assert count_rows(session, RiskDecisionSnapshot) == 0


def test_web_actor_cannot_call_internal_risk_precheck_handler_directly(
    database: Database,
) -> None:
    request = make_request()
    internal = risk_envelope(request)
    direct = internal.model_copy(
        update={
            "actor_id": str(uuid4()),
            "service_principal": None,
            "channel": CommandChannel.WEB,
        }
    )

    result = execute_precheck(database, direct)

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "INTERNAL_SERVICE_REQUIRED"
    with database.session_factory.begin() as session:
        assert count_rows(session, RiskDecisionSnapshot) == 0


def test_policy_decision_and_state_history_are_database_immutable(database: Database) -> None:
    now = datetime.now(UTC)
    policy_id = seed_policy(database, now=now)
    seed_state(database)
    execute_precheck(database, risk_envelope(make_request(now=now)))

    with pytest.raises(DBAPIError, match="risk_policies is immutable"):
        with database.session_factory.begin() as session:
            session.execute(
                update(RiskPolicyRecord)
                .where(RiskPolicyRecord.risk_policy_id == policy_id)
                .values(policy_version="mutated")
            )
    with pytest.raises(DBAPIError, match="risk_decision_snapshots is immutable"):
        with database.session_factory.begin() as session:
            session.execute(update(RiskDecisionSnapshot).values(result="DENY"))
    with pytest.raises(DBAPIError, match="append-only"):
        with database.session_factory.begin() as session:
            session.execute(update(SystemRiskStateTransition).values(reason_code="mutated"))


def test_system_risk_state_only_tightens_and_records_every_transition(
    database: Database,
) -> None:
    seed_state(database)
    service = SystemRiskStateService()
    with database.session_factory.begin() as session:
        first = service.tighten(
            session,
            organization_id="org-1",
            target_status=SystemRiskState.NO_PYRAMID,
            reason_code="STALE_MARKET_DATA",
            policy_version="risk-state-test-v1",
            source_ref="monitor:test-1",
        )
        second = service.tighten(
            session,
            organization_id="org-1",
            target_status=SystemRiskState.NO_NEW_POSITION,
            reason_code="ACCOUNT_RECONCILIATION_FAILED",
            policy_version="risk-state-test-v1",
            source_ref="monitor:test-2",
        )

    assert first.changed is True
    assert second.current_status is SystemRiskState.NO_NEW_POSITION
    assert second.version == 3
    with database.session_factory.begin() as session:
        transitions = (
            session.execute(
                select(SystemRiskStateTransition).order_by(SystemRiskStateTransition.state_version)
            )
            .scalars()
            .all()
        )
        assert [item.transition_kind for item in transitions] == [
            "INITIAL",
            "AUTOMATIC_TIGHTEN",
            "AUTOMATIC_TIGHTEN",
        ]
        assert [item.to_status for item in transitions] == [
            "NORMAL",
            "NO_PYRAMID",
            "NO_NEW_POSITION",
        ]

    with pytest.raises(
        CommandRejected,
        match="automatic risk state recovery is forbidden",
    ):
        with database.session_factory.begin() as session:
            service.tighten(
                session,
                organization_id="org-1",
                target_status=SystemRiskState.NO_PYRAMID,
                reason_code="UNSAFE_RECOVERY_ATTEMPT",
                policy_version="risk-state-test-v1",
                source_ref="monitor:test-3",
            )
    with pytest.raises(DBAPIError, match="invalid automatic system_risk_state transition"):
        with database.session_factory.begin() as session:
            session.execute(
                update(SystemRiskStateRecord)
                .where(SystemRiskStateRecord.organization_id == "org-1")
                .values(status="NORMAL", version=4)
            )


def test_state_tightening_command_atomically_writes_history_audit_and_outbox(
    database: Database,
) -> None:
    seed_state(database)
    envelope = state_envelope(target=SystemRiskState.NO_PYRAMID)
    executor = IdempotentCommandExecutor(database.session_factory)

    result = executor.execute(envelope, SystemRiskStateCommandService().tighten)
    replay = executor.execute(envelope, SystemRiskStateCommandService().tighten)

    assert result.status is CommandStatus.COMPLETED
    assert result.object_version == 2
    assert result.data["current_status"] == "NO_PYRAMID"
    assert replay.status is CommandStatus.ALREADY_PROCESSED
    with database.session_factory.begin() as session:
        assert count_rows(session, SystemRiskStateTransition) == 2
        assert count_rows(session, AuditEvent) == 1
        assert count_rows(session, OutboxMessage) == 1


def test_state_tightening_command_rejects_stale_expected_version(
    database: Database,
) -> None:
    seed_state(database)
    envelope = state_envelope(
        target=SystemRiskState.NO_NEW_POSITION,
        expected_version=2,
    )

    result = IdempotentCommandExecutor(database.session_factory).execute(
        envelope,
        SystemRiskStateCommandService().tighten,
    )

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "VERSION_CONFLICT"
    with database.session_factory.begin() as session:
        state = session.get(SystemRiskStateRecord, "org-1")
        assert state is not None
        assert (state.status, state.version) == ("NORMAL", 1)
        assert count_rows(session, SystemRiskStateTransition) == 1


def test_concurrent_automatic_tightening_cannot_end_in_weaker_state(
    database: Database,
) -> None:
    seed_state(database)

    def tighten(target: SystemRiskState) -> str:
        try:
            with database.session_factory.begin() as session:
                result = SystemRiskStateService().tighten(
                    session,
                    organization_id="org-1",
                    target_status=target,
                    reason_code=f"CONCURRENT_{target.value}",
                    policy_version="risk-state-test-v1",
                    source_ref=f"monitor:{target.value.lower()}",
                )
                return result.current_status.value
        except CommandRejected as exc:
            return exc.error_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                tighten,
                (SystemRiskState.NO_PYRAMID, SystemRiskState.REDUCE_ONLY),
            )
        )

    with database.session_factory.begin() as session:
        state = session.get(SystemRiskStateRecord, "org-1")
        assert state is not None
        assert state.status == "REDUCE_ONLY"
        transitions = session.execute(select(SystemRiskStateTransition)).scalars().all()
        assert all(
            item.to_status != "NORMAL" or item.transition_kind == "INITIAL" for item in transitions
        )
        assert len(transitions) in {2, 3}
    assert "REDUCE_ONLY" in outcomes
