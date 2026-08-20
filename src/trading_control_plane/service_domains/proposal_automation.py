from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trading_control_plane import domain, models, rejections
from trading_control_plane.database import Database

FACT_RETRY_REASONS = frozenset(
    {
        "READ_ONLY_SOURCE_UNAVAILABLE",
        "STALE_FACTS",
        "POSITION_UNKNOWN",
        "EQUITY_UNKNOWN",
        "PROTECTION_UNKNOWN",
    }
)
POLICY_RETRY_REASONS = frozenset(
    {
        "RISK_LIMITS_UNCONFIGURED",
        "KILL_SWITCH",
        "REDUCE_ONLY",
        "PYRAMID_DISABLED",
    }
)
CAPACITY_RETRY_REASONS = frozenset({"RISK_CAPACITY_EXHAUSTED"})


class ApprovedProposalAutomationService(Protocol):
    @property
    def database(self) -> Database: ...

    def issue_authorization(
        self,
        *,
        proposal_id: UUID,
        actor_id: UUID,
        expires_at: datetime,
        allowed_adds: int,
        idempotency_key: str,
        now: datetime,
    ) -> UUID: ...

    def decide_risk(
        self,
        *,
        proposal_id: UUID,
        actor_id: UUID,
        kind: domain.IntentKind,
        idempotency_key: str,
        now: datetime,
        requested_quantity: Decimal | None = None,
    ) -> UUID: ...

    def create_order_intent(
        self,
        authorization_id: UUID,
        actor_id: UUID,
        kind: domain.IntentKind,
        account_id: str,
        venue: str,
        instrument_id: UUID,
        direction: domain.Direction,
        quantity: Decimal,
        idempotency_key: str,
        *,
        now: datetime,
    ) -> domain.IntentCreation: ...


def automatic_workflow_actor(
    session: Session,
    *,
    proposal: models.Proposal,
    fallback_username: str,
) -> models.User:
    account = session.scalar(
        select(models.ExchangeAccount).where(
            models.ExchangeAccount.team_id == proposal.team_id,
            models.ExchangeAccount.environment == proposal.environment,
            models.ExchangeAccount.account_id == proposal.account_id,
            models.ExchangeAccount.venue == proposal.venue,
            models.ExchangeAccount.active,
            models.ExchangeAccount.deleted_at.is_(None),
        )
    )
    actor = (
        None
        if account is None or account.runtime_service_principal_id is None
        else session.get(models.User, account.runtime_service_principal_id)
    )
    if (
        actor is None
        or not actor.active
        or actor.principal_type != domain.PrincipalType.SERVICE.value
    ):
        actor = session.scalar(
            select(models.User).where(
                models.User.username == fallback_username,
                models.User.principal_type == domain.PrincipalType.SERVICE.value,
                models.User.active,
            )
        )
    if actor is None:
        rejections.reject(
            "AUTOMATIC_WORKFLOW_ACTOR_UNAVAILABLE",
            "the configured automatic workflow service principal is unavailable",
        )
    return actor


