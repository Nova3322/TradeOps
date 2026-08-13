from __future__ import annotations

from trading_control_plane.query_component import QueryComponent

# ruff: noqa: F403, F405
from trading_control_plane.query_core import *


class WorkspaceQueries(QueryComponent):
    def _active_scope_ids(self, user_id: UUID) -> tuple[UUID, UUID]:
        context = self.user_context(user_id)
        workspace = context.get("active_workspace")
        team = context.get("active_team")
        if not isinstance(workspace, dict) or not isinstance(team, dict):
            raise DomainRejected("TEAM_CONTEXT_REQUIRED", "select an active team")
        return UUID(str(workspace["workspace_id"])), UUID(str(team["team_id"]))

    def user_by_username(self, username: str) -> User:
        with self.database.session_factory() as session:
            user = session.scalar(select(User).where(User.username == username))
            if user is None or not user.active or user.principal_type != PrincipalType.HUMAN.value:
                raise DomainRejected("LOGIN_DENIED", "internal user is missing or inactive")
            session.expunge(user)
            return user

    def password_credential(self, username: str) -> dict[str, Any] | None:
        with self.database.session_factory() as session:
            user = session.scalar(select(User).where(User.username == username))
            if user is None:
                return None
            return {
                "user_id": user.user_id,
                "username": user.username,
                "password_hash": user.password_hash,
                "auth_version": user.auth_version,
                "active": user.active,
                "principal_type": user.principal_type,
            }

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
            api_context = current_api_client_context()
            if api_context is not None and api_context.owner_user_id != user_id:
                raise DomainRejected("API_CLIENT_SCOPE_DENIED", "API Key owner mismatch")
            workspace_memberships = session.execute(
                select(WorkspaceMembership, Workspace)
                .join(Workspace, Workspace.workspace_id == WorkspaceMembership.workspace_id)
                .where(
                    WorkspaceMembership.user_id == user_id,
                    WorkspaceMembership.active,
                    Workspace.active,
                )
                .order_by(Workspace.name, Workspace.workspace_id)
            ).all()
            team_memberships = session.execute(
                select(TeamMembership, Team)
                .join(Team, Team.team_id == TeamMembership.team_id)
                .where(
                    TeamMembership.user_id == user_id,
                    TeamMembership.active,
                    Team.active,
                )
                .order_by(Team.name, Team.team_id)
            ).all()
            if api_context is not None:
                workspace_memberships = [
                    item
                    for item in workspace_memberships
                    if item[1].workspace_id == api_context.workspace_id
                ]
                team_memberships = [
                    item for item in team_memberships if item[1].team_id == api_context.team_id
                ]
            workspace_ids = [
                workspace.workspace_id for _membership, workspace in workspace_memberships
            ]
            workspace_principal_counts: dict[UUID, dict[str, int]] = {
                workspace_id: {"human": 0, "agent": 0} for workspace_id in workspace_ids
            }
            if workspace_ids:
                count_rows = session.execute(
                    select(
                        WorkspaceMembership.workspace_id,
                        User.principal_type,
                        func.count(User.user_id),
                    )
                    .join(User, User.user_id == WorkspaceMembership.user_id)
                    .where(
                        WorkspaceMembership.workspace_id.in_(workspace_ids),
                        WorkspaceMembership.active,
                        User.active,
                    )
                    .group_by(WorkspaceMembership.workspace_id, User.principal_type)
                ).all()
                for workspace_id, principal_type, count in count_rows:
                    key = "human" if principal_type == PrincipalType.HUMAN.value else "agent"
                    workspace_principal_counts[workspace_id][key] = int(count)
            accessible_teams_by_workspace: dict[UUID, list[Team]] = {}
            for _membership, team in team_memberships:
                accessible_teams_by_workspace.setdefault(team.workspace_id, []).append(team)
            roles = session.scalars(
                select(RoleAssignment)
                .where(
                    RoleAssignment.user_id == user_id,
                    RoleAssignment.team_id
                    == (api_context.team_id if api_context is not None else user.active_team_id),
                )
                .order_by(RoleAssignment.role)
            ).all()
            active_workspace = next(
                (
                    workspace
                    for membership, workspace in workspace_memberships
                    if membership.workspace_id
                    == (
                        api_context.workspace_id
                        if api_context is not None
                        else user.active_workspace_id
                    )
                ),
                None,
            )
            active_team = next(
                (
                    team
                    for membership, team in team_memberships
                    if membership.team_id
                    == (api_context.team_id if api_context is not None else user.active_team_id)
                    and team.workspace_id
                    == (
                        api_context.workspace_id
                        if api_context is not None
                        else user.active_workspace_id
                    )
                ),
                None,
            )
            return {
                "user_id": str(user.user_id),
                "username": user.username,
                "auth_version": user.auth_version,
                "principal_type": user.principal_type,
                "service_kind": user.service_kind,
                "api_client_scope": (
                    None
                    if api_context is None
                    else {
                        "api_client_id": str(api_context.api_client_id),
                        "workspace_id": str(api_context.workspace_id),
                        "team_id": str(api_context.team_id),
                        "account_id": None,
                        "venue": None,
                        "scope_model": "USER_RBAC",
                        "permissions_source": "HUMAN_DYNAMIC",
                    }
                ),
                "active_workspace": (
                    None
                    if active_workspace is None
                    else {
                        "workspace_id": str(active_workspace.workspace_id),
                        "name": active_workspace.name,
                        "slug": active_workspace.slug,
                    }
                ),
                "active_team": (
                    None
                    if active_team is None
                    else {
                        "team_id": str(active_team.team_id),
                        "workspace_id": str(active_team.workspace_id),
                        "name": active_team.name,
                        "slug": active_team.slug,
                        "trading_enabled": active_team.trading_enabled,
                        "execution_mode": active_team.execution_mode,
                    }
                ),
                "workspaces": [
                    {
                        "workspace_id": str(workspace.workspace_id),
                        "name": workspace.name,
                        "slug": workspace.slug,
                        "role": membership.role,
                        "member_count": workspace_principal_counts[workspace.workspace_id]["human"],
                        "agent_count": workspace_principal_counts[workspace.workspace_id]["agent"],
                        "team_count": len(
                            accessible_teams_by_workspace.get(workspace.workspace_id, [])
                        ),
                        "default_team_id": next(
                            (
                                str(team.team_id)
                                for team in accessible_teams_by_workspace.get(
                                    workspace.workspace_id, []
                                )
                                if team.slug == "default"
                            ),
                            (
                                str(
                                    accessible_teams_by_workspace[workspace.workspace_id][0].team_id
                                )
                                if accessible_teams_by_workspace.get(workspace.workspace_id)
                                else None
                            ),
                        ),
                    }
                    for membership, workspace in workspace_memberships
                ],
                "teams": [
                    {
                        "team_id": str(team.team_id),
                        "workspace_id": str(team.workspace_id),
                        "name": team.name,
                        "slug": team.slug,
                        "trading_enabled": team.trading_enabled,
                        "execution_mode": team.execution_mode,
                    }
                    for _membership, team in team_memberships
                ],
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
            actor = session.get(User, actor_id)
            if actor is None or actor.active_team_id is None:
                raise DomainRejected("TEAM_CONTEXT_REQUIRED", "select an active team")
            users = session.scalars(
                select(User)
                .join(TeamMembership, TeamMembership.user_id == User.user_id)
                .where(User.principal_type == PrincipalType.HUMAN.value)
                .where(TeamMembership.team_id == actor.active_team_id)
                .order_by(User.username)
            ).all()
            assignments = session.scalars(
                select(RoleAssignment)
                .where(
                    RoleAssignment.user_id.in_([item.user_id for item in users]),
                    RoleAssignment.team_id == actor.active_team_id,
                )
                .order_by(RoleAssignment.role)
            ).all()
            memberships = {
                item.user_id: item
                for item in session.scalars(
                    select(TeamMembership).where(
                        TeamMembership.team_id == actor.active_team_id,
                        TeamMembership.user_id.in_([item.user_id for item in users]),
                    )
                )
            }
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
                    "password_configured": user.password_hash is not None,
                    "active": user.active and memberships[user.user_id].active,
                    "workspace_id": str(actor.active_workspace_id),
                    "team_id": str(actor.active_team_id),
                    "roles": by_user[user.user_id],
                    "created_at": _iso(user.created_at),
                    "is_current_user": user.user_id == actor_id,
                }
                for user in users
            ]

    def managed_agents(
        self,
        actor_id: UUID,
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        if not self.service.can_user(actor_id, "user.manage"):
            raise DomainRejected("RBAC_DENIED", "agent access management requires SYSTEM_ADMIN")
        observed_at = datetime.now(UTC) if now is None else now
        with self.database.session_factory() as session:
            actor = session.get(User, actor_id)
            if actor is None or actor.active_team_id is None:
                raise DomainRejected("TEAM_CONTEXT_REQUIRED", "select an active team")
            agents = session.scalars(
                select(User)
                .join(TeamMembership, TeamMembership.user_id == User.user_id)
                .where(
                    User.principal_type == PrincipalType.SERVICE.value,
                    User.service_kind == ServicePrincipalKind.AGENT.value,
                    TeamMembership.team_id == actor.active_team_id,
                )
                .order_by(User.username)
            ).all()
            if not agents:
                return []
            memberships = {
                item.user_id: item
                for item in session.scalars(
                    select(TeamMembership).where(
                        TeamMembership.team_id == actor.active_team_id,
                        TeamMembership.user_id.in_([item.user_id for item in agents]),
                    )
                )
            }
            assignments = session.scalars(
                select(RoleAssignment)
                .where(
                    RoleAssignment.team_id == actor.active_team_id,
                    RoleAssignment.user_id.in_([item.user_id for item in agents]),
                )
                .order_by(RoleAssignment.role)
            ).all()
            by_user: dict[UUID, list[dict[str, Any]]] = {item.user_id: [] for item in agents}
            for assignment in assignments:
                by_user[assignment.user_id].append(
                    {
                        "role": assignment.role,
                        "account_scope": assignment.account_scope,
                        "venue_scope": assignment.venue_scope,
                    }
                )
            result: list[dict[str, Any]] = []
            for agent in agents:
                membership = memberships[agent.user_id]
                active = bool(agent.active and membership.active)
                token_status = (
                    "INACTIVE"
                    if not active
                    else "EXPIRED"
                    if agent.agent_token_expires_at is None
                    or agent.agent_token_expires_at <= observed_at
                    else "ACTIVE"
                )
                result.append(
                    {
                        "agent_id": str(agent.user_id),
                        "username": agent.username,
                        "principal_type": agent.principal_type,
                        "service_kind": agent.service_kind,
                        "workspace_id": str(actor.active_workspace_id),
                        "team_id": str(actor.active_team_id),
                        "active": active,
                        "auth_version": agent.auth_version,
                        "roles": by_user[agent.user_id],
                        "token": {
                            "status": token_status,
                            "hint": agent.agent_token_hint,
                            "version": agent.agent_token_version,
                            "created_at": _iso(agent.agent_token_created_at),
                            "expires_at": _iso(agent.agent_token_expires_at),
                            "last_used_at": _iso(agent.agent_token_last_used_at),
                        },
                        "created_at": _iso(agent.created_at),
                    }
                )
            return result

    def api_clients(
        self,
        owner_id: UUID,
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        if current_api_client_context() is not None:
            raise DomainRejected(
                "HUMAN_WEB_CONFIRMATION_REQUIRED",
                "API Key inventory is available only in an interactive HUMAN session",
            )
        observed_at = datetime.now(UTC) if now is None else now
        with self.database.session_factory() as session:
            owner = session.get(User, owner_id)
            if (
                owner is None
                or not owner.active
                or owner.principal_type != PrincipalType.HUMAN.value
            ):
                raise DomainRejected("SESSION_REVOKED", "internal user is inactive or missing")
            clients = session.scalars(
                select(ApiClient)
                .where(ApiClient.owner_user_id == owner_id)
                .order_by(ApiClient.created_at.desc(), ApiClient.api_client_id)
            ).all()
            result: list[dict[str, Any]] = []
            for client in clients:
                workspace = session.get(Workspace, client.workspace_id)
                team = session.get(Team, client.team_id)
                workspace_membership = session.scalar(
                    select(WorkspaceMembership).where(
                        WorkspaceMembership.workspace_id == client.workspace_id,
                        WorkspaceMembership.user_id == owner_id,
                        WorkspaceMembership.active,
                    )
                )
                team_membership = session.scalar(
                    select(TeamMembership).where(
                        TeamMembership.team_id == client.team_id,
                        TeamMembership.user_id == owner_id,
                        TeamMembership.active,
                    )
                )
                assignments = session.scalars(
                    select(RoleAssignment)
                    .where(
                        RoleAssignment.user_id == owner_id,
                        RoleAssignment.team_id == client.team_id,
                    )
                    .order_by(RoleAssignment.role)
                ).all()
                effective_roles = sorted({assignment.role for assignment in assignments})
                access_status = (
                    "WORKSPACE_ACCESS_REVOKED"
                    if workspace is None or not workspace.active or workspace_membership is None
                    else "TEAM_ACCESS_REVOKED"
                    if team is None or not team.active or team_membership is None
                    else "NO_CURRENT_PERMISSION"
                    if not effective_roles
                    else "AVAILABLE"
                )
                token_status = (
                    "REVOKED"
                    if client.state == ApiClientState.REVOKED.value
                    else "DISABLED"
                    if client.state == ApiClientState.DISABLED.value
                    else "EXPIRED"
                    if client.token_expires_at <= observed_at
                    else "BLOCKED"
                    if access_status != "AVAILABLE"
                    else "ACTIVE"
                )
                result.append(
                    {
                        "api_key_id": str(client.api_client_id),
                        "api_client_id": str(client.api_client_id),
                        "name": client.name,
                        "owner_user_id": str(owner_id),
                        "workspace": {
                            "workspace_id": str(client.workspace_id),
                            "name": None if workspace is None else workspace.name,
                        },
                        "team": {
                            "team_id": str(client.team_id),
                            "name": None if team is None else team.name,
                        },
                        "account_id": None,
                        "venue": None,
                        "scope_model": "USER_RBAC",
                        "state": client.state,
                        "version": client.version,
                        "access_status": access_status,
                        "permissions_source": "HUMAN_DYNAMIC",
                        "effective_roles": effective_roles,
                        "token": {
                            "status": token_status,
                            "hint": client.token_hint,
                            "version": client.token_version,
                            "created_at": _iso(client.token_created_at),
                            "expires_at": _iso(client.token_expires_at),
                            "last_used_at": _iso(client.token_last_used_at),
                        },
                        "revoked_at": _iso(client.revoked_at),
                        "created_at": _iso(client.created_at),
                        "updated_at": _iso(client.updated_at),
                    }
                )
            return result

    def api_client_scopes(self, owner_id: UUID) -> list[dict[str, Any]]:
        if current_api_client_context() is not None:
            raise DomainRejected(
                "HUMAN_WEB_CONFIRMATION_REQUIRED",
                "API Key Team selection requires an interactive HUMAN session",
            )
        with self.database.session_factory() as session:
            owner = session.get(User, owner_id)
            if owner is None or not owner.active:
                raise DomainRejected("SESSION_REVOKED", "internal user is inactive or missing")
            rows = session.execute(
                select(Team, Workspace)
                .join(Workspace, Workspace.workspace_id == Team.workspace_id)
                .join(
                    TeamMembership,
                    and_(
                        TeamMembership.team_id == Team.team_id,
                        TeamMembership.user_id == owner_id,
                        TeamMembership.active,
                    ),
                )
                .join(
                    WorkspaceMembership,
                    and_(
                        WorkspaceMembership.workspace_id == Workspace.workspace_id,
                        WorkspaceMembership.user_id == owner_id,
                        WorkspaceMembership.active,
                    ),
                )
                .where(
                    Team.active,
                    Workspace.active,
                )
                .order_by(Workspace.name, Team.name, Team.team_id)
            ).all()
            result: list[dict[str, Any]] = []
            for team, workspace in rows:
                assignments = session.scalars(
                    select(RoleAssignment).where(
                        RoleAssignment.user_id == owner_id,
                        RoleAssignment.team_id == team.team_id,
                    )
                ).all()
                if not assignments:
                    continue
                result.append(
                    {
                        "workspace_id": str(workspace.workspace_id),
                        "workspace_name": workspace.name,
                        "team_id": str(team.team_id),
                        "team_name": team.name,
                        "account_id": None,
                        "account_label": None,
                        "venue": None,
                        "scope_model": "USER_RBAC",
                    }
                )
            return result

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

    def notification_center(
        self,
        user_id: UUID,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        workspace_id, team_id = self._active_scope_ids(user_id)
        if not self.service.can_user(user_id, "notification.view"):
            raise DomainRejected(
                "RBAC_DENIED",
                "notification.view is not allowed in the active team",
            )
        bounded_limit = min(max(limit, 1), 200)
        can_manage = self.service.can_user(user_id, "notification.manage")
        with self.database.session_factory() as session:
            team = session.get(Team, team_id)
            routes = session.scalars(
                select(NotificationRoute)
                .where(
                    NotificationRoute.team_id == team_id,
                    NotificationRoute.deleted_at.is_(None),
                )
                .order_by(NotificationRoute.name, NotificationRoute.notification_route_id)
            ).all()
            deliveries = session.scalars(
                select(NotificationDelivery)
                .where(NotificationDelivery.team_id == team_id)
                .order_by(
                    NotificationDelivery.created_at.desc(),
                    NotificationDelivery.notification_delivery_id.desc(),
                )
                .limit(bounded_limit)
            ).all()
            status_counts = {
                str(status): int(count)
                for status, count in session.execute(
                    select(
                        NotificationDelivery.status,
                        func.count(NotificationDelivery.notification_delivery_id),
                    )
                    .where(NotificationDelivery.team_id == team_id)
                    .group_by(NotificationDelivery.status)
                )
            }
            return {
                "scope": {
                    "workspace_id": str(workspace_id),
                    "team_id": str(team_id),
                    "team_name": "Unknown Team" if team is None else team.name,
                },
                "can_manage": can_manage,
                "channel_permissions": {
                    "trading": False,
                    "funding": False,
                    "signing": False,
                    "broadcast": False,
                },
                "event_catalog": [
                    {
                        "event_type": template.event_type,
                        "template_key": template.key,
                        "template_version": template.version,
                        "title": template.title,
                        "integration_status": (
                            "ACTIVE"
                            if template.event_type in ROUTABLE_NOTIFICATION_EVENT_TYPES
                            else "SCOPE_MIGRATION_REQUIRED"
                        ),
                        "blocker": (
                            None
                            if template.event_type in ROUTABLE_NOTIFICATION_EVENT_TYPES
                            else ("团队资金真源尚未迁移完成; 不会把旧资金对象猜测映射到团队。")
                        ),
                    }
                    for template in NOTIFICATION_TEMPLATES.values()
                    if template.event_type != "TEST_NOTIFICATION"
                ],
                "routes": [
                    {
                        "notification_route_id": str(route.notification_route_id),
                        "workspace_id": str(workspace_id),
                        "team_id": str(team_id),
                        "name": route.name,
                        "environment": route.environment,
                        "channel": route.channel,
                        "event_types": list(route.event_types),
                        "enabled": route.enabled,
                        "configuration_state": "ENCRYPTED",
                        "configuration_metadata": route.configuration_metadata,
                        "credential_version": route.credential_version,
                        "version": route.version,
                        "updated_at": _iso(route.updated_at),
                    }
                    for route in routes
                ],
                "deliveries": [
                    {
                        "notification_delivery_id": str(delivery.notification_delivery_id),
                        "notification_event_id": str(delivery.notification_event_id),
                        "notification_route_id": str(delivery.notification_route_id),
                        "workspace_id": str(workspace_id),
                        "team_id": str(team_id),
                        "channel": delivery.channel,
                        "event_type": delivery.event_type,
                        "template_key": delivery.template_key,
                        "template_version": delivery.template_version,
                        "payload": delivery.payload,
                        "object_type": delivery.object_type,
                        "object_id": delivery.object_id,
                        "object_version": delivery.object_version,
                        "environment": delivery.environment,
                        "account_id": delivery.account_id,
                        "venue": delivery.venue,
                        "status": delivery.status,
                        "attempt_count": delivery.attempt_count,
                        "max_attempts": delivery.max_attempts,
                        "next_attempt_at": _iso(delivery.next_attempt_at),
                        "last_attempt_at": _iso(delivery.last_attempt_at),
                        "sent_at": _iso(delivery.sent_at),
                        "last_error_code": delivery.last_error_code,
                        "created_at": _iso(delivery.created_at),
                        "updated_at": _iso(delivery.updated_at),
                    }
                    for delivery in deliveries
                ],
                "delivery_status_counts": status_counts,
                "delivery_limit": bounded_limit,
            }
