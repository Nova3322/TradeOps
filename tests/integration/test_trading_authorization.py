from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from tests.capability_fixtures import issue_shadow_certificate_for_proposal
from tests.integration.test_review import (
    approval_assurance,
    execute_review,
    review_envelope,
    seed_proposal,
    seed_reviewer,
)
from tests.risk_fixtures import TEST_EXECUTION_CAPITAL_PROJECTION_BINDING
from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import CommandChannel, CommandEnvelope, CommandStatus, hash_json
from trading_control_plane.database import Database
from trading_control_plane.models import AuditEvent, CommandReceipt, OutboxMessage
from trading_control_plane.proposal_models import (
    ApprovalDecision,
    FrozenProposalVersion,
    SystemRiskStateRecord,
)
from trading_control_plane.trading_authorization import (
    AUTHORIZATION_SERVICE_PRINCIPAL,
    TradingAuthorizationService,
)
from trading_control_plane.trading_authorization_models import (
    AddAuthorizationPackage,
    AddAuthorizationPackageState,
    AddUnit,
    AddUnitState,
    AuthorizationStateTransition,
    Campaign,
    CampaignState,
    InitialAuthorizationState,
    InitialOrderAuthorization,
    TradingAuthorization,
)

pytestmark = pytest.mark.integration


def count_rows(session: Session, model: type[object]) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def approve_proposal(database: Database, proposal_id: UUID, risk_tier: str) -> ApprovalDecision:
    with database.session_factory.begin() as session:
        proposal = session.get(FrozenProposalVersion, proposal_id)
        assert proposal is not None
    reviewer_count = 2 if risk_tier == "HIGH" else 1
    for _ in range(reviewer_count):
        reviewer = seed_reviewer(database)
        assurance_id, context_ref = approval_assurance(database, proposal, reviewer)
        result = execute_review(
            database,
            review_envelope(
                proposal,
                reviewer,
                assurance_id=assurance_id,
                auth_context_ref=context_ref,
            ),
        )
        assert result.status in {CommandStatus.ACCEPTED, CommandStatus.COMPLETED}
    with database.session_factory.begin() as session:
        return session.execute(
            select(ApprovalDecision).where(
                ApprovalDecision.proposal_version_id == proposal.proposal_version_id
            )
        ).scalar_one()


def issue_envelope(
    proposal_id: UUID,
    proposal_version: int,
    proposal_spec_hash: str,
    risk_summary_hash: str,
    approval_decision_id: UUID,
    *,
    idempotency_key: str | None = None,
    service_principal: str = AUTHORIZATION_SERVICE_PRINCIPAL,
) -> CommandEnvelope:
    now = datetime.now(UTC)
    return CommandEnvelope(
        idempotency_key=idempotency_key or f"issue-authorization-{uuid4()}",
        command_type="trading.authorization.issue.v1",
        object_type="ProposalVersion",
        object_id=str(proposal_id),
        expected_version=proposal_version,
        service_principal=service_principal,
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": "org-1"},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref="internal:trading-authorization-service",
        payload_schema_version=1,
        reason="issue approved shadow authorization",
        payload={
            "approval_decision_id": str(approval_decision_id),
            "proposal_spec_hash": proposal_spec_hash,
            "risk_summary_hash": risk_summary_hash,
        },
    )


def tighten_envelope(
    authorization_id: UUID,
    action: str,
    *,
    reason_code: str = "TEST_RISK_TIGHTENING",
) -> CommandEnvelope:
    now = datetime.now(UTC)
    return CommandEnvelope(
        idempotency_key=f"tighten-authorization-{uuid4()}",
        command_type="trading.authorization.tighten.v1",
        object_type="TradingAuthorization",
        object_id=str(authorization_id),
        expected_version=1,
        service_principal=AUTHORIZATION_SERVICE_PRINCIPAL,
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": "org-1"},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref="internal:trading-authorization-service",
        payload_schema_version=1,
        reason="tighten existing authorization",
        payload={"action": action, "reason_code": reason_code},
    )


def execute_issue(
    database: Database,
    envelope: CommandEnvelope,
    service: TradingAuthorizationService | None = None,
):
    handler = (service or TradingAuthorizationService()).issue
    return IdempotentCommandExecutor(database.session_factory).execute(envelope, handler)


