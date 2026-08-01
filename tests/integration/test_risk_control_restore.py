from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select

from trading_control_plane.database import Database
from trading_control_plane.domain import (
    CapabilityStatus,
    Direction,
    DomainRejected,
    ExecutionEnvironment,
    IntentKind,
    ProposalSource,
    ReviewDecision,
    RiskPolicyChangeStatus,
    RiskTier,
    Role,
    SystemRiskState,
)
from trading_control_plane.models import (
    CapabilityGate,
    RiskControlChangeRequest,
    RiskPolicy,
    TradingAuthorization,
)
from trading_control_plane.service import TradingService

NOW = datetime(2026, 8, 1, 8, tzinfo=UTC)
SCOPE = (("SHADOW", "acct-1", "BINANCE"),)


def seed(service: TradingService) -> dict[str, UUID]:
    admin = service.bootstrap_admin("risk-admin", now=NOW)
    proposer = service.create_user("risk-proposer", admin, now=NOW)
    reviewer_one = service.create_user("risk-reviewer-1", admin, now=NOW)
    reviewer_two = service.create_user("risk-reviewer-2", admin, now=NOW)
    operator = service.create_user("risk-operator", admin, now=NOW)
    service.assign_role(proposer, Role.PROPOSER, admin, "acct-1", "BINANCE", now=NOW)
    service.assign_role(reviewer_one, Role.REVIEWER, admin, now=NOW)
    service.assign_role(reviewer_two, Role.REVIEWER, admin, now=NOW)
    service.assign_role(operator, Role.OPERATOR, admin, "acct-1", "BINANCE", now=NOW)
    instrument = service.register_instrument(
        actor_id=admin,
        venue="BINANCE",
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        lot_size=Decimal("0.001"),
        minimum_notional=Decimal("5"),
        contract_multiplier=Decimal("1"),
        quote_currency="USDT",
        collateral_currency="USDT",
        protection_supported=True,
        now=NOW,
    )
    service.set_risk_policy(
        actor_id=admin,
        version="risk-restore-v1",
        system_state=SystemRiskState.NORMAL,
        max_total_risk=Decimal("100"),
        max_fact_age=timedelta(hours=2),
        now=NOW,
    )
    service.record_position(
        "acct-1",
        "BINANCE",
        instrument,
        Decimal("0"),
        Decimal("0"),
        Decimal("100"),
        True,
        operator,
        environment=ExecutionEnvironment.SHADOW,
        now=NOW,
    )
    service.record_account_equity(
        "acct-1",
        "BINANCE",
        Decimal("10000"),
        Decimal("9000"),
        "USDT",
        True,
        operator,
        environment=ExecutionEnvironment.SHADOW,
        now=NOW,
    )
    return {
        "admin": admin,
        "proposer": proposer,
        "reviewer_one": reviewer_one,
        "reviewer_two": reviewer_two,
        "operator": operator,
        "instrument": instrument,
    }


def enable_auto_add_for_test(database: Database, admin: UUID, *, now: datetime) -> None:
    with database.session_factory.begin() as session:
        gate = session.get(CapabilityGate, "AUTO_ADD", with_for_update=True)
        assert gate is not None
        gate.status = CapabilityStatus.ENABLED.value
        gate.reason = "test fixture enables the initially closed gate"
        gate.operator_id = str(admin)
        gate.version += 1
        gate.updated_at = now


def prepare_add_proposal(service: TradingService, ids: dict[str, UUID], *, key: str) -> UUID:
    proposal_id = service.create_proposal(
        actor_id=ids["proposer"],
        source=ProposalSource.MANUAL,
        risk_tier=RiskTier.LOW,
        account_id="acct-1",
        venue="BINANCE",
        instrument_id=ids["instrument"],
        direction=Direction.LONG,
        quantity=Decimal("1"),
        max_risk=Decimal("10"),
        expires_at=NOW + timedelta(hours=2),
        idempotency_key=f"{key}-proposal",
        details={
            "allow_auto_add": True,
            "requested_adds": 1,
            "add_trigger_price": "105",
            "initial_quantity": "0.5",
            "invalidation_price": "90",
        },
        now=NOW,
    )
    service.submit_proposal(proposal_id, ids["proposer"], now=NOW)
    service.review_proposal(
        proposal_id,
        ids["reviewer_one"],
        ReviewDecision.APPROVE,
        "independent proposal review",
        now=NOW,
    )
    service.decide_risk(
        proposal_id=proposal_id,
        actor_id=ids["operator"],
        kind=IntentKind.INITIAL,
        idempotency_key=f"{key}-decision",
        now=NOW,
    )
    return proposal_id


