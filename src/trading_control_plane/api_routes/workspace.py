from __future__ import annotations

from trading_control_plane.api_core import (
    SESSION_COOKIE,
    UUID,
    AgentAccessRequest,
    AgentCreateRequest,
    AgentTokenRotationRequest,
    Any,
    ApiClientCreateRequest,
    ApiClientRevokeRequest,
    ApiClientStateRequest,
    DomainRejected,
    HTTPException,
    ManagedUserAccessRequest,
    ManagedUserCreateRequest,
    MockLoginRequest,
    MockStepUpRequest,
    NotificationRouteDeleteRequest,
    NotificationRouteWriteRequest,
    NotificationTestRequest,
    PasswordChangeRequest,
    PasswordLoginRequest,
    Query,
    Request,
    Response,
    Role,
    ScopeSelectRequest,
    SessionIdentity,
    TeamCreateRequest,
    TeamMemberInviteRequest,
    TeamMemberRemoveRequest,
    TeamTradingModeRequest,
    WorkspaceCreateRequest,
    _now,
    status,
    timedelta,
)
from trading_control_plane.api_routes.context import ApiRouteContext


class _WorkspaceRoutes:
    def __init__(self, context: ApiRouteContext) -> None:
        dependencies = context.workspace
        common = dependencies.common
        self.app = context.app
        self.identity_dependency = common.identity
        self.login_limiter = dependencies.login_limiter
        self.password_hasher = dependencies.password_hasher
        self.queries = common.queries
        self.resolved_settings = common.settings
        self.token_service = dependencies.token_service
        self.require_capability = common.require_capability
        self.service = common.service
        self.configured_risk_scopes = dependencies.configured_risk_scopes
        self.is_agent_identity = dependencies.is_agent_identity
        self.resolved_telegram = dependencies.telegram

    def require_human_session(self, identity: SessionIdentity) -> None:
        if self.is_agent_identity(identity):
            raise DomainRejected(
                "HUMAN_WEB_CONFIRMATION_REQUIRED",
                "this action requires the owner to use an interactive web session",
            )

    def register_auth(self) -> None:
        @self.app.get("/api/auth/status")
        def auth_status() -> dict[str, Any]:
            return {
                "provider": "PASSWORD",
                "provider_configured": True,
                "password_login_available": True,
                "mock_identity_available": (
                    self.resolved_settings.allow_mock_identity
                    and self.resolved_settings.environment in {"local", "test"}
                ),
                "environment": self.resolved_settings.environment,
            }

        @self.app.post("/api/auth/login")
        def password_login(
            payload: PasswordLoginRequest,
            request: Request,
            response: Response,
        ) -> dict[str, Any]:
            username = payload.username.strip()
            client_host = request.client.host if request.client is not None else "unknown"
            limiter_key = f"{client_host}:{username.casefold()}"
            now = _now()
            retry_after = self.login_limiter.retry_after(limiter_key, now=now)
            if retry_after is not None:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={
                        "error_code": "LOGIN_RATE_LIMITED",
                        "message": "登录尝试过多，请稍后再试",  # noqa: RUF001
                    },
                    headers={"Retry-After": str(retry_after)},
                )
            credential = self.queries().password_credential(username)
            encoded = (
                str(credential["password_hash"])
                if credential is not None and credential["password_hash"] is not None
                else self.password_hasher.dummy_hash
            )
            password_valid = self.password_hasher.verify(payload.password, encoded)
            credential_valid = bool(
                credential is not None
                and credential["active"]
                and credential["principal_type"] == "HUMAN"
                and credential["password_hash"] is not None
                and password_valid
            )
            if not credential_valid:
                locked_for = self.login_limiter.fail(limiter_key, now=now)
                if locked_for is not None:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail={
                            "error_code": "LOGIN_RATE_LIMITED",
                            "message": "登录尝试过多，请稍后再试",  # noqa: RUF001
                        },
                        headers={"Retry-After": str(locked_for)},
                    )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={"error_code": "LOGIN_DENIED", "message": "用户名或密码不正确"},
                )
            assert credential is not None
            self.login_limiter.success(limiter_key)
            token = self.token_service.issue_session(
                user_id=credential["user_id"],
                username=str(credential["username"]),
                now=now,
                ttl=timedelta(seconds=self.resolved_settings.session_ttl_seconds),
                authentication_method="password-scrypt",
                auth_version=int(credential["auth_version"]),
            )
            response.set_cookie(
                SESSION_COOKIE,
                token,
                httponly=True,
                secure=self.resolved_settings.environment in {"staging", "production"},
                samesite="strict",
                max_age=self.resolved_settings.session_ttl_seconds,
                path="/",
            )
            return {
                "session": self.queries().user_context(credential["user_id"]),
                "authentication_method": "PASSWORD",
                "expires_at": (
                    now + timedelta(seconds=self.resolved_settings.session_ttl_seconds)
                ).isoformat(),
            }

        @self.app.post("/api/auth/mock/login")
        def mock_login(payload: MockLoginRequest, response: Response) -> dict[str, Any]:
            if not (
                self.resolved_settings.allow_mock_identity
                and self.resolved_settings.environment in {"local", "test"}
            ):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
            user = self.queries().user_by_username(payload.username)
            now = _now()
            token = self.token_service.issue_session(
                user_id=user.user_id,
                username=user.username,
                now=now,
                ttl=timedelta(seconds=self.resolved_settings.session_ttl_seconds),
                authentication_method="mock-internal-user",
                auth_version=user.auth_version,
            )
            response.set_cookie(
                SESSION_COOKIE,
                token,
                httponly=True,
                secure=False,
                samesite="strict",
                max_age=self.resolved_settings.session_ttl_seconds,
                path="/",
            )
            return {
                "session": self.queries().user_context(user.user_id),
                "authentication_method": "MOCK_NON_PRODUCTION",
                "expires_at": (
                    now + timedelta(seconds=self.resolved_settings.session_ttl_seconds)
                ).isoformat(),
            }

        @self.app.post("/api/auth/logout")
        def logout(response: Response) -> dict[str, str]:
            response.delete_cookie(SESSION_COOKIE, path="/")
            return {"status": "logged_out"}

        @self.app.post("/api/auth/password")
        def change_password(
            payload: PasswordChangeRequest,
            response: Response,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_human_session(identity)
            if identity.authentication_method != "password-scrypt":
                raise DomainRejected(
                    "PASSWORD_AUTH_REQUIRED",
                    "password changes require a password-authenticated session",
                )
            now = _now()
            auth_version = self.service().change_own_password(
                actor_id=identity.user_id,
                current_password=payload.current_password,
                new_password=payload.new_password,
                expected_auth_version=payload.expected_auth_version,
                idempotency_key=payload.idempotency_key,
                now=now,
            )
            token = self.token_service.issue_session(
                user_id=identity.user_id,
                username=identity.username,
                now=now,
                ttl=timedelta(seconds=self.resolved_settings.session_ttl_seconds),
                authentication_method="password-scrypt",
                auth_version=auth_version,
            )
            response.set_cookie(
                SESSION_COOKIE,
                token,
                httponly=True,
                secure=self.resolved_settings.environment in {"staging", "production"},
                samesite="strict",
                max_age=self.resolved_settings.session_ttl_seconds,
                path="/",
            )
            return {
                "session": self.queries().user_context(identity.user_id),
                "authentication_method": "PASSWORD",
                "other_sessions_revoked": True,
            }

        @self.app.get("/api/auth/session")
        def auth_session(
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return {
                "session": self.queries().user_context(identity.user_id),
                "authentication_method": identity.authentication_method,
                "expires_at": identity.expires_at.isoformat(),
            }

    def register_scope(self) -> None:
        @self.app.get("/api/scopes")
        def scopes(identity: SessionIdentity = self.identity_dependency) -> dict[str, Any]:
            return {
                "data": self.queries().user_context(identity.user_id),
                "as_of": _now().isoformat(),
            }

        @self.app.post("/api/workspaces")
        def create_workspace(
            payload: WorkspaceCreateRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_human_session(identity)
            workspace_id = self.service().create_workspace(
                actor_id=identity.user_id,
                name=payload.name,
                slug=payload.slug,
                idempotency_key=payload.idempotency_key,
                now=_now(),
            )
            return {
                "workspace_id": str(workspace_id),
                "session": self.queries().user_context(identity.user_id),
            }

        @self.app.post("/api/teams")
        def create_team(
            payload: TeamCreateRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_human_session(identity)
            team_id = self.service().create_team(
                actor_id=identity.user_id,
                name=payload.name,
                slug=payload.slug,
                idempotency_key=payload.idempotency_key,
                now=_now(),
            )
            return {
                "team_id": str(team_id),
                "session": self.queries().user_context(identity.user_id),
            }

        @self.app.get("/api/trading-mode")
        def trading_mode(
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_capability(identity, "venue.view")
            return {
                "data": self.service().trading_mode_status(
                    actor_id=identity.user_id,
                    now=_now(),
                )
            }

        @self.app.put("/api/teams/{team_id}/trading-mode")
        def set_trading_mode(
            team_id: UUID,
            payload: TeamTradingModeRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_human_session(identity)
            result = self.service().set_team_execution_mode(
                actor_id=identity.user_id,
                team_id=team_id,
                mode=payload.mode,
                confirmation=payload.confirmation,
                expected_version=payload.expected_version,
                idempotency_key=payload.idempotency_key,
                now=_now(),
            )
            return {
                "data": result,
                "session": self.queries().user_context(identity.user_id),
            }

        @self.app.post("/api/scopes/select")
        def select_scope(
            payload: ScopeSelectRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_human_session(identity)
            self.service().select_scope(
                actor_id=identity.user_id,
                workspace_id=payload.workspace_id,
                team_id=payload.team_id,
                idempotency_key=payload.idempotency_key,
                now=_now(),
            )
            return {"session": self.queries().user_context(identity.user_id)}

    def register_access(self) -> None:
        @self.app.get("/api/admin/users")
        def managed_users(
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return {
                "data": self.queries().managed_users(identity.user_id),
                "as_of": _now().isoformat(),
            }

        @self.app.post("/api/admin/users")
        def create_managed_user(
            payload: ManagedUserCreateRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_human_session(identity)
            user_id = self.service().create_managed_user(
                payload.username,
                [Role(value) for value in payload.roles],
                identity.user_id,
                payload.account_scope,
                payload.venue_scope,
                payload.password,
                now=_now(),
            )
            return {
                "user_id": str(user_id),
                "data": self.queries().managed_users(identity.user_id),
            }

        @self.app.post("/api/admin/team-members")
        def add_team_member(
            payload: TeamMemberInviteRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            user_id = self.service().add_team_member(
                username=payload.username,
                roles=[Role(value) for value in payload.roles],
                actor_id=identity.user_id,
                account_scope=payload.account_scope,
                venue_scope=payload.venue_scope,
                idempotency_key=payload.idempotency_key,
                now=_now(),
            )
            return {
                "user_id": str(user_id),
                "data": self.queries().managed_users(identity.user_id),
            }

        @self.app.put("/api/admin/users/{user_id}/access")
        def update_managed_user_access(
            user_id: UUID,
            payload: ManagedUserAccessRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_human_session(identity)
            self.service().update_managed_user_access(
                user_id,
                [Role(value) for value in payload.roles],
                payload.active,
                identity.user_id,
                payload.account_scope,
                payload.venue_scope,
                payload.new_password,
                now=_now(),
            )
            return {"user_id": str(user_id), "data": self.queries().managed_users(identity.user_id)}

        @self.app.delete("/api/admin/team-members/{user_id}")
        def remove_team_member(
            user_id: UUID,
            payload: TeamMemberRemoveRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_human_session(identity)
            result = self.service().remove_team_member(
                user_id=user_id,
                actor_id=identity.user_id,
                idempotency_key=payload.idempotency_key,
                now=_now(),
            )
            return {**result, "data": self.queries().managed_users(identity.user_id)}

        @self.app.get("/api/profile/api-keys")
        @self.app.get("/api/profile/api-clients")
        def api_clients(
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_human_session(identity)
            return {
                "data": self.queries().api_clients(identity.user_id, now=_now()),
                "as_of": _now().isoformat(),
            }

        @self.app.get("/api/profile/api-key-contexts")
        @self.app.get("/api/profile/api-client-scopes")
        def api_client_scopes(
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_human_session(identity)
            return {
                "data": self.queries().api_client_scopes(identity.user_id),
                "as_of": _now().isoformat(),
            }

        @self.app.post("/api/profile/api-keys")
        @self.app.post("/api/profile/api-clients")
        def create_api_client(
            payload: ApiClientCreateRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_human_session(identity)
            result = self.service().create_api_client(
                name=payload.name,
                workspace_id=payload.workspace_id,
                team_id=payload.team_id,
                account_id=payload.account_id,
                venue=payload.venue,
                expires_in_days=payload.expires_in_days,
                idempotency_key=payload.idempotency_key,
                actor_id=identity.user_id,
                now=_now(),
            )
            return {
                "result": result,
                "data": self.queries().api_clients(identity.user_id, now=_now()),
            }

        @self.app.put("/api/profile/api-keys/{api_client_id}/state")
        @self.app.put("/api/profile/api-clients/{api_client_id}/state")
        def update_api_client_state(
            api_client_id: UUID,
            payload: ApiClientStateRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_human_session(identity)
            result = self.service().update_api_client_state(
                api_client_id,
                active=payload.active,
                expected_version=payload.expected_version,
                idempotency_key=payload.idempotency_key,
                actor_id=identity.user_id,
                now=_now(),
            )
            return {
                "result": result,
                "data": self.queries().api_clients(identity.user_id, now=_now()),
            }

        @self.app.post("/api/profile/api-keys/{api_client_id}/rotations")
        @self.app.post("/api/profile/api-clients/{api_client_id}/token-rotations")
        def rotate_api_client_token(
            api_client_id: UUID,
            payload: AgentTokenRotationRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_human_session(identity)
            result = self.service().rotate_api_client_token(
                api_client_id,
                expected_token_version=payload.expected_token_version,
                expires_in_days=payload.expires_in_days,
                idempotency_key=payload.idempotency_key,
                actor_id=identity.user_id,
                now=_now(),
            )
            return {
                "result": result,
                "data": self.queries().api_clients(identity.user_id, now=_now()),
            }

        @self.app.post("/api/profile/api-keys/{api_client_id}/revoke")
        @self.app.post("/api/profile/api-clients/{api_client_id}/revoke")
        def revoke_api_client(
            api_client_id: UUID,
            payload: ApiClientRevokeRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_human_session(identity)
            result = self.service().revoke_api_client(
                api_client_id,
                expected_version=payload.expected_version,
                idempotency_key=payload.idempotency_key,
                actor_id=identity.user_id,
                now=_now(),
            )
            return {
                "result": result,
                "data": self.queries().api_clients(identity.user_id, now=_now()),
            }

        @self.app.get("/api/api-key/connection")
        @self.app.get("/api/api-client/connection")
        def api_client_connection(
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            if not self.is_agent_identity(identity):
                raise DomainRejected(
                    "AGENT_IDENTITY_REQUIRED",
                    "this endpoint requires an API Key Bearer credential",
                )
            context = self.queries().user_context(identity.user_id)
            return {
                "connected": True,
                "owner_user_id": str(identity.user_id),
                "api_key_id": str(identity.api_client_id),
                "api_client_id": str(identity.api_client_id),
                "api_key_name": identity.api_client_name,
                "api_client_name": identity.api_client_name,
                "scope": context["api_client_scope"],
                "context": context["api_client_scope"],
                "effective_roles": context["roles"],
                "permissions_source": "HUMAN_DYNAMIC",
                "as_of": _now().isoformat(),
            }

        # Legacy paths remain as read/write compatibility aliases. They now list
        # only the current HUMAN user's clients and never persist independent roles.
        @self.app.get("/api/admin/agents")
        def managed_agents(
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_human_session(identity)
            return {
                "data": self.queries().api_clients(identity.user_id, now=_now()),
                "as_of": _now().isoformat(),
            }

        @self.app.post("/api/admin/agents")
        def create_agent(
            payload: AgentCreateRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_human_session(identity)
            result = self.service().create_agent(
                username=payload.username,
                roles=[Role(value) for value in payload.roles],
                account_scope=payload.account_scope,
                venue_scope=payload.venue_scope,
                expires_in_days=payload.expires_in_days,
                idempotency_key=payload.idempotency_key,
                actor_id=identity.user_id,
                now=_now(),
            )
            return {
                "result": result,
                "data": self.queries().api_clients(identity.user_id, now=_now()),
            }

        @self.app.put("/api/admin/agents/{agent_id}/access")
        def update_agent_access(
            agent_id: UUID,
            payload: AgentAccessRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_human_session(identity)
            result = self.service().update_agent_access(
                agent_id,
                roles=[Role(value) for value in payload.roles],
                active=payload.active,
                account_scope=payload.account_scope,
                venue_scope=payload.venue_scope,
                expected_auth_version=payload.expected_auth_version,
                idempotency_key=payload.idempotency_key,
                actor_id=identity.user_id,
                now=_now(),
            )
            return {
                "result": result,
                "data": self.queries().api_clients(identity.user_id, now=_now()),
            }

        @self.app.post("/api/admin/agents/{agent_id}/token-rotations")
        def rotate_agent_token(
            agent_id: UUID,
            payload: AgentTokenRotationRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_human_session(identity)
            result = self.service().rotate_agent_token(
                agent_id,
                expected_token_version=payload.expected_token_version,
                expires_in_days=payload.expires_in_days,
                idempotency_key=payload.idempotency_key,
                actor_id=identity.user_id,
                now=_now(),
            )
            return {
                "result": result,
                "data": self.queries().api_clients(identity.user_id, now=_now()),
            }

    def register_notification(self) -> None:
        @self.app.post("/api/auth/mock/step-up")
        def mock_step_up(
            payload: MockStepUpRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            if self.is_agent_identity(identity):
                raise DomainRejected(
                    "AGENT_STEP_UP_FORBIDDEN",
                    "Agent credentials cannot mint human action grants",
                )
            if not (
                self.resolved_settings.allow_mock_identity
                and self.resolved_settings.environment in {"local", "test"}
            ):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
            if payload.action == "proposal.approve":
                current_version = self.queries().proposal_version(payload.object_id)
                detail = self.queries().proposal_detail(identity.user_id, payload.object_id)
                review_action = "proposal.review"
            elif payload.action == "capital.approve":
                current_version = self.queries().transfer_proposal_version(
                    identity.user_id,
                    payload.object_id,
                )
                detail = self.queries().transfer_proposal_detail(
                    identity.user_id, payload.object_id
                )
                review_action = "capital.review"
            elif payload.action in {"risk.restore.review", "risk.restore.execute"}:
                current_version = self.service().risk_control_change_version(
                    payload.object_id,
                    identity.user_id,
                )
                detail = {"account_id": None, "venue": None}
                review_action = payload.action
            elif payload.action == "risk.restore.direct":
                status_detail = self.service().risk_control_status(
                    identity.user_id,
                    self.configured_risk_scopes(),
                    require_live_scope=True,
                    now=_now(),
                )
                current_version = int(status_detail["policy"]["revision"])
                if payload.object_id != UUID(str(status_detail["policy"]["policy_id"])):
                    raise DomainRejected("VERSION_CONFLICT", "risk policy changed before step-up")
                detail = {"account_id": None, "venue": None}
                review_action = payload.action
            else:
                raise DomainRejected("STEP_UP_ACTION_INVALID", "step-up action is not supported")
            if current_version != payload.object_version:
                raise DomainRejected("VERSION_CONFLICT", "proposal changed before step-up")
            if not self.service().can_user(
                identity.user_id,
                review_action,
                str(detail["account_id"]),
                str(detail["venue"]),
            ):
                raise DomainRejected("RBAC_DENIED", "approval is outside the current scope")
            token = self.token_service.issue_action_grant(
                user_id=identity.user_id,
                action=payload.action,
                object_id=payload.object_id,
                object_version=payload.object_version,
                now=_now(),
                ttl=timedelta(seconds=self.resolved_settings.action_token_ttl_seconds),
                authentication_method="mock-passkey-step-up",
            )
            return {
                "action_grant": token,
                "authentication_method": "MOCK_PASSKEY_NON_PRODUCTION",
                "expires_in_seconds": self.resolved_settings.action_token_ttl_seconds,
            }

        @self.app.get("/api/notifications")
        def notifications(
            limit: int = Query(default=100, ge=1, le=200),
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return {
                **self.queries().notification_center(identity.user_id, limit=limit),
                "as_of": _now().isoformat(),
            }

        @self.app.post("/api/notification-routes")
        def create_notification_route(
            payload: NotificationRouteWriteRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            result = self.service().configure_notification_route(
                actor_id=identity.user_id,
                notification_route_id=None,
                environment=payload.environment,
                name=payload.name,
                channel=payload.channel,
                event_types=list(payload.event_types),
                enabled=payload.enabled,
                configuration=(
                    None if payload.configuration is None else payload.configuration.plaintext()
                ),
                expected_version=payload.expected_version,
                idempotency_key=payload.idempotency_key,
                now=_now(),
            )
            return {
                "result": result,
                "center": self.queries().notification_center(identity.user_id),
            }

        @self.app.put("/api/notification-routes/{notification_route_id}")
        def update_notification_route(
            notification_route_id: UUID,
            payload: NotificationRouteWriteRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            result = self.service().configure_notification_route(
                actor_id=identity.user_id,
                notification_route_id=notification_route_id,
                environment=payload.environment,
                name=payload.name,
                channel=payload.channel,
                event_types=list(payload.event_types),
                enabled=payload.enabled,
                configuration=(
                    None if payload.configuration is None else payload.configuration.plaintext()
                ),
                expected_version=payload.expected_version,
                idempotency_key=payload.idempotency_key,
                now=_now(),
            )
            return {
                "result": result,
                "center": self.queries().notification_center(identity.user_id),
            }

        @self.app.delete("/api/notification-routes/{notification_route_id}")
        def delete_notification_route(
            notification_route_id: UUID,
            payload: NotificationRouteDeleteRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_human_session(identity)
            result = self.service().delete_notification_route(
                actor_id=identity.user_id,
                notification_route_id=notification_route_id,
                expected_version=payload.expected_version,
                idempotency_key=payload.idempotency_key,
                now=_now(),
            )
            return {
                "result": result,
                "center": self.queries().notification_center(identity.user_id),
            }

        @self.app.post("/api/notification-routes/{notification_route_id}/tests")
        def test_notification_route(
            notification_route_id: UUID,
            payload: NotificationTestRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            event = self.service().enqueue_test_notification(
                actor_id=identity.user_id,
                notification_route_id=notification_route_id,
                idempotency_key=payload.idempotency_key,
                now=now,
            )
            delivery_ids = event["notification_delivery_ids"]
            delivery_status = "UNROUTED" if not delivery_ids else "QUEUED"
            return {
                "event": event,
                "delivery_status": delivery_status,
                "center": self.queries().notification_center(identity.user_id),
            }

        @self.app.get("/api/telegram/mock/notifications")
        def mock_telegram_notifications(
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            if self.resolved_settings.environment not in {"local", "test"}:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
            data = [
                {
                    "notification_id": item.notification_id,
                    "proposal_id": str(item.proposal_id),
                    "proposal_version": item.proposal_version,
                    "environment": item.environment,
                    "summary": item.summary,
                    "review_code": item.review_code,
                    "review_url": item.review_url,
                    "created_at": item.created_at.isoformat(),
                }
                for item in self.resolved_telegram.notifications()
                if item.reviewer_id == identity.user_id
            ]
            return {
                "transport": "MOCK_ONLY",
                "scope": "PROPOSAL_REVIEW_ONLY",
                "data": data,
            }


def register_workspace_routes(context: ApiRouteContext) -> None:
    """Register workspace routes from bounded lifecycle groups."""

    routes = _WorkspaceRoutes(context)
    routes.register_auth()
    routes.register_scope()
    routes.register_access()
    routes.register_notification()