def execute_tighten(
    database: Database,
    envelope: CommandEnvelope,
    service: TradingAuthorizationService | None = None,
):
    handler = (service or TradingAuthorizationService()).tighten
    return IdempotentCommandExecutor(database.session_factory).execute(envelope, handler)


def set_system_risk_state(database: Database, status: str) -> None:
    with database.session_factory.begin() as session:
        state = session.execute(
            select(SystemRiskStateRecord)
            .where(SystemRiskStateRecord.organization_id == "org-1")
            .with_for_update()
        ).scalar_one()
        state.status = status
        state.version += 1
        state.reason_code = f"TEST_{status}"
        state.policy_version = "risk-state-v2"
        state.transition_source_ref = f"test-only:{status.lower()}"
        state.updated_at = datetime.now(UTC)


def prepare_approved(
    database: Database,
    *,
    risk_tier: str = "LOW",
    issue_certificate: bool = True,
    **proposal_kwargs: object,
):
    proposal = seed_proposal(database, risk_tier=risk_tier, **proposal_kwargs)
    decision = approve_proposal(database, proposal.proposal_version_id, risk_tier)
    certificate_ref = proposal.spec.get("capability_certificate_ref")
    if issue_certificate and isinstance(certificate_ref, str) and certificate_ref:
        issue_shadow_certificate_for_proposal(database, proposal)
    return proposal, decision


def test_issue_creates_shadow_root_campaign_and_initial_without_execution_side_effects(
    database: Database,
) -> None:
    proposal, decision = prepare_approved(database)

    result = execute_issue(
        database,
        issue_envelope(
            proposal.proposal_version_id,
            proposal.version,
            proposal.spec_hash,
            proposal.risk_summary_hash,
            decision.approval_decision_id,
        ),
    )

    assert result.status is CommandStatus.COMPLETED
    assert result.data["authorization_mode"] == "SHADOW"
    assert result.data["execution_eligible"] is False
    assert result.data["risk_reservation_created"] is False
    assert result.data["order_intent_created"] is False
    with database.session_factory.begin() as session:
        root = session.execute(select(TradingAuthorization)).scalar_one()
        campaign = session.execute(select(Campaign)).scalar_one()
        campaign_state = session.execute(select(CampaignState)).scalar_one()
        initial = session.execute(select(InitialOrderAuthorization)).scalar_one()
        initial_state = session.execute(select(InitialAuthorizationState)).scalar_one()
        assert root.issuance_snapshot_hash == hash_json(root.issuance_snapshot)
        assert root.issuance_snapshot[
            "capital_projection_binding"
        ] == TEST_EXECUTION_CAPITAL_PROJECTION_BINDING.model_dump(mode="json")
        assert root.execution_eligible is False
        assert campaign.authorization_id == root.authorization_id
        assert campaign_state.status == "PENDING_ENTRY"
        assert initial.max_quantity == proposal.risk_approved_quantity
        assert initial.price_lower_bound < initial.price_reference < initial.price_upper_bound
        assert initial_state.status == "ACTIVE"
        assert count_rows(session, AddAuthorizationPackage) == 0
        assert count_rows(session, AuthorizationStateTransition) == 2
        assert session.execute(text("SELECT count(*) FROM risk_reservations")).scalar_one() == 0
        assert session.execute(text("SELECT count(*) FROM order_intents")).scalar_one() == 0


def test_issue_has_no_legacy_authorization_without_frozen_capital_scope(
    database: Database,
) -> None:
    proposal = seed_proposal(database, include_capital_projection_binding=False)
    decision = approve_proposal(database, proposal.proposal_version_id, proposal.risk_tier)

    result = execute_issue(
        database,
        issue_envelope(
            proposal.proposal_version_id,
            proposal.version,
            proposal.spec_hash,
            proposal.risk_summary_hash,
            decision.approval_decision_id,
        ),
    )

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "CAPITAL_PROJECTION_BINDING_INVALID"
    with database.session_factory.begin() as session:
        assert count_rows(session, TradingAuthorization) == 0


