from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from uuid import UUID

import pytest
from sqlalchemy import select

from trading_control_plane.database import Database
from trading_control_plane.domain import (
    AddCandidateFacts,
    CapabilityStatus,
    Direction,
    DomainRejected,
    ExecutionEnvironment,
    IntentKind,
    OrderIntentStatus,
    ProposalSource,
    ReviewDecision,
    RiskPolicyChangeStatus,
    RiskTier,
    Role,
    SystemRiskState,
)
from trading_control_plane.models import (
    Campaign,
    CapabilityGate,
    OrderIntent,
    RiskControlChangeRequest,
    RiskPolicy,
    TradingAuthorization,
)
from trading_control_plane.service import TradingService

NOW = datetime(2026, 8, 1, 8, tzinfo=UTC)
SCOPE = (("SHADOW", "acct-1", "BINANCE"),)


def test_live_restore_scope_projection_excludes_shadow_scopes_and_campaigns() -> None:
    campaigns = [
        Campaign(
            environment="SHADOW",
            account_id="shadow-account",
            venue="HYPERLIQUID",
            status="OPEN",
        ),
        Campaign(
            environment="LIVE",
            account_id="live-campaign-account",
            venue="BINANCE",
            status="OPEN",
        ),
    ]

    scopes = TradingService._canonical_restore_scopes(
        (
            ("SHADOW", "configured-shadow", "BINANCE"),
            ("LIVE", "configured-live", "HYPERLIQUID"),
        ),
        campaigns,
        required_environment=ExecutionEnvironment.LIVE.value,
    )

    assert scopes == [
        {
            "environment": "LIVE",
            "account_id": "configured-live",
            "venue": "HYPERLIQUID",
        },
        {
            "environment": "LIVE",
            "account_id": "live-campaign-account",
            "venue": "BINANCE",
        },
    ]


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


