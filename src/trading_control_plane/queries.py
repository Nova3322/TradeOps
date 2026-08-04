from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, text, tuple_

from trading_control_plane.database import Database
from trading_control_plane.domain import DomainRejected, PrincipalType, Role
from trading_control_plane.models import (
    AccountEquity,
    AccountEquityObservation,
    Approval,
    AuditEvent,
    Campaign,
    CapabilityGate,
    CapitalAutomationPolicy,
    CapitalTransfer,
    DirectCapitalOperation,
    FundingPayment,
    Instrument,
    OrderIntent,
    PerptapeFeed,
    Position,
    Proposal,
    ProtectionOrder,
    ReconciliationRun,
    RiskDecision,
    RiskPolicy,
    RiskReservation,
    RoleAssignment,
    RuntimeSourceHealth,
    SenderLease,
    TradingAuthorization,
    TransferAuthorization,
    TransferProposal,
    User,
    VenueFill,
    VenueOrder,
)
from trading_control_plane.notilt import USD_STABLE_ASSETS
from trading_control_plane.perptape import PerptapeCandidate, PerptapeFeedSnapshot
from trading_control_plane.service import TradingService


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _uuid_or_none(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _effective_proposal_status(proposal: Proposal, now: datetime) -> str:
    if proposal.status in {"DRAFT", "PENDING_REVIEW"} and proposal.expires_at <= now:
        return "EXPIRED"
    return proposal.status


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
                    "SERVICE_PRINCIPAL_MISSING",
                    "configured service principal is missing or inactive",
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

    def managed_users(self, actor_id: UUID) -> list[dict[str, Any]]:
        if not self.service.can_user(actor_id, "user.manage"):
            raise DomainRejected("RBAC_DENIED", "user access management requires SYSTEM_ADMIN")
        with self.database.session_factory() as session:
            users = session.scalars(
                select(User)
                .where(User.principal_type == PrincipalType.HUMAN.value)
                .order_by(User.username)
            ).all()
            assignments = session.scalars(
                select(RoleAssignment)
                .where(RoleAssignment.user_id.in_([item.user_id for item in users]))
                .order_by(RoleAssignment.role)
            ).all()
            by_user: dict[UUID, list[dict[str, Any]]] = {item.user_id: [] for item in users}
            for assignment in assignments:
                by_user[assignment.user_id].append(
                    {
                        "role": assignment.role,
                        "account_scope": assignment.account_scope,
                        "venue_scope": assignment.venue_scope,
                    }
                )
            return [
                {
                    "user_id": str(user.user_id),
                    "username": user.username,
                    "identity_bound": user.identity_subject is not None,
                    "active": user.active,
                    "roles": by_user[user.user_id],
                    "created_at": _iso(user.created_at),
                    "is_current_user": user.user_id == actor_id,
                }
                for user in users
            ]

    def telegram_chat_id(self, user_id: UUID) -> str | None:
        with self.database.session_factory() as session:
            user = session.get(User, user_id)
            if user is None or not user.active or user.principal_type != PrincipalType.HUMAN.value:
                return None
            return user.telegram_chat_id

    def telegram_user_id(self, chat_id: str) -> UUID | None:
        with self.database.session_factory() as session:
            user = session.scalar(
                select(User).where(
                    User.telegram_chat_id == chat_id,
                    User.active,
                    User.principal_type == PrincipalType.HUMAN.value,
                )
            )
            return None if user is None else user.user_id

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

    def active_instrument_keys(self, venue_symbols: set[tuple[str, str]]) -> set[tuple[str, str]]:
        """Return exact active Catalog matches without normalizing or guessing symbols."""

        if not venue_symbols:
            return set()
        with self.database.session_factory() as session:
            return set(
                session.execute(
                    select(Instrument.venue, Instrument.symbol).where(
                        Instrument.active,
                        tuple_(Instrument.venue, Instrument.symbol).in_(venue_symbols),
                    )
                ).tuples()
            )

    def compatible_legacy_system_candidate_id(
        self,
        legacy_candidate_id: str,
        candidate: PerptapeCandidate,
        instrument_id: UUID,
    ) -> str | None:
        """Reuse an exact legacy proposal identity without conflating quote contracts."""

        with self.database.session_factory() as session:
            proposal = session.scalar(
                select(Proposal).where(
                    Proposal.source == "SYSTEM",
                    Proposal.source_candidate_id == legacy_candidate_id,
                )
            )
            if (
                proposal is None
                or proposal.instrument_id != instrument_id
                or proposal.venue != candidate.venue
                or proposal.direction != candidate.direction.value
            ):
                return None
            details = proposal.frozen_payload.get("details")
            snapshot = details.get("candidate") if isinstance(details, dict) else None
            if not isinstance(snapshot, dict):
                return None
            current = candidate.to_dict()
            identity_fields = (
                "venue",
                "source_exchange",
                "symbol",
                "canonical_symbol",
                "direction",
                "source_direction",
                "timeframe",
                "triggered_at",
            )
            if any(snapshot.get(field) != current[field] for field in identity_fields):
                return None
            return legacy_candidate_id

    def perptape_feed(self) -> PerptapeFeedSnapshot | None:
        with self.database.session_factory() as session:
            feed = session.get(PerptapeFeed, "BREAKOUTS")
            if feed is None:
                return None
            candidates: list[PerptapeCandidate] = []
            for value in feed.candidates:
                if not isinstance(value, dict):
                    raise DomainRejected(
                        "PERPTAPE_CACHE_INVALID",
                        "persisted Perptape feed contains an invalid candidate",
                    )
                candidates.append(PerptapeCandidate.from_dict(value))
            return PerptapeFeedSnapshot(
                contract_version=feed.contract_version,
                generated_at=feed.generated_at,
                fetched_at=feed.fetched_at,
                next_allowed_at=feed.next_allowed_at,
                candidates=tuple(candidates),
            )

    def list_proposals(
        self,
        user_id: UUID,
        *,
        status: str | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        current_time = now or datetime.now(UTC)
        with self.database.session_factory() as session:
            statement = (
                select(Proposal, Instrument)
                .join(Instrument, Instrument.instrument_id == Proposal.instrument_id)
                .order_by(Proposal.created_at.desc())
            )
            if status in {"DRAFT", "PENDING_REVIEW"}:
                statement = statement.where(
                    Proposal.status == status,
                    Proposal.expires_at > current_time,
                )
            elif status == "EXPIRED":
                statement = statement.where(
                    or_(
                        Proposal.status == "EXPIRED",
                        and_(
                            Proposal.status.in_(["DRAFT", "PENDING_REVIEW"]),
                            Proposal.expires_at <= current_time,
                        ),
                    )
                )
            elif status is not None:
                statement = statement.where(Proposal.status == status)
            values = session.execute(statement).all()
            reviewed_proposal_ids = set(
                session.scalars(select(Approval.proposal_id).where(Approval.reviewer_id == user_id))
            )
            reused_proposal_ids = set(
                session.scalars(
                    select(AuditEvent.object_id).where(
                        AuditEvent.event_type == "PROPOSAL_DUPLICATE_REUSED",
                        AuditEvent.object_type == "Proposal",
                        AuditEvent.actor_id == str(user_id),
                    )
                )
            )
            approval_counts = dict(
                session.execute(
                    select(Approval.proposal_id, func.count(Approval.approval_id)).group_by(
                        Approval.proposal_id
                    )
                ).all()
            )
            campaign_by_proposal = dict(
                session.execute(select(Campaign.proposal_id, Campaign.campaign_id)).all()
            )
            result: list[dict[str, Any]] = []
            for proposal, instrument in values:
                if not self.service.can_user(user_id, "view", proposal.account_id, proposal.venue):
                    continue
                effective_status = _effective_proposal_status(proposal, current_time)
                summary = self._proposal_summary(proposal, instrument)
                summary["status"] = effective_status
                summary["approval_count"] = int(approval_counts.get(proposal.proposal_id, 0))
                summary["required_approvals"] = 2 if proposal.risk_tier == "HIGH" else 1
                campaign_id = campaign_by_proposal.get(proposal.proposal_id)
                summary["campaign_id"] = None if campaign_id is None else str(campaign_id)
                summary["actionable_for_current_user"] = bool(
                    effective_status == "PENDING_REVIEW"
                    and proposal.proposer_id != user_id
                    and str(proposal.proposal_id) not in reused_proposal_ids
                    and proposal.proposal_id not in reviewed_proposal_ids
                    and self.service.can_user(
                        user_id,
                        "proposal.review",
                        proposal.account_id,
                        proposal.venue,
                    )
                )
                result.append(summary)
            return result

    def proposal_detail(
        self,
        user_id: UUID,
        proposal_id: UUID,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current_time = now or datetime.now(UTC)
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
                select(TradingAuthorization)
                .where(TradingAuthorization.proposal_id == proposal_id)
                .order_by(TradingAuthorization.created_at.desc())
                .limit(1)
            )
            campaign = session.scalar(
                select(Campaign)
                .where(Campaign.proposal_id == proposal_id)
                .order_by(Campaign.created_at.desc())
                .limit(1)
            )
            initial_intent = (
                None
                if campaign is None
                else session.scalar(
                    select(OrderIntent)
                    .where(
                        OrderIntent.campaign_id == campaign.campaign_id,
                        OrderIntent.kind == "INITIAL",
                    )
                    .order_by(OrderIntent.created_at.desc())
                    .limit(1)
                )
            )
            risk_inputs = risk.input_data if risk is not None else {}
            risk_policy = risk_inputs.get("policy", {})
            risk_position = risk_inputs.get("position")
            risk_equity = risk_inputs.get("equity")
            risk_capital = risk_inputs.get("managed_capital", {})
            risk_protection = risk_inputs.get("protection")
            result = self._proposal_summary(
                proposal, session.get(Instrument, proposal.instrument_id)
            )
            effective_status = _effective_proposal_status(proposal, current_time)
            reused_by_current_user = session.scalar(
                select(AuditEvent.audit_event_id)
                .where(
                    AuditEvent.event_type == "PROPOSAL_DUPLICATE_REUSED",
                    AuditEvent.object_type == "Proposal",
                    AuditEvent.object_id == str(proposal.proposal_id),
                    AuditEvent.actor_id == str(user_id),
                )
                .limit(1)
            )
            result["status"] = effective_status
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
                    "actionable_for_current_user": bool(
                        effective_status == "PENDING_REVIEW"
                        and proposal.proposer_id != user_id
                        and reused_by_current_user is None
                        and all(item.reviewer_id != user_id for item in approvals)
                        and self.service.can_user(
                            user_id,
                            "proposal.review",
                            proposal.account_id,
                            proposal.venue,
                        )
                    ),
                    "risk_decision": None
                    if risk is None
                    else {
                        "decision_id": str(risk.decision_id),
                        "result": risk.result,
                        "approved_quantity": str(risk.approved_quantity),
                        "risk_amount": str(risk.risk_amount),
                        "reasons": risk.reasons,
                        "data_as_of": _iso(risk.data_as_of),
                        "created_at": _iso(risk.created_at),
                        "context": {
                            "requested_quantity": risk_inputs.get("requested_quantity"),
                            "requested_risk": risk_inputs.get("requested_risk"),
                            "current_risk": risk_inputs.get("current_risk"),
                            "system_state": risk_policy.get("system_state"),
                            "max_total_risk": risk_policy.get("max_total_risk"),
                            "effective_max_total_risk": risk_capital.get(
                                "effective_max_total_risk"
                            ),
                            "fact_age_seconds": risk_inputs.get("fact_age_seconds"),
                            "max_fact_age_seconds": risk_policy.get("max_fact_age_seconds"),
                            "position_status": (
                                "MISSING"
                                if not isinstance(risk_position, dict)
                                else risk_position.get("fact_status")
                            ),
                            "equity_status": (
                                "MISSING"
                                if not isinstance(risk_equity, dict)
                                else risk_equity.get("fact_status")
                            ),
                            "managed_capital_known": risk_capital.get("known"),
                            "protection_required": risk_inputs.get("protection_required"),
                            "protection_status": (
                                "NOT_REQUIRED"
                                if not risk_inputs.get("protection_required")
                                else (
                                    "MISSING"
                                    if not isinstance(risk_protection, dict)
                                    else risk_protection.get("status")
                                )
                            ),
                            "protection_fully_covered": (
                                None
                                if not isinstance(risk_protection, dict)
                                else risk_protection.get("fully_covered")
                            ),
                        },
                    },
                    "authorization": None
                    if authorization is None
                    else {
                        "authorization_id": str(authorization.authorization_id),
                        "environment": authorization.environment,
                        "created_at": _iso(authorization.created_at),
                        "quantity_limit": str(authorization.quantity_limit),
                        "used_quantity": str(authorization.used_quantity),
                        "remaining_quantity": str(
                            max(
                                Decimal(0),
                                authorization.quantity_limit - authorization.used_quantity,
                            )
                        ),
                        "risk_limit": str(authorization.risk_limit),
                        "allowed_adds": authorization.allowed_adds,
                        "used_adds": authorization.used_adds,
                        "add_revoked_at": _iso(authorization.add_revoked_at),
                        "active": authorization.active,
                        "expires_at": _iso(authorization.expires_at),
                    },
                    "initial_entry": None
                    if campaign is None or initial_intent is None
                    else {
                        "campaign_id": str(campaign.campaign_id),
                        "campaign_status": campaign.status,
                        "intent_id": str(initial_intent.intent_id),
                        "intent_status": initial_intent.status,
                        "created_at": _iso(initial_intent.created_at),
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

    def treasury_reviewers_for_transfer(self, transfer_proposal_id: UUID) -> list[User]:
        with self.database.session_factory() as session:
            proposal = session.get(TransferProposal, transfer_proposal_id)
            if proposal is None:
                raise DomainRejected(
                    "TRANSFER_PROPOSAL_NOT_FOUND", "transfer proposal does not exist"
                )
            assignments = session.scalars(
                select(RoleAssignment).where(RoleAssignment.role == Role.TREASURY_ADMIN.value)
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

    def treasury_users(self, account_id: str, venue: str) -> list[User]:
        with self.database.session_factory() as session:
            assignments = session.scalars(
                select(RoleAssignment).where(RoleAssignment.role == Role.TREASURY_ADMIN.value)
            ).all()
            user_ids = {
                item.user_id
                for item in assignments
                if (item.account_scope is None or item.account_scope == account_id)
                and (item.venue_scope is None or item.venue_scope == venue)
            }
            users = session.scalars(
                select(User).where(User.user_id.in_(user_ids), User.active)
            ).all()
            for user in users:
                session.expunge(user)
            return list(users)

    def transfer_proposal_version(self, transfer_proposal_id: UUID) -> int:
        with self.database.session_factory() as session:
            proposal = session.get(TransferProposal, transfer_proposal_id)
            if proposal is None:
                raise DomainRejected(
                    "TRANSFER_PROPOSAL_NOT_FOUND", "transfer proposal does not exist"
                )
            return proposal.version

    @staticmethod
    def _transfer_proposal_summary(item: TransferProposal) -> dict[str, Any]:
        return {
            "transfer_proposal_id": str(item.transfer_proposal_id),
            "proposer_id": str(item.proposer_id),
            "environment": item.environment,
            "direction": item.direction,
            "purpose": item.purpose,
            "status": item.status,
            "version": item.version,
            "account_id": item.account_id,
            "venue": item.venue,
            "source_type": item.source_type,
            "source_id": item.source_id,
            "destination_type": item.destination_type,
            "destination_id": item.destination_id,
            "asset": item.asset,
            "network": item.network,
            "destination_reference": item.destination_reference,
            "amount": str(item.amount),
            "max_fee": str(item.max_fee),
            "min_received": str(item.min_received),
            "reason": item.reason,
            "frozen_at": _iso(item.frozen_at),
            "expires_at": _iso(item.expires_at),
            "created_at": _iso(item.created_at),
            "updated_at": _iso(item.updated_at),
        }

    def transfer_proposal_detail(self, user_id: UUID, transfer_proposal_id: UUID) -> dict[str, Any]:
        with self.database.session_factory() as session:
            proposal = session.get(TransferProposal, transfer_proposal_id)
            if proposal is None:
                raise DomainRejected(
                    "TRANSFER_PROPOSAL_NOT_FOUND", "transfer proposal does not exist"
                )
            if not self.service.can_user(
                user_id, "capital.view", proposal.account_id, proposal.venue
            ):
                raise DomainRejected("RBAC_DENIED", "transfer proposal is outside scope")
            approvals = session.scalars(
                select(Approval)
                .where(Approval.transfer_proposal_id == transfer_proposal_id)
                .order_by(Approval.created_at)
            ).all()
            authorization = session.scalar(
                select(TransferAuthorization).where(
                    TransferAuthorization.transfer_proposal_id == transfer_proposal_id
                )
            )
            result = self._transfer_proposal_summary(proposal)
            result.update(
                {
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
                    "authorization": None
                    if authorization is None
                    else {
                        "transfer_authorization_id": str(authorization.transfer_authorization_id),
                        "active": authorization.active,
                        "version": authorization.version,
                        "expires_at": _iso(authorization.expires_at),
                        "amount_limit": str(authorization.amount_limit),
                    },
                }
            )
            return result

    @staticmethod
    def _capital_transfer_summary(item: CapitalTransfer) -> dict[str, Any]:
        return {
            "capital_transfer_id": str(item.capital_transfer_id),
            "transfer_authorization_id": str(item.transfer_authorization_id),
            "environment": item.environment,
            "account_id": item.account_id,
            "venue": item.venue,
            "direction": item.direction,
            "source_id": item.source_id,
            "destination_id": item.destination_id,
            "asset": item.asset,
            "network": item.network,
            "status": item.status,
            "gross_amount": str(item.gross_amount),
            "reserved_amount": str(item.reserved_amount),
            "fee_amount": None if item.fee_amount is None else str(item.fee_amount),
            "net_received": None if item.net_received is None else str(item.net_received),
            "external_transfer_id": item.external_transfer_id,
            "transaction_reference": item.transaction_reference,
            "transport": item.transport,
            "chain_id": item.chain_id,
            "transport_state": item.transport_state,
            "planned_transactions": item.planned_transactions,
            "confirmed_transaction_hashes": item.confirmed_transaction_hashes,
            "protocol_request_id": item.protocol_request_id,
            "protocol_execute_after": _iso(item.protocol_execute_after),
            "protocol_expires_at": _iso(item.protocol_expires_at),
            "reconciliation_status": item.reconciliation_status,
            "reconciliation_details": item.reconciliation_details,
            "version": item.version,
            "observed_at": _iso(item.observed_at),
            "reconciled_at": _iso(item.reconciled_at),
            "updated_at": _iso(item.updated_at),
        }

    def capital_transfer_detail(self, user_id: UUID, capital_transfer_id: UUID) -> dict[str, Any]:
        with self.database.session_factory() as session:
            transfer = session.get(CapitalTransfer, capital_transfer_id)
            if transfer is None:
                raise DomainRejected("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer is missing")
            if not self.service.can_user(
                user_id, "capital.view", transfer.account_id, transfer.venue
            ):
                raise DomainRejected("RBAC_DENIED", "capital transfer is outside scope")
            return self._capital_transfer_summary(transfer)

    def capital_center(
        self,
        user_id: UUID,
        *,
        authoritative_live_accounts: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        with self.database.session_factory() as session:
            now = datetime.now(UTC)
            authoritative_accounts = {
                venue.upper(): account_id
                for venue, account_id in (authoritative_live_accounts or {}).items()
                if account_id
            }

            def is_authoritative_live_venue(
                environment: str,
                location_type: str,
                venue: str,
                account_id: str,
            ) -> bool:
                if environment != "LIVE" or location_type != "VENUE":
                    return True
                expected = authoritative_accounts.get(venue.upper())
                return expected is None or expected == account_id

            assignments = session.scalars(
                select(RoleAssignment).where(RoleAssignment.user_id == user_id)
            ).all()
            if not self.service.can_user(user_id, "capital.view"):
                raise DomainRejected("RBAC_DENIED", "capital center access is not assigned")
            treasury_assignments = [
                item
                for item in assignments
                if item.role in {Role.TREASURY_ADMIN.value, Role.SYSTEM_ADMIN.value}
            ]

            def can_view_history(item: AccountEquityObservation) -> bool:
                if not is_authoritative_live_venue(
                    item.environment,
                    item.location_type,
                    item.venue,
                    item.account_id,
                ):
                    return False
                if item.location_type == "VAULT":
                    return any(
                        assignment.account_scope is None and assignment.venue_scope is None
                        for assignment in treasury_assignments
                    )
                return any(
                    (
                        assignment.account_scope is None
                        or assignment.account_scope == item.account_id
                    )
                    and (assignment.venue_scope is None or assignment.venue_scope == item.venue)
                    for assignment in treasury_assignments
                )

            balances = session.scalars(
                select(AccountEquity).order_by(
                    AccountEquity.location_type,
                    AccountEquity.venue,
                    AccountEquity.account_id,
                )
            ).all()
            proposals = session.scalars(
                select(TransferProposal).order_by(TransferProposal.updated_at.desc())
            ).all()
            authorizations = session.scalars(select(TransferAuthorization)).all()
            authorization_by_proposal = {item.transfer_proposal_id: item for item in authorizations}
            transfers = session.scalars(
                select(CapitalTransfer).order_by(CapitalTransfer.updated_at.desc())
            ).all()
            direct_operations = session.scalars(
                select(DirectCapitalOperation).order_by(DirectCapitalOperation.updated_at.desc())
            ).all()
            policies = session.scalars(
                select(CapitalAutomationPolicy).order_by(
                    CapitalAutomationPolicy.environment,
                    CapitalAutomationPolicy.venue,
                    CapitalAutomationPolicy.account_id,
                )
            ).all()
            observations = list(
                reversed(
                    session.scalars(
                        select(AccountEquityObservation)
                        .where(AccountEquityObservation.environment == "LIVE")
                        .order_by(AccountEquityObservation.observed_at.desc())
                        .limit(5_000)
                    ).all()
                )
            )
            risk_policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
            max_fact_age = timedelta(
                seconds=(risk_policy.max_fact_age_seconds if risk_policy is not None else 300)
            )
            visible_transfers = [
                item
                for item in transfers
                if self.service.can_user(user_id, "capital.view", item.account_id, item.venue)
            ]
            occupied_statuses = {
                "SOURCE_RESERVED",
                "SUBMITTED",
                "IN_FLIGHT",
                "DESTINATION_CONFIRMED",
                "UNKNOWN",
                "MANUAL_REQUIRED",
            }
            balance_data: list[dict[str, Any]] = []
            valuation_issues: set[str] = set()
            venue_net_worth: dict[str, Decimal] = {}
            vault_net_worth = Decimal(0)
            total_net_worth = Decimal(0)
            live_balance_count = 0
            live_sources: set[str] = set()
            current_live_sources: set[str] = set()
            for item in balances:
                if not is_authoritative_live_venue(
                    item.environment,
                    item.location_type,
                    item.venue,
                    item.account_id,
                ):
                    continue
                can_view = (
                    self.service.can_user(user_id, "capital.view", item.account_id, item.venue)
                    if item.location_type == "VENUE"
                    else self.service.can_user(user_id, "capital.view")
                )
                if not can_view:
                    continue
                occupied = sum(
                    (
                        transfer.reserved_amount
                        for transfer in visible_transfers
                        if transfer.environment == item.environment
                        and transfer.source_id == item.account_id
                        and transfer.asset == item.currency
                        and transfer.status in occupied_statuses
                    ),
                    Decimal(0),
                )
                confirmed_available = (
                    item.available_balance
                    if item.withdrawable_balance is None
                    else item.withdrawable_balance
                )
                valuation_time = item.observed_at
                usd_equity: Decimal | None
                valuation_price: Decimal | None
                if item.currency.upper() in USD_STABLE_ASSETS:
                    usd_equity = item.equity
                    valuation_price = Decimal(1)
                else:
                    usd_equity = item.valuation_equity
                    valuation_price = item.valuation_price
                    if item.valuation_observed_at is not None:
                        valuation_time = min(valuation_time, item.valuation_observed_at)
                valuation_current = (
                    item.fact_status == "KNOWN"
                    and usd_equity is not None
                    and valuation_price is not None
                    and valuation_price > 0
                    and not self.service._fact_is_stale(valuation_time, now, max_fact_age)
                )
                if item.environment == "LIVE":
                    live_balance_count += 1
                    live_sources.add("VAULT" if item.location_type == "VAULT" else item.venue)
                if valuation_current and item.environment == "LIVE":
                    assert usd_equity is not None
                    current_live_sources.add(
                        "VAULT" if item.location_type == "VAULT" else item.venue
                    )
                    total_net_worth += usd_equity
                    if item.location_type == "VAULT":
                        vault_net_worth += usd_equity
                    else:
                        venue_net_worth[item.venue] = (
                            venue_net_worth.get(item.venue, Decimal(0)) + usd_equity
                        )
                elif item.environment == "LIVE":
                    source = "VAULT" if item.location_type == "VAULT" else item.venue
                    if item.fact_status != "KNOWN":
                        valuation_issues.add(f"CURRENT_VALUE_MISSING:{source}")
                    elif usd_equity is None or valuation_price is None or valuation_price <= 0:
                        valuation_issues.add(f"UNKNOWN_USD_VALUE:{source}")
                    elif self.service._fact_is_stale(valuation_time, now, max_fact_age):
                        valuation_issues.add(f"STALE_LIVE_SOURCE:{source}")
                    else:
                        valuation_issues.add(f"CURRENT_VALUE_MISSING:{source}")
                balance_data.append(
                    {
                        "account_equity_id": str(item.account_equity_id),
                        "environment": item.environment,
                        "location_type": item.location_type,
                        "location_id": item.account_id,
                        "venue": item.venue,
                        "asset": item.currency,
                        "equity": str(item.equity),
                        "confirmed_available": str(confirmed_available),
                        "source_reserved": str(occupied),
                        "effective_available": str(max(Decimal(0), confirmed_available - occupied)),
                        "control_status": item.control_status,
                        "deposit_status": item.deposit_status,
                        "network": item.network,
                        "address_reference": item.address_reference,
                        "valuation_currency": (
                            "USD"
                            if item.currency.upper() in USD_STABLE_ASSETS
                            else item.valuation_currency
                        ),
                        "valuation_price": (
                            None if valuation_price is None else str(valuation_price)
                        ),
                        "usd_equity": (None if not valuation_current else str(usd_equity)),
                        "valuation_observed_at": _iso(item.valuation_observed_at),
                        "valuation_current": valuation_current,
                        "fact_status": item.fact_status,
                        "observed_at": _iso(item.observed_at),
                    }
                )
            for required_source in ("BINANCE", "HYPERLIQUID", "VAULT"):
                if required_source not in live_sources:
                    valuation_issues.add(f"MISSING_LIVE_SOURCE:{required_source}")
                elif required_source not in current_live_sources and not any(
                    issue.endswith(f":{required_source}") for issue in valuation_issues
                ):
                    valuation_issues.add(f"CURRENT_VALUE_MISSING:{required_source}")
            gate = session.get(CapabilityGate, "CAPITAL_TRANSFER")
            automation_gates = {
                key: (None if (value := session.get(CapabilityGate, key)) is None else value.status)
                for key in ("AUTO_PROFIT_SWEEP", "AUTO_OPERATING_REFILL")
            }
            return {
                "real_transfer_gate": None if gate is None else gate.status,
                "real_transfer_reason": None if gate is None else gate.reason,
                "balances": balance_data,
                "history": [
                    {
                        "source": ("VAULT" if item.location_type == "VAULT" else item.venue),
                        "location_type": item.location_type,
                        "location_id": item.account_id,
                        "venue": item.venue,
                        "asset": item.currency,
                        "equity": str(item.equity),
                        "available_balance": str(item.available_balance),
                        "usd_equity": (None if item.usd_equity is None else str(item.usd_equity)),
                        "observed_at": _iso(item.observed_at),
                    }
                    for item in observations
                    if can_view_history(item)
                ],
                "net_worth": {
                    "environment": "LIVE",
                    "currency": "USD",
                    "venues": {
                        venue: (
                            str(venue_net_worth[venue]) if venue in current_live_sources else None
                        )
                        for venue in ("BINANCE", "HYPERLIQUID")
                    },
                    "vault": str(vault_net_worth) if "VAULT" in current_live_sources else None,
                    "total": (
                        str(total_net_worth)
                        if {"BINANCE", "HYPERLIQUID", "VAULT"}.issubset(current_live_sources)
                        and not valuation_issues
                        else None
                    ),
                    "complete": live_balance_count > 0 and not valuation_issues,
                    "issues": sorted(valuation_issues),
                    "as_of": now.isoformat(),
                },
                "in_transit": str(
                    sum(
                        (
                            item.reserved_amount
                            for item in visible_transfers
                            if item.status in occupied_statuses
                        ),
                        Decimal(0),
                    )
                ),
                "proposals": [
                    {
                        **self._transfer_proposal_summary(item),
                        "authorization": (
                            None
                            if (
                                authorization := authorization_by_proposal.get(
                                    item.transfer_proposal_id
                                )
                            )
                            is None
                            else {
                                "transfer_authorization_id": str(
                                    authorization.transfer_authorization_id
                                ),
                                "active": authorization.active,
                                "expires_at": _iso(authorization.expires_at),
                            }
                        ),
                    }
                    for item in proposals
                    if self.service.can_user(user_id, "capital.view", item.account_id, item.venue)
                ],
                "transfers": [self._capital_transfer_summary(item) for item in visible_transfers],
                "direct_operations": [
                    {
                        "operation_id": str(item.operation_id),
                        "path": item.path,
                        "status": item.status,
                        "receipt_status": item.receipt_status,
                        "account_id": item.account_id,
                        "venue": item.venue,
                        "vault_id": item.vault_id,
                        "asset": item.asset,
                        "network": item.network,
                        "amount": str(item.amount),
                        "max_fee": None if item.max_fee is None else str(item.max_fee),
                        "min_received": (
                            None if item.min_received is None else str(item.min_received)
                        ),
                        "source_reference_configured": item.source_reference is not None,
                        "destination_reference_configured": (
                            item.destination_reference is not None
                        ),
                        "stages": item.stages,
                        "blockers": item.blockers,
                        "execute_after": _iso(item.execute_after),
                        "expires_at": _iso(item.expires_at),
                        "final_confirmed_at": _iso(item.final_confirmed_at),
                        "version": item.version,
                        "updated_at": _iso(item.updated_at),
                    }
                    for item in direct_operations
                    if self.service.can_user(user_id, "capital.view", item.account_id, item.venue)
                ],
                "automation": {
                    "gates": automation_gates,
                    "policies": [
                        {
                            "policy_id": str(item.policy_id),
                            "environment": item.environment,
                            "account_id": item.account_id,
                            "venue": item.venue,
                            "vault_id": item.vault_id,
                            "asset": item.asset,
                            "network": item.network,
                            "operating_low": str(item.operating_low),
                            "operating_target": str(item.operating_target),
                            "operating_high": str(item.operating_high),
                            "vault_minimum_reserve": str(item.vault_minimum_reserve),
                            "minimum_transfer": str(item.minimum_transfer),
                            "maximum_transfer": str(item.maximum_transfer),
                            "max_fee": str(item.max_fee),
                            "active": item.active,
                            "version": item.version,
                            "updated_at": _iso(item.updated_at),
                        }
                        for item in policies
                        if self.service.can_user(
                            user_id, "capital.view", item.account_id, item.venue
                        )
                    ],
                },
            }

    def actual_results(
        self,
        user_id: UUID,
        environment: str,
        *,
        source: str | None = None,
        source_type: str | None = None,
        source_candidate_id: str | None = None,
        source_version: str | None = None,
        venue: str | None = None,
        account_id: str | None = None,
        instrument_id: UUID | None = None,
        direction: str | None = None,
        risk_tier: str | None = None,
        campaign_id: UUID | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
    ) -> dict[str, Any]:
        if environment not in {"SHADOW", "TESTNET", "LIVE"}:
            raise DomainRejected("ENVIRONMENT_INVALID", "results require an exact environment")
        if from_time is not None and to_time is not None and from_time > to_time:
            raise DomainRejected("TIME_RANGE_INVALID", "results from_time must not exceed to_time")
        with self.database.session_factory() as session:
            campaign_query = select(Campaign).where(Campaign.environment == environment)
            for field, value in (
                (Campaign.venue, venue),
                (Campaign.account_id, account_id),
                (Campaign.instrument_id, instrument_id),
                (Campaign.direction, direction),
                (Campaign.campaign_id, campaign_id),
            ):
                if value is not None:
                    campaign_query = campaign_query.where(field == value)
            if from_time is not None:
                campaign_query = campaign_query.where(Campaign.updated_at >= from_time)
            if to_time is not None:
                campaign_query = campaign_query.where(Campaign.updated_at <= to_time)
            campaigns = [
                item
                for item in session.scalars(
                    campaign_query.order_by(Campaign.updated_at, Campaign.campaign_id)
                ).all()
                if self.service.can_user(user_id, "view", item.account_id, item.venue)
            ]
            rows: list[dict[str, Any]] = []
            totals: dict[str, dict[str, Decimal]] = {}
            for campaign in campaigns:
                proposal = session.get(Proposal, campaign.proposal_id)
                proposal_source_type = (
                    None
                    if proposal is None
                    else (proposal.strategy_id if proposal.source == "SYSTEM" else "MANUAL")
                )
                if source is not None and (proposal is None or proposal.source != source):
                    continue
                if source_type is not None and proposal_source_type != source_type:
                    continue
                if source_candidate_id is not None and (
                    proposal is None or proposal.source_candidate_id != source_candidate_id
                ):
                    continue
                if source_version is not None and (
                    proposal is None or proposal.strategy_version != source_version
                ):
                    continue
                if risk_tier is not None and (proposal is None or proposal.risk_tier != risk_tier):
                    continue
                instrument = session.get(Instrument, campaign.instrument_id)
                intents = session.scalars(
                    select(OrderIntent).where(OrderIntent.campaign_id == campaign.campaign_id)
                ).all()
                intent_ids = [item.intent_id for item in intents]
                fills = (
                    session.scalars(
                        select(VenueFill)
                        .where(VenueFill.order_intent_id.in_(intent_ids))
                        .order_by(VenueFill.executed_at, VenueFill.venue_fill_id)
                    ).all()
                    if intent_ids
                    else []
                )
                funding = session.scalars(
                    select(FundingPayment).where(FundingPayment.campaign_id == campaign.campaign_id)
                ).all()
                currency = "UNKNOWN" if instrument is None else instrument.collateral_currency
                fees = sum((item.fee for item in fills), Decimal(0))
                slippage = sum((item.slippage_cost for item in fills), Decimal(0))
                funding_total = sum((item.amount for item in funding), Decimal(0))
                bucket = totals.setdefault(
                    currency,
                    {
                        "realized_pnl": Decimal(0),
                        "unrealized_pnl": Decimal(0),
                        "final_pnl": Decimal(0),
                        "fees": Decimal(0),
                        "funding": Decimal(0),
                        "slippage": Decimal(0),
                    },
                )
                bucket["realized_pnl"] += campaign.realized_pnl
                bucket["unrealized_pnl"] += campaign.unrealized_pnl
                bucket["final_pnl"] += campaign.final_pnl
                bucket["fees"] += fees
                bucket["funding"] += funding_total
                bucket["slippage"] += slippage
                rows.append(
                    {
                        "campaign_id": str(campaign.campaign_id),
                        "environment": campaign.environment,
                        "actuality": {
                            "SHADOW": "SYNTHETIC_RECORDED_FACTS",
                            "TESTNET": "NON_PRODUCTION_RECORDED_FACTS",
                            "LIVE": "LIVE_RECORDED_FACTS",
                        }[campaign.environment],
                        "status": campaign.status,
                        "account_id": campaign.account_id,
                        "venue": campaign.venue,
                        "instrument_id": str(campaign.instrument_id),
                        "symbol": None if instrument is None else instrument.symbol,
                        "currency": currency,
                        "direction": campaign.direction,
                        "source": None if proposal is None else proposal.source,
                        "source_type": proposal_source_type,
                        "source_candidate_id": (
                            None if proposal is None else proposal.source_candidate_id
                        ),
                        "source_version": (None if proposal is None else proposal.strategy_version),
                        "risk_tier": None if proposal is None else proposal.risk_tier,
                        "fill_count": len(fills),
                        "filled_quantity": str(sum((item.quantity for item in fills), Decimal(0))),
                        "realized_pnl": str(campaign.realized_pnl),
                        "unrealized_pnl": str(campaign.unrealized_pnl),
                        "final_pnl": str(campaign.final_pnl),
                        "fees": str(fees),
                        "funding": str(funding_total),
                        "slippage": str(slippage),
                        "created_at": _iso(campaign.created_at),
                        "updated_at": _iso(campaign.updated_at),
                    }
                )

            curves: dict[str, dict[str, Any]] = {}
            for currency in totals:
                cumulative = Decimal(0)
                peak = Decimal(0)
                maximum_drawdown = Decimal(0)
                points: list[dict[str, str | None]] = []
                for row in rows:
                    if row["currency"] != currency or row["status"] != "CLOSED":
                        continue
                    cumulative += Decimal(str(row["final_pnl"]))
                    peak = max(peak, cumulative)
                    drawdown = peak - cumulative
                    maximum_drawdown = max(maximum_drawdown, drawdown)
                    points.append(
                        {
                            "campaign_id": str(row["campaign_id"]),
                            "at": None if row["updated_at"] is None else str(row["updated_at"]),
                            "cumulative_pnl": str(cumulative),
                            "running_peak": str(peak),
                            "drawdown": str(drawdown),
                        }
                    )
                curves[currency] = {
                    "points": points,
                    "maximum_drawdown": str(maximum_drawdown),
                    "unit": currency,
                    "percentage_available": False,
                }
            return {
                "environment": environment,
                "filters": {
                    "source": source,
                    "source_type": source_type,
                    "source_candidate_id": source_candidate_id,
                    "source_version": source_version,
                    "venue": venue,
                    "account_id": account_id,
                    "instrument_id": None if instrument_id is None else str(instrument_id),
                    "direction": direction,
                    "risk_tier": risk_tier,
                    "campaign_id": None if campaign_id is None else str(campaign_id),
                    "from": _iso(from_time),
                    "to": _iso(to_time),
                },
                "environment_notice": {
                    "SHADOW": "Synthetic facts; not exchange execution or profit",
                    "TESTNET": "Recorded non-production facts; not live profit",
                    "LIVE": "Recorded LIVE facts; no profitability guarantee",
                }[environment],
                "campaigns": rows,
                "totals_by_currency": {
                    currency: {key: str(value) for key, value in values.items()}
                    for currency, values in totals.items()
                },
                "curves_by_currency": curves,
            }

    def audit_timeline(
        self, user_id: UUID, environment: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        if environment not in {"SHADOW", "TESTNET", "LIVE"}:
            raise DomainRejected("ENVIRONMENT_INVALID", "audit requires an exact environment")
        with self.database.session_factory() as session:
            object_ids: set[str] = set()
            proposals = [
                item
                for item in session.scalars(
                    select(Proposal).where(Proposal.environment == environment)
                ).all()
                if self.service.can_user(user_id, "view", item.account_id, item.venue)
            ]
            proposal_ids = [item.proposal_id for item in proposals]
            object_ids.update(str(item.proposal_id) for item in proposals)
            campaigns = [
                item
                for item in session.scalars(
                    select(Campaign).where(Campaign.environment == environment)
                ).all()
                if self.service.can_user(user_id, "view", item.account_id, item.venue)
            ]
            campaign_ids = [item.campaign_id for item in campaigns]
            object_ids.update(str(item.campaign_id) for item in campaigns)
            if proposal_ids:
                object_ids.update(
                    str(item.decision_id)
                    for item in session.scalars(
                        select(RiskDecision).where(RiskDecision.proposal_id.in_(proposal_ids))
                    ).all()
                )
                object_ids.update(
                    str(item.authorization_id)
                    for item in session.scalars(
                        select(TradingAuthorization).where(
                            TradingAuthorization.proposal_id.in_(proposal_ids)
                        )
                    ).all()
                )
            if campaign_ids:
                intents = session.scalars(
                    select(OrderIntent).where(OrderIntent.campaign_id.in_(campaign_ids))
                ).all()
                intent_ids = [item.intent_id for item in intents]
                object_ids.update(str(item.intent_id) for item in intents)
                object_ids.update(
                    str(item.reservation_id)
                    for item in session.scalars(
                        select(RiskReservation).where(RiskReservation.campaign_id.in_(campaign_ids))
                    ).all()
                )
                object_ids.update(
                    str(item.funding_payment_id)
                    for item in session.scalars(
                        select(FundingPayment).where(FundingPayment.campaign_id.in_(campaign_ids))
                    ).all()
                )
                object_ids.update(
                    str(item.reconciliation_id)
                    for item in session.scalars(
                        select(ReconciliationRun).where(
                            ReconciliationRun.campaign_id.in_(campaign_ids)
                        )
                    ).all()
                )
                if intent_ids:
                    object_ids.update(
                        str(item.venue_order_fact_id)
                        for item in session.scalars(
                            select(VenueOrder).where(VenueOrder.order_intent_id.in_(intent_ids))
                        ).all()
                    )
                    object_ids.update(
                        str(item.venue_fill_fact_id)
                        for item in session.scalars(
                            select(VenueFill).where(VenueFill.order_intent_id.in_(intent_ids))
                        ).all()
                    )
            transfer_proposals = [
                item
                for item in session.scalars(
                    select(TransferProposal).where(TransferProposal.environment == environment)
                ).all()
                if self.service.can_user(user_id, "capital.view", item.account_id, item.venue)
            ]
            transfer_proposal_ids = [item.transfer_proposal_id for item in transfer_proposals]
            object_ids.update(str(item) for item in transfer_proposal_ids)
            if transfer_proposal_ids:
                transfer_authorizations = session.scalars(
                    select(TransferAuthorization).where(
                        TransferAuthorization.transfer_proposal_id.in_(transfer_proposal_ids)
                    )
                ).all()
                authorization_ids = [
                    item.transfer_authorization_id for item in transfer_authorizations
                ]
                object_ids.update(str(item) for item in authorization_ids)
                if authorization_ids:
                    object_ids.update(
                        str(item.capital_transfer_id)
                        for item in session.scalars(
                            select(CapitalTransfer).where(
                                CapitalTransfer.transfer_authorization_id.in_(authorization_ids)
                            )
                        ).all()
                    )
            policies = [
                item
                for item in session.scalars(
                    select(CapitalAutomationPolicy).where(
                        CapitalAutomationPolicy.environment == environment
                    )
                ).all()
                if self.service.can_user(user_id, "capital.view", item.account_id, item.venue)
            ]
            object_ids.update(str(item.policy_id) for item in policies)
            if not object_ids:
                return []
            events = session.scalars(
                select(AuditEvent)
                .where(AuditEvent.object_id.in_(object_ids))
                .order_by(AuditEvent.created_at.desc(), AuditEvent.audit_event_id)
                .limit(limit)
            ).all()
            parsed_actor_ids = {
                item.actor_id: parsed
                for item in events
                if (parsed := _uuid_or_none(item.actor_id)) is not None
            }
            actors = {
                item.user_id: item.username
                for item in session.scalars(
                    select(User).where(User.user_id.in_(parsed_actor_ids.values()))
                ).all()
            }
            return [
                {
                    "audit_event_id": str(item.audit_event_id),
                    "actor_id": item.actor_id,
                    "actor": actors.get(parsed_actor_ids[item.actor_id], item.actor_id)
                    if item.actor_id in parsed_actor_ids
                    else item.actor_id,
                    "event_type": item.event_type,
                    "object_type": item.object_type,
                    "object_id": item.object_id,
                    "reason": item.reason,
                    "correlation_id": str(item.correlation_id),
                    "idempotency_key": item.idempotency_key,
                    "object_version": item.object_version,
                    "created_at": _iso(item.created_at),
                }
                for item in events
            ]

    def runtime_snapshot(self, user_id: UUID) -> dict[str, Any]:
        self.user_context(user_id)
        with self.database.session_factory() as session:
            gates = session.scalars(
                select(CapabilityGate).order_by(CapabilityGate.capability_key)
            ).all()
            revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            table_count = session.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name <> 'alembic_version'"
                )
            ).scalar_one()
            perptape_feed = session.get(PerptapeFeed, "BREAKOUTS")
            source_health = session.scalars(
                select(RuntimeSourceHealth).order_by(RuntimeSourceHealth.source_name)
            ).all()
            return {
                "database_ready": self.database.is_ready()[0],
                "schema_revision": revision,
                "business_table_count": int(table_count),
                "capability_gates": {
                    item.capability_key: {
                        "status": item.status,
                        "reason": item.reason,
                        "updated_at": _iso(item.updated_at),
                    }
                    for item in gates
                },
                "perptape_feed": (
                    {
                        "available": True,
                        "contract_version": perptape_feed.contract_version,
                        "candidate_count": len(perptape_feed.candidates),
                        "generated_at": _iso(perptape_feed.generated_at),
                        "fetched_at": _iso(perptape_feed.fetched_at),
                        "updated_at": _iso(perptape_feed.updated_at),
                    }
                    if perptape_feed is not None
                    else {
                        "available": False,
                        "contract_version": None,
                        "candidate_count": 0,
                        "generated_at": None,
                        "fetched_at": None,
                        "updated_at": None,
                    }
                ),
                "source_health": {
                    item.source_name: {
                        "status": item.status,
                        "items_observed": item.items_observed,
                        "error_code": item.error_code,
                        "checked_at": _iso(item.checked_at),
                        "last_success_at": _iso(item.last_success_at),
                        "retry_at": _iso(item.retry_at),
                        "consecutive_failures": item.consecutive_failures,
                    }
                    for item in source_health
                },
            }

    def runtime_source_health(self, source_name: str) -> dict[str, Any] | None:
        with self.database.session_factory() as session:
            item = session.get(RuntimeSourceHealth, source_name)
            if item is None:
                return None
            return {
                "status": item.status,
                "items_observed": item.items_observed,
                "error_code": item.error_code,
                "checked_at": _iso(item.checked_at),
                "last_success_at": _iso(item.last_success_at),
                "retry_at": _iso(item.retry_at),
                "consecutive_failures": item.consecutive_failures,
            }

    def list_campaigns(self, user_id: UUID) -> list[dict[str, Any]]:
        with self.database.session_factory() as session:
            values = session.execute(
                select(Campaign, Instrument)
                .outerjoin(Instrument, Instrument.instrument_id == Campaign.instrument_id)
                .order_by(Campaign.updated_at.desc(), Campaign.campaign_id)
            ).all()
            return [
                self._campaign_summary(campaign, instrument)
                for campaign, instrument in values
                if self.service.can_user(user_id, "view", campaign.account_id, campaign.venue)
            ]

    def campaign_detail(self, user_id: UUID, campaign_id: UUID) -> dict[str, Any]:
        with self.database.session_factory() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise DomainRejected("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            if not self.service.can_user(user_id, "view", campaign.account_id, campaign.venue):
                raise DomainRejected("RBAC_DENIED", "campaign is outside the current scope")
            instrument = session.get(Instrument, campaign.instrument_id)
            proposal = session.get(Proposal, campaign.proposal_id)
            authorization = session.get(TradingAuthorization, campaign.authorization_id)
            auto_add_gate = session.get(CapabilityGate, "AUTO_ADD")
            reservations = session.scalars(
                select(RiskReservation)
                .where(RiskReservation.campaign_id == campaign_id)
                .order_by(RiskReservation.created_at)
            ).all()
            intents = session.scalars(
                select(OrderIntent)
                .where(OrderIntent.campaign_id == campaign_id)
                .order_by(OrderIntent.created_at, OrderIntent.intent_id)
            ).all()
            intent_ids = [item.intent_id for item in intents]
            orders = (
                session.scalars(
                    select(VenueOrder).where(VenueOrder.order_intent_id.in_(intent_ids))
                ).all()
                if intent_ids
                else []
            )
            fills = session.scalars(
                select(VenueFill)
                .where(VenueFill.campaign_id == campaign_id)
                .order_by(VenueFill.executed_at, VenueFill.venue_fill_fact_id)
            ).all()
            position = session.scalar(
                select(Position).where(
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.environment == campaign.environment,
                    Position.instrument_id == campaign.instrument_id,
                )
            )
            protection = (
                session.scalar(
                    select(ProtectionOrder).where(
                        ProtectionOrder.position_id == position.position_id
                    )
                )
                if position is not None
                else None
            )
            funding = session.scalars(
                select(FundingPayment)
                .where(FundingPayment.campaign_id == campaign_id)
                .order_by(FundingPayment.paid_at)
            ).all()
            scope = (
                f"{campaign.account_id}:{campaign.venue}"
                if campaign.environment == "SHADOW"
                else f"{campaign.environment}:{campaign.account_id}:{campaign.venue}"
            )
            reconciliation = session.scalar(
                select(ReconciliationRun)
                .where(ReconciliationRun.execution_scope == scope)
                .order_by(ReconciliationRun.completed_at.desc())
                .limit(1)
            )
            lease = session.get(SenderLease, scope)
            orders_by_intent = {item.order_intent_id: item for item in orders}
            result = self._campaign_summary(campaign, instrument)
            result.update(
                {
                    "instrument": None
                    if instrument is None
                    else {
                        "symbol": instrument.symbol,
                        "collateral_currency": instrument.collateral_currency,
                    },
                    "authorization": None
                    if authorization is None
                    else {
                        "authorization_id": str(authorization.authorization_id),
                        "environment": authorization.environment,
                        "active": authorization.active,
                        "quantity_limit": str(authorization.quantity_limit),
                        "used_quantity": str(authorization.used_quantity),
                        "allowed_adds": authorization.allowed_adds,
                        "used_adds": authorization.used_adds,
                        "add_revoked_at": _iso(authorization.add_revoked_at),
                        "expires_at": _iso(authorization.expires_at),
                    },
                    "reservations": [
                        {
                            "reservation_id": str(item.reservation_id),
                            "status": item.status,
                            "amount": str(item.amount),
                            "version": item.version,
                            "created_at": _iso(item.created_at),
                            "updated_at": _iso(item.updated_at),
                        }
                        for item in reservations
                    ],
                    "intents": [
                        {
                            "intent_id": str(item.intent_id),
                            "kind": item.kind,
                            "side": item.side,
                            "quantity": str(item.quantity),
                            "limit_price": (
                                None if item.limit_price is None else str(item.limit_price)
                            ),
                            "reduce_only": item.reduce_only,
                            "trigger_source": item.trigger_source,
                            "trigger_observed_at": _iso(item.trigger_observed_at),
                            "add_unit_consumed": item.add_unit_consumed,
                            "status": item.status,
                            "version": item.version,
                            "created_at": _iso(item.created_at),
                            "updated_at": _iso(item.updated_at),
                            "order": self._order_summary(orders_by_intent.get(item.intent_id)),
                        }
                        for item in intents
                    ],
                    "fills": [
                        {
                            "fill_id": str(item.venue_fill_fact_id),
                            "venue_fill_id": item.venue_fill_id,
                            "intent_id": str(item.order_intent_id),
                            "side": item.side,
                            "quantity": str(item.quantity),
                            "price": str(item.price),
                            "fee": str(item.fee),
                            "fee_currency": item.fee_currency,
                            "slippage_cost": str(item.slippage_cost),
                            "executed_at": _iso(item.executed_at),
                        }
                        for item in fills
                    ],
                    "position": None
                    if position is None
                    else {
                        "position_id": str(position.position_id),
                        "quantity": str(position.quantity),
                        "average_entry_price": str(position.average_entry_price),
                        "mark_price": str(position.mark_price),
                        "fact_status": position.fact_status,
                        "observed_at": _iso(position.observed_at),
                    },
                    "protection": None
                    if protection is None
                    else {
                        "protection_id": str(protection.protection_id),
                        "venue_order_id": protection.venue_order_id,
                        "quantity": str(protection.quantity),
                        "trigger_price": str(protection.trigger_price),
                        "status": protection.status,
                        "fully_covered": protection.fully_covered,
                        "observed_at": _iso(protection.observed_at),
                    },
                    "funding": [
                        {
                            "venue_payment_id": item.venue_payment_id,
                            "amount": str(item.amount),
                            "currency": item.currency,
                            "paid_at": _iso(item.paid_at),
                        }
                        for item in funding
                    ],
                    "reconciliation": None
                    if reconciliation is None
                    else {
                        "reconciliation_id": str(reconciliation.reconciliation_id),
                        "status": reconciliation.status,
                        "is_computed": reconciliation.is_computed,
                        "differences": reconciliation.differences,
                        "resolution_reason": reconciliation.resolution_reason,
                        "completed_at": _iso(reconciliation.completed_at),
                    },
                    "sender_lease": None
                    if lease is None
                    else {
                        "execution_scope": lease.execution_scope,
                        "owner_id": lease.owner_id,
                        "fencing_token": lease.fencing_token,
                        "expires_at": _iso(lease.expires_at),
                    },
                    "management": {
                        "auto_add_gate": (
                            "UNKNOWN" if auto_add_gate is None else auto_add_gate.status
                        ),
                        "allow_auto_add": bool(
                            proposal is not None
                            and isinstance(proposal.frozen_payload.get("details"), dict)
                            and proposal.frozen_payload["details"].get("allow_auto_add") is True
                        ),
                        "initial_quantity": (
                            None
                            if proposal is None
                            or not isinstance(proposal.frozen_payload.get("details"), dict)
                            else proposal.frozen_payload["details"].get("initial_quantity")
                        ),
                        "add_trigger_price": (
                            None
                            if proposal is None
                            or not isinstance(proposal.frozen_payload.get("details"), dict)
                            else proposal.frozen_payload["details"].get("add_trigger_price")
                        ),
                        "requested_adds": (
                            0
                            if proposal is None
                            or not isinstance(proposal.frozen_payload.get("details"), dict)
                            else proposal.frozen_payload["details"].get("requested_adds", 0)
                        ),
                        "remaining_quantity": (
                            "0"
                            if authorization is None or not authorization.active
                            else str(authorization.quantity_limit - authorization.used_quantity)
                        ),
                        "remaining_adds": (
                            0
                            if authorization is None
                            or not authorization.active
                            or authorization.add_revoked_at is not None
                            else authorization.allowed_adds - authorization.used_adds
                        ),
                    },
                }
            )
            return result

    def campaign_id_for_intent(self, user_id: UUID, intent_id: UUID) -> UUID:
        with self.database.session_factory() as session:
            intent = session.get(OrderIntent, intent_id)
            if intent is None:
                raise DomainRejected("ORDER_INTENT_NOT_FOUND", "intent does not exist")
            campaign = session.get(Campaign, intent.campaign_id)
            if campaign is None:
                raise DomainRejected("CAMPAIGN_NOT_FOUND", "intent campaign is missing")
            if not self.service.can_user(user_id, "view", campaign.account_id, campaign.venue):
                raise DomainRejected("RBAC_DENIED", "intent is outside the current scope")
            return campaign.campaign_id

    def list_exceptions(self, user_id: UUID, *, now: datetime) -> list[dict[str, Any]]:
        exceptions: list[dict[str, Any]] = []
        with self.database.session_factory() as session:
            risk_policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
            max_fact_age = timedelta(
                seconds=(risk_policy.max_fact_age_seconds if risk_policy is not None else 300)
            )
        for campaign in self.list_campaigns(user_id):
            if campaign["status"] == "CLOSED":
                continue
            detail = self.campaign_detail(user_id, UUID(str(campaign["campaign_id"])))
            campaign_id = str(campaign["campaign_id"])
            campaign_occurred_at = str(campaign["updated_at"] or campaign["created_at"])
            if campaign["status"] == "UNKNOWN":
                exceptions.append(
                    self._exception(
                        campaign_id,
                        "CAMPAIGN_UNKNOWN",
                        "BLOCKING",
                        occurred_at=campaign_occurred_at,
                    )
                )
            for reservation in detail["reservations"]:
                if reservation["status"] == "UNKNOWN":
                    exceptions.append(
                        self._exception(
                            campaign_id,
                            "RISK_RESERVATION_UNKNOWN",
                            "BLOCKING",
                            object_id=str(reservation["reservation_id"]),
                            occurred_at=str(reservation["updated_at"]),
                        )
                    )
            for intent in detail["intents"]:
                if intent["status"] == "UNKNOWN":
                    exceptions.append(
                        self._exception(
                            campaign_id,
                            "ORDER_INTENT_UNKNOWN",
                            "BLOCKING",
                            object_id=str(intent["intent_id"]),
                            occurred_at=str(intent["updated_at"]),
                        )
                    )
            position = detail["position"]
            if position is None or position["fact_status"] == "UNKNOWN":
                exceptions.append(
                    self._exception(
                        campaign_id,
                        "POSITION_UNKNOWN",
                        "BLOCKING",
                        occurred_at=campaign_occurred_at,
                    )
                )
            else:
                position_observed_at = datetime.fromisoformat(str(position["observed_at"]))
                if self.service._fact_is_stale(position_observed_at, now, max_fact_age):
                    exceptions.append(
                        self._exception(
                            campaign_id,
                            "POSITION_STALE",
                            "BLOCKING",
                            object_id=str(position["position_id"]),
                            occurred_at=(position_observed_at + max_fact_age).isoformat(),
                            details=[
                                f"observed_at={position['observed_at']}",
                                f"max_age_seconds={int(max_fact_age.total_seconds())}",
                            ],
                        )
                    )
            if (
                position is not None
                and position["fact_status"] != "UNKNOWN"
                and Decimal(str(position["quantity"])) != 0
            ):
                protection = detail["protection"]
                if protection is None or protection["status"] == "UNKNOWN":
                    exceptions.append(
                        self._exception(
                            campaign_id,
                            "PROTECTION_UNKNOWN",
                            "BLOCKING",
                            occurred_at=str(position["observed_at"]),
                        )
                    )
                else:
                    protection_observed_at = datetime.fromisoformat(str(protection["observed_at"]))
                    if self.service._fact_is_stale(protection_observed_at, now, max_fact_age):
                        exceptions.append(
                            self._exception(
                                campaign_id,
                                "PROTECTION_STALE",
                                "BLOCKING",
                                object_id=str(protection["protection_id"]),
                                occurred_at=(protection_observed_at + max_fact_age).isoformat(),
                                details=[
                                    f"observed_at={protection['observed_at']}",
                                    f"max_age_seconds={int(max_fact_age.total_seconds())}",
                                ],
                            )
                        )
                    if not protection["fully_covered"]:
                        exceptions.append(
                            self._exception(
                                campaign_id,
                                "PROTECTION_INSUFFICIENT",
                                "BLOCKING",
                                object_id=str(protection["protection_id"]),
                                occurred_at=str(protection["observed_at"]),
                            )
                        )
            reconciliation = detail["reconciliation"]
            if reconciliation is not None and reconciliation["status"] != "MATCH":
                exceptions.append(
                    self._exception(
                        campaign_id,
                        f"RECONCILIATION_{reconciliation['status']}",
                        "BLOCKING",
                        occurred_at=str(reconciliation["completed_at"]),
                        details=list(reconciliation["differences"]),
                    )
                )
            elif reconciliation is not None:
                completed_at = datetime.fromisoformat(str(reconciliation["completed_at"]))
                newer_facts: list[tuple[str, datetime]] = []
                if (
                    position is not None
                    and datetime.fromisoformat(str(position["observed_at"])) > completed_at
                ):
                    newer_facts.append(
                        (
                            "POSITION_FACT_NEWER",
                            datetime.fromisoformat(str(position["observed_at"])),
                        )
                    )
                if (
                    detail["intents"]
                    and datetime.fromisoformat(str(detail["intents"][-1]["updated_at"]))
                    > completed_at
                ):
                    newer_facts.append(
                        (
                            "ORDER_INTENT_NEWER",
                            datetime.fromisoformat(str(detail["intents"][-1]["updated_at"])),
                        )
                    )
                if newer_facts:
                    exceptions.append(
                        self._exception(
                            campaign_id,
                            "RECONCILIATION_STALE",
                            "BLOCKING",
                            object_id=str(reconciliation["reconciliation_id"]),
                            occurred_at=min(value for _name, value in newer_facts).isoformat(),
                            details=[name for name, _value in newer_facts],
                        )
                    )
        guidance = {
            "CAMPAIGN_UNKNOWN": (
                "CRITICAL",
                "交易运维",
                "任务结果未知会阻断新增风险",
                "核对交易所事实并完成计算型对账",
            ),
            "RISK_RESERVATION_UNKNOWN": (
                "CRITICAL",
                "交易运维",
                "风险占用无法安全释放",
                "核对订单结果并重新计算风险预留",
            ),
            "ORDER_INTENT_UNKNOWN": (
                "CRITICAL",
                "交易运维",
                "可能存在未确认订单结果",
                "先同步订单与成交, 再处理未知意图",
            ),
            "POSITION_UNKNOWN": (
                "CRITICAL",
                "交易运维",
                "无法确认真实仓位",
                "同步该账户与标的的仓位事实",
            ),
            "POSITION_STALE": (
                "HIGH",
                "交易运维",
                "仓位事实可能不再代表当前状态",
                "刷新仓位后重新对账",
            ),
            "PROTECTION_UNKNOWN": (
                "CRITICAL",
                "交易运维",
                "无法确认持仓保护是否有效",
                "同步或补齐保护单事实",
            ),
            "PROTECTION_STALE": (
                "HIGH",
                "交易运维",
                "保护事实可能已经变化",
                "刷新保护单并确认足额覆盖",
            ),
            "PROTECTION_INSUFFICIENT": (
                "CRITICAL",
                "交易运维",
                "现有持仓未被足额保护",
                "按交易任务允许的降险路径补齐保护或退出",
            ),
            "RECONCILIATION_STALE": (
                "HIGH",
                "交易运维",
                "旧对账早于最新仓位或订单事实",
                "用最新事实重新运行计算型对账",
            ),
        }
        for item in exceptions:
            code = str(item["code"])
            default = ("HIGH", "交易运维", "运行事实存在不一致", "检查差异并重新运行计算型对账")
            severity, owner_role, impact, next_action = guidance.get(code, default)
            item.update(
                {
                    "severity": severity,
                    "last_checked_at": now.isoformat(),
                    "impact": impact,
                    "owner_role": owner_role,
                    "next_action": next_action,
                    "action_available": False,
                    "action_unavailable_reason": (
                        "告警详情只提供事实与路径; 必须回到受影响交易任务按后端安全条件处理"
                    ),
                }
            )
        return exceptions

    def venue_facts(
        self,
        user_id: UUID,
        account_id: str,
        venue: str,
        environment: str,
    ) -> dict[str, Any]:
        if not self.service.can_user(user_id, "view", account_id, venue):
            raise DomainRejected("RBAC_DENIED", "venue facts are outside the current scope")
        with self.database.session_factory() as session:
            instruments = session.scalars(
                select(Instrument).where(Instrument.venue == venue).order_by(Instrument.symbol)
            ).all()
            instrument_by_id = {item.instrument_id: item for item in instruments}
            positions = session.scalars(
                select(Position).where(
                    Position.account_id == account_id,
                    Position.venue == venue,
                    Position.environment == environment,
                )
            ).all()
            protections = (
                session.scalars(
                    select(ProtectionOrder).where(
                        ProtectionOrder.position_id.in_([item.position_id for item in positions])
                    )
                ).all()
                if positions
                else []
            )
            protection_by_position = {item.position_id: item for item in protections}
            orders = session.scalars(
                select(VenueOrder)
                .where(
                    VenueOrder.account_id == account_id,
                    VenueOrder.venue == venue,
                    VenueOrder.environment == environment,
                )
                .order_by(VenueOrder.observed_at.desc())
            ).all()
            fills = session.scalars(
                select(VenueFill)
                .where(
                    VenueFill.account_id == account_id,
                    VenueFill.venue == venue,
                    VenueFill.environment == environment,
                )
                .order_by(VenueFill.executed_at.desc())
            ).all()
            funding = session.scalars(
                select(FundingPayment)
                .where(
                    FundingPayment.account_id == account_id,
                    FundingPayment.venue == venue,
                    FundingPayment.environment == environment,
                )
                .order_by(FundingPayment.paid_at.desc())
            ).all()
            equity = session.scalar(
                select(AccountEquity).where(
                    AccountEquity.account_id == account_id,
                    AccountEquity.venue == venue,
                    AccountEquity.environment == environment,
                )
            )
            execution_scope = f"{environment}:{account_id}:{venue}"
            reconciliation = session.scalar(
                select(ReconciliationRun)
                .where(ReconciliationRun.execution_scope == execution_scope)
                .order_by(ReconciliationRun.completed_at.desc())
            )
            return {
                "account_id": account_id,
                "venue": venue,
                "environment": environment,
                "instruments": [
                    {
                        "instrument_id": str(item.instrument_id),
                        "symbol": item.symbol,
                        "tick_size": str(item.tick_size),
                        "lot_size": str(item.lot_size),
                        "minimum_notional": str(item.minimum_notional),
                        "active": item.active,
                        "updated_at": _iso(item.updated_at),
                    }
                    for item in instruments
                ],
                "positions": [
                    {
                        "position_id": str(item.position_id),
                        "instrument_id": str(item.instrument_id),
                        "symbol": instrument_by_id[item.instrument_id].symbol,
                        "quantity": str(item.quantity),
                        "average_entry_price": str(item.average_entry_price),
                        "mark_price": str(item.mark_price),
                        "fact_status": item.fact_status,
                        "observed_at": _iso(item.observed_at),
                        "protection": (
                            None
                            if item.position_id not in protection_by_position
                            else {
                                "venue_order_id": protection_by_position[
                                    item.position_id
                                ].venue_order_id,
                                "quantity": str(protection_by_position[item.position_id].quantity),
                                "trigger_price": str(
                                    protection_by_position[item.position_id].trigger_price
                                ),
                                "status": protection_by_position[item.position_id].status,
                                "fully_covered": protection_by_position[
                                    item.position_id
                                ].fully_covered,
                                "observed_at": _iso(
                                    protection_by_position[item.position_id].observed_at
                                ),
                            }
                        ),
                    }
                    for item in positions
                ],
                "orders": [
                    {
                        "venue_order_id": item.venue_order_id,
                        "client_order_id": item.client_order_id,
                        "instrument_id": str(item.instrument_id),
                        "symbol": instrument_by_id[item.instrument_id].symbol,
                        "intent_id": (
                            None if item.order_intent_id is None else str(item.order_intent_id)
                        ),
                        "status": item.status,
                        "side": item.side,
                        "order_type": item.order_type,
                        "reduce_only": item.reduce_only,
                        "ordered_quantity": str(item.ordered_quantity),
                        "filled_quantity": str(item.filled_quantity),
                        "observed_at": _iso(item.observed_at),
                    }
                    for item in orders
                ],
                "fills": [
                    {
                        "venue_fill_id": item.venue_fill_id,
                        "instrument_id": str(item.instrument_id),
                        "symbol": instrument_by_id[item.instrument_id].symbol,
                        "intent_id": (
                            None if item.order_intent_id is None else str(item.order_intent_id)
                        ),
                        "side": item.side,
                        "quantity": str(item.quantity),
                        "price": str(item.price),
                        "fee": str(item.fee),
                        "fee_currency": item.fee_currency,
                        "executed_at": _iso(item.executed_at),
                    }
                    for item in fills
                ],
                "funding": [
                    {
                        "venue_payment_id": item.venue_payment_id,
                        "instrument_id": str(item.instrument_id),
                        "symbol": instrument_by_id[item.instrument_id].symbol,
                        "amount": str(item.amount),
                        "currency": item.currency,
                        "paid_at": _iso(item.paid_at),
                    }
                    for item in funding
                ],
                "equity": None
                if equity is None
                else {
                    "equity": str(equity.equity),
                    "available_balance": str(equity.available_balance),
                    "currency": equity.currency,
                    "fact_status": equity.fact_status,
                    "observed_at": _iso(equity.observed_at),
                },
                "reconciliation": None
                if reconciliation is None
                else {
                    "reconciliation_id": str(reconciliation.reconciliation_id),
                    "status": reconciliation.status,
                    "is_computed": reconciliation.is_computed,
                    "differences": reconciliation.differences,
                    "completed_at": _iso(reconciliation.completed_at),
                },
            }

    @staticmethod
    def _exception(
        campaign_id: str,
        code: str,
        severity: str,
        *,
        object_id: str | None = None,
        details: list[str] | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        return {
            "campaign_id": campaign_id,
            "object_id": object_id or campaign_id,
            "code": code,
            "severity": severity,
            "details": details or [],
            "occurred_at": occurred_at,
        }

    @staticmethod
    def _order_summary(order: VenueOrder | None) -> dict[str, Any] | None:
        if order is None:
            return None
        return {
            "venue_order_fact_id": str(order.venue_order_fact_id),
            "venue_order_id": order.venue_order_id,
            "client_order_id": order.client_order_id,
            "status": order.status,
            "side": order.side,
            "order_type": order.order_type,
            "reduce_only": order.reduce_only,
            "ordered_quantity": str(order.ordered_quantity),
            "filled_quantity": str(order.filled_quantity),
            "observed_at": _iso(order.observed_at),
        }

    @staticmethod
    def _campaign_summary(
        campaign: Campaign, instrument: Instrument | None = None
    ) -> dict[str, Any]:
        return {
            "campaign_id": str(campaign.campaign_id),
            "proposal_id": str(campaign.proposal_id),
            "authorization_id": str(campaign.authorization_id),
            "account_id": campaign.account_id,
            "venue": campaign.venue,
            "environment": campaign.environment,
            "instrument_id": str(campaign.instrument_id),
            "symbol": None if instrument is None else instrument.symbol,
            "collateral_currency": (None if instrument is None else instrument.collateral_currency),
            "direction": campaign.direction,
            "status": campaign.status,
            "current_target_quantity": str(campaign.current_target_quantity),
            "target_version": campaign.target_version,
            "target_reason": campaign.target_reason,
            "target_urgency": campaign.target_urgency,
            "target_calculated_at": _iso(campaign.target_calculated_at),
            "realized_pnl": str(campaign.realized_pnl),
            "unrealized_pnl": str(campaign.unrealized_pnl),
            "final_pnl": str(campaign.final_pnl),
            "created_at": _iso(campaign.created_at),
            "updated_at": _iso(campaign.updated_at),
        }

    @staticmethod
    def _proposal_summary(
        proposal: Proposal, instrument: Instrument | None = None
    ) -> dict[str, Any]:
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
            "symbol": None if instrument is None else instrument.symbol,
            "quote_currency": None if instrument is None else instrument.quote_currency,
            "collateral_currency": (None if instrument is None else instrument.collateral_currency),
            "direction": proposal.direction,
            "quantity": str(proposal.quantity),
            "max_risk": str(proposal.max_risk),
            "expires_at": _iso(proposal.expires_at),
            "created_at": _iso(proposal.created_at),
            "updated_at": _iso(proposal.updated_at),
        }
