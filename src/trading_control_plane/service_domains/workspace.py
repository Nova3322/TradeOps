from __future__ import annotations

from trading_control_plane.service_component import ServiceComponent

# The domain implementation intentionally consumes the explicit service_core export surface.
# ruff: noqa: F403, F405
from trading_control_plane.service_core import *


class WorkspaceService(ServiceComponent):
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
                execution_mode_locked_at=now,
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
                    name="Perptape",
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
                    last_checked_at=None,
                    last_success_at=None,
                    last_error_code=None,
                    consecutive_failures=0,
                    version=1,
                    created_by=user.user_id,
                    updated_by=user.user_id,
                    created_at=now,
                    updated_at=now,
                    deleted_at=None,
                    deleted_by=None,
                )
            )
            self.transactions._audit(
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
            digest, replay = self.transactions._idempotency(
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
            self.transactions._audit(
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
            self.transactions._audit(
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
            self.transactions._save_receipt(
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
            actor, workspace = self.transactions._require_workspace_admin(session, actor_id)
            payload = {
                "workspace_id": str(workspace.workspace_id),
                "name": normalized_name,
                "slug": normalized_slug,
            }
            caller = f"{actor_id}:{workspace.workspace_id}"
            digest, replay = self.transactions._idempotency(
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
            self.transactions._audit(
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
            self.transactions._save_receipt(
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
            select(TeamSignalSource)
            .where(
                TeamSignalSource.team_id == team.team_id,
                TeamSignalSource.deleted_at.is_(None),
                TeamSignalSource.enabled,
            )
            .limit(1)
        )
        if source is None:
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
            team = self.transactions._require_role(session, actor_id, "team.view")
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
            active_team = self.transactions._require_role(
                session, actor_id, "team.manage", team_id=team_id
            )
            digest, replay = self.transactions._idempotency(
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
                team.execution_mode_locked_at = now
                team.trading_enabled = True
                team.version += 1
                team.updated_at = now
            shadow_account = session.scalar(
                select(TeamShadowAccount).where(
                    TeamShadowAccount.team_id == team.team_id,
                    TeamShadowAccount.status == "ACTIVE",
                )
            )
            shadow_account_created = shadow_account is None
            if shadow_account is None:
                latest_generation = session.scalar(
                    select(func.max(TeamShadowAccount.generation)).where(
                        TeamShadowAccount.team_id == team.team_id
                    )
                )
                shadow_account = TeamShadowAccount(
                    team_id=team.team_id,
                    generation=int(latest_generation or 0) + 1,
                    initial_equity=Decimal("100000"),
                    equity=Decimal("100000"),
                    available_balance=Decimal("100000"),
                    realized_pnl=Decimal(0),
                    unrealized_pnl=Decimal(0),
                    fees_paid=Decimal(0),
                    status="ACTIVE",
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(shadow_account)
                session.flush()
                session.add(
                    AnalyticsEquitySnapshot(
                        team_id=team.team_id,
                        environment=ExecutionEnvironment.SHADOW.value,
                        account_id="TEAM_SHADOW",
                        venue="TRADINGOPS",
                        generation=shadow_account.generation,
                        equity=shadow_account.equity,
                        currency="U",
                        source_kind="TEAM_SHADOW_ACCOUNT",
                        source_id=f"{shadow_account.shadow_account_id}:{shadow_account.version}",
                        version=shadow_account.version,
                        fact_metadata={
                            "shadow_account_id": str(shadow_account.shadow_account_id),
                            "initial_equity": str(shadow_account.initial_equity),
                        },
                        observed_at=now,
                        recorded_at=now,
                    )
                )
            result = {
                "team_id": str(team.team_id),
                "execution_mode": team.execution_mode,
                "trading_enabled": team.trading_enabled,
                "version": team.version,
            }
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{active_team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
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
            if shadow_account_created:
                self.transactions._audit(
                    session,
                    actor_id=str(actor_id),
                    event_type="SHADOW_ACCOUNT_INITIALIZED",
                    object_type="TeamShadowAccount",
                    object_id=shadow_account.shadow_account_id,
                    reason="generation=1;initial_equity=100000",
                    correlation_id=uuid4(),
                    object_version=shadow_account.version,
                    idempotency_key=idempotency_key,
                    workspace_id=team.workspace_id,
                    team_id=team.team_id,
                    environment=ExecutionEnvironment.SHADOW.value,
                    generation=shadow_account.generation,
                    rule_summary={
                        "initial_equity": "100000",
                        "capital_unit": "USDT_EQUIVALENT",
                        "status": "ACTIVE",
                    },
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
            digest, replay = self.transactions._idempotency(
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
            self.transactions._audit(
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
            self.transactions._save_receipt(
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
            self.transactions._require_role(session, actor_id, "user.manage")
            _actor, workspace, team = self.transactions._active_scope(session, actor_id)
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
            self.transactions._audit(
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
            self.transactions._require_role(session, actor_id, "user.manage")
            _actor, workspace, team = self.transactions._active_scope(session, actor_id)
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
            self.transactions._audit(
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
            self.transactions._require_role(session, actor_id, "user.manage")
            _actor, workspace, team = self.transactions._active_scope(session, actor_id)
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
            digest, replay = self.transactions._idempotency(
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
            self.transactions._audit(
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
            self.transactions._save_receipt(
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
            self.transactions._require_role(session, actor_id, "user.manage")
            _actor, _workspace, team = self.transactions._active_scope(session, actor_id)
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
            self.transactions._audit(
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

    def change_own_password(
        self,
        *,
        actor_id: UUID,
        current_password: str,
        new_password: str,
        expected_auth_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> int:
        """Rotate the authenticated human's password and revoke older sessions."""

        with self.database.session_factory.begin() as session:
            user = session.get(User, actor_id, with_for_update=True)
            if (
                user is None
                or not user.active
                or user.principal_type != PrincipalType.HUMAN.value
                or user.password_hash is None
            ):
                _reject("PASSWORD_CHANGE_DENIED", "an active password identity is required")
            payload = {
                "user_id": str(actor_id),
                "expected_auth_version": expected_auth_version,
                "new_password_length": len(new_password),
            }
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=str(actor_id),
                operation="user.password.change",
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return int(replay["auth_version"])
            if user.auth_version != expected_auth_version:
                _reject(
                    "AUTH_VERSION_CONFLICT",
                    "the login identity changed before password update",
                )
            if not PASSWORD_HASHER.verify(current_password, user.password_hash):
                _reject("CURRENT_PASSWORD_INVALID", "the current password is incorrect")
            if PASSWORD_HASHER.verify(new_password, user.password_hash):
                _reject("PASSWORD_UNCHANGED", "the new password must differ from the current one")
            user.password_hash = PASSWORD_HASHER.hash(new_password)
            user.password_changed_at = now
            user.auth_version += 1
            response = {"auth_version": user.auth_version}
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="USER_PASSWORD_CHANGED",
                object_type="User",
                object_id=user.user_id,
                reason="authentication=password-scrypt;other_sessions_revoked=true",
                correlation_id=uuid4(),
                idempotency_key=idempotency_key,
                object_version=user.auth_version,
                workspace_id=user.active_workspace_id,
                team_id=user.active_team_id,
                now=now,
            )
            self.transactions._save_receipt(
                session,
                caller_id=str(actor_id),
                operation="user.password.change",
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=response,
                now=now,
            )
            return user.auth_version

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
            self.transactions._audit(
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
            self.transactions._audit(
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

    def create_service_principal(self, username: str, actor_id: UUID, *, now: datetime) -> UUID:
        with self.database.session_factory.begin() as session:
            self.transactions._require_role(session, actor_id, "user.manage")
            _actor, workspace, team = self.transactions._active_scope(session, actor_id)
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
            self.transactions._audit(
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
            self.transactions._require_role(session, actor_id, "role.manage")
            _actor, _workspace, team = self.transactions._active_scope(session, actor_id)
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
            self.transactions._audit(
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