def _latest_risk_retry_input_at(
    session: Session,
    *,
    proposal: models.Proposal,
    decision: models.RiskDecision,
    now: datetime,
) -> datetime | None:
    reasons = frozenset(decision.reasons)
    changed_at: list[datetime] = []

    if reasons & FACT_RETRY_REASONS:
        for value in (
            session.scalar(
                select(func.max(models.RuntimeSourceHealth.checked_at)).where(
                    models.RuntimeSourceHealth.team_id == proposal.team_id,
                    models.RuntimeSourceHealth.environment == proposal.environment,
                    models.RuntimeSourceHealth.source_name == proposal.venue,
                    models.RuntimeSourceHealth.account_id == proposal.account_id,
                    models.RuntimeSourceHealth.venue == proposal.venue,
                )
            ),
            session.scalar(
                select(func.max(models.Position.updated_at)).where(
                    models.Position.team_id == proposal.team_id,
                    models.Position.environment == proposal.environment,
                    models.Position.account_id == proposal.account_id,
                    models.Position.venue == proposal.venue,
                    models.Position.instrument_id == proposal.instrument_id,
                )
            ),
            session.scalar(
                select(func.max(models.AccountEquity.updated_at)).where(
                    models.AccountEquity.team_id == proposal.team_id,
                    models.AccountEquity.environment == proposal.environment,
                )
            ),
            session.scalar(
                select(func.max(models.ProtectionOrder.updated_at))
                .join(
                    models.Position,
                    models.Position.position_id == models.ProtectionOrder.position_id,
                )
                .where(
                    models.Position.team_id == proposal.team_id,
                    models.Position.environment == proposal.environment,
                    models.Position.account_id == proposal.account_id,
                    models.Position.venue == proposal.venue,
                    models.Position.instrument_id == proposal.instrument_id,
                )
            ),
        ):
            if value is not None:
                changed_at.append(value)

    if reasons & POLICY_RETRY_REASONS:
        policy_updated_at = session.scalar(
            select(func.max(models.RiskPolicy.updated_at)).where(
                models.RiskPolicy.team_id == proposal.team_id,
                models.RiskPolicy.active,
            )
        )
        if policy_updated_at is not None:
            changed_at.append(policy_updated_at)

    if reasons & CAPACITY_RETRY_REASONS:
        for value in (
            session.scalar(
                select(func.max(models.Campaign.updated_at)).where(
                    models.Campaign.team_id == proposal.team_id,
                    models.Campaign.environment == proposal.environment,
                )
            ),
            session.scalar(
                select(func.max(models.RiskReservation.updated_at))
                .join(
                    models.Campaign,
                    models.Campaign.campaign_id == models.RiskReservation.campaign_id,
                )
                .where(
                    models.Campaign.team_id == proposal.team_id,
                    models.Campaign.environment == proposal.environment,
                )
            ),
        ):
            if value is not None:
                changed_at.append(value)

    if "LOSS_COOLDOWN_ACTIVE" in reasons:
        remaining_raw = decision.input_data.get("loss_cooldown_remaining_seconds")
        try:
            remaining = Decimal(str(remaining_raw))
        except (TypeError, ValueError):
            remaining = Decimal(0)
        if remaining > 0:
            retry_at = decision.created_at + timedelta(seconds=float(remaining))
            if now >= retry_at:
                changed_at.append(retry_at)

    return max(changed_at, default=None)


def refresh_approved_proposal_risk(
    service: ApprovedProposalAutomationService,
    *,
    proposal_id: UUID,
    fallback_service_username: str,
    now: datetime,
) -> bool:
    """Retry automatic risk only after a relevant denied input has changed."""

    with service.database.session_factory() as session:
        proposal = session.get(models.Proposal, proposal_id)
        if proposal is None:
            rejections.reject("PROPOSAL_NOT_FOUND", "proposal does not exist")
        if proposal.status != domain.ProposalStatus.APPROVED.value:
            rejections.reject(
                "PROPOSAL_NOT_APPROVED",
                "automatic workflow requires an approved proposal",
            )
        if proposal.expires_at <= now:
            rejections.reject("PROPOSAL_EXPIRED", "approved proposal has expired")
        actor = automatic_workflow_actor(
            session,
            proposal=proposal,
            fallback_username=fallback_service_username,
        )
        decision = session.scalar(
            select(models.RiskDecision)
            .where(models.RiskDecision.proposal_id == proposal_id)
            .order_by(models.RiskDecision.created_at.desc())
            .limit(1)
        )
        if decision is None:
            retry_marker = f"proposal-v{proposal.version}"
        elif decision.result != domain.RiskResult.DENY.value:
            authorization_exists = session.scalar(
                select(models.TradingAuthorization.authorization_id).where(
                    models.TradingAuthorization.proposal_id == proposal_id
                )
            )
            if authorization_exists is not None:
                return False
            policy_updated_at = session.scalar(
                select(func.max(models.RiskPolicy.updated_at)).where(
                    models.RiskPolicy.team_id == proposal.team_id,
                    models.RiskPolicy.active,
                )
            )
            if policy_updated_at is None or policy_updated_at <= decision.created_at:
                return False
            retry_marker = f"policy-{int(policy_updated_at.timestamp() * 1_000_000)}"
        else:
            retry_input_at = _latest_risk_retry_input_at(
                session,
                proposal=proposal,
                decision=decision,
                now=now,
            )
            if retry_input_at is None or retry_input_at <= decision.created_at:
                return False
            retry_marker = str(int(retry_input_at.timestamp() * 1_000_000))
        actor_id = actor.user_id

    service.decide_risk(
        proposal_id=proposal_id,
        actor_id=actor_id,
        kind=domain.IntentKind.INITIAL,
        idempotency_key=f"automatic-risk-retry:{proposal_id}:{retry_marker}",
        now=now,
    )
    return True


