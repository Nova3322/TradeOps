from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_control_plane.commands import (
    CommandChannel,
    CommandEnvelope,
    CommandOutcome,
    CommandRejected,
    CommandStatus,
    DomainEvent,
    hash_json,
)
from trading_control_plane.metrics import AUTHORIZATION_ISSUANCE, AUTHORIZATION_TIGHTENING
from trading_control_plane.proposal_models import (
    ApprovalDecision,
    FrozenProposalVersion,
    ProposalVersionState,
    ReviewerVote,
    SystemRiskStateRecord,
)
from trading_control_plane.trading_authorization_models import (
    AddAuthorizationPackage,
    AddAuthorizationPackageState,
    AddUnit,
    AddUnitState,
    Campaign,
    CampaignState,
    InitialAuthorizationState,
    InitialOrderAuthorization,
    TradingAuthorization,
)

AUTHORIZATION_SERVICE_PRINCIPAL = "trading-authorization-service"
ADD_MILESTONES = (30, 50, 100)
INITIAL_TERMINAL = frozenset({"CONSUMED", "EXPIRED", "REVOKED", "INVALIDATED"})
ADD_PACKAGE_TERMINAL = frozenset({"EXHAUSTED", "REVOKED", "EXPIRED", "INVALIDATED"})
ADD_UNIT_TERMINAL = frozenset({"CONSUMED", "EXPIRED", "INVALIDATED"})
RISK_STATES_INVALIDATING_INITIAL = frozenset(
    {"NO_NEW_POSITION", "REDUCE_ONLY", "KILL_SWITCH", "UNKNOWN"}
)


class FrozenAuthorizationBinding(BaseModel):
    """Required immutable bindings promoted into authorization/campaign facts."""

    model_config = ConfigDict(frozen=True, extra="allow")

    strategy_id: str = Field(min_length=1, max_length=160)
    strategy_version: str = Field(min_length=1, max_length=120)
    strategy_parameter_version: str = Field(min_length=1, max_length=120)
    account_abstraction: str = Field(min_length=1, max_length=80)
    margin_mode: str = Field(min_length=1, max_length=80)
    collateral_scope: str = Field(min_length=1, max_length=120)
    collateral_pool_id: str = Field(min_length=1, max_length=160)
    authorization_policy_version: str = Field(min_length=1, max_length=120)
    position_management_template_version: str = Field(min_length=1, max_length=120)
    add_milestone_policy_version: str = Field(min_length=1, max_length=120)
    adapter_version: str = Field(min_length=1, max_length=120)
    freqtrade_worker_version: str = Field(min_length=1, max_length=120)
    account_capability_version: str = Field(min_length=1, max_length=120)
    capability_certificate_ref: str = Field(min_length=1, max_length=255)


class IssueAuthorizationRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    approval_decision_id: UUID
    proposal_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    risk_summary_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class AuthorizationTightenAction(StrEnum):
    SYNC_RISK_STATE = "SYNC_RISK_STATE"
    EXPIRE = "EXPIRE"
    REVOKE_ADD = "REVOKE_ADD"
    REVOKE_ALL = "REVOKE_ALL"
    INVALIDATE_ALL = "INVALIDATE_ALL"


class TightenAuthorizationRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: AuthorizationTightenAction
    reason_code: str = Field(min_length=3, max_length=160)


