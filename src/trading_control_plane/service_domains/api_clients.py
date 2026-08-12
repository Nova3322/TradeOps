from __future__ import annotations

from trading_control_plane.service_component import ServiceComponent

# ruff: noqa: F403, F405
from trading_control_plane.service_core import *


class ApiClientService(ServiceComponent):
    def _token_digest(self, client_id: UUID, version: int, token: str) -> str:
        purpose = (
            "agent-api-token"
            if token.startswith(f"{AGENT_TOKEN_MARKER}.")
            else "api-client-token"
        )
        return self.credential_cipher.secret_fingerprint(
            token,
            purpose=f"{purpose}:{client_id}:v{version}",
        )

    def _owner_scope(
        self,
        session: Session,
        *,
        owner_id: UUID,
        workspace_id: UUID,
        team_id: UUID,
        account_id: str,
        venue: str,
    ) -> tuple[User, Workspace, Team, ExchangeAccount]:
        normalized_account, normalized_venue, _label = self.facade._exchange_account_definition(
            account_id,
            venue,
            account_id,
        )
        owner = session.get(User, owner_id)
        workspace = session.get(Workspace, workspace_id)
        team = session.get(Team, team_id)
        account = session.scalar(
            select(ExchangeAccount).where(
                ExchangeAccount.team_id == team_id,
                ExchangeAccount.account_id == normalized_account,
                ExchangeAccount.venue == normalized_venue,
                ExchangeAccount.active,
            )
        )
        if (
            owner is None
            or not owner.active
            or owner.principal_type != PrincipalType.HUMAN.value
            or workspace is None
            or not workspace.active
            or team is None
            or not team.active
            or team.workspace_id != workspace_id
            or account is None
        ):
            _reject("API_CLIENT_SCOPE_INVALID", "owner or requested scope is unavailable")
        workspace_membership = session.scalar(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id == owner_id,
                WorkspaceMembership.active,
            )
        )
        team_membership = session.scalar(
            select(TeamMembership).where(
                TeamMembership.team_id == team_id,
                TeamMembership.user_id == owner_id,
                TeamMembership.active,
            )
        )
        assignments = session.scalars(
            select(RoleAssignment).where(
                RoleAssignment.user_id == owner_id,
                RoleAssignment.team_id == team_id,
            )
        ).all()
        applicable = [
            assignment
            for assignment in assignments
            if (assignment.account_scope is None or assignment.account_scope == account.account_id)
            and (assignment.venue_scope is None or assignment.venue_scope == account.venue)
        ]
        if workspace_membership is None or team_membership is None or not applicable:
            _reject(
                "API_CLIENT_SCOPE_INVALID",
                "owner has no active membership and business role in the requested scope",
            )
        return owner, workspace, team, account

    @staticmethod
    def _require_human_request() -> None:
        if current_api_client_context() is not None:
            _reject(
                "HUMAN_WEB_CONFIRMATION_REQUIRED",
                "API Client lifecycle changes require an interactive HUMAN session",
            )

    def authenticate_api_client_token(self, token: str, *, now: datetime) -> dict[str, Any]:
        client_id = parse_api_client_token(token)
        with self.database.session_factory.begin() as session:
            client = session.get(ApiClient, client_id, with_for_update=True)
            if client is None or client.state != ApiClientState.ACTIVE.value:
                _reject("AGENT_TOKEN_INVALID", "API Client credential is invalid")
            expected = self._token_digest(client.api_client_id, client.token_version, token)
            if not hmac.compare_digest(expected, client.token_digest):
                _reject("AGENT_TOKEN_INVALID", "API Client credential is invalid")
            if client.token_expires_at <= now:
                _reject("AGENT_TOKEN_EXPIRED", "API Client credential has expired")
            try:
                owner, workspace, team, account = self._owner_scope(
                    session,
                    owner_id=client.owner_user_id,
                    workspace_id=client.workspace_id,
                    team_id=client.team_id,
                    account_id=client.account_id,
                    venue=client.venue,
                )
            except DomainRejected as exc:
                if exc.code == "API_CLIENT_SCOPE_INVALID":
                    _reject(
                        "AGENT_TOKEN_INVALID",
                        "API Client owner permission or scope is no longer active",
                    )
                raise
            client.token_last_used_at = now
            client.updated_at = now
            return {
                "user_id": owner.user_id,
                "username": owner.username,
                "auth_version": owner.auth_version,
                "expires_at": client.token_expires_at,
                "api_client_id": client.api_client_id,
                "api_client_name": client.name,
                "workspace_id": workspace.workspace_id,
                "team_id": team.team_id,
                "account_id": account.account_id,
                "venue": account.venue,
            }

    def create_api_client(
        self,
        *,
        name: str,
        workspace_id: UUID,
        team_id: UUID,
        account_id: str,
        venue: str,
        expires_in_days: int,
        idempotency_key: str,
        actor_id: UUID,
        now: datetime,
    ) -> dict[str, Any]:
        self._require_human_request()
        normalized_name = name.strip()
        if (
            normalized_name != name
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", normalized_name)
            or not 1 <= expires_in_days <= 365
        ):
            _reject("API_CLIENT_INVALID", "client name or credential lifetime is invalid")
        with self.database.session_factory.begin() as session:
            _owner, workspace, team, account = self._owner_scope(
                session,
                owner_id=actor_id,
                workspace_id=workspace_id,
                team_id=team_id,
                account_id=account_id,
                venue=venue,
            )
            caller = f"{actor_id}:api-clients"
            payload = {
                "name": normalized_name,
                "workspace_id": str(workspace.workspace_id),
                "team_id": str(team.team_id),
                "account_id": account.account_id,
                "venue": account.venue,
                "expires_in_days": expires_in_days,
            }
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=caller,
                operation="api-client.create",
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return {**replay, "token": None, "display_once": False, "replayed": True}
            if session.scalar(
                select(ApiClient).where(
                    ApiClient.owner_user_id == actor_id,
                    ApiClient.name == normalized_name,
                )
            ) is not None:
                _reject("API_CLIENT_NAME_CONFLICT", "this API Client name already exists")
            client_id = uuid4()
            issued = issue_api_client_token(client_id)
            expires_at = now + timedelta(days=expires_in_days)
            client = ApiClient(
                api_client_id=client_id,
                owner_user_id=actor_id,
                name=normalized_name,
                workspace_id=workspace.workspace_id,
                team_id=team.team_id,
                account_id=account.account_id,
                venue=account.venue,
                state=ApiClientState.ACTIVE.value,
                token_digest=self._token_digest(client_id, 1, issued.token),
                token_hint=issued.hint,
                token_version=1,
                token_created_at=now,
                token_expires_at=expires_at,
                token_last_used_at=None,
                revoked_at=None,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(client)
            response = self._client_response(client)
            self.transactions._save_receipt(
                session,
                caller_id=caller,
                operation="api-client.create",
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=response,
                now=now,
            )
            self._audit_lifecycle(
                session,
                client=client,
                actor_id=actor_id,
                event_type="API_CLIENT_CREATED",
                reason=f"scope={account.account_id}:{account.venue};token_version=1",
                idempotency_key=idempotency_key,
                now=now,
            )
            return {**response, "token": issued.token, "display_once": True, "replayed": False}

    @staticmethod
    def _client_response(client: ApiClient) -> dict[str, Any]:
        return {
            "api_client_id": str(client.api_client_id),
            "name": client.name,
            "owner_user_id": str(client.owner_user_id),
            "workspace_id": str(client.workspace_id),
            "team_id": str(client.team_id),
            "account_id": client.account_id,
            "venue": client.venue,
            "state": client.state,
            "version": client.version,
            "token_hint": client.token_hint,
            "token_version": client.token_version,
            "token_created_at": client.token_created_at.isoformat(),
            "token_expires_at": client.token_expires_at.isoformat(),
        }

    def _audit_lifecycle(
        self,
        session: Session,
        *,
        client: ApiClient,
        actor_id: UUID,
        event_type: str,
        reason: str,
        idempotency_key: str,
        now: datetime,
    ) -> None:
        self.transactions._audit(
            session,
            actor_id=str(actor_id),
            event_type=event_type,
            object_type="ApiClient",
            object_id=client.api_client_id,
            reason=reason,
            correlation_id=uuid4(),
            object_version=client.version,
            idempotency_key=idempotency_key,
            workspace_id=client.workspace_id,
            team_id=client.team_id,
            account_id=client.account_id,
            now=now,
        )

    def update_api_client_state(
        self,
        api_client_id: UUID,
        *,
        active: bool,
        expected_version: int,
        idempotency_key: str,
        actor_id: UUID,
        now: datetime,
    ) -> dict[str, Any]:
        self._require_human_request()
        with self.database.session_factory.begin() as session:
            client = self._owned_client(session, api_client_id, actor_id, lock=True)
            caller = f"{actor_id}:api-clients"
            operation = f"api-client.state:{api_client_id}"
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                payload={
                    "api_client_id": str(api_client_id),
                    "active": active,
                    "expected_version": expected_version,
                },
            )
            if replay is not None:
                return replay
            if client.state == ApiClientState.REVOKED.value:
                _reject("API_CLIENT_REVOKED", "a revoked API Client cannot be re-enabled")
            if client.version != expected_version:
                _reject("VERSION_CONFLICT", "API Client changed; refresh before updating")
            client.state = (
                ApiClientState.ACTIVE.value if active else ApiClientState.DISABLED.value
            )
            client.version += 1
            client.updated_at = now
            response = {
                "api_client_id": str(client.api_client_id),
                "state": client.state,
                "version": client.version,
            }
            self.transactions._save_receipt(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=response,
                now=now,
            )
            self._audit_lifecycle(
                session,
                client=client,
                actor_id=actor_id,
                event_type="API_CLIENT_STATE_UPDATED",
                reason=f"state={client.state}",
                idempotency_key=idempotency_key,
                now=now,
            )
            return response

    def rotate_api_client_token(
        self,
        api_client_id: UUID,
        *,
        expected_token_version: int,
        expires_in_days: int,
        idempotency_key: str,
        actor_id: UUID,
        now: datetime,
    ) -> dict[str, Any]:
        self._require_human_request()
        if not 1 <= expires_in_days <= 365:
            _reject("API_CLIENT_INVALID", "client credential lifetime is invalid")
        with self.database.session_factory.begin() as session:
            client = self._owned_client(session, api_client_id, actor_id, lock=True)
            caller = f"{actor_id}:api-clients"
            operation = f"api-client.token.rotate:{api_client_id}"
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                payload={
                    "api_client_id": str(api_client_id),
                    "expected_token_version": expected_token_version,
                    "expires_in_days": expires_in_days,
                },
            )
            if replay is not None:
                return {**replay, "token": None, "display_once": False, "replayed": True}
            if client.state == ApiClientState.REVOKED.value:
                _reject("API_CLIENT_REVOKED", "a revoked API Client cannot rotate credentials")
            if client.token_version != expected_token_version:
                _reject("VERSION_CONFLICT", "API Client token changed; refresh before rotating")
            next_version = client.token_version + 1
            issued = issue_api_client_token(api_client_id)
            expires_at = now + timedelta(days=expires_in_days)
            client.token_digest = self._token_digest(api_client_id, next_version, issued.token)
            client.token_hint = issued.hint
            client.token_version = next_version
            client.token_created_at = now
            client.token_expires_at = expires_at
            client.token_last_used_at = None
            client.version += 1
            client.updated_at = now
            response = self._client_response(client)
            self.transactions._save_receipt(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=response,
                now=now,
            )
            self._audit_lifecycle(
                session,
                client=client,
                actor_id=actor_id,
                event_type="API_CLIENT_TOKEN_ROTATED",
                reason=f"token_version={next_version};previous credential revoked",
                idempotency_key=idempotency_key,
                now=now,
            )
            return {**response, "token": issued.token, "display_once": True, "replayed": False}

    def revoke_api_client(
        self,
        api_client_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
        actor_id: UUID,
        now: datetime,
    ) -> dict[str, Any]:
        self._require_human_request()
        with self.database.session_factory.begin() as session:
            client = self._owned_client(session, api_client_id, actor_id, lock=True)
            caller = f"{actor_id}:api-clients"
            operation = f"api-client.revoke:{api_client_id}"
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                payload={
                    "api_client_id": str(api_client_id),
                    "expected_version": expected_version,
                },
            )
            if replay is not None:
                return replay
            if client.version != expected_version:
                _reject("VERSION_CONFLICT", "API Client changed; refresh before revoking")
            if client.state != ApiClientState.REVOKED.value:
                client.state = ApiClientState.REVOKED.value
                client.revoked_at = now
                client.version += 1
                client.updated_at = now
            response = {
                "api_client_id": str(client.api_client_id),
                "state": client.state,
                "version": client.version,
                "revoked_at": None if client.revoked_at is None else client.revoked_at.isoformat(),
            }
            self.transactions._save_receipt(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=response,
                now=now,
            )
            self._audit_lifecycle(
                session,
                client=client,
                actor_id=actor_id,
                event_type="API_CLIENT_REVOKED",
                reason="credential permanently revoked",
                idempotency_key=idempotency_key,
                now=now,
            )
            return response

    @staticmethod
    def _owned_client(
        session: Session,
        api_client_id: UUID,
        owner_id: UUID,
        *,
        lock: bool,
    ) -> ApiClient:
        statement = select(ApiClient).where(
            ApiClient.api_client_id == api_client_id,
            ApiClient.owner_user_id == owner_id,
        )
        if lock:
            statement = statement.with_for_update()
        client = session.scalar(statement)
        if client is None:
            _reject("API_CLIENT_NOT_FOUND", "API Client does not belong to this user")
        return client

    # Compatibility aliases preserve old integrations while changing their
    # authorization semantics to HUMAN inheritance.
    def authenticate_agent_token(self, token: str, *, now: datetime) -> dict[str, Any]:
        return self.authenticate_api_client_token(token, now=now)

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
        del roles
        with self.database.session_factory() as session:
            owner = session.get(User, actor_id)
            if owner is None or owner.active_workspace_id is None or owner.active_team_id is None:
                _reject("TEAM_CONTEXT_REQUIRED", "select an active Workspace and Team")
            workspace_id = owner.active_workspace_id
            team_id = owner.active_team_id
        return self.create_api_client(
            name=username,
            workspace_id=workspace_id,
            team_id=team_id,
            account_id=account_scope,
            venue=venue_scope,
            expires_in_days=expires_in_days,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            now=now,
        )

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
        del roles
        with self.database.session_factory() as session:
            client = self._owned_client(session, agent_id, actor_id, lock=False)
            if client.account_id != account_scope or client.venue != venue_scope:
                _reject("API_CLIENT_SCOPE_IMMUTABLE", "create a new client to change its scope")
        return self.update_api_client_state(
            agent_id,
            active=active,
            expected_version=expected_auth_version,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            now=now,
        )

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
        return self.rotate_api_client_token(
            agent_id,
            expected_token_version=expected_token_version,
            expires_in_days=expires_in_days,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            now=now,
        )