def test_high_risk_add_package_preserves_quorum_and_30_50_100_units(
    database: Database,
) -> None:
    proposal, decision = prepare_approved(
        database,
        risk_tier="HIGH",
        auto_add_enabled=True,
        requested_add_count=3,
    )

    result = execute_issue(
        database,
        issue_envelope(
            proposal.proposal_version_id,
            proposal.version,
            proposal.spec_hash,
            proposal.risk_summary_hash,
            decision.approval_decision_id,
        ),
    )

    assert result.data["add_package_status"] == "DORMANT"
    assert len(result.data["add_unit_ids"]) == 3
    with database.session_factory.begin() as session:
        root = session.execute(select(TradingAuthorization)).scalar_one()
        package = session.execute(select(AddAuthorizationPackage)).scalar_one()
        package_state = session.execute(select(AddAuthorizationPackageState)).scalar_one()
        units = tuple(session.execute(select(AddUnit).order_by(AddUnit.ordinal)).scalars())
        unit_states = tuple(session.execute(select(AddUnitState)).scalars())
        assert root.authorized_loss_capacity == proposal.one_r_0 * 3
        assert root.issuance_snapshot["approval_decision"]["required_quorum"] == 2
        assert len(root.issuance_snapshot["reviewer_votes"]) == 2
        assert package.authorized_add_count == 3
        assert package_state.status == "DORMANT"
        assert [unit.unlock_milestone_pct for unit in units] == [30, 50, 100]
        assert {state.status for state in unit_states} == {"AVAILABLE"}
        assert count_rows(session, AuthorizationStateTransition) == 6


def test_no_pyramid_issues_initial_but_permanently_invalidates_add_capacity(
    database: Database,
) -> None:
    proposal, decision = prepare_approved(
        database,
        risk_tier="MEDIUM",
        auto_add_enabled=True,
        requested_add_count=2,
    )
    set_system_risk_state(database, "NO_PYRAMID")

    result = execute_issue(
        database,
        issue_envelope(
            proposal.proposal_version_id,
            proposal.version,
            proposal.spec_hash,
            proposal.risk_summary_hash,
            decision.approval_decision_id,
        ),
    )

    assert result.data["initial_status"] == "ACTIVE"
    assert result.data["add_package_status"] == "INVALIDATED"
    with database.session_factory.begin() as session:
        assert session.execute(select(InitialAuthorizationState.status)).scalar_one() == "ACTIVE"
        assert (
            session.execute(select(AddAuthorizationPackageState.status)).scalar_one()
            == "INVALIDATED"
        )
        assert set(session.execute(select(AddUnitState.status)).scalars()) == {"INVALIDATED"}


@pytest.mark.parametrize("status", ["NO_NEW_POSITION", "REDUCE_ONLY", "KILL_SWITCH", "UNKNOWN"])
def test_strict_system_risk_states_deny_issuance_without_partial_rows(
    database: Database, status: str
) -> None:
    proposal, decision = prepare_approved(database)
    set_system_risk_state(database, status)

    result = execute_issue(
        database,
        issue_envelope(
            proposal.proposal_version_id,
            proposal.version,
            proposal.spec_hash,
            proposal.risk_summary_hash,
            decision.approval_decision_id,
        ),
    )

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "SYSTEM_RISK_STATE_DENY"
    with database.session_factory.begin() as session:
        assert count_rows(session, TradingAuthorization) == 0
        assert count_rows(session, Campaign) == 0


def test_missing_promoted_binding_fails_closed_after_valid_human_approval(
    database: Database,
) -> None:
    proposal, decision = prepare_approved(
        database,
        spec_overrides={"capability_certificate_ref": ""},
    )

    result = execute_issue(
        database,
        issue_envelope(
            proposal.proposal_version_id,
            proposal.version,
            proposal.spec_hash,
            proposal.risk_summary_hash,
            decision.approval_decision_id,
        ),
    )

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "AUTHORIZATION_BINDING_INVALID"
    with database.session_factory.begin() as session:
        assert count_rows(session, TradingAuthorization) == 0