class TradingAuthorizationService:
    issue_command_type = "trading.authorization.issue.v1"
    tighten_command_type = "trading.authorization.tighten.v1"

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def issue(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        self._require_internal(envelope, self.issue_command_type, "ProposalVersion")
        if envelope.object_id is None:  # pragma: no cover - enforced above
            raise RuntimeError("missing proposal id")
        try:
            proposal_version_id = UUID(envelope.object_id)
        except ValueError as exc:
            raise CommandRejected("IDENTIFIER_INVALID", "proposal version id must be UUID") from exc
        try:
            request = IssueAuthorizationRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("COMMAND_PAYLOAD_INVALID", str(exc)) from exc

        proposal = session.execute(
            select(FrozenProposalVersion)
            .where(FrozenProposalVersion.proposal_version_id == proposal_version_id)
            .with_for_update()
        ).scalar_one_or_none()
        if proposal is None:
            raise CommandRejected("PROPOSAL_NOT_FOUND", "proposal version is unavailable")
        self._validate_issue_scope(envelope, proposal)

        existing = session.execute(
            select(TradingAuthorization).where(
                TradingAuthorization.proposal_version_id == proposal_version_id
            )
        ).scalar_one_or_none()
        if existing is not None:
            if (
                existing.approval_decision_id != request.approval_decision_id
                or existing.proposal_spec_hash != request.proposal_spec_hash
                or existing.risk_summary_hash != request.risk_summary_hash
            ):
                raise CommandRejected(
                    "AUTHORIZATION_BINDING_MISMATCH",
                    "existing authorization has different frozen bindings",
                )
            return self._existing_issue_outcome(session, existing)

        now = self._clock()
        proposal_state = session.execute(
            select(ProposalVersionState)
            .where(ProposalVersionState.proposal_version_id == proposal_version_id)
            .with_for_update()
        ).scalar_one_or_none()
        decision = session.execute(
            select(ApprovalDecision)
            .where(ApprovalDecision.approval_decision_id == request.approval_decision_id)
            .with_for_update()
        ).scalar_one_or_none()
        risk_state = session.execute(
            select(SystemRiskStateRecord)
            .where(SystemRiskStateRecord.organization_id == proposal.organization_id)
            .with_for_update()
        ).scalar_one_or_none()
        votes = tuple(
            session.execute(
                select(ReviewerVote)
                .where(ReviewerVote.proposal_version_id == proposal_version_id)
                .order_by(ReviewerVote.decided_at, ReviewerVote.vote_id)
            ).scalars()
        )
        binding = self._validate_issue_facts(
            proposal,
            proposal_state,
            decision,
            votes,
            risk_state,
            request,
            now,
        )
        if decision is None or risk_state is None:  # pragma: no cover - validated above
            raise RuntimeError("validated issuance facts unexpectedly missing")

        authorization_id = uuid4()
        campaign_id = uuid4()
        initial_id = uuid4()
        authorized_capacity = proposal.one_r_0 * proposal.requested_max_r
        price_reference = proposal.limit_price or proposal.trigger_price
        if price_reference is None or price_reference <= 0:
            raise CommandRejected("PRICE_REFERENCE_MISSING", "a positive frozen price is required")
        slippage_fraction = proposal.max_slippage_bps / Decimal("10000")
        lower_bound = price_reference * (Decimal("1") - slippage_fraction)
        upper_bound = price_reference * (Decimal("1") + slippage_fraction)
        if lower_bound <= 0:
            raise CommandRejected("PRICE_BOUNDARY_INVALID", "frozen slippage boundary is invalid")

        snapshot = {
            "approval_decision": {
                "approval_decision_id": str(decision.approval_decision_id),
                "approved_count": decision.approved_count,
                "required_quorum": decision.required_quorum,
                "version": decision.version,
            },
            "reviewer_votes": [
                {
                    "vote_id": str(vote.vote_id),
                    "reviewer_principal_id": str(vote.reviewer_principal_id),
                    "review_authorization_decision_id": str(vote.authorization_decision_id),
                    "auth_context_ref": vote.auth_context_ref,
                    "policy_version": vote.policy_version,
                }
                for vote in votes
                if vote.choice == "APPROVE"
            ],
            "proposal": {
                "proposal_id": str(proposal.proposal_id),
                "proposal_version_id": str(proposal.proposal_version_id),
                "version": proposal.version,
                "spec_hash": proposal.spec_hash,
                "risk_summary_hash": proposal.risk_summary_hash,
                "risk_decision_ref": proposal.risk_decision_ref,
            },
            "system_risk_state": {
                "status": risk_state.status,
                "version": risk_state.version,
                "policy_version": risk_state.policy_version,
            },
            "binding": binding.model_dump(mode="json"),
            "execution_eligible": False,
        }
        authorization = TradingAuthorization(
            authorization_id=authorization_id,
            proposal_version_id=proposal_version_id,
            approval_decision_id=decision.approval_decision_id,
            organization_id=proposal.organization_id,
            source=proposal.source,
            risk_tier=proposal.risk_tier,
            authorized_loss_capacity=authorized_capacity,
            approved_initial_quantity=proposal.risk_approved_quantity,
            auto_add_enabled=proposal.auto_add_enabled,
            requested_add_count=proposal.requested_add_count,
            total_capital_snapshot_0=proposal.total_capital_snapshot_0,
            one_r_0=proposal.one_r_0,
            frozen_trade_loss_cap=proposal.frozen_trade_loss_cap,
            funding_envelope_0=proposal.funding_envelope_0,
            risk_policy_version=proposal.risk_policy_version,
            authorization_policy_version=binding.authorization_policy_version,
            catalog_version=proposal.catalog_version,
            execution_capability_version=proposal.execution_capability_version,
            capability_certificate_ref=binding.capability_certificate_ref,
            proposal_spec_hash=proposal.spec_hash,
            risk_summary_hash=proposal.risk_summary_hash,
            authorization_mode="SHADOW",
            execution_eligible=False,
            issuance_snapshot=snapshot,
            issuance_snapshot_hash=hash_json(snapshot),
            valid_until=proposal.valid_until,
            issued_at=now,
        )
        session.add(authorization)
        session.flush()
        campaign = Campaign(
            campaign_id=campaign_id,
            authorization_id=authorization_id,
            proposal_id=proposal.proposal_id,
            proposal_version_id=proposal_version_id,
            organization_id=proposal.organization_id,
            strategy_id=binding.strategy_id,
            strategy_version=binding.strategy_version,
            venue=proposal.venue,
            execution_domain=proposal.execution_domain,
            account_id=proposal.account_id,
            instrument_id=proposal.instrument_id,
            direction=proposal.direction,
            one_r_0=proposal.one_r_0,
            funding_envelope_0=proposal.funding_envelope_0,
            created_at=now,
        )
        session.add(campaign)
        session.flush()
        initial = InitialOrderAuthorization(
            initial_authorization_id=initial_id,
            authorization_id=authorization_id,
            campaign_id=campaign_id,
            account_id=proposal.account_id,
            account_abstraction=binding.account_abstraction,
            margin_mode=binding.margin_mode,
            collateral_scope=binding.collateral_scope,
            collateral_pool_id=binding.collateral_pool_id,
            instrument_id=proposal.instrument_id,
            direction=proposal.direction,
            max_quantity=proposal.risk_approved_quantity,
            authorized_loss_capacity=authorized_capacity,
            price_reference=price_reference,
            price_lower_bound=lower_bound,
            price_upper_bound=upper_bound,
            position_management_template_version=binding.position_management_template_version,
            valid_from=now,
            valid_until=proposal.valid_until,
            created_at=now,
        )
        session.add(initial)
        session.flush()
        session.add_all(
            (
                CampaignState(
                    campaign_id=campaign_id,
                    status="PENDING_ENTRY",
                    version=1,
                    reason_code="AUTHORIZATION_ISSUED",
                    updated_at=now,
                ),
                InitialAuthorizationState(
                    initial_authorization_id=initial_id,
                    status="ACTIVE",
                    version=1,
                    reason_code="AUTHORIZATION_ISSUED",
                    updated_at=now,
                ),
            )
        )

        add_package_id: UUID | None = None
        add_status: str | None = None
        add_unit_ids: list[UUID] = []
        if proposal.auto_add_enabled:
            add_package_id, add_status, add_unit_ids = self._create_add_package(
                session,
                proposal,
                binding,
                authorization_id,
                campaign_id,
                risk_state.status,
                now,
            )

        session.flush()
        AUTHORIZATION_ISSUANCE.labels("ISSUED", risk_state.status).inc()
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="TradingAuthorization",
            object_id=str(authorization_id),
            object_version=1,
            data={
                "authorization_id": str(authorization_id),
                "campaign_id": str(campaign_id),
                "campaign_status": "PENDING_ENTRY",
                "initial_authorization_id": str(initial_id),
                "initial_status": "ACTIVE",
                "add_package_id": str(add_package_id) if add_package_id else None,
                "add_package_status": add_status,
                "add_unit_ids": [str(item) for item in add_unit_ids],
                "authorization_mode": "SHADOW",
                "execution_eligible": False,
                "risk_reservation_created": False,
                "order_intent_created": False,
            },
            events=(
                DomainEvent(
                    event_type="TradingAuthorizationIssued",
                    aggregate_type="TradingAuthorization",
                    aggregate_id=str(authorization_id),
                    payload={
                        "proposal_version_id": str(proposal_version_id),
                        "approval_decision_id": str(decision.approval_decision_id),
                        "campaign_id": str(campaign_id),
                        "initial_authorization_id": str(initial_id),
                        "add_package_id": str(add_package_id) if add_package_id else None,
                        "add_package_status": add_status,
                        "system_risk_state": risk_state.status,
                        "execution_eligible": False,
                    },
                ),
            ),
        )

    def tighten(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        self._require_internal(envelope, self.tighten_command_type, "TradingAuthorization")
        if envelope.object_id is None:  # pragma: no cover - enforced above
            raise RuntimeError("missing authorization id")
        try:
            authorization_id = UUID(envelope.object_id)
        except ValueError as exc:
            raise CommandRejected("IDENTIFIER_INVALID", "authorization id must be UUID") from exc
        try:
            request = TightenAuthorizationRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("COMMAND_PAYLOAD_INVALID", str(exc)) from exc
        root = session.execute(
            select(TradingAuthorization)
            .where(TradingAuthorization.authorization_id == authorization_id)
            .with_for_update()
        ).scalar_one_or_none()
        if root is None:
            raise CommandRejected("AUTHORIZATION_NOT_FOUND", "authorization is unavailable")
        if envelope.scope.get("organization_id") != root.organization_id:
            raise CommandRejected("SCOPE_MISMATCH", "organization scope does not match")
        if envelope.expected_version != 1:
            raise CommandRejected("VERSION_CONFLICT", "authorization version binding changed")

        initial = session.execute(
            select(InitialOrderAuthorization)
            .where(InitialOrderAuthorization.authorization_id == authorization_id)
            .with_for_update()
        ).scalar_one()
        initial_state = session.execute(
            select(InitialAuthorizationState)
            .where(
                InitialAuthorizationState.initial_authorization_id
                == initial.initial_authorization_id
            )
            .with_for_update()
        ).scalar_one()
        add_package = session.execute(
            select(AddAuthorizationPackage)
            .where(AddAuthorizationPackage.authorization_id == authorization_id)
            .with_for_update()
        ).scalar_one_or_none()
        add_state = None
        unit_states: tuple[AddUnitState, ...] = ()
        if add_package is not None:
            add_state = session.execute(
                select(AddAuthorizationPackageState)
                .where(AddAuthorizationPackageState.add_package_id == add_package.add_package_id)
                .with_for_update()
            ).scalar_one()
            unit_states = tuple(
                session.execute(
                    select(AddUnitState)
                    .join(AddUnit, AddUnit.add_unit_id == AddUnitState.add_unit_id)
                    .where(AddUnit.add_package_id == add_package.add_package_id)
                    .order_by(AddUnit.ordinal)
                    .with_for_update()
                ).scalars()
            )

        now = self._clock()
        risk_status: str | None = None
        invalidate_initial = False
        add_target: str | None = None
        unit_target: str | None = None
        if request.action is AuthorizationTightenAction.SYNC_RISK_STATE:
            risk_state = session.execute(
                select(SystemRiskStateRecord)
                .where(SystemRiskStateRecord.organization_id == root.organization_id)
                .with_for_update()
            ).scalar_one_or_none()
            risk_status = risk_state.status if risk_state is not None else "UNKNOWN"
            if risk_status == "NO_PYRAMID":
                add_target = "INVALIDATED"
                unit_target = "INVALIDATED"
            elif risk_status in RISK_STATES_INVALIDATING_INITIAL:
                invalidate_initial = True
                add_target = "INVALIDATED"
                unit_target = "INVALIDATED"
        elif request.action is AuthorizationTightenAction.EXPIRE:
            if root.valid_until > now:
                raise CommandRejected(
                    "AUTHORIZATION_NOT_EXPIRED", "authorization validity has not elapsed"
                )
            if initial_state.status == "ACTIVE":
                self._transition(initial_state, "EXPIRED", request.reason_code, now)
            add_target = "EXPIRED"
            unit_target = "EXPIRED"
        elif request.action is AuthorizationTightenAction.REVOKE_ADD:
            add_target = "REVOKED"
            unit_target = "INVALIDATED"
        elif request.action is AuthorizationTightenAction.REVOKE_ALL:
            if initial_state.status == "ACTIVE":
                self._transition(initial_state, "REVOKED", request.reason_code, now)
            add_target = "REVOKED"
            unit_target = "INVALIDATED"
        else:
            invalidate_initial = True
            add_target = "INVALIDATED"
            unit_target = "INVALIDATED"

        if invalidate_initial and initial_state.status == "ACTIVE":
            self._transition(initial_state, "INVALIDATED", request.reason_code, now)
        if (
            add_state is not None
            and add_target is not None
            and add_state.status not in ADD_PACKAGE_TERMINAL
        ):
            self._transition(add_state, add_target, request.reason_code, now)
        for unit_state in unit_states:
            if unit_target is not None and unit_state.status not in ADD_UNIT_TERMINAL:
                self._transition(unit_state, unit_target, request.reason_code, now)

        session.flush()
        AUTHORIZATION_TIGHTENING.labels(request.action.value, risk_status or "NOT_APPLICABLE").inc()
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="TradingAuthorization",
            object_id=str(authorization_id),
            object_version=1,
            data={
                "authorization_id": str(authorization_id),
                "action": request.action.value,
                "system_risk_state": risk_status,
                "initial_status": initial_state.status,
                "add_package_status": add_state.status if add_state else None,
                "add_unit_statuses": [state.status for state in unit_states],
                "execution_eligible": False,
            },
            events=(
                DomainEvent(
                    event_type="TradingAuthorizationTightened",
                    aggregate_type="TradingAuthorization",
                    aggregate_id=str(authorization_id),
                    payload={
                        "action": request.action.value,
                        "reason_code": request.reason_code,
                        "system_risk_state": risk_status,
                        "initial_status": initial_state.status,
                        "add_package_status": add_state.status if add_state else None,
                    },
                ),
            ),
        )

    @staticmethod
    def _require_internal(
        envelope: CommandEnvelope, expected_command: str, expected_object_type: str
    ) -> None:
        if envelope.command_type != expected_command:
            raise CommandRejected("COMMAND_TYPE_MISMATCH", "unexpected command type")
        if envelope.object_type != expected_object_type or envelope.object_id is None:
            raise CommandRejected("OBJECT_BINDING_MISMATCH", "object binding is required")
        if (
            envelope.service_principal != AUTHORIZATION_SERVICE_PRINCIPAL
            or envelope.channel not in {CommandChannel.INTERNAL, CommandChannel.SYSTEM}
        ):
            raise CommandRejected(
                "INTERNAL_SERVICE_REQUIRED", "internal authorization service required"
            )

    @staticmethod
    def _validate_issue_scope(envelope: CommandEnvelope, proposal: FrozenProposalVersion) -> None:
        if envelope.expected_version != proposal.version:
            raise CommandRejected("VERSION_CONFLICT", "proposal version binding changed")
        if envelope.scope.get("organization_id") != proposal.organization_id:
            raise CommandRejected("SCOPE_MISMATCH", "organization scope does not match")

    @staticmethod
    def _validate_issue_facts(
        proposal: FrozenProposalVersion,
        proposal_state: ProposalVersionState | None,
        decision: ApprovalDecision | None,
        votes: tuple[ReviewerVote, ...],
        risk_state: SystemRiskStateRecord | None,
        request: IssueAuthorizationRequest,
        now: datetime,
    ) -> FrozenAuthorizationBinding:
        if proposal.proposal_purpose != "INITIAL_ENTRY" or proposal.reduce_only:
            raise CommandRejected("PROPOSAL_PURPOSE_MISMATCH", "initial entry proposal required")
        if proposal_state is None or proposal_state.status != "FROZEN":
            raise CommandRejected("PROPOSAL_NOT_FROZEN", "proposal must remain frozen")
        if (
            request.proposal_spec_hash != proposal.spec_hash
            or request.risk_summary_hash != proposal.risk_summary_hash
            or hash_json(proposal.spec) != proposal.spec_hash
            or hash_json(proposal.risk_summary) != proposal.risk_summary_hash
        ):
            raise CommandRejected("PROPOSAL_INTEGRITY_FAILED", "frozen proposal hashes changed")
        if proposal.valid_until <= now:
            raise CommandRejected("PROPOSAL_EXPIRED", "proposal validity elapsed")
        if decision is None or decision.proposal_version_id != proposal.proposal_version_id:
            raise CommandRejected("APPROVAL_BINDING_MISMATCH", "approval does not bind proposal")
        if decision.status != "APPROVED":
            raise CommandRejected("APPROVAL_NOT_APPROVED", "approval is not approved")
        if decision.valid_until <= now:
            raise CommandRejected("APPROVAL_EXPIRED", "approval validity elapsed")
        approved_votes = tuple(vote for vote in votes if vote.choice == "APPROVE")
        if (
            decision.approved_count != decision.required_quorum
            or len(approved_votes) != decision.approved_count
            or any(vote.risk_summary_hash != proposal.risk_summary_hash for vote in approved_votes)
        ):
            raise CommandRejected(
                "APPROVAL_EVIDENCE_INCOMPLETE", "approval quorum evidence is incomplete"
            )
        if risk_state is None:
            raise CommandRejected("SYSTEM_RISK_STATE_UNKNOWN", "system risk state is missing")
        if risk_state.status not in {"NORMAL", "NO_PYRAMID"}:
            raise CommandRejected("SYSTEM_RISK_STATE_DENY", "system risk state forbids issuance")
        if proposal.auto_add_enabled and proposal.requested_add_count == 0:
            raise CommandRejected(
                "ADD_AUTHORIZATION_INVALID", "enabled auto-add requires at least one unit"
            )
        try:
            binding = FrozenAuthorizationBinding.model_validate(proposal.spec)
        except ValueError as exc:
            raise CommandRejected("AUTHORIZATION_BINDING_INVALID", str(exc)) from exc
        if proposal.source == "SYSTEM" and (
            binding.strategy_id != proposal.strategy_id
            or binding.strategy_version != proposal.strategy_version
        ):
            raise CommandRejected("STRATEGY_BINDING_MISMATCH", "system strategy binding changed")
        return binding

    @staticmethod
    def _create_add_package(
        session: Session,
        proposal: FrozenProposalVersion,
        binding: FrozenAuthorizationBinding,
        authorization_id: UUID,
        campaign_id: UUID,
        risk_status: str,
        now: datetime,
    ) -> tuple[UUID, str, list[UUID]]:
        add_package_id = uuid4()
        add_status = "DORMANT" if risk_status == "NORMAL" else "INVALIDATED"
        unit_status = "AVAILABLE" if risk_status == "NORMAL" else "INVALIDATED"
        package = AddAuthorizationPackage(
            add_package_id=add_package_id,
            authorization_id=authorization_id,
            campaign_id=campaign_id,
            direction=proposal.direction,
            authorized_add_count=proposal.requested_add_count,
            target_leverage_min=proposal.target_leverage_min,
            target_leverage_max=proposal.target_leverage_max,
            add_milestone_policy_version=binding.add_milestone_policy_version,
            valid_from=now,
            valid_until=proposal.valid_until,
            created_at=now,
        )
        session.add(package)
        session.flush()
        session.add(
            AddAuthorizationPackageState(
                add_package_id=add_package_id,
                status=add_status,
                version=1,
                reason_code=(
                    "AUTHORIZATION_ISSUED" if risk_status == "NORMAL" else "SYSTEM_NO_PYRAMID"
                ),
                updated_at=now,
            )
        )
        unit_ids: list[UUID] = []
        unit_states: list[AddUnitState] = []
        for ordinal in range(1, proposal.requested_add_count + 1):
            unit_id = uuid4()
            unit_ids.append(unit_id)
            session.add(
                AddUnit(
                    add_unit_id=unit_id,
                    add_package_id=add_package_id,
                    ordinal=ordinal,
                    unlock_milestone_pct=ADD_MILESTONES[ordinal - 1],
                    created_at=now,
                )
            )
            unit_states.append(
                AddUnitState(
                    add_unit_id=unit_id,
                    status=unit_status,
                    version=1,
                    reason_code=(
                        "AUTHORIZATION_ISSUED" if risk_status == "NORMAL" else "SYSTEM_NO_PYRAMID"
                    ),
                    updated_at=now,
                )
            )
        session.flush()
        session.add_all(unit_states)
        return add_package_id, add_status, unit_ids

    @staticmethod
    def _existing_issue_outcome(session: Session, existing: TradingAuthorization) -> CommandOutcome:
        campaign = session.execute(
            select(Campaign).where(Campaign.authorization_id == existing.authorization_id)
        ).scalar_one()
        initial = session.execute(
            select(InitialOrderAuthorization).where(
                InitialOrderAuthorization.authorization_id == existing.authorization_id
            )
        ).scalar_one()
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="TradingAuthorization",
            object_id=str(existing.authorization_id),
            object_version=1,
            data={
                "authorization_id": str(existing.authorization_id),
                "campaign_id": str(campaign.campaign_id),
                "initial_authorization_id": str(initial.initial_authorization_id),
                "already_issued": True,
                "execution_eligible": False,
                "risk_reservation_created": False,
                "order_intent_created": False,
            },
            events=(
                DomainEvent(
                    event_type="TradingAuthorizationAlreadyIssued",
                    aggregate_type="TradingAuthorization",
                    aggregate_id=str(existing.authorization_id),
                    payload={"proposal_version_id": str(existing.proposal_version_id)},
                ),
            ),
        )

    @staticmethod
    def _transition(
        state: InitialAuthorizationState | AddAuthorizationPackageState | AddUnitState,
        status: str,
        reason_code: str,
        now: datetime,
    ) -> None:
        state.status = status
        state.version += 1
        state.reason_code = reason_code
        state.updated_at = now
