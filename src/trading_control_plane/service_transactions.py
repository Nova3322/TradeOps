from __future__ import annotations

from trading_control_plane.credentials import CredentialCipher
from trading_control_plane.database import Database

# ruff: noqa: F403, F405
from trading_control_plane.service_core import *


class TransactionService:
    """Shared fail-closed transaction primitives; owns no domain state or projections."""

    def __init__(self, database: Database, credential_cipher: CredentialCipher) -> None:
        self.database = database
        self.credential_cipher = credential_cipher

    @staticmethod
    def _lock_risk_capacity(session: Session, team_id: UUID | None = None) -> None:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {
                "key": (
                    RISK_CAPACITY_LOCK_KEY
                    if team_id is None
                    else _advisory_lock_key(str(team_id), "risk-capacity", "team")
                )
            },
        )

    @staticmethod
    def _validate_sender_lease(
        session: Session,
        team_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        now: datetime,
    ) -> None:
        _scope_parts(execution_scope)
        lease = session.get(SenderLease, (team_id, execution_scope))
        if (
            lease is None
            or lease.owner_id != owner_id
            or lease.fencing_token != fencing_token
            or lease.expires_at <= now
        ):
            FENCING_REJECTIONS.inc()
            _reject("FENCING_TOKEN_REJECTED", "sender lease is stale, expired, or superseded")

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
        environment: str | None = None,
        generation: int | None = None,
        rule_summary: dict[str, Any] | None = None,
        now: datetime,
    ) -> None:
        api_context = current_api_client_context()
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
                account_id = getattr(
                    scoped_object,
                    "account_id",
                    getattr(scoped_object, "source_account_id", None),
                )
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

    def _idempotency(
        self,
        session: Session,
        *,
        caller_id: str,
        operation: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any] | None]:
        api_context = current_api_client_context()
        if api_context is not None:
            caller_id = f"api-client:{api_context.api_client_id}:{caller_id}"
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

    def _save_receipt(
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
        api_context = current_api_client_context()
        if api_context is not None:
            caller_id = f"api-client:{api_context.api_client_id}:{caller_id}"
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
        api_context = current_api_client_context()
        if api_context is not None and api_context.owner_user_id != user_id:
            _reject("API_CLIENT_SCOPE_DENIED", "API Key owner does not match the request actor")
        workspace_id = (
            api_context.workspace_id if api_context is not None else user.active_workspace_id
        )
        team_id = api_context.team_id if api_context is not None else user.active_team_id
        if workspace_id is None:
            _reject("WORKSPACE_CONTEXT_REQUIRED", "select an active workspace")
        workspace = session.get(Workspace, workspace_id)
        membership = session.scalar(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.active,
            )
        )
        if workspace is None or not workspace.active or membership is None:
            _reject("WORKSPACE_ACCESS_DENIED", "workspace membership is missing or inactive")
        if team_id is None:
            if require_team:
                _reject("TEAM_CONTEXT_REQUIRED", "select an active team")
            return user, workspace, None
        team = session.get(Team, team_id)
        team_membership = session.scalar(
            select(TeamMembership).where(
                TeamMembership.team_id == team_id,
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
        api_context = current_api_client_context()
        if api_context is not None:
            if (
                action in API_CLIENT_HUMAN_ONLY_ACTIONS
                or action not in API_CLIENT_ALLOWED_BUSINESS_ACTIONS
            ):
                _reject(
                    "HUMAN_WEB_CONFIRMATION_REQUIRED",
                    f"{action} requires the owner to use an interactive web session",
                )
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
        mode = team.execution_mode
        if mode == TeamExecutionMode.SETUP.value:
            _reject(
                "TEAM_SETUP_INCOMPLETE",
                "team must complete setup and explicitly select TESTNET or LIVE",
            )
        if environment.value != mode:
            if mode == TeamExecutionMode.TESTNET.value:
                _reject(
                    "TEAM_TESTNET_ONLY",
                    "Team mode is locked to TESTNET; LIVE workflows are blocked",
                )
            _reject(
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
                self._require_role(session, user_id, action, account_id, venue)
            except DomainRejected:
                return False
            return True

    def _require_action_assignment(
        self,
        session: Session,
        user_id: UUID,
        action: str,
        *,
        allow_setup: bool = False,
    ) -> Team:
        """Require an active-team grant; the eventual write rechecks object scope."""

        api_context = current_api_client_context()
        if api_context is not None and (
            action in API_CLIENT_HUMAN_ONLY_ACTIONS
            or action not in API_CLIENT_ALLOWED_BUSINESS_ACTIONS
        ):
            _reject(
                "HUMAN_WEB_CONFIRMATION_REQUIRED",
                f"{action} requires the owner to use an interactive web session",
            )

        _user, _workspace, team = self._active_scope(session, user_id)
        assert team is not None
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
        environment: str = "LIVE",
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
        normalized_environment = environment.strip().upper()
        if normalized_environment not in {"TESTNET", "LIVE"}:
            _reject("NOTIFICATION_ROUTE_INVALID", "environment must be TESTNET or LIVE")
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
                        NotificationRoute.deleted_at.is_(None),
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
            if route is not None and route.environment != normalized_environment:
                _reject(
                    "NOTIFICATION_ROUTE_ENVIRONMENT_IMMUTABLE",
                    "create a new route to change notification environment",
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
                "environment": normalized_environment,
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
                    NotificationRoute.environment == normalized_environment,
                    NotificationRoute.name == normalized_name,
                    NotificationRoute.notification_route_id != route_id,
                    NotificationRoute.deleted_at.is_(None),
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
                    purpose=(
                        f"notification-route:{normalized_channel.lower()}"
                        if normalized_environment == "LIVE"
                        else f"notification-route:testnet:{normalized_channel.lower()}"
                    ),
                    credential_version=credential_version,
                )
                ciphertext = encrypted.ciphertext
            assert ciphertext is not None
            if route is None:
                route = NotificationRoute(
                    notification_route_id=route_id,
                    team_id=team.team_id,
                    environment=normalized_environment,
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
                route.environment = normalized_environment
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

    def delete_notification_route(
        self,
        *,
        actor_id: UUID,
        notification_route_id: UUID,
        expected_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        """Archive a route, clear its secret, and retain immutable delivery history."""

        with self.database.session_factory.begin() as session:
            team = self._require_role(session, actor_id, "notification.manage")
            route = session.scalar(
                select(NotificationRoute)
                .where(
                    NotificationRoute.notification_route_id == notification_route_id,
                    NotificationRoute.team_id == team.team_id,
                )
                .with_for_update()
            )
            if route is None:
                _reject(
                    "NOTIFICATION_ROUTE_NOT_FOUND",
                    "notification route does not exist in the active team",
                )
            caller = f"{actor_id}:{team.team_id}"
            payload = {
                "notification_route_id": str(notification_route_id),
                "expected_version": expected_version,
            }
            digest, replay = self._idempotency(
                session,
                caller_id=caller,
                operation=f"notification-route.delete:{notification_route_id}",
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return replay
            if route.deleted_at is not None:
                _reject(
                    "NOTIFICATION_ROUTE_NOT_FOUND",
                    "notification route does not exist in the active team",
                )
            if route.version != expected_version:
                _reject("VERSION_CONFLICT", "notification route changed before deletion")
            sending = session.scalar(
                select(NotificationDelivery.notification_delivery_id)
                .where(
                    NotificationDelivery.team_id == team.team_id,
                    NotificationDelivery.notification_route_id == notification_route_id,
                    NotificationDelivery.status == "SENDING",
                )
                .limit(1)
            )
            if sending is not None:
                _reject(
                    "NOTIFICATION_ROUTE_DELETE_BLOCKED",
                    "wait for the in-flight notification attempt to reach a known outcome",
                )
            session.execute(
                update(NotificationDelivery)
                .where(
                    NotificationDelivery.team_id == team.team_id,
                    NotificationDelivery.notification_route_id == notification_route_id,
                    NotificationDelivery.status.in_(["PENDING", "RETRY_WAIT"]),
                )
                .values(
                    status="CANCELLED",
                    last_error_code="NOTIFICATION_ROUTE_DELETED",
                    updated_at=now,
                )
            )
            route.enabled = False
            route.configuration_ciphertext = "deleted"
            route.configuration_metadata = {"deleted": True}
            route.deleted_at = now
            route.deleted_by = actor_id
            route.version += 1
            route.updated_by = actor_id
            route.updated_at = now
            response = {
                "notification_route_id": str(notification_route_id),
                "status": "DELETED",
                "version": route.version,
            }
            self._save_receipt(
                session,
                caller_id=caller,
                operation=f"notification-route.delete:{notification_route_id}",
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=response,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="NOTIFICATION_ROUTE_DELETED",
                object_type="NotificationRoute",
                object_id=notification_route_id,
                reason="enabled=false;credential=cleared;delivery_history_retained=true",
                correlation_id=uuid4(),
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
            NotificationRoute.deleted_at.is_(None),
        )
        if environment is not None:
            route_query = route_query.where(NotificationRoute.environment == environment)
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
                    NotificationRoute.deleted_at.is_(None),
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
