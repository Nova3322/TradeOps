from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select

from trading_control_plane import domain, models
from trading_control_plane.query_component import QueryComponent, iso_datetime
from trading_control_plane.query_domains.execution import proposal_summary


def effective_proposal_status(proposal: models.Proposal, now: datetime) -> str:
    if proposal.status in {"DRAFT", "PENDING_REVIEW"} and proposal.expires_at <= now:
        return "EXPIRED"
    return proposal.status


def proposal_execution_status(
    proposal: models.Proposal,
    now: datetime,
    campaign_id: UUID | None,
) -> str | None:
    if proposal.status != "APPROVED":
        return None
    if campaign_id is not None:
        return "TRADE_CREATED"
    if proposal.expires_at <= now:
        return "WINDOW_EXPIRED"
    return "AWAITING_LAUNCH"


class ProposalQueries(QueryComponent):
    def list_proposals(
        self,
        user_id: UUID,
        *,
        status: str | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        current_time = now or datetime.now(UTC)
        workspace_id, team_id = self.active_scope_ids(user_id)
        with self.database.session_factory() as session:
            statement = (
                select(models.Proposal, models.Instrument)
                .join(
                    models.Instrument,
                    models.Instrument.instrument_id == models.Proposal.instrument_id,
                )
                .where(models.Proposal.team_id == team_id)
                .order_by(models.Proposal.created_at.desc())
            )
            if status in {"DRAFT", "PENDING_REVIEW"}:
                statement = statement.where(
                    models.Proposal.status == status,
                    models.Proposal.expires_at > current_time,
                )
            elif status == "EXPIRED":
                statement = statement.where(
                    or_(
                        models.Proposal.status == "EXPIRED",
                        and_(
                            models.Proposal.status.in_(["DRAFT", "PENDING_REVIEW"]),
                            models.Proposal.expires_at <= current_time,
                        ),
                    )
                )
            elif status is not None:
                statement = statement.where(models.Proposal.status == status)
            values = session.execute(statement).all()
            proposal_ids = [proposal.proposal_id for proposal, _instrument in values]
            proposer_names = {
                item.user_id: item.username
                for item in session.scalars(
                    select(models.User).where(
                        models.User.user_id.in_({proposal.proposer_id for proposal, _ in values})
                    )
                ).all()
            }
            reviewed_proposal_ids = set(
                session.scalars(
                    select(models.Approval.proposal_id).where(
                        models.Approval.reviewer_id == user_id,
                        models.Approval.proposal_id.in_(proposal_ids),
                    )
                )
            )
            reused_proposal_ids = set(
                session.scalars(
                    select(models.AuditEvent.object_id).where(
                        models.AuditEvent.event_type == "PROPOSAL_DUPLICATE_REUSED",
                        models.AuditEvent.object_type == "Proposal",
                        models.AuditEvent.actor_id == str(user_id),
                        models.AuditEvent.team_id == team_id,
                    )
                )
            )
            approval_counts: dict[UUID, int] = {
                proposal_id: int(count)
                for proposal_id, count in session.execute(
                    select(models.Approval.proposal_id, func.count(models.Approval.approval_id))
                    .where(models.Approval.proposal_id.in_(proposal_ids))
                    .group_by(models.Approval.proposal_id)
                ).all()
                if proposal_id is not None
            }
            campaign_by_proposal: dict[UUID, UUID] = {
                proposal_id: campaign_id
                for proposal_id, campaign_id in session.execute(
                    select(models.Campaign.proposal_id, models.Campaign.campaign_id).where(
                        models.Campaign.team_id == team_id,
                        models.Campaign.proposal_id.in_(proposal_ids),
                    )
                ).all()
            }
            result: list[dict[str, Any]] = []
            for proposal, instrument in values:
                if not self.can_user(user_id, "view", proposal.account_id, proposal.venue):
                    continue
                effective_status = effective_proposal_status(proposal, current_time)
                summary = proposal_summary(proposal, instrument)
                summary["workspace_id"] = str(workspace_id)
                summary["proposer_username"] = proposer_names.get(proposal.proposer_id)
                summary["status"] = effective_status
                summary["approval_count"] = int(approval_counts.get(proposal.proposal_id, 0))
                summary["required_approvals"] = 2 if proposal.risk_tier == "HIGH" else 1
                campaign_id = campaign_by_proposal.get(proposal.proposal_id)
                summary["campaign_id"] = None if campaign_id is None else str(campaign_id)
                summary["execution_status"] = proposal_execution_status(
                    proposal,
                    current_time,
                    campaign_id,
                )
                summary["actionable_for_current_user"] = bool(
                    effective_status == "PENDING_REVIEW"
                    and proposal.proposer_id != user_id
                    and str(proposal.proposal_id) not in reused_proposal_ids
                    and proposal.proposal_id not in reviewed_proposal_ids
                    and self.can_user(
                        user_id,
                        "proposal.review",
                        proposal.account_id,
                        proposal.venue,
                    )
                )
                result.append(summary)
            return result

    def active_perptape_system_proposals(
        self,
        user_id: UUID,
        *,
        now: datetime,
    ) -> list[dict[str, Any]]:
        """Return the current visible Perptape proposal occupying each trading scope."""

        workspace_id, team_id = self.active_scope_ids(user_id)
        with self.database.session_factory() as session:
            values = session.execute(
                select(models.Proposal, models.Instrument)
                .join(
                    models.Instrument,
                    models.Instrument.instrument_id == models.Proposal.instrument_id,
                )
                .where(
                    models.Proposal.source == "SYSTEM",
                    models.Proposal.team_id == team_id,
                    models.Proposal.strategy_id.in_(("perptape", "perptape-resonance")),
                    models.Proposal.environment == "LIVE",
                    models.Proposal.status.in_(("DRAFT", "PENDING_REVIEW")),
                    models.Proposal.expires_at > now,
                )
                .order_by(models.Proposal.created_at, models.Proposal.proposal_id)
            ).all()
            grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
            for proposal, instrument in values:
                if not self.can_user(
                    user_id,
                    "view",
                    proposal.account_id,
                    proposal.venue,
                ):
                    continue
                key = (proposal.venue, instrument.symbol, proposal.direction)
                current = grouped.get(key)
                if current is None:
                    grouped[key] = {
                        "proposal_id": str(proposal.proposal_id),
                        "workspace_id": str(workspace_id),
                        "team_id": str(proposal.team_id),
                        "status": effective_proposal_status(proposal, now),
                        "venue": proposal.venue,
                        "symbol": instrument.symbol,
                        "direction": proposal.direction,
                        "expires_at": iso_datetime(proposal.expires_at),
                        "source_observed_at": iso_datetime(proposal.source_observed_at),
                        "active_count": 1,
                    }
                else:
                    current["active_count"] += 1
            return list(grouped.values())

    def proposal_detail(
        self,
        user_id: UUID,
        proposal_id: UUID,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current_time = now or datetime.now(UTC)
        workspace_id, team_id = self.active_scope_ids(user_id)
        with self.database.session_factory() as session:
            proposal = session.get(models.Proposal, proposal_id)
            if proposal is None:
                raise domain.DomainRejected("PROPOSAL_NOT_FOUND", "proposal does not exist")
            if proposal.team_id != team_id:
                raise domain.DomainRejected("TEAM_SCOPE_DENIED", "proposal is outside active team")
            if not self.can_user(user_id, "view", proposal.account_id, proposal.venue):
                raise domain.DomainRejected("RBAC_DENIED", "proposal is outside the current scope")
            approvals = session.scalars(
                select(models.Approval)
                .where(models.Approval.proposal_id == proposal_id)
                .order_by(models.Approval.created_at)
            ).all()
            proposal_users = {
                item.user_id: item.username
                for item in session.scalars(
                    select(models.User).where(
                        models.User.user_id.in_(
                            {proposal.proposer_id, *(item.reviewer_id for item in approvals)}
                        )
                    )
                ).all()
            }
            risk = session.scalar(
                select(models.RiskDecision)
                .where(models.RiskDecision.proposal_id == proposal_id)
                .order_by(models.RiskDecision.created_at.desc())
                .limit(1)
            )
            authorization = session.scalar(
                select(models.TradingAuthorization)
                .where(models.TradingAuthorization.proposal_id == proposal_id)
                .order_by(models.TradingAuthorization.created_at.desc())
                .limit(1)
            )
            campaign = session.scalar(
                select(models.Campaign)
                .where(models.Campaign.proposal_id == proposal_id)
                .order_by(models.Campaign.created_at.desc())
                .limit(1)
            )
            initial_intent = (
                None
                if campaign is None
                else session.scalar(
                    select(models.OrderIntent)
                    .where(
                        models.OrderIntent.campaign_id == campaign.campaign_id,
                        models.OrderIntent.kind == "INITIAL",
                    )
                    .order_by(models.OrderIntent.created_at.desc())
                    .limit(1)
                )
            )
            risk_inputs = risk.input_data if risk is not None else {}
            risk_policy = risk_inputs.get("policy", {})
            risk_position = risk_inputs.get("position")
            risk_equity = risk_inputs.get("equity")
            risk_capital = risk_inputs.get("managed_capital", {})
            risk_protection = risk_inputs.get("protection")
            result = proposal_summary(
                proposal, session.get(models.Instrument, proposal.instrument_id)
            )
            result["workspace_id"] = str(workspace_id)
            effective_status = effective_proposal_status(proposal, current_time)
            reused_by_current_user = session.scalar(
                select(models.AuditEvent.audit_event_id)
                .where(
                    models.AuditEvent.event_type == "PROPOSAL_DUPLICATE_REUSED",
                    models.AuditEvent.object_type == "Proposal",
                    models.AuditEvent.object_id == str(proposal.proposal_id),
                    models.AuditEvent.actor_id == str(user_id),
                )
                .limit(1)
            )
            result["status"] = effective_status
            result["execution_status"] = proposal_execution_status(
                proposal,
                current_time,
                None if campaign is None else campaign.campaign_id,
            )
            result["proposer_username"] = proposal_users.get(proposal.proposer_id)
            result.update(
                {
                    "frozen_payload": proposal.frozen_payload,
                    "semantic_hash": proposal.semantic_hash,
                    "frozen_at": iso_datetime(proposal.frozen_at),
                    "correlation_id": str(proposal.correlation_id),
                    "approvals": [
                        {
                            "approval_id": str(item.approval_id),
                            "workspace_id": str(workspace_id),
                            "team_id": str(proposal.team_id),
                            "account_id": proposal.account_id,
                            "reviewer_id": str(item.reviewer_id),
                            "reviewer_username": proposal_users.get(item.reviewer_id),
                            "decision": item.decision,
                            "reason": item.reason,
                            "created_at": iso_datetime(item.created_at),
                        }
                        for item in approvals
                    ],
                    "actionable_for_current_user": bool(
                        effective_status == "PENDING_REVIEW"
                        and proposal.proposer_id != user_id
                        and reused_by_current_user is None
                        and all(item.reviewer_id != user_id for item in approvals)
                        and self.can_user(
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
                        "workspace_id": str(workspace_id),
                        "team_id": str(risk.team_id),
                        "account_id": proposal.account_id,
                        "result": risk.result,
                        "approved_quantity": str(risk.approved_quantity),
                        "leverage": None if risk.leverage is None else str(risk.leverage),
                        "risk_amount": str(risk.risk_amount),
                        "reasons": risk.reasons,
                        "data_as_of": iso_datetime(risk.data_as_of),
                        "created_at": iso_datetime(risk.created_at),
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
                        "workspace_id": str(workspace_id),
                        "team_id": str(authorization.team_id),
                        "account_id": authorization.account_id,
                        "environment": authorization.environment,
                        "created_at": iso_datetime(authorization.created_at),
                        "quantity_limit": str(authorization.quantity_limit),
                        "leverage": (
                            None if authorization.leverage is None else str(authorization.leverage)
                        ),
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
                        "add_revoked_at": iso_datetime(authorization.add_revoked_at),
                        "active": authorization.active,
                        "expires_at": iso_datetime(authorization.expires_at),
                    },
                    "initial_entry": None
                    if campaign is None or initial_intent is None
                    else {
                        "campaign_id": str(campaign.campaign_id),
                        "workspace_id": str(workspace_id),
                        "team_id": str(campaign.team_id),
                        "account_id": campaign.account_id,
                        "campaign_status": campaign.status,
                        "intent_id": str(initial_intent.intent_id),
                        "intent_status": initial_intent.status,
                        "leverage": (
                            None
                            if initial_intent.leverage is None
                            else str(initial_intent.leverage)
                        ),
                        "created_at": iso_datetime(initial_intent.created_at),
                    },
                }
            )
            return result

    def proposal_version(self, proposal_id: UUID) -> int:
        with self.database.session_factory() as session:
            proposal = session.get(models.Proposal, proposal_id)
            if proposal is None:
                raise domain.DomainRejected("PROPOSAL_NOT_FOUND", "proposal does not exist")
            return proposal.version

    def reviewers_for_proposal(self, proposal_id: UUID) -> list[models.User]:
        with self.database.session_factory() as session:
            proposal = session.get(models.Proposal, proposal_id)
            if proposal is None:
                raise domain.DomainRejected("PROPOSAL_NOT_FOUND", "proposal does not exist")
            reviewed_user_ids = set(
                session.scalars(
                    select(models.Approval.reviewer_id).where(
                        models.Approval.proposal_id == proposal_id
                    )
                ).all()
            )
            reused_actor_ids = set(
                session.scalars(
                    select(models.AuditEvent.actor_id).where(
                        models.AuditEvent.event_type == "PROPOSAL_DUPLICATE_REUSED",
                        models.AuditEvent.object_type == "Proposal",
                        models.AuditEvent.object_id == str(proposal_id),
                    )
                ).all()
            )
            assignments = session.scalars(
                select(models.RoleAssignment)
                .join(
                    models.TeamMembership,
                    and_(
                        models.TeamMembership.team_id == models.RoleAssignment.team_id,
                        models.TeamMembership.user_id == models.RoleAssignment.user_id,
                    ),
                )
                .where(
                    models.RoleAssignment.team_id == proposal.team_id,
                    models.RoleAssignment.role == domain.Role.REVIEWER.value,
                    models.TeamMembership.active,
                )
            ).all()
            reviewer_ids = {
                item.user_id
                for item in assignments
                if (item.account_scope is None or item.account_scope == proposal.account_id)
                and (item.venue_scope is None or item.venue_scope == proposal.venue)
                and item.user_id != proposal.proposer_id
                and item.user_id not in reviewed_user_ids
                and str(item.user_id) not in reused_actor_ids
            }
            users = session.scalars(
                select(models.User).where(models.User.user_id.in_(reviewer_ids), models.User.active)
            ).all()
            for user in users:
                session.expunge(user)
            return list(users)

    def proposal_uses_notification_routes(self, proposal_id: UUID) -> bool:
        """A configured team route is authoritative for proactive proposal notifications."""

        with self.database.session_factory() as session:
            team_id = session.scalar(
                select(models.Proposal.team_id).where(models.Proposal.proposal_id == proposal_id)
            )
            if team_id is None:
                raise domain.DomainRejected("PROPOSAL_NOT_FOUND", "proposal does not exist")
            route_id = session.scalar(
                select(models.NotificationRoute.notification_route_id)
                .where(
                    models.NotificationRoute.team_id == team_id,
                    models.NotificationRoute.deleted_at.is_(None),
                )
                .limit(1)
            )
            return route_id is not None
