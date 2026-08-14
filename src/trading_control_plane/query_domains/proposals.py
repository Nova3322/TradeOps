from __future__ import annotations

from trading_control_plane.query_component import QueryComponent

# ruff: noqa: F403, F405
from trading_control_plane.query_core import *


class ProposalQueries(QueryComponent):
    def list_proposals(
        self,
        user_id: UUID,
        *,
        status: str | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        current_time = now or datetime.now(UTC)
        workspace_id, team_id = self._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            statement = (
                select(Proposal, Instrument)
                .join(Instrument, Instrument.instrument_id == Proposal.instrument_id)
                .where(Proposal.team_id == team_id)
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
            proposal_ids = [proposal.proposal_id for proposal, _instrument in values]
            proposer_names = {
                item.user_id: item.username
                for item in session.scalars(
                    select(User).where(
                        User.user_id.in_({proposal.proposer_id for proposal, _ in values})
                    )
                ).all()
            }
            reviewed_proposal_ids = set(
                session.scalars(
                    select(Approval.proposal_id).where(
                        Approval.reviewer_id == user_id,
                        Approval.proposal_id.in_(proposal_ids),
                    )
                )
            )
            reused_proposal_ids = set(
                session.scalars(
                    select(AuditEvent.object_id).where(
                        AuditEvent.event_type == "PROPOSAL_DUPLICATE_REUSED",
                        AuditEvent.object_type == "Proposal",
                        AuditEvent.actor_id == str(user_id),
                        AuditEvent.team_id == team_id,
                    )
                )
            )
            approval_counts: dict[UUID, int] = {
                proposal_id: int(count)
                for proposal_id, count in session.execute(
                    select(Approval.proposal_id, func.count(Approval.approval_id))
                    .where(Approval.proposal_id.in_(proposal_ids))
                    .group_by(Approval.proposal_id)
                ).all()
                if proposal_id is not None
            }
            campaign_by_proposal: dict[UUID, UUID] = {
                proposal_id: campaign_id
                for proposal_id, campaign_id in session.execute(
                    select(Campaign.proposal_id, Campaign.campaign_id).where(
                        Campaign.team_id == team_id,
                        Campaign.proposal_id.in_(proposal_ids),
                    )
                ).all()
            }
            result: list[dict[str, Any]] = []
            for proposal, instrument in values:
                if not self.service.can_user(user_id, "view", proposal.account_id, proposal.venue):
                    continue
                effective_status = _effective_proposal_status(proposal, current_time)
                summary = self._proposal_summary(proposal, instrument)
                summary["workspace_id"] = str(workspace_id)
                summary["proposer_username"] = proposer_names.get(proposal.proposer_id)
                summary["status"] = effective_status
                summary["approval_count"] = int(approval_counts.get(proposal.proposal_id, 0))
                summary["required_approvals"] = 2 if proposal.risk_tier == "HIGH" else 1
                campaign_id = campaign_by_proposal.get(proposal.proposal_id)
                summary["campaign_id"] = None if campaign_id is None else str(campaign_id)
                summary["execution_status"] = _proposal_execution_status(
                    proposal,
                    current_time,
                    campaign_id,
                )
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

    def active_perptape_system_proposals(
        self,
        user_id: UUID,
        *,
        now: datetime,
    ) -> list[dict[str, Any]]:
        """Return the current visible Perptape proposal occupying each trading scope."""

        workspace_id, team_id = self._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            values = session.execute(
                select(Proposal, Instrument)
                .join(Instrument, Instrument.instrument_id == Proposal.instrument_id)
                .where(
                    Proposal.source == "SYSTEM",
                    Proposal.team_id == team_id,
                    Proposal.strategy_id.in_(("perptape", "perptape-resonance")),
                    Proposal.environment == "LIVE",
                    Proposal.status.in_(("DRAFT", "PENDING_REVIEW")),
                    Proposal.expires_at > now,
                )
                .order_by(Proposal.created_at, Proposal.proposal_id)
            ).all()
            grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
            for proposal, instrument in values:
                if not self.service.can_user(
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
                        "status": _effective_proposal_status(proposal, now),
                        "venue": proposal.venue,
                        "symbol": instrument.symbol,
                        "direction": proposal.direction,
                        "expires_at": _iso(proposal.expires_at),
                        "source_observed_at": _iso(proposal.source_observed_at),
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
        workspace_id, team_id = self._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            proposal = session.get(Proposal, proposal_id)
            if proposal is None:
                raise DomainRejected("PROPOSAL_NOT_FOUND", "proposal does not exist")
            if proposal.team_id != team_id:
                raise DomainRejected("TEAM_SCOPE_DENIED", "proposal is outside active team")
            if not self.service.can_user(user_id, "view", proposal.account_id, proposal.venue):
                raise DomainRejected("RBAC_DENIED", "proposal is outside the current scope")
            approvals = session.scalars(
                select(Approval)
                .where(Approval.proposal_id == proposal_id)
                .order_by(Approval.created_at)
            ).all()
            proposal_users = {
                item.user_id: item.username
                for item in session.scalars(
                    select(User).where(
                        User.user_id.in_(
                            {proposal.proposer_id, *(item.reviewer_id for item in approvals)}
                        )
                    )
                ).all()
            }
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
            result["workspace_id"] = str(workspace_id)
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
            result["execution_status"] = _proposal_execution_status(
                proposal,
                current_time,
                None if campaign is None else campaign.campaign_id,
            )
            result["proposer_username"] = proposal_users.get(proposal.proposer_id)
            result.update(
                {
                    "frozen_payload": proposal.frozen_payload,
                    "semantic_hash": proposal.semantic_hash,
                    "frozen_at": _iso(proposal.frozen_at),
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
                        "workspace_id": str(workspace_id),
                        "team_id": str(risk.team_id),
                        "account_id": proposal.account_id,
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
                        "workspace_id": str(workspace_id),
                        "team_id": str(authorization.team_id),
                        "account_id": authorization.account_id,
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
                        "workspace_id": str(workspace_id),
                        "team_id": str(campaign.team_id),
                        "account_id": campaign.account_id,
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
            reviewed_user_ids = set(
                session.scalars(
                    select(Approval.reviewer_id).where(Approval.proposal_id == proposal_id)
                ).all()
            )
            reused_actor_ids = set(
                session.scalars(
                    select(AuditEvent.actor_id).where(
                        AuditEvent.event_type == "PROPOSAL_DUPLICATE_REUSED",
                        AuditEvent.object_type == "Proposal",
                        AuditEvent.object_id == str(proposal_id),
                    )
                ).all()
            )
            assignments = session.scalars(
                select(RoleAssignment)
                .join(
                    TeamMembership,
                    and_(
                        TeamMembership.team_id == RoleAssignment.team_id,
                        TeamMembership.user_id == RoleAssignment.user_id,
                    ),
                )
                .where(
                    RoleAssignment.team_id == proposal.team_id,
                    RoleAssignment.role == Role.REVIEWER.value,
                    TeamMembership.active,
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
                select(User).where(User.user_id.in_(reviewer_ids), User.active)
            ).all()
            for user in users:
                session.expunge(user)
            return list(users)
