from __future__ import annotations

from trading_control_plane.service_core import (
    NAMESPACE_URL,
    PASSWORD_HASHER,
    ROLE_ACTIONS,
    SCOPE_SLUG_PATTERN,
    TEAM_SETUP_ACTIONS,
    UUID,
    AccountEquity,
    AccountEquityObservation,
    Any,
    AuditEvent,
    Campaign,
    CapitalAutomationPolicy,
    CapitalTransfer,
    CommandReceipt,
    DirectCapitalOperation,
    DomainRejected,
    ExchangeAccount,
    ExecutionEnvironment,
    FundingPayment,
    IdempotencyConflict,
    NotificationDelivery,
    NotificationRoute,
    OrderIntent,
    Position,
    PrincipalType,
    Proposal,
    ProposalDefaultConfig,
    ProtectionOrder,
    ReconciliationRun,
    RiskDecision,
    RiskPolicy,
    RiskReservation,
    Role,
    RoleAssignment,
    Sequence,
    ServiceMixinBase,
    ServicePrincipalKind,
    Session,
    SignalSourceMode,
    Team,
    TeamExecutionMode,
    TeamMembership,
    TeamSignalSource,
    TradingAuthorization,
    TransferAuthorization,
    TransferProposal,
    User,
    VenueFill,
    VenueOrder,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
    _advisory_lock_key,
    _canonical,
    _normalize_venue_scope,
    _reject,
    _scope_parts,
    _semantic_hash,
    datetime,
    delete,
    func,
    hmac,
    issue_agent_token,
    normalize_notification_event_types,
    notification_template,
    parse_agent_token,
    select,
    text,
    timedelta,
    uuid4,
    uuid5,
    validate_agent_roles,
    validate_notification_configuration,
    validate_notification_payload,
)
from trading_control_plane.service_domains.accounts import AccountServiceMixin