def issue_add_authorization(service: TradingService, ids: dict[str, UUID]) -> UUID:
    proposal_id = prepare_add_proposal(service, ids, key="risk-restore")
    return service.issue_authorization(
        proposal_id=proposal_id,
        actor_id=ids["operator"],
        expires_at=NOW + timedelta(minutes=30),
        allowed_adds=1,
        idempotency_key="risk-restore-authorization",
        now=NOW,
    )


def test_pause_and_authorization_issue_serialize_fail_closed(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    proposal_id = prepare_add_proposal(service, ids, key="pause-issue-race")

    def issue() -> str:
        try:
            authorization_id = service.issue_authorization(
                proposal_id=proposal_id,
                actor_id=ids["operator"],
                expires_at=NOW + timedelta(minutes=30),
                allowed_adds=0,
                idempotency_key="pause-issue-race-authorization",
                now=NOW + timedelta(seconds=1),
            )
        except DomainRejected as exc:
            return exc.code
        return str(authorization_id)

    def pause() -> str:
        return service.pause_new_risk(
            ids["admin"],
            "pause-issue-race-pause",
            reason="serialize pause against authorization issue",
            now=NOW + timedelta(seconds=1),
        ).value

    with ThreadPoolExecutor(max_workers=2) as executor:
        issue_result = executor.submit(issue)
        pause_result = executor.submit(pause)
        results = {issue_result.result(), pause_result.result()}

    assert SystemRiskState.REDUCE_ONLY.value in results
    with database.session_factory() as session:
        active_authorizations = session.scalars(
            select(TradingAuthorization).where(TradingAuthorization.active)
        ).all()
        assert active_authorizations == []
        policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
        assert policy is not None
        assert policy.system_state == SystemRiskState.REDUCE_ONLY.value


def approve_restore(
    service: TradingService,
    request_id: UUID,
    ids: dict[str, UUID],
    *,
    now: datetime,
) -> None:
    assert (
        service.review_risk_control_change_request(
            request_id,
            ids["reviewer_one"],
            ReviewDecision.APPROVE,
            "first independent restore review",
            1,
            "restore-review-one",
            now=now,
        )
        is RiskPolicyChangeStatus.PENDING_REVIEW
    )
    assert (
        service.review_risk_control_change_request(
            request_id,
            ids["reviewer_two"],
            ReviewDecision.APPROVE,
            "second independent restore review",
            2,
            "restore-review-two",
            now=now,
        )
        is RiskPolicyChangeStatus.APPROVED
    )


def test_tighten_actions_permanently_revoke_old_authorization(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    enable_auto_add_for_test(database, ids["admin"], now=NOW)
    authorization_id = issue_add_authorization(service, ids)

    service.disable_global_auto_add(
        ids["admin"],
        "disable-global-add",
        reason="incident response disables all old AddUnits",
        now=NOW + timedelta(seconds=1),
    )
    with database.session_factory() as session:
        authorization = session.get(TradingAuthorization, authorization_id)
        assert authorization is not None
        assert authorization.active is True
        assert authorization.add_revoked_at == NOW + timedelta(seconds=1)
        assert authorization.used_adds == 0
        assert authorization.allowed_adds == 1

    service.pause_new_risk(
        ids["admin"],
        "pause-new-risk",
        reason="incident response pauses all new risk",
        now=NOW + timedelta(seconds=2),
    )
    with database.session_factory() as session:
        authorization = session.get(TradingAuthorization, authorization_id)
        policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
        assert authorization is not None and authorization.active is False
        assert authorization.add_revoked_at == NOW + timedelta(seconds=1)
        assert policy is not None
        assert policy.system_state == SystemRiskState.REDUCE_ONLY.value
        assert policy.reason == "incident response pauses all new risk"

    with pytest.raises(DomainRejected, match="REVIEWED_RESTORE_REQUIRED"):
        service.set_risk_policy(
            actor_id=ids["admin"],
            version="unsafe-direct-normal",
            system_state=SystemRiskState.NORMAL,
            max_total_risk=Decimal("100"),
            max_fact_age=timedelta(hours=2),
            now=NOW + timedelta(seconds=3),
        )
    with pytest.raises(DomainRejected, match="REVIEWED_RESTORE_REQUIRED"):
        service.set_capability_gate(
            "AUTO_ADD",
            CapabilityStatus.ENABLED,
            "unsafe direct enable",
            ids["admin"],
            now=NOW + timedelta(seconds=3),
        )


def test_reviewed_restore_requires_two_distinct_reviewers_and_fresh_match(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    enable_auto_add_for_test(database, ids["admin"], now=NOW)
    old_authorization_id = issue_add_authorization(service, ids)
    service.disable_global_auto_add(
        ids["admin"],
        "disable-before-reviewed-restore",
        reason="freeze prior AddUnits before reviewed recovery",
        now=NOW + timedelta(seconds=30),
    )
    service.pause_new_risk(
        ids["admin"],
        "pause-for-reviewed-restore",
        reason="pause before reviewed recovery",
        now=NOW + timedelta(minutes=1),
    )
    request_id = service.create_risk_control_change_request(
        ids["admin"],
        "create-reviewed-restore",
        reason="root cause remediated and independent review requested",
        restore_auto_add=True,
        configured_scopes=SCOPE,
        now=NOW + timedelta(minutes=2),
    )
    assert (
        service.create_risk_control_change_request(
            ids["admin"],
            "create-reviewed-restore",
            reason="root cause remediated and independent review requested",
            restore_auto_add=True,
            configured_scopes=SCOPE,
            now=NOW + timedelta(minutes=2),
        )
        == request_id
    )
    with pytest.raises(DomainRejected, match="SELF_REVIEW_FORBIDDEN"):
        service.review_risk_control_change_request(
            request_id,
            ids["admin"],
            ReviewDecision.APPROVE,
            "requester cannot approve their own change",
            1,
            "self-review",
            now=NOW + timedelta(minutes=3),
        )
    assert (
        service.review_risk_control_change_request(
            request_id,
            ids["reviewer_one"],
            ReviewDecision.APPROVE,
            "first independent restore review",
            1,
            "restore-review-one",
            now=NOW + timedelta(minutes=3),
        )
        is RiskPolicyChangeStatus.PENDING_REVIEW
    )
    with pytest.raises(DomainRejected, match="REVIEW_ALREADY_RECORDED"):
        service.review_risk_control_change_request(
            request_id,
            ids["reviewer_one"],
            ReviewDecision.APPROVE,
            "duplicate independent restore review",
            2,
            "duplicate-review",
            now=NOW + timedelta(minutes=4),
        )
    with pytest.raises(DomainRejected, match="VERSION_CONFLICT"):
        service.review_risk_control_change_request(
            request_id,
            ids["reviewer_two"],
            ReviewDecision.APPROVE,
            "stale second restore review",
            1,
            "stale-review",
            now=NOW + timedelta(minutes=4),
        )
    assert (
        service.review_risk_control_change_request(
            request_id,
            ids["reviewer_two"],
            ReviewDecision.APPROVE,
            "second independent restore review",
            2,
            "restore-review-two",
            now=NOW + timedelta(minutes=4),
        )
        is RiskPolicyChangeStatus.APPROVED
    )
    with pytest.raises(DomainRejected, match="RISK_RESTORE_COOLDOWN"):
        service.execute_risk_control_change_request(
            request_id,
            ids["admin"],
            3,
            "execute-too-soon",
            SCOPE,
            now=NOW + timedelta(minutes=5),
        )

    ready_at = NOW + timedelta(minutes=17)
    service.record_position(
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Decimal("0"),
        Decimal("0"),
        Decimal("100"),
        True,
        ids["operator"],
        now=ready_at,
    )
    service.record_account_equity(
        "acct-1",
        "BINANCE",
        Decimal("10000"),
        Decimal("9000"),
        "USDT",
        True,
        ids["operator"],
        now=ready_at,
    )
    reconciliation_id = service.reconcile_scope(
        "acct-1:BINANCE", ids["operator"], now=ready_at + timedelta(seconds=1)
    )
    assert service.reconciliation_status(reconciliation_id).value == "MATCH"
    restored_policy_id = service.execute_risk_control_change_request(
        request_id,
        ids["admin"],
        3,
        "execute-reviewed-restore",
        SCOPE,
        now=ready_at + timedelta(seconds=2),
    )
    with database.session_factory() as session:
        request = session.get(RiskControlChangeRequest, request_id)
        active_policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
        gate = session.get(CapabilityGate, "AUTO_ADD")
        old_authorization = session.get(TradingAuthorization, old_authorization_id)
        assert request is not None
        assert request.status == RiskPolicyChangeStatus.EXECUTED.value
        assert request.resulting_policy_id == restored_policy_id
        assert active_policy is not None
        assert active_policy.policy_id == restored_policy_id
        assert active_policy.system_state == SystemRiskState.NORMAL.value
        assert active_policy.revision == 3
        assert gate is not None and gate.status == CapabilityStatus.ENABLED.value
        assert old_authorization is not None
        assert old_authorization.active is False
        assert old_authorization.add_revoked_at == NOW + timedelta(seconds=30)


def test_restore_fails_closed_on_live_scope_configuration_and_control_drift(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    service.pause_new_risk(
        ids["admin"],
        "pause-for-drift",
        reason="pause before drift validation",
        now=NOW + timedelta(minutes=1),
    )
    status = service.risk_control_status(
        ids["admin"], (), require_live_scope=True, now=NOW + timedelta(minutes=2)
    )
    assert status["restore_conditions"]["ready"] is False
    assert status["restore_conditions"]["live_scope_required"] is True
    assert "LIVE_SCOPE_CONFIGURATION_REQUIRED" in status["restore_conditions"]["blockers"]

    request_id = service.create_risk_control_change_request(
        ids["admin"],
        "create-drift-request",
        reason="request that will be invalidated by control drift",
        restore_auto_add=False,
        configured_scopes=(),
        now=NOW + timedelta(minutes=2),
    )
    approve_restore(service, request_id, ids, now=NOW + timedelta(minutes=3))
    service.disable_global_auto_add(
        ids["admin"],
        "drift-gate-version",
        reason="a later tighten invalidates the frozen control version",
        now=NOW + timedelta(minutes=4),
    )
    with pytest.raises(DomainRejected, match="RISK_RESTORE_CONTROL_DRIFT"):
        service.execute_risk_control_change_request(
            request_id,
            ids["admin"],
            3,
            "execute-drifted-request",
            (),
            now=NOW + timedelta(minutes=17),
        )


def test_restore_rejection_expiry_and_terminal_status_are_durable(
    service: TradingService,
) -> None:
    ids = seed(service)
    with pytest.raises(DomainRejected, match="RISK_CONTROL_ALREADY_NORMAL"):
        service.create_risk_control_change_request(
            ids["admin"],
            "normal-control-request",
            reason="normal controls cannot create a no-op restoration",
            restore_auto_add=False,
            configured_scopes=SCOPE,
            now=NOW,
        )

    service.pause_new_risk(
        ids["admin"],
        "pause-for-terminal-status",
        reason="exercise durable restore terminal states",
        now=NOW + timedelta(minutes=1),
    )
    rejected_id = service.create_risk_control_change_request(
        ids["admin"],
        "create-rejected-restore",
        reason="independent reviewer will reject this restoration",
        restore_auto_add=False,
        configured_scopes=SCOPE,
        now=NOW + timedelta(minutes=2),
    )
    assert service.risk_control_change_version(rejected_id) == 1
    assert (
        service.review_risk_control_change_request(
            rejected_id,
            ids["reviewer_one"],
            ReviewDecision.REJECT,
            "recovery evidence is insufficient",
            1,
            "reject-restore",
            now=NOW + timedelta(minutes=3),
        )
        is RiskPolicyChangeStatus.REJECTED
    )
    status = service.risk_control_status(ids["admin"], SCOPE, now=NOW + timedelta(minutes=3))
    rejected = next(item for item in status["requests"] if item["request_id"] == str(rejected_id))
    assert rejected["status"] == RiskPolicyChangeStatus.REJECTED.value
    assert rejected["reviews"][0]["decision"] == ReviewDecision.REJECT.value

    expiring_id = service.create_risk_control_change_request(
        ids["admin"],
        "create-expiring-restore",
        reason="unreviewed request must expire durably",
        restore_auto_add=False,
        configured_scopes=SCOPE,
        now=NOW + timedelta(minutes=4),
    )
    with pytest.raises(DomainRejected, match="RISK_RESTORE_EXPIRED"):
        service.review_risk_control_change_request(
            expiring_id,
            ids["reviewer_one"],
            ReviewDecision.APPROVE,
            "review arrived after the request lifetime",
            1,
            "expired-review",
            now=NOW + timedelta(days=2),
        )
    assert service.risk_control_change_version(expiring_id) == 2

    with pytest.raises(DomainRejected, match="RISK_RESTORE_NOT_FOUND"):
        service.risk_control_change_version(UUID(int=0))
