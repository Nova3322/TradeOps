from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from trading_control_plane.authorization import (
    POLICY_VERSION,
    AuthorizationEvaluationService,
    AuthorizationRequest,
    RiskEngineStatus,
    RiskTier,
    SystemRiskState,
)
from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import CommandChannel, CommandEnvelope, CommandStatus
from trading_control_plane.database import Database
from trading_control_plane.iam_models import (
    ActionAssurance,
    AuthorizationDecision,
    ExplicitDeny,
    IdentityPrincipal,
    PermissionScope,
    RoleAssignment,
)
from trading_control_plane.models import AuditEvent, CommandReceipt, OutboxMessage

pytestmark = pytest.mark.integration


def row_count(session: Session, model: type[object]) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def seed_principal(
    database: Database,
    *,
    role: str | None = "REVIEWER",
    scope_sector: str | None = "CRYPTO",
    scope_risk_tier: str | None = None,
) -> tuple[UUID, UUID | None]:
    now = datetime.now(UTC)
    principal_id = uuid4()
    assignment_id = uuid4() if role else None
    with database.session_factory.begin() as session:
        session.add(
            IdentityPrincipal(
                principal_id=principal_id,
                organization_id="org-1",
                principal_type="HUMAN",
                external_subject_ref=f"idp:{principal_id}",
                status="ACTIVE",
                version=1,
                updated_at=now,
            )
        )
        session.flush()
        if role is not None and assignment_id is not None:
            session.add(
                RoleAssignment(
                    assignment_id=assignment_id,
                    principal_id=principal_id,
                    organization_id="org-1",
                    role_key=role,
                    policy_version=POLICY_VERSION,
                    version=1,
                    valid_from=now - timedelta(minutes=1),
                )
            )
            session.flush()
            session.add(
                PermissionScope(
                    scope_id=uuid4(),
                    assignment_id=assignment_id,
                    organization_id="org-1",
                    account_id="account-1",
                    venue="BINANCE",
                    sector=scope_sector,
                    risk_tier=scope_risk_tier,
                    action_id="ACT-PROPOSAL-APPROVE",
                    channel=None,
                )
            )
    return principal_id, assignment_id


def create_assurance(
    database: Database,
    *,
    principal_id: UUID,
    object_id: str = "proposal-1:v1",
    auth_context_ref: str | None = None,
) -> tuple[UUID, str]:
    now = datetime.now(UTC)
    assurance_id = uuid4()
    context_ref = auth_context_ref or f"webauthn:{assurance_id}"
    with database.session_factory.begin() as session:
        session.add(
            ActionAssurance(
                assurance_id=assurance_id,
                principal_id=principal_id,
                auth_context_ref=context_ref,
                device_ref="device-1",
                channel="WEB",
                action_id="ACT-PROPOSAL-APPROVE",
                object_type="ProposalVersion",
                object_id=object_id,
                object_version=1,
                status="VERIFIED",
                issued_at=now - timedelta(seconds=5),
                expires_at=now + timedelta(minutes=2),
                assurance_method="PASSKEY_WEBAUTHN",
                assurance_level="ACTION_STEP_UP",
                verifier_ref="managed-idp:fixture",
            )
        )
    return assurance_id, context_ref


def make_request(
    *,
    principal_id: UUID,
    creator_id: UUID | None = None,
    risk_tier: RiskTier = RiskTier.LOW,
    assurance_id: UUID | None = None,
    **overrides: object,
) -> AuthorizationRequest:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "principal_id": principal_id,
        "action_id": "ACT-PROPOSAL-APPROVE",
        "object_type": "ProposalVersion",
        "object_id": "proposal-1:v1",
        "object_version": 1,
        "organization_id": "org-1",
        "account_id": "account-1",
        "venue": "BINANCE",
        "sector": "CRYPTO",
        "risk_tier": risk_tier,
        "channel": CommandChannel.WEB,
        "online": True,
        "device_ref": "device-1",
        "assurance_id": assurance_id,
        "resource_creator_id": creator_id or uuid4(),
        "resource_status": "FROZEN",
        "resource_valid_until": now + timedelta(minutes=5),
        "risk_engine_status": RiskEngineStatus.PASSED,
        "system_risk_state": SystemRiskState.NORMAL,
        "requested_at": now,
    }
    values.update(overrides)
    return AuthorizationRequest.model_validate(values)


def make_envelope(
    request: AuthorizationRequest,
    *,
    auth_context_ref: str = "auth:no-action-assurance",
) -> CommandEnvelope:
    now = datetime.now(UTC)
    return CommandEnvelope(
        idempotency_key=f"authz-{uuid4()}",
        command_type="authorization.evaluate.v1",
        object_type=request.object_type,
        object_id=request.object_id,
        expected_version=request.object_version,
        actor_id=str(request.principal_id),
        channel=request.channel,
        scope={"organization_id": request.organization_id},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref=auth_context_ref,
        payload_schema_version=1,
        reason="server-side authorization evaluation",
        payload=request.model_dump(mode="json"),
    )