class WorkspaceServiceMixin(ServiceMixinBase):
    """Workspace, membership, identity, notification, and scope transactions."""

    def _audit(
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
        now: datetime,
    ) -> None:
        if workspace_id is None or team_id is None:
            try:
                actor_user_id = UUID(actor_id)
            except ValueError:
                actor_user_id = None
            actor = None if actor_user_id is None else session.get(User, actor_user_id)
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
                    "Proposal": Proposal,
                    "Campaign": Campaign,
                    "VenueOrder": VenueOrder,
                    "VenueFill": VenueFill,
                    "Position": Position,
                    "AccountEquity": AccountEquity,
                    "AccountEquityObservation": AccountEquityObservation,
                    "FundingPayment": FundingPayment,
                    "ProposalDefaultConfig": ProposalDefaultConfig,
                    "TransferProposal": TransferProposal,
                    "TransferAuthorization": TransferAuthorization,
                    "CapitalTransfer": CapitalTransfer,
                    "DirectCapitalOperation": DirectCapitalOperation,
                    "CapitalAutomationPolicy": CapitalAutomationPolicy,
                }
                model = direct_account_models.get(object_type)
                if model is not None:
                    scoped_object = session.get(model, object_uuid)
                elif object_type == "RiskDecision":
                    decision = session.get(RiskDecision, object_uuid)
                    scoped_object = (
                        None if decision is None else session.get(Proposal, decision.proposal_id)
                    )
                elif object_type == "TradingAuthorization":
                    authorization = session.get(TradingAuthorization, object_uuid)
                    scoped_object = (
                        None
                        if authorization is None
                        else session.get(Proposal, authorization.proposal_id)
                    )
                elif object_type == "OrderIntent":
                    intent = session.get(OrderIntent, object_uuid)
                    scoped_object = (
                        None if intent is None else session.get(Campaign, intent.campaign_id)
                    )
                elif object_type == "RiskReservation":
                    reservation = session.get(RiskReservation, object_uuid)
                    scoped_object = (
                        None
                        if reservation is None
                        else session.get(Campaign, reservation.campaign_id)
                    )
                elif object_type == "ProtectionOrder":
                    protection = session.get(ProtectionOrder, object_uuid)
                    scoped_object = (
                        None
                        if protection is None
                        else session.get(Position, protection.position_id)
                    )
                elif object_type == "ReconciliationRun":
                    reconciliation = session.get(ReconciliationRun, object_uuid)
                    if reconciliation is not None and reconciliation.campaign_id is not None:
                        scoped_object = session.get(Campaign, reconciliation.campaign_id)
                    elif reconciliation is not None:
                        try:
                            _environment, account_id, _venue = _scope_parts(
                                reconciliation.execution_scope
                            )
                        except DomainRejected:
                            account_id = None
            if scoped_object is not None:
                account_id = scoped_object.account_id
                scoped_team_id = getattr(scoped_object, "team_id", None)
                if scoped_team_id is not None:
                    team_id = team_id or scoped_team_id
                    if workspace_id is None:
                        scoped_team = session.get(Team, scoped_team_id)
                        workspace_id = None if scoped_team is None else scoped_team.workspace_id
        session.add(
            AuditEvent(
                workspace_id=workspace_id,
                team_id=team_id,
                account_id=account_id,
                actor_id=actor_id,
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

    def _idempotency(
        self,
        session: Session,
        *,
        caller_id: str,
        operation: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any] | None]:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _advisory_lock_key(caller_id, operation, idempotency_key)},
        )
        digest = _semantic_hash(payload)
        receipt = session.scalar(
            select(CommandReceipt).where(
                CommandReceipt.caller_id == caller_id,
                CommandReceipt.operation == operation,
                CommandReceipt.idempotency_key == idempotency_key,
            )
        )
        if receipt is None:
            return digest, None
        if receipt.semantic_hash != digest:
            raise IdempotencyConflict
        return digest, receipt.response

    @staticmethod
    def _save_receipt(
        session: Session,
        *,
        caller_id: str,
        operation: str,
        idempotency_key: str,
        semantic_hash: str,
        response: dict[str, Any],
        now: datetime,
    ) -> None:
        session.add(
            CommandReceipt(
                caller_id=caller_id,
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=semantic_hash,
                response=response,
                created_at=now,
            )
        )

    @staticmethod
    def _active_scope(
        session: Session,
        user_id: UUID,
        *,
        require_team: bool = True,
    ) -> tuple[User, Workspace, Team | None]:
        user = session.get(User, user_id)
        if user is None or not user.active:
            _reject("USER_NOT_AUTHORIZED", "user is missing or inactive")
        if user.active_workspace_id is None:
            _reject("WORKSPACE_CONTEXT_REQUIRED", "select an active workspace")
        workspace = session.get(Workspace, user.active_workspace_id)
        membership = session.scalar(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == user.active_workspace_id,
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.active,
            )
        )
        if workspace is None or not workspace.active or membership is None:
            _reject("WORKSPACE_ACCESS_DENIED", "workspace membership is missing or inactive")
        if user.active_team_id is None:
            if require_team:
                _reject("TEAM_CONTEXT_REQUIRED", "select an active team")
            return user, workspace, None
        team = session.get(Team, user.active_team_id)
        team_membership = session.scalar(
            select(TeamMembership).where(
                TeamMembership.team_id == user.active_team_id,
                TeamMembership.user_id == user_id,
                TeamMembership.active,
            )
        )
        if (
            team is None
            or not team.active
            or team.workspace_id != workspace.workspace_id
            or team_membership is None
        ):
            _reject("TEAM_ACCESS_DENIED", "team membership is missing or inactive")
        return user, workspace, team

    def _require_workspace_admin(
        self,
        session: Session,
        user_id: UUID,
    ) -> tuple[User, Workspace]:
        user, workspace, _team = self._active_scope(session, user_id, require_team=False)
        membership = session.scalar(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace.workspace_id,
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.role == WorkspaceRole.ADMIN.value,
                WorkspaceMembership.active,
            )
        )
        if membership is None:
            _reject("WORKSPACE_ADMIN_REQUIRED", "workspace administration is required")
        return user, workspace

    def _require_role(
        self,
        session: Session,
        user_id: UUID,
        action: str,
        account_id: str | None = None,
        venue: str | None = None,
        *,
        team_id: UUID | None = None,
        allow_setup: bool = False,
    ) -> Team:
        _user, _workspace, team = self._active_scope(session, user_id)
        assert team is not None
        if team_id is not None and team.team_id != team_id:
            _reject("TEAM_SCOPE_DENIED", "resource is outside the active team scope")
        if not team.trading_enabled and action not in TEAM_SETUP_ACTIONS and not allow_setup:
            _reject(
                "TEAM_NOT_OPERATIONAL",
                "team data scope is not ready; configure scoped accounts and risk policy first",
            )
        assignments = session.scalars(
            select(RoleAssignment).where(
                RoleAssignment.user_id == user_id,
                RoleAssignment.team_id == team.team_id,
            )
        ).all()
        for assignment in assignments:
            role = Role(assignment.role)
            if action not in ROLE_ACTIONS[role] and "*" not in ROLE_ACTIONS[role]:
                continue
            if assignment.account_scope is not None and assignment.account_scope != account_id:
                continue
            if assignment.venue_scope is not None and assignment.venue_scope != venue:
                continue
            return team
        _reject("RBAC_DENIED", f"{action} is not allowed in the requested scope")

    @staticmethod
    def _require_team_environment(team: Team, environment: ExecutionEnvironment) -> None:
        mode = (
            TeamExecutionMode.LIVE.value
            if team.execution_mode == TeamExecutionMode.SETUP.value and team.trading_enabled
            else team.execution_mode
        )
        if mode == TeamExecutionMode.SETUP.value:
            _reject(
                "TEAM_SETUP_INCOMPLETE",
                "team must complete setup and explicitly enter SHADOW mode",
            )
        if (
            mode == TeamExecutionMode.SHADOW.value
            and environment is not ExecutionEnvironment.SHADOW
        ):
            _reject(
                "TEAM_SHADOW_ONLY",
                "team is isolated to SHADOW; TESTNET and LIVE workflows are blocked",
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
                self._require_role(session, user_id, action, account_id, venue)
            except DomainRejected:
                return False
            return True

    @staticmethod
    def _require_agent_scope(
        session: Session,
        *,
        team: Team,
        account_id: str,
        venue: str,
    ) -> ExchangeAccount:
        normalized_account_id, normalized_venue, _label = (
            AccountServiceMixin._exchange_account_definition(account_id, venue, account_id)
        )
        account = session.scalar(
            select(ExchangeAccount).where(
                ExchangeAccount.team_id == team.team_id,
                ExchangeAccount.account_id == normalized_account_id,
                ExchangeAccount.venue == normalized_venue,
                ExchangeAccount.active,
            )
        )
        if account is None:
            _reject(
                "AGENT_SCOPE_INVALID",
                "agent access requires an existing active account and exact venue scope",
            )
        return account

    def _agent_token_digest(self, user_id: UUID, version: int, token: str) -> str:
        return self.credential_cipher.secret_fingerprint(
            token,
            purpose=f"agent-api-token:{user_id}:v{version}",
        )

    def authenticate_agent_token(self, token: str, *, now: datetime) -> dict[str, Any]:
        """Authenticate one opaque Agent credential without exposing token material."""

        agent_id = parse_agent_token(token)
        with self.database.session_factory.begin() as session:
            principal = session.get(User, agent_id, with_for_update=True)
            if (
                principal is None
                or not principal.active
                or principal.principal_type != PrincipalType.SERVICE.value
                or principal.service_kind != ServicePrincipalKind.AGENT.value
                or principal.agent_token_digest is None
                or principal.agent_token_expires_at is None
            ):
                _reject("AGENT_TOKEN_INVALID", "agent API credential is invalid")
            expected = self._agent_token_digest(
                principal.user_id,
                principal.agent_token_version,
                token,
            )
            if not hmac.compare_digest(expected, principal.agent_token_digest):
                _reject("AGENT_TOKEN_INVALID", "agent API credential is invalid")
            if principal.agent_token_expires_at <= now:
                _reject("AGENT_TOKEN_EXPIRED", "agent API credential has expired")
            if principal.active_workspace_id is None or principal.active_team_id is None:
                _reject("AGENT_TOKEN_INVALID", "agent API credential is invalid")
            workspace = session.get(Workspace, principal.active_workspace_id)
            team = session.get(Team, principal.active_team_id)
            workspace_membership = session.scalar(
                select(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == principal.active_workspace_id,
                    WorkspaceMembership.user_id == principal.user_id,
                    WorkspaceMembership.active,
                )
            )
            team_membership = session.scalar(
                select(TeamMembership).where(
                    TeamMembership.team_id == principal.active_team_id,
                    TeamMembership.user_id == principal.user_id,
                    TeamMembership.active,
                )
            )
            if (
                workspace is None
                or not workspace.active
                or team is None
                or not team.active
                or team.workspace_id != workspace.workspace_id
                or workspace_membership is None
                or team_membership is None
            ):
                _reject("AGENT_TOKEN_INVALID", "agent API credential is invalid")
            if (
                principal.agent_token_last_used_at is None
                or now - principal.agent_token_last_used_at >= timedelta(minutes=5)
            ):
                principal.agent_token_last_used_at = now
            return {
                "user_id": principal.user_id,
                "username": principal.username,
                "auth_version": principal.auth_version,
                "expires_at": principal.agent_token_expires_at,
            }

    def _require_action_assignment(
        self,
        session: Session,
        user_id: UUID,
        action: str,
    ) -> Team:
        """Require an active-team grant; the eventual write rechecks object scope."""

        _user, _workspace, team = self._active_scope(session, user_id)
        assert team is not None
        if not team.trading_enabled and action not in TEAM_SETUP_ACTIONS:
            _reject(
                "TEAM_NOT_OPERATIONAL",
                "team data scope is not ready; configure scoped accounts and risk policy first",
            )
        assignments = session.scalars(
            select(RoleAssignment).where(
                RoleAssignment.user_id == user_id,
                RoleAssignment.team_id == team.team_id,
            )
        ).all()
        if any(
            action in ROLE_ACTIONS[Role(item.role)] or "*" in ROLE_ACTIONS[Role(item.role)]
            for item in assignments
        ):
            return team
        _reject("RBAC_DENIED", f"{action} is not allowed in the active team")

    def configure_notification_route(
        self,
        *,
        actor_id: UUID,
        notification_route_id: UUID | None,
        name: str,
        channel: str,
        event_types: list[str],
        enabled: bool,
        configuration: dict[str, str] | None,
        expected_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        normalized_name = " ".join(name.strip().split())
        normalized_channel = channel.strip().upper()
        normalized_events = normalize_notification_event_types(event_types)
        if not normalized_name or len(normalized_name) > 120:
            _reject(
                "NOTIFICATION_ROUTE_NAME_INVALID",
                "notification route name must contain 1-120 characters",
            )
        with self.database.session_factory.begin() as session:
            team = self._require_role(session, actor_id, "notification.manage")
            route = (
                None
                if notification_route_id is None
                else session.scalar(
                    select(NotificationRoute)
                    .where(
                        NotificationRoute.notification_route_id == notification_route_id,
                        NotificationRoute.team_id == team.team_id,
                    )
                    .with_for_update()
                )
            )
            if notification_route_id is not None and route is None:
                _reject(
                    "NOTIFICATION_ROUTE_NOT_FOUND",
                    "notification route does not exist in the active team",
                )
            if route is not None and route.channel != normalized_channel:
                _reject(
                    "NOTIFICATION_ROUTE_CHANNEL_IMMUTABLE",
                    "create a new route to change notification channel",
                )
            route_id = (
                uuid5(
                    NAMESPACE_URL,
                    f"tradingops:notification-route:{team.team_id}:{actor_id}:{idempotency_key}",
                )
                if route is None
                else route.notification_route_id
            )
            if configuration is None:
                if route is None:
                    _reject(
                        "NOTIFICATION_CONFIGURATION_REQUIRED",
                        "a new notification route requires channel configuration",
                    )
                normalized_configuration = None
                configuration_metadata = route.configuration_metadata
                configuration_semantics = f"unchanged:{route.credential_version}"
            else:
                normalized_configuration, configuration_metadata = (
                    validate_notification_configuration(normalized_channel, configuration)
                )
                configuration_semantics = self.credential_cipher.secret_fingerprint(
                    _canonical(normalized_configuration),
                    purpose=(
                        f"notification-route:{team.team_id}:{route_id}:{normalized_channel.lower()}"
                    ),
                )
            payload = {
                "notification_route_id": str(route_id),
                "name": normalized_name,
                "channel": normalized_channel,
                "event_types": normalized_events,
                "enabled": enabled,
                "configuration_semantics": configuration_semantics,
                "expected_version": expected_version,
            }
            caller = f"{actor_id}:{team.team_id}"
            operation = (
                "notification-route.create"
                if route is None
                else f"notification-route.update:{route_id}"
            )
            digest, replay = self._idempotency(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return replay
            current_version = 0 if route is None else route.version
            if current_version != expected_version:
                _reject("VERSION_CONFLICT", "notification route changed before configuration")
            name_conflict = session.scalar(
                select(NotificationRoute.notification_route_id).where(
                    NotificationRoute.team_id == team.team_id,
                    NotificationRoute.name == normalized_name,
                    NotificationRoute.notification_route_id != route_id,
                )
            )
            if name_conflict is not None:
                _reject(
                    "NOTIFICATION_ROUTE_NAME_CONFLICT",
                    "notification route name already exists in the active team",
                )
            credential_version = 1 if route is None else route.credential_version
            ciphertext = None if route is None else route.configuration_ciphertext
            if normalized_configuration is not None:
                credential_version = 1 if route is None else route.credential_version + 1
                encrypted = self.credential_cipher.encrypt_secret(
                    _canonical(normalized_configuration),
                    team_id=team.team_id,
                    object_id=route_id,
                    purpose=f"notification-route:{normalized_channel.lower()}",
                    credential_version=credential_version,
                )
                ciphertext = encrypted.ciphertext
            assert ciphertext is not None
            if route is None:
                route = NotificationRoute(
                    notification_route_id=route_id,
                    team_id=team.team_id,
                    name=normalized_name,
                    channel=normalized_channel,
                    event_types=normalized_events,
                    enabled=enabled,
                    configuration_ciphertext=ciphertext,
                    configuration_metadata=configuration_metadata,
                    credential_version=credential_version,
                    version=1,
                    created_by=actor_id,
                    updated_by=actor_id,
                    created_at=now,
                    updated_at=now,
                )
                session.add(route)
            else:
                route.name = normalized_name
                route.event_types = normalized_events
                route.enabled = enabled
                route.configuration_ciphertext = ciphertext
                route.configuration_metadata = configuration_metadata
                route.credential_version = credential_version
                route.version += 1
                route.updated_by = actor_id
                route.updated_at = now
            session.flush()
            response = {
                "notification_route_id": str(route.notification_route_id),
                "version": route.version,
            }
            self._save_receipt(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=response,
                now=now,
            )
            correlation_id = uuid4()
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="NOTIFICATION_ROUTE_CONFIGURED",
                object_type="NotificationRoute",
                object_id=route.notification_route_id,
                reason=(
                    f"channel={route.channel};enabled={str(route.enabled).lower()};"
                    f"events={','.join(route.event_types)};"
                    f"credential_version={route.credential_version}"
                ),
                correlation_id=correlation_id,
                object_version=route.version,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                now=now,
            )
            return response

    def _enqueue_notification_event(
        self,
        session: Session,
        *,
        actor_id: str,
        team: Team,
        event_type: str,
        payload: dict[str, Any],
        object_type: str,
        object_id: UUID | str,
        object_version: int,
        idempotency_key: str,
        correlation_id: UUID,
        environment: str | None,
        account_id: str | None,
        venue: str | None,
        now: datetime,
        target_route_id: UUID | None = None,
    ) -> dict[str, Any]:
        template = notification_template(event_type)
        normalized_payload = validate_notification_payload(payload)
        event_identity = {
            "event_type": template.event_type,
            "template_key": template.key,
            "template_version": template.version,
            "payload": normalized_payload,
            "object_type": object_type,
            "object_id": str(object_id),
            "object_version": object_version,
            "environment": environment,
            "account_id": account_id,
            "venue": venue,
            "target_route_id": None if target_route_id is None else str(target_route_id),
        }
        caller = f"notification:{team.team_id}"
        operation = f"notification-event:{template.event_type}:{object_type}:{object_id}"
        digest, replay = self._idempotency(
            session,
            caller_id=caller,
            operation=operation,
            idempotency_key=idempotency_key,
            payload=event_identity,
        )
        if replay is not None:
            return replay
        event_id = uuid5(
            NAMESPACE_URL,
            f"tradingops:{team.team_id}:{operation}:{idempotency_key}",
        )
        route_query = select(NotificationRoute).where(
            NotificationRoute.team_id == team.team_id,
            NotificationRoute.enabled,
        )
        if target_route_id is not None:
            route_query = route_query.where(
                NotificationRoute.notification_route_id == target_route_id
            )
        routes = session.scalars(route_query.order_by(NotificationRoute.name)).all()
        if target_route_id is not None and not routes:
            _reject(
                "NOTIFICATION_ROUTE_UNAVAILABLE",
                "notification test route is missing or disabled",
            )
        delivery_ids: list[str] = []
        for route in routes:
            if target_route_id is None and template.event_type not in route.event_types:
                continue
            delivery = NotificationDelivery(
                notification_event_id=event_id,
                team_id=team.team_id,
                notification_route_id=route.notification_route_id,
                route_version=route.version,
                channel=route.channel,
                event_type=template.event_type,
                template_key=template.key,
                template_version=template.version,
                payload=normalized_payload,
                semantic_hash=digest,
                object_type=object_type,
                object_id=str(object_id),
                object_version=object_version,
                environment=environment,
                account_id=account_id,
                venue=venue,
                status="PENDING",
                attempt_count=0,
                max_attempts=5,
                next_attempt_at=now,
                last_attempt_at=None,
                sent_at=None,
                external_delivery_id=None,
                last_error_code=None,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                created_at=now,
                updated_at=now,
            )
            session.add(delivery)
            session.flush()
            delivery_ids.append(str(delivery.notification_delivery_id))
        response = {
            "notification_event_id": str(event_id),
            "notification_delivery_ids": delivery_ids,
            "route_count": len(delivery_ids),
        }
        self._save_receipt(
            session,
            caller_id=caller,
            operation=operation,
            idempotency_key=idempotency_key,
            semantic_hash=digest,
            response=response,
            now=now,
        )
        self._audit(
            session,
            actor_id=actor_id,
            event_type=(
                "NOTIFICATION_EVENT_QUEUED" if delivery_ids else "NOTIFICATION_EVENT_UNROUTED"
            ),
            object_type="NotificationEvent",
            object_id=event_id,
            reason=(
                f"event_type={template.event_type};template={template.key}:v{template.version};"
                f"source={object_type}:{object_id}:v{object_version};routes={len(delivery_ids)}"
            ),
            correlation_id=correlation_id,
            object_version=template.version,
            idempotency_key=idempotency_key,
            workspace_id=team.workspace_id,
            team_id=team.team_id,
            account_id=account_id,
            now=now,
        )
        return response

    def _enqueue_proposal_review_notification(
        self,
        session: Session,
        *,
        actor_id: UUID,
        team: Team,
        proposal: Proposal,
        idempotency_key: str,
        now: datetime,
    ) -> None:
        self._enqueue_notification_event(
            session,
            actor_id=str(actor_id),
            team=team,
            event_type="PROPOSAL_REVIEW_REQUIRED",
            payload={
                "summary": "冻结提案已提交, 等待团队成员独立审核。",
                "environment": proposal.environment,
                "account_id": proposal.account_id,
                "venue": proposal.venue,
                "direction": proposal.direction,
                "risk_tier": proposal.risk_tier,
                "quantity": str(proposal.quantity),
                "max_risk": str(proposal.max_risk),
                "expires_at": proposal.expires_at.isoformat(),
            },
            object_type="Proposal",
            object_id=proposal.proposal_id,
            object_version=proposal.version,
            idempotency_key=idempotency_key,
            correlation_id=proposal.correlation_id,
            environment=proposal.environment,
            account_id=proposal.account_id,
            venue=proposal.venue,
            now=now,
        )

    def _enqueue_campaign_status_notification(
        self,
        session: Session,
        *,
        actor_id: str,
        campaign: Campaign,
        summary: str,
        idempotency_key: str,
        correlation_id: UUID,
        now: datetime,
    ) -> None:
        team = session.get(Team, campaign.team_id)
        assert team is not None
        self._enqueue_notification_event(
            session,
            actor_id=actor_id,
            team=team,
            event_type="CAMPAIGN_STATUS_CHANGED",
            payload={
                "summary": summary,
                "status": campaign.status,
                "environment": campaign.environment,
                "account_id": campaign.account_id,
                "venue": campaign.venue,
                "direction": campaign.direction,
                "target_quantity": str(campaign.current_target_quantity),
            },
            object_type="Campaign",
            object_id=campaign.campaign_id,
            object_version=campaign.target_version,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            environment=campaign.environment,
            account_id=campaign.account_id,
            venue=campaign.venue,
            now=now,
        )

    def enqueue_notification_event(
        self,
        *,
        actor_id: str,
        team_id: UUID,
        event_type: str,
        payload: dict[str, Any],
        object_type: str,
        object_id: UUID | str,
        object_version: int,
        idempotency_key: str,
        correlation_id: UUID,
        environment: str | None,
        account_id: str | None,
        venue: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        with self.database.session_factory.begin() as session:
            team = session.get(Team, team_id)
            if team is None or not team.active:
                _reject("TEAM_SCOPE_DENIED", "notification event team is missing or inactive")
            return self._enqueue_notification_event(
                session,
                actor_id=actor_id,
                team=team,
                event_type=event_type,
                payload=payload,
                object_type=object_type,
                object_id=object_id,
                object_version=object_version,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                environment=environment,
                account_id=account_id,
                venue=venue,
                now=now,
            )

    def enqueue_test_notification(
        self,
        *,
        actor_id: UUID,
        notification_route_id: UUID,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        with self.database.session_factory.begin() as session:
            team = self._require_role(session, actor_id, "notification.manage")
            route = session.scalar(
                select(NotificationRoute).where(
                    NotificationRoute.team_id == team.team_id,
                    NotificationRoute.notification_route_id == notification_route_id,
                )
            )
            if route is None:
                _reject(
                    "NOTIFICATION_ROUTE_NOT_FOUND",
                    "notification route does not exist in the active team",
                )
            return self._enqueue_notification_event(
                session,
                actor_id=str(actor_id),
                team=team,
                event_type="TEST_NOTIFICATION",
                payload={
                    "summary": "这是一条团队通知路由测试, 不包含交易或资金操作。",
                    "route_name": route.name,
                    "channel": route.channel,
                },
                object_type="NotificationRoute",
                object_id=route.notification_route_id,
                object_version=route.version,
                idempotency_key=idempotency_key,
                correlation_id=uuid4(),
                environment=None,
                account_id=None,
                venue=None,
                now=now,
                target_route_id=route.notification_route_id,
            )

    def enqueue_capital_status_notification(
        self,
        *,
        actor_id: UUID,
        team_id: UUID,
        object_id: UUID,
        object_type: str,
        status: str,
        environment: str,
        account_id: str,
        venue: str,
        object_version: int,
        summary: str,
        now: datetime,
    ) -> dict[str, Any]:
        with self.database.session_factory.begin() as session:
            team = self._require_role(
                session,
                actor_id,
                "capital.view",
                account_id,
                venue,
                team_id=team_id,
            )
            notification_key = f"{object_type}:{object_id}:{status}:v{object_version}"
            return self._enqueue_notification_event(
                session,
                actor_id=str(actor_id),
                team=team,
                event_type="CAPITAL_STATUS_CHANGED",
                payload={
                    "summary": summary,
                    "status": status,
                    "environment": environment,
                    "account_id": account_id,
                    "venue": venue,
                },
                object_type=object_type,
                object_id=object_id,
                object_version=object_version,
                idempotency_key=notification_key,
                correlation_id=uuid5(
                    NAMESPACE_URL,
                    f"tradingops:{team.team_id}:capital-notification:{notification_key}",
                ),
                environment=environment,
                account_id=account_id,
                venue=venue,
                now=now,
            )

    def bootstrap_admin(self, username: str, *, now: datetime) -> UUID:
        with self.database.session_factory.begin() as session:
            if session.scalar(select(func.count()).select_from(User)) != 0:
                _reject("BOOTSTRAP_CLOSED", "an administrator already exists")
            user = User(
                username=username,
                principal_type=PrincipalType.HUMAN.value,
                active=True,
                created_at=now,
            )
            session.add(user)
            session.flush()
            workspace = Workspace(
                name="Default Workspace",
                slug="default",
                created_by=user.user_id,
                active=True,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(workspace)
            session.flush()
            team = Team(
                workspace_id=workspace.workspace_id,
                name="Default Team",
                slug="default",
                created_by=user.user_id,
                active=True,
                trading_enabled=True,
                execution_mode=TeamExecutionMode.LIVE.value,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(team)
            session.flush()
            user.active_workspace_id = workspace.workspace_id
            user.active_team_id = team.team_id
            session.add(
                WorkspaceMembership(
                    workspace_id=workspace.workspace_id,
                    user_id=user.user_id,
                    role=WorkspaceRole.ADMIN.value,
                    active=True,
                    invited_by=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                TeamMembership(
                    team_id=team.team_id,
                    user_id=user.user_id,
                    active=True,
                    invited_by=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                RoleAssignment(
                    user_id=user.user_id,
                    team_id=team.team_id,
                    role=Role.SYSTEM_ADMIN.value,
                    account_scope=None,
                    venue_scope=None,
                    created_at=now,
                )
            )
            session.add(
                TeamSignalSource(
                    team_id=team.team_id,
                    mode=SignalSourceMode.PERPTAPE.value,
                    enabled=True,
                    credential_ciphertext=None,
                    credential_metadata={
                        "credential_source": "RUNTIME_FALLBACK",
                        "key_hint": None,
                    },
                    credential_version=0,
                    webhook_max_age_seconds=300,
                    service_principal_id=None,
                    version=1,
                    created_by=user.user_id,
                    updated_by=user.user_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            self._audit(
                session,
                actor_id="bootstrap",
                event_type="USER_BOOTSTRAPPED",
                object_type="User",
                object_id=user.user_id,
                reason="first internal administrator",
                correlation_id=uuid4(),
                object_version=1,
                workspace_id=workspace.workspace_id,
                team_id=team.team_id,
                now=now,
            )
            return user.user_id

    @staticmethod
    def _scope_slug(name: str, slug: str | None) -> tuple[str, str]:
        normalized_name = " ".join(name.strip().split())
        normalized_slug = (slug or normalized_name.lower().replace(" ", "-")).strip("-")
        if not normalized_name or len(normalized_name) > 120:
            _reject("SCOPE_NAME_INVALID", "workspace and team names must contain 1-120 characters")
        if not SCOPE_SLUG_PATTERN.fullmatch(normalized_slug):
            _reject(
                "SCOPE_SLUG_INVALID",
                "scope slug must use lowercase letters, digits, or hyphens",
            )
        return normalized_name, normalized_slug

    def create_workspace(
        self,
        *,
        actor_id: UUID,
        name: str,
        slug: str | None,
        idempotency_key: str,
        now: datetime,
    ) -> UUID:
        normalized_name, normalized_slug = self._scope_slug(name, slug)
        payload = {"name": normalized_name, "slug": normalized_slug}
        with self.database.session_factory.begin() as session:
            actor = session.get(User, actor_id)
            if actor is None or not actor.active:
                _reject("USER_NOT_AUTHORIZED", "user is missing or inactive")
            digest, replay = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation="workspace.create",
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return UUID(str(replay["workspace_id"]))
            if session.scalar(select(Workspace).where(Workspace.slug == normalized_slug)):
                _reject("WORKSPACE_SLUG_CONFLICT", "workspace slug already exists")
            workspace = Workspace(
                name=normalized_name,
                slug=normalized_slug,
                created_by=actor_id,
                active=True,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(workspace)
            session.flush()
            session.add(
                WorkspaceMembership(
                    workspace_id=workspace.workspace_id,
                    user_id=actor_id,
                    role=WorkspaceRole.ADMIN.value,
                    active=True,
                    invited_by=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            team = Team(
                workspace_id=workspace.workspace_id,
                name=normalized_name,
                slug="default",
                created_by=actor_id,
                active=True,
                trading_enabled=False,
                execution_mode=TeamExecutionMode.SETUP.value,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(team)
            session.flush()
            session.add_all(
                [
                    TeamMembership(
                        team_id=team.team_id,
                        user_id=actor_id,
                        active=True,
                        invited_by=None,
                        created_at=now,
                        updated_at=now,
                    ),
                    RoleAssignment(
                        user_id=actor_id,
                        team_id=team.team_id,
                        role=Role.SYSTEM_ADMIN.value,
                        account_scope=None,
                        venue_scope=None,
                        created_at=now,
                    ),
                ]
            )
            actor.active_workspace_id = workspace.workspace_id
            actor.active_team_id = team.team_id
            response = {
                "workspace_id": str(workspace.workspace_id),
                "team_id": str(team.team_id),
            }
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="WORKSPACE_CREATED",
                object_type="Workspace",
                object_id=workspace.workspace_id,
                reason=f"workspace={normalized_slug}",
                correlation_id=uuid4(),
                idempotency_key=idempotency_key,
                object_version=1,
                workspace_id=workspace.workspace_id,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="TEAM_CREATED",
                object_type="Team",
                object_id=team.team_id,
                reason="team=default;created_with_workspace=true;trading_enabled=false",
                correlation_id=uuid4(),
                idempotency_key=idempotency_key,
                object_version=1,
                workspace_id=workspace.workspace_id,
                team_id=team.team_id,
                now=now,
            )
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation="workspace.create",
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=response,
                now=now,
            )
            return workspace.workspace_id

    def create_team(
        self,
        *,
        actor_id: UUID,
        name: str,
        slug: str | None,
        idempotency_key: str,
        now: datetime,
    ) -> UUID:
        normalized_name, normalized_slug = self._scope_slug(name, slug)
        with self.database.session_factory.begin() as session:
            actor, workspace = self._require_workspace_admin(session, actor_id)
            payload = {
                "workspace_id": str(workspace.workspace_id),
                "name": normalized_name,
                "slug": normalized_slug,
            }
            caller = f"{actor_id}:{workspace.workspace_id}"
            digest, replay = self._idempotency(
                session,
                caller_id=caller,
                operation="team.create",
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return UUID(str(replay["team_id"]))
            existing = session.scalar(
                select(Team).where(
                    Team.workspace_id == workspace.workspace_id,
                    Team.slug == normalized_slug,
                )
            )
            if existing is not None:
                _reject("TEAM_SLUG_CONFLICT", "team slug already exists in this workspace")
            team = Team(
                workspace_id=workspace.workspace_id,
                name=normalized_name,
                slug=normalized_slug,
                created_by=actor_id,
                active=True,
                trading_enabled=False,
                execution_mode=TeamExecutionMode.SETUP.value,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(team)
            session.flush()
            session.add(
                TeamMembership(
                    team_id=team.team_id,
                    user_id=actor_id,
                    active=True,
                    invited_by=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                RoleAssignment(
                    user_id=actor_id,
                    team_id=team.team_id,
                    role=Role.SYSTEM_ADMIN.value,
                    account_scope=None,
                    venue_scope=None,
                    created_at=now,
                )
            )
            actor.active_team_id = team.team_id
            response = {"team_id": str(team.team_id)}
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="TEAM_CREATED",
                object_type="Team",
                object_id=team.team_id,
                reason=f"team={normalized_slug};trading_enabled=false",
                correlation_id=uuid4(),
                idempotency_key=idempotency_key,
                object_version=1,
                workspace_id=workspace.workspace_id,
                team_id=team.team_id,
                now=now,
            )
            self._save_receipt(
                session,
                caller_id=caller,
                operation="team.create",
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=response,
                now=now,
            )
            return team.team_id

    @staticmethod
    def _shadow_activation_blockers(session: Session, team: Team) -> list[str]:
        blockers: list[str] = []
        source = session.scalar(
            select(TeamSignalSource).where(TeamSignalSource.team_id == team.team_id)
        )
        if source is None or not source.enabled:
            blockers.append("SIGNAL_SOURCE_REQUIRED")
        policy = session.scalar(
            select(RiskPolicy).where(
                RiskPolicy.team_id == team.team_id,
                RiskPolicy.active,
            )
        )
        if policy is None:
            blockers.append("RISK_POLICY_REQUIRED")
        elif any(
            value is None
            for value in (
                policy.max_account_risk,
                policy.max_single_loss,
                policy.max_consecutive_losses,
                policy.loss_cooldown_seconds,
            )
        ):
            blockers.append("RISK_LIMITS_REQUIRED")
        accounts = session.scalars(
            select(ExchangeAccount).where(
                ExchangeAccount.team_id == team.team_id,
                ExchangeAccount.active,
            )
        ).all()
        if not accounts:
            blockers.append("EXCHANGE_ACCOUNT_REQUIRED")

        member_ids = set(
            session.scalars(
                select(TeamMembership.user_id)
                .join(User, User.user_id == TeamMembership.user_id)
                .where(
                    TeamMembership.team_id == team.team_id,
                    TeamMembership.active,
                    User.active,
                )
            )
        )
        assignments = session.scalars(
            select(RoleAssignment).where(
                RoleAssignment.team_id == team.team_id,
                RoleAssignment.user_id.in_(member_ids),
            )
        ).all()

        def scoped_users(action: str, account: ExchangeAccount) -> set[UUID]:
            return {
                assignment.user_id
                for assignment in assignments
                if (
                    assignment.account_scope is None
                    or assignment.account_scope == account.account_id
                )
                and (assignment.venue_scope is None or assignment.venue_scope == account.venue)
                and (
                    action in ROLE_ACTIONS[Role(assignment.role)]
                    or "*" in ROLE_ACTIONS[Role(assignment.role)]
                )
            }

        independent_ready = any(
            any(
                proposer != reviewer
                for proposer in scoped_users("proposal.create", account)
                for reviewer in scoped_users("proposal.review", account)
            )
            for account in accounts
        )
        operator_ready = any(scoped_users("order.prepare", account) for account in accounts)
        if not independent_ready:
            blockers.append("INDEPENDENT_REVIEWER_REQUIRED")
        if not operator_ready:
            blockers.append("OPERATOR_REQUIRED")
        return blockers

    def shadow_activation_status(self, actor_id: UUID) -> dict[str, Any]:
        with self.database.session_factory() as session:
            team = self._require_role(session, actor_id, "team.view")
            assignments = session.scalars(
                select(RoleAssignment).where(
                    RoleAssignment.team_id == team.team_id,
                    RoleAssignment.user_id == actor_id,
                )
            ).all()
            blockers = self._shadow_activation_blockers(session, team)
            return {
                "workspace_id": str(team.workspace_id),
                "team_id": str(team.team_id),
                "team_name": team.name,
                "execution_mode": team.execution_mode,
                "trading_enabled": team.trading_enabled,
                "version": team.version,
                "blockers": blockers,
                "ready": not blockers,
                "can_activate": any(
                    "team.manage" in ROLE_ACTIONS[Role(item.role)]
                    or "*" in ROLE_ACTIONS[Role(item.role)]
                    for item in assignments
                ),
            }

    def activate_team_shadow_mode(
        self,
        *,
        actor_id: UUID,
        team_id: UUID,
        expected_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        operation = "team.shadow.activate"
        payload = {
            "team_id": str(team_id),
            "expected_version": expected_version,
            "execution_mode": TeamExecutionMode.SHADOW.value,
        }
        with self.database.session_factory.begin() as session:
            active_team = self._require_role(session, actor_id, "team.manage", team_id=team_id)
            digest, replay = self._idempotency(
                session,
                caller_id=f"{actor_id}:{active_team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return replay
            team = session.get(Team, team_id, with_for_update=True)
            if team is None or team.team_id != active_team.team_id:
                _reject("TEAM_SCOPE_DENIED", "team is outside the active scope")
            if team.version != expected_version:
                _reject("VERSION_CONFLICT", "team changed before SHADOW activation")
            if team.execution_mode == TeamExecutionMode.LIVE.value:
                _reject("TEAM_MODE_TRANSITION_INVALID", "LIVE teams do not downgrade here")
            already_active = team.execution_mode == TeamExecutionMode.SHADOW.value
            if not already_active:
                blockers = self._shadow_activation_blockers(session, team)
                if blockers:
                    _reject(
                        "TEAM_SHADOW_PREREQUISITES_MISSING",
                        ",".join(blockers),
                    )
                team.execution_mode = TeamExecutionMode.SHADOW.value
                team.trading_enabled = True
                team.version += 1
                team.updated_at = now
            result = {
                "team_id": str(team.team_id),
                "execution_mode": team.execution_mode,
                "trading_enabled": team.trading_enabled,
                "version": team.version,
            }
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{active_team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="TEAM_SHADOW_MODE_ACTIVATED",
                object_type="Team",
                object_id=team.team_id,
                reason=(
                    "already active; no state change"
                    if already_active
                    else "setup passed; LIVE, funding, signing and broadcast remain off"
                ),
                correlation_id=uuid4(),
                object_version=team.version,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                now=now,
            )
            return result

    def select_scope(
        self,
        *,
        actor_id: UUID,
        workspace_id: UUID,
        team_id: UUID | None,
        idempotency_key: str,
        now: datetime,
    ) -> None:
        payload = {
            "workspace_id": str(workspace_id),
            "team_id": None if team_id is None else str(team_id),
        }
        with self.database.session_factory.begin() as session:
            actor = session.get(User, actor_id, with_for_update=True)
            if actor is None or not actor.active:
                _reject("USER_NOT_AUTHORIZED", "user is missing or inactive")
            digest, replay = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation="scope.select",
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return
            workspace_membership = session.scalar(
                select(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.user_id == actor_id,
                    WorkspaceMembership.active,
                )
            )
            workspace = session.get(Workspace, workspace_id)
            if workspace is None or not workspace.active or workspace_membership is None:
                _reject("WORKSPACE_ACCESS_DENIED", "workspace membership is missing or inactive")
            if team_id is not None:
                team = session.get(Team, team_id)
                team_membership = session.scalar(
                    select(TeamMembership).where(
                        TeamMembership.team_id == team_id,
                        TeamMembership.user_id == actor_id,
                        TeamMembership.active,
                    )
                )
                if (
                    team is None
                    or not team.active
                    or team.workspace_id != workspace_id
                    or team_membership is None
                ):
                    _reject("TEAM_ACCESS_DENIED", "team membership is missing or inactive")
            actor.active_workspace_id = workspace_id
            actor.active_team_id = team_id
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="SCOPE_SELECTED",
                object_type="Workspace" if team_id is None else "Team",
                object_id=workspace_id if team_id is None else team_id,
                reason="active workspace/team scope changed",
                correlation_id=uuid4(),
                idempotency_key=idempotency_key,
                object_version=1,
                workspace_id=workspace_id,
                team_id=team_id,
                now=now,
            )
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation="scope.select",
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=payload,
                now=now,
            )

    def create_user(self, username: str, actor_id: UUID, *, now: datetime) -> UUID:
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "user.manage")
            _actor, workspace, team = self._active_scope(session, actor_id)
            assert team is not None
            user = User(
                username=username,
                active_workspace_id=workspace.workspace_id,
                active_team_id=team.team_id,
                principal_type=PrincipalType.HUMAN.value,
                active=True,
                created_at=now,
            )
            session.add(user)
            session.flush()
            session.add_all(
                [
                    WorkspaceMembership(
                        workspace_id=workspace.workspace_id,
                        user_id=user.user_id,
                        role=WorkspaceRole.MEMBER.value,
                        active=True,
                        invited_by=actor_id,
                        created_at=now,
                        updated_at=now,
                    ),
                    TeamMembership(
                        team_id=team.team_id,
                        user_id=user.user_id,
                        active=True,
                        invited_by=actor_id,
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="USER_CREATED",
                object_type="User",
                object_id=user.user_id,
                reason="internal user created",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return user.user_id

    def create_managed_user(
        self,
        username: str,
        roles: Sequence[Role],
        actor_id: UUID,
        account_scope: str | None = None,
        venue_scope: str | None = None,
        password: str | None = None,
        *,
        now: datetime,
    ) -> UUID:
        normalized_username = username.strip()
        normalized_roles = tuple(dict.fromkeys(roles))
        normalized_venue_scope = _normalize_venue_scope(venue_scope)
        if not normalized_username or not normalized_roles:
            _reject("USER_ACCESS_INVALID", "an active user requires a username and role")
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "user.manage")
            _actor, workspace, team = self._active_scope(session, actor_id)
            assert team is not None
            if session.scalar(select(User).where(User.username == normalized_username)) is not None:
                _reject("USERNAME_CONFLICT", "the internal username already exists")
            user = User(
                username=normalized_username,
                password_hash=(PASSWORD_HASHER.hash(password) if password is not None else None),
                password_changed_at=(now if password is not None else None),
                active_workspace_id=workspace.workspace_id,
                active_team_id=team.team_id,
                principal_type=PrincipalType.HUMAN.value,
                active=True,
                created_at=now,
            )
            session.add(user)
            session.flush()
            session.add_all(
                [
                    WorkspaceMembership(
                        workspace_id=workspace.workspace_id,
                        user_id=user.user_id,
                        role=WorkspaceRole.MEMBER.value,
                        active=True,
                        invited_by=actor_id,
                        created_at=now,
                        updated_at=now,
                    ),
                    TeamMembership(
                        team_id=team.team_id,
                        user_id=user.user_id,
                        active=True,
                        invited_by=actor_id,
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )
            for role in normalized_roles:
                session.add(
                    RoleAssignment(
                        user_id=user.user_id,
                        team_id=team.team_id,
                        role=role.value,
                        account_scope=None if role is Role.SYSTEM_ADMIN else account_scope,
                        venue_scope=(None if role is Role.SYSTEM_ADMIN else normalized_venue_scope),
                        created_at=now,
                    )
                )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="USER_ACCESS_CREATED",
                object_type="User",
                object_id=user.user_id,
                reason=f"roles={','.join(sorted(role.value for role in normalized_roles))}",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return user.user_id

    def add_team_member(
        self,
        *,
        username: str,
        roles: Sequence[Role],
        actor_id: UUID,
        account_scope: str | None,
        venue_scope: str | None,
        idempotency_key: str,
        now: datetime,
    ) -> UUID:
        normalized_username = username.strip()
        normalized_roles = tuple(dict.fromkeys(roles))
        normalized_venue_scope = _normalize_venue_scope(venue_scope)
        if not normalized_username or not normalized_roles:
            _reject("TEAM_INVITE_INVALID", "a team invitation requires a username and role")
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "user.manage")
            _actor, workspace, team = self._active_scope(session, actor_id)
            assert team is not None
            payload = {
                "workspace_id": str(workspace.workspace_id),
                "team_id": str(team.team_id),
                "username": normalized_username,
                "roles": sorted(role.value for role in normalized_roles),
                "account_scope": account_scope,
                "venue_scope": normalized_venue_scope,
            }
            caller = f"{actor_id}:{workspace.workspace_id}:{team.team_id}"
            digest, replay = self._idempotency(
                session,
                caller_id=caller,
                operation="team.member.add",
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return UUID(str(replay["user_id"]))
            user = session.scalar(
                select(User).where(
                    User.username == normalized_username,
                    User.principal_type == PrincipalType.HUMAN.value,
                    User.active,
                )
            )
            if user is None:
                _reject("USER_NOT_FOUND", "invitee must already have an active account")
            team_membership = session.scalar(
                select(TeamMembership).where(
                    TeamMembership.team_id == team.team_id,
                    TeamMembership.user_id == user.user_id,
                )
            )
            if team_membership is not None and team_membership.active:
                _reject("TEAM_MEMBERSHIP_CONFLICT", "user is already active in this team")
            workspace_membership = session.scalar(
                select(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == workspace.workspace_id,
                    WorkspaceMembership.user_id == user.user_id,
                )
            )
            if workspace_membership is None:
                session.add(
                    WorkspaceMembership(
                        workspace_id=workspace.workspace_id,
                        user_id=user.user_id,
                        role=WorkspaceRole.MEMBER.value,
                        active=True,
                        invited_by=actor_id,
                        created_at=now,
                        updated_at=now,
                    )
                )
            elif not workspace_membership.active:
                _reject(
                    "WORKSPACE_MEMBERSHIP_INACTIVE",
                    "workspace membership must be restored by a workspace administrator",
                )
            if team_membership is None:
                team_membership = TeamMembership(
                    team_id=team.team_id,
                    user_id=user.user_id,
                    active=True,
                    invited_by=actor_id,
                    created_at=now,
                    updated_at=now,
                )
                session.add(team_membership)
            else:
                team_membership.active = True
                team_membership.invited_by = actor_id
                team_membership.updated_at = now
            session.execute(
                delete(RoleAssignment).where(
                    RoleAssignment.user_id == user.user_id,
                    RoleAssignment.team_id == team.team_id,
                )
            )
            for role in normalized_roles:
                session.add(
                    RoleAssignment(
                        user_id=user.user_id,
                        team_id=team.team_id,
                        role=role.value,
                        account_scope=None if role is Role.SYSTEM_ADMIN else account_scope,
                        venue_scope=(None if role is Role.SYSTEM_ADMIN else normalized_venue_scope),
                        created_at=now,
                    )
                )
            session.flush()
            response = {"user_id": str(user.user_id)}
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="TEAM_MEMBER_ADDED",
                object_type="TeamMembership",
                object_id=team_membership.membership_id,
                reason=(
                    f"user={user.user_id};roles={','.join(role.value for role in normalized_roles)}"
                ),
                correlation_id=uuid4(),
                idempotency_key=idempotency_key,
                object_version=1,
                workspace_id=workspace.workspace_id,
                team_id=team.team_id,
                now=now,
            )
            self._save_receipt(
                session,
                caller_id=caller,
                operation="team.member.add",
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=response,
                now=now,
            )
            return user.user_id

    def update_managed_user_access(
        self,
        user_id: UUID,
        roles: Sequence[Role],
        active: bool,
        actor_id: UUID,
        account_scope: str | None = None,
        venue_scope: str | None = None,
        new_password: str | None = None,
        *,
        now: datetime,
    ) -> None:
        normalized_roles = tuple(dict.fromkeys(roles))
        normalized_venue_scope = _normalize_venue_scope(venue_scope)
        if active and not normalized_roles:
            _reject("USER_ACCESS_INVALID", "an active user requires at least one role")
        if user_id == actor_id:
            _reject(
                "SELF_ACCESS_CHANGE_DENIED",
                "manage your own access through another system administrator",
            )
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "user.manage")
            _actor, _workspace, team = self._active_scope(session, actor_id)
            assert team is not None
            user = session.get(User, user_id, with_for_update=True)
            if user is None or user.principal_type != PrincipalType.HUMAN.value:
                _reject("USER_NOT_FOUND", "the managed human user does not exist")
            membership = session.scalar(
                select(TeamMembership)
                .where(
                    TeamMembership.team_id == team.team_id,
                    TeamMembership.user_id == user_id,
                )
                .with_for_update()
            )
            if membership is None:
                _reject("TEAM_MEMBER_NOT_FOUND", "the user is not a member of the active team")
            current_assignments = session.scalars(
                select(RoleAssignment).where(
                    RoleAssignment.user_id == user_id,
                    RoleAssignment.team_id == team.team_id,
                )
            ).all()
            current_roles = {Role(item.role) for item in current_assignments}
            removing_admin = Role.SYSTEM_ADMIN in current_roles and (
                Role.SYSTEM_ADMIN not in normalized_roles or not active
            )
            if removing_admin:
                active_admins = session.scalar(
                    select(func.count(func.distinct(User.user_id)))
                    .select_from(User)
                    .join(RoleAssignment, RoleAssignment.user_id == User.user_id)
                    .join(
                        TeamMembership,
                        (TeamMembership.user_id == User.user_id)
                        & (TeamMembership.team_id == RoleAssignment.team_id),
                    )
                    .where(
                        User.active,
                        TeamMembership.active,
                        RoleAssignment.team_id == team.team_id,
                        RoleAssignment.role == Role.SYSTEM_ADMIN.value,
                    )
                )
                if int(active_admins or 0) <= 1:
                    _reject(
                        "LAST_SYSTEM_ADMIN_REQUIRED",
                        "the last active system administrator cannot be removed or disabled",
                    )
            session.execute(
                delete(RoleAssignment).where(
                    RoleAssignment.user_id == user_id,
                    RoleAssignment.team_id == team.team_id,
                )
            )
            for role in normalized_roles:
                session.add(
                    RoleAssignment(
                        user_id=user_id,
                        team_id=team.team_id,
                        role=role.value,
                        account_scope=None if role is Role.SYSTEM_ADMIN else account_scope,
                        venue_scope=(None if role is Role.SYSTEM_ADMIN else normalized_venue_scope),
                        created_at=now,
                    )
                )
            membership.active = active
            membership.updated_at = now
            if not active and user.active_team_id == team.team_id:
                user.active_team_id = None
            password_reset = new_password is not None
            if new_password is not None:
                user.password_hash = PASSWORD_HASHER.hash(new_password)
                user.password_changed_at = now
                user.auth_version += 1
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="USER_ACCESS_UPDATED",
                object_type="User",
                object_id=user_id,
                reason=(
                    f"active={str(active).lower()};"
                    f"roles={','.join(sorted(role.value for role in normalized_roles))};"
                    f"password_reset={str(password_reset).lower()}"
                ),
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )

    def ensure_local_human_password(
        self,
        username: str,
        password: str,
        *,
        now: datetime,
    ) -> bool:
        """Set a local human credential without ever storing or auditing plaintext."""

        with self.database.session_factory.begin() as session:
            user = session.scalar(
                select(User).where(
                    User.username == username,
                    User.principal_type == PrincipalType.HUMAN.value,
                    User.active,
                )
            )
            if user is None:
                _reject("USER_NOT_FOUND", "the local human user does not exist")
            if user.password_hash is not None and PASSWORD_HASHER.verify(
                password, user.password_hash
            ):
                return False
            user.password_hash = PASSWORD_HASHER.hash(password)
            user.password_changed_at = now
            user.auth_version += 1
            self._audit(
                session,
                actor_id="local-setup",
                event_type="USER_PASSWORD_CONFIGURED",
                object_type="User",
                object_id=user.user_id,
                reason="local password credential configured or rotated",
                correlation_id=uuid4(),
                object_version=user.auth_version,
                now=now,
            )
            return True

    def bind_telegram_private_chat(
        self,
        *,
        internal_username: str,
        telegram_username: str,
        telegram_chat_id: str,
        now: datetime,
    ) -> str:
        """Bind one allowlisted private chat to one existing human user.

        The caller performs the Telegram username allowlist check. Once bound, all
        authorization uses the immutable numeric private-chat ID and current Trading RBAC.
        """

        with self.database.session_factory.begin() as session:
            user = session.scalar(
                select(User).where(
                    User.username == internal_username,
                    User.principal_type == PrincipalType.HUMAN.value,
                    User.active,
                )
            )
            if user is None:
                _reject(
                    "TELEGRAM_INTERNAL_USER_NOT_FOUND",
                    "the configured internal Telegram user is missing or inactive",
                )
            bound_to_chat = session.scalar(
                select(User).where(User.telegram_chat_id == telegram_chat_id)
            )
            if bound_to_chat is not None and bound_to_chat.user_id != user.user_id:
                _reject(
                    "TELEGRAM_BINDING_CONFLICT",
                    "the Telegram private chat is already bound to another internal user",
                )
            if user.telegram_chat_id is not None and user.telegram_chat_id != telegram_chat_id:
                _reject(
                    "TELEGRAM_BINDING_CONFLICT",
                    "the internal user already has another Telegram private chat",
                )
            if user.telegram_chat_id == telegram_chat_id:
                return user.username
            user.telegram_chat_id = telegram_chat_id
            self._audit(
                session,
                actor_id=str(user.user_id),
                event_type="TELEGRAM_PRIVATE_CHAT_BOUND",
                object_type="User",
                object_id=user.user_id,
                reason=f"allowlisted Telegram user @{telegram_username} completed private /start",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return user.username

    def create_agent(
        self,
        *,
        username: str,
        roles: Sequence[Role],
        account_scope: str,
        venue_scope: str,
        expires_in_days: int,
        idempotency_key: str,
        actor_id: UUID,
        now: datetime,
    ) -> dict[str, Any]:
        normalized_username = username.strip()
        normalized_roles = validate_agent_roles(tuple(roles))
        if (
            not normalized_username
            or normalized_username != username
            or len(normalized_username) > 120
            or not 1 <= expires_in_days <= 365
        ):
            _reject("AGENT_ACCESS_INVALID", "agent identity or credential lifetime is invalid")
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "user.manage")
            _actor, workspace, team = self._active_scope(session, actor_id)
            assert team is not None
            account = self._require_agent_scope(
                session,
                team=team,
                account_id=account_scope,
                venue=venue_scope,
            )
            caller = f"{actor_id}:{team.team_id}"
            operation = "agent.create"
            payload = {
                "workspace_id": str(workspace.workspace_id),
                "team_id": str(team.team_id),
                "username": normalized_username,
                "roles": sorted(role.value for role in normalized_roles),
                "account_scope": account.account_id,
                "venue_scope": account.venue,
                "expires_in_days": expires_in_days,
            }
            digest, replay = self._idempotency(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return {**replay, "token": None, "display_once": False, "replayed": True}
            if session.scalar(select(User).where(User.username == normalized_username)) is not None:
                _reject("USERNAME_CONFLICT", "the internal username already exists")

            agent_id = uuid4()
            issued = issue_agent_token(agent_id)
            expires_at = now + timedelta(days=expires_in_days)
            token_version = 1
            principal = User(
                user_id=agent_id,
                username=normalized_username,
                active_workspace_id=workspace.workspace_id,
                active_team_id=team.team_id,
                principal_type=PrincipalType.SERVICE.value,
                service_kind=ServicePrincipalKind.AGENT.value,
                agent_token_digest=self._agent_token_digest(
                    agent_id,
                    token_version,
                    issued.token,
                ),
                agent_token_hint=issued.hint,
                agent_token_version=token_version,
                agent_token_created_at=now,
                agent_token_expires_at=expires_at,
                auth_version=1,
                active=True,
                created_at=now,
            )
            session.add(principal)
            session.flush()
            session.add_all(
                [
                    WorkspaceMembership(
                        workspace_id=workspace.workspace_id,
                        user_id=agent_id,
                        role=WorkspaceRole.MEMBER.value,
                        active=True,
                        invited_by=actor_id,
                        created_at=now,
                        updated_at=now,
                    ),
                    TeamMembership(
                        team_id=team.team_id,
                        user_id=agent_id,
                        active=True,
                        invited_by=actor_id,
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )
            for role in normalized_roles:
                session.add(
                    RoleAssignment(
                        user_id=agent_id,
                        team_id=team.team_id,
                        role=role.value,
                        account_scope=account.account_id,
                        venue_scope=account.venue,
                        created_at=now,
                    )
                )
            response = {
                "agent_id": str(agent_id),
                "username": normalized_username,
                "workspace_id": str(workspace.workspace_id),
                "team_id": str(team.team_id),
                "roles": sorted(role.value for role in normalized_roles),
                "account_scope": account.account_id,
                "venue_scope": account.venue,
                "active": True,
                "auth_version": principal.auth_version,
                "token_hint": issued.hint,
                "token_version": token_version,
                "token_created_at": now.isoformat(),
                "token_expires_at": expires_at.isoformat(),
            }
            self._save_receipt(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=response,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="AGENT_CREATED",
                object_type="User",
                object_id=agent_id,
                reason=(
                    f"roles={','.join(sorted(role.value for role in normalized_roles))};"
                    f"scope={account.account_id}:{account.venue};token_version=1"
                ),
                correlation_id=uuid4(),
                object_version=principal.auth_version,
                idempotency_key=idempotency_key,
                workspace_id=workspace.workspace_id,
                team_id=team.team_id,
                account_id=account.account_id,
                now=now,
            )
            return {
                **response,
                "token": issued.token,
                "display_once": True,
                "replayed": False,
            }

    def update_agent_access(
        self,
        agent_id: UUID,
        *,
        roles: Sequence[Role],
        active: bool,
        account_scope: str,
        venue_scope: str,
        expected_auth_version: int,
        idempotency_key: str,
        actor_id: UUID,
        now: datetime,
    ) -> dict[str, Any]:
        normalized_roles = validate_agent_roles(tuple(roles), active=active)
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "user.manage")
            _actor, workspace, team = self._active_scope(session, actor_id)
            assert team is not None
            account = self._require_agent_scope(
                session,
                team=team,
                account_id=account_scope,
                venue=venue_scope,
            )
            caller = f"{actor_id}:{team.team_id}"
            operation = f"agent.access:{agent_id}"
            payload = {
                "agent_id": str(agent_id),
                "roles": sorted(role.value for role in normalized_roles),
                "active": active,
                "account_scope": account.account_id,
                "venue_scope": account.venue,
                "expected_auth_version": expected_auth_version,
            }
            digest, replay = self._idempotency(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return replay
            principal = session.get(User, agent_id, with_for_update=True)
            membership = session.scalar(
                select(TeamMembership)
                .where(
                    TeamMembership.team_id == team.team_id,
                    TeamMembership.user_id == agent_id,
                )
                .with_for_update()
            )
            if (
                principal is None
                or principal.principal_type != PrincipalType.SERVICE.value
                or principal.service_kind != ServicePrincipalKind.AGENT.value
                or principal.active_workspace_id != workspace.workspace_id
                or principal.active_team_id != team.team_id
                or membership is None
            ):
                _reject("AGENT_NOT_FOUND", "agent is outside the active team or does not exist")
            if principal.auth_version != expected_auth_version:
                _reject("VERSION_CONFLICT", "agent access changed; refresh before updating")
            workspace_membership = session.scalar(
                select(WorkspaceMembership)
                .where(
                    WorkspaceMembership.workspace_id == workspace.workspace_id,
                    WorkspaceMembership.user_id == agent_id,
                )
                .with_for_update()
            )
            if workspace_membership is None:
                _reject("AGENT_NOT_FOUND", "agent workspace membership is missing")
            session.execute(
                delete(RoleAssignment).where(
                    RoleAssignment.user_id == agent_id,
                    RoleAssignment.team_id == team.team_id,
                )
            )
            for role in normalized_roles:
                session.add(
                    RoleAssignment(
                        user_id=agent_id,
                        team_id=team.team_id,
                        role=role.value,
                        account_scope=account.account_id,
                        venue_scope=account.venue,
                        created_at=now,
                    )
                )
            principal.active = active
            principal.auth_version += 1
            membership.active = active
            membership.updated_at = now
            workspace_membership.active = active
            workspace_membership.updated_at = now
            response = {
                "agent_id": str(agent_id),
                "active": active,
                "auth_version": principal.auth_version,
                "roles": sorted(role.value for role in normalized_roles),
                "account_scope": account.account_id,
                "venue_scope": account.venue,
            }
            self._save_receipt(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=response,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="AGENT_ACCESS_UPDATED",
                object_type="User",
                object_id=agent_id,
                reason=(
                    f"active={str(active).lower()};"
                    f"roles={','.join(sorted(role.value for role in normalized_roles))};"
                    f"scope={account.account_id}:{account.venue}"
                ),
                correlation_id=uuid4(),
                object_version=principal.auth_version,
                idempotency_key=idempotency_key,
                workspace_id=workspace.workspace_id,
                team_id=team.team_id,
                account_id=account.account_id,
                now=now,
            )
            return response

    def rotate_agent_token(
        self,
        agent_id: UUID,
        *,
        expected_token_version: int,
        expires_in_days: int,
        idempotency_key: str,
        actor_id: UUID,
        now: datetime,
    ) -> dict[str, Any]:
        if not 1 <= expires_in_days <= 365:
            _reject("AGENT_ACCESS_INVALID", "agent credential lifetime is invalid")
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "user.manage")
            _actor, workspace, team = self._active_scope(session, actor_id)
            assert team is not None
            caller = f"{actor_id}:{team.team_id}"
            operation = f"agent.token.rotate:{agent_id}"
            payload = {
                "agent_id": str(agent_id),
                "expected_token_version": expected_token_version,
                "expires_in_days": expires_in_days,
            }
            digest, replay = self._idempotency(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return {**replay, "token": None, "display_once": False, "replayed": True}
            principal = session.get(User, agent_id, with_for_update=True)
            membership = session.scalar(
                select(TeamMembership).where(
                    TeamMembership.team_id == team.team_id,
                    TeamMembership.user_id == agent_id,
                )
            )
            if (
                principal is None
                or principal.principal_type != PrincipalType.SERVICE.value
                or principal.service_kind != ServicePrincipalKind.AGENT.value
                or principal.active_workspace_id != workspace.workspace_id
                or principal.active_team_id != team.team_id
                or membership is None
            ):
                _reject("AGENT_NOT_FOUND", "agent is outside the active team or does not exist")
            if principal.agent_token_version != expected_token_version:
                _reject("VERSION_CONFLICT", "agent token changed; refresh before rotating")
            next_version = principal.agent_token_version + 1
            issued = issue_agent_token(agent_id)
            expires_at = now + timedelta(days=expires_in_days)
            principal.agent_token_digest = self._agent_token_digest(
                agent_id,
                next_version,
                issued.token,
            )
            principal.agent_token_hint = issued.hint
            principal.agent_token_version = next_version
            principal.agent_token_created_at = now
            principal.agent_token_expires_at = expires_at
            principal.agent_token_last_used_at = None
            principal.auth_version += 1
            response = {
                "agent_id": str(agent_id),
                "token_hint": issued.hint,
                "token_version": next_version,
                "token_created_at": now.isoformat(),
                "token_expires_at": expires_at.isoformat(),
                "auth_version": principal.auth_version,
            }
            self._save_receipt(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=response,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="AGENT_TOKEN_ROTATED",
                object_type="User",
                object_id=agent_id,
                reason=f"token_version={next_version};previous credential revoked",
                correlation_id=uuid4(),
                object_version=principal.auth_version,
                idempotency_key=idempotency_key,
                workspace_id=workspace.workspace_id,
                team_id=team.team_id,
                now=now,
            )
            return {
                **response,
                "token": issued.token,
                "display_once": True,
                "replayed": False,
            }

    def create_service_principal(self, username: str, actor_id: UUID, *, now: datetime) -> UUID:
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "user.manage")
            _actor, workspace, team = self._active_scope(session, actor_id)
            assert team is not None
            principal = User(
                username=username,
                active_workspace_id=workspace.workspace_id,
                active_team_id=team.team_id,
                principal_type=PrincipalType.SERVICE.value,
                service_kind=ServicePrincipalKind.INTERNAL.value,
                active=True,
                created_at=now,
            )
            session.add(principal)
            session.flush()
            session.add_all(
                [
                    WorkspaceMembership(
                        workspace_id=workspace.workspace_id,
                        user_id=principal.user_id,
                        role=WorkspaceRole.MEMBER.value,
                        active=True,
                        invited_by=actor_id,
                        created_at=now,
                        updated_at=now,
                    ),
                    TeamMembership(
                        team_id=team.team_id,
                        user_id=principal.user_id,
                        active=True,
                        invited_by=actor_id,
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="SERVICE_PRINCIPAL_CREATED",
                object_type="User",
                object_id=principal.user_id,
                reason="internal strategy or service principal created",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return principal.user_id

    def assign_role(
        self,
        user_id: UUID,
        role: Role,
        actor_id: UUID,
        account_scope: str | None = None,
        venue_scope: str | None = None,
        *,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "role.manage")
            _actor, _workspace, team = self._active_scope(session, actor_id)
            assert team is not None
            if session.get(User, user_id) is None:
                _reject("USER_NOT_FOUND", "role target does not exist")
            membership = session.scalar(
                select(TeamMembership).where(
                    TeamMembership.team_id == team.team_id,
                    TeamMembership.user_id == user_id,
                    TeamMembership.active,
                )
            )
            if membership is None:
                _reject("TEAM_MEMBER_NOT_FOUND", "role target is not active in this team")
            assignment = RoleAssignment(
                user_id=user_id,
                team_id=team.team_id,
                role=role.value,
                account_scope=account_scope,
                venue_scope=venue_scope,
                created_at=now,
            )
            session.add(assignment)
            session.flush()
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="ROLE_ASSIGNED",
                object_type="RoleAssignment",
                object_id=assignment.assignment_id,
                reason=f"{role.value} assigned",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return assignment.assignment_id
