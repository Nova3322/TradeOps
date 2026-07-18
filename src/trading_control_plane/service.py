from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, NoReturn
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from trading_control_plane.database import Database
from trading_control_plane.domain import (
    CampaignStatus,
    CapabilityStatus,
    Direction,
    DomainRejected,
    EconomicFill,
    FactStatus,
    IdempotencyConflict,
    IntentCreation,
    IntentKind,
    OrderIntentStatus,
    PnlBreakdown,
    ProposalSource,
    ProposalStatus,
    ProtectionStatus,
    ReconciliationStatus,
    ReservationStatus,
    ReviewDecision,
    RiskEvaluationInput,
    RiskPolicyInput,
    RiskResult,
    RiskTier,
    Role,
    SystemRiskState,
    TargetCandidate,
    TargetDecision,
    VenueOrderStatus,
    compute_pnl,
    evaluate_risk,
    select_target_position,
)
from trading_control_plane.metrics import (
    FENCING_REJECTIONS,
    INTENT_TRANSITIONS,
    PROTECTION_ISSUES,
    RECONCILIATION_RESULTS,
    RISK_RESULTS,
)
from trading_control_plane.models import (
    AccountEquity,
    Approval,
    AuditEvent,
    Campaign,
    CapabilityGate,
    CommandReceipt,
    FundingPayment,
    Instrument,
    OrderIntent,
    Position,
    Proposal,
    ProtectionOrder,
    ReconciliationRun,
    RiskDecision,
    RiskPolicy,
    RiskReservation,
    RoleAssignment,
    SenderLease,
    TradingAuthorization,
    User,
    VenueFill,
    VenueOrder,
)

ACTIVE_INTENT_STATUSES = {
    OrderIntentStatus.PENDING.value,
    OrderIntentStatus.RESERVED.value,
    OrderIntentStatus.READY.value,
    OrderIntentStatus.SENT.value,
    OrderIntentStatus.PARTIALLY_FILLED.value,
    OrderIntentStatus.UNKNOWN.value,
}

ROLE_ACTIONS: dict[Role, frozenset[str]] = {
    Role.OBSERVER: frozenset({"view"}),
    Role.PROPOSER: frozenset({"view", "proposal.create", "proposal.submit"}),
    Role.REVIEWER: frozenset({"view", "proposal.review"}),
    Role.OPERATOR: frozenset(
        {
            "view",
            "risk.decide",
            "authorization.issue",
            "order.prepare",
            "venue.record",
            "reconcile",
            "sender.manage",
        }
    ),
    Role.SYSTEM_ADMIN: frozenset({"*"}),
}


def _reject(code: str, detail: str) -> NoReturn:
    raise DomainRejected(code, detail)


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _semantic_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _advisory_lock_key(caller_id: str, operation: str, key: str) -> int:
    digest = hashlib.sha256(f"{caller_id}:{operation}:{key}".encode()).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


def _as_uuid(value: str) -> UUID:
    return UUID(value)


def _scope_parts(execution_scope: str) -> tuple[str | None, str | None]:
    account_id, separator, venue = execution_scope.partition(":")
    if not separator:
        return None, None
    return account_id, venue