def test_pause_and_initial_intent_creation_share_risk_first_lock_order(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    proposal_id = prepare_add_proposal(service, ids, key="pause-create-race")
    authorization_id = service.issue_authorization(
        proposal_id=proposal_id,
        actor_id=ids["operator"],
        expires_at=NOW + timedelta(minutes=30),
        allowed_adds=0,
        idempotency_key="pause-create-race-authorization",
        now=NOW,
    )
    race = Barrier(2)

    def create_initial() -> str:
        race.wait(timeout=5)
        try:
            service.create_order_intent(
                authorization_id,
                ids["operator"],
                IntentKind.INITIAL,
                "acct-1",
                "BINANCE",
                ids["instrument"],
                Direction.LONG,
                Decimal("0.5"),
                "pause-create-race-intent",
                now=NOW + timedelta(seconds=1),
            )
        except DomainRejected as exc:
            return exc.code
        return "CREATED"

    def pause() -> str:
        race.wait(timeout=5)
        return service.pause_new_risk(
            ids["admin"],
            "pause-create-race-pause",
            reason="serialize pause against initial intent creation",
            now=NOW + timedelta(seconds=1),
        ).value

    with ThreadPoolExecutor(max_workers=2) as executor:
        create_future = executor.submit(create_initial)
        pause_future = executor.submit(pause)
        results = {create_future.result(timeout=10), pause_future.result(timeout=10)}

    assert SystemRiskState.REDUCE_ONLY.value in results
    assert results <= {
        SystemRiskState.REDUCE_ONLY.value,
        "CREATED",
        "AUTHORIZATION_INACTIVE",
        "FINAL_RISK_CHECK_FAILED",
    }
    with database.session_factory() as session:
        authorization = session.get(TradingAuthorization, authorization_id)
        assert authorization is not None and authorization.active is False
        policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
        assert policy is not None
        assert policy.system_state == SystemRiskState.REDUCE_ONLY.value


def test_disable_auto_add_and_add_creation_share_risk_gate_auth_lock_order(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    enable_auto_add_for_test(database, ids["admin"], now=NOW)
    authorization_id = issue_add_authorization(service, ids)
    opening = service.create_order_intent(
        authorization_id,
        ids["admin"],
        IntentKind.INITIAL,
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Direction.LONG,
        Decimal("0.5"),
        "disable-add-race-opening",
        now=NOW,
    )
    token = service.acquire_sender("acct-1:BINANCE", "disable-add-worker", ids["operator"], NOW)
    service.record_shadow_order(
        opening.intent_id,
        ids["operator"],
        "acct-1:BINANCE",
        "disable-add-worker",
        token,
        "disable-add-race-order",
        now=NOW,
    )
    service.record_fill(
        opening.intent_id,
        ids["operator"],
        "disable-add-race-fill",
        "BUY",
        Decimal("0.5"),
        Decimal("100"),
        Decimal("0"),
        "USDT",
        Decimal("0"),
        now=NOW,
    )
    position_id = service.record_position(
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Decimal("0.5"),
        Decimal("100"),
        Decimal("110"),
        True,
        ids["operator"],
        now=NOW,
    )
    service.record_protection(
        position_id,
        "disable-add-race-stop",
        Decimal("0.5"),
        Decimal("90"),
        True,
        ids["operator"],
        now=NOW,
    )
    candidate = AddCandidateFacts(
        candidate_id="disable_add_race_candidate",
        contract_version="breakouts-v1",
        venue="BINANCE",
        symbol="BTCUSDT",
        direction=Direction.LONG,
        observed_at=NOW + timedelta(seconds=1),
        reference_price=Decimal("110"),
        readiness="READY",
    )
    race = Barrier(2)

    def create_add() -> str:
        race.wait(timeout=5)
        try:
            service.create_order_intent(
                authorization_id,
                ids["operator"],
                IntentKind.ADD,
                "acct-1",
                "BINANCE",
                ids["instrument"],
                Direction.LONG,
                Decimal("0.5"),
                "disable-add-race-intent",
                add_candidate=candidate,
                now=NOW + timedelta(seconds=1),
            )
        except DomainRejected as exc:
            return exc.code
        return "CREATED"

    def disable() -> str:
        race.wait(timeout=5)
        service.disable_global_auto_add(
            ids["admin"],
            "disable-add-race-disable",
            reason="serialize global disable against Add creation",
            now=NOW + timedelta(seconds=1),
        )
        return "DISABLED"

    with ThreadPoolExecutor(max_workers=2) as executor:
        add_future = executor.submit(create_add)
        disable_future = executor.submit(disable)
        results = {add_future.result(timeout=10), disable_future.result(timeout=10)}

    assert "DISABLED" in results
    assert results <= {
        "DISABLED",
        "CREATED",
        "AUTO_ADD_DISABLED",
        "AUTHORIZATION_ADD_REVOKED",
    }
    with database.session_factory() as session:
        gate = session.get(CapabilityGate, "AUTO_ADD")
        authorization = session.get(TradingAuthorization, authorization_id)
        add_intents = session.scalars(
            select(OrderIntent).where(OrderIntent.kind == IntentKind.ADD.value)
        ).all()
        assert gate is not None and gate.status == CapabilityStatus.DISABLED.value
        assert authorization is not None and authorization.add_revoked_at is not None
        assert len(add_intents) <= 1
        if add_intents:
            assert add_intents[0].status == OrderIntentStatus.READY.value


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


def test_reviewed_restore_requires_operator_and_one_independent_reviewer(
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
        ids["operator"],
        "create-reviewed-restore",
        reason="root cause remediated and independent review requested",
        restore_auto_add=False,
        configured_scopes=SCOPE,
        now=NOW + timedelta(minutes=2),
    )
    assert (
        service.create_risk_control_change_request(
            ids["operator"],
            "create-reviewed-restore",
            reason="root cause remediated and independent review requested",
            restore_auto_add=False,
            configured_scopes=SCOPE,
            now=NOW + timedelta(minutes=2),
        )
        == request_id
    )
    service.assign_role(
        ids["operator"],
        Role.REVIEWER,
        ids["admin"],
        now=NOW + timedelta(minutes=2, seconds=1),
    )
    with pytest.raises(DomainRejected, match="SELF_REVIEW_FORBIDDEN"):
        service.review_risk_control_change_request(
            request_id,
            ids["operator"],
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
        is RiskPolicyChangeStatus.APPROVED
    )
    with pytest.raises(DomainRejected, match="RISK_RESTORE_NOT_REVIEWABLE"):
        service.review_risk_control_change_request(
            request_id,
            ids["reviewer_one"],
            ReviewDecision.APPROVE,
            "duplicate independent restore review",
            2,
            "duplicate-review",
            now=NOW + timedelta(minutes=4),
        )
    with pytest.raises(DomainRejected, match="RISK_RESTORE_NOT_REVIEWABLE"):
        service.review_risk_control_change_request(
            request_id,
            ids["reviewer_two"],
            ReviewDecision.APPROVE,
            "stale second restore review",
            2,
            "stale-review",
            now=NOW + timedelta(minutes=4),
        )
    with pytest.raises(DomainRejected, match="RISK_RESTORE_COOLDOWN"):
        service.execute_risk_control_change_request(
            request_id,
            ids["reviewer_one"],
            2,
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
        ids["reviewer_one"],
        2,
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
        assert gate is not None and gate.status == CapabilityStatus.DISABLED.value
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
        ids["operator"],
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
    drifted_status = service.risk_control_status(
        ids["admin"], (), now=NOW + timedelta(minutes=4, seconds=1)
    )
    drifted_request = next(
        item for item in drifted_status["requests"] if item["request_id"] == str(request_id)
    )
    assert drifted_request["status"] == RiskPolicyChangeStatus.EXPIRED.value
    assert drifted_request["superseded_by_control_state"] is True
    assert drifted_status["actions"]["review_restore"]["allowed"] is False
    assert drifted_status["actions"]["execute_restore"]["allowed"] is False
    with pytest.raises(DomainRejected, match="RISK_RESTORE_CONTROL_DRIFT"):
        service.execute_risk_control_change_request(
            request_id,
            ids["reviewer_one"],
            2,
            "execute-drifted-request",
            (),
            now=NOW + timedelta(minutes=17),
        )


def test_system_admin_direct_restore_requires_live_conditions_and_keeps_auto_add_off(
    database: Database,
    service: TradingService,
) -> None:
    ids = seed(service)
    enable_auto_add_for_test(database, ids["admin"], now=NOW)
    old_authorization_id = issue_add_authorization(service, ids)
    service.disable_global_auto_add(
        ids["admin"],
        "disable-before-admin-restore",
        reason="direct restore must never reopen AUTO_ADD",
        now=NOW + timedelta(seconds=30),
    )
    service.pause_new_risk(
        ids["admin"],
        "pause-before-admin-restore",
        reason="exercise direct administrator restoration",
        now=NOW + timedelta(minutes=1),
    )
    live_scope = (("LIVE", "acct-1", "BINANCE"),)
    with pytest.raises(DomainRejected, match="RISK_RESTORE_BLOCKED"):
        service.direct_restore_risk_controls(
            ids["admin"],
            "admin-restore-blocked",
            reason="must not restore without live facts",
            configured_scopes=live_scope,
            now=NOW + timedelta(minutes=2),
        )
    pending_request_id = service.create_risk_control_change_request(
        ids["operator"],
        "pending-before-admin-restore",
        reason="a direct administrator restore should supersede this request",
        restore_auto_add=False,
        configured_scopes=live_scope,
        require_live_scope=True,
        now=NOW + timedelta(minutes=2, seconds=1),
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
        environment=ExecutionEnvironment.LIVE,
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
        environment=ExecutionEnvironment.LIVE,
        now=ready_at,
    )
    reconciliation_id = service.reconcile_scope(
        "LIVE:acct-1:BINANCE",
        ids["operator"],
        now=ready_at + timedelta(seconds=1),
    )
    assert service.reconciliation_status(reconciliation_id).value == "MATCH"
    restored_policy_id = service.direct_restore_risk_controls(
        ids["admin"],
        "admin-restore-success",
        reason="all live safety conditions passed",
        configured_scopes=live_scope,
        now=ready_at + timedelta(seconds=2),
    )

    with database.session_factory() as session:
        restored = session.get(RiskPolicy, restored_policy_id)
        gate = session.get(CapabilityGate, "AUTO_ADD")
        old_authorization = session.get(TradingAuthorization, old_authorization_id)
        pending_request = session.get(RiskControlChangeRequest, pending_request_id)
        assert restored is not None
        assert restored.system_state == SystemRiskState.NORMAL.value
        assert gate is not None and gate.status == CapabilityStatus.DISABLED.value
        assert old_authorization is not None and old_authorization.active is False
        assert pending_request is not None
        assert pending_request.status == RiskPolicyChangeStatus.EXPIRED.value
        assert pending_request.version == 2
        assert pending_request.resulting_policy_id == restored_policy_id

    status = service.risk_control_status(
        ids["admin"], live_scope, require_live_scope=True, now=ready_at + timedelta(seconds=3)
    )
    superseded_request = next(
        item
        for item in status["requests"]
        if item["request_id"] == str(pending_request_id)
    )
    assert superseded_request["status"] == RiskPolicyChangeStatus.EXPIRED.value
    assert superseded_request["superseded_by_control_state"] is True
    assert status["actions"]["review_restore"]["allowed"] is False
    assert status["actions"]["execute_restore"]["allowed"] is False


def test_restore_rejection_expiry_and_terminal_status_are_durable(
    service: TradingService,
) -> None:
    ids = seed(service)
    with pytest.raises(DomainRejected, match="RISK_CONTROL_ALREADY_NORMAL"):
        service.create_risk_control_change_request(
            ids["operator"],
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
        ids["operator"],
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
        ids["operator"],
        "create-expiring-restore",
        reason="unreviewed request must expire durably",
        restore_auto_add=False,
        configured_scopes=SCOPE,
        now=NOW + timedelta(minutes=4),
    )
    expired_projection = service.risk_control_status(
        ids["admin"], SCOPE, now=NOW + timedelta(days=1, minutes=5)
    )
    projected_request = next(
        item for item in expired_projection["requests"] if item["request_id"] == str(expiring_id)
    )
    assert projected_request["status"] == RiskPolicyChangeStatus.EXPIRED.value
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


def test_expired_restore_does_not_block_a_replacement_request(
    database: Database,
    service: TradingService,
) -> None:
    ids = seed(service)
    service.pause_new_risk(
        ids["admin"],
        "pause-for-replacement",
        reason="exercise replacement after expiry",
        now=NOW + timedelta(minutes=1),
    )
    expired_id = service.create_risk_control_change_request(
        ids["operator"],
        "create-request-that-expires",
        reason="this request will not be reviewed in time",
        restore_auto_add=False,
        configured_scopes=SCOPE,
        now=NOW + timedelta(minutes=2),
    )

    replacement_id = service.create_risk_control_change_request(
        ids["operator"],
        "create-replacement-request",
        reason="replace the expired request with current evidence",
        restore_auto_add=False,
        configured_scopes=SCOPE,
        now=NOW + timedelta(days=1, minutes=3),
    )

    with database.session_factory() as session:
        expired = session.get(RiskControlChangeRequest, expired_id)
        replacement = session.get(RiskControlChangeRequest, replacement_id)
        assert expired is not None
        assert expired.status == RiskPolicyChangeStatus.EXPIRED.value
        assert expired.version == 2
        assert replacement is not None
        assert replacement.status == RiskPolicyChangeStatus.PENDING_REVIEW.value
