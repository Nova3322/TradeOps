from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from trading_control_plane.database import Database
from trading_control_plane.domain import DomainRejected
from trading_control_plane.models import Team, TeamMembership, User, Workspace, WorkspaceMembership
from trading_control_plane.request_context import current_api_client_context
from trading_control_plane.service import TradingService


def iso_datetime(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class QueryRuntime:
    database: Database
    service: TradingService


class QueryComponent:
    """Shared runtime access for the projection methods composed by TradingQueries."""

    runtime: QueryRuntime

    @property
    def database(self) -> Database:
        return self.runtime.database

    @property
    def service(self) -> TradingService:
        return self.runtime.service

    def active_scope_ids(self, user_id: UUID) -> tuple[UUID, UUID]:
        """Resolve and validate the request actor's exact active workspace/team scope."""

        with self.database.session_factory() as session:
            user = session.get(User, user_id)
            if user is None or not user.active:
                raise DomainRejected("SESSION_REVOKED", "internal user is inactive or missing")
            api_context = current_api_client_context()
            if api_context is not None and api_context.owner_user_id != user_id:
                raise DomainRejected("API_CLIENT_SCOPE_DENIED", "API Key owner mismatch")
            workspace_id = (
                api_context.workspace_id if api_context is not None else user.active_workspace_id
            )
            team_id = api_context.team_id if api_context is not None else user.active_team_id
            if workspace_id is None or team_id is None:
                raise DomainRejected("TEAM_CONTEXT_REQUIRED", "select an active team")
            workspace = session.get(Workspace, workspace_id)
            team = session.get(Team, team_id)
            workspace_membership = session.scalar(
                select(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.user_id == user_id,
                    WorkspaceMembership.active,
                )
            )
            team_membership = session.scalar(
                select(TeamMembership).where(
                    TeamMembership.team_id == team_id,
                    TeamMembership.user_id == user_id,
                    TeamMembership.active,
                )
            )
            if (
                workspace is None
                or not workspace.active
                or team is None
                or not team.active
                or team.workspace_id != workspace_id
                or workspace_membership is None
                or team_membership is None
            ):
                raise DomainRejected(
                    "TEAM_CONTEXT_REQUIRED",
                    "select an active team",
                )
            return workspace_id, team_id
