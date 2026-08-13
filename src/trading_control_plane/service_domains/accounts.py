from __future__ import annotations

from trading_control_plane.service_component import ServiceComponent

# The domain implementation intentionally consumes the explicit service_core export surface.
# ruff: noqa: F403, F405
from trading_control_plane.service_core import *
from trading_control_plane.service_domains.account_registry import (
    delete_exchange_account,
    exchange_account_definition,
)


class AccountService(ServiceComponent):
    _exchange_account_definition = staticmethod(exchange_account_definition)
    delete_exchange_account = delete_exchange_account

    def _ensure_exchange_account_reference(
        self,
        session: Session,
        *,
        team: Team,
        actor_id: UUID,
        account_id: str,
        venue: str,
        now: datetime,
    ) -> ExchangeAccount:
        normalized_account_id, normalized_venue, label = self._exchange_account_definition(
            account_id, venue, account_id
        )
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {
                "key": _advisory_lock_key(
                    str(team.team_id),
                    "exchange-account-reference",
                    f"{normalized_account_id}:{normalized_venue}",
                )
            },
        )
        account = session.scalar(
            select(ExchangeAccount).where(
                ExchangeAccount.team_id == team.team_id,
                ExchangeAccount.account_id == normalized_account_id,
                ExchangeAccount.venue == normalized_venue,
            )
        )
        if account is not None:
            if not account.active:
                _reject("EXCHANGE_ACCOUNT_INACTIVE", "exchange account is inactive")
            return account
        account = ExchangeAccount(
            team_id=team.team_id,
            account_id=normalized_account_id,
            venue=normalized_venue,
            label=label,
            registration_source="WORKFLOW_REFERENCE",
            connection_status="UNCONFIGURED",
            trading_status="DISABLED",
            credentials_ciphertext=None,
            credential_metadata={},
            credential_version=0,
            connection_error_code=None,
            last_connection_check_at=None,
            last_verified_at=None,
            freqtrade_worker_name=None,
            freqtrade_worker_url=None,
            freqtrade_worker_mode="UNCONFIGURED",
            freqtrade_worker_status="UNCONFIGURED",
            freqtrade_auth_ciphertext=None,
            freqtrade_auth_metadata={},
            freqtrade_auth_version=0,
            freqtrade_hip3_dexes=[],
            freqtrade_error_code=None,
            freqtrade_last_check_at=None,
            freqtrade_last_verified_at=None,
            active=True,
            version=1,
            created_by=actor_id,
            updated_by=actor_id,
            created_at=now,
            updated_at=now,
        )
        session.add(account)
        session.flush()
        self.transactions._audit(
            session,
            actor_id=str(actor_id),
            event_type="EXCHANGE_ACCOUNT_REFERENCED",
            object_type="ExchangeAccount",
            object_id=account.exchange_account_id,
            reason=f"venue={normalized_venue};credentials=unconfigured;trading=disabled",
            correlation_id=uuid4(),
            object_version=1,
            workspace_id=team.workspace_id,
            team_id=team.team_id,
            account_id=normalized_account_id,
            now=now,
        )
        return account

    def create_exchange_account(
        self,
        *,
        actor_id: UUID,
        account_id: str,
        venue: str,
        label: str | None,
        credentials: dict[str, str] | None,
        idempotency_key: str,
        now: datetime,
    ) -> UUID:
        normalized_account_id, normalized_venue, normalized_label = (
            self._exchange_account_definition(account_id, venue, label)
        )
        with self.database.session_factory.begin() as session:
            team = self.transactions._require_role(
                session,
                actor_id,
                "account.manage",
                normalized_account_id,
                normalized_venue,
            )
            credential_semantics = (
                None
                if credentials is None
                else self.credential_cipher.exchange_credentials_fingerprint(
                    credentials,
                    venue=normalized_venue,
                    purpose=(
                        f"exchange-account.create:{team.team_id}:"
                        f"{normalized_account_id}:{normalized_venue}"
                    ),
                )
            )
            caller = f"{actor_id}:{team.team_id}"
            payload = {
                "team_id": str(team.team_id),
                "account_id": normalized_account_id,
                "venue": normalized_venue,
                "label": normalized_label,
                "credential_semantics": credential_semantics,
            }
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=caller,
                operation="exchange-account.create",
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return UUID(str(replay["exchange_account_id"]))
            existing = session.scalar(
                select(ExchangeAccount).where(
                    ExchangeAccount.team_id == team.team_id,
                    ExchangeAccount.account_id == normalized_account_id,
                    ExchangeAccount.venue == normalized_venue,
                )
            )
            if existing is not None and existing.active:
                _reject(
                    "EXCHANGE_ACCOUNT_CONFLICT",
                    "that account ID already exists for this exchange in the active team",
                )
            exchange_account_id = (
                existing.exchange_account_id if existing is not None else uuid4()
            )
            encrypted = (
                None
                if credentials is None
                else self.credential_cipher.encrypt(
                    credentials,
                    team_id=team.team_id,
                    exchange_account_id=exchange_account_id,
                    venue=normalized_venue,
                    credential_version=1,
                )
            )
            if existing is None:
                account = ExchangeAccount(
                    exchange_account_id=exchange_account_id,
                    team_id=team.team_id,
                    account_id=normalized_account_id,
                    venue=normalized_venue,
                    label=normalized_label,
                    registration_source="MANUAL",
                    created_by=actor_id,
                    created_at=now,
                )
                session.add(account)
                event_type = "EXCHANGE_ACCOUNT_CREATED"
                object_version = 1
            else:
                account = existing
                event_type = "EXCHANGE_ACCOUNT_RESTORED"
                account.version += 1
                object_version = account.version
            account.label = normalized_label
            account.registration_source = "MANUAL"
            account.connection_status = "UNCONFIGURED" if encrypted is None else "NOT_VERIFIED"
            account.trading_status = "DISABLED"
            account.credentials_ciphertext = None if encrypted is None else encrypted.ciphertext
            account.credential_metadata = {} if encrypted is None else encrypted.metadata
            account.credential_version = 0 if encrypted is None else 1
            account.connection_error_code = None
            account.last_connection_check_at = None
            account.last_verified_at = None
            account.runtime_sync_enabled = False
            account.freqtrade_worker_name = None
            account.freqtrade_worker_url = None
            account.freqtrade_worker_mode = "UNCONFIGURED"
            account.freqtrade_worker_status = "UNCONFIGURED"
            account.freqtrade_auth_ciphertext = None
            account.freqtrade_auth_metadata = {}
            account.freqtrade_auth_version = 0
            account.freqtrade_hip3_dexes = []
            account.freqtrade_error_code = None
            account.freqtrade_last_check_at = None
            account.freqtrade_last_verified_at = None
            account.active = True
            account.updated_by = actor_id
            account.updated_at = now
            response = {"exchange_account_id": str(exchange_account_id)}
            self.transactions._save_receipt(
                session,
                caller_id=caller,
                operation="exchange-account.create",
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=response,
                now=now,
            )
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type=event_type,
                object_type="ExchangeAccount",
                object_id=exchange_account_id,
                reason=(
                    f"venue={normalized_venue};credentials="
                    f"{'configured-unverified' if encrypted is not None else 'unconfigured'};"
                    "trading=disabled"
                ),
                correlation_id=uuid4(),
                object_version=object_version,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                account_id=normalized_account_id,
                now=now,
            )
            return exchange_account_id

    def rotate_exchange_account_credentials(
        self,
        exchange_account_id: UUID,
        *,
        actor_id: UUID,
        credentials: dict[str, str],
        expected_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> int:
        with self.database.session_factory.begin() as session:
            _actor, _workspace, active_team = self.transactions._active_scope(session, actor_id)
            assert active_team is not None
            account = session.scalar(
                select(ExchangeAccount)
                .where(
                    ExchangeAccount.exchange_account_id == exchange_account_id,
                    ExchangeAccount.team_id == active_team.team_id,
                )
                .with_for_update()
            )
            if account is None:
                _reject(
                    "EXCHANGE_ACCOUNT_NOT_FOUND",
                    "exchange account is outside the active team or does not exist",
                )
            self.transactions._require_role(
                session,
                actor_id,
                "account.credentials.manage",
                account.account_id,
                account.venue,
                team_id=account.team_id,
            )
            credential_semantics = self.credential_cipher.exchange_credentials_fingerprint(
                credentials,
                venue=account.venue,
                purpose=f"exchange-account.credentials.rotate:{account.exchange_account_id}",
            )
            caller = f"{actor_id}:{account.team_id}"
            payload = {
                "exchange_account_id": str(exchange_account_id),
                "expected_version": expected_version,
                "credential_semantics": credential_semantics,
            }
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=caller,
                operation="exchange-account.credentials.rotate",
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return int(replay["version"])
            if account.version != expected_version:
                _reject("VERSION_CONFLICT", "exchange account version changed")
            next_credential_version = account.credential_version + 1
            encrypted = self.credential_cipher.encrypt(
                credentials,
                team_id=account.team_id,
                exchange_account_id=account.exchange_account_id,
                venue=account.venue,
                credential_version=next_credential_version,
            )
            account.credentials_ciphertext = encrypted.ciphertext
            account.credential_metadata = encrypted.metadata
            account.credential_version = next_credential_version
            account.connection_status = "NOT_VERIFIED"
            account.connection_error_code = None
            account.last_connection_check_at = None
            account.last_verified_at = None
            account.runtime_sync_enabled = False
            self._set_internal_principal_active(
                session,
                account.runtime_service_principal_id,
                False,
            )
            account.trading_status = "DISABLED"
            account.version += 1
            account.updated_by = actor_id
            account.updated_at = now
            response = {"version": account.version}
            self.transactions._save_receipt(
                session,
                caller_id=caller,
                operation="exchange-account.credentials.rotate",
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=response,
                now=now,
            )
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="EXCHANGE_ACCOUNT_CREDENTIALS_ROTATED",
                object_type="ExchangeAccount",
                object_id=account.exchange_account_id,
                reason=(
                    f"venue={account.venue};credential_version={next_credential_version};"
                    "connection=not-verified;trading=disabled"
                ),
                correlation_id=uuid4(),
                object_version=account.version,
                idempotency_key=idempotency_key,
                workspace_id=active_team.workspace_id,
                team_id=account.team_id,
                account_id=account.account_id,
                now=now,
            )
            return account.version

    @staticmethod
    def _set_internal_principal_active(
        session: Session,
        principal_id: UUID | None,
        active: bool,
    ) -> None:
        if principal_id is None:
            return
        principal = session.scalar(
            select(User).where(User.user_id == principal_id).with_for_update()
        )
        if (
            principal is None
            or principal.principal_type != PrincipalType.SERVICE.value
            or principal.service_kind != ServicePrincipalKind.INTERNAL.value
        ):
            return
        if principal.active != active:
            principal.active = active
            principal.auth_version += 1

    @staticmethod
    def _require_exact_runtime_principal(
        session: Session,
        *,
        principal_id: UUID,
        team: Team,
        role: Role,
        account_id: str | None,
        venue: str | None,
        error_code: str,
        error_message: str,
        lock: bool = False,
        allow_inactive: bool = False,
    ) -> User:
        principal_statement = select(User).where(User.user_id == principal_id)
        workspace_membership_statement = select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == team.workspace_id,
            WorkspaceMembership.user_id == principal_id,
            WorkspaceMembership.active,
        )
        team_membership_statement = select(TeamMembership).where(
            TeamMembership.team_id == team.team_id,
            TeamMembership.user_id == principal_id,
            TeamMembership.active,
        )
        assignments_statement = select(RoleAssignment).where(RoleAssignment.user_id == principal_id)
        if lock:
            principal_statement = principal_statement.with_for_update()
            workspace_membership_statement = workspace_membership_statement.with_for_update()
            team_membership_statement = team_membership_statement.with_for_update()
            assignments_statement = assignments_statement.with_for_update()
        principal = session.scalar(principal_statement)
        workspace_membership = session.scalar(workspace_membership_statement)
        team_membership = session.scalar(team_membership_statement)
        assignments = session.scalars(assignments_statement).all()
        exact_assignment = (
            len(assignments) == 1
            and assignments[0].team_id == team.team_id
            and assignments[0].role == role.value
            and assignments[0].account_scope == account_id
            and assignments[0].venue_scope == venue
        )
        if (
            principal is None
            or principal.principal_type != PrincipalType.SERVICE.value
            or principal.service_kind != ServicePrincipalKind.INTERNAL.value
            or (not principal.active and not allow_inactive)
            or principal.active_workspace_id != team.workspace_id
            or principal.active_team_id != team.team_id
            or workspace_membership is None
            or team_membership is None
            or not exact_assignment
        ):
            _reject(error_code, error_message)
        return principal

    @staticmethod
    def _ensure_account_runtime_service_principal(
        session: Session,
        *,
        team: Team,
        account: ExchangeAccount,
        actor_id: UUID,
        now: datetime,
    ) -> User:
        username = f"runtime-{team.team_id.hex}-{account.exchange_account_id.hex}"
        principal = session.scalar(select(User).where(User.username == username))
        if principal is None:
            principal = User(
                username=username,
                principal_type=PrincipalType.SERVICE.value,
                service_kind=ServicePrincipalKind.INTERNAL.value,
                active_workspace_id=team.workspace_id,
                active_team_id=team.team_id,
                active=True,
                created_at=now,
            )
            session.add(principal)
            session.flush()
            session.add_all(
                [
                    WorkspaceMembership(
                        workspace_id=team.workspace_id,
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
                    RoleAssignment(
                        user_id=principal.user_id,
                        team_id=team.team_id,
                        role=Role.OPERATOR.value,
                        account_scope=account.account_id,
                        venue_scope=account.venue,
                        created_at=now,
                    ),
                ]
            )
        principal = AccountService._require_exact_runtime_principal(
            session,
            principal_id=principal.user_id,
            team=team,
            role=Role.OPERATOR,
            account_id=account.account_id,
            venue=account.venue,
            error_code="RUNTIME_SERVICE_PRINCIPAL_INVALID",
            error_message=(
                "the read-only runtime principal is outside its exact team/account scope"
            ),
            allow_inactive=True,
        )
        AccountService._set_internal_principal_active(session, principal.user_id, True)
        return principal

    def configure_exchange_account_runtime_sync(
        self,
        exchange_account_id: UUID,
        *,
        actor_id: UUID,
        enabled: bool,
        expected_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        operation = "exchange-account.runtime-sync.configure"
        with self.database.session_factory.begin() as session:
            _actor, _workspace, team = self.transactions._active_scope(session, actor_id)
            assert team is not None
            account = session.scalar(
                select(ExchangeAccount)
                .where(
                    ExchangeAccount.exchange_account_id == exchange_account_id,
                    ExchangeAccount.team_id == team.team_id,
                )
                .with_for_update()
            )
            if account is None:
                _reject(
                    "EXCHANGE_ACCOUNT_NOT_FOUND",
                    "exchange account is outside the active team or does not exist",
                )
            self.transactions._require_role(
                session,
                actor_id,
                "account.credentials.manage",
                account.account_id,
                account.venue,
                team_id=account.team_id,
            )
            payload = {
                "exchange_account_id": str(account.exchange_account_id),
                "enabled": enabled,
                "expected_version": expected_version,
            }
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return replay
            if account.version != expected_version:
                _reject("VERSION_CONFLICT", "exchange account version changed")
            if enabled and (
                not account.active
                or account.connection_status != "VERIFIED"
                or account.credentials_ciphertext is None
                or account.credential_version < 1
            ):
                _reject(
                    "RUNTIME_BINDING_NOT_READY",
                    "an active verified account with encrypted credentials is required",
                )
            if enabled:
                principal = self._ensure_account_runtime_service_principal(
                    session,
                    team=team,
                    account=account,
                    actor_id=actor_id,
                    now=now,
                )
                account.runtime_service_principal_id = principal.user_id
            else:
                self._set_internal_principal_active(
                    session,
                    account.runtime_service_principal_id,
                    False,
                )
                if account.trading_status == "ELIGIBLE":
                    account.trading_status = "BLOCKED"
            account.runtime_sync_enabled = enabled
            account.version += 1
            account.updated_by = actor_id
            account.updated_at = now
            result = {
                "exchange_account_id": str(account.exchange_account_id),
                "runtime_sync_enabled": account.runtime_sync_enabled,
                "version": account.version,
            }
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="EXCHANGE_ACCOUNT_RUNTIME_SYNC_CONFIGURED",
                object_type="ExchangeAccount",
                object_id=account.exchange_account_id,
                reason=(
                    f"enabled={str(enabled).lower()};source=database-envelope;"
                    f"trading={account.trading_status.lower()}"
                ),
                correlation_id=uuid4(),
                object_version=account.version,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                account_id=account.account_id,
                now=now,
            )
            return result

    def configure_exchange_account_trading(
        self,
        exchange_account_id: UUID,
        *,
        actor_id: UUID,
        enabled: bool,
        expected_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        """Configure exact-account LIVE eligibility without opening global gates."""

        operation = "exchange-account.trading.configure"
        with self.database.session_factory.begin() as session:
            _actor, workspace, team = self.transactions._active_scope(session, actor_id)
            assert team is not None
            account = session.scalar(
                select(ExchangeAccount)
                .where(
                    ExchangeAccount.exchange_account_id == exchange_account_id,
                    ExchangeAccount.team_id == team.team_id,
                )
                .with_for_update()
            )
            if account is None:
                _reject(
                    "EXCHANGE_ACCOUNT_NOT_FOUND",
                    "exchange account is outside the active team or does not exist",
                )
            self.transactions._require_role(
                session,
                actor_id,
                "account.manage",
                account.account_id,
                account.venue,
                team_id=account.team_id,
            )
            payload = {
                "exchange_account_id": str(account.exchange_account_id),
                "enabled": enabled,
                "expected_version": expected_version,
            }
            caller = f"{actor_id}:{team.team_id}"
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return replay
            if account.version != expected_version:
                _reject("VERSION_CONFLICT", "exchange account version changed")
            if enabled:
                if not team.trading_enabled or team.execution_mode != TeamExecutionMode.LIVE.value:
                    _reject(
                        "TEAM_LIVE_MODE_REQUIRED",
                        "account trading eligibility requires an active LIVE team",
                    )
                if (
                    not account.active
                    or account.connection_status != "VERIFIED"
                    or account.credentials_ciphertext is None
                    or account.credential_version < 1
                    or not account.runtime_sync_enabled
                    or account.runtime_service_principal_id is None
                ):
                    _reject(
                        "ACCOUNT_TRADING_NOT_READY",
                        "verified encrypted credentials and continuous read-only sync are required",
                    )
                assert account.runtime_service_principal_id is not None
                self._require_exact_runtime_principal(
                    session,
                    principal_id=account.runtime_service_principal_id,
                    team=team,
                    role=Role.OPERATOR,
                    account_id=account.account_id,
                    venue=account.venue,
                    error_code="RUNTIME_SERVICE_PRINCIPAL_INVALID",
                    error_message=(
                        "the read-only runtime principal is outside its exact account scope"
                    ),
                )
            account.trading_status = "ELIGIBLE" if enabled else "DISABLED"
            account.version += 1
            account.updated_by = actor_id
            account.updated_at = now
            result = {
                "exchange_account_id": str(account.exchange_account_id),
                "trading_status": account.trading_status,
                "trading_enabled": account.trading_status == "ELIGIBLE",
                "version": account.version,
            }
            self.transactions._save_receipt(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type=(
                    "EXCHANGE_ACCOUNT_TRADING_ELIGIBILITY_ENABLED"
                    if enabled
                    else "EXCHANGE_ACCOUNT_TRADING_ELIGIBILITY_DISABLED"
                ),
                object_type="ExchangeAccount",
                object_id=account.exchange_account_id,
                reason=(
                    f"venue={account.venue};trading={account.trading_status.lower()};"
                    "global_live_send_gate=unchanged"
                ),
                correlation_id=uuid4(),
                object_version=account.version,
                idempotency_key=idempotency_key,
                workspace_id=workspace.workspace_id,
                team_id=team.team_id,
                account_id=account.account_id,
                now=now,
            )
            return result

    @staticmethod
    def _freqtrade_auth_payload(username: str, password: str) -> str:
        normalized_username = username.strip()
        if (
            not normalized_username
            or normalized_username != username
            or len(normalized_username) > 120
        ):
            _reject(
                "FREQTRADE_WORKER_AUTH_INVALID",
                "Freqtrade username must contain 1-120 characters without surrounding whitespace",
            )
        if not password or password.strip() != password or len(password) > 2_048:
            _reject(
                "FREQTRADE_WORKER_AUTH_INVALID",
                "Freqtrade password must be non-empty without surrounding whitespace",
            )
        return json.dumps(
            {"password": password, "username": normalized_username},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    @staticmethod
    def _parse_freqtrade_auth_payload(payload: str) -> tuple[str, str]:
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise DomainRejected(
                "FREQTRADE_WORKER_AUTH_INVALID",
                "Freqtrade worker authentication envelope is invalid",
            ) from exc
        if (
            not isinstance(decoded, dict)
            or set(decoded) != {"username", "password"}
            or not all(isinstance(value, str) for value in decoded.values())
        ):
            _reject(
                "FREQTRADE_WORKER_AUTH_INVALID",
                "Freqtrade worker authentication envelope is invalid",
            )
        return str(decoded["username"]), str(decoded["password"])

    def configure_exchange_account_freqtrade_worker(
        self,
        exchange_account_id: UUID,
        *,
        actor_id: UUID,
        mode: str,
        name: str | None,
        base_url: str | None,
        username: str | None,
        password: str | None,
        hip3_dexes: tuple[str, ...],
        expected_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        """Configure one encrypted Worker binding for one exact exchange account."""

        normalized_mode = mode.upper()
        if normalized_mode not in {"UNCONFIGURED", "DRY_RUN", "LIVE"}:
            _reject("FREQTRADE_WORKER_MODE_INVALID", "Freqtrade worker mode is invalid")
        normalized_name: str | None = None
        normalized_url: str | None = None
        auth_payload: str | None = None
        normalized_hip3: tuple[str, ...] = ()
        if normalized_mode == "UNCONFIGURED":
            if (
                any(value is not None for value in (name, base_url, username, password))
                or hip3_dexes
            ):
                _reject(
                    "FREQTRADE_WORKER_CONFIGURATION_INVALID",
                    "unconfiguring a Freqtrade worker must not include endpoint or credentials",
                )
        else:
            normalized_name = "" if name is None else name.strip()
            if (
                not normalized_name
                or normalized_name != name
                or len(normalized_name) > 120
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", normalized_name) is None
            ):
                _reject(
                    "FREQTRADE_WORKER_NAME_INVALID",
                    "Freqtrade worker name must be a stable identifier",
                )
            try:
                normalized_url = validate_worker_url("" if base_url is None else base_url)
            except ValueError as exc:
                raise DomainRejected("FREQTRADE_WORKER_URL_INVALID", str(exc)) from exc
            if username is None or password is None:
                _reject(
                    "FREQTRADE_WORKER_AUTH_INVALID",
                    "Freqtrade worker username and password are required together",
                )
            auth_payload = self._freqtrade_auth_payload(username, password)
            try:
                normalized_hip3 = parse_hip3_dexes(",".join(hip3_dexes))
            except ValueError as exc:
                raise DomainRejected("FREQTRADE_HIP3_SCOPE_INVALID", str(exc)) from exc
        operation = "exchange-account.freqtrade-worker.configure"
        with self.database.session_factory.begin() as session:
            _actor, workspace, team = self.transactions._active_scope(session, actor_id)
            assert team is not None
            account = session.scalar(
                select(ExchangeAccount)
                .where(
                    ExchangeAccount.exchange_account_id == exchange_account_id,
                    ExchangeAccount.team_id == team.team_id,
                )
                .with_for_update()
            )
            if account is None:
                _reject(
                    "EXCHANGE_ACCOUNT_NOT_FOUND",
                    "exchange account is outside the active team or does not exist",
                )
            self.transactions._require_role(
                session,
                actor_id,
                "account.credentials.manage",
                account.account_id,
                account.venue,
                team_id=account.team_id,
            )
            if normalized_mode != "UNCONFIGURED" and account.venue not in (
                SUPPORTED_EXCHANGE_VENUES
            ):
                _reject(
                    "FREQTRADE_VENUE_UNSUPPORTED",
                    "Freqtrade worker binding requires a supported TradingOPS venue",
                )
            if account.venue != "HYPERLIQUID" and normalized_hip3:
                _reject(
                    "FREQTRADE_HIP3_SCOPE_INVALID",
                    "HIP-3 DEX scope is only valid for Hyperliquid workers",
                )
            auth_semantics = (
                None
                if auth_payload is None
                else self.credential_cipher.secret_fingerprint(
                    auth_payload,
                    purpose=f"freqtrade-worker-auth:{account.exchange_account_id}",
                )
            )
            payload = {
                "exchange_account_id": str(account.exchange_account_id),
                "mode": normalized_mode,
                "name": normalized_name,
                "base_url": normalized_url,
                "hip3_dexes": list(normalized_hip3),
                "expected_version": expected_version,
                "auth_semantics": auth_semantics,
            }
            caller = f"{actor_id}:{team.team_id}"
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return replay
            if account.version != expected_version:
                _reject("VERSION_CONFLICT", "exchange account version changed")
            if normalized_mode == "UNCONFIGURED":
                account.freqtrade_worker_name = None
                account.freqtrade_worker_url = None
                account.freqtrade_worker_mode = "UNCONFIGURED"
                account.freqtrade_worker_status = "UNCONFIGURED"
                account.freqtrade_auth_ciphertext = None
                account.freqtrade_auth_metadata = {}
                account.freqtrade_auth_version = 0
                account.freqtrade_hip3_dexes = []
            else:
                assert (
                    normalized_name is not None
                    and normalized_url is not None
                    and auth_payload is not None
                    and username is not None
                )
                next_auth_version = account.freqtrade_auth_version + 1
                encrypted = self.credential_cipher.encrypt_secret(
                    auth_payload,
                    team_id=account.team_id,
                    object_id=account.exchange_account_id,
                    purpose="freqtrade-worker-auth",
                    credential_version=next_auth_version,
                )
                account.freqtrade_worker_name = normalized_name
                account.freqtrade_worker_url = normalized_url
                account.freqtrade_worker_mode = normalized_mode
                account.freqtrade_worker_status = "NOT_VERIFIED"
                account.freqtrade_auth_ciphertext = encrypted.ciphertext
                account.freqtrade_auth_metadata = {
                    "envelope_version": encrypted.metadata.get("envelope_version"),
                    "purpose": "freqtrade-worker-auth",
                    "username_hint": (
                        username if len(username) <= 2 else f"{username[0]}•••{username[-1]}"
                    ),
                }
                account.freqtrade_auth_version = next_auth_version
                account.freqtrade_hip3_dexes = list(normalized_hip3)
            account.freqtrade_error_code = None
            account.freqtrade_last_check_at = None
            account.freqtrade_last_verified_at = None
            account.version += 1
            account.updated_by = actor_id
            account.updated_at = now
            result = {
                "exchange_account_id": str(account.exchange_account_id),
                "version": account.version,
                "worker": {
                    "mode": account.freqtrade_worker_mode,
                    "status": account.freqtrade_worker_status,
                    "auth_version": account.freqtrade_auth_version,
                },
            }
            self.transactions._save_receipt(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="FREQTRADE_WORKER_CONFIGURED",
                object_type="ExchangeAccount",
                object_id=account.exchange_account_id,
                reason=(
                    f"venue={account.venue};mode={account.freqtrade_worker_mode.lower()};"
                    f"status={account.freqtrade_worker_status.lower()};"
                    "global_live_send_gate=unchanged"
                ),
                correlation_id=uuid4(),
                object_version=account.version,
                idempotency_key=idempotency_key,
                workspace_id=workspace.workspace_id,
                team_id=team.team_id,
                account_id=account.account_id,
                now=now,
            )
            return result

    @staticmethod
    def _freqtrade_verification_payload(
        exchange_account_id: UUID,
        expected_version: int,
    ) -> dict[str, Any]:
        return {
            "exchange_account_id": str(exchange_account_id),
            "expected_version": expected_version,
        }

    def prepare_exchange_account_freqtrade_verification(
        self,
        exchange_account_id: UUID,
        *,
        actor_id: UUID,
        expected_version: int,
        idempotency_key: str,
    ) -> tuple[PreparedFreqtradeWorkerBinding | None, dict[str, Any] | None]:
        with self.database.session_factory.begin() as session:
            _actor, workspace, team = self.transactions._active_scope(session, actor_id)
            assert team is not None
            account = session.scalar(
                select(ExchangeAccount).where(
                    ExchangeAccount.exchange_account_id == exchange_account_id,
                    ExchangeAccount.team_id == team.team_id,
                )
            )
            if account is None:
                _reject(
                    "EXCHANGE_ACCOUNT_NOT_FOUND",
                    "exchange account is outside the active team or does not exist",
                )
            self.transactions._require_role(
                session,
                actor_id,
                "account.credentials.manage",
                account.account_id,
                account.venue,
                team_id=account.team_id,
            )
            _digest, replay = self.transactions._idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation="exchange-account.freqtrade-worker.verify",
                idempotency_key=idempotency_key,
                payload=self._freqtrade_verification_payload(
                    exchange_account_id,
                    expected_version,
                ),
            )
            if replay is not None:
                return None, replay
            if account.version != expected_version:
                _reject("VERSION_CONFLICT", "exchange account version changed")
            if not account.active:
                _reject("EXCHANGE_ACCOUNT_INACTIVE", "exchange account is inactive")
            return self._prepared_freqtrade_worker_binding(account, workspace.workspace_id), None

    def _prepared_freqtrade_worker_binding(
        self,
        account: ExchangeAccount,
        workspace_id: UUID,
        *,
        require_live_verified: bool = False,
    ) -> PreparedFreqtradeWorkerBinding:
        if (
            account.venue not in SUPPORTED_EXCHANGE_VENUES
            or account.freqtrade_worker_mode == "UNCONFIGURED"
            or account.freqtrade_worker_name is None
            or account.freqtrade_worker_url is None
            or account.freqtrade_auth_ciphertext is None
            or account.freqtrade_auth_version < 1
        ):
            _reject(
                "FREQTRADE_WORKER_NOT_CONFIGURED",
                "the exact exchange account has no configured Freqtrade worker",
            )
        if require_live_verified and (
            account.freqtrade_worker_mode != "LIVE" or account.freqtrade_worker_status != "VERIFIED"
        ):
            _reject(
                "FREQTRADE_WORKER_NOT_VERIFIED",
                "LIVE execution requires a verified LIVE worker bound to the exact account",
            )
        payload = self.credential_cipher.decrypt_secret(
            account.freqtrade_auth_ciphertext,
            team_id=account.team_id,
            object_id=account.exchange_account_id,
            purpose="freqtrade-worker-auth",
            credential_version=account.freqtrade_auth_version,
        )
        username, password = self._parse_freqtrade_auth_payload(payload)
        return PreparedFreqtradeWorkerBinding(
            exchange_account_id=account.exchange_account_id,
            workspace_id=workspace_id,
            team_id=account.team_id,
            account_id=account.account_id,
            venue=account.venue,
            account_version=account.version,
            worker_name=account.freqtrade_worker_name,
            worker_url=account.freqtrade_worker_url,
            worker_mode=account.freqtrade_worker_mode,
            worker_status=account.freqtrade_worker_status,
            auth_version=account.freqtrade_auth_version,
            username=username,
            password=password,
            hip3_dexes=tuple(account.freqtrade_hip3_dexes or []),
        )

    def record_exchange_account_freqtrade_verification(
        self,
        binding: PreparedFreqtradeWorkerBinding,
        *,
        actor_id: UUID,
        error_code: str | None,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        normalized_error = error_code
        if normalized_error is not None and (
            CONNECTION_ERROR_CODE_PATTERN.fullmatch(normalized_error) is None
        ):
            normalized_error = "FREQTRADE_WORKER_PROBE_FAILED"
        with self.database.session_factory.begin() as session:
            _actor, workspace, team = self.transactions._active_scope(session, actor_id)
            assert team is not None
            account = session.scalar(
                select(ExchangeAccount)
                .where(
                    ExchangeAccount.exchange_account_id == binding.exchange_account_id,
                    ExchangeAccount.team_id == team.team_id,
                )
                .with_for_update()
            )
            if account is None:
                _reject(
                    "EXCHANGE_ACCOUNT_NOT_FOUND",
                    "exchange account is outside the active team or does not exist",
                )
            self.transactions._require_role(
                session,
                actor_id,
                "account.credentials.manage",
                account.account_id,
                account.venue,
                team_id=account.team_id,
            )
            caller = f"{actor_id}:{team.team_id}"
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=caller,
                operation="exchange-account.freqtrade-worker.verify",
                idempotency_key=idempotency_key,
                payload=self._freqtrade_verification_payload(
                    binding.exchange_account_id,
                    binding.account_version,
                ),
            )
            if replay is not None:
                return replay
            if (
                account.version != binding.account_version
                or account.team_id != binding.team_id
                or account.account_id != binding.account_id
                or account.venue != binding.venue
                or account.freqtrade_auth_version != binding.auth_version
                or account.freqtrade_worker_name != binding.worker_name
                or account.freqtrade_worker_url != binding.worker_url
                or account.freqtrade_worker_mode != binding.worker_mode
            ):
                _reject(
                    "VERSION_CONFLICT",
                    "Freqtrade worker binding changed during verification",
                )
            account.freqtrade_worker_status = "VERIFIED" if normalized_error is None else "FAILED"
            account.freqtrade_error_code = normalized_error
            account.freqtrade_last_check_at = now
            if normalized_error is None:
                account.freqtrade_last_verified_at = now
            account.version += 1
            account.updated_by = actor_id
            account.updated_at = now
            result = {
                "exchange_account_id": str(account.exchange_account_id),
                "version": account.version,
                "worker": {
                    "mode": account.freqtrade_worker_mode,
                    "status": account.freqtrade_worker_status,
                    "error_code": account.freqtrade_error_code,
                    "checked_at": now.astimezone(UTC).isoformat(),
                    "last_verified_at": (
                        None
                        if account.freqtrade_last_verified_at is None
                        else account.freqtrade_last_verified_at.astimezone(UTC).isoformat()
                    ),
                },
            }
            self.transactions._save_receipt(
                session,
                caller_id=caller,
                operation="exchange-account.freqtrade-worker.verify",
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type=(
                    "FREQTRADE_WORKER_VERIFIED"
                    if normalized_error is None
                    else "FREQTRADE_WORKER_VERIFICATION_FAILED"
                ),
                object_type="ExchangeAccount",
                object_id=account.exchange_account_id,
                reason=(
                    f"venue={account.venue};mode={account.freqtrade_worker_mode.lower()};"
                    f"status={account.freqtrade_worker_status.lower()};"
                    f"error_code={normalized_error or 'none'};order_send=none"
                ),
                correlation_id=uuid4(),
                object_version=account.version,
                idempotency_key=idempotency_key,
                workspace_id=workspace.workspace_id,
                team_id=team.team_id,
                account_id=account.account_id,
                now=now,
            )
            return result

    def freqtrade_live_worker_binding(
        self,
        *,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        now: datetime,
        campaign_id: UUID | None = None,
    ) -> PreparedFreqtradeWorkerBinding:
        environment, account_id, venue = _scope_parts(execution_scope)
        if environment is not ExecutionEnvironment.LIVE:
            _reject("FREQTRADE_LIVE_SCOPE_REQUIRED", "Freqtrade LIVE requires a LIVE scope")
        with self.database.session_factory() as session:
            team = self.transactions._require_role(
                session, actor_id, "venue.record", account_id, venue
            )
            self.facade._validate_sender(
                session,
                team.team_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
            )
            if campaign_id is not None:
                campaign = session.get(Campaign, campaign_id)
                if (
                    campaign is None
                    or campaign.team_id != team.team_id
                    or execution_scope
                    != _scope_key(
                        campaign.environment,
                        campaign.account_id,
                        campaign.venue,
                    )
                ):
                    _reject(
                        "EXECUTION_SCOPE_MISMATCH",
                        "Freqtrade worker scope does not match the campaign",
                    )
            live_gate = session.get(CapabilityGate, "LIVE_ORDER_SEND")
            if live_gate is None or live_gate.status != CapabilityStatus.ENABLED.value:
                _reject(
                    "LIVE_ORDER_SEND_DISABLED",
                    "LIVE order send requires the explicit capability gate",
                )
            account = self.facade._require_exchange_account_live_ready(
                session,
                team_id=team.team_id,
                account_id=account_id,
                venue=venue,
            )
            return self._prepared_freqtrade_worker_binding(
                account,
                team.workspace_id,
                require_live_verified=True,
            )

    def validate_freqtrade_worker_binding(
        self,
        binding: PreparedFreqtradeWorkerBinding,
    ) -> None:
        with self.database.session_factory() as session:
            account = session.get(ExchangeAccount, binding.exchange_account_id)
            if (
                account is None
                or account.team_id != binding.team_id
                or account.account_id != binding.account_id
                or account.venue != binding.venue
                or account.version != binding.account_version
                or account.freqtrade_worker_name != binding.worker_name
                or account.freqtrade_worker_url != binding.worker_url
                or account.freqtrade_worker_mode != "LIVE"
                or account.freqtrade_worker_status != "VERIFIED"
                or account.freqtrade_auth_version != binding.auth_version
            ):
                _reject(
                    "FREQTRADE_WORKER_BINDING_CHANGED",
                    "the exact account-bound Freqtrade worker changed before execution",
                )

    def start_freqtrade_live_dispatch(
        self,
        intent_id: UUID,
        *,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        binding: PreparedFreqtradeWorkerBinding,
        command: FreqtradeEntryCommand | FreqtradeExitCommand,
        external_trade_id: str | None,
        idempotency_key: str,
        now: datetime,
    ) -> PreparedFreqtradeDispatch:
        """Persist the exact external handoff before a Freqtrade write can occur."""

        if not idempotency_key or len(idempotency_key) > 160:
            _reject(
                "IDEMPOTENCY_KEY_INVALID",
                "Freqtrade dispatch requires an idempotency key of 1-160 characters",
            )
        if owner_id != owner_id.strip() or not owner_id:
            _reject("SENDER_OWNER_INVALID", "sender owner identity is invalid")
        if isinstance(command, FreqtradeEntryCommand):
            if external_trade_id is not None:
                _reject(
                    "FREQTRADE_DISPATCH_IDENTITY_INVALID",
                    "entry dispatch must not pre-bind an external trade identity",
                )
            command_payload = {
                "kind": "ENTRY",
                "pair": command.pair,
                "side": command.side,
                "max_quantity": str(command.max_quantity),
                "enter_tag": command.enter_tag,
                "client_order_id": command.client_order_id,
            }
        else:
            normalized_external_id = "" if external_trade_id is None else external_trade_id.strip()
            if (
                not normalized_external_id
                or normalized_external_id != external_trade_id
                or len(normalized_external_id) > 255
            ):
                _reject(
                    "FREQTRADE_POSITION_NOT_FOUND",
                    "exit dispatch requires the exact current Freqtrade trade identity",
                )
            command_payload = {
                "kind": "EXIT",
                "pair": command.pair,
                "max_quantity": str(command.max_quantity),
                "client_order_id": command.client_order_id,
                "external_trade_id": normalized_external_id,
            }
        environment, account_id, venue = _scope_parts(execution_scope)
        if environment is not ExecutionEnvironment.LIVE:
            _reject("FREQTRADE_LIVE_SCOPE_REQUIRED", "Freqtrade LIVE requires a LIVE scope")
        operation = f"freqtrade-live.dispatch:{intent_id}"
        with self.database.session_factory.begin() as session:
            intent = session.get(OrderIntent, intent_id, with_for_update=True)
            campaign = None if intent is None else session.get(Campaign, intent.campaign_id)
            if intent is None or campaign is None:
                _reject("ORDER_INTENT_NOT_FOUND", "Freqtrade intent campaign is unavailable")
            if (
                campaign.environment != ExecutionEnvironment.LIVE.value
                or campaign.account_id != account_id
                or campaign.venue != venue
                or execution_scope
                != _scope_key(campaign.environment, campaign.account_id, campaign.venue)
            ):
                _reject(
                    "EXECUTION_SCOPE_MISMATCH",
                    "Freqtrade dispatch is outside the intent's exact execution scope",
                )
            self.transactions._require_role(
                session,
                actor_id,
                "venue.record",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            self.facade._validate_sender(
                session,
                campaign.team_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
            )
            live_gate = session.get(CapabilityGate, "LIVE_ORDER_SEND")
            if live_gate is None or live_gate.status != CapabilityStatus.ENABLED.value:
                _reject(
                    "LIVE_ORDER_SEND_DISABLED",
                    "LIVE order send requires the explicit capability gate",
                )
            account = self.facade._require_exchange_account_live_ready(
                session,
                team_id=campaign.team_id,
                account_id=campaign.account_id,
                venue=campaign.venue,
            )
            if (
                binding.exchange_account_id != account.exchange_account_id
                or binding.team_id != account.team_id
                or binding.account_id != account.account_id
                or binding.venue != account.venue
                or binding.account_version != account.version
                or binding.worker_name != account.freqtrade_worker_name
                or binding.worker_url != account.freqtrade_worker_url
                or binding.worker_mode != "LIVE"
                or binding.worker_status != "VERIFIED"
                or binding.auth_version != account.freqtrade_auth_version
            ):
                _reject(
                    "FREQTRADE_WORKER_BINDING_CHANGED",
                    "the exact account-bound Freqtrade worker changed before dispatch",
                )
            if isinstance(command, FreqtradeEntryCommand) == intent.reduce_only:
                _reject(
                    "FREQTRADE_DISPATCH_IDENTITY_INVALID",
                    "Freqtrade dispatch kind does not match the frozen intent",
                )
            payload = {
                "intent_id": str(intent.intent_id),
                "intent_semantic_hash": intent.semantic_hash,
                "execution_scope": execution_scope,
                "exchange_account_id": str(binding.exchange_account_id),
                "account_version": binding.account_version,
                "worker_name": binding.worker_name,
                "auth_version": binding.auth_version,
                "command": command_payload,
            }
            # The durable handoff belongs to the exact team/account intent, not to
            # one process lifetime. A newly fenced, authorized sender may therefore
            # recover the same key by query after a crash without creating a second
            # external write.
            caller = f"team:{campaign.team_id}"
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                if (
                    intent.dispatch_backend != "FREQTRADE"
                    or intent.dispatch_account_version != binding.account_version
                    or intent.dispatch_auth_version != binding.auth_version
                ):
                    _reject(
                        "FREQTRADE_DISPATCH_SNAPSHOT_CHANGED",
                        "persisted Freqtrade dispatch scope changed before recovery",
                    )
                if isinstance(command, FreqtradeExitCommand) and (
                    intent.dispatch_external_id != external_trade_id
                ):
                    _reject(
                        "FREQTRADE_DISPATCH_SNAPSHOT_CHANGED",
                        "persisted Freqtrade exit identity changed before recovery",
                    )
                if intent.status == OrderIntentStatus.FILLED.value:
                    mode = "COMPLETED"
                elif intent.status in {
                    OrderIntentStatus.DISPATCHING.value,
                    OrderIntentStatus.UNKNOWN.value,
                }:
                    mode = "QUERY_ONLY"
                else:
                    _reject(
                        "FREQTRADE_DISPATCH_STATE_INVALID",
                        "persisted Freqtrade dispatch is not recoverable",
                    )
                return PreparedFreqtradeDispatch(
                    mode=mode,
                    external_trade_id=intent.dispatch_external_id,
                    intent_version=intent.version,
                )
            if intent.dispatch_backend is not None:
                _reject(
                    "FREQTRADE_DISPATCH_ALREADY_STARTED",
                    "the intent already has a durable Freqtrade dispatch identity",
                )
            if intent.status != OrderIntentStatus.READY.value:
                _reject(
                    "ORDER_INTENT_STATE_INVALID",
                    "only a READY intent can begin a new Freqtrade dispatch",
                )
            previous = intent.status
            intent.status = OrderIntentStatus.DISPATCHING.value
            intent.dispatch_backend = "FREQTRADE"
            intent.dispatch_account_version = binding.account_version
            intent.dispatch_auth_version = binding.auth_version
            intent.dispatch_owner_id = owner_id
            intent.dispatch_fencing_token = fencing_token
            intent.dispatch_external_id = external_trade_id
            intent.dispatch_started_at = now
            intent.version += 1
            intent.updated_at = now
            response = {
                "intent_id": str(intent.intent_id),
                "dispatch_status": OrderIntentStatus.DISPATCHING.value,
                "intent_version": intent.version,
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
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="FREQTRADE_LIVE_DISPATCH_STARTED",
                object_type="OrderIntent",
                object_id=intent.intent_id,
                reason=(
                    f"worker={binding.worker_name};account_version={binding.account_version};"
                    f"auth_version={binding.auth_version};fencing_token={fencing_token}"
                ),
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                idempotency_key=idempotency_key,
                workspace_id=binding.workspace_id,
                team_id=campaign.team_id,
                account_id=campaign.account_id,
                now=now,
            )
            INTENT_TRANSITIONS.labels(previous, intent.status).inc()
            return PreparedFreqtradeDispatch(
                mode="SEND",
                external_trade_id=intent.dispatch_external_id,
                intent_version=intent.version,
            )

    def freqtrade_dispatch_external_id(
        self,
        intent_id: UUID,
        *,
        actor_id: UUID,
        execution_scope: str,
    ) -> str | None:
        """Read only the persisted backend identity for an exact intent scope."""

        with self.database.session_factory() as session:
            intent = session.get(OrderIntent, intent_id)
            campaign = None if intent is None else session.get(Campaign, intent.campaign_id)
            if intent is None or campaign is None:
                _reject("ORDER_INTENT_NOT_FOUND", "Freqtrade intent campaign is unavailable")
            if execution_scope != _scope_key(
                campaign.environment,
                campaign.account_id,
                campaign.venue,
            ):
                _reject(
                    "EXECUTION_SCOPE_MISMATCH",
                    "Freqtrade dispatch identity is outside the exact intent scope",
                )
            self.transactions._require_role(
                session,
                actor_id,
                "venue.record",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            if intent.dispatch_backend not in {None, "FREQTRADE"}:
                _reject(
                    "FREQTRADE_DISPATCH_SNAPSHOT_CHANGED",
                    "the intent is bound to a different execution backend",
                )
            return intent.dispatch_external_id

    def runtime_account_bindings(self) -> tuple[PreparedRuntimeAccountBinding, ...]:
        with self.database.session_factory() as session:
            accounts = session.scalars(
                select(ExchangeAccount)
                .where(ExchangeAccount.runtime_sync_enabled)
                .order_by(
                    ExchangeAccount.team_id,
                    ExchangeAccount.venue,
                    ExchangeAccount.account_id,
                )
            ).all()
            bindings: list[PreparedRuntimeAccountBinding] = []
            for account in accounts:
                team = session.get(Team, account.team_id)
                if (
                    team is None
                    or not team.active
                    or account.runtime_service_principal_id is None
                    or not account.active
                    or account.connection_status != "VERIFIED"
                    or account.credentials_ciphertext is None
                    or account.credential_version < 1
                    or account.venue not in SUPPORTED_EXCHANGE_VENUES
                ):
                    _reject(
                        "RUNTIME_BINDING_INVALID",
                        "an enabled read-only runtime binding no longer matches its frozen scope",
                    )
                principal = self._require_exact_runtime_principal(
                    session,
                    principal_id=account.runtime_service_principal_id,
                    team=team,
                    role=Role.OPERATOR,
                    account_id=account.account_id,
                    venue=account.venue,
                    error_code="RUNTIME_BINDING_INVALID",
                    error_message=(
                        "an enabled read-only runtime binding no longer matches its frozen scope"
                    ),
                )
                credentials = self.credential_cipher.decrypt(
                    account.credentials_ciphertext,
                    team_id=account.team_id,
                    exchange_account_id=account.exchange_account_id,
                    venue=account.venue,
                    credential_version=account.credential_version,
                )
                if account.venue == "HYPERLIQUID":
                    credentials = {
                        key: value
                        for key, value in credentials.items()
                        if key in {"account_address", "api_wallet_address"}
                    }
                bindings.append(
                    PreparedRuntimeAccountBinding(
                        exchange_account_id=account.exchange_account_id,
                        workspace_id=team.workspace_id,
                        team_id=team.team_id,
                        service_principal_id=principal.user_id,
                        service_principal_username=principal.username,
                        account_id=account.account_id,
                        venue=account.venue,
                        account_version=account.version,
                        credential_version=account.credential_version,
                        credentials=credentials,
                    )
                )
            return tuple(bindings)

    def perptape_runtime_bindings(self) -> tuple[PreparedPerptapeRuntimeBinding, ...]:
        with self.database.session_factory() as session:
            sources = session.scalars(
                select(TeamSignalSource)
                .where(
                    TeamSignalSource.enabled,
                    TeamSignalSource.mode == SignalSourceMode.PERPTAPE.value,
                    TeamSignalSource.deleted_at.is_(None),
                    TeamSignalSource.credential_ciphertext.is_not(None),
                )
                .order_by(TeamSignalSource.team_id)
            ).all()
            bindings: list[PreparedPerptapeRuntimeBinding] = []
            for source in sources:
                team = session.get(Team, source.team_id)
                if team is None or not team.active or source.service_principal_id is None:
                    _reject(
                        "SIGNAL_SERVICE_PRINCIPAL_INVALID",
                        "an enabled Perptape source is outside its exact team scope",
                    )
                principal = self._require_exact_runtime_principal(
                    session,
                    principal_id=source.service_principal_id,
                    team=team,
                    role=Role.PROPOSER,
                    account_id=None,
                    venue=None,
                    error_code="SIGNAL_SERVICE_PRINCIPAL_INVALID",
                    error_message="an enabled Perptape source is outside its exact team scope",
                )
                assert source.credential_ciphertext is not None
                bindings.append(
                    PreparedPerptapeRuntimeBinding(
                        signal_source_id=source.signal_source_id,
                        workspace_id=team.workspace_id,
                        team_id=team.team_id,
                        service_principal_id=principal.user_id,
                        service_principal_username=principal.username,
                        source_version=source.version,
                        credential_version=source.credential_version,
                        api_key=self.credential_cipher.decrypt_secret(
                            source.credential_ciphertext,
                            team_id=source.team_id,
                            object_id=source.signal_source_id,
                            purpose="signal-source:perptape",
                            credential_version=source.credential_version,
                        ),
                    )
                )
            return tuple(bindings)

    def validate_runtime_account_binding(self, binding: PreparedRuntimeAccountBinding) -> None:
        with self.database.session_factory() as session:
            account = session.get(ExchangeAccount, binding.exchange_account_id)
            if (
                account is None
                or account.team_id != binding.team_id
                or account.account_id != binding.account_id
                or account.venue != binding.venue
                or not account.runtime_sync_enabled
                or account.runtime_service_principal_id != binding.service_principal_id
                or account.version != binding.account_version
                or account.credential_version != binding.credential_version
            ):
                _reject(
                    "RUNTIME_BINDING_CHANGED",
                    "the database-bound runtime account changed during synchronization",
                )

    def validate_perptape_runtime_binding(self, binding: PreparedPerptapeRuntimeBinding) -> None:
        with self.database.session_factory() as session:
            source = session.get(TeamSignalSource, binding.signal_source_id)
            if (
                source is None
                or source.team_id != binding.team_id
                or not source.enabled
                or source.deleted_at is not None
                or source.mode != SignalSourceMode.PERPTAPE.value
                or source.service_principal_id != binding.service_principal_id
                or source.version != binding.source_version
                or source.credential_version != binding.credential_version
            ):
                _reject(
                    "SIGNAL_RUNTIME_BINDING_CHANGED",
                    "the team Perptape binding changed during synchronization",
                )

    @staticmethod
    def _lock_runtime_account_binding(
        session: Session,
        binding: PreparedRuntimeAccountBinding,
    ) -> ExchangeAccount:
        account = session.scalar(
            select(ExchangeAccount)
            .where(ExchangeAccount.exchange_account_id == binding.exchange_account_id)
            .with_for_update()
        )
        if (
            account is None
            or account.team_id != binding.team_id
            or account.account_id != binding.account_id
            or account.venue != binding.venue
            or not account.runtime_sync_enabled
            or account.runtime_service_principal_id != binding.service_principal_id
            or account.version != binding.account_version
            or account.credential_version != binding.credential_version
        ):
            _reject(
                "RUNTIME_BINDING_CHANGED",
                "the database-bound runtime account changed during synchronization",
            )
        team = session.get(Team, binding.team_id)
        if team is None or not team.active:
            _reject(
                "RUNTIME_BINDING_CHANGED",
                "the database-bound runtime account changed during synchronization",
            )
        AccountService._require_exact_runtime_principal(
            session,
            principal_id=binding.service_principal_id,
            team=team,
            role=Role.OPERATOR,
            account_id=binding.account_id,
            venue=binding.venue,
            error_code="RUNTIME_BINDING_CHANGED",
            error_message=("the database-bound runtime account changed during synchronization"),
            lock=True,
        )
        return account

    @staticmethod
    def _lock_perptape_runtime_binding(
        session: Session,
        binding: PreparedPerptapeRuntimeBinding,
    ) -> TeamSignalSource:
        source = session.scalar(
            select(TeamSignalSource)
            .where(TeamSignalSource.signal_source_id == binding.signal_source_id)
            .with_for_update()
        )
        if (
            source is None
            or source.team_id != binding.team_id
            or not source.enabled
            or source.deleted_at is not None
            or source.mode != SignalSourceMode.PERPTAPE.value
            or source.service_principal_id != binding.service_principal_id
            or source.version != binding.source_version
            or source.credential_version != binding.credential_version
        ):
            _reject(
                "SIGNAL_RUNTIME_BINDING_CHANGED",
                "the team Perptape binding changed during synchronization",
            )
        team = session.get(Team, binding.team_id)
        if team is None or not team.active:
            _reject(
                "SIGNAL_RUNTIME_BINDING_CHANGED",
                "the team Perptape binding changed during synchronization",
            )
        AccountService._require_exact_runtime_principal(
            session,
            principal_id=binding.service_principal_id,
            team=team,
            role=Role.PROPOSER,
            account_id=None,
            venue=None,
            error_code="SIGNAL_RUNTIME_BINDING_CHANGED",
            error_message="the team Perptape binding changed during synchronization",
            lock=True,
        )
        return source

    @staticmethod
    def _connection_verification_payload(
        exchange_account_id: UUID,
        expected_version: int,
    ) -> dict[str, Any]:
        return {
            "exchange_account_id": str(exchange_account_id),
            "expected_version": expected_version,
        }

    def prepare_exchange_account_connection_verification(
        self,
        exchange_account_id: UUID,
        *,
        actor_id: UUID,
        expected_version: int,
        idempotency_key: str,
    ) -> tuple[PreparedExchangeConnectionVerification | None, dict[str, Any] | None]:
        """Authorize and decrypt a version-pinned probe after checking for a replay."""

        with self.database.session_factory.begin() as session:
            _actor, _workspace, active_team = self.transactions._active_scope(session, actor_id)
            assert active_team is not None
            account = session.scalar(
                select(ExchangeAccount).where(
                    ExchangeAccount.exchange_account_id == exchange_account_id,
                    ExchangeAccount.team_id == active_team.team_id,
                )
            )
            if account is None:
                _reject(
                    "EXCHANGE_ACCOUNT_NOT_FOUND",
                    "exchange account is outside the active team or does not exist",
                )
            self.transactions._require_role(
                session,
                actor_id,
                "account.credentials.manage",
                account.account_id,
                account.venue,
                team_id=account.team_id,
            )
            caller = f"{actor_id}:{account.team_id}"
            _digest, replay = self.transactions._idempotency(
                session,
                caller_id=caller,
                operation="exchange-account.connection.verify",
                idempotency_key=idempotency_key,
                payload=self._connection_verification_payload(
                    exchange_account_id,
                    expected_version,
                ),
            )
            if replay is not None:
                return None, replay
            if not account.active:
                _reject("EXCHANGE_ACCOUNT_INACTIVE", "exchange account is inactive")
            if account.version != expected_version:
                _reject("VERSION_CONFLICT", "exchange account version changed")
            if account.credentials_ciphertext is None or account.credential_version < 1:
                _reject(
                    "EXCHANGE_ACCOUNT_CREDENTIALS_MISSING",
                    "encrypted exchange credentials must be configured before verification",
                )
            credentials = self.credential_cipher.decrypt(
                account.credentials_ciphertext,
                team_id=account.team_id,
                exchange_account_id=account.exchange_account_id,
                venue=account.venue,
                credential_version=account.credential_version,
            )
            if account.venue == "HYPERLIQUID":
                credentials = {
                    key: value
                    for key, value in credentials.items()
                    if key in {"account_address", "api_wallet_address"}
                }
            return (
                PreparedExchangeConnectionVerification(
                    exchange_account_id=account.exchange_account_id,
                    team_id=account.team_id,
                    account_id=account.account_id,
                    venue=account.venue,
                    account_version=account.version,
                    credential_version=account.credential_version,
                    credentials=credentials,
                ),
                None,
            )

    def record_exchange_account_connection_verification(
        self,
        command: PreparedExchangeConnectionVerification,
        outcome: ConnectionProbeResult,
        *,
        actor_id: UUID,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        """Commit a probe only when its account and credential versions are still current."""

        error_code = outcome.error_code
        if outcome.success:
            error_code = None
        elif error_code is None or CONNECTION_ERROR_CODE_PATTERN.fullmatch(error_code) is None:
            error_code = "READ_ONLY_PROBE_FAILED"
        with self.database.session_factory.begin() as session:
            _actor, _workspace, active_team = self.transactions._active_scope(session, actor_id)
            assert active_team is not None
            account = session.scalar(
                select(ExchangeAccount)
                .where(
                    ExchangeAccount.exchange_account_id == command.exchange_account_id,
                    ExchangeAccount.team_id == active_team.team_id,
                )
                .with_for_update()
            )
            if account is None:
                _reject(
                    "EXCHANGE_ACCOUNT_NOT_FOUND",
                    "exchange account is outside the active team or does not exist",
                )
            self.transactions._require_role(
                session,
                actor_id,
                "account.credentials.manage",
                account.account_id,
                account.venue,
                team_id=account.team_id,
            )
            caller = f"{actor_id}:{account.team_id}"
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=caller,
                operation="exchange-account.connection.verify",
                idempotency_key=idempotency_key,
                payload=self._connection_verification_payload(
                    command.exchange_account_id,
                    command.account_version,
                ),
            )
            if replay is not None:
                return replay
            if (
                account.version != command.account_version
                or account.credential_version != command.credential_version
                or account.team_id != command.team_id
                or account.account_id != command.account_id
                or account.venue != command.venue
            ):
                _reject(
                    "VERSION_CONFLICT",
                    "exchange account or credential version changed during verification",
                )
            account.connection_status = "VERIFIED" if outcome.success else "FAILED"
            account.connection_error_code = error_code
            account.last_connection_check_at = now
            if outcome.success:
                account.last_verified_at = now
            else:
                account.runtime_sync_enabled = False
                if account.trading_status == "ELIGIBLE":
                    account.trading_status = "BLOCKED"
                self._set_internal_principal_active(
                    session,
                    account.runtime_service_principal_id,
                    False,
                )
            account.version += 1
            account.updated_by = actor_id
            account.updated_at = now
            response = {
                "exchange_account_id": str(account.exchange_account_id),
                "version": account.version,
                "connection": {
                    "status": account.connection_status,
                    "error_code": account.connection_error_code,
                    "checked_at": now.astimezone(UTC).isoformat(),
                    "last_verified_at": (
                        None
                        if account.last_verified_at is None
                        else account.last_verified_at.astimezone(UTC).isoformat()
                    ),
                },
                "trading": {
                    "status": account.trading_status,
                    "enabled": account.trading_status == "ELIGIBLE",
                },
            }
            self.transactions._save_receipt(
                session,
                caller_id=caller,
                operation="exchange-account.connection.verify",
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=response,
                now=now,
            )
            correlation_id = uuid4()
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type=(
                    "EXCHANGE_ACCOUNT_CONNECTION_VERIFIED"
                    if outcome.success
                    else "EXCHANGE_ACCOUNT_CONNECTION_FAILED"
                ),
                object_type="ExchangeAccount",
                object_id=account.exchange_account_id,
                reason=(
                    f"venue={account.venue};connection={account.connection_status.lower()};"
                    f"error_code={error_code or 'none'};trading={account.trading_status.lower()}"
                ),
                correlation_id=correlation_id,
                object_version=account.version,
                idempotency_key=idempotency_key,
                workspace_id=active_team.workspace_id,
                team_id=account.team_id,
                account_id=account.account_id,
                now=now,
            )
            if not outcome.success:
                self.transactions._enqueue_notification_event(
                    session,
                    actor_id=str(actor_id),
                    team=active_team,
                    event_type="CONNECTION_CHECK_FAILED",
                    payload={
                        "summary": "交易账户只读连接验证失败; 交易能力仍保持关闭。",
                        "account_id": account.account_id,
                        "venue": account.venue,
                        "connection_status": account.connection_status,
                        "error_code": error_code,
                        "trading_status": account.trading_status,
                    },
                    object_type="ExchangeAccount",
                    object_id=account.exchange_account_id,
                    object_version=account.version,
                    idempotency_key=idempotency_key,
                    correlation_id=correlation_id,
                    environment=None,
                    account_id=account.account_id,
                    venue=account.venue,
                    now=now,
                )
            return response