class TradingService:
    """Single transactional entry point for the pre-production SHADOW trading core."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def _audit(
        self,
        session: Session,
        *,
        actor_id: str,
        event_type: str,
        object_type: str,
        object_id: UUID | str,
        reason: str,
        correlation_id: UUID,
        object_version: int,
        idempotency_key: str | None = None,
        now: datetime,
    ) -> None:
        session.add(
            AuditEvent(
                actor_id=actor_id,
                event_type=event_type,
                object_type=object_type,
                object_id=str(object_id),
                reason=reason,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                object_version=object_version,
                created_at=now,
            )
        )

    def _idempotency(
        self,
        session: Session,
        *,
        caller_id: str,
        operation: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any] | None]:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key(caller_id, operation, idempotency_key)},
        )
        digest = _semantic_hash(payload)
        receipt = session.scalar(
            select(CommandReceipt).where(
                CommandReceipt.caller_id == caller_id,
                CommandReceipt.operation == operation,
                CommandReceipt.idempotency_key == idempotency_key,
            )
        )
        if receipt is None:
            return digest, None
        if receipt.semantic_hash != digest:
            raise IdempotencyConflict
        return digest, receipt.response

    @staticmethod
    def _save_receipt(
        session: Session,
        *,
        caller_id: str,
        operation: str,
        idempotency_key: str,
        semantic_hash: str,
        response: dict[str, Any],
        now: datetime,
    ) -> None:
        session.add(
            CommandReceipt(
                caller_id=caller_id,
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=semantic_hash,
                response=response,
                created_at=now,
            )
        )

    def _require_role(
        self,
        session: Session,
        user_id: UUID,
        action: str,
        account_id: str | None = None,
        venue: str | None = None,
    ) -> None:
        user = session.get(User, user_id)
        if user is None or not user.active:
            _reject("USER_NOT_AUTHORIZED", "user is missing or inactive")
        assignments = session.scalars(
            select(RoleAssignment).where(RoleAssignment.user_id == user_id)
        ).all()
        for assignment in assignments:
            role = Role(assignment.role)
            if action not in ROLE_ACTIONS[role] and "*" not in ROLE_ACTIONS[role]:
                continue
            if assignment.account_scope is not None and assignment.account_scope != account_id:
                continue
            if assignment.venue_scope is not None and assignment.venue_scope != venue:
                continue
            return
        _reject("RBAC_DENIED", f"{action} is not allowed in the requested scope")

    def can_user(
        self,
        user_id: UUID,
        action: str,
        account_id: str | None = None,
        venue: str | None = None,
    ) -> bool:
        with self.database.session_factory() as session:
            try:
                self._require_role(session, user_id, action, account_id, venue)
            except DomainRejected:
                return False
            return True

    def bootstrap_admin(self, username: str, *, now: datetime) -> UUID:
        with self.database.session_factory.begin() as session:
            if session.scalar(select(func.count()).select_from(User)) != 0:
                _reject("BOOTSTRAP_CLOSED", "an administrator already exists")
            user = User(username=username, active=True, created_at=now)
            session.add(user)
            session.flush()
            session.add(
                RoleAssignment(
                    user_id=user.user_id,
                    role=Role.SYSTEM_ADMIN.value,
                    account_scope=None,
                    venue_scope=None,
                    created_at=now,
                )
            )
            self._audit(
                session,
                actor_id="bootstrap",
                event_type="USER_BOOTSTRAPPED",
                object_type="User",
                object_id=user.user_id,
                reason="first internal administrator",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return user.user_id

    def create_user(self, username: str, actor_id: UUID, *, now: datetime) -> UUID:
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "user.manage")
            user = User(username=username, active=True, created_at=now)
            session.add(user)
            session.flush()
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="USER_CREATED",
                object_type="User",
                object_id=user.user_id,
                reason="internal user created",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return user.user_id

    def assign_role(
        self,
        user_id: UUID,
        role: Role,
        actor_id: UUID,
        account_scope: str | None = None,
        venue_scope: str | None = None,
        *,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "role.manage")
            if session.get(User, user_id) is None:
                _reject("USER_NOT_FOUND", "role target does not exist")
            assignment = RoleAssignment(
                user_id=user_id,
                role=role.value,
                account_scope=account_scope,
                venue_scope=venue_scope,
                created_at=now,
            )
            session.add(assignment)
            session.flush()
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="ROLE_ASSIGNED",
                object_type="RoleAssignment",
                object_id=assignment.assignment_id,
                reason=f"{role.value} assigned",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return assignment.assignment_id

    def register_instrument(
        self,
        *,
        actor_id: UUID,
        venue: str,
        symbol: str,
        tick_size: Decimal,
        lot_size: Decimal,
        minimum_notional: Decimal,
        contract_multiplier: Decimal,
        quote_currency: str,
        collateral_currency: str,
        protection_supported: bool,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "instrument.manage")
            instrument = Instrument(
                venue=venue,
                symbol=symbol,
                tick_size=tick_size,
                lot_size=lot_size,
                minimum_notional=minimum_notional,
                contract_multiplier=contract_multiplier,
                quote_currency=quote_currency,
                collateral_currency=collateral_currency,
                active=True,
                protection_supported=protection_supported,
                updated_at=now,
            )
            session.add(instrument)
            session.flush()
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="INSTRUMENT_REGISTERED",
                object_type="Instrument",
                object_id=instrument.instrument_id,
                reason=f"{venue}:{symbol}",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return instrument.instrument_id

    def set_risk_policy(
        self,
        *,
        actor_id: UUID,
        version: str,
        system_state: SystemRiskState,
        max_total_risk: Decimal,
        max_fact_age: timedelta,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "risk_policy.manage")
            for current in session.scalars(select(RiskPolicy).where(RiskPolicy.active)).all():
                current.active = False
            policy = RiskPolicy(
                version=version,
                system_state=system_state.value,
                max_total_risk=max_total_risk,
                max_fact_age_seconds=int(max_fact_age.total_seconds()),
                active=True,
                updated_by=str(actor_id),
                updated_at=now,
            )
            session.add(policy)
            session.flush()
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="RISK_POLICY_SET",
                object_type="RiskPolicy",
                object_id=policy.policy_id,
                reason=system_state.value,
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return policy.policy_id

    def create_proposal(
        self,
        *,
        actor_id: UUID,
        source: ProposalSource,
        risk_tier: RiskTier,
        account_id: str,
        venue: str,
        instrument_id: UUID,
        direction: Direction,
        quantity: Decimal,
        max_risk: Decimal,
        expires_at: datetime,
        idempotency_key: str,
        now: datetime,
    ) -> UUID:
        payload = {
            "source": source.value,
            "risk_tier": risk_tier.value,
            "account_id": account_id,
            "venue": venue,
            "instrument_id": str(instrument_id),
            "direction": direction.value,
            "quantity": str(quantity),
            "max_risk": str(max_risk),
            "expires_at": expires_at.isoformat(),
        }
        operation = "proposal.create"
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, operation, account_id, venue)
            digest, response = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return _as_uuid(str(response["proposal_id"]))
            instrument = session.get(Instrument, instrument_id)
            if instrument is None or not instrument.active or instrument.venue != venue:
                _reject("INSTRUMENT_UNAVAILABLE", "instrument is inactive or outside venue scope")
            if expires_at <= now:
                _reject("PROPOSAL_EXPIRY_INVALID", "proposal expiry must be in the future")
            correlation_id = uuid4()
            proposal = Proposal(
                source=source.value,
                proposer_id=actor_id,
                status=ProposalStatus.DRAFT.value,
                version=1,
                risk_tier=risk_tier.value,
                account_id=account_id,
                venue=venue,
                instrument_id=instrument_id,
                direction=direction.value,
                quantity=quantity,
                max_risk=max_risk,
                frozen_payload=payload,
                semantic_hash=digest,
                frozen_at=None,
                expires_at=expires_at,
                correlation_id=correlation_id,
                created_at=now,
                updated_at=now,
            )
            session.add(proposal)
            session.flush()
            result = {"proposal_id": str(proposal.proposal_id)}
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="PROPOSAL_CREATED",
                object_type="Proposal",
                object_id=proposal.proposal_id,
                reason=source.value,
                correlation_id=correlation_id,
                object_version=proposal.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return proposal.proposal_id

    def submit_proposal(self, proposal_id: UUID, actor_id: UUID, *, now: datetime) -> None:
        expired = False
        with self.database.session_factory.begin() as session:
            proposal = session.get(Proposal, proposal_id, with_for_update=True)
            if proposal is None:
                _reject("PROPOSAL_NOT_FOUND", "proposal does not exist")
            self._require_role(
                session, actor_id, "proposal.submit", proposal.account_id, proposal.venue
            )
            if proposal.proposer_id != actor_id:
                _reject("PROPOSAL_OWNER_REQUIRED", "only the proposer may submit the draft")
            if proposal.expires_at <= now:
                proposal.status = ProposalStatus.EXPIRED.value
                proposal.updated_at = now
                proposal.version += 1
                self._audit(
                    session,
                    actor_id=str(actor_id),
                    event_type="PROPOSAL_EXPIRED",
                    object_type="Proposal",
                    object_id=proposal.proposal_id,
                    reason="expired before submission",
                    correlation_id=proposal.correlation_id,
                    object_version=proposal.version,
                    now=now,
                )
                expired = True
            elif proposal.status != ProposalStatus.DRAFT.value:
                _reject("PROPOSAL_NOT_DRAFT", "only a draft can be submitted")
            else:
                proposal.status = ProposalStatus.PENDING_REVIEW.value
                proposal.frozen_at = now
                proposal.updated_at = now
                proposal.version += 1
                self._audit(
                    session,
                    actor_id=str(actor_id),
                    event_type="PROPOSAL_SUBMITTED",
                    object_type="Proposal",
                    object_id=proposal.proposal_id,
                    reason="frozen for review",
                    correlation_id=proposal.correlation_id,
                    object_version=proposal.version,
                    now=now,
                )
        if expired:
            _reject("PROPOSAL_EXPIRED", "proposal expired before submission")

    def review_proposal(
        self,
        proposal_id: UUID,
        reviewer_id: UUID,
        decision: ReviewDecision,
        reason: str,
        *,
        now: datetime,
    ) -> ProposalStatus:
        expired = False
        result: ProposalStatus | None = None
        with self.database.session_factory.begin() as session:
            proposal = session.get(Proposal, proposal_id, with_for_update=True)
            if proposal is None:
                _reject("PROPOSAL_NOT_FOUND", "proposal does not exist")
            if proposal.proposer_id == reviewer_id:
                _reject("SELF_REVIEW_FORBIDDEN", "a proposer cannot review the same proposal")
            self._require_role(
                session, reviewer_id, "proposal.review", proposal.account_id, proposal.venue
            )
            if proposal.expires_at <= now:
                proposal.status = ProposalStatus.EXPIRED.value
                proposal.updated_at = now
                proposal.version += 1
                self._audit(
                    session,
                    actor_id=str(reviewer_id),
                    event_type="PROPOSAL_EXPIRED",
                    object_type="Proposal",
                    object_id=proposal.proposal_id,
                    reason="expired before review",
                    correlation_id=proposal.correlation_id,
                    object_version=proposal.version,
                    now=now,
                )
                expired = True
            else:
                if proposal.status != ProposalStatus.PENDING_REVIEW.value:
                    _reject("PROPOSAL_NOT_REVIEWABLE", "proposal is not pending review")
                duplicate = session.scalar(
                    select(Approval).where(
                        Approval.proposal_id == proposal_id,
                        Approval.reviewer_id == reviewer_id,
                    )
                )
                if duplicate is not None:
                    _reject("REVIEW_ALREADY_RECORDED", "reviewer already voted")
                session.add(
                    Approval(
                        proposal_id=proposal_id,
                        reviewer_id=reviewer_id,
                        decision=decision.value,
                        reason=reason,
                        created_at=now,
                    )
                )
                session.flush()
                if decision is ReviewDecision.REJECT:
                    proposal.status = ProposalStatus.REJECTED.value
                else:
                    approvals = session.execute(
                        select(func.count())
                        .select_from(Approval)
                        .where(
                            Approval.proposal_id == proposal_id,
                            Approval.decision == ReviewDecision.APPROVE.value,
                        )
                    ).scalar_one()
                    required = 2 if proposal.risk_tier == RiskTier.HIGH.value else 1
                    if approvals >= required:
                        proposal.status = ProposalStatus.APPROVED.value
                proposal.updated_at = now
                proposal.version += 1
                self._audit(
                    session,
                    actor_id=str(reviewer_id),
                    event_type="PROPOSAL_REVIEWED",
                    object_type="Proposal",
                    object_id=proposal_id,
                    reason=f"{decision.value}: {reason}",
                    correlation_id=proposal.correlation_id,
                    object_version=proposal.version,
                    now=now,
                )
                result = ProposalStatus(proposal.status)
        if expired:
            _reject("PROPOSAL_EXPIRED", "proposal expired before review")
        if result is None:
            raise RuntimeError("proposal review completed without a result")
        return result

    def decide_risk(
        self,
        *,
        proposal_id: UUID,
        actor_id: UUID,
        inputs: RiskEvaluationInput,
        idempotency_key: str,
        now: datetime,
    ) -> UUID:
        payload = {
            "proposal_id": str(proposal_id),
            "kind": inputs.kind.value,
            "requested_quantity": str(inputs.requested_quantity),
            "requested_risk": str(inputs.requested_risk),
            "current_risk": str(inputs.current_risk),
            "fact_age_seconds": str(inputs.fact_age.total_seconds()),
            "position_known": inputs.position_known,
            "protection_known": inputs.protection_known,
        }
        operation = "risk.decide"
        with self.database.session_factory.begin() as session:
            proposal = session.get(Proposal, proposal_id)
            if proposal is None:
                _reject("PROPOSAL_NOT_FOUND", "proposal does not exist")
            self._require_role(session, actor_id, operation, proposal.account_id, proposal.venue)
            digest, response = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return _as_uuid(str(response["decision_id"]))
            if proposal.status != ProposalStatus.APPROVED.value:
                _reject("PROPOSAL_NOT_APPROVED", "risk decision requires approved proposal")
            policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
            if policy is None:
                _reject("RISK_POLICY_MISSING", "no active risk policy exists")
            outcome = evaluate_risk(
                RiskPolicyInput(
                    version=policy.version,
                    system_state=SystemRiskState(policy.system_state),
                    max_total_risk=policy.max_total_risk,
                    max_fact_age=timedelta(seconds=policy.max_fact_age_seconds),
                ),
                inputs,
            )
            decision = RiskDecision(
                proposal_id=proposal_id,
                policy_id=policy.policy_id,
                input_data=payload,
                result=outcome.result.value,
                approved_quantity=outcome.allowed_quantity,
                risk_amount=outcome.allowed_risk,
                reasons=list(outcome.reasons),
                data_as_of=now - inputs.fact_age,
                actor_id=str(actor_id),
                correlation_id=proposal.correlation_id,
                created_at=now,
            )
            session.add(decision)
            session.flush()
            result = {"decision_id": str(decision.decision_id)}
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="RISK_DECIDED",
                object_type="RiskDecision",
                object_id=decision.decision_id,
                reason=outcome.result.value,
                correlation_id=proposal.correlation_id,
                object_version=1,
                idempotency_key=idempotency_key,
                now=now,
            )
            RISK_RESULTS.labels(outcome.result.value).inc()
            return decision.decision_id

    def issue_authorization(
        self,
        *,
        proposal_id: UUID,
        actor_id: UUID,
        expires_at: datetime,
        allowed_adds: int,
        idempotency_key: str,
        now: datetime,
    ) -> UUID:
        payload = {
            "proposal_id": str(proposal_id),
            "expires_at": expires_at.isoformat(),
            "allowed_adds": allowed_adds,
        }
        operation = "authorization.issue"
        with self.database.session_factory.begin() as session:
            proposal = session.get(Proposal, proposal_id)
            if proposal is None:
                _reject("PROPOSAL_NOT_FOUND", "proposal does not exist")
            self._require_role(session, actor_id, operation, proposal.account_id, proposal.venue)
            digest, response = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return _as_uuid(str(response["authorization_id"]))
            if proposal.status != ProposalStatus.APPROVED.value:
                _reject("PROPOSAL_NOT_APPROVED", "authorization requires approved proposal")
            decision = session.scalar(
                select(RiskDecision)
                .where(RiskDecision.proposal_id == proposal_id)
                .order_by(RiskDecision.created_at.desc())
                .limit(1)
            )
            if decision is None or decision.result == RiskResult.DENY.value:
                _reject("RISK_DECISION_NOT_ALLOWING", "latest risk decision does not allow risk")
            if expires_at <= now or expires_at > proposal.expires_at:
                _reject("AUTHORIZATION_EXPIRY_INVALID", "authorization must be short-lived")
            authorization = TradingAuthorization(
                proposal_id=proposal_id,
                risk_decision_id=decision.decision_id,
                account_id=proposal.account_id,
                venue=proposal.venue,
                instrument_id=proposal.instrument_id,
                direction=proposal.direction,
                quantity_limit=decision.approved_quantity,
                used_quantity=Decimal(0),
                risk_limit=decision.risk_amount,
                expires_at=expires_at,
                allowed_adds=allowed_adds,
                used_adds=0,
                active=True,
                actor_id=str(actor_id),
                created_at=now,
            )
            session.add(authorization)
            session.flush()
            result = {"authorization_id": str(authorization.authorization_id)}
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="AUTHORIZATION_ISSUED",
                object_type="TradingAuthorization",
                object_id=authorization.authorization_id,
                reason="approved proposal and risk decision",
                correlation_id=proposal.correlation_id,
                object_version=1,
                idempotency_key=idempotency_key,
                now=now,
            )
            return authorization.authorization_id

    @staticmethod
    def _intent_creation(response: dict[str, Any]) -> IntentCreation:
        return IntentCreation(
            campaign_id=_as_uuid(str(response["campaign_id"])),
            reservation_id=_as_uuid(str(response["reservation_id"])),
            intent_id=_as_uuid(str(response["intent_id"])),
        )

    def create_order_intent(
        self,
        authorization_id: UUID,
        actor_id: UUID,
        kind: IntentKind,
        account_id: str,
        venue: str,
        instrument_id: UUID,
        direction: Direction,
        quantity: Decimal,
        idempotency_key: str,
        *,
        now: datetime,
    ) -> IntentCreation:
        if kind not in {IntentKind.INITIAL, IntentKind.ADD}:
            _reject("NEW_RISK_INTENT_REQUIRED", "this entry point only creates INITIAL or ADD")
        payload = {
            "authorization_id": str(authorization_id),
            "kind": kind.value,
            "account_id": account_id,
            "venue": venue,
            "instrument_id": str(instrument_id),
            "direction": direction.value,
            "quantity": str(quantity),
        }
        operation = "order.prepare"
        with self.database.session_factory.begin() as session:
            digest, response = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return self._intent_creation(response)
            authorization = session.get(
                TradingAuthorization, authorization_id, with_for_update=True
            )
            if authorization is None or not authorization.active:
                _reject("AUTHORIZATION_INACTIVE", "authorization is missing or inactive")
            if authorization.expires_at <= now:
                _reject("AUTHORIZATION_EXPIRED", "authorization expired")
            if (
                authorization.account_id != account_id
                or authorization.venue != venue
                or authorization.instrument_id != instrument_id
                or authorization.direction != direction.value
            ):
                _reject("AUTHORIZATION_SCOPE_MISMATCH", "request exceeds frozen scope")
            self._require_role(session, actor_id, operation, account_id, venue)
            if (
                quantity <= 0
                or authorization.used_quantity + quantity > authorization.quantity_limit
            ):
                _reject("AUTHORIZATION_QUANTITY_EXCEEDED", "request exceeds quantity limit")
            if kind is IntentKind.ADD:
                gate = session.get(CapabilityGate, "AUTO_ADD")
                if gate is None or gate.status != CapabilityStatus.ENABLED.value:
                    _reject("AUTO_ADD_DISABLED", "automatic add capability is disabled")
                if authorization.used_adds >= authorization.allowed_adds:
                    _reject("ADD_LIMIT_EXHAUSTED", "authorization add count is exhausted")

            campaign = session.scalar(
                select(Campaign).where(Campaign.authorization_id == authorization_id)
            )
            if campaign is None:
                proposal = session.get(Proposal, authorization.proposal_id)
                if proposal is None:
                    _reject("PROPOSAL_NOT_FOUND", "authorization proposal is missing")
                campaign = Campaign(
                    proposal_id=authorization.proposal_id,
                    authorization_id=authorization_id,
                    account_id=authorization.account_id,
                    venue=authorization.venue,
                    instrument_id=authorization.instrument_id,
                    direction=authorization.direction,
                    status=CampaignStatus.OPENING.value,
                    current_target_quantity=authorization.quantity_limit,
                    target_version=0,
                    target_reason=None,
                    target_urgency=None,
                    target_calculated_at=None,
                    realized_pnl=Decimal(0),
                    unrealized_pnl=Decimal(0),
                    final_pnl=Decimal(0),
                    created_at=now,
                    updated_at=now,
                )
                session.add(campaign)
                session.flush()
            active = session.scalar(
                select(OrderIntent.intent_id).where(
                    OrderIntent.campaign_id == campaign.campaign_id,
                    OrderIntent.status.in_(ACTIVE_INTENT_STATUSES),
                )
            )
            if active is not None:
                _reject("ACTIVE_ORDER_INTENT", "campaign already has unresolved intent")
            risk_amount = authorization.risk_limit * quantity / authorization.quantity_limit
            reservation = RiskReservation(
                campaign_id=campaign.campaign_id,
                authorization_id=authorization_id,
                status=ReservationStatus.RESERVED.value,
                amount=risk_amount,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(reservation)
            session.flush()
            side = "BUY" if direction is Direction.LONG else "SELL"
            intent = OrderIntent(
                campaign_id=campaign.campaign_id,
                authorization_id=authorization_id,
                reservation_id=reservation.reservation_id,
                kind=kind.value,
                side=side,
                quantity=quantity,
                reduce_only=False,
                status=OrderIntentStatus.READY.value,
                semantic_hash=digest,
                actor_id=str(actor_id),
                correlation_id=uuid4(),
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(intent)
            authorization.used_quantity += quantity
            if kind is IntentKind.ADD:
                authorization.used_adds += 1
            session.flush()
            result = {
                "campaign_id": str(campaign.campaign_id),
                "reservation_id": str(reservation.reservation_id),
                "intent_id": str(intent.intent_id),
            }
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="ORDER_INTENT_PREPARED",
                object_type="OrderIntent",
                object_id=intent.intent_id,
                reason=kind.value,
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            INTENT_TRANSITIONS.labels("CREATED", intent.status).inc()
            return IntentCreation(
                campaign_id=campaign.campaign_id,
                reservation_id=reservation.reservation_id,
                intent_id=intent.intent_id,
            )

    def mark_intent_unknown(
        self, intent_id: UUID, actor_id: UUID, reason: str, *, now: datetime
    ) -> None:
        with self.database.session_factory.begin() as session:
            intent = session.get(OrderIntent, intent_id, with_for_update=True)
            if intent is None:
                _reject("ORDER_INTENT_NOT_FOUND", "intent does not exist")
            campaign = session.get(Campaign, intent.campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "intent campaign does not exist")
            self._require_role(
                session, actor_id, "order.prepare", campaign.account_id, campaign.venue
            )
            campaign = session.get(Campaign, intent.campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "intent campaign does not exist")
            self._require_role(
                session, actor_id, "order.prepare", campaign.account_id, campaign.venue
            )
            previous = intent.status
            intent.status = OrderIntentStatus.UNKNOWN.value
            intent.updated_at = now
            intent.version += 1
            if intent.reservation_id is not None:
                reservation = session.get(RiskReservation, intent.reservation_id)
                if reservation is not None:
                    reservation.status = ReservationStatus.UNKNOWN.value
                    reservation.updated_at = now
                    reservation.version += 1
            campaign.status = CampaignStatus.UNKNOWN.value
            campaign.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="ORDER_INTENT_UNKNOWN",
                object_type="OrderIntent",
                object_id=intent.intent_id,
                reason=reason,
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                now=now,
            )
            INTENT_TRANSITIONS.labels(previous, intent.status).inc()

    def release_unfilled_intent(
        self,
        intent_id: UUID,
        actor_id: UUID,
        terminal_status: OrderIntentStatus,
        reason: str,
        *,
        now: datetime,
    ) -> None:
        if terminal_status not in {OrderIntentStatus.CANCELLED, OrderIntentStatus.REJECTED}:
            _reject("INVALID_TERMINAL_STATUS", "only cancelled or rejected may release risk")
        with self.database.session_factory.begin() as session:
            intent = session.get(OrderIntent, intent_id, with_for_update=True)
            if intent is None:
                _reject("ORDER_INTENT_NOT_FOUND", "intent does not exist")
            filled = session.scalar(
                select(func.coalesce(func.sum(VenueFill.quantity), 0)).where(
                    VenueFill.order_intent_id == intent_id
                )
            )
            if filled != 0:
                _reject("FILLED_INTENT_RISK_REQUIRED", "filled intent cannot release all risk")
            previous = intent.status
            intent.status = terminal_status.value
            intent.updated_at = now
            intent.version += 1
            if intent.reservation_id is not None:
                reservation = session.get(RiskReservation, intent.reservation_id)
                if reservation is not None:
                    reservation.status = ReservationStatus.RELEASED.value
                    reservation.updated_at = now
                    reservation.version += 1
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="ORDER_INTENT_TERMINATED",
                object_type="OrderIntent",
                object_id=intent.intent_id,
                reason=reason,
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                now=now,
            )
            INTENT_TRANSITIONS.labels(previous, intent.status).inc()

    def acquire_sender(
        self,
        execution_scope: str,
        owner_id: str,
        actor_id: UUID,
        now: datetime,
        lease_duration: timedelta = timedelta(minutes=1),
    ) -> int:
        with self.database.session_factory.begin() as session:
            account_id, venue = _scope_parts(execution_scope)
            self._require_role(session, actor_id, "sender.manage", account_id, venue)
            lease = session.get(SenderLease, execution_scope, with_for_update=True)
            if lease is None:
                token = 1
                session.add(
                    SenderLease(
                        execution_scope=execution_scope,
                        owner_id=owner_id,
                        fencing_token=token,
                        expires_at=now + lease_duration,
                        updated_at=now,
                    )
                )
            elif lease.owner_id == owner_id and lease.expires_at > now:
                token = lease.fencing_token
                lease.expires_at = now + lease_duration
                lease.updated_at = now
            else:
                latest = session.scalar(
                    select(ReconciliationRun)
                    .where(ReconciliationRun.execution_scope == execution_scope)
                    .order_by(ReconciliationRun.completed_at.desc())
                    .limit(1)
                )
                if latest is None or latest.status not in {
                    ReconciliationStatus.MATCH.value,
                    ReconciliationStatus.RESOLVED.value,
                }:
                    _reject(
                        "RECONCILIATION_REQUIRED",
                        "sender takeover requires current reconciliation",
                    )
                token = lease.fencing_token + 1
                lease.owner_id = owner_id
                lease.fencing_token = token
                lease.expires_at = now + lease_duration
                lease.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="SENDER_LEASE_ACQUIRED",
                object_type="SenderLease",
                object_id=execution_scope,
                reason=owner_id,
                correlation_id=uuid4(),
                object_version=token,
                now=now,
            )
            return token

    def _validate_sender(
        self,
        session: Session,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        now: datetime,
    ) -> None:
        lease = session.get(SenderLease, execution_scope)
        if (
            lease is None
            or lease.owner_id != owner_id
            or lease.fencing_token != fencing_token
            or lease.expires_at <= now
        ):
            FENCING_REJECTIONS.inc()
            _reject("FENCING_TOKEN_REJECTED", "sender lease is stale, expired, or superseded")

    def validate_sender(
        self,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        now: datetime,
    ) -> None:
        with self.database.session_factory() as session:
            self._validate_sender(session, execution_scope, owner_id, fencing_token, now)

    def record_shadow_order(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        venue_order_id: str,
        *,
        now: datetime,
    ) -> UUID:
        """Record a synthetic SHADOW send; this method never connects to a venue."""

        with self.database.session_factory.begin() as session:
            self._validate_sender(session, execution_scope, owner_id, fencing_token, now)
            intent = session.get(OrderIntent, intent_id, with_for_update=True)
            if intent is None or intent.status != OrderIntentStatus.READY.value:
                _reject("ORDER_INTENT_NOT_READY", "only a ready intent can be shadow-sent")
            campaign = session.get(Campaign, intent.campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "intent campaign is missing")
            self._require_role(
                session, actor_id, "venue.record", campaign.account_id, campaign.venue
            )
            fact = VenueOrder(
                order_intent_id=intent_id,
                venue=campaign.venue,
                venue_order_id=venue_order_id,
                status=VenueOrderStatus.SENT.value,
                ordered_quantity=intent.quantity,
                filled_quantity=Decimal(0),
                updated_at=now,
            )
            session.add(fact)
            previous = intent.status
            intent.status = OrderIntentStatus.SENT.value
            intent.updated_at = now
            intent.version += 1
            session.flush()
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="SHADOW_ORDER_RECORDED",
                object_type="VenueOrder",
                object_id=fact.venue_order_fact_id,
                reason=venue_order_id,
                correlation_id=intent.correlation_id,
                object_version=1,
                now=now,
            )
            INTENT_TRANSITIONS.labels(previous, intent.status).inc()
            return fact.venue_order_fact_id

    def record_fill(
        self,
        intent_id: UUID,
        actor_id: UUID,
        venue_fill_id: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
        fee_currency: str,
        slippage_cost: Decimal,
        *,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            intent = session.get(OrderIntent, intent_id, with_for_update=True)
            if intent is None:
                _reject("ORDER_INTENT_NOT_FOUND", "intent does not exist")
            campaign = session.get(Campaign, intent.campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "intent campaign is missing")
            self._require_role(
                session, actor_id, "venue.record", campaign.account_id, campaign.venue
            )
            existing = session.scalar(
                select(VenueFill).where(
                    VenueFill.venue == campaign.venue,
                    VenueFill.venue_fill_id == venue_fill_id,
                )
            )
            if existing is not None:
                return existing.venue_fill_fact_id
            venue_order = session.scalar(
                select(VenueOrder).where(VenueOrder.order_intent_id == intent_id)
            )
            if venue_order is None:
                _reject("VENUE_ORDER_MISSING", "fill must reference a known venue order")
            fact = VenueFill(
                venue=campaign.venue,
                venue_fill_id=venue_fill_id,
                order_intent_id=intent_id,
                campaign_id=campaign.campaign_id,
                side=side,
                quantity=quantity,
                price=price,
                fee=fee,
                fee_currency=fee_currency,
                slippage_cost=slippage_cost,
                executed_at=now,
            )
            session.add(fact)
            session.flush()
            total_filled = session.execute(
                select(func.coalesce(func.sum(VenueFill.quantity), 0)).where(
                    VenueFill.order_intent_id == intent_id
                )
            ).scalar_one()
            previous = intent.status
            if total_filled >= intent.quantity:
                intent.status = OrderIntentStatus.FILLED.value
                venue_order.status = VenueOrderStatus.FILLED.value
            else:
                intent.status = OrderIntentStatus.PARTIALLY_FILLED.value
                venue_order.status = VenueOrderStatus.PARTIALLY_FILLED.value
            venue_order.filled_quantity = total_filled
            venue_order.updated_at = now
            intent.updated_at = now
            intent.version += 1
            if intent.reservation_id is not None:
                reservation = session.get(RiskReservation, intent.reservation_id)
                if reservation is not None:
                    reservation.status = ReservationStatus.OPEN.value
                    reservation.updated_at = now
                    reservation.version += 1
            campaign.status = CampaignStatus.OPEN.value
            campaign.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="VENUE_FILL_RECORDED",
                object_type="VenueFill",
                object_id=fact.venue_fill_fact_id,
                reason=venue_fill_id,
                correlation_id=intent.correlation_id,
                object_version=1,
                now=now,
            )
            INTENT_TRANSITIONS.labels(previous, intent.status).inc()
            return fact.venue_fill_fact_id

    def record_position(
        self,
        account_id: str,
        venue: str,
        instrument_id: UUID,
        quantity: Decimal,
        average_entry_price: Decimal,
        mark_price: Decimal,
        known: bool,
        actor_id: UUID,
        *,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "venue.record", account_id, venue)
            position = session.scalar(
                select(Position).where(
                    Position.account_id == account_id,
                    Position.venue == venue,
                    Position.instrument_id == instrument_id,
                )
            )
            if position is None:
                position = Position(
                    account_id=account_id,
                    venue=venue,
                    instrument_id=instrument_id,
                    quantity=quantity,
                    average_entry_price=average_entry_price,
                    mark_price=mark_price,
                    fact_status=FactStatus.KNOWN.value if known else FactStatus.UNKNOWN.value,
                    updated_at=now,
                )
                session.add(position)
                session.flush()
            else:
                position.quantity = quantity
                position.average_entry_price = average_entry_price
                position.mark_price = mark_price
                position.fact_status = FactStatus.KNOWN.value if known else FactStatus.UNKNOWN.value
                position.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="POSITION_RECORDED",
                object_type="Position",
                object_id=position.position_id,
                reason=position.fact_status,
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return position.position_id

    def record_protection(
        self,
        position_id: UUID,
        venue_order_id: str,
        quantity: Decimal,
        trigger_price: Decimal,
        fully_covered: bool,
        actor_id: UUID,
        *,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            position = session.get(Position, position_id)
            if position is None:
                _reject("POSITION_NOT_FOUND", "protection position is missing")
            self._require_role(
                session, actor_id, "venue.record", position.account_id, position.venue
            )
            protection = session.scalar(
                select(ProtectionOrder).where(ProtectionOrder.position_id == position_id)
            )
            status = ProtectionStatus.ACTIVE if fully_covered else ProtectionStatus.DEGRADED
            if protection is None:
                protection = ProtectionOrder(
                    position_id=position_id,
                    venue_order_id=venue_order_id,
                    quantity=quantity,
                    trigger_price=trigger_price,
                    status=status.value,
                    fully_covered=fully_covered,
                    updated_at=now,
                )
                session.add(protection)
                session.flush()
            else:
                protection.venue_order_id = venue_order_id
                protection.quantity = quantity
                protection.trigger_price = trigger_price
                protection.status = status.value
                protection.fully_covered = fully_covered
                protection.updated_at = now
            if not fully_covered:
                PROTECTION_ISSUES.inc()
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="PROTECTION_RECORDED",
                object_type="ProtectionOrder",
                object_id=protection.protection_id,
                reason=status.value,
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return protection.protection_id

    def record_account_equity(
        self,
        account_id: str,
        venue: str,
        equity: Decimal,
        available_balance: Decimal,
        currency: str,
        known: bool,
        actor_id: UUID,
        *,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "venue.record", account_id, venue)
            fact = session.scalar(
                select(AccountEquity).where(
                    AccountEquity.account_id == account_id,
                    AccountEquity.venue == venue,
                )
            )
            if fact is None:
                fact = AccountEquity(
                    account_id=account_id,
                    venue=venue,
                    equity=equity,
                    available_balance=available_balance,
                    currency=currency,
                    fact_status=FactStatus.KNOWN.value if known else FactStatus.UNKNOWN.value,
                    updated_at=now,
                )
                session.add(fact)
                session.flush()
            else:
                fact.equity = equity
                fact.available_balance = available_balance
                fact.currency = currency
                fact.fact_status = FactStatus.KNOWN.value if known else FactStatus.UNKNOWN.value
                fact.updated_at = now
            return fact.account_equity_id

    def record_funding(
        self,
        campaign_id: UUID,
        venue: str,
        venue_payment_id: str,
        amount: Decimal,
        currency: str,
        actor_id: UUID,
        *,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "funding campaign is missing")
            self._require_role(
                session, actor_id, "venue.record", campaign.account_id, campaign.venue
            )
            existing = session.scalar(
                select(FundingPayment).where(
                    FundingPayment.venue == venue,
                    FundingPayment.venue_payment_id == venue_payment_id,
                )
            )
            if existing is not None:
                return existing.funding_payment_id
            payment = FundingPayment(
                campaign_id=campaign_id,
                venue=venue,
                venue_payment_id=venue_payment_id,
                amount=amount,
                currency=currency,
                paid_at=now,
            )
            session.add(payment)
            session.flush()
            return payment.funding_payment_id

    def update_campaign_target(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        candidates: tuple[TargetCandidate, ...],
        *,
        now: datetime,
    ) -> TargetDecision:
        decision = select_target_position(candidates)
        with self.database.session_factory.begin() as session:
            campaign = session.get(Campaign, campaign_id, with_for_update=True)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self._require_role(
                session, actor_id, "order.prepare", campaign.account_id, campaign.venue
            )
            campaign.current_target_quantity = decision.target_quantity
            campaign.target_version += 1
            campaign.target_reason = ",".join(decision.reasons)
            campaign.target_urgency = decision.urgency.value
            campaign.target_calculated_at = now
            campaign.status = (
                CampaignStatus.CLOSING.value
                if decision.target_quantity == 0
                else CampaignStatus.REDUCING.value
            )
            campaign.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAMPAIGN_TARGET_UPDATED",
                object_type="Campaign",
                object_id=campaign.campaign_id,
                reason=campaign.target_reason,
                correlation_id=uuid4(),
                object_version=campaign.target_version,
                now=now,
            )
        return decision

    def create_reduction_intent(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        idempotency_key: str,
        *,
        now: datetime,
    ) -> UUID:
        operation = "order.reduce"
        with self.database.session_factory.begin() as session:
            campaign = session.get(Campaign, campaign_id, with_for_update=True)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self._require_role(
                session, actor_id, "order.prepare", campaign.account_id, campaign.venue
            )
            position = session.scalar(
                select(Position).where(
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.instrument_id == campaign.instrument_id,
                )
            )
            if position is None or position.fact_status != FactStatus.KNOWN.value:
                _reject("POSITION_UNKNOWN", "reduction requires current known position")
            payload = {
                "campaign_id": str(campaign_id),
                "target_version": campaign.target_version,
                "target_quantity": str(campaign.current_target_quantity),
                "position_quantity": str(position.quantity),
            }
            digest, response = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return _as_uuid(str(response["intent_id"]))
            active = session.scalar(
                select(OrderIntent.intent_id).where(
                    OrderIntent.campaign_id == campaign_id,
                    OrderIntent.status.in_(ACTIVE_INTENT_STATUSES),
                )
            )
            if active is not None:
                _reject("ACTIVE_ORDER_INTENT", "campaign already has unresolved intent")
            reduction_quantity = abs(position.quantity) - campaign.current_target_quantity
            if reduction_quantity <= 0:
                _reject("TARGET_NOT_REDUCING", "target does not reduce current position")
            side = "SELL" if position.quantity > 0 else "BUY"
            kind = IntentKind.EXIT if campaign.current_target_quantity == 0 else IntentKind.REDUCE
            intent = OrderIntent(
                campaign_id=campaign_id,
                authorization_id=campaign.authorization_id,
                reservation_id=None,
                kind=kind.value,
                side=side,
                quantity=reduction_quantity,
                reduce_only=True,
                status=OrderIntentStatus.READY.value,
                semantic_hash=digest,
                actor_id=str(actor_id),
                correlation_id=uuid4(),
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(intent)
            session.flush()
            result = {"intent_id": str(intent.intent_id)}
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            return intent.intent_id

    def record_scope_reconciliation(
        self,
        execution_scope: str,
        actor_id: UUID,
        status: ReconciliationStatus,
        differences: tuple[str, ...],
        *,
        now: datetime,
        campaign_id: UUID | None = None,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            account_id, venue = _scope_parts(execution_scope)
            self._require_role(session, actor_id, "reconcile", account_id, venue)
            run = ReconciliationRun(
                execution_scope=execution_scope,
                campaign_id=campaign_id,
                status=status.value,
                differences=list(differences),
                resolution_reason=None,
                actor_id=str(actor_id),
                correlation_id=uuid4(),
                started_at=now,
                completed_at=now,
            )
            session.add(run)
            session.flush()
            RECONCILIATION_RESULTS.labels(status.value).inc()
            return run.reconciliation_id

    def require_manual_reconciliation(
        self, reconciliation_id: UUID, actor_id: UUID, reason: str, *, now: datetime
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            run = session.get(ReconciliationRun, reconciliation_id, with_for_update=True)
            if run is None:
                _reject("RECONCILIATION_NOT_FOUND", "run does not exist")
            account_id, venue = _scope_parts(run.execution_scope)
            self._require_role(session, actor_id, "reconcile", account_id, venue)
            run.status = ReconciliationStatus.MANUAL_REQUIRED.value
            run.resolution_reason = reason
            run.completed_at = now
            return run.reconciliation_id

    def resolve_reconciliation(
        self, reconciliation_id: UUID, actor_id: UUID, reason: str, *, now: datetime
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            run = session.get(ReconciliationRun, reconciliation_id, with_for_update=True)
            if run is None:
                _reject("RECONCILIATION_NOT_FOUND", "run does not exist")
            account_id, venue = _scope_parts(run.execution_scope)
            self._require_role(session, actor_id, "reconcile", account_id, venue)
            run.status = ReconciliationStatus.RESOLVED.value
            run.resolution_reason = reason
            run.completed_at = now
            return run.reconciliation_id

    def reconciliation_status(self, reconciliation_id: UUID) -> ReconciliationStatus:
        with self.database.session_factory() as session:
            run = session.get(ReconciliationRun, reconciliation_id)
            if run is None:
                _reject("RECONCILIATION_NOT_FOUND", "run does not exist")
            return ReconciliationStatus(run.status)

    def reconcile_campaign(
        self,
        campaign_id: UUID,
        execution_scope: str,
        actor_id: UUID,
        *,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self._require_role(session, actor_id, "reconcile", campaign.account_id, campaign.venue)
            intents = session.scalars(
                select(OrderIntent).where(OrderIntent.campaign_id == campaign_id)
            ).all()
            fills = session.scalars(
                select(VenueFill).where(VenueFill.campaign_id == campaign_id)
            ).all()
            position = session.scalar(
                select(Position).where(
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.instrument_id == campaign.instrument_id,
                )
            )
            equity = session.scalar(
                select(AccountEquity).where(
                    AccountEquity.account_id == campaign.account_id,
                    AccountEquity.venue == campaign.venue,
                )
            )
            differences: list[str] = []
            unknown: list[str] = []
            if not intents:
                differences.append("ORDER_INTENT_MISSING")
            for intent in intents:
                order = session.scalar(
                    select(VenueOrder).where(VenueOrder.order_intent_id == intent.intent_id)
                )
                if order is None:
                    differences.append(f"VENUE_ORDER_MISSING:{intent.intent_id}")
                else:
                    intent_fill_quantity = sum(
                        (
                            fill.quantity
                            for fill in fills
                            if fill.order_intent_id == intent.intent_id
                        ),
                        Decimal(0),
                    )
                    if order.filled_quantity != intent_fill_quantity:
                        differences.append(f"ORDER_FILL_MISMATCH:{intent.intent_id}")
                    if order.status == VenueOrderStatus.UNKNOWN.value:
                        unknown.append(f"VENUE_ORDER_UNKNOWN:{intent.intent_id}")
                if intent.status == OrderIntentStatus.UNKNOWN.value:
                    unknown.append(f"ORDER_UNKNOWN:{intent.intent_id}")
            if position is None or position.fact_status == FactStatus.UNKNOWN.value:
                unknown.append("POSITION_UNKNOWN")
            if equity is None or equity.fact_status == FactStatus.UNKNOWN.value:
                unknown.append("ACCOUNT_EQUITY_UNKNOWN")
            if position is not None and position.fact_status == FactStatus.KNOWN.value:
                signed_fills = sum(
                    (fill.quantity if fill.side == "BUY" else -fill.quantity for fill in fills),
                    Decimal(0),
                )
                if signed_fills != position.quantity:
                    differences.append("POSITION_QUANTITY_MISMATCH")
                if position.quantity != 0:
                    protection = session.scalar(
                        select(ProtectionOrder).where(
                            ProtectionOrder.position_id == position.position_id
                        )
                    )
                    if protection is None or protection.status == ProtectionStatus.UNKNOWN.value:
                        unknown.append("PROTECTION_UNKNOWN")
                    elif not protection.fully_covered or protection.quantity < abs(
                        position.quantity
                    ):
                        differences.append("PROTECTION_INSUFFICIENT")

        if unknown:
            status = ReconciliationStatus.UNKNOWN
            result_differences = tuple(sorted(set(unknown + differences)))
        elif differences:
            status = ReconciliationStatus.DIFFERENCE
            result_differences = tuple(sorted(set(differences)))
        else:
            status = ReconciliationStatus.MATCH
            result_differences = ()
        return self.record_scope_reconciliation(
            execution_scope,
            actor_id,
            status,
            result_differences,
            now=now,
            campaign_id=campaign_id,
        )

    def refresh_campaign_pnl(
        self, campaign_id: UUID, actor_id: UUID, *, now: datetime
    ) -> PnlBreakdown:
        with self.database.session_factory.begin() as session:
            campaign = session.get(Campaign, campaign_id, with_for_update=True)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self._require_role(session, actor_id, "view", campaign.account_id, campaign.venue)
            fills = session.scalars(
                select(VenueFill)
                .where(VenueFill.campaign_id == campaign_id)
                .order_by(VenueFill.executed_at, VenueFill.venue_fill_fact_id)
            ).all()
            position = session.scalar(
                select(Position).where(
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.instrument_id == campaign.instrument_id,
                )
            )
            if position is None or position.fact_status != FactStatus.KNOWN.value:
                _reject("POSITION_UNKNOWN", "PnL requires known current position")
            funding = session.execute(
                select(func.coalesce(func.sum(FundingPayment.amount), 0)).where(
                    FundingPayment.campaign_id == campaign_id
                )
            ).scalar_one()
            result = compute_pnl(
                fills=tuple(
                    EconomicFill(
                        fill.side,
                        fill.quantity,
                        fill.price,
                        fill.fee,
                        fill.slippage_cost,
                    )
                    for fill in fills
                ),
                mark_price=position.mark_price,
                funding=funding,
            )
            campaign.realized_pnl = result.realized_pnl
            campaign.unrealized_pnl = result.unrealized_pnl
            campaign.final_pnl = result.total_pnl
            campaign.updated_at = now
            return result

    def set_capability_gate(
        self,
        capability_key: str,
        status: CapabilityStatus,
        reason: str,
        actor_id: UUID,
        *,
        now: datetime,
    ) -> None:
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "capability.manage")
            gate = session.get(CapabilityGate, capability_key, with_for_update=True)
            if gate is None:
                _reject("CAPABILITY_GATE_NOT_FOUND", "unknown capability")
            gate.status = status.value
            gate.reason = reason
            gate.operator_id = str(actor_id)
            gate.updated_at = now
