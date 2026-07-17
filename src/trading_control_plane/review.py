from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_control_plane.authorization import (
    POLICY_VERSION,
    AuthorizationEvaluation,
    AuthorizationEvaluator,
    AuthorizationRequest,
    AuthorizationResult,
    RiskEngineStatus,
    RiskTier,
    SystemRiskState,
)
from trading_control_plane.commands import (
    CommandEnvelope,
    CommandOutcome,
    CommandRejected,
    CommandStatus,
    DomainEvent,
    hash_json,
)
from trading_control_plane.proposal_models import (
    ApprovalDecision,
    FrozenProposalVersion,
    ProposalVersionState,
    ReviewerVote,
    SystemRiskStateRecord,
)


class ReviewChoice(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    RETURN = "RETURN"


ACTION_BY_CHOICE = {
    ReviewChoice.APPROVE: "ACT-PROPOSAL-APPROVE",
    ReviewChoice.REJECT: "ACT-PROPOSAL-REJECT",
    ReviewChoice.RETURN: "ACT-PROPOSAL-RETURN",
}
TERMINAL_DECISIONS = frozenset({"APPROVED", "REJECTED", "RETURNED", "EXPIRED", "ABANDONED"})


class ReviewRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    choice: ReviewChoice
    reason: str = Field(min_length=1, max_length=1000)
    risk_summary_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    assurance_id: UUID | None = None
    device_ref: str | None = Field(default=None, max_length=255)


class ProposalReviewService:
    """Records reviewer facts and aggregates one decision; never issues authorization."""

    command_type = "proposal.review.v1"

    def __init__(self, authorization: AuthorizationEvaluator | None = None) -> None:
        self._authorization = authorization or AuthorizationEvaluator()

    def review(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        if envelope.command_type != self.command_type:
            raise CommandRejected("COMMAND_TYPE_MISMATCH", "unexpected command type")
        if envelope.object_type != "ProposalVersion" or envelope.object_id is None:
            raise CommandRejected("OBJECT_BINDING_MISMATCH", "ProposalVersion is required")
        if envelope.actor_id is None:
            raise CommandRejected("HUMAN_REVIEWER_REQUIRED", "review requires a human actor")
        try:
            reviewer_id = UUID(envelope.actor_id)
            proposal_version_id = UUID(envelope.object_id)
        except ValueError as exc:
            raise CommandRejected("IDENTIFIER_INVALID", "review identifiers must be UUIDs") from exc

        request = ReviewRequest.model_validate(envelope.payload)
        proposal = session.execute(
            select(FrozenProposalVersion)
            .where(FrozenProposalVersion.proposal_version_id == proposal_version_id)
            .with_for_update()
        ).scalar_one_or_none()
        if proposal is None:
            raise CommandRejected("PROPOSAL_NOT_FOUND", "proposal version is unavailable")
        if proposal.proposal_purpose != "INITIAL_ENTRY":
            raise CommandRejected(
                "PROPOSAL_PURPOSE_MISMATCH", "initial-entry review cannot process this proposal"
            )
        if (
            hash_json(proposal.spec) != proposal.spec_hash
            or hash_json(proposal.risk_summary) != proposal.risk_summary_hash
        ):
            raise CommandRejected(
                "PROPOSAL_INTEGRITY_FAILED", "frozen proposal hash verification failed"
            )
        if envelope.expected_version != proposal.version:
            raise CommandRejected("VERSION_CONFLICT", "proposal version binding changed")
        if request.risk_summary_hash != proposal.risk_summary_hash:
            raise CommandRejected("RISK_SUMMARY_MISMATCH", "risk summary binding changed")

        state = session.execute(
            select(ProposalVersionState)
            .where(ProposalVersionState.proposal_version_id == proposal_version_id)
            .with_for_update()
        ).scalar_one_or_none()
        if state is None:
            raise CommandRejected("PROPOSAL_STATE_MISSING", "proposal state is unavailable")

        now = datetime.now(UTC)
        decision = session.execute(
            select(ApprovalDecision)
            .where(ApprovalDecision.proposal_version_id == proposal_version_id)
            .with_for_update()
        ).scalar_one_or_none()

        if state.status != "FROZEN":
            return self._non_reviewable_outcome(proposal, decision, "PROPOSAL_NOT_REVIEWABLE")
        if proposal.valid_until <= now and not (
            decision is not None and decision.status in TERMINAL_DECISIONS
        ):
            decision = self._expire_decision(session, proposal, decision, now)
            return CommandOutcome(
                status=CommandStatus.REJECTED,
                object_type="ProposalVersion",
                object_id=str(proposal_version_id),
                object_version=proposal.version,
                error_code="PROPOSAL_EXPIRED",
                data=self._decision_data(decision, vote_id=None),
                events=(self._decision_event(proposal, decision, "ApprovalDecisionExpired"),),
            )

        if decision is not None and decision.status in TERMINAL_DECISIONS:
            view_evaluation = self._authorize(
                session,
                envelope,
                proposal,
                reviewer_id,
                action_id="ACT-PROPOSAL-VIEW",
                request=request,
                system_state=SystemRiskState.UNKNOWN,
            )
            if view_evaluation.result is AuthorizationResult.DENY:
                return self._authorization_denied(proposal, view_evaluation)
            return CommandOutcome(
                status=CommandStatus.COMPLETED,
                object_type="ProposalVersion",
                object_id=str(proposal_version_id),
                object_version=proposal.version,
                data={
                    **self._decision_data(decision, vote_id=None),
                    "already_terminal": True,
                    "trading_authorization_created": False,
                },
                events=(
                    self._decision_event(proposal, decision, "ReviewActionObservedAfterTerminal"),
                ),
            )

        existing_vote = session.execute(
            select(ReviewerVote).where(
                ReviewerVote.proposal_version_id == proposal_version_id,
                ReviewerVote.reviewer_principal_id == reviewer_id,
            )
        ).scalar_one_or_none()
        if existing_vote is not None:
            if existing_vote.choice != request.choice.value:
                return CommandOutcome(
                    status=CommandStatus.REJECTED,
                    object_type="ProposalVersion",
                    object_id=str(proposal_version_id),
                    object_version=proposal.version,
                    error_code="VOTE_CONFLICT",
                    data={"existing_vote_id": str(existing_vote.vote_id)},
                    events=(
                        DomainEvent(
                            event_type="ReviewerVoteConflictDetected",
                            aggregate_type="ProposalVersion",
                            aggregate_id=str(proposal_version_id),
                            payload={"existing_vote_id": str(existing_vote.vote_id)},
                        ),
                    ),
                )
            if decision is None:  # pragma: no cover - database invariant
                raise RuntimeError("reviewer vote exists without approval decision")
            return CommandOutcome(
                status=CommandStatus.COMPLETED,
                object_type="ProposalVersion",
                object_id=str(proposal_version_id),
                object_version=proposal.version,
                data={
                    **self._decision_data(decision, vote_id=existing_vote.vote_id),
                    "already_recorded": True,
                    "trading_authorization_created": False,
                },
                events=(
                    DomainEvent(
                        event_type="ReviewerVoteAlreadyRecorded",
                        aggregate_type="ProposalVersion",
                        aggregate_id=str(proposal_version_id),
                        payload={"vote_id": str(existing_vote.vote_id)},
                    ),
                ),
            )

        system_state_record = session.get(SystemRiskStateRecord, proposal.organization_id)
        system_state = (
            SystemRiskState(system_state_record.status)
            if system_state_record is not None
            else SystemRiskState.UNKNOWN
        )
        evaluation = self._authorize(
            session,
            envelope,
            proposal,
            reviewer_id,
            action_id=ACTION_BY_CHOICE[request.choice],
            request=request,
            system_state=system_state,
        )
        session.flush()
        if evaluation.result is AuthorizationResult.DENY:
            return self._authorization_denied(proposal, evaluation)

        decision_was_existing = decision is not None
        if decision is None:
            decision = ApprovalDecision(
                approval_decision_id=uuid4(),
                proposal_version_id=proposal_version_id,
                status="PENDING",
                required_quorum=2 if proposal.risk_tier == "HIGH" else 1,
                approved_count=0,
                version=1,
                terminal_reason_code=None,
                valid_until=proposal.valid_until,
                created_at=now,
                updated_at=now,
                terminal_at=None,
            )
            session.add(decision)

        vote = ReviewerVote(
            vote_id=uuid4(),
            proposal_version_id=proposal_version_id,
            reviewer_principal_id=reviewer_id,
            choice=request.choice.value,
            reason=request.reason,
            authorization_decision_id=evaluation.decision_id,
            auth_context_ref=envelope.auth_context_ref,
            risk_summary_hash=proposal.risk_summary_hash,
            policy_version=POLICY_VERSION,
            channel=envelope.channel.value,
            decided_at=now,
        )
        session.add(vote)

        if request.choice is ReviewChoice.APPROVE:
            decision.approved_count += 1
            if decision.approved_count >= decision.required_quorum:
                self._make_terminal(
                    decision,
                    "APPROVED",
                    "QUORUM_MET",
                    now,
                    decision_was_existing,
                )
            elif decision_was_existing:
                decision.version += 1
                decision.updated_at = now
        elif request.choice is ReviewChoice.REJECT:
            self._make_terminal(
                decision,
                "REJECTED",
                "REVIEWER_REJECTED",
                now,
                decision_was_existing,
            )
        else:
            self._make_terminal(
                decision,
                "RETURNED",
                "REVIEWER_RETURNED",
                now,
                decision_was_existing,
            )

        pending = decision.status == "PENDING"
        return CommandOutcome(
            status=CommandStatus.ACCEPTED if pending else CommandStatus.COMPLETED,
            object_type="ProposalVersion",
            object_id=str(proposal_version_id),
            object_version=proposal.version,
            data={
                **self._decision_data(decision, vote_id=vote.vote_id),
                "trading_authorization_created": False,
            },
            events=(
                DomainEvent(
                    event_type="ReviewerVoteRecorded",
                    aggregate_type="ProposalVersion",
                    aggregate_id=str(proposal_version_id),
                    payload={
                        "vote_id": str(vote.vote_id),
                        "choice": request.choice.value,
                        "authorization_decision_id": str(evaluation.decision_id),
                    },
                ),
                self._decision_event(
                    proposal,
                    decision,
                    "ApprovalDecisionPending" if pending else "ApprovalDecisionFinalized",
                ),
            ),
        )

    def _authorize(
        self,
        session: Session,
        envelope: CommandEnvelope,
        proposal: FrozenProposalVersion,
        reviewer_id: UUID,
        *,
        action_id: str,
        request: ReviewRequest,
        system_state: SystemRiskState,
    ) -> AuthorizationEvaluation:
        creator_service = proposal.creator_service_principal
        authorization_request = AuthorizationRequest(
            principal_id=reviewer_id,
            action_id=action_id,
            object_type="ProposalVersion",
            object_id=str(proposal.proposal_version_id),
            object_version=proposal.version,
            organization_id=proposal.organization_id,
            account_id=proposal.account_id,
            venue=proposal.venue,
            sector=proposal.sector,
            risk_tier=RiskTier(proposal.risk_tier),
            channel=envelope.channel,
            online=True,
            device_ref=request.device_ref,
            assurance_id=request.assurance_id,
            resource_creator_id=proposal.creator_principal_id,
            resource_creator_service_principal=creator_service,
            resource_status="FROZEN",
            resource_valid_until=proposal.valid_until,
            risk_engine_status=RiskEngineStatus(proposal.risk_precheck_status),
            system_risk_state=system_state,
            requested_at=envelope.issued_at,
        )
        return self._authorization.evaluate(session, envelope, authorization_request)

    @staticmethod
    def _authorization_denied(
        proposal: FrozenProposalVersion,
        evaluation: AuthorizationEvaluation,
    ) -> CommandOutcome:
        return CommandOutcome(
            status=CommandStatus.REJECTED,
            object_type="ProposalVersion",
            object_id=str(proposal.proposal_version_id),
            object_version=proposal.version,
            error_code=evaluation.reason_code,
            data={
                "authorization_decision_id": str(evaluation.decision_id),
                "trading_authorization_created": False,
            },
            events=(
                DomainEvent(
                    event_type="ReviewAuthorizationDenied",
                    aggregate_type="ProposalVersion",
                    aggregate_id=str(proposal.proposal_version_id),
                    payload={
                        "authorization_decision_id": str(evaluation.decision_id),
                        "reason_code": evaluation.reason_code,
                    },
                ),
            ),
        )

    @staticmethod
    def _make_terminal(
        decision: ApprovalDecision,
        status: str,
        reason_code: str,
        now: datetime,
        was_existing: bool,
    ) -> None:
        decision.status = status
        decision.terminal_reason_code = reason_code
        decision.terminal_at = now
        decision.updated_at = now
        if was_existing:
            decision.version += 1

    @staticmethod
    def _expire_decision(
        session: Session,
        proposal: FrozenProposalVersion,
        decision: ApprovalDecision | None,
        now: datetime,
    ) -> ApprovalDecision:
        if decision is None:
            decision = ApprovalDecision(
                approval_decision_id=uuid4(),
                proposal_version_id=proposal.proposal_version_id,
                status="EXPIRED",
                required_quorum=2 if proposal.risk_tier == "HIGH" else 1,
                approved_count=0,
                version=1,
                terminal_reason_code="PROPOSAL_EXPIRED",
                valid_until=proposal.valid_until,
                created_at=now,
                updated_at=now,
                terminal_at=now,
            )
            session.add(decision)
        elif decision.status == "PENDING":
            ProposalReviewService._make_terminal(
                decision, "EXPIRED", "PROPOSAL_EXPIRED", now, was_existing=True
            )
        return decision

    @staticmethod
    def _non_reviewable_outcome(
        proposal: FrozenProposalVersion,
        decision: ApprovalDecision | None,
        error_code: str,
    ) -> CommandOutcome:
        data: dict[str, JsonValue] = (
            ProposalReviewService._decision_data(decision, vote_id=None)
            if decision is not None
            else {"trading_authorization_created": False}
        )
        return CommandOutcome(
            status=CommandStatus.REJECTED,
            object_type="ProposalVersion",
            object_id=str(proposal.proposal_version_id),
            object_version=proposal.version,
            error_code=error_code,
            data=data,
            events=(
                DomainEvent(
                    event_type="ProposalReviewRejected",
                    aggregate_type="ProposalVersion",
                    aggregate_id=str(proposal.proposal_version_id),
                    payload={"reason_code": error_code},
                ),
            ),
        )

    @staticmethod
    def _decision_data(
        decision: ApprovalDecision,
        *,
        vote_id: UUID | None,
    ) -> dict[str, JsonValue]:
        return {
            "approval_decision_id": str(decision.approval_decision_id),
            "approval_status": decision.status,
            "approved_count": decision.approved_count,
            "required_quorum": decision.required_quorum,
            "decision_version": decision.version,
            "vote_id": str(vote_id) if vote_id else None,
        }

    @staticmethod
    def _decision_event(
        proposal: FrozenProposalVersion,
        decision: ApprovalDecision,
        event_type: str,
    ) -> DomainEvent:
        return DomainEvent(
            event_type=event_type,
            aggregate_type="ProposalVersion",
            aggregate_id=str(proposal.proposal_version_id),
            payload={
                "approval_decision_id": str(decision.approval_decision_id),
                "status": decision.status,
                "approved_count": decision.approved_count,
                "required_quorum": decision.required_quorum,
                "decision_version": decision.version,
                "trading_authorization_created": False,
            },
        )