def execute(
    database: Database,
    request: AuthorizationRequest,
    *,
    auth_context_ref: str = "auth:no-action-assurance",
):
    executor = IdempotentCommandExecutor(database.session_factory)
    return executor.execute(
        make_envelope(request, auth_context_ref=auth_context_ref),
        AuthorizationEvaluationService().evaluate,
    )


def test_default_deny_is_durable_when_no_role_exists(database: Database) -> None:
    principal_id, _ = seed_principal(database, role=None)

    result = execute(database, make_request(principal_id=principal_id))

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "ROLE_NOT_ALLOWED"
    with database.session_factory.begin() as session:
        decision = session.execute(select(AuthorizationDecision)).scalar_one()
        assert (decision.result, decision.reason_code) == ("DENY", "ROLE_NOT_ALLOWED")
        assert row_count(session, CommandReceipt) == 1
        assert row_count(session, AuditEvent) == 1
        assert row_count(session, OutboxMessage) == 1


def test_multi_label_does_not_bypass_self_review(database: Database) -> None:
    principal_id, _ = seed_principal(database)
    now = datetime.now(UTC)
    with database.session_factory.begin() as session:
        proposer_assignment = uuid4()
        session.add(
            RoleAssignment(
                assignment_id=proposer_assignment,
                principal_id=principal_id,
                organization_id="org-1",
                role_key="PROPOSER",
                policy_version=POLICY_VERSION,
                version=1,
                valid_from=now - timedelta(minutes=1),
            )
        )
        session.flush()
        session.add(
            PermissionScope(
                scope_id=uuid4(),
                assignment_id=proposer_assignment,
                organization_id="org-1",
                account_id="account-1",
                venue="BINANCE",
                sector="CRYPTO",
                risk_tier="LOW",
                action_id=None,
                channel=None,
            )
        )

    request = make_request(principal_id=principal_id, creator_id=principal_id)
    result = execute(database, request)

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "SELF_REVIEW_FORBIDDEN"
    with database.session_factory.begin() as session:
        decision = session.execute(select(AuthorizationDecision)).scalar_one()
        assert decision.is_self_review is True


@pytest.mark.parametrize(
    ("risk_tier", "expected_quorum"),
    [(RiskTier.LOW, 1), (RiskTier.MEDIUM, 1), (RiskTier.HIGH, 2)],
)
def test_independent_reviewer_gets_fixed_quorum_but_no_approval_fact(
    database: Database,
    risk_tier: RiskTier,
    expected_quorum: int,
) -> None:
    principal_id, _ = seed_principal(database)
    assurance_id, context_ref = create_assurance(database, principal_id=principal_id)
    request = make_request(
        principal_id=principal_id,
        risk_tier=risk_tier,
        assurance_id=assurance_id,
    )

    result = execute(database, request, auth_context_ref=context_ref)

    assert result.status is CommandStatus.COMPLETED
    assert result.data["authorization_result"] == "ALLOW"
    assert result.data["required_quorum"] == expected_quorum
    with database.session_factory.begin() as session:
        decision = session.execute(select(AuthorizationDecision)).scalar_one()
        assurance = session.get(ActionAssurance, assurance_id)
        assert decision.required_quorum == expected_quorum
        assert assurance is not None and assurance.used_at is not None
        assert (
            session.execute(
                text("SELECT to_regclass('public.reviewer_votes')")
            ).scalar_one_or_none()
            is None
        )


def test_abac_scope_mismatch_denies_before_mfa_consumption(database: Database) -> None:
    principal_id, _ = seed_principal(database, scope_sector="METALS")
    assurance_id, context_ref = create_assurance(database, principal_id=principal_id)

    result = execute(
        database,
        make_request(principal_id=principal_id, assurance_id=assurance_id),
        auth_context_ref=context_ref,
    )

    assert result.error_code == "SCOPE_MISMATCH"
    with database.session_factory.begin() as session:
        assurance = session.get(ActionAssurance, assurance_id)
        assert assurance is not None and assurance.used_at is None


def test_explicit_deny_overrides_reviewer_role_and_scope(database: Database) -> None:
    principal_id, _ = seed_principal(database)
    assurance_id, context_ref = create_assurance(database, principal_id=principal_id)
    now = datetime.now(UTC)
    with database.session_factory.begin() as session:
        session.add(
            ExplicitDeny(
                deny_id=uuid4(),
                principal_id=principal_id,
                organization_id="org-1",
                action_id="ACT-PROPOSAL-APPROVE",
                account_id="account-1",
                venue="BINANCE",
                sector=None,
                risk_tier=None,
                channel=None,
                reason_code="INCIDENT_RESTRICTION",
                policy_version=POLICY_VERSION,
                valid_from=now - timedelta(minutes=1),
            )
        )

    result = execute(
        database,
        make_request(principal_id=principal_id, assurance_id=assurance_id),
        auth_context_ref=context_ref,
    )

    assert result.error_code == "EXPLICIT_DENY"
    with database.session_factory.begin() as session:
        decision = session.execute(select(AuthorizationDecision)).scalar_one()
        assurance = session.get(ActionAssurance, assurance_id)
        assert len(decision.matched_deny_ids) == 1
        assert assurance is not None and assurance.used_at is None


