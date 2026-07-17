from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from tests.integration.test_authorization import create_assurance, seed_principal
from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import (
    CommandChannel,
    CommandEnvelope,
    CommandStatus,
    hash_json,
)
from trading_control_plane.database import Database
from trading_control_plane.iam_models import AuthorizationDecision, PermissionScope
from trading_control_plane.proposal_models import (
    ApprovalDecision,
    FrozenProposalVersion,
    ProposalVersionState,
    ReviewerVote,
    SystemRiskStateRecord,
)
from trading_control_plane.review import ProposalReviewService, ReviewChoice

pytestmark = pytest.mark.integration


def count_rows(session: Session, model: type[object]) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def seed_reviewer(database: Database) -> UUID:
    principal_id, assignment_id = seed_principal(database)
    assert assignment_id is not None
    with database.session_factory.begin() as session:
        session.execute(
            update(PermissionScope)
            .where(PermissionScope.assignment_id == assignment_id)
            .values(action_id=None)
        )
    return principal_id


def seed_proposal(
    database: Database,
    *,
    creator_id: UUID | None = None,
    risk_tier: str = "LOW",
    valid_until: datetime | None = None,
    source: str = "MANUAL",
    risk_summary_hash_override: str | None = None,
    one_r_override: Decimal | None = None,
) -> FrozenProposalVersion:
    now = datetime.now(UTC)
    proposal_version_id = uuid4()
    proposal_id = uuid4()
    risk_summary = {
        "risk_tier": risk_tier,
        "total_capital_snapshot_0": "100000",
        "one_r_0": "500",
    }
    spec = {
        "instrument_id": "BINANCE:BTCUSDT-PERP",
        "direction": "LONG",
        "requested_quantity": "1",
    }
    multiplier = {"LOW": Decimal("1"), "MEDIUM": Decimal("2"), "HIGH": Decimal("3")}
    leverage = {"LOW": Decimal("3"), "MEDIUM": Decimal("5"), "HIGH": Decimal("10")}
    creator = creator_id or uuid4()
    proposal = FrozenProposalVersion(
        proposal_version_id=proposal_version_id,
        proposal_id=proposal_id,
        version=1,
        organization_id="org-1",
        source=source,
        proposal_purpose="INITIAL_ENTRY",
        creator_principal_id=creator if source == "MANUAL" else None,
        creator_service_principal="strategy:trend-v1" if source == "SYSTEM" else None,
        business_owner_principal_id=uuid4() if source == "SYSTEM" else None,
        strategy_id="trend-breakout" if source == "SYSTEM" else None,
        strategy_version="1.0.0" if source == "SYSTEM" else None,
        account_id="account-1",
        venue="BINANCE",
        execution_domain="BINANCE_USDM",
        instrument_id="BINANCE:BTCUSDT-PERP",
        sector="CRYPTO",
        direction="LONG",
        decision_timeframe="4h",
        order_type="LIMIT",
        trigger_price=Decimal("100"),
        limit_price=Decimal("100.5"),
        max_slippage_bps=Decimal("20"),
        requested_quantity=Decimal("1"),
        risk_approved_quantity=Decimal("0.5"),
        reduce_only=False,
        initial_invalidation_price=Decimal("90"),
        requested_max_r=multiplier[risk_tier],
        risk_tier=risk_tier,
        auto_add_enabled=False,
        requested_add_count=0,
        target_leverage_min=Decimal("1"),
        target_leverage_max=leverage[risk_tier],
        hypothesis="trend continuation",
        supporting_reason="confirmed breakout structure",
        counter_thesis="failed breakout",
        data_as_of=now,
        market_state="OPEN",
        total_capital_snapshot_0=Decimal("100000"),
        funding_envelope_0=Decimal("1500"),
        one_r_0=one_r_override or Decimal("500"),
        frozen_trade_loss_cap=Decimal("500") * multiplier[risk_tier],
        risk_decision_ref=f"risk-decision:{uuid4()}",
        risk_precheck_status="PASSED",
        risk_policy_version="risk-policy-v1",
        catalog_version="catalog-v1",
        execution_capability_version="shadow-only-v1",
        spec=spec,
        spec_hash=hash_json(spec),
        risk_summary=risk_summary,
        risk_summary_hash=risk_summary_hash_override or hash_json(risk_summary),
        valid_from=now - timedelta(minutes=1),
        valid_until=valid_until or now + timedelta(minutes=10),
        frozen_at=now - timedelta(seconds=30),
    )
    with database.session_factory.begin() as session:
        session.add(proposal)
        session.flush()
        session.add(
            ProposalVersionState(
                proposal_version_id=proposal_version_id,
                status="FROZEN",
                version=1,
                reason_code="RISK_PRECHECK_PASSED",
                updated_at=now,
            )
        )
        session.add(
            SystemRiskStateRecord(
                organization_id="org-1",
                status="NORMAL",
                version=1,
                reason_code="INTEGRATION_FIXTURE",
                policy_version="risk-state-v1",
                transition_source_ref="test-only:review-fixture",
                updated_at=now,
            )
        )
    return proposal