def advance_approved_proposal(
    service: ApprovedProposalAutomationService,
    *,
    proposal_id: UUID,
    fallback_service_username: str,
    now: datetime,
) -> dict[str, str | None]:
    """Idempotently prepare one approved proposal through its READY initial intent.

    This boundary creates governance records only. It never acquires a sender lease,
    calls Freqtrade, submits an exchange order, or performs a capital operation.
    """

    with service.database.session_factory() as session:
        proposal = session.get(models.Proposal, proposal_id)
        if proposal is None:
            rejections.reject("PROPOSAL_NOT_FOUND", "proposal does not exist")
        if proposal.status != domain.ProposalStatus.APPROVED.value:
            rejections.reject(
                "PROPOSAL_NOT_APPROVED",
                "automatic workflow requires an approved proposal",
            )
        if proposal.expires_at <= now:
            rejections.reject("PROPOSAL_EXPIRED", "approved proposal has expired")
        actor = automatic_workflow_actor(
            session,
            proposal=proposal,
            fallback_username=fallback_service_username,
        )
        decision = session.scalar(
            select(models.RiskDecision)
            .where(models.RiskDecision.proposal_id == proposal_id)
            .order_by(models.RiskDecision.created_at.desc())
            .limit(1)
        )
        if decision is None:
            rejections.reject(
                "AUTOMATIC_RISK_DECISION_MISSING",
                "approved proposal is missing its automatic risk decision",
            )
        if decision.result == domain.RiskResult.DENY.value:
            return {
                "status": "RISK_DENIED",
                "risk_decision_id": str(decision.decision_id),
                "authorization_id": None,
                "campaign_id": None,
                "reservation_id": None,
                "intent_id": None,
            }
        if decision.approved_quantity <= 0:
            rejections.reject(
                "AUTOMATIC_RISK_QUANTITY_INVALID",
                "allowing risk decision has no approved quantity",
            )
        authorization = session.scalar(
            select(models.TradingAuthorization).where(
                models.TradingAuthorization.proposal_id == proposal_id
            )
        )
        initial_intent = session.scalar(
            select(models.OrderIntent)
            .join(models.Campaign, models.Campaign.campaign_id == models.OrderIntent.campaign_id)
            .where(
                models.Campaign.proposal_id == proposal_id,
                models.OrderIntent.kind == domain.IntentKind.INITIAL.value,
            )
            .limit(1)
        )
        actor_id = actor.user_id
        approved_quantity = decision.approved_quantity
        risk_decision_id = decision.decision_id
        authorization_id = (
            None if authorization is None else authorization.authorization_id
        )
        existing_campaign_id = (
            None if initial_intent is None else initial_intent.campaign_id
        )
        existing_reservation_id = (
            None if initial_intent is None else initial_intent.reservation_id
        )
        existing_intent_id = None if initial_intent is None else initial_intent.intent_id
        proposal_expiry = proposal.expires_at
        account_id = proposal.account_id
        venue = proposal.venue
        instrument_id = proposal.instrument_id
        direction = domain.Direction(proposal.direction)
        details = proposal.frozen_payload.get("details")
        management = details if isinstance(details, dict) else {}
        auto_add_gate = session.get(models.CapabilityGate, "AUTO_ADD")
        allowed_adds = (
            int(management.get("requested_adds", 0))
            if management.get("allow_auto_add") is True
            and auto_add_gate is not None
            and auto_add_gate.status == domain.CapabilityStatus.ENABLED.value
            else 0
        )

    if authorization_id is None:
        authorization_id = service.issue_authorization(
            proposal_id=proposal_id,
            actor_id=actor_id,
            expires_at=min(proposal_expiry, now + timedelta(minutes=30)),
            allowed_adds=allowed_adds,
            idempotency_key=f"automatic-authorization:{proposal_id}",
            now=now,
        )

    campaign_id: UUID
    reservation_id: UUID | None
    intent_id: UUID
    if existing_intent_id is None:
        created = service.create_order_intent(
            authorization_id,
            actor_id,
            domain.IntentKind.INITIAL,
            account_id,
            venue,
            instrument_id,
            direction,
            approved_quantity,
            f"automatic-initial-intent:{proposal_id}",
            now=now,
        )
        campaign_id = created.campaign_id
        reservation_id = created.reservation_id
        intent_id = created.intent_id
    else:
        assert existing_campaign_id is not None
        campaign_id = existing_campaign_id
        reservation_id = existing_reservation_id
        intent_id = existing_intent_id

    return {
        "status": "READY",
        "risk_decision_id": str(risk_decision_id),
        "authorization_id": str(authorization_id),
        "campaign_id": str(campaign_id),
        "reservation_id": None if reservation_id is None else str(reservation_id),
        "intent_id": str(intent_id),
    }