def test_unpersisted_capability_certificate_fails_closed_after_valid_approval(
    database: Database,
) -> None:
    proposal, decision = prepare_approved(database, issue_certificate=False)

    result = execute_issue(
        database,
        issue_envelope(
            proposal.proposal_version_id,
            proposal.version,
            proposal.spec_hash,
            proposal.risk_summary_hash,
            decision.approval_decision_id,
        ),
    )

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "CAPABILITY_CERTIFICATE_INVALID"
    assert result.data["message"] == "CAPABILITY_CERTIFICATE_NOT_FOUND"
    with database.session_factory.begin() as session:
        assert count_rows(session, TradingAuthorization) == 0


def test_concurrent_distinct_commands_create_exactly_one_authorization_and_campaign(
    database: Database,
) -> None:
    proposal, decision = prepare_approved(database)
    envelopes = tuple(
        issue_envelope(
            proposal.proposal_version_id,
            proposal.version,
            proposal.spec_hash,
            proposal.risk_summary_hash,
            decision.approval_decision_id,
            idempotency_key=f"concurrent-issue-{index}-{uuid4()}",
        )
        for index in range(2)
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda item: execute_issue(database, item), envelopes))

    assert {result.status for result in results} == {CommandStatus.COMPLETED}
    assert {result.object_id for result in results} == {results[0].object_id}
    assert sum(bool(result.data.get("already_issued")) for result in results) == 1
    with database.session_factory.begin() as session:
        assert count_rows(session, TradingAuthorization) == 1
        assert count_rows(session, Campaign) == 1
        assert count_rows(session, InitialOrderAuthorization) == 1


def test_same_idempotency_key_replays_without_duplicate_authorization(
    database: Database,
) -> None:
    proposal, decision = prepare_approved(database)
    envelope = issue_envelope(
        proposal.proposal_version_id,
        proposal.version,
        proposal.spec_hash,
        proposal.risk_summary_hash,
        decision.approval_decision_id,
        idempotency_key="stable-authorization-issue-key",
    )

    first = execute_issue(database, envelope)
    replay = execute_issue(database, envelope)

    assert first.status is CommandStatus.COMPLETED
    assert replay.status is CommandStatus.ALREADY_PROCESSED
    assert replay.replayed is True
    with database.session_factory.begin() as session:
        assert count_rows(session, TradingAuthorization) == 1


def test_existing_authorization_cannot_be_observed_through_mismatched_bindings(
    database: Database,
) -> None:
    proposal, decision = prepare_approved(database)
    first = execute_issue(
        database,
        issue_envelope(
            proposal.proposal_version_id,
            proposal.version,
            proposal.spec_hash,
            proposal.risk_summary_hash,
            decision.approval_decision_id,
        ),
    )
    assert first.status is CommandStatus.COMPLETED

    mismatched = execute_issue(
        database,
        issue_envelope(
            proposal.proposal_version_id,
            proposal.version,
            proposal.spec_hash,
            proposal.risk_summary_hash,
            uuid4(),
        ),
    )

    assert mismatched.status is CommandStatus.REJECTED
    assert mismatched.error_code == "AUTHORIZATION_BINDING_MISMATCH"
    with database.session_factory.begin() as session:
        assert count_rows(session, TradingAuthorization) == 1


def test_database_enforces_approval_and_proposal_composite_binding(database: Database) -> None:
    first_proposal, _first_decision = prepare_approved(database)
    second_proposal, second_decision = prepare_approved(database)
    now = datetime.now(UTC)
    snapshot = {"test_only": "mismatched-approval-proposal-binding"}

    with pytest.raises(IntegrityError, match="fk_trading_auth_approval_proposal_binding"):
        with database.session_factory.begin() as session:
            session.add(
                TradingAuthorization(
                    authorization_id=uuid4(),
                    proposal_version_id=first_proposal.proposal_version_id,
                    approval_decision_id=second_decision.approval_decision_id,
                    organization_id="org-1",
                    source="MANUAL",
                    risk_tier="LOW",
                    authorized_loss_capacity=first_proposal.one_r_0,
                    approved_initial_quantity=first_proposal.risk_approved_quantity,
                    auto_add_enabled=False,
                    requested_add_count=0,
                    total_capital_snapshot_0=first_proposal.total_capital_snapshot_0,
                    one_r_0=first_proposal.one_r_0,
                    frozen_trade_loss_cap=first_proposal.frozen_trade_loss_cap,
                    funding_envelope_0=first_proposal.funding_envelope_0,
                    risk_policy_version=first_proposal.risk_policy_version,
                    authorization_policy_version="authorization-policy-v1",
                    catalog_version=first_proposal.catalog_version,
                    execution_capability_version=first_proposal.execution_capability_version,
                    capability_certificate_ref="capability:test-shadow-only",
                    proposal_spec_hash=first_proposal.spec_hash,
                    risk_summary_hash=first_proposal.risk_summary_hash,
                    authorization_mode="SHADOW",
                    execution_eligible=False,
                    issuance_snapshot=snapshot,
                    issuance_snapshot_hash=hash_json(snapshot),
                    valid_until=min(
                        first_proposal.valid_until,
                        second_proposal.valid_until,
                    ),
                    issued_at=now,
                )
            )