def review_envelope(
    proposal: FrozenProposalVersion,
    reviewer_id: UUID,
    *,
    choice: ReviewChoice = ReviewChoice.APPROVE,
    assurance_id: UUID | None = None,
    auth_context_ref: str = "auth:review-fixture",
    channel: CommandChannel = CommandChannel.WEB,
) -> CommandEnvelope:
    now = datetime.now(UTC)
    return CommandEnvelope(
        idempotency_key=f"review-{uuid4()}",
        command_type="proposal.review.v1",
        object_type="ProposalVersion",
        object_id=str(proposal.proposal_version_id),
        expected_version=proposal.version,
        actor_id=str(reviewer_id),
        channel=channel,
        scope={"organization_id": proposal.organization_id},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref=auth_context_ref,
        payload_schema_version=1,
        reason="review frozen proposal",
        payload={
            "choice": choice.value,
            "reason": "review evidence recorded",
            "risk_summary_hash": proposal.risk_summary_hash,
            "assurance_id": str(assurance_id) if assurance_id else None,
            "device_ref": "device-1" if assurance_id else None,
        },
    )


def execute_review(
    database: Database,
    envelope: CommandEnvelope,
):
    return IdempotentCommandExecutor(database.session_factory).execute(
        envelope, ProposalReviewService().review
    )


def approval_assurance(
    database: Database,
    proposal: FrozenProposalVersion,
    reviewer_id: UUID,
    *,
    channel: str = "WEB",
) -> tuple[UUID, str]:
    return create_assurance(
        database,
        principal_id=reviewer_id,
        object_id=str(proposal.proposal_version_id),
        channel=channel,
    )


def test_low_risk_independent_vote_finalizes_without_authorization(
    database: Database,
) -> None:
    proposal = seed_proposal(database, risk_tier="LOW")
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

    assert result.status is CommandStatus.COMPLETED
    assert result.data["approval_status"] == "APPROVED"
    assert result.data["trading_authorization_created"] is False
    with database.session_factory.begin() as session:
        assert count_rows(session, ReviewerVote) == 1
        decision = session.execute(select(ApprovalDecision)).scalar_one()
        assert (decision.approved_count, decision.required_quorum) == (1, 1)
        assert (
            session.execute(
                text("SELECT to_regclass('public.trading_authorizations')")
            ).scalar_one_or_none()
            is None
        )


def test_high_risk_first_vote_stays_pending_until_second_reviewer(
    database: Database,
) -> None:
    proposal = seed_proposal(database, risk_tier="HIGH")
    first_reviewer = seed_reviewer(database)
    second_reviewer = seed_reviewer(database)
    first_assurance, first_context = approval_assurance(database, proposal, first_reviewer)
    second_assurance, second_context = approval_assurance(database, proposal, second_reviewer)

    first = execute_review(
        database,
        review_envelope(
            proposal,
            first_reviewer,
            assurance_id=first_assurance,
            auth_context_ref=first_context,
        ),
    )
    second = execute_review(
        database,
        review_envelope(
            proposal,
            second_reviewer,
            assurance_id=second_assurance,
            auth_context_ref=second_context,
        ),
    )

    assert first.status is CommandStatus.ACCEPTED
    assert first.data["approval_status"] == "PENDING"
    assert first.data["approved_count"] == 1
    assert second.status is CommandStatus.COMPLETED
    assert second.data["approval_status"] == "APPROVED"
    with database.session_factory.begin() as session:
        assert count_rows(session, ReviewerVote) == 2
        decision = session.execute(select(ApprovalDecision)).scalar_one()
        assert (decision.status, decision.approved_count, decision.version) == (
            "APPROVED",
            2,
            2,
        )