def test_system_admin_cannot_review_without_independent_reviewer_role(
    database: Database,
) -> None:
    principal_id, _ = seed_principal(database, role="SYSTEM_ADMIN")

    result = execute(database, make_request(principal_id=principal_id))

    assert result.error_code == "ROLE_NOT_ALLOWED"


def test_revocation_affects_next_sensitive_action_immediately(database: Database) -> None:
    principal_id, assignment_id = seed_principal(database)
    assert assignment_id is not None
    with database.session_factory.begin() as session:
        assignment = session.get(RoleAssignment, assignment_id)
        assert assignment is not None
        assignment.revoked_at = datetime.now(UTC)

    result = execute(database, make_request(principal_id=principal_id))

    assert result.error_code == "ROLE_NOT_ALLOWED"


def test_one_action_assurance_cannot_be_reused(database: Database) -> None:
    principal_id, _ = seed_principal(database)
    assurance_id, context_ref = create_assurance(database, principal_id=principal_id)

    first = execute(
        database,
        make_request(principal_id=principal_id, assurance_id=assurance_id),
        auth_context_ref=context_ref,
    )
    second = execute(
        database,
        make_request(principal_id=principal_id, assurance_id=assurance_id),
        auth_context_ref=context_ref,
    )

    assert first.status is CommandStatus.COMPLETED
    assert second.status is CommandStatus.REJECTED
    assert second.error_code == "MFA_ALREADY_USED"


def test_concurrent_action_assurance_consumption_allows_only_one(
    database: Database,
) -> None:
    principal_id, _ = seed_principal(database)
    assurance_id, context_ref = create_assurance(database, principal_id=principal_id)
    requests = (
        make_request(principal_id=principal_id, assurance_id=assurance_id),
        make_request(principal_id=principal_id, assurance_id=assurance_id),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda request: execute(database, request, auth_context_ref=context_ref),
                requests,
            )
        )

    assert {result.status for result in results} == {
        CommandStatus.COMPLETED,
        CommandStatus.REJECTED,
    }
    assert {result.error_code for result in results} == {None, "MFA_ALREADY_USED"}


def test_authorization_decision_is_append_only(database: Database) -> None:
    principal_id, _ = seed_principal(database, role=None)
    execute(database, make_request(principal_id=principal_id))

    with pytest.raises(DBAPIError, match="authorization_decisions is append-only"):
        with database.engine.begin() as connection:
            connection.execute(text("UPDATE authorization_decisions SET result = 'ALLOW'"))

    with pytest.raises(DBAPIError, match="authorization_decisions is append-only"):
        with database.engine.begin() as connection:
            connection.execute(text("DELETE FROM authorization_decisions"))


def test_action_assurance_binding_and_history_are_protected(database: Database) -> None:
    principal_id, _ = seed_principal(database)
    assurance_id, _ = create_assurance(database, principal_id=principal_id)

    with pytest.raises(DBAPIError, match="action_assurance binding is immutable"):
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE action_assurances "
                    "SET object_id = 'different-proposal:v1' "
                    "WHERE assurance_id = :assurance_id"
                ),
                {"assurance_id": assurance_id},
            )

    with pytest.raises(DBAPIError, match="action_assurances cannot be deleted"):
        with database.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM action_assurances WHERE assurance_id = :assurance_id"),
                {"assurance_id": assurance_id},
            )


def test_action_assurance_consumption_and_revocation_are_irreversible(
    database: Database,
) -> None:
    principal_id, _ = seed_principal(database)
    consumed_id, context_ref = create_assurance(database, principal_id=principal_id)
    execute(
        database,
        make_request(principal_id=principal_id, assurance_id=consumed_id),
        auth_context_ref=context_ref,
    )

    with pytest.raises(DBAPIError, match="consumption is irreversible"):
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE action_assurances SET used_at = NULL WHERE assurance_id = :assurance_id"
                ),
                {"assurance_id": consumed_id},
            )

    revoked_id, _ = create_assurance(
        database,
        principal_id=principal_id,
        object_id="proposal-2:v1",
    )
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE action_assurances SET status = 'REVOKED' WHERE assurance_id = :assurance_id"
            ),
            {"assurance_id": revoked_id},
        )
    with pytest.raises(DBAPIError, match="revocation is irreversible"):
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE action_assurances SET status = 'VERIFIED' "
                    "WHERE assurance_id = :assurance_id"
                ),
                {"assurance_id": revoked_id},
            )
