from __future__ import annotations

from trading_control_plane.api_core import (
    SESSION_COOKIE,
    UUID,
    AgentAccessRequest,
    AgentCreateRequest,
    AgentTokenRotationRequest,
    Any,
    DomainRejected,
    HTTPException,
    ManagedUserAccessRequest,
    ManagedUserCreateRequest,
    MockLoginRequest,
    MockStepUpRequest,
    NotificationRouteWriteRequest,
    NotificationTestRequest,
    PasswordLoginRequest,
    Query,
    Request,
    Response,
    Role,
    ScopeSelectRequest,
    SessionIdentity,
    ShadowScopeInitializeRequest,
    TeamCreateRequest,
    TeamMemberInviteRequest,
    TeamShadowActivationRequest,
    WorkspaceCreateRequest,
    _now,
    status,
    timedelta,
)
from trading_control_plane.api_routes.context import ApiRouteContext


def register_workspace_routes(context: ApiRouteContext) -> None:
    """Register workspace routes against one application dependency context."""

    app = context.app
    configured_risk_scopes = context.require("configured_risk_scopes")
    identity_dependency = context.require("identity_dependency")
    is_agent_identity = context.require("is_agent_identity")
    login_limiter = context.require("login_limiter")
    password_hasher = context.require("password_hasher")
    queries = context.require("queries")
    require_capability = context.require("require_capability")
    resolved_settings = context.require("resolved_settings")
    resolved_telegram = context.require("resolved_telegram")
    service = context.require("service")
    token_service = context.require("token_service")

    @app.get("/api/auth/status")
    def auth_status() -> dict[str, Any]:
        return {
            "provider": "PASSWORD",
            "provider_configured": True,
            "password_login_available": True,
            "mock_identity_available": (
                resolved_settings.allow_mock_identity
                and resolved_settings.environment in {"local", "test"}
            ),
            "environment": resolved_settings.environment,
        }

    @app.post("/api/auth/login")
    def password_login(
        payload: PasswordLoginRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        username = payload.username.strip()
        client_host = request.client.host if request.client is not None else "unknown"
        limiter_key = f"{client_host}:{username.casefold()}"
        now = _now()
        retry_after = login_limiter.retry_after(limiter_key, now=now)
        if retry_after is not None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error_code": "LOGIN_RATE_LIMITED",
                    "message": "登录尝试过多，请稍后再试",  # noqa: RUF001
                },
                headers={"Retry-After": str(retry_after)},
            )
        credential = queries().password_credential(username)
        encoded = (
            str(credential["password_hash"])
            if credential is not None and credential["password_hash"] is not None
            else password_hasher.dummy_hash
        )
        password_valid = password_hasher.verify(payload.password, encoded)
        credential_valid = bool(
            credential is not None
            and credential["active"]
            and credential["principal_type"] == "HUMAN"
            and credential["password_hash"] is not None
            and password_valid
        )
        if not credential_valid:
            locked_for = login_limiter.fail(limiter_key, now=now)
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
        login_limiter.success(limiter_key)
        token = token_service.issue_session(
            user_id=credential["user_id"],
            username=str(credential["username"]),
            now=now,
            ttl=timedelta(seconds=resolved_settings.session_ttl_seconds),
            authentication_method="password-scrypt",
            auth_version=int(credential["auth_version"]),
        )
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            secure=resolved_settings.environment in {"staging", "production"},
            samesite="strict",
            max_age=resolved_settings.session_ttl_seconds,
            path="/",
        )
        return {
            "session": queries().user_context(credential["user_id"]),
            "authentication_method": "PASSWORD",
            "expires_at": (
                now + timedelta(seconds=resolved_settings.session_ttl_seconds)
            ).isoformat(),
        }

    @app.post("/api/auth/mock/login")
    def mock_login(payload: MockLoginRequest, response: Response) -> dict[str, Any]:
        if not (
            resolved_settings.allow_mock_identity
            and resolved_settings.environment in {"local", "test"}
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        user = queries().user_by_username(payload.username)
        now = _now()
        token = token_service.issue_session(
            user_id=user.user_id,
            username=user.username,
            now=now,
            ttl=timedelta(seconds=resolved_settings.session_ttl_seconds),
            authentication_method="mock-internal-user",
            auth_version=user.auth_version,
        )
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            secure=False,
            samesite="strict",
            max_age=resolved_settings.session_ttl_seconds,
            path="/",
        )
        return {
            "session": queries().user_context(user.user_id),
            "authentication_method": "MOCK_NON_PRODUCTION",
            "expires_at": (
                now + timedelta(seconds=resolved_settings.session_ttl_seconds)
            ).isoformat(),
        }

    @app.post("/api/auth/logout")
    def logout(response: Response) -> dict[str, str]:
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"status": "logged_out"}

    @app.get("/api/auth/session")
    def auth_session(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return {
            "session": queries().user_context(identity.user_id),
            "authentication_method": identity.authentication_method,
            "expires_at": identity.expires_at.isoformat(),
        }

    @app.get("/api/scopes")
    def scopes(identity: SessionIdentity = identity_dependency) -> dict[str, Any]:
        return {"data": queries().user_context(identity.user_id), "as_of": _now().isoformat()}

    @app.post("/api/workspaces")
    def create_workspace(
        payload: WorkspaceCreateRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        workspace_id = service().create_workspace(
            actor_id=identity.user_id,
            name=payload.name,
            slug=payload.slug,
            idempotency_key=payload.idempotency_key,
            now=_now(),
        )
        return {
            "workspace_id": str(workspace_id),
            "session": queries().user_context(identity.user_id),
        }

    @app.post("/api/teams")
    def create_team(
        payload: TeamCreateRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        team_id = service().create_team(
            actor_id=identity.user_id,
            name=payload.name,
            slug=payload.slug,
            idempotency_key=payload.idempotency_key,
            now=_now(),
        )
        return {
            "team_id": str(team_id),
            "session": queries().user_context(identity.user_id),
        }

    @app.get("/api/shadow")
    def shadow_workspace(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_capability(identity, "venue.view")
        return {"data": queries().shadow_workspace(identity.user_id), "as_of": _now().isoformat()}

    @app.post("/api/teams/{team_id}/shadow-activation")
    def activate_shadow_mode(
        team_id: UUID,
        payload: TeamShadowActivationRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        result = service().activate_team_shadow_mode(
            actor_id=identity.user_id,
            team_id=team_id,
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            now=_now(),
        )
        return {
            "data": result,
            "session": queries().user_context(identity.user_id),
        }

    @app.post("/api/shadow/scopes")
    def initialize_shadow_scope(
        payload: ShadowScopeInitializeRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        result = service().initialize_shadow_scope(
            actor_id=identity.user_id,
            account_id=payload.account_id,
            venue=payload.venue,
            instrument_id=payload.instrument_id,
            currency=payload.currency,
            initial_equity=payload.initial_equity,
            idempotency_key=payload.idempotency_key,
            now=_now(),
        )
        return {
            "result": result,
            "data": queries().shadow_workspace(identity.user_id),
        }

    @app.post("/api/scopes/select")
    def select_scope(
        payload: ScopeSelectRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        service().select_scope(
            actor_id=identity.user_id,
            workspace_id=payload.workspace_id,
            team_id=payload.team_id,
            idempotency_key=payload.idempotency_key,
            now=_now(),
        )
        return {"session": queries().user_context(identity.user_id)}

    @app.get("/api/admin/users")
    def managed_users(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return {"data": queries().managed_users(identity.user_id), "as_of": _now().isoformat()}

    @app.post("/api/admin/users")
    def create_managed_user(
        payload: ManagedUserCreateRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        user_id = service().create_managed_user(
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
            "data": queries().managed_users(identity.user_id),
        }

    @app.post("/api/admin/team-members")
    def add_team_member(
        payload: TeamMemberInviteRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        user_id = service().add_team_member(
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
            "data": queries().managed_users(identity.user_id),
        }

    @app.put("/api/admin/users/{user_id}/access")
    def update_managed_user_access(
        user_id: UUID,
        payload: ManagedUserAccessRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        service().update_managed_user_access(
            user_id,
            [Role(value) for value in payload.roles],
            payload.active,
            identity.user_id,
            payload.account_scope,
            payload.venue_scope,
            payload.new_password,
            now=_now(),
        )
        return {"user_id": str(user_id), "data": queries().managed_users(identity.user_id)}

    @app.get("/api/admin/agents")
    def managed_agents(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return {
            "data": queries().managed_agents(identity.user_id, now=_now()),
            "as_of": _now().isoformat(),
        }

    @app.post("/api/admin/agents")
    def create_agent(
        payload: AgentCreateRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        result = service().create_agent(
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
            "data": queries().managed_agents(identity.user_id, now=_now()),
        }

    @app.put("/api/admin/agents/{agent_id}/access")
    def update_agent_access(
        agent_id: UUID,
        payload: AgentAccessRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        result = service().update_agent_access(
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
            "data": queries().managed_agents(identity.user_id, now=_now()),
        }

    @app.post("/api/admin/agents/{agent_id}/token-rotations")
    def rotate_agent_token(
        agent_id: UUID,
        payload: AgentTokenRotationRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        result = service().rotate_agent_token(
            agent_id,
            expected_token_version=payload.expected_token_version,
            expires_in_days=payload.expires_in_days,
            idempotency_key=payload.idempotency_key,
            actor_id=identity.user_id,
            now=_now(),
        )
        return {
            "result": result,
            "data": queries().managed_agents(identity.user_id, now=_now()),
        }

    @app.post("/api/auth/mock/step-up")
    def mock_step_up(
        payload: MockStepUpRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        if is_agent_identity(identity):
            raise DomainRejected(
                "AGENT_STEP_UP_FORBIDDEN",
                "Agent credentials cannot mint human action grants",
            )
        if not (
            resolved_settings.allow_mock_identity
            and resolved_settings.environment in {"local", "test"}
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if payload.action == "proposal.approve":
            current_version = queries().proposal_version(payload.object_id)
            detail = queries().proposal_detail(identity.user_id, payload.object_id)
            review_action = "proposal.review"
        elif payload.action == "capital.approve":
            current_version = queries().transfer_proposal_version(
                identity.user_id,
                payload.object_id,
            )
            detail = queries().transfer_proposal_detail(identity.user_id, payload.object_id)
            review_action = "capital.review"
        elif payload.action in {"risk.restore.review", "risk.restore.execute"}:
            current_version = service().risk_control_change_version(
                payload.object_id,
                identity.user_id,
            )
            detail = {"account_id": None, "venue": None}
            review_action = payload.action
        elif payload.action == "risk.restore.direct":
            status_detail = service().risk_control_status(
                identity.user_id,
                configured_risk_scopes(),
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
        if not service().can_user(
            identity.user_id,
            review_action,
            str(detail["account_id"]),
            str(detail["venue"]),
        ):
            raise DomainRejected("RBAC_DENIED", "approval is outside the current scope")
        token = token_service.issue_action_grant(
            user_id=identity.user_id,
            action=payload.action,
            object_id=payload.object_id,
            object_version=payload.object_version,
            now=_now(),
            ttl=timedelta(seconds=resolved_settings.action_token_ttl_seconds),
            authentication_method="mock-passkey-step-up",
        )
        return {
            "action_grant": token,
            "authentication_method": "MOCK_PASSKEY_NON_PRODUCTION",
            "expires_in_seconds": resolved_settings.action_token_ttl_seconds,
        }

    @app.get("/api/notifications")
    def notifications(
        limit: int = Query(default=100, ge=1, le=200),
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return {
            **queries().notification_center(identity.user_id, limit=limit),
            "as_of": _now().isoformat(),
        }

    @app.post("/api/notification-routes")
    def create_notification_route(
        payload: NotificationRouteWriteRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        result = service().configure_notification_route(
            actor_id=identity.user_id,
            notification_route_id=None,
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
            "center": queries().notification_center(identity.user_id),
        }

    @app.put("/api/notification-routes/{notification_route_id}")
    def update_notification_route(
        notification_route_id: UUID,
        payload: NotificationRouteWriteRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        result = service().configure_notification_route(
            actor_id=identity.user_id,
            notification_route_id=notification_route_id,
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
            "center": queries().notification_center(identity.user_id),
        }

    @app.post("/api/notification-routes/{notification_route_id}/tests")
    def test_notification_route(
        notification_route_id: UUID,
        payload: NotificationTestRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        event = service().enqueue_test_notification(
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
            "center": queries().notification_center(identity.user_id),
        }

    @app.get("/api/telegram/mock/notifications")
    def mock_telegram_notifications(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        if resolved_settings.environment not in {"local", "test"}:
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
            for item in resolved_telegram.notifications()
            if item.reviewer_id == identity.user_id
        ]
        return {
            "transport": "MOCK_ONLY",
            "scope": "PROPOSAL_REVIEW_ONLY",
            "data": data,
        }
