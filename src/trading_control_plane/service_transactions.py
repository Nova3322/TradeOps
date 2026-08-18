from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from trading_control_plane import (
    authorization_policy,
    domain,
    idempotency,
    metrics,
    models,
    rejections,
    request_context,
)
from trading_control_plane import execution_scope as scope_rules
from trading_control_plane.credentials import CredentialCipher
from trading_control_plane.database import Database


class TransactionService:
    """Shared fail-closed transaction primitives; owns no domain state or projections."""

    def __init__(self, database: Database, credential_cipher: CredentialCipher) -> None:
        self.database = database
        self.credential_cipher = credential_cipher

    @staticmethod
    def lock_risk_capacity(session: Session, team_id: UUID | None = None) -> None:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {
                "key": (
                    scope_rules.RISK_CAPACITY_LOCK_KEY
                    if team_id is None
                    else scope_rules.advisory_lock_key(str(team_id), "risk-capacity", "team")
                )
            },
        )

    @staticmethod
    def validate_sender_lease(
        session: Session,
        team_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        now: datetime,
    ) -> None:
        scope_rules.scope_parts(execution_scope)
        lease = session.get(models.SenderLease, (team_id, execution_scope))
        if (
            lease is None
            or lease.owner_id != owner_id
            or lease.fencing_token != fencing_token
            or lease.expires_at <= now
        ):
            metrics.FENCING_REJECTIONS.inc()
            rejections.reject(
                "FENCING_TOKEN_REJECTED", "sender lease is stale, expired, or superseded"
            )

    def audit(
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
        workspace_id: UUID | None = None,
        team_id: UUID | None = None,
        account_id: str | None = None,
        environment: str | None = None,
        generation: int | None = None,
        rule_summary: dict[str, Any] | None = None,
        now: datetime,
    ) -> None:
        api_context = request_context.current_api_client_context()
        api_client_id = None
        if api_context is not None and actor_id == str(api_context.owner_user_id):
            api_client_id = api_context.api_client_id
            workspace_id = workspace_id or api_context.workspace_id
            team_id = team_id or api_context.team_id
        if workspace_id is None or team_id is None:
            try:
                actor_user_id = UUID(actor_id)
            except ValueError:
                actor_user_id = None
            actor = None if actor_user_id is None else session.get(models.User, actor_user_id)
            if actor is not None:
                workspace_id = workspace_id or actor.active_workspace_id
                team_id = team_id or actor.active_team_id
        if account_id is None:
            try:
                object_uuid = UUID(str(object_id))
            except ValueError:
                object_uuid = None
            scoped_object: Any | None = None
            if object_uuid is not None:
                direct_account_models = {
                    "Proposal": models.Proposal,
                    "Campaign": models.Campaign,
                    "VenueOrder": models.VenueOrder,
                    "VenueFill": models.VenueFill,
                    "Position": models.Position,
                    "AccountEquity": models.AccountEquity,
                    "AccountEquityObservation": models.AccountEquityObservation,
                    "FundingPayment": models.FundingPayment,
                    "ProposalDefaultConfig": models.ProposalDefaultConfig,
                    "TransferProposal": models.TransferProposal,
                    "TransferAuthorization": models.TransferAuthorization,
                    "CapitalTransfer": models.CapitalTransfer,
                    "DirectCapitalOperation": models.DirectCapitalOperation,
                    "CapitalAutomationPolicy": models.CapitalAutomationPolicy,
                }
                model = direct_account_models.get(object_type)
                if model is not None:
                    scoped_object = session.get(model, object_uuid)
                elif object_type == "RiskDecision":
                    decision = session.get(models.RiskDecision, object_uuid)
                    scoped_object = (
                        None
                        if decision is None
                        else session.get(models.Proposal, decision.proposal_id)
                    )
                elif object_type == "TradingAuthorization":
                    authorization = session.get(models.TradingAuthorization, object_uuid)
                    scoped_object = (
                        None
                        if authorization is None
                        else session.get(models.Proposal, authorization.proposal_id)
                    )
                elif object_type == "OrderIntent":
                    intent = session.get(models.OrderIntent, object_uuid)
                    scoped_object = (
                        None if intent is None else session.get(models.Campaign, intent.campaign_id)
                    )
                elif object_type == "RiskReservation":
                    reservation = session.get(models.RiskReservation, object_uuid)
                    scoped_object = (
                        None
                        if reservation is None
                        else session.get(models.Campaign, reservation.campaign_id)
                    )
                elif object_type == "ProtectionOrder":
                    protection = session.get(models.ProtectionOrder, object_uuid)
                    scoped_object = (
                        None
                        if protection is None
                        else session.get(models.Position, protection.position_id)
                    )
                elif object_type == "ReconciliationRun":
                    reconciliation = session.get(models.ReconciliationRun, object_uuid)
                    if reconciliation is not None and reconciliation.campaign_id is not None:
                        scoped_object = session.get(models.Campaign, reconciliation.campaign_id)
                    elif reconciliation is not None:
                        try:
                            _environment, account_id, _venue = scope_rules.scope_parts(
                                reconciliation.execution_scope
                            )
                        except domain.DomainRejected:
                            account_id = None
            if scoped_object is not None:
                account_id = getattr(
                    scoped_object,
                    "account_id",
                    getattr(scoped_object, "source_account_id", None),
                )
                scoped_team_id = getattr(scoped_object, "team_id", None)
                if scoped_team_id is not None:
                    team_id = team_id or scoped_team_id
                    if workspace_id is None:
                        scoped_team = session.get(models.Team, scoped_team_id)
                        workspace_id = None if scoped_team is None else scoped_team.workspace_id
        session.add(
            models.AuditEvent(
                workspace_id=workspace_id,
                team_id=team_id,
                account_id=account_id,
                environment=environment,
                generation=generation,
                rule_summary=rule_summary,
                actor_id=actor_id,
                api_client_id=api_client_id,
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

    def idempotency(
        self,
        session: Session,
        *,
        caller_id: str,
        operation: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any] | None]:
        api_context = request_context.current_api_client_context()
        if api_context is not None:
            caller_id = f"api-client:{api_context.api_client_id}:{caller_id}"
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": scope_rules.advisory_lock_key(caller_id, operation, idempotency_key)},
        )
        digest = idempotency.semantic_hash(payload)
        receipt = session.scalar(
            select(models.CommandReceipt).where(
                models.CommandReceipt.caller_id == caller_id,
                models.CommandReceipt.operation == operation,
                models.CommandReceipt.idempotency_key == idempotency_key,
            )
        )
        if receipt is None:
            return digest, None
        if receipt.semantic_hash != digest:
            raise domain.IdempotencyConflict
        return digest, receipt.response

    def save_receipt(
        self,
        session: Session,
        *,
        caller_id: str,
        operation: str,
        idempotency_key: str,
        semantic_hash: str,
        response: dict[str, Any],
        now: datetime,
    ) -> None:
        api_context = request_context.current_api_client_context()
        if api_context is not None:
            caller_id = f"api-client:{api_context.api_client_id}:{caller_id}"
        session.add(
            models.CommandReceipt(
                caller_id=caller_id,
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=semantic_hash,
                response=response,
                created_at=now,
            )
        )

    @staticmethod
    def active_scope(
        session: Session,
        user_id: UUID,
        *,
        require_team: bool = True,
    ) -> tuple[models.User, models.Workspace, models.Team | None]:
        user = session.get(models.User, user_id)
        if user is None or not user.active:
            rejections.reject("USER_NOT_AUTHORIZED", "user is missing or inactive")
        api_context = request_context.current_api_client_context()
        if api_context is not None and api_context.owner_user_id != user_id:
            rejections.reject(
                "API_CLIENT_SCOPE_DENIED", "API Key owner does not match the request actor"
            )
        workspace_id = (
            api_context.workspace_id if api_context is not None else user.active_workspace_id
        )
        team_id = api_context.team_id if api_context is not None else user.active_team_id
        if workspace_id is None:
            rejections.reject("WORKSPACE_CONTEXT_REQUIRED", "select an active workspace")
        workspace = session.get(models.Workspace, workspace_id)
        membership = session.scalar(
            select(models.WorkspaceMembership).where(
                models.WorkspaceMembership.workspace_id == workspace_id,
                models.WorkspaceMembership.user_id == user_id,
                models.WorkspaceMembership.active,
            )
        )
        if workspace is None or not workspace.active or membership is None:
            rejections.reject(
                "WORKSPACE_ACCESS_DENIED", "workspace membership is missing or inactive"
            )
        if team_id is None:
            if require_team:
                rejections.reject("TEAM_CONTEXT_REQUIRED", "select an active team")
            return user, workspace, None
        team = session.get(models.Team, team_id)
        team_membership = session.scalar(
            select(models.TeamMembership).where(
                models.TeamMembership.team_id == team_id,
                models.TeamMembership.user_id == user_id,
                models.TeamMembership.active,
            )
        )
        if (
            team is None
            or not team.active
            or team.workspace_id != workspace.workspace_id
            or team_membership is None
        ):
            rejections.reject("TEAM_ACCESS_DENIED", "team membership is missing or inactive")
        return user, workspace, team

    def require_workspace_admin(
        self,
        session: Session,
        user_id: UUID,
    ) -> tuple[models.User, models.Workspace]:
        user, workspace, _team = self.active_scope(session, user_id, require_team=False)
        membership = session.scalar(
            select(models.WorkspaceMembership).where(
                models.WorkspaceMembership.workspace_id == workspace.workspace_id,
                models.WorkspaceMembership.user_id == user_id,
                models.WorkspaceMembership.role == domain.WorkspaceRole.ADMIN.value,
                models.WorkspaceMembership.active,
            )
        )
        if membership is None:
            rejections.reject("WORKSPACE_ADMIN_REQUIRED", "workspace administration is required")
        return user, workspace

    def require_role(
        self,
        session: Session,
        user_id: UUID,
        action: str,
        account_id: str | None = None,
        venue: str | None = None,
        *,
        team_id: UUID | None = None,
        allow_setup: bool = False,
    ) -> models.Team:
        api_context = request_context.current_api_client_context()
        if api_context is not None:
            if (
                action in authorization_policy.API_CLIENT_HUMAN_ONLY_ACTIONS
                or action not in authorization_policy.API_CLIENT_ALLOWED_BUSINESS_ACTIONS
            ):
                rejections.reject(
                    "HUMAN_WEB_CONFIRMATION_REQUIRED",
                    f"{action} requires the owner to use an interactive web session",
                )
        _user, _workspace, team = self.active_scope(session, user_id)
        assert team is not None
        if team_id is not None and team.team_id != team_id:
            rejections.reject("TEAM_SCOPE_DENIED", "resource is outside the active team scope")
        if (
            not team.trading_enabled
            and action not in authorization_policy.TEAM_SETUP_ACTIONS
            and not allow_setup
        ):
            rejections.reject(
                "TEAM_NOT_OPERATIONAL",
                "team data scope is not ready; configure scoped accounts and risk policy first",
            )
        assignments = session.scalars(
            select(models.RoleAssignment).where(
                models.RoleAssignment.user_id == user_id,
                models.RoleAssignment.team_id == team.team_id,
            )
        ).all()
        for assignment in assignments:
            role = domain.Role(assignment.role)
            if (
                action not in authorization_policy.ROLE_ACTIONS[role]
                and "*" not in authorization_policy.ROLE_ACTIONS[role]
            ):
                continue
            if assignment.account_scope is not None and assignment.account_scope != account_id:
                continue
            if assignment.venue_scope is not None and assignment.venue_scope != venue:
                continue
            return team
        rejections.reject("RBAC_DENIED", f"{action} is not allowed in the requested scope")

    @staticmethod
    def require_team_environment(
        team: models.Team, environment: domain.ExecutionEnvironment
    ) -> None:
        mode = team.execution_mode
        if mode == domain.TeamExecutionMode.SETUP.value:
            rejections.reject(
                "TEAM_SETUP_INCOMPLETE",
                "team must complete setup and explicitly select TESTNET or LIVE",
            )
        if environment.value != mode:
            if mode == domain.TeamExecutionMode.TESTNET.value:
                rejections.reject(
                    "TEAM_TESTNET_ONLY",
                    "Team mode is locked to TESTNET; LIVE workflows are blocked",
                )
            rejections.reject(
                "TEAM_LIVE_ONLY",
                "Team mode is locked to LIVE; TESTNET workflows are blocked",
            )

    def can_user(
        self,
        user_id: UUID,
        action: str,
        account_id: str | None = None,
        venue: str | None = None,
    ) -> bool:
        with self.database.session_factory() as session:
            try:
                self.require_role(session, user_id, action, account_id, venue)
            except domain.DomainRejected:
                return False
            return True

    def require_action_assignment(
        self,
        session: Session,
        user_id: UUID,
        action: str,
        *,
        allow_setup: bool = False,
    ) -> models.Team:
        """Require an active-team grant; the eventual write rechecks object scope."""

        api_context = request_context.current_api_client_context()
        if api_context is not None and (
            action in authorization_policy.API_CLIENT_HUMAN_ONLY_ACTIONS
            or action not in authorization_policy.API_CLIENT_ALLOWED_BUSINESS_ACTIONS
        ):
            rejections.reject(
                "HUMAN_WEB_CONFIRMATION_REQUIRED",
                f"{action} requires the owner to use an interactive web session",
            )

        _user, _workspace, team = self.active_scope(session, user_id)
        assert team is not None
        if (
            not team.trading_enabled
            and action not in authorization_policy.TEAM_SETUP_ACTIONS
            and not allow_setup
        ):
            rejections.reject(
                "TEAM_NOT_OPERATIONAL",
                "team data scope is not ready; configure scoped accounts and risk policy first",
            )
        assignments = session.scalars(
            select(models.RoleAssignment).where(
                models.RoleAssignment.user_id == user_id,
                models.RoleAssignment.team_id == team.team_id,
            )
        ).all()
        if any(
            action in authorization_policy.ROLE_ACTIONS[domain.Role(item.role)]
            or "*" in authorization_policy.ROLE_ACTIONS[domain.Role(item.role)]
            for item in assignments
        ):
            return team
        rejections.reject("RBAC_DENIED", f"{action} is not allowed in the active team")