def test_two_high_risk_votes_aggregate_atomically_under_concurrency(
    database: Database,
) -> None:
    proposal = seed_proposal(database, risk_tier="HIGH")
    reviewers = (seed_reviewer(database), seed_reviewer(database))
    assurances = tuple(approval_assurance(database, proposal, reviewer) for reviewer in reviewers)
    envelopes = tuple(
        review_envelope(
            proposal,
            reviewer,
            assurance_id=assurance[0],
            auth_context_ref=assurance[1],
        )
        for reviewer, assurance in zip(reviewers, assurances, strict=True)
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda envelope: execute_review(database, envelope), envelopes))

    assert {result.status for result in results} == {
        CommandStatus.ACCEPTED,
        CommandStatus.COMPLETED,
    }
    with database.session_factory.begin() as session:
        decision = session.execute(select(ApprovalDecision)).scalar_one()
        assert (decision.status, decision.approved_count, decision.version) == (
            "APPROVED",
            2,
            2,
        )
        assert count_rows(session, ReviewerVote) == 2


def test_self_review_is_denied_without_vote(database: Database) -> None:
    reviewer = seed_reviewer(database)
    proposal = seed_proposal(database, creator_id=reviewer)
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

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "SELF_REVIEW_FORBIDDEN"
    with database.session_factory.begin() as session:
        assert count_rows(session, ReviewerVote) == 0
        assert count_rows(session, ApprovalDecision) == 0
        assert count_rows(session, AuthorizationDecision) == 1


def test_system_risk_state_cannot_be_overridden_by_reviewer(database: Database) -> None:
    proposal = seed_proposal(database)
    reviewer = seed_reviewer(database)
    assurance_id, context_ref = approval_assurance(database, proposal, reviewer)
    with database.session_factory.begin() as session:
        state = session.get(SystemRiskStateRecord, "org-1")
        assert state is not None
        state.status = "NO_NEW_POSITION"
        state.version = 2

    result = execute_review(
        database,
        review_envelope(
            proposal,
            reviewer,
            assurance_id=assurance_id,
            auth_context_ref=context_ref,
        ),
    )

    assert result.error_code == "SYSTEM_RISK_STATE_DENY"
    with database.session_factory.begin() as session:
        assert count_rows(session, ReviewerVote) == 0
        assert count_rows(session, ApprovalDecision) == 0


@pytest.mark.parametrize(
    ("choice", "expected_status"),
    [(ReviewChoice.REJECT, "REJECTED"), (ReviewChoice.RETURN, "RETURNED")],
)
def test_reject_or_return_is_immediately_terminal(
    database: Database,
    choice: ReviewChoice,
    expected_status: str,
) -> None:
    proposal = seed_proposal(database, risk_tier="HIGH")
    reviewer = seed_reviewer(database)

    result = execute_review(
        database,
        review_envelope(proposal, reviewer, choice=choice),
    )

    assert result.status is CommandStatus.COMPLETED
    assert result.data["approval_status"] == expected_status
    with database.session_factory.begin() as session:
        decision = session.execute(select(ApprovalDecision)).scalar_one()
        assert decision.status == expected_status
        assert count_rows(session, ReviewerVote) == 1


def test_terminal_decision_ignores_late_vote_and_creates_no_second_vote(
    database: Database,
) -> None:
    proposal = seed_proposal(database, risk_tier="LOW")
    rejecting_reviewer = seed_reviewer(database)
    late_reviewer = seed_reviewer(database)
    execute_review(
        database,
        review_envelope(proposal, rejecting_reviewer, choice=ReviewChoice.REJECT),
    )
    assurance_id, context_ref = approval_assurance(database, proposal, late_reviewer)

    late = execute_review(
        database,
        review_envelope(
            proposal,
            late_reviewer,
            assurance_id=assurance_id,
            auth_context_ref=context_ref,
        ),
    )

    assert late.status is CommandStatus.COMPLETED
    assert late.data["approval_status"] == "REJECTED"
    assert late.data["already_terminal"] is True
    with database.session_factory.begin() as session:
        assert count_rows(session, ReviewerVote) == 1