def test_sync_no_new_position_invalidates_initial_and_all_add_capacity(
    database: Database,
) -> None:
    proposal, decision = prepare_approved(
        database,
        risk_tier="MEDIUM",
        auto_add_enabled=True,
        requested_add_count=2,
    )
    issued = execute_issue(
        database,
        issue_envelope(
            proposal.proposal_version_id,
            proposal.version,
            proposal.spec_hash,
            proposal.risk_summary_hash,
            decision.approval_decision_id,
        ),
    )
    authorization_id = UUID(str(issued.data["authorization_id"]))
    set_system_risk_state(database, "NO_NEW_POSITION")

    tightened = execute_tighten(database, tighten_envelope(authorization_id, "SYNC_RISK_STATE"))

    assert tightened.data["initial_status"] == "INVALIDATED"
    assert tightened.data["add_package_status"] == "INVALIDATED"
    assert set(tightened.data["add_unit_statuses"]) == {"INVALIDATED"}


def test_revoke_add_never_revokes_still_valid_initial_authorization(
    database: Database,
) -> None:
    proposal, decision = prepare_approved(
        database,
        auto_add_enabled=True,
        requested_add_count=1,
    )
    issued = execute_issue(
        database,
        issue_envelope(
            proposal.proposal_version_id,
            proposal.version,
            proposal.spec_hash,
            proposal.risk_summary_hash,
            decision.approval_decision_id,
        ),
    )
    authorization_id = UUID(str(issued.data["authorization_id"]))

    revoked = execute_tighten(
        database,
        tighten_envelope(authorization_id, "REVOKE_ADD", reason_code="USER_DISABLED_AUTO_ADD"),
    )

    assert revoked.data["initial_status"] == "ACTIVE"
    assert revoked.data["add_package_status"] == "REVOKED"
    assert revoked.data["add_unit_statuses"] == ["INVALIDATED"]


def test_sync_no_pyramid_is_one_way_and_keeps_initial_active(database: Database) -> None:
    proposal, decision = prepare_approved(
        database,
        risk_tier="HIGH",
        auto_add_enabled=True,
        requested_add_count=3,
    )
    issued = execute_issue(
        database,
        issue_envelope(
            proposal.proposal_version_id,
            proposal.version,
            proposal.spec_hash,
            proposal.risk_summary_hash,
            decision.approval_decision_id,
        ),
    )
    authorization_id = UUID(str(issued.data["authorization_id"]))
    set_system_risk_state(database, "NO_PYRAMID")

    tightened = execute_tighten(database, tighten_envelope(authorization_id, "SYNC_RISK_STATE"))
    repeated = execute_tighten(database, tighten_envelope(authorization_id, "SYNC_RISK_STATE"))

    assert tightened.data["initial_status"] == "ACTIVE"
    assert tightened.data["add_package_status"] == "INVALIDATED"
    assert set(tightened.data["add_unit_statuses"]) == {"INVALIDATED"}
    assert repeated.data["add_package_status"] == "INVALIDATED"
    with database.session_factory.begin() as session:
        package_state = session.execute(select(AddAuthorizationPackageState)).scalar_one()
        assert package_state.version == 2
        assert count_rows(session, AuthorizationStateTransition) == 10


