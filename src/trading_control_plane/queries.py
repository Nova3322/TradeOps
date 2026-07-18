from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from trading_control_plane.database import Database
from trading_control_plane.domain import DomainRejected, PrincipalType, Role
from trading_control_plane.models import (
    Approval,
    Instrument,
    Proposal,
    RiskDecision,
    RoleAssignment,
    TradingAuthorization,
    User,
)
from trading_control_plane.service import TradingService


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


class TradingQueries:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.service = TradingService(database)

    def user_by_username(self, username: str) -> User:
        with self.database.session_factory() as session:
            user = session.scalar(select(User).where(User.username == username))
            if user is None or not user.active or user.principal_type != PrincipalType.HUMAN.value:
                raise DomainRejected("LOGIN_DENIED", "internal user is missing or inactive")
            session.expunge(user)
            return user

    def service_principal_by_username(self, username: str) -> User:
        with self.database.session_factory() as session:
            user = session.scalar(select(User).where(User.username == username))
            if (
                user is None
                or not user.active
                or user.principal_type != PrincipalType.SERVICE.value
            ):
                raise DomainRejected(
                    "PERPTAPE_PRINCIPAL_MISSING",
                    "configured Perptape service principal is missing or inactive",
                )
            session.expunge(user)
            return user

    def user_context(self, user_id: UUID) -> dict[str, Any]:
        with self.database.session_factory() as session:
            user = session.get(User, user_id)
            if user is None or not user.active:
                raise DomainRejected("SESSION_REVOKED", "internal user is inactive or missing")
            roles = session.scalars(
                select(RoleAssignment)
                .where(RoleAssignment.user_id == user_id)
                .order_by(RoleAssignment.role)
            ).all()
            return {
                "user_id": str(user.user_id),
                "username": user.username,
                "roles": [
                    {
                        "role": role.role,
                        "account_scope": role.account_scope,
                        "venue_scope": role.venue_scope,
                    }
                    for role in roles
                ],
            }

    def list_instruments(self, user_id: UUID) -> list[dict[str, Any]]:
        with self.database.session_factory() as session:
            user = session.get(User, user_id)
            if user is None or not user.active:
                raise DomainRejected("SESSION_REVOKED", "internal user is inactive or missing")
            assignments = session.scalars(
                select(RoleAssignment).where(RoleAssignment.user_id == user_id)
            ).all()
            values = session.scalars(
                select(Instrument)
                .where(Instrument.active)
                .order_by(Instrument.venue, Instrument.symbol)
            ).all()
            return [
                {
                    "instrument_id": str(item.instrument_id),
                    "venue": item.venue,
                    "symbol": item.symbol,
                    "tick_size": str(item.tick_size),
                    "lot_size": str(item.lot_size),
                    "minimum_notional": str(item.minimum_notional),
                    "contract_multiplier": str(item.contract_multiplier),
                    "quote_currency": item.quote_currency,
                    "collateral_currency": item.collateral_currency,
                    "protection_supported": item.protection_supported,
                    "updated_at": _iso(item.updated_at),
                }
                for item in values
                if any(
                    assignment.venue_scope is None or assignment.venue_scope == item.venue
                    for assignment in assignments
                )
            ]

    def instrument_id_by_venue_symbol(self, venue: str, symbol: str) -> UUID:
        with self.database.session_factory() as session:
            instrument = session.scalar(
                select(Instrument).where(
                    Instrument.venue == venue,
                    Instrument.symbol == symbol,
                    Instrument.active,
                )
            )
            if instrument is None:
                raise DomainRejected(
                    "INSTRUMENT_UNAVAILABLE",
                    "candidate instrument is not active in the Trading catalog",
                )
            return instrument.instrument_id

    def list_proposals(self, user_id: UUID, *, status: str | None = None) -> list[dict[str, Any]]:
        with self.database.session_factory() as session:
            statement = select(Proposal).order_by(Proposal.created_at.desc())
            if status is not None:
                statement = statement.where(Proposal.status == status)
            values = session.scalars(statement).all()
            return [
                self._proposal_summary(item)
                for item in values
                if self.service.can_user(user_id, "view", item.account_id, item.venue)
            ]

    def proposal_detail(self, user_id: UUID, proposal_id: UUID) -> dict[str, Any]:
        with self.database.session_factory() as session:
            proposal = session.get(Proposal, proposal_id)
            if proposal is None:
                raise DomainRejected("PROPOSAL_NOT_FOUND", "proposal does not exist")
            if not self.service.can_user(user_id, "view", proposal.account_id, proposal.venue):
                raise DomainRejected("RBAC_DENIED", "proposal is outside the current scope")
            approvals = session.scalars(
                select(Approval)
                .where(Approval.proposal_id == proposal_id)
                .order_by(Approval.created_at)
            ).all()
            risk = session.scalar(
                select(RiskDecision)
                .where(RiskDecision.proposal_id == proposal_id)
                .order_by(RiskDecision.created_at.desc())
                .limit(1)
            )
            authorization = session.scalar(
                select(TradingAuthorization).where(TradingAuthorization.proposal_id == proposal_id)
            )
            result = self._proposal_summary(proposal)
            result.update(
                {
                    "frozen_payload": proposal.frozen_payload,
                    "semantic_hash": proposal.semantic_hash,
                    "frozen_at": _iso(proposal.frozen_at),
                    "correlation_id": str(proposal.correlation_id),
                    "approvals": [
                        {
                            "approval_id": str(item.approval_id),
                            "reviewer_id": str(item.reviewer_id),
                            "decision": item.decision,
                            "reason": item.reason,
                            "created_at": _iso(item.created_at),
                        }
                        for item in approvals
                    ],
                    "risk_decision": None
                    if risk is None
                    else {
                        "decision_id": str(risk.decision_id),
                        "result": risk.result,
                        "approved_quantity": str(risk.approved_quantity),
                        "risk_amount": str(risk.risk_amount),
                        "reasons": risk.reasons,
                        "data_as_of": _iso(risk.data_as_of),
                    },
                    "authorization": None
                    if authorization is None
                    else {
                        "authorization_id": str(authorization.authorization_id),
                        "quantity_limit": str(authorization.quantity_limit),
                        "risk_limit": str(authorization.risk_limit),
                        "allowed_adds": authorization.allowed_adds,
                        "used_adds": authorization.used_adds,
                        "active": authorization.active,
                        "expires_at": _iso(authorization.expires_at),
                    },
                }
            )
            return result

    def proposal_version(self, proposal_id: UUID) -> int:
        with self.database.session_factory() as session:
            proposal = session.get(Proposal, proposal_id)
            if proposal is None:
                raise DomainRejected("PROPOSAL_NOT_FOUND", "proposal does not exist")
            return proposal.version

    def reviewers_for_proposal(self, proposal_id: UUID) -> list[User]:
        with self.database.session_factory() as session:
            proposal = session.get(Proposal, proposal_id)
            if proposal is None:
                raise DomainRejected("PROPOSAL_NOT_FOUND", "proposal does not exist")
            assignments = session.scalars(
                select(RoleAssignment).where(RoleAssignment.role == Role.REVIEWER.value)
            ).all()
            reviewer_ids = {
                item.user_id
                for item in assignments
                if (item.account_scope is None or item.account_scope == proposal.account_id)
                and (item.venue_scope is None or item.venue_scope == proposal.venue)
                and item.user_id != proposal.proposer_id
            }
            users = session.scalars(
                select(User).where(User.user_id.in_(reviewer_ids), User.active)
            ).all()
            for user in users:
                session.expunge(user)
            return list(users)

    @staticmethod
    def _proposal_summary(proposal: Proposal) -> dict[str, Any]:
        return {
            "proposal_id": str(proposal.proposal_id),
            "source": proposal.source,
            "environment": proposal.environment,
            "proposer_id": str(proposal.proposer_id),
            "strategy_id": proposal.strategy_id,
            "strategy_version": proposal.strategy_version,
            "source_candidate_id": proposal.source_candidate_id,
            "source_link": proposal.source_link,
            "source_observed_at": _iso(proposal.source_observed_at),
            "source_readiness": proposal.source_readiness,
            "status": proposal.status,
            "version": proposal.version,
            "risk_tier": proposal.risk_tier,
            "account_id": proposal.account_id,
            "venue": proposal.venue,
            "instrument_id": str(proposal.instrument_id),
            "direction": proposal.direction,
            "quantity": str(proposal.quantity),
            "max_risk": str(proposal.max_risk),
            "expires_at": _iso(proposal.expires_at),
            "created_at": _iso(proposal.created_at),
            "updated_at": _iso(proposal.updated_at),
        }