def test_same_reviewer_cannot_change_vote_on_pending_high_risk(
    database: Database,
) -> None:
    proposal = seed_proposal(database, risk_tier="HIGH")
    reviewer = seed_reviewer(database)
    assurance_id, context_ref = approval_assurance(database, proposal, reviewer)
    execute_review(
        database,
        review_envelope(
            proposal,
            reviewer,
            assurance_id=assurance_id,
            auth_context_ref=context_ref,
        ),
    )

    conflict = execute_review(
        database,
        review_envelope(proposal, reviewer, choice=ReviewChoice.REJECT),
    )

    assert conflict.status is CommandStatus.REJECTED
    assert conflict.error_code == "VOTE_CONFLICT"
    with database.session_factory.begin() as session:
        assert count_rows(session, ReviewerVote) == 1
        decision = session.execute(select(ApprovalDecision)).scalar_one()
        assert decision.status == "PENDING"


def test_expired_proposal_creates_expired_decision_without_vote(
    database: Database,
) -> None:
    proposal = seed_proposal(
        database,
        valid_until=datetime.now(UTC) - timedelta(seconds=1),
    )
    reviewer = seed_reviewer(database)

    result = execute_review(database, review_envelope(proposal, reviewer))

    assert result.error_code == "PROPOSAL_EXPIRED"
    with database.session_factory.begin() as session:
        decision = session.execute(select(ApprovalDecision)).scalar_one()
        assert decision.status == "EXPIRED"
        assert count_rows(session, ReviewerVote) == 0


def test_frozen_proposal_vote_and_terminal_decision_are_immutable(
    database: Database,
) -> None:
    proposal = seed_proposal(database)
    reviewer = seed_reviewer(database)
    execute_review(
        database,
        review_envelope(proposal, reviewer, choice=ReviewChoice.REJECT),
    )

    with pytest.raises(DBAPIError, match="proposal_versions is immutable"):
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE proposal_versions SET risk_tier = 'HIGH' "
                    "WHERE proposal_version_id = :proposal_version_id"
                ),
                {"proposal_version_id": proposal.proposal_version_id},
            )
    with pytest.raises(DBAPIError, match="reviewer_votes is append-only"):
        with database.engine.begin() as connection:
            connection.execute(text("DELETE FROM reviewer_votes"))
    with pytest.raises(DBAPIError, match="terminal approval_decision is immutable"):
        with database.engine.begin() as connection:
            connection.execute(
                text("UPDATE approval_decisions SET status = 'APPROVED', version = version + 1")
            )


def test_system_proposal_can_be_reviewed_but_not_self_reviewed_by_service(
    database: Database,
) -> None:
    proposal = seed_proposal(database, source="SYSTEM")
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

    assert result.status is CommandStatus.COMPLETED
    assert result.data["approval_status"] == "APPROVED"


def test_frozen_risk_formulas_are_enforced_by_database(database: Database) -> None:
    with pytest.raises(
        IntegrityError,
        match=r"ck_proposal_versions_(one_r|loss_cap)_formula",
    ):
        seed_proposal(database, one_r_override=Decimal("600"))


def test_mismatched_frozen_hash_is_rejected_before_vote(database: Database) -> None:
    proposal = seed_proposal(database, risk_summary_hash_override="b" * 64)
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

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "PROPOSAL_INTEGRITY_FAILED"
    with database.session_factory.begin() as session:
        assert count_rows(session, ReviewerVote) == 0


def test_telegram_approval_requires_bound_passkey_step_up(database: Database) -> None:
    proposal = seed_proposal(database)
    reviewer = seed_reviewer(database)

    missing = execute_review(
        database,
        review_envelope(proposal, reviewer, channel=CommandChannel.TELEGRAM),
    )
    assert missing.error_code == "MFA_REQUIRED"

    assurance_id, context_ref = approval_assurance(
        database,
        proposal,
        reviewer,
        channel="TELEGRAM",
    )
    allowed = execute_review(
        database,
        review_envelope(
            proposal,
            reviewer,
            assurance_id=assurance_id,
            auth_context_ref=context_ref,
            channel=CommandChannel.TELEGRAM,
        ),
    )

    assert allowed.status is CommandStatus.COMPLETED
    assert allowed.data["approval_status"] == "APPROVED"