def test_expiry_uses_injected_clock_and_expires_only_unconsumed_capacity(
    database: Database,
) -> None:
    proposal, decision = prepare_approved(
        database,
        risk_tier="MEDIUM",
        auto_add_enabled=True,
        requested_add_count=2,
    )
    clock_value = [datetime.now(UTC)]
    service = TradingAuthorizationService(clock=lambda: clock_value[0])
    issued = execute_issue(
        database,
        issue_envelope(
            proposal.proposal_version_id,
            proposal.version,
            proposal.spec_hash,
            proposal.risk_summary_hash,
            decision.approval_decision_id,
        ),
        service,
    )
    authorization_id = UUID(str(issued.data["authorization_id"]))

    early = execute_tighten(database, tighten_envelope(authorization_id, "EXPIRE"), service)
    assert early.error_code == "AUTHORIZATION_NOT_EXPIRED"

    clock_value[0] = proposal.valid_until + timedelta(seconds=1)
    expired = execute_tighten(database, tighten_envelope(authorization_id, "EXPIRE"), service)
    assert expired.data["initial_status"] == "EXPIRED"
    assert expired.data["add_package_status"] == "EXPIRED"
    assert set(expired.data["add_unit_statuses"]) == {"EXPIRED"}


def test_database_rejects_root_mutation_and_terminal_state_revival(database: Database) -> None:
    proposal, decision = prepare_approved(database)
    issued = execute_issue(
        database,
        issue_envelope(
            proposal.proposal_version_id,
            proposal.version,
            proposal.spec_hash,
            proposal.risk_summary_hash,
            decision.approval_decision_id,
        ),
    )
    authorization_id = UUID(str(issued.data["authorization_id"]))
    execute_tighten(database, tighten_envelope(authorization_id, "INVALIDATE_ALL"))

    with pytest.raises(DBAPIError, match="immutable"):
        with database.session_factory.begin() as session:
            session.execute(
                text(
                    "UPDATE trading_authorizations SET approved_initial_quantity = 2 "
                    "WHERE authorization_id = :authorization_id"
                ),
                {"authorization_id": authorization_id},
            )

    with pytest.raises(DBAPIError, match="invalid state transition"):
        with database.session_factory.begin() as session:
            session.execute(
                text(
                    "UPDATE initial_authorization_states "
                    "SET status = 'ACTIVE', version = version + 1, updated_at = now() "
                    "WHERE initial_authorization_id = "
                    "(SELECT initial_authorization_id FROM initial_order_authorizations "
                    "WHERE authorization_id = :authorization_id)"
                ),
                {"authorization_id": authorization_id},
            )


def test_issue_rejects_non_internal_service_without_authorization_rows(
    database: Database,
) -> None:
    proposal, decision = prepare_approved(database)
    result = execute_issue(
        database,
        issue_envelope(
            proposal.proposal_version_id,
            proposal.version,
            proposal.spec_hash,
            proposal.risk_summary_hash,
            decision.approval_decision_id,
            service_principal="untrusted-worker",
        ),
    )

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "INTERNAL_SERVICE_REQUIRED"
    with database.session_factory.begin() as session:
        assert count_rows(session, TradingAuthorization) == 0


def test_failure_after_partial_flush_rolls_back_root_children_receipt_and_events(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal, decision = prepare_approved(
        database,
        auto_add_enabled=True,
        requested_add_count=1,
    )
    envelope = issue_envelope(
        proposal.proposal_version_id,
        proposal.version,
        proposal.spec_hash,
        proposal.risk_summary_hash,
        decision.approval_decision_id,
    )
    service = TradingAuthorizationService()

    def fail_after_root_flush(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated add-package persistence failure")

    monkeypatch.setattr(service, "_create_add_package", fail_after_root_flush)

    with pytest.raises(RuntimeError, match="simulated add-package persistence failure"):
        execute_issue(database, envelope, service)

    with database.session_factory.begin() as session:
        assert count_rows(session, TradingAuthorization) == 0
        assert count_rows(session, Campaign) == 0
        assert count_rows(session, InitialOrderAuthorization) == 0
        assert (
            session.execute(
                select(func.count())
                .select_from(CommandReceipt)
                .where(CommandReceipt.command_id == envelope.command_id)
            ).scalar_one()
            == 0
        )
        assert (
            session.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.command_id == envelope.command_id)
            ).scalar_one()
            == 0
        )
        assert (
            session.execute(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.headers["command_id"].astext == str(envelope.command_id))
            ).scalar_one()
            == 0
        )
