from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import and_, false, func, or_, select, text, tuple_
from sqlalchemy.orm import Session

from trading_control_plane.database import Database
from trading_control_plane.domain import (
    DomainRejected,
    PrincipalType,
    Role,
    ServicePrincipalKind,
)
from trading_control_plane.models import (
    AccountEquity,
    AccountEquityObservation,
    Approval,
    AuditEvent,
    Campaign,
    CapabilityGate,
    CapitalAutomationPolicy,
    CapitalTransfer,
    DirectCapitalOperation,
    ExchangeAccount,
    FundingPayment,
    Instrument,
    NotificationDelivery,
    NotificationRoute,
    OrderIntent,
    PerptapeFeed,
    Position,
    Proposal,
    ProtectionOrder,
    ReconciliationRun,
    RiskDecision,
    RiskPolicy,
    RiskReservation,
    RoleAssignment,
    RuntimeSourceHealth,
    SenderLease,
    SignalEvent,
    Team,
    TeamMembership,
    TradingAuthorization,
    TransferAuthorization,
    TransferProposal,
    User,
    VenueFill,
    VenueOrder,
    Workspace,
    WorkspaceMembership,
)
from trading_control_plane.notification import (
    NOTIFICATION_TEMPLATES,
    ROUTABLE_NOTIFICATION_EVENT_TYPES,
)
from trading_control_plane.notilt import USD_STABLE_ASSETS
from trading_control_plane.perptape import PerptapeCandidate, PerptapeFeedSnapshot
from trading_control_plane.service import ROLE_ACTIONS, TradingService


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


PERPTAPE_STRATEGIES = frozenset({"perptape", "perptape-resonance"})


def _report_attribution(session: Session, proposal: Proposal) -> dict[str, Any]:
    """Project immutable proposal/signal facts without consulting the mutable source config."""

    signal = (
        None
        if proposal.signal_event_id is None
        else session.scalar(
            select(SignalEvent).where(
                SignalEvent.signal_event_id == proposal.signal_event_id,
                SignalEvent.team_id == proposal.team_id,
            )
        )
    )
    if signal is not None:
        return {
            "source_type": "MANUAL",
            "strategy_id": signal.strategy_id,
            "strategy_version": signal.strategy_version,
            "signal_source_mode": "WEBHOOK",
            "signal_source_id": str(signal.signal_source_id),
            "signal_provider": signal.provider,
            "signal_external_id": signal.external_id,
            "attribution": "FROZEN_SIGNAL_EVENT",
        }
    if proposal.source == "SYSTEM":
        perptape = proposal.strategy_id in PERPTAPE_STRATEGIES
        return {
            "source_type": proposal.strategy_id,
            "strategy_id": proposal.strategy_id,
            "strategy_version": proposal.strategy_version,
            "signal_source_mode": "PERPTAPE" if perptape else "SYSTEM",
            "signal_source_id": None,
            "signal_provider": "PERPTAPE" if perptape else None,
            "signal_external_id": proposal.source_candidate_id,
            "attribution": "FROZEN_PROPOSAL",
        }
    return {
        "source_type": "MANUAL",
        "strategy_id": None,
        "strategy_version": None,
        "signal_source_mode": "MANUAL",
        "signal_source_id": None,
        "signal_provider": None,
        "signal_external_id": None,
        "attribution": "FROZEN_PROPOSAL",
    }


def _performance_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [item for item in rows if item["status"] == "CLOSED"]
    open_rows = [item for item in rows if item["status"] != "CLOSED"]
    wins = [
        Decimal(str(item["final_pnl"])) for item in closed if Decimal(str(item["final_pnl"])) > 0
    ]
    losses = [
        Decimal(str(item["final_pnl"])) for item in closed if Decimal(str(item["final_pnl"])) < 0
    ]
    gross_profit = sum(wins, Decimal(0))
    gross_loss_abs = abs(sum(losses, Decimal(0)))
    average_win = None if not wins else gross_profit / len(wins)
    average_loss_abs = None if not losses else gross_loss_abs / len(losses)
    win_rate = None if not closed else Decimal(len(wins)) / Decimal(len(closed))
    profit_loss_ratio = None
    if average_win is not None and average_loss_abs not in {None, Decimal(0)}:
        assert average_loss_abs is not None
        profit_loss_ratio = average_win / average_loss_abs
    profit_factor = None if gross_loss_abs == 0 else gross_profit / gross_loss_abs
    cumulative = Decimal(0)
    peak = Decimal(0)
    maximum_drawdown = Decimal(0)
    points: list[dict[str, str | None]] = []
    for item in closed:
        cumulative += Decimal(str(item["final_pnl"]))
        peak = max(peak, cumulative)
        drawdown = peak - cumulative
        maximum_drawdown = max(maximum_drawdown, drawdown)
        points.append(
            {
                "campaign_id": str(item["campaign_id"]),
                "at": None if item["updated_at"] is None else str(item["updated_at"]),
                "cumulative_pnl": str(cumulative),
                "running_peak": str(peak),
                "drawdown": str(drawdown),
            }
        )
    return {
        "campaign_count": len(rows),
        "closed_count": len(closed),
        "open_count": len(rows) - len(closed),
        "win_count": len(wins),
        "loss_count": len(losses),
        "breakeven_count": len(closed) - len(wins) - len(losses),
        "net_pnl": str(sum((Decimal(str(item["final_pnl"])) for item in rows), Decimal(0))),
        "closed_net_pnl": str(
            sum((Decimal(str(item["final_pnl"])) for item in closed), Decimal(0))
        ),
        "open_current_pnl": str(
            sum((Decimal(str(item["final_pnl"])) for item in open_rows), Decimal(0))
        ),
        "gross_profit": str(gross_profit),
        "gross_loss_abs": str(gross_loss_abs),
        "average_win": None if average_win is None else str(average_win),
        "average_loss_abs": None if average_loss_abs is None else str(average_loss_abs),
        "win_rate": None if win_rate is None else str(win_rate),
        "profit_loss_ratio": None if profit_loss_ratio is None else str(profit_loss_ratio),
        "profit_factor": None if profit_factor is None else str(profit_factor),
        "maximum_drawdown": str(maximum_drawdown),
        "percentage_return": None,
        "percentage_drawdown": None,
        "availability": {
            "win_rate": "AVAILABLE" if closed else "NO_CLOSED_CAMPAIGNS",
            "profit_loss_ratio": (
                "AVAILABLE" if profit_loss_ratio is not None else "REQUIRES_WIN_AND_LOSS"
            ),
            "percentage_metrics": "OPENING_CAPITAL_UNAVAILABLE",
        },
        "curve": points,
    }


def _uuid_or_none(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _effective_proposal_status(proposal: Proposal, now: datetime) -> str:
    if proposal.status in {"DRAFT", "PENDING_REVIEW"} and proposal.expires_at <= now:
        return "EXPIRED"
    return proposal.status


def _proposal_execution_status(
    proposal: Proposal,
    now: datetime,
    campaign_id: UUID | None,
) -> str | None:
    if proposal.status != "APPROVED":
        return None
    if campaign_id is not None:
        return "TRADE_CREATED"
    if proposal.expires_at <= now:
        return "WINDOW_EXPIRED"
    return "AWAITING_LAUNCH"


class TradingQueries:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.service = TradingService(database)

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
            roles = session.scalars(
                select(RoleAssignment)
                .where(
                    RoleAssignment.user_id == user_id,
                    RoleAssignment.team_id == user.active_team_id,
                )
                .order_by(RoleAssignment.role)
            ).all()
            active_workspace = next(
                (
                    workspace
                    for membership, workspace in workspace_memberships
                    if membership.workspace_id == user.active_workspace_id
                ),
                None,
            )
            active_team = next(
                (
                    team
                    for membership, team in team_memberships
                    if membership.team_id == user.active_team_id
                    and team.workspace_id == user.active_workspace_id
                ),
                None,
            )
            return {
                "user_id": str(user.user_id),
                "username": user.username,
                "auth_version": user.auth_version,
                "principal_type": user.principal_type,
                "service_kind": user.service_kind,
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

    def exchange_accounts(self, actor_id: UUID) -> dict[str, Any]:
        workspace_id, team_id = self._active_scope_ids(actor_id)
        with self.database.session_factory() as session:
            assignments = session.scalars(
                select(RoleAssignment).where(
                    RoleAssignment.user_id == actor_id,
                    RoleAssignment.team_id == team_id,
                )
            ).all()
            listing_actions = {"venue.view", "proposal.create"}

            def can_list_account(assignment: RoleAssignment) -> bool:
                actions = ROLE_ACTIONS[Role(assignment.role)]
                return "*" in actions or not listing_actions.isdisjoint(actions)

            if not any(can_list_account(item) for item in assignments):
                raise DomainRejected("RBAC_DENIED", "exchange account visibility is not assigned")
            accounts = session.scalars(
                select(ExchangeAccount)
                .where(ExchangeAccount.team_id == team_id)
                .order_by(ExchangeAccount.venue, ExchangeAccount.label, ExchangeAccount.account_id)
            ).all()
            visible = [
                item
                for item in accounts
                if any(
                    (
                        assignment.account_scope is None
                        or assignment.account_scope == item.account_id
                    )
                    and (assignment.venue_scope is None or assignment.venue_scope == item.venue)
                    and can_list_account(assignment)
                    for assignment in assignments
                )
            ]

            def granted(account: ExchangeAccount, action: str) -> bool:
                return any(
                    (
                        assignment.account_scope is None
                        or assignment.account_scope == account.account_id
                    )
                    and (assignment.venue_scope is None or assignment.venue_scope == account.venue)
                    and (
                        action in ROLE_ACTIONS[Role(assignment.role)]
                        or "*" in ROLE_ACTIONS[Role(assignment.role)]
                    )
                    for assignment in assignments
                )

            projected: list[dict[str, Any]] = []
            for item in visible:
                projection = self._exchange_account_projection(item)
                can_manage_credentials = granted(item, "account.credentials.manage")
                projection["permissions"] = {
                    "can_manage": granted(item, "account.manage"),
                    "can_manage_trading": granted(item, "account.manage"),
                    "can_manage_credentials": can_manage_credentials,
                    "can_verify_connection": can_manage_credentials,
                    "can_manage_worker": can_manage_credentials,
                }
                if not can_manage_credentials:
                    projection["execution_worker"]["endpoint"] = None
                    projection["execution_worker"]["auth"]["username_hint"] = None
                projected.append(projection)
            return {
                "workspace_id": str(workspace_id),
                "team_id": str(team_id),
                "can_manage": any(
                    "account.manage" in ROLE_ACTIONS[Role(item.role)]
                    or "*" in ROLE_ACTIONS[Role(item.role)]
                    for item in assignments
                ),
                "supported_venues": ["BINANCE", "HYPERLIQUID", "OKX", "BYBIT"],
                "data": projected,
            }

    @staticmethod
    def _exchange_account_projection(item: ExchangeAccount) -> dict[str, Any]:
        metadata = dict(item.credential_metadata or {})
        credential_state = "UNCONFIGURED" if item.credential_version == 0 else "CONFIGURED"
        if item.trading_status == "ELIGIBLE":
            trading_reason = "account policy is eligible; global and task gates still apply"
        elif item.trading_status == "BLOCKED":
            trading_reason = (
                "account eligibility is blocked because a required connection "
                "or runtime fact was lost"
            )
        else:
            trading_reason = (
                "trading capability is disabled; connection status never enables order sending"
            )
        if credential_state == "UNCONFIGURED":
            next_action = "add encrypted credentials"
        elif item.connection_status != "VERIFIED":
            next_action = "run a supported no-side-effect connection verification"
        elif not item.runtime_sync_enabled:
            next_action = "enable the database-bound continuous read-only sync"
        elif item.freqtrade_worker_mode == "UNCONFIGURED":
            next_action = "bind one encrypted Freqtrade worker to this exact account"
        elif (
            item.freqtrade_worker_mode != "LIVE"
            or item.freqtrade_worker_status != "VERIFIED"
        ):
            next_action = "verify an account-bound LIVE Freqtrade worker"
        elif item.trading_status == "ELIGIBLE":
            next_action = "verify global, sender, risk, and task gates before LIVE execution"
        else:
            next_action = "explicitly enable exact-account trading eligibility when approved"
        return {
            "exchange_account_id": str(item.exchange_account_id),
            "team_id": str(item.team_id),
            "account_id": item.account_id,
            "venue": item.venue,
            "label": item.label,
            "registration_source": item.registration_source,
            "active": item.active,
            "version": item.version,
            "connection": {
                "status": item.connection_status,
                "error_code": item.connection_error_code,
                "checked_at": _iso(item.last_connection_check_at),
                "last_verified_at": _iso(item.last_verified_at),
                "read_only_capability": item.connection_status == "VERIFIED",
            },
            "trading": {
                "status": item.trading_status,
                "enabled": item.trading_status == "ELIGIBLE",
                "reason": trading_reason,
            },
            "credentials": {
                "state": credential_state,
                "version": item.credential_version,
                "configured_fields": list(metadata.get("configured_fields") or []),
                "key_hint": metadata.get("key_hint"),
                "signing_material_configured": bool(
                    metadata.get("signing_material_configured", False)
                ),
            },
            "runtime_binding": {
                "bound": item.runtime_sync_enabled,
                "source": "DATABASE_ENVELOPE",
                "read_only_connector": "IMPLEMENTED",
                "read_only_scope": (
                    "USDT_LINEAR_PERPETUALS"
                    if item.venue in {"OKX", "BYBIT"}
                    else "USD_M_PERPETUALS"
                    if item.venue == "BINANCE"
                    else "CORE_AND_CONFIGURED_HIP3"
                ),
                "connection_verification_connector": "IMPLEMENTED",
                "connection_verification_source": "DATABASE_ENVELOPE",
                "service_principal_configured": (
                    item.runtime_service_principal_id is not None
                ),
                "trading_connector": "FREQTRADE_EXTERNAL",
            },
            "execution_worker": {
                "supported": True,
                "configured": item.freqtrade_worker_mode != "UNCONFIGURED",
                "scope": {
                    "team_id": str(item.team_id),
                    "account_id": item.account_id,
                    "venue": item.venue,
                },
                "name": item.freqtrade_worker_name,
                "endpoint": item.freqtrade_worker_url,
                "mode": item.freqtrade_worker_mode,
                "status": item.freqtrade_worker_status,
                "error_code": item.freqtrade_error_code,
                "checked_at": _iso(item.freqtrade_last_check_at),
                "last_verified_at": _iso(item.freqtrade_last_verified_at),
                "hip3_dexes": list(item.freqtrade_hip3_dexes or []),
                "auth": {
                    "state": (
                        "CONFIGURED"
                        if item.freqtrade_auth_version > 0
                        else "UNCONFIGURED"
                    ),
                    "version": item.freqtrade_auth_version,
                    "username_hint": (item.freqtrade_auth_metadata or {}).get(
                        "username_hint"
                    ),
                },
                "live_ready": (
                    item.freqtrade_worker_mode == "LIVE"
                    and item.freqtrade_worker_status == "VERIFIED"
                ),
                "order_send": False,
            },
            "next_action": next_action,
            "created_at": _iso(item.created_at),
            "updated_at": _iso(item.updated_at),
        }

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

    def list_instruments(self, user_id: UUID) -> list[dict[str, Any]]:
        with self.database.session_factory() as session:
            user = session.get(User, user_id)
            if user is None or not user.active:
                raise DomainRejected("SESSION_REVOKED", "internal user is inactive or missing")
            assignments = session.scalars(
                select(RoleAssignment).where(RoleAssignment.user_id == user_id)
            ).all()
            values = session.scalars(
                select(Instrument)
                .where(
                    Instrument.active,
                    Instrument.collateral_currency.in_(("USDT", "USDC")),
                    Instrument.quote_currency == Instrument.collateral_currency,
                )
                .order_by(Instrument.venue, Instrument.symbol)
            ).all()
            return [
                {
                    "instrument_id": str(item.instrument_id),
                    "venue": item.venue,
                    "symbol": item.symbol,
                    "tick_size": str(item.tick_size),
                    "lot_size": str(item.lot_size),
                    "minimum_notional": str(item.minimum_notional),
                    "contract_multiplier": str(item.contract_multiplier),
                    "quote_currency": item.quote_currency,
                    "collateral_currency": item.collateral_currency,
                    "protection_supported": item.protection_supported,
                    "updated_at": _iso(item.updated_at),
                }
                for item in values
                if any(
                    assignment.venue_scope is None or assignment.venue_scope == item.venue
                    for assignment in assignments
                )
            ]

    def instrument_id_by_venue_symbol(self, venue: str, symbol: str) -> UUID:
        with self.database.session_factory() as session:
            instrument = session.scalar(
                select(Instrument).where(
                    Instrument.venue == venue,
                    Instrument.symbol == symbol,
                    Instrument.active,
                )
            )
            if instrument is None:
                raise DomainRejected(
                    "INSTRUMENT_UNAVAILABLE",
                    "candidate instrument is not active in the Trading catalog",
                )
            return instrument.instrument_id

    def active_instrument_keys(self, venue_symbols: set[tuple[str, str]]) -> set[tuple[str, str]]:
        """Return exact active Catalog matches without normalizing or guessing symbols."""

        if not venue_symbols:
            return set()
        with self.database.session_factory() as session:
            return set(
                session.execute(
                    select(Instrument.venue, Instrument.symbol).where(
                        Instrument.active,
                        tuple_(Instrument.venue, Instrument.symbol).in_(venue_symbols),
                    )
                ).tuples()
            )

    def compatible_legacy_system_candidate_id(
        self,
        legacy_candidate_id: str,
        candidate: PerptapeCandidate,
        instrument_id: UUID,
    ) -> str | None:
        """Reuse an exact legacy proposal identity without conflating quote contracts."""

        with self.database.session_factory() as session:
            proposal = session.scalar(
                select(Proposal).where(
                    Proposal.source == "SYSTEM",
                    Proposal.source_candidate_id == legacy_candidate_id,
                )
            )
            if (
                proposal is None
                or proposal.instrument_id != instrument_id
                or proposal.venue != candidate.venue
                or proposal.direction != candidate.direction.value
            ):
                return None
            details = proposal.frozen_payload.get("details")
            snapshot = details.get("candidate") if isinstance(details, dict) else None
            if not isinstance(snapshot, dict):
                return None
            current = candidate.to_dict()
            identity_fields = (
                "venue",
                "source_exchange",
                "symbol",
                "canonical_symbol",
                "direction",
                "source_direction",
                "timeframe",
                "triggered_at",
            )
            if any(snapshot.get(field) != current[field] for field in identity_fields):
                return None
            return legacy_candidate_id

    def perptape_feed(self, user_id: UUID) -> PerptapeFeedSnapshot | None:
        _workspace_id, team_id = self._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            feed = session.get(PerptapeFeed, (team_id, "BREAKOUTS"))
            if feed is None:
                return None
            candidates: list[PerptapeCandidate] = []
            for value in feed.candidates:
                if not isinstance(value, dict):
                    raise DomainRejected(
                        "PERPTAPE_CACHE_INVALID",
                        "persisted Perptape feed contains an invalid candidate",
                    )
                candidates.append(PerptapeCandidate.from_dict(value))
            return PerptapeFeedSnapshot(
                contract_version=feed.contract_version,
                generated_at=feed.generated_at,
                fetched_at=feed.fetched_at,
                next_allowed_at=feed.next_allowed_at,
                candidates=tuple(candidates),
            )

    def list_proposals(
        self,
        user_id: UUID,
        *,
        status: str | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        current_time = now or datetime.now(UTC)
        workspace_id, team_id = self._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            statement = (
                select(Proposal, Instrument)
                .join(Instrument, Instrument.instrument_id == Proposal.instrument_id)
                .where(Proposal.team_id == team_id)
                .order_by(Proposal.created_at.desc())
            )
            if status in {"DRAFT", "PENDING_REVIEW"}:
                statement = statement.where(
                    Proposal.status == status,
                    Proposal.expires_at > current_time,
                )
            elif status == "EXPIRED":
                statement = statement.where(
                    or_(
                        Proposal.status == "EXPIRED",
                        and_(
                            Proposal.status.in_(["DRAFT", "PENDING_REVIEW"]),
                            Proposal.expires_at <= current_time,
                        ),
                    )
                )
            elif status is not None:
                statement = statement.where(Proposal.status == status)
            values = session.execute(statement).all()
            proposal_ids = [proposal.proposal_id for proposal, _instrument in values]
            proposer_names = {
                item.user_id: item.username
                for item in session.scalars(
                    select(User).where(
                        User.user_id.in_({proposal.proposer_id for proposal, _ in values})
                    )
                ).all()
            }
            reviewed_proposal_ids = set(
                session.scalars(
                    select(Approval.proposal_id).where(
                        Approval.reviewer_id == user_id,
                        Approval.proposal_id.in_(proposal_ids),
                    )
                )
            )
            reused_proposal_ids = set(
                session.scalars(
                    select(AuditEvent.object_id).where(
                        AuditEvent.event_type == "PROPOSAL_DUPLICATE_REUSED",
                        AuditEvent.object_type == "Proposal",
                        AuditEvent.actor_id == str(user_id),
                        AuditEvent.team_id == team_id,
                    )
                )
            )
            approval_counts: dict[UUID, int] = {
                proposal_id: int(count)
                for proposal_id, count in session.execute(
                    select(Approval.proposal_id, func.count(Approval.approval_id))
                    .where(Approval.proposal_id.in_(proposal_ids))
                    .group_by(Approval.proposal_id)
                ).all()
                if proposal_id is not None
            }
            campaign_by_proposal: dict[UUID, UUID] = {
                proposal_id: campaign_id
                for proposal_id, campaign_id in session.execute(
                    select(Campaign.proposal_id, Campaign.campaign_id).where(
                        Campaign.team_id == team_id,
                        Campaign.proposal_id.in_(proposal_ids),
                    )
                ).all()
            }
            result: list[dict[str, Any]] = []
            for proposal, instrument in values:
                if not self.service.can_user(user_id, "view", proposal.account_id, proposal.venue):
                    continue
                effective_status = _effective_proposal_status(proposal, current_time)
                summary = self._proposal_summary(proposal, instrument)
                summary["workspace_id"] = str(workspace_id)
                summary["proposer_username"] = proposer_names.get(proposal.proposer_id)
                summary["status"] = effective_status
                summary["approval_count"] = int(approval_counts.get(proposal.proposal_id, 0))
                summary["required_approvals"] = 2 if proposal.risk_tier == "HIGH" else 1
                campaign_id = campaign_by_proposal.get(proposal.proposal_id)
                summary["campaign_id"] = None if campaign_id is None else str(campaign_id)
                summary["execution_status"] = _proposal_execution_status(
                    proposal,
                    current_time,
                    campaign_id,
                )
                summary["actionable_for_current_user"] = bool(
                    effective_status == "PENDING_REVIEW"
                    and proposal.proposer_id != user_id
                    and str(proposal.proposal_id) not in reused_proposal_ids
                    and proposal.proposal_id not in reviewed_proposal_ids
                    and self.service.can_user(
                        user_id,
                        "proposal.review",
                        proposal.account_id,
                        proposal.venue,
                    )
                )
                result.append(summary)
            return result

    def active_perptape_system_proposals(
        self,
        user_id: UUID,
        *,
        now: datetime,
    ) -> list[dict[str, Any]]:
        """Return the current visible Perptape proposal occupying each trading scope."""

        workspace_id, team_id = self._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            values = session.execute(
                select(Proposal, Instrument)
                .join(Instrument, Instrument.instrument_id == Proposal.instrument_id)
                .where(
                    Proposal.source == "SYSTEM",
                    Proposal.team_id == team_id,
                    Proposal.strategy_id.in_(("perptape", "perptape-resonance")),
                    Proposal.environment == "LIVE",
                    Proposal.status.in_(("DRAFT", "PENDING_REVIEW")),
                    Proposal.expires_at > now,
                )
                .order_by(Proposal.created_at, Proposal.proposal_id)
            ).all()
            grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
            for proposal, instrument in values:
                if not self.service.can_user(
                    user_id,
                    "view",
                    proposal.account_id,
                    proposal.venue,
                ):
                    continue
                key = (proposal.venue, instrument.symbol, proposal.direction)
                current = grouped.get(key)
                if current is None:
                    grouped[key] = {
                        "proposal_id": str(proposal.proposal_id),
                        "workspace_id": str(workspace_id),
                        "team_id": str(proposal.team_id),
                        "status": _effective_proposal_status(proposal, now),
                        "venue": proposal.venue,
                        "symbol": instrument.symbol,
                        "direction": proposal.direction,
                        "expires_at": _iso(proposal.expires_at),
                        "source_observed_at": _iso(proposal.source_observed_at),
                        "active_count": 1,
                    }
                else:
                    current["active_count"] += 1
            return list(grouped.values())

    def proposal_detail(
        self,
        user_id: UUID,
        proposal_id: UUID,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current_time = now or datetime.now(UTC)
        workspace_id, team_id = self._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            proposal = session.get(Proposal, proposal_id)
            if proposal is None:
                raise DomainRejected("PROPOSAL_NOT_FOUND", "proposal does not exist")
            if proposal.team_id != team_id:
                raise DomainRejected("TEAM_SCOPE_DENIED", "proposal is outside active team")
            if not self.service.can_user(user_id, "view", proposal.account_id, proposal.venue):
                raise DomainRejected("RBAC_DENIED", "proposal is outside the current scope")
            approvals = session.scalars(
                select(Approval)
                .where(Approval.proposal_id == proposal_id)
                .order_by(Approval.created_at)
            ).all()
            proposal_users = {
                item.user_id: item.username
                for item in session.scalars(
                    select(User).where(
                        User.user_id.in_(
                            {proposal.proposer_id, *(item.reviewer_id for item in approvals)}
                        )
                    )
                ).all()
            }
            risk = session.scalar(
                select(RiskDecision)
                .where(RiskDecision.proposal_id == proposal_id)
                .order_by(RiskDecision.created_at.desc())
                .limit(1)
            )
            authorization = session.scalar(
                select(TradingAuthorization)
                .where(TradingAuthorization.proposal_id == proposal_id)
                .order_by(TradingAuthorization.created_at.desc())
                .limit(1)
            )
            campaign = session.scalar(
                select(Campaign)
                .where(Campaign.proposal_id == proposal_id)
                .order_by(Campaign.created_at.desc())
                .limit(1)
            )
            initial_intent = (
                None
                if campaign is None
                else session.scalar(
                    select(OrderIntent)
                    .where(
                        OrderIntent.campaign_id == campaign.campaign_id,
                        OrderIntent.kind == "INITIAL",
                    )
                    .order_by(OrderIntent.created_at.desc())
                    .limit(1)
                )
            )
            risk_inputs = risk.input_data if risk is not None else {}
            risk_policy = risk_inputs.get("policy", {})
            risk_position = risk_inputs.get("position")
            risk_equity = risk_inputs.get("equity")
            risk_capital = risk_inputs.get("managed_capital", {})
            risk_protection = risk_inputs.get("protection")
            result = self._proposal_summary(
                proposal, session.get(Instrument, proposal.instrument_id)
            )
            result["workspace_id"] = str(workspace_id)
            effective_status = _effective_proposal_status(proposal, current_time)
            reused_by_current_user = session.scalar(
                select(AuditEvent.audit_event_id)
                .where(
                    AuditEvent.event_type == "PROPOSAL_DUPLICATE_REUSED",
                    AuditEvent.object_type == "Proposal",
                    AuditEvent.object_id == str(proposal.proposal_id),
                    AuditEvent.actor_id == str(user_id),
                )
                .limit(1)
            )
            result["status"] = effective_status
            result["execution_status"] = _proposal_execution_status(
                proposal,
                current_time,
                None if campaign is None else campaign.campaign_id,
            )
            result["proposer_username"] = proposal_users.get(proposal.proposer_id)
            result.update(
                {
                    "frozen_payload": proposal.frozen_payload,
                    "semantic_hash": proposal.semantic_hash,
                    "frozen_at": _iso(proposal.frozen_at),
                    "correlation_id": str(proposal.correlation_id),
                    "approvals": [
                        {
                            "approval_id": str(item.approval_id),
                            "workspace_id": str(workspace_id),
                            "team_id": str(proposal.team_id),
                            "account_id": proposal.account_id,
                            "reviewer_id": str(item.reviewer_id),
                            "reviewer_username": proposal_users.get(item.reviewer_id),
                            "decision": item.decision,
                            "reason": item.reason,
                            "created_at": _iso(item.created_at),
                        }
                        for item in approvals
                    ],
                    "actionable_for_current_user": bool(
                        effective_status == "PENDING_REVIEW"
                        and proposal.proposer_id != user_id
                        and reused_by_current_user is None
                        and all(item.reviewer_id != user_id for item in approvals)
                        and self.service.can_user(
                            user_id,
                            "proposal.review",
                            proposal.account_id,
                            proposal.venue,
                        )
                    ),
                    "risk_decision": None
                    if risk is None
                    else {
                        "decision_id": str(risk.decision_id),
                        "workspace_id": str(workspace_id),
                        "team_id": str(risk.team_id),
                        "account_id": proposal.account_id,
                        "result": risk.result,
                        "approved_quantity": str(risk.approved_quantity),
                        "risk_amount": str(risk.risk_amount),
                        "reasons": risk.reasons,
                        "data_as_of": _iso(risk.data_as_of),
                        "created_at": _iso(risk.created_at),
                        "context": {
                            "requested_quantity": risk_inputs.get("requested_quantity"),
                            "requested_risk": risk_inputs.get("requested_risk"),
                            "current_risk": risk_inputs.get("current_risk"),
                            "system_state": risk_policy.get("system_state"),
                            "max_total_risk": risk_policy.get("max_total_risk"),
                            "effective_max_total_risk": risk_capital.get(
                                "effective_max_total_risk"
                            ),
                            "fact_age_seconds": risk_inputs.get("fact_age_seconds"),
                            "max_fact_age_seconds": risk_policy.get("max_fact_age_seconds"),
                            "position_status": (
                                "MISSING"
                                if not isinstance(risk_position, dict)
                                else risk_position.get("fact_status")
                            ),
                            "equity_status": (
                                "MISSING"
                                if not isinstance(risk_equity, dict)
                                else risk_equity.get("fact_status")
                            ),
                            "managed_capital_known": risk_capital.get("known"),
                            "protection_required": risk_inputs.get("protection_required"),
                            "protection_status": (
                                "NOT_REQUIRED"
                                if not risk_inputs.get("protection_required")
                                else (
                                    "MISSING"
                                    if not isinstance(risk_protection, dict)
                                    else risk_protection.get("status")
                                )
                            ),
                            "protection_fully_covered": (
                                None
                                if not isinstance(risk_protection, dict)
                                else risk_protection.get("fully_covered")
                            ),
                        },
                    },
                    "authorization": None
                    if authorization is None
                    else {
                        "authorization_id": str(authorization.authorization_id),
                        "workspace_id": str(workspace_id),
                        "team_id": str(authorization.team_id),
                        "account_id": authorization.account_id,
                        "environment": authorization.environment,
                        "created_at": _iso(authorization.created_at),
                        "quantity_limit": str(authorization.quantity_limit),
                        "used_quantity": str(authorization.used_quantity),
                        "remaining_quantity": str(
                            max(
                                Decimal(0),
                                authorization.quantity_limit - authorization.used_quantity,
                            )
                        ),
                        "risk_limit": str(authorization.risk_limit),
                        "allowed_adds": authorization.allowed_adds,
                        "used_adds": authorization.used_adds,
                        "add_revoked_at": _iso(authorization.add_revoked_at),
                        "active": authorization.active,
                        "expires_at": _iso(authorization.expires_at),
                    },
                    "initial_entry": None
                    if campaign is None or initial_intent is None
                    else {
                        "campaign_id": str(campaign.campaign_id),
                        "workspace_id": str(workspace_id),
                        "team_id": str(campaign.team_id),
                        "account_id": campaign.account_id,
                        "campaign_status": campaign.status,
                        "intent_id": str(initial_intent.intent_id),
                        "intent_status": initial_intent.status,
                        "created_at": _iso(initial_intent.created_at),
                    },
                }
            )
            return result

    def proposal_version(self, proposal_id: UUID) -> int:
        with self.database.session_factory() as session:
            proposal = session.get(Proposal, proposal_id)
            if proposal is None:
                raise DomainRejected("PROPOSAL_NOT_FOUND", "proposal does not exist")
            return proposal.version

    def reviewers_for_proposal(self, proposal_id: UUID) -> list[User]:
        with self.database.session_factory() as session:
            proposal = session.get(Proposal, proposal_id)
            if proposal is None:
                raise DomainRejected("PROPOSAL_NOT_FOUND", "proposal does not exist")
            reviewed_user_ids = set(
                session.scalars(
                    select(Approval.reviewer_id).where(Approval.proposal_id == proposal_id)
                ).all()
            )
            reused_actor_ids = set(
                session.scalars(
                    select(AuditEvent.actor_id).where(
                        AuditEvent.event_type == "PROPOSAL_DUPLICATE_REUSED",
                        AuditEvent.object_type == "Proposal",
                        AuditEvent.object_id == str(proposal_id),
                    )
                ).all()
            )
            assignments = session.scalars(
                select(RoleAssignment)
                .join(
                    TeamMembership,
                    and_(
                        TeamMembership.team_id == RoleAssignment.team_id,
                        TeamMembership.user_id == RoleAssignment.user_id,
                    ),
                )
                .where(
                    RoleAssignment.team_id == proposal.team_id,
                    RoleAssignment.role == Role.REVIEWER.value,
                    TeamMembership.active,
                )
            ).all()
            reviewer_ids = {
                item.user_id
                for item in assignments
                if (item.account_scope is None or item.account_scope == proposal.account_id)
                and (item.venue_scope is None or item.venue_scope == proposal.venue)
                and item.user_id != proposal.proposer_id
                and item.user_id not in reviewed_user_ids
                and str(item.user_id) not in reused_actor_ids
            }
            users = session.scalars(
                select(User).where(User.user_id.in_(reviewer_ids), User.active)
            ).all()
            for user in users:
                session.expunge(user)
            return list(users)

    def treasury_reviewers_for_transfer(self, transfer_proposal_id: UUID) -> list[User]:
        with self.database.session_factory() as session:
            proposal = session.get(TransferProposal, transfer_proposal_id)
            if proposal is None:
                raise DomainRejected(
                    "TRANSFER_PROPOSAL_NOT_FOUND", "transfer proposal does not exist"
                )
            assignments = session.scalars(
                select(RoleAssignment)
                .join(
                    TeamMembership,
                    and_(
                        TeamMembership.team_id == RoleAssignment.team_id,
                        TeamMembership.user_id == RoleAssignment.user_id,
                    ),
                )
                .where(
                    RoleAssignment.team_id == proposal.team_id,
                    RoleAssignment.role == Role.TREASURY_ADMIN.value,
                    TeamMembership.active,
                )
            ).all()
            reviewer_ids = {
                item.user_id
                for item in assignments
                if (item.account_scope is None or item.account_scope == proposal.account_id)
                and (item.venue_scope is None or item.venue_scope == proposal.venue)
                and item.user_id != proposal.proposer_id
            }
            users = session.scalars(
                select(User).where(User.user_id.in_(reviewer_ids), User.active)
            ).all()
            for user in users:
                session.expunge(user)
            return list(users)

    def treasury_users(self, team_id: UUID, account_id: str, venue: str) -> list[User]:
        with self.database.session_factory() as session:
            assignments = session.scalars(
                select(RoleAssignment)
                .join(
                    TeamMembership,
                    and_(
                        TeamMembership.team_id == RoleAssignment.team_id,
                        TeamMembership.user_id == RoleAssignment.user_id,
                    ),
                )
                .where(
                    RoleAssignment.team_id == team_id,
                    RoleAssignment.role == Role.TREASURY_ADMIN.value,
                    TeamMembership.active,
                )
            ).all()
            user_ids = {
                item.user_id
                for item in assignments
                if (item.account_scope is None or item.account_scope == account_id)
                and (item.venue_scope is None or item.venue_scope == venue)
            }
            users = session.scalars(
                select(User).where(User.user_id.in_(user_ids), User.active)
            ).all()
            for user in users:
                session.expunge(user)
            return list(users)

    def transfer_proposal_version(self, user_id: UUID, transfer_proposal_id: UUID) -> int:
        _workspace_id, team_id = self._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            proposal = session.get(TransferProposal, transfer_proposal_id)
            if proposal is None:
                raise DomainRejected(
                    "TRANSFER_PROPOSAL_NOT_FOUND", "transfer proposal does not exist"
                )
            if proposal.team_id != team_id:
                raise DomainRejected("TEAM_SCOPE_DENIED", "transfer proposal is outside scope")
            return proposal.version

    @staticmethod
    def _transfer_proposal_summary(item: TransferProposal) -> dict[str, Any]:
        return {
            "transfer_proposal_id": str(item.transfer_proposal_id),
            "team_id": str(item.team_id),
            "proposer_id": str(item.proposer_id),
            "environment": item.environment,
            "direction": item.direction,
            "purpose": item.purpose,
            "status": item.status,
            "version": item.version,
            "account_id": item.account_id,
            "venue": item.venue,
            "source_type": item.source_type,
            "source_id": item.source_id,
            "destination_type": item.destination_type,
            "destination_id": item.destination_id,
            "asset": item.asset,
            "network": item.network,
            "destination_reference": item.destination_reference,
            "amount": str(item.amount),
            "max_fee": str(item.max_fee),
            "min_received": str(item.min_received),
            "reason": item.reason,
            "frozen_at": _iso(item.frozen_at),
            "expires_at": _iso(item.expires_at),
            "created_at": _iso(item.created_at),
            "updated_at": _iso(item.updated_at),
        }

    def transfer_proposal_detail(self, user_id: UUID, transfer_proposal_id: UUID) -> dict[str, Any]:
        workspace_id, team_id = self._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            proposal = session.get(TransferProposal, transfer_proposal_id)
            if proposal is None:
                raise DomainRejected(
                    "TRANSFER_PROPOSAL_NOT_FOUND", "transfer proposal does not exist"
                )
            if proposal.team_id != team_id:
                raise DomainRejected("TEAM_SCOPE_DENIED", "transfer proposal is outside scope")
            if not self.service.can_user(
                user_id, "capital.view", proposal.account_id, proposal.venue
            ):
                raise DomainRejected("RBAC_DENIED", "transfer proposal is outside scope")
            approvals = session.scalars(
                select(Approval)
                .where(Approval.transfer_proposal_id == transfer_proposal_id)
                .order_by(Approval.created_at)
            ).all()
            authorization = session.scalar(
                select(TransferAuthorization).where(
                    TransferAuthorization.team_id == team_id,
                    TransferAuthorization.transfer_proposal_id == transfer_proposal_id
                )
            )
            result = self._transfer_proposal_summary(proposal)
            result["workspace_id"] = str(workspace_id)
            result.update(
                {
                    "approvals": [
                        {
                            "approval_id": str(item.approval_id),
                            "reviewer_id": str(item.reviewer_id),
                            "decision": item.decision,
                            "reason": item.reason,
                            "created_at": _iso(item.created_at),
                        }
                        for item in approvals
                    ],
                    "authorization": None
                    if authorization is None
                    else {
                        "transfer_authorization_id": str(authorization.transfer_authorization_id),
                        "active": authorization.active,
                        "version": authorization.version,
                        "expires_at": _iso(authorization.expires_at),
                        "amount_limit": str(authorization.amount_limit),
                    },
                }
            )
            return result

    @staticmethod
    def _capital_transfer_summary(item: CapitalTransfer) -> dict[str, Any]:
        return {
            "capital_transfer_id": str(item.capital_transfer_id),
            "team_id": str(item.team_id),
            "transfer_authorization_id": str(item.transfer_authorization_id),
            "environment": item.environment,
            "account_id": item.account_id,
            "venue": item.venue,
            "direction": item.direction,
            "source_id": item.source_id,
            "destination_id": item.destination_id,
            "asset": item.asset,
            "network": item.network,
            "status": item.status,
            "gross_amount": str(item.gross_amount),
            "reserved_amount": str(item.reserved_amount),
            "fee_amount": None if item.fee_amount is None else str(item.fee_amount),
            "net_received": None if item.net_received is None else str(item.net_received),
            "external_transfer_id": item.external_transfer_id,
            "transaction_reference": item.transaction_reference,
            "transport": item.transport,
            "chain_id": item.chain_id,
            "transport_state": item.transport_state,
            "planned_transactions": item.planned_transactions,
            "confirmed_transaction_hashes": item.confirmed_transaction_hashes,
            "protocol_request_id": item.protocol_request_id,
            "protocol_execute_after": _iso(item.protocol_execute_after),
            "protocol_expires_at": _iso(item.protocol_expires_at),
            "reconciliation_status": item.reconciliation_status,
            "reconciliation_details": item.reconciliation_details,
            "version": item.version,
            "observed_at": _iso(item.observed_at),
            "reconciled_at": _iso(item.reconciled_at),
            "updated_at": _iso(item.updated_at),
        }

    def capital_transfer_detail(self, user_id: UUID, capital_transfer_id: UUID) -> dict[str, Any]:
        workspace_id, team_id = self._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            transfer = session.get(CapitalTransfer, capital_transfer_id)
            if transfer is None:
                raise DomainRejected("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer is missing")
            if transfer.team_id != team_id:
                raise DomainRejected("TEAM_SCOPE_DENIED", "capital transfer is outside scope")
            if not self.service.can_user(
                user_id, "capital.view", transfer.account_id, transfer.venue
            ):
                raise DomainRejected("RBAC_DENIED", "capital transfer is outside scope")
            result = self._capital_transfer_summary(transfer)
            result["workspace_id"] = str(workspace_id)
            return result

    def capital_center(
        self,
        user_id: UUID,
        *,
        authoritative_live_accounts: dict[str, str] | None = None,
        authoritative_live_treasury_account_id: str | None = None,
        require_authoritative_live_treasury: bool = False,
    ) -> dict[str, Any]:
        workspace_id, team_id = self._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            now = datetime.now(UTC)
            authoritative_accounts = {
                venue.upper(): account_id
                for venue, account_id in (authoritative_live_accounts or {}).items()
                if account_id
            }

            def is_authoritative_live_venue(
                environment: str,
                location_type: str,
                venue: str,
                account_id: str,
            ) -> bool:
                if environment != "LIVE" or location_type != "VENUE":
                    return True
                expected = authoritative_accounts.get(venue.upper())
                return expected is None or expected == account_id

            def is_authoritative_live_treasury(
                environment: str,
                location_type: str,
                account_id: str,
            ) -> bool:
                if environment != "LIVE" or location_type != "VAULT":
                    return True
                if authoritative_live_treasury_account_id is None:
                    return not require_authoritative_live_treasury
                return account_id.lower() == authoritative_live_treasury_account_id.lower()

            assignments = session.scalars(
                select(RoleAssignment).where(
                    RoleAssignment.user_id == user_id,
                    RoleAssignment.team_id == team_id,
                )
            ).all()
            if not self.service.can_user(user_id, "capital.view"):
                raise DomainRejected("RBAC_DENIED", "capital center access is not assigned")
            treasury_assignments = [
                item
                for item in assignments
                if item.role in {Role.TREASURY_ADMIN.value, Role.SYSTEM_ADMIN.value}
            ]

            def can_view_history(item: AccountEquityObservation) -> bool:
                if not is_authoritative_live_venue(
                    item.environment,
                    item.location_type,
                    item.venue,
                    item.account_id,
                ):
                    return False
                if not is_authoritative_live_treasury(
                    item.environment,
                    item.location_type,
                    item.account_id,
                ):
                    return False
                if item.location_type == "VAULT":
                    return any(
                        assignment.account_scope is None and assignment.venue_scope is None
                        for assignment in treasury_assignments
                    )
                return any(
                    (
                        assignment.account_scope is None
                        or assignment.account_scope == item.account_id
                    )
                    and (assignment.venue_scope is None or assignment.venue_scope == item.venue)
                    for assignment in treasury_assignments
                )

            balances = session.scalars(
                select(AccountEquity)
                .where(AccountEquity.team_id == team_id)
                .order_by(
                    AccountEquity.location_type,
                    AccountEquity.venue,
                    AccountEquity.account_id,
                )
            ).all()
            proposals = session.scalars(
                select(TransferProposal)
                .where(TransferProposal.team_id == team_id)
                .order_by(TransferProposal.updated_at.desc())
            ).all()
            authorizations = session.scalars(
                select(TransferAuthorization).where(TransferAuthorization.team_id == team_id)
            ).all()
            authorization_by_proposal = {item.transfer_proposal_id: item for item in authorizations}
            transfers = session.scalars(
                select(CapitalTransfer)
                .where(CapitalTransfer.team_id == team_id)
                .order_by(CapitalTransfer.updated_at.desc())
            ).all()
            direct_operations = session.scalars(
                select(DirectCapitalOperation)
                .where(DirectCapitalOperation.team_id == team_id)
                .order_by(DirectCapitalOperation.updated_at.desc())
            ).all()
            policies = session.scalars(
                select(CapitalAutomationPolicy)
                .where(CapitalAutomationPolicy.team_id == team_id)
                .order_by(
                    CapitalAutomationPolicy.environment,
                    CapitalAutomationPolicy.venue,
                    CapitalAutomationPolicy.account_id,
                )
            ).all()
            observation_query = select(AccountEquityObservation).where(
                AccountEquityObservation.team_id == team_id,
                AccountEquityObservation.environment == "LIVE",
            )
            if authoritative_accounts:
                authoritative_history_scopes = [
                    AccountEquityObservation.location_type == "VAULT",
                    *[
                        and_(
                            AccountEquityObservation.location_type == "VENUE",
                            func.upper(AccountEquityObservation.venue) == venue,
                            AccountEquityObservation.account_id == account_id,
                        )
                        for venue, account_id in authoritative_accounts.items()
                    ],
                ]
                observation_query = observation_query.where(or_(*authoritative_history_scopes))
            if require_authoritative_live_treasury:
                observation_query = observation_query.where(
                    or_(
                        AccountEquityObservation.location_type != "VAULT",
                        (
                            false()
                            if authoritative_live_treasury_account_id is None
                            else func.lower(AccountEquityObservation.account_id)
                            == authoritative_live_treasury_account_id.lower()
                        ),
                    )
                )
            observations = list(
                reversed(
                    session.scalars(
                        observation_query.order_by(AccountEquityObservation.observed_at.desc())
                    ).all()
                )
            )
            visible_observations = [item for item in observations if can_view_history(item)]
            risk_policy = session.scalar(
                select(RiskPolicy).where(
                    RiskPolicy.team_id == team_id,
                    RiskPolicy.active,
                )
            )
            max_fact_age = timedelta(
                seconds=(risk_policy.max_fact_age_seconds if risk_policy is not None else 300)
            )
            visible_transfers = [
                item
                for item in transfers
                if self.service.can_user(user_id, "capital.view", item.account_id, item.venue)
            ]
            occupied_statuses = {
                "SOURCE_RESERVED",
                "SUBMITTED",
                "IN_FLIGHT",
                "DESTINATION_CONFIRMED",
                "UNKNOWN",
                "MANUAL_REQUIRED",
            }
            balance_data: list[dict[str, Any]] = []
            valuation_issues: set[str] = set()
            venue_net_worth: dict[str, Decimal] = {}
            vault_net_worth = Decimal(0)
            total_net_worth = Decimal(0)
            live_balance_count = 0
            live_sources: set[str] = set()
            current_live_sources: set[str] = set()
            latest_live_source_times: dict[str, datetime] = {}
            current_live_source_times: dict[str, datetime] = {}
            for item in balances:
                if not is_authoritative_live_venue(
                    item.environment,
                    item.location_type,
                    item.venue,
                    item.account_id,
                ):
                    continue
                if not is_authoritative_live_treasury(
                    item.environment,
                    item.location_type,
                    item.account_id,
                ):
                    continue
                can_view = (
                    self.service.can_user(user_id, "capital.view", item.account_id, item.venue)
                    if item.location_type == "VENUE"
                    else self.service.can_user(user_id, "capital.view")
                )
                if not can_view:
                    continue
                occupied = sum(
                    (
                        transfer.reserved_amount
                        for transfer in visible_transfers
                        if transfer.environment == item.environment
                        and transfer.source_id == item.account_id
                        and transfer.asset == item.currency
                        and transfer.status in occupied_statuses
                    ),
                    Decimal(0),
                )
                confirmed_available = (
                    item.available_balance
                    if item.withdrawable_balance is None
                    else item.withdrawable_balance
                )
                valuation_time = item.observed_at
                usd_equity: Decimal | None
                valuation_price: Decimal | None
                if item.currency.upper() in USD_STABLE_ASSETS:
                    usd_equity = item.equity
                    valuation_price = Decimal(1)
                else:
                    usd_equity = item.valuation_equity
                    valuation_price = item.valuation_price
                    if item.valuation_observed_at is not None:
                        valuation_time = min(valuation_time, item.valuation_observed_at)
                valuation_current = (
                    item.fact_status == "KNOWN"
                    and usd_equity is not None
                    and valuation_price is not None
                    and valuation_price > 0
                    and not self.service._fact_is_stale(valuation_time, now, max_fact_age)
                )
                source = "VAULT" if item.location_type == "VAULT" else item.venue
                if item.environment == "LIVE":
                    previous_time = latest_live_source_times.get(source)
                    if previous_time is None or valuation_time > previous_time:
                        latest_live_source_times[source] = valuation_time
                if item.environment == "LIVE":
                    live_balance_count += 1
                    live_sources.add("VAULT" if item.location_type == "VAULT" else item.venue)
                if valuation_current and item.environment == "LIVE":
                    assert usd_equity is not None
                    current_live_sources.add(source)
                    previous_current_time = current_live_source_times.get(source)
                    if previous_current_time is None or valuation_time < previous_current_time:
                        current_live_source_times[source] = valuation_time
                    total_net_worth += usd_equity
                    if item.location_type == "VAULT":
                        vault_net_worth += usd_equity
                    else:
                        venue_net_worth[item.venue] = (
                            venue_net_worth.get(item.venue, Decimal(0)) + usd_equity
                        )
                elif item.environment == "LIVE":
                    source = "VAULT" if item.location_type == "VAULT" else item.venue
                    if item.fact_status != "KNOWN":
                        valuation_issues.add(f"CURRENT_VALUE_MISSING:{source}")
                    elif usd_equity is None or valuation_price is None or valuation_price <= 0:
                        valuation_issues.add(f"UNKNOWN_USD_VALUE:{source}")
                    elif self.service._fact_is_stale(valuation_time, now, max_fact_age):
                        valuation_issues.add(f"STALE_LIVE_SOURCE:{source}")
                    else:
                        valuation_issues.add(f"CURRENT_VALUE_MISSING:{source}")
                balance_data.append(
                    {
                        "account_equity_id": str(item.account_equity_id),
                        "environment": item.environment,
                        "location_type": item.location_type,
                        "location_id": (
                            "selected-onchain-treasury"
                            if item.environment == "LIVE" and item.location_type == "VAULT"
                            else item.account_id
                        ),
                        "venue": item.venue,
                        "asset": item.currency,
                        "equity": str(item.equity),
                        "confirmed_available": str(confirmed_available),
                        "source_reserved": str(occupied),
                        "effective_available": str(max(Decimal(0), confirmed_available - occupied)),
                        "control_status": item.control_status,
                        "deposit_status": item.deposit_status,
                        "network": item.network,
                        "address_reference": (
                            None
                            if item.environment == "LIVE" and item.location_type == "VAULT"
                            else item.address_reference
                        ),
                        "valuation_currency": (
                            "USD"
                            if item.currency.upper() in USD_STABLE_ASSETS
                            else item.valuation_currency
                        ),
                        "valuation_price": (
                            None if valuation_price is None else str(valuation_price)
                        ),
                        "usd_equity": (None if not valuation_current else str(usd_equity)),
                        "valuation_observed_at": _iso(item.valuation_observed_at),
                        "valuation_current": valuation_current,
                        "fact_status": item.fact_status,
                        "observed_at": _iso(item.observed_at),
                    }
                )
            for required_source in ("BINANCE", "HYPERLIQUID", "VAULT"):
                if required_source not in live_sources:
                    valuation_issues.add(f"MISSING_LIVE_SOURCE:{required_source}")
                elif required_source not in current_live_sources and not any(
                    issue.endswith(f":{required_source}") for issue in valuation_issues
                ):
                    valuation_issues.add(f"CURRENT_VALUE_MISSING:{required_source}")
            alignment_tolerance = timedelta(seconds=60)
            required_sources = {"BINANCE", "HYPERLIQUID", "VAULT"}
            if required_sources.issubset(current_live_source_times):
                newest_source_time = max(current_live_source_times.values())
                for source, source_time in current_live_source_times.items():
                    if newest_source_time - source_time > alignment_tolerance:
                        valuation_issues.add(f"TIME_MISALIGNED_SOURCE:{source}")
            gate = session.get(CapabilityGate, "CAPITAL_TRANSFER")
            automation_gates = {
                key: (None if (value := session.get(CapabilityGate, key)) is None else value.status)
                for key in ("AUTO_PROFIT_SWEEP", "AUTO_OPERATING_REFILL")
            }
            return {
                "workspace_id": str(workspace_id),
                "team_id": str(team_id),
                "real_transfer_gate": None if gate is None else gate.status,
                "real_transfer_reason": None if gate is None else gate.reason,
                "balances": balance_data,
                "history": [
                    {
                        "source": ("VAULT" if item.location_type == "VAULT" else item.venue),
                        "location_type": item.location_type,
                        "location_id": (
                            "selected-onchain-treasury"
                            if item.location_type == "VAULT"
                            else item.account_id
                        ),
                        "venue": item.venue,
                        "asset": item.currency,
                        "equity": str(item.equity),
                        "available_balance": str(item.available_balance),
                        "usd_equity": (None if item.usd_equity is None else str(item.usd_equity)),
                        "observed_at": _iso(item.observed_at),
                    }
                    for item in visible_observations
                ],
                "history_retention": {
                    "complete": True,
                    "minimum_interval_seconds": 60,
                    "first_observed_at": _iso(visible_observations[0].observed_at)
                    if visible_observations
                    else None,
                    "last_observed_at": _iso(visible_observations[-1].observed_at)
                    if visible_observations
                    else None,
                    "stored_observations": len(visible_observations),
                },
                "net_worth": {
                    "environment": "LIVE",
                    "currency": "USD",
                    "max_fact_age_seconds": int(max_fact_age.total_seconds()),
                    "alignment_tolerance_seconds": int(alignment_tolerance.total_seconds()),
                    "source_as_of": {
                        source: _iso(latest_live_source_times.get(source))
                        for source in ("BINANCE", "HYPERLIQUID", "VAULT")
                    },
                    "venues": {
                        venue: (
                            str(venue_net_worth[venue]) if venue in current_live_sources else None
                        )
                        for venue in ("BINANCE", "HYPERLIQUID")
                    },
                    "vault": str(vault_net_worth) if "VAULT" in current_live_sources else None,
                    "total": (
                        str(total_net_worth)
                        if {"BINANCE", "HYPERLIQUID", "VAULT"}.issubset(current_live_sources)
                        and not valuation_issues
                        else None
                    ),
                    "complete": live_balance_count > 0 and not valuation_issues,
                    "issues": sorted(valuation_issues),
                    "as_of": now.isoformat(),
                },
                "in_transit": str(
                    sum(
                        (
                            item.reserved_amount
                            for item in visible_transfers
                            if item.status in occupied_statuses
                        ),
                        Decimal(0),
                    )
                ),
                "proposals": [
                    {
                        **self._transfer_proposal_summary(item),
                        "authorization": (
                            None
                            if (
                                authorization := authorization_by_proposal.get(
                                    item.transfer_proposal_id
                                )
                            )
                            is None
                            else {
                                "transfer_authorization_id": str(
                                    authorization.transfer_authorization_id
                                ),
                                "active": authorization.active,
                                "expires_at": _iso(authorization.expires_at),
                            }
                        ),
                    }
                    for item in proposals
                    if self.service.can_user(user_id, "capital.view", item.account_id, item.venue)
                ],
                "transfers": [self._capital_transfer_summary(item) for item in visible_transfers],
                "direct_operations": [
                    {
                        "operation_id": str(item.operation_id),
                        "path": item.path,
                        "treasury_provider": item.treasury_provider,
                        "status": item.status,
                        "receipt_status": item.receipt_status,
                        "account_id": item.account_id,
                        "venue": item.venue,
                        "vault_id": item.vault_id,
                        "asset": item.asset,
                        "network": item.network,
                        "amount": str(item.amount),
                        "max_fee": None if item.max_fee is None else str(item.max_fee),
                        "min_received": (
                            None if item.min_received is None else str(item.min_received)
                        ),
                        "source_reference_configured": item.source_reference is not None,
                        "destination_reference_configured": (
                            item.destination_reference is not None
                        ),
                        "stages": item.stages,
                        "blockers": item.blockers,
                        "execute_after": _iso(item.execute_after),
                        "expires_at": _iso(item.expires_at),
                        "final_confirmed_at": _iso(item.final_confirmed_at),
                        "version": item.version,
                        "updated_at": _iso(item.updated_at),
                    }
                    for item in direct_operations
                    if self.service.can_user(user_id, "capital.view", item.account_id, item.venue)
                ],
                "automation": {
                    "gates": automation_gates,
                    "policies": [
                        {
                            "policy_id": str(item.policy_id),
                            "environment": item.environment,
                            "account_id": item.account_id,
                            "venue": item.venue,
                            "vault_id": item.vault_id,
                            "asset": item.asset,
                            "network": item.network,
                            "operating_low": str(item.operating_low),
                            "operating_target": str(item.operating_target),
                            "operating_high": str(item.operating_high),
                            "vault_minimum_reserve": str(item.vault_minimum_reserve),
                            "minimum_transfer": str(item.minimum_transfer),
                            "maximum_transfer": str(item.maximum_transfer),
                            "max_fee": str(item.max_fee),
                            "active": item.active,
                            "version": item.version,
                            "updated_at": _iso(item.updated_at),
                        }
                        for item in policies
                        if self.service.can_user(
                            user_id, "capital.view", item.account_id, item.venue
                        )
                    ],
                },
            }

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
                .where(NotificationRoute.team_id == team_id)
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

    def actual_results(
        self,
        user_id: UUID,
        environment: str,
        *,
        source: str | None = None,
        source_type: str | None = None,
        source_candidate_id: str | None = None,
        source_version: str | None = None,
        strategy_id: str | None = None,
        strategy_version: str | None = None,
        signal_source_mode: str | None = None,
        signal_provider: str | None = None,
        venue: str | None = None,
        account_id: str | None = None,
        instrument_id: UUID | None = None,
        direction: str | None = None,
        risk_tier: str | None = None,
        campaign_id: UUID | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
    ) -> dict[str, Any]:
        if environment not in {"SHADOW", "TESTNET", "LIVE"}:
            raise DomainRejected("ENVIRONMENT_INVALID", "results require an exact environment")
        if signal_source_mode not in {None, "PERPTAPE", "WEBHOOK", "MANUAL", "SYSTEM"}:
            raise DomainRejected(
                "SIGNAL_SOURCE_MODE_INVALID",
                "results require an exact supported signal source mode",
            )
        if signal_provider not in {None, "TRADINGVIEW", "MODEL", "PERPTAPE"}:
            raise DomainRejected(
                "SIGNAL_PROVIDER_INVALID",
                "results require an exact supported signal provider",
            )
        if from_time is not None and to_time is not None and from_time > to_time:
            raise DomainRejected("TIME_RANGE_INVALID", "results from_time must not exceed to_time")
        workspace_id, team_id = self._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            campaign_query = select(Campaign).where(
                Campaign.environment == environment,
                Campaign.team_id == team_id,
            )
            for field, value in (
                (Campaign.venue, venue),
                (Campaign.account_id, account_id),
                (Campaign.instrument_id, instrument_id),
                (Campaign.direction, direction),
                (Campaign.campaign_id, campaign_id),
            ):
                if value is not None:
                    campaign_query = campaign_query.where(field == value)
            if from_time is not None:
                campaign_query = campaign_query.where(Campaign.updated_at >= from_time)
            if to_time is not None:
                campaign_query = campaign_query.where(Campaign.updated_at <= to_time)
            campaigns = [
                item
                for item in session.scalars(
                    campaign_query.order_by(Campaign.updated_at, Campaign.campaign_id)
                ).all()
                if self.service.can_user(user_id, "view", item.account_id, item.venue)
            ]
            attribution_cache: dict[UUID, dict[str, Any]] = {}

            def report_attribution(proposal: Proposal) -> dict[str, Any]:
                attribution = attribution_cache.get(proposal.proposal_id)
                if attribution is None:
                    attribution = _report_attribution(session, proposal)
                    attribution_cache[proposal.proposal_id] = attribution
                return attribution

            rows: list[dict[str, Any]] = []
            totals: dict[str, dict[str, Decimal]] = {}
            for campaign in campaigns:
                proposal = session.get(Proposal, campaign.proposal_id)
                attribution = None if proposal is None else report_attribution(proposal)
                if source is not None and (proposal is None or proposal.source != source):
                    continue
                if source_type is not None and (
                    attribution is None or attribution["source_type"] != source_type
                ):
                    continue
                if source_candidate_id is not None and (
                    proposal is None or proposal.source_candidate_id != source_candidate_id
                ):
                    continue
                if source_version is not None and (
                    attribution is None or attribution["strategy_version"] != source_version
                ):
                    continue
                if strategy_id is not None and (
                    attribution is None or attribution["strategy_id"] != strategy_id
                ):
                    continue
                if strategy_version is not None and (
                    attribution is None or attribution["strategy_version"] != strategy_version
                ):
                    continue
                if signal_source_mode is not None and (
                    attribution is None or attribution["signal_source_mode"] != signal_source_mode
                ):
                    continue
                if signal_provider is not None and (
                    attribution is None or attribution["signal_provider"] != signal_provider
                ):
                    continue
                if risk_tier is not None and (proposal is None or proposal.risk_tier != risk_tier):
                    continue
                instrument = session.get(Instrument, campaign.instrument_id)
                intents = session.scalars(
                    select(OrderIntent).where(OrderIntent.campaign_id == campaign.campaign_id)
                ).all()
                intent_ids = [item.intent_id for item in intents]
                fills = (
                    session.scalars(
                        select(VenueFill)
                        .where(VenueFill.order_intent_id.in_(intent_ids))
                        .order_by(VenueFill.executed_at, VenueFill.venue_fill_id)
                    ).all()
                    if intent_ids
                    else []
                )
                funding = session.scalars(
                    select(FundingPayment).where(FundingPayment.campaign_id == campaign.campaign_id)
                ).all()
                currency = "UNKNOWN" if instrument is None else instrument.collateral_currency
                fees = sum((item.fee for item in fills), Decimal(0))
                slippage = sum((item.slippage_cost for item in fills), Decimal(0))
                funding_total = sum((item.amount for item in funding), Decimal(0))
                total_bucket = totals.setdefault(
                    currency,
                    {
                        "realized_pnl": Decimal(0),
                        "unrealized_pnl": Decimal(0),
                        "final_pnl": Decimal(0),
                        "fees": Decimal(0),
                        "funding": Decimal(0),
                        "slippage": Decimal(0),
                    },
                )
                total_bucket["realized_pnl"] += campaign.realized_pnl
                total_bucket["unrealized_pnl"] += campaign.unrealized_pnl
                total_bucket["final_pnl"] += campaign.final_pnl
                total_bucket["fees"] += fees
                total_bucket["funding"] += funding_total
                total_bucket["slippage"] += slippage
                rows.append(
                    {
                        "campaign_id": str(campaign.campaign_id),
                        "workspace_id": str(workspace_id),
                        "team_id": str(team_id),
                        "environment": campaign.environment,
                        "actuality": {
                            "SHADOW": "SYNTHETIC_RECORDED_FACTS",
                            "TESTNET": "NON_PRODUCTION_RECORDED_FACTS",
                            "LIVE": "LIVE_RECORDED_FACTS",
                        }[campaign.environment],
                        "status": campaign.status,
                        "account_id": campaign.account_id,
                        "venue": campaign.venue,
                        "instrument_id": str(campaign.instrument_id),
                        "symbol": None if instrument is None else instrument.symbol,
                        "currency": currency,
                        "direction": campaign.direction,
                        "source": None if proposal is None else proposal.source,
                        "source_type": (
                            None if attribution is None else attribution["source_type"]
                        ),
                        "strategy_id": (
                            None if attribution is None else attribution["strategy_id"]
                        ),
                        "strategy_version": (
                            None if attribution is None else attribution["strategy_version"]
                        ),
                        "signal_source_mode": (
                            None if attribution is None else attribution["signal_source_mode"]
                        ),
                        "signal_source_id": (
                            None if attribution is None else attribution["signal_source_id"]
                        ),
                        "signal_provider": (
                            None if attribution is None else attribution["signal_provider"]
                        ),
                        "signal_external_id": (
                            None if attribution is None else attribution["signal_external_id"]
                        ),
                        "source_attribution": (
                            None if attribution is None else attribution["attribution"]
                        ),
                        "source_candidate_id": (
                            None if proposal is None else proposal.source_candidate_id
                        ),
                        "source_version": (
                            None if attribution is None else attribution["strategy_version"]
                        ),
                        "risk_tier": None if proposal is None else proposal.risk_tier,
                        "fill_count": len(fills),
                        "filled_quantity": str(sum((item.quantity for item in fills), Decimal(0))),
                        "realized_pnl": str(campaign.realized_pnl),
                        "unrealized_pnl": str(campaign.unrealized_pnl),
                        "final_pnl": str(campaign.final_pnl),
                        "fees": str(fees),
                        "funding": str(funding_total),
                        "slippage": str(slippage),
                        "created_at": _iso(campaign.created_at),
                        "updated_at": _iso(campaign.updated_at),
                    }
                )

            risk_proposal_query = select(Proposal).where(
                Proposal.environment == environment,
                Proposal.team_id == team_id,
            )
            for field, value in (
                (Proposal.venue, venue),
                (Proposal.account_id, account_id),
                (Proposal.instrument_id, instrument_id),
                (Proposal.direction, direction),
            ):
                if value is not None:
                    risk_proposal_query = risk_proposal_query.where(field == value)
            campaign_proposal_ids = {
                item.proposal_id for item in campaigns if campaign_id in {None, item.campaign_id}
            }
            risk_proposals: dict[UUID, tuple[Proposal, dict[str, Any]]] = {}
            for proposal in session.scalars(risk_proposal_query).all():
                if not self.service.can_user(user_id, "view", proposal.account_id, proposal.venue):
                    continue
                if campaign_id is not None and proposal.proposal_id not in campaign_proposal_ids:
                    continue
                attribution = report_attribution(proposal)
                if source is not None and proposal.source != source:
                    continue
                if source_type is not None and attribution["source_type"] != source_type:
                    continue
                if source_candidate_id is not None and (
                    proposal.source_candidate_id != source_candidate_id
                ):
                    continue
                if source_version is not None and (
                    attribution["strategy_version"] != source_version
                ):
                    continue
                if strategy_id is not None and attribution["strategy_id"] != strategy_id:
                    continue
                if strategy_version is not None and (
                    attribution["strategy_version"] != strategy_version
                ):
                    continue
                if signal_source_mode is not None and (
                    attribution["signal_source_mode"] != signal_source_mode
                ):
                    continue
                if signal_provider is not None and (
                    attribution["signal_provider"] != signal_provider
                ):
                    continue
                if risk_tier is not None and proposal.risk_tier != risk_tier:
                    continue
                risk_proposals[proposal.proposal_id] = (proposal, attribution)

            risk_events: list[dict[str, Any]] = []
            if risk_proposals:
                decision_query = select(RiskDecision).where(
                    RiskDecision.team_id == team_id,
                    RiskDecision.proposal_id.in_(risk_proposals),
                )
                if from_time is not None:
                    decision_query = decision_query.where(RiskDecision.created_at >= from_time)
                if to_time is not None:
                    decision_query = decision_query.where(RiskDecision.created_at <= to_time)
                for decision in session.scalars(
                    decision_query.order_by(RiskDecision.created_at, RiskDecision.decision_id)
                ).all():
                    proposal, attribution = risk_proposals[decision.proposal_id]
                    policy = session.get(RiskPolicy, decision.policy_id)
                    risk_events.append(
                        {
                            "decision_id": str(decision.decision_id),
                            "proposal_id": str(proposal.proposal_id),
                            "workspace_id": str(workspace_id),
                            "team_id": str(team_id),
                            "environment": environment,
                            "account_id": proposal.account_id,
                            "venue": proposal.venue,
                            "instrument_id": str(proposal.instrument_id),
                            "direction": proposal.direction,
                            "risk_tier": proposal.risk_tier,
                            "source": proposal.source,
                            **attribution,
                            "result": decision.result,
                            "reasons": list(decision.reasons),
                            "risk_amount": str(decision.risk_amount),
                            "approved_quantity": str(decision.approved_quantity),
                            "policy_id": str(decision.policy_id),
                            "policy_version": None if policy is None else policy.version,
                            "policy_revision": None if policy is None else policy.revision,
                            "data_as_of": _iso(decision.data_as_of),
                            "created_at": _iso(decision.created_at),
                        }
                    )

            curves: dict[str, dict[str, Any]] = {}
            for currency in totals:
                metrics = _performance_metrics(
                    [item for item in rows if item["currency"] == currency]
                )
                curves[currency] = {
                    "points": metrics["curve"],
                    "maximum_drawdown": metrics["maximum_drawdown"],
                    "unit": currency,
                    "percentage_available": False,
                }

            team = session.get(Team, team_id)
            team_name = "Unknown Team" if team is None else team.name
            dimension_buckets: dict[str, dict[str, dict[str, Any]]] = {
                "team": {
                    str(team_id): {
                        "key": str(team_id),
                        "label": team_name,
                        "scope": {"team_id": str(team_id), "team_name": team_name},
                        "campaigns": [],
                        "risk_events": [],
                    }
                },
                "account": {},
                "strategy": {},
                "signal_source": {},
            }

            def add_dimension_record(record: dict[str, Any], *, risk_event: bool) -> None:
                strategy_key = ":".join(
                    [
                        str(record.get("strategy_id") or "MANUAL"),
                        str(record.get("strategy_version") or "UNVERSIONED"),
                    ]
                )
                signal_key = ":".join(
                    [
                        str(record.get("signal_source_mode") or "UNKNOWN"),
                        str(record.get("signal_source_id") or "NO_SOURCE_ID"),
                        str(record.get("signal_provider") or "NO_PROVIDER"),
                    ]
                )
                descriptors = {
                    "team": (
                        str(team_id),
                        team_name,
                        {"team_id": str(team_id), "team_name": team_name},
                    ),
                    "account": (
                        f"{record['venue']}:{record['account_id']}",
                        f"{record['account_id']} / {record['venue']}",
                        {
                            "account_id": record["account_id"],
                            "venue": record["venue"],
                        },
                    ),
                    "strategy": (
                        strategy_key,
                        (
                            "MANUAL"
                            if record.get("strategy_id") is None
                            else (
                                f"{record['strategy_id']} / {record.get('strategy_version') or '—'}"
                            )
                        ),
                        {
                            "strategy_id": record.get("strategy_id"),
                            "strategy_version": record.get("strategy_version"),
                        },
                    ),
                    "signal_source": (
                        signal_key,
                        " / ".join(
                            filter(
                                None,
                                [
                                    record.get("signal_source_mode"),
                                    record.get("signal_provider"),
                                ],
                            )
                        ),
                        {
                            "signal_source_mode": record.get("signal_source_mode"),
                            "signal_source_id": record.get("signal_source_id"),
                            "signal_provider": record.get("signal_provider"),
                        },
                    ),
                }
                target = "risk_events" if risk_event else "campaigns"
                for dimension, (key, label, scope) in descriptors.items():
                    bucket = dimension_buckets[dimension].setdefault(
                        key,
                        {
                            "key": key,
                            "label": label,
                            "scope": scope,
                            "campaigns": [],
                            "risk_events": [],
                        },
                    )
                    bucket[target].append(record)

            for row in rows:
                add_dimension_record(row, risk_event=False)
            for event in risk_events:
                add_dimension_record(event, risk_event=True)

            dimensions: dict[str, list[dict[str, Any]]] = {}
            for dimension, buckets in dimension_buckets.items():
                groups: list[dict[str, Any]] = []
                for dimension_bucket in buckets.values():
                    currency_rows: dict[str, list[dict[str, Any]]] = {}
                    for row in dimension_bucket["campaigns"]:
                        currency_rows.setdefault(str(row["currency"]), []).append(row)
                    result_counts: dict[str, int] = {}
                    reason_counts: dict[str, int] = {}
                    for event in dimension_bucket["risk_events"]:
                        result_counts[event["result"]] = result_counts.get(event["result"], 0) + 1
                        for reason in event["reasons"]:
                            reason_counts[reason] = reason_counts.get(reason, 0) + 1
                    groups.append(
                        {
                            "key": dimension_bucket["key"],
                            "label": dimension_bucket["label"],
                            "scope": dimension_bucket["scope"],
                            "campaign_count": len(dimension_bucket["campaigns"]),
                            "risk_event_count": len(dimension_bucket["risk_events"]),
                            "risk_events_by_result": result_counts,
                            "risk_events_by_reason": reason_counts,
                            "metrics_by_currency": {
                                currency: _performance_metrics(currency_campaigns)
                                for currency, currency_campaigns in currency_rows.items()
                            },
                        }
                    )
                dimensions[dimension] = sorted(groups, key=lambda item: item["label"])

            return {
                "scope": {
                    "workspace_id": str(workspace_id),
                    "team_id": str(team_id),
                    "team_name": team_name,
                },
                "environment": environment,
                "report_state": "RECORDED_HISTORY",
                "data_status": "AVAILABLE" if rows or risk_events else "EMPTY",
                "filters": {
                    "source": source,
                    "source_type": source_type,
                    "source_candidate_id": source_candidate_id,
                    "source_version": source_version,
                    "strategy_id": strategy_id,
                    "strategy_version": strategy_version,
                    "signal_source_mode": signal_source_mode,
                    "signal_provider": signal_provider,
                    "venue": venue,
                    "account_id": account_id,
                    "instrument_id": None if instrument_id is None else str(instrument_id),
                    "direction": direction,
                    "risk_tier": risk_tier,
                    "campaign_id": None if campaign_id is None else str(campaign_id),
                    "from": _iso(from_time),
                    "to": _iso(to_time),
                },
                "environment_notice": {
                    "SHADOW": "Synthetic facts; not exchange execution or profit",
                    "TESTNET": "Recorded non-production facts; not live profit",
                    "LIVE": "Recorded LIVE facts; no profitability guarantee",
                }[environment],
                "campaigns": rows,
                "risk_events": risk_events,
                "dimensions": dimensions,
                "coverage": {
                    "campaign_count": len(rows),
                    "closed_campaign_count": sum(1 for item in rows if item["status"] == "CLOSED"),
                    "risk_event_count": len(risk_events),
                    "currency_mixing": "SEPARATED",
                    "percentage_metrics": "OPENING_CAPITAL_UNAVAILABLE",
                    "time_filter_semantics": {
                        "campaigns": "campaign.updated_at",
                        "risk_events": "risk_decision.created_at",
                    },
                },
                "totals_by_currency": {
                    currency: {key: str(value) for key, value in values.items()}
                    for currency, values in totals.items()
                },
                "curves_by_currency": curves,
            }

    def audit_timeline(
        self, user_id: UUID, environment: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        if environment not in {"SHADOW", "TESTNET", "LIVE"}:
            raise DomainRejected("ENVIRONMENT_INVALID", "audit requires an exact environment")
        workspace_id, team_id = self._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            object_ids: set[str] = set()
            proposals = [
                item
                for item in session.scalars(
                    select(Proposal).where(
                        Proposal.environment == environment,
                        Proposal.team_id == team_id,
                    )
                ).all()
                if self.service.can_user(user_id, "view", item.account_id, item.venue)
            ]
            proposal_ids = [item.proposal_id for item in proposals]
            object_ids.update(str(item.proposal_id) for item in proposals)
            campaigns = [
                item
                for item in session.scalars(
                    select(Campaign).where(
                        Campaign.environment == environment,
                        Campaign.team_id == team_id,
                    )
                ).all()
                if self.service.can_user(user_id, "view", item.account_id, item.venue)
            ]
            campaign_ids = [item.campaign_id for item in campaigns]
            object_ids.update(str(item.campaign_id) for item in campaigns)
            if proposal_ids:
                object_ids.update(
                    str(item.decision_id)
                    for item in session.scalars(
                        select(RiskDecision).where(RiskDecision.proposal_id.in_(proposal_ids))
                    ).all()
                )
                object_ids.update(
                    str(item.authorization_id)
                    for item in session.scalars(
                        select(TradingAuthorization).where(
                            TradingAuthorization.proposal_id.in_(proposal_ids)
                        )
                    ).all()
                )
            if campaign_ids:
                intents = session.scalars(
                    select(OrderIntent).where(OrderIntent.campaign_id.in_(campaign_ids))
                ).all()
                intent_ids = [item.intent_id for item in intents]
                object_ids.update(str(item.intent_id) for item in intents)
                object_ids.update(
                    str(item.reservation_id)
                    for item in session.scalars(
                        select(RiskReservation).where(RiskReservation.campaign_id.in_(campaign_ids))
                    ).all()
                )
                object_ids.update(
                    str(item.funding_payment_id)
                    for item in session.scalars(
                        select(FundingPayment).where(FundingPayment.campaign_id.in_(campaign_ids))
                    ).all()
                )
                object_ids.update(
                    str(item.reconciliation_id)
                    for item in session.scalars(
                        select(ReconciliationRun).where(
                            ReconciliationRun.campaign_id.in_(campaign_ids)
                        )
                    ).all()
                )
                if intent_ids:
                    object_ids.update(
                        str(item.venue_order_fact_id)
                        for item in session.scalars(
                            select(VenueOrder).where(VenueOrder.order_intent_id.in_(intent_ids))
                        ).all()
                    )
                    object_ids.update(
                        str(item.venue_fill_fact_id)
                        for item in session.scalars(
                            select(VenueFill).where(VenueFill.order_intent_id.in_(intent_ids))
                        ).all()
                    )
            transfer_proposals = [
                item
                for item in session.scalars(
                    select(TransferProposal).where(
                        TransferProposal.team_id == team_id,
                        TransferProposal.environment == environment,
                    )
                ).all()
                if self.service.can_user(user_id, "capital.view", item.account_id, item.venue)
            ]
            transfer_proposal_ids = [item.transfer_proposal_id for item in transfer_proposals]
            object_ids.update(str(item) for item in transfer_proposal_ids)
            if transfer_proposal_ids:
                transfer_authorizations = session.scalars(
                    select(TransferAuthorization).where(
                        TransferAuthorization.team_id == team_id,
                        TransferAuthorization.transfer_proposal_id.in_(transfer_proposal_ids)
                    )
                ).all()
                authorization_ids = [
                    item.transfer_authorization_id for item in transfer_authorizations
                ]
                object_ids.update(str(item) for item in authorization_ids)
                if authorization_ids:
                    object_ids.update(
                        str(item.capital_transfer_id)
                        for item in session.scalars(
                            select(CapitalTransfer).where(
                                CapitalTransfer.team_id == team_id,
                                CapitalTransfer.transfer_authorization_id.in_(authorization_ids)
                            )
                        ).all()
                    )
            policies = [
                item
                for item in session.scalars(
                    select(CapitalAutomationPolicy).where(
                        CapitalAutomationPolicy.team_id == team_id,
                        CapitalAutomationPolicy.environment == environment
                    )
                ).all()
                if self.service.can_user(user_id, "capital.view", item.account_id, item.venue)
            ]
            object_ids.update(str(item.policy_id) for item in policies)
            if not object_ids:
                return []
            events = session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.object_id.in_(object_ids),
                    AuditEvent.workspace_id == workspace_id,
                    AuditEvent.team_id == team_id,
                )
                .order_by(AuditEvent.created_at.desc(), AuditEvent.audit_event_id)
                .limit(limit)
            ).all()
            parsed_actor_ids = {
                item.actor_id: parsed
                for item in events
                if (parsed := _uuid_or_none(item.actor_id)) is not None
            }
            actors = {
                item.user_id: item.username
                for item in session.scalars(
                    select(User).where(User.user_id.in_(parsed_actor_ids.values()))
                ).all()
            }
            return [
                {
                    "audit_event_id": str(item.audit_event_id),
                    "workspace_id": str(workspace_id),
                    "team_id": str(team_id),
                    "account_id": item.account_id,
                    "actor_id": item.actor_id,
                    "actor": actors.get(parsed_actor_ids[item.actor_id], item.actor_id)
                    if item.actor_id in parsed_actor_ids
                    else item.actor_id,
                    "event_type": item.event_type,
                    "object_type": item.object_type,
                    "object_id": item.object_id,
                    "reason": item.reason,
                    "correlation_id": str(item.correlation_id),
                    "idempotency_key": item.idempotency_key,
                    "object_version": item.object_version,
                    "created_at": _iso(item.created_at),
                }
                for item in events
            ]

    def runtime_snapshot(self, user_id: UUID) -> dict[str, Any]:
        _workspace_id, team_id = self._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            gates = session.scalars(
                select(CapabilityGate).order_by(CapabilityGate.capability_key)
            ).all()
            revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            table_count = session.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name <> 'alembic_version'"
                )
            ).scalar_one()
            perptape_feed = session.get(PerptapeFeed, (team_id, "BREAKOUTS"))
            source_health = session.scalars(
                select(RuntimeSourceHealth)
                .where(RuntimeSourceHealth.team_id == team_id)
                .order_by(
                    RuntimeSourceHealth.source_name,
                    RuntimeSourceHealth.account_id,
                )
            ).all()
            runtime_binding_counts = {
                venue: int(count)
                for venue, count in session.execute(
                    select(ExchangeAccount.venue, func.count())
                    .where(
                        ExchangeAccount.team_id == team_id,
                        ExchangeAccount.runtime_sync_enabled.is_(True),
                    )
                    .group_by(ExchangeAccount.venue)
                ).all()
            }
            freqtrade_binding_counts = {
                venue: int(count)
                for venue, count in session.execute(
                    select(ExchangeAccount.venue, func.count())
                    .where(
                        ExchangeAccount.team_id == team_id,
                        ExchangeAccount.freqtrade_worker_mode != "UNCONFIGURED",
                    )
                    .group_by(ExchangeAccount.venue)
                ).all()
            }
            return {
                "database_ready": self.database.is_ready()[0],
                "schema_revision": revision,
                "business_table_count": int(table_count),
                "capability_gates": {
                    item.capability_key: {
                        "status": item.status,
                        "reason": item.reason,
                        "updated_at": _iso(item.updated_at),
                    }
                    for item in gates
                },
                "perptape_feed": (
                    {
                        "available": True,
                        "contract_version": perptape_feed.contract_version,
                        "candidate_count": len(perptape_feed.candidates),
                        "generated_at": _iso(perptape_feed.generated_at),
                        "fetched_at": _iso(perptape_feed.fetched_at),
                        "updated_at": _iso(perptape_feed.updated_at),
                    }
                    if perptape_feed is not None
                    else {
                        "available": False,
                        "contract_version": None,
                        "candidate_count": 0,
                        "generated_at": None,
                        "fetched_at": None,
                        "updated_at": None,
                    }
                ),
                "source_health": {
                    (
                        item.source_name
                        if item.account_id is None
                        else f"{item.source_name}:{item.account_id}"
                    ): {
                        "status": item.status,
                        "account_id": item.account_id,
                        "venue": item.venue,
                        "items_observed": item.items_observed,
                        "error_code": item.error_code,
                        "checked_at": _iso(item.checked_at),
                        "last_success_at": _iso(item.last_success_at),
                        "retry_at": _iso(item.retry_at),
                        "consecutive_failures": item.consecutive_failures,
                    }
                    for item in source_health
                },
                "runtime_binding_counts": runtime_binding_counts,
                "freqtrade_binding_counts": freqtrade_binding_counts,
            }

    def runtime_source_health(
        self,
        user_id: UUID,
        source_name: str,
        *,
        account_id: str | None = None,
        venue: str | None = None,
    ) -> dict[str, Any] | None:
        _workspace_id, team_id = self._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            item = session.scalar(
                select(RuntimeSourceHealth).where(
                    RuntimeSourceHealth.team_id == team_id,
                    RuntimeSourceHealth.source_name == source_name,
                    RuntimeSourceHealth.account_id == account_id,
                    RuntimeSourceHealth.venue == venue,
                )
            )
            if item is None:
                return None
            return {
                "status": item.status,
                "account_id": item.account_id,
                "venue": item.venue,
                "items_observed": item.items_observed,
                "error_code": item.error_code,
                "checked_at": _iso(item.checked_at),
                "last_success_at": _iso(item.last_success_at),
                "retry_at": _iso(item.retry_at),
                "consecutive_failures": item.consecutive_failures,
            }

    def list_campaigns(self, user_id: UUID) -> list[dict[str, Any]]:
        workspace_id, team_id = self._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            values = session.execute(
                select(Campaign, Instrument)
                .outerjoin(Instrument, Instrument.instrument_id == Campaign.instrument_id)
                .where(Campaign.team_id == team_id)
                .order_by(Campaign.updated_at.desc(), Campaign.campaign_id)
            ).all()
            result: list[dict[str, Any]] = []
            for campaign, instrument in values:
                if not self.service.can_user(user_id, "view", campaign.account_id, campaign.venue):
                    continue
                summary = self._campaign_summary(campaign, instrument)
                summary["workspace_id"] = str(workspace_id)
                result.append(summary)
            return result

    def shadow_workspace(self, user_id: UUID) -> dict[str, Any]:
        """Project team-scoped virtual capital without exposing live credentials."""

        workspace_id, team_id = self._active_scope_ids(user_id)
        activation = self.service.shadow_activation_status(user_id)
        with self.database.session_factory() as session:
            team = session.get(Team, team_id)
            if team is None or not team.active or team.workspace_id != workspace_id:
                raise DomainRejected("TEAM_SCOPE_DENIED", "active team is unavailable")
            assignments = session.scalars(
                select(RoleAssignment).where(
                    RoleAssignment.user_id == user_id,
                    RoleAssignment.team_id == team_id,
                )
            ).all()

            def granted(action: str, account_id: str, venue: str) -> bool:
                return any(
                    (item.account_scope is None or item.account_scope == account_id)
                    and (item.venue_scope is None or item.venue_scope == venue)
                    and (
                        action in ROLE_ACTIONS[Role(item.role)]
                        or "*" in ROLE_ACTIONS[Role(item.role)]
                    )
                    for item in assignments
                )

            accounts = [
                item
                for item in session.scalars(
                    select(ExchangeAccount)
                    .where(ExchangeAccount.team_id == team_id, ExchangeAccount.active)
                    .order_by(
                        ExchangeAccount.venue,
                        ExchangeAccount.label,
                        ExchangeAccount.account_id,
                    )
                ).all()
                if granted("venue.view", item.account_id, item.venue)
            ]
            equities = session.scalars(
                select(AccountEquity).where(
                    AccountEquity.team_id == team_id,
                    AccountEquity.environment == "SHADOW",
                )
            ).all()
            positions = session.execute(
                select(Position, Instrument)
                .join(Instrument, Instrument.instrument_id == Position.instrument_id)
                .where(
                    Position.team_id == team_id,
                    Position.environment == "SHADOW",
                )
                .order_by(Position.venue, Position.account_id, Instrument.symbol)
            ).all()
            campaigns = session.execute(
                select(Campaign, Instrument)
                .outerjoin(Instrument, Instrument.instrument_id == Campaign.instrument_id)
                .where(
                    Campaign.team_id == team_id,
                    Campaign.environment == "SHADOW",
                )
                .order_by(Campaign.updated_at.desc(), Campaign.campaign_id)
            ).all()
            occupied = {
                (account_id, venue): Decimal(str(amount))
                for account_id, venue, amount in session.execute(
                    select(
                        Campaign.account_id,
                        Campaign.venue,
                        func.coalesce(func.sum(RiskReservation.amount), 0),
                    )
                    .join(Campaign, Campaign.campaign_id == RiskReservation.campaign_id)
                    .where(
                        Campaign.team_id == team_id,
                        Campaign.environment == "SHADOW",
                        RiskReservation.status.in_(("RESERVED", "OPEN", "UNKNOWN")),
                    )
                    .group_by(Campaign.account_id, Campaign.venue)
                ).all()
            }
            instrument_rows = session.scalars(
                select(Instrument)
                .where(
                    Instrument.active,
                    Instrument.collateral_currency.in_(tuple(USD_STABLE_ASSETS)),
                    Instrument.quote_currency == Instrument.collateral_currency,
                )
                .order_by(Instrument.venue, Instrument.symbol)
            ).all()
            gates = {
                item.capability_key: item.status
                for item in session.scalars(
                    select(CapabilityGate).where(
                        CapabilityGate.capability_key.in_(
                            ("LIVE_ORDER_SEND", "CAPITAL_TRANSFER", "SIGNING", "BROADCAST")
                        )
                    )
                ).all()
            }

            equity_by_scope: dict[tuple[str, str], list[AccountEquity]] = {}
            for item in equities:
                equity_by_scope.setdefault((item.account_id, item.venue), []).append(item)
            position_by_scope: dict[tuple[str, str], list[tuple[Position, Instrument]]] = {}
            for position, instrument in positions:
                position_by_scope.setdefault((position.account_id, position.venue), []).append(
                    (position, instrument)
                )

            account_rows: list[dict[str, Any]] = []
            for account in accounts:
                scope = (account.account_id, account.venue)
                scope_equities = equity_by_scope.get(scope, [])
                scope_occupied = occupied.get(scope, Decimal(0))
                account_rows.append(
                    {
                        "exchange_account_id": str(account.exchange_account_id),
                        "account_id": account.account_id,
                        "venue": account.venue,
                        "label": account.label,
                        "connection_status": account.connection_status,
                        "trading_status": account.trading_status,
                        "credential_state": (
                            "UNCONFIGURED"
                            if account.credential_version == 0
                            else "CONFIGURED_REDACTED"
                        ),
                        "can_initialize": granted(
                            "account.manage", account.account_id, account.venue
                        ),
                        "occupied_risk": str(scope_occupied),
                        "virtual_capital": [
                            {
                                "account_equity_id": str(item.account_equity_id),
                                "currency": item.currency,
                                "equity": str(item.equity),
                                "available_balance": str(item.available_balance),
                                "risk_available": str(
                                    max(Decimal(0), item.available_balance - scope_occupied)
                                ),
                                "fact_status": item.fact_status,
                                "control_status": item.control_status,
                                "observed_at": _iso(item.observed_at),
                            }
                            for item in scope_equities
                        ],
                        "positions": [
                            {
                                "position_id": str(position.position_id),
                                "instrument_id": str(position.instrument_id),
                                "symbol": instrument.symbol,
                                "quantity": str(position.quantity),
                                "average_entry_price": str(position.average_entry_price),
                                "mark_price": str(position.mark_price),
                                "fact_status": position.fact_status,
                                "observed_at": _iso(position.observed_at),
                            }
                            for position, instrument in position_by_scope.get(scope, [])
                        ],
                        "instruments": [
                            {
                                "instrument_id": str(item.instrument_id),
                                "symbol": item.symbol,
                                "currency": item.collateral_currency,
                                "tick_size": str(item.tick_size),
                                "lot_size": str(item.lot_size),
                            }
                            for item in instrument_rows
                            if item.venue == account.venue
                        ],
                    }
                )

            campaign_rows = []
            for campaign, instrument in campaigns:
                if not granted("view", campaign.account_id, campaign.venue):
                    continue
                campaign_summary = self._campaign_summary(campaign, instrument)
                campaign_summary["workspace_id"] = str(workspace_id)
                campaign_rows.append(campaign_summary)

            return {
                "workspace_id": str(workspace_id),
                "team_id": str(team_id),
                "team_name": team.name,
                "execution_mode": team.execution_mode,
                "trading_enabled": team.trading_enabled,
                "version": team.version,
                "activation": activation,
                "safety_boundary": {
                    "environment": "SHADOW",
                    "capital": "VIRTUAL_ONLY",
                    "venue_connectors_used": False,
                    "live_order_send": False,
                    "funding": False,
                    "signing": False,
                    "broadcast": False,
                    "runtime_gate_status": gates,
                },
                "accounts": account_rows,
                "campaigns": campaign_rows,
            }

    def campaign_detail(self, user_id: UUID, campaign_id: UUID) -> dict[str, Any]:
        workspace_id, team_id = self._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise DomainRejected("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            if campaign.team_id != team_id:
                raise DomainRejected("TEAM_SCOPE_DENIED", "campaign is outside active team")
            if not self.service.can_user(user_id, "view", campaign.account_id, campaign.venue):
                raise DomainRejected("RBAC_DENIED", "campaign is outside the current scope")
            instrument = session.get(Instrument, campaign.instrument_id)
            proposal = session.get(Proposal, campaign.proposal_id)
            authorization = session.get(TradingAuthorization, campaign.authorization_id)
            auto_add_gate = session.get(CapabilityGate, "AUTO_ADD")
            reservations = session.scalars(
                select(RiskReservation)
                .where(RiskReservation.campaign_id == campaign_id)
                .order_by(RiskReservation.created_at)
            ).all()
            intents = session.scalars(
                select(OrderIntent)
                .where(OrderIntent.campaign_id == campaign_id)
                .order_by(OrderIntent.created_at, OrderIntent.intent_id)
            ).all()
            intent_ids = [item.intent_id for item in intents]
            orders = (
                session.scalars(
                    select(VenueOrder).where(VenueOrder.order_intent_id.in_(intent_ids))
                ).all()
                if intent_ids
                else []
            )
            fills = session.scalars(
                select(VenueFill)
                .where(VenueFill.campaign_id == campaign_id)
                .order_by(VenueFill.executed_at, VenueFill.venue_fill_fact_id)
            ).all()
            position = session.scalar(
                select(Position).where(
                    Position.team_id == campaign.team_id,
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.environment == campaign.environment,
                    Position.instrument_id == campaign.instrument_id,
                )
            )
            protection = (
                session.scalar(
                    select(ProtectionOrder).where(
                        ProtectionOrder.position_id == position.position_id
                    )
                )
                if position is not None
                else None
            )
            funding = session.scalars(
                select(FundingPayment)
                .where(FundingPayment.campaign_id == campaign_id)
                .order_by(FundingPayment.paid_at)
            ).all()
            scope = (
                f"{campaign.account_id}:{campaign.venue}"
                if campaign.environment == "SHADOW"
                else f"{campaign.environment}:{campaign.account_id}:{campaign.venue}"
            )
            reconciliation = session.scalar(
                select(ReconciliationRun)
                .where(
                    ReconciliationRun.team_id == campaign.team_id,
                    ReconciliationRun.execution_scope == scope,
                )
                .order_by(ReconciliationRun.completed_at.desc())
                .limit(1)
            )
            lease = session.get(SenderLease, (campaign.team_id, scope))
            orders_by_intent = {item.order_intent_id: item for item in orders}
            result = self._campaign_summary(campaign, instrument)
            result["workspace_id"] = str(workspace_id)
            result.update(
                {
                    "instrument": None
                    if instrument is None
                    else {
                        "symbol": instrument.symbol,
                        "collateral_currency": instrument.collateral_currency,
                    },
                    "authorization": None
                    if authorization is None
                    else {
                        "authorization_id": str(authorization.authorization_id),
                        "environment": authorization.environment,
                        "active": authorization.active,
                        "quantity_limit": str(authorization.quantity_limit),
                        "used_quantity": str(authorization.used_quantity),
                        "allowed_adds": authorization.allowed_adds,
                        "used_adds": authorization.used_adds,
                        "add_revoked_at": _iso(authorization.add_revoked_at),
                        "expires_at": _iso(authorization.expires_at),
                    },
                    "reservations": [
                        {
                            "reservation_id": str(item.reservation_id),
                            "workspace_id": str(workspace_id),
                            "team_id": str(campaign.team_id),
                            "account_id": campaign.account_id,
                            "status": item.status,
                            "amount": str(item.amount),
                            "version": item.version,
                            "created_at": _iso(item.created_at),
                            "updated_at": _iso(item.updated_at),
                        }
                        for item in reservations
                    ],
                    "intents": [
                        {
                            "intent_id": str(item.intent_id),
                            "workspace_id": str(workspace_id),
                            "team_id": str(campaign.team_id),
                            "account_id": campaign.account_id,
                            "kind": item.kind,
                            "side": item.side,
                            "quantity": str(item.quantity),
                            "limit_price": (
                                None if item.limit_price is None else str(item.limit_price)
                            ),
                            "reduce_only": item.reduce_only,
                            "trigger_source": item.trigger_source,
                            "trigger_observed_at": _iso(item.trigger_observed_at),
                            "add_unit_consumed": item.add_unit_consumed,
                            "status": item.status,
                            "dispatch": (
                                None
                                if item.dispatch_backend is None
                                else {
                                    "backend": item.dispatch_backend,
                                    "account_version": item.dispatch_account_version,
                                    "auth_version": item.dispatch_auth_version,
                                    "started_at": _iso(item.dispatch_started_at),
                                }
                            ),
                            "version": item.version,
                            "created_at": _iso(item.created_at),
                            "updated_at": _iso(item.updated_at),
                            "order": self._order_summary(
                                orders_by_intent.get(item.intent_id),
                                workspace_id=workspace_id,
                                team_id=campaign.team_id,
                                account_id=campaign.account_id,
                            ),
                        }
                        for item in intents
                    ],
                    "fills": [
                        {
                            "fill_id": str(item.venue_fill_fact_id),
                            "workspace_id": str(workspace_id),
                            "team_id": str(campaign.team_id),
                            "account_id": campaign.account_id,
                            "venue_fill_id": item.venue_fill_id,
                            "intent_id": str(item.order_intent_id),
                            "side": item.side,
                            "quantity": str(item.quantity),
                            "price": str(item.price),
                            "fee": str(item.fee),
                            "fee_currency": item.fee_currency,
                            "slippage_cost": str(item.slippage_cost),
                            "executed_at": _iso(item.executed_at),
                        }
                        for item in fills
                    ],
                    "position": None
                    if position is None
                    else {
                        "position_id": str(position.position_id),
                        "workspace_id": str(workspace_id),
                        "team_id": str(campaign.team_id),
                        "account_id": campaign.account_id,
                        "quantity": str(position.quantity),
                        "average_entry_price": str(position.average_entry_price),
                        "mark_price": str(position.mark_price),
                        "fact_status": position.fact_status,
                        "observed_at": _iso(position.observed_at),
                    },
                    "protection": None
                    if protection is None
                    else {
                        "protection_id": str(protection.protection_id),
                        "venue_order_id": protection.venue_order_id,
                        "quantity": str(protection.quantity),
                        "trigger_price": str(protection.trigger_price),
                        "status": protection.status,
                        "fully_covered": protection.fully_covered,
                        "observed_at": _iso(protection.observed_at),
                    },
                    "funding": [
                        {
                            "venue_payment_id": item.venue_payment_id,
                            "workspace_id": str(workspace_id),
                            "team_id": str(campaign.team_id),
                            "account_id": campaign.account_id,
                            "amount": str(item.amount),
                            "currency": item.currency,
                            "paid_at": _iso(item.paid_at),
                        }
                        for item in funding
                    ],
                    "reconciliation": None
                    if reconciliation is None
                    else {
                        "reconciliation_id": str(reconciliation.reconciliation_id),
                        "workspace_id": str(workspace_id),
                        "team_id": str(campaign.team_id),
                        "account_id": campaign.account_id,
                        "status": reconciliation.status,
                        "is_computed": reconciliation.is_computed,
                        "differences": reconciliation.differences,
                        "resolution_reason": reconciliation.resolution_reason,
                        "completed_at": _iso(reconciliation.completed_at),
                    },
                    "sender_lease": None
                    if lease is None
                    else {
                        "execution_scope": lease.execution_scope,
                        "owner_id": lease.owner_id,
                        "fencing_token": lease.fencing_token,
                        "expires_at": _iso(lease.expires_at),
                    },
                    "management": {
                        "auto_add_gate": (
                            "UNKNOWN" if auto_add_gate is None else auto_add_gate.status
                        ),
                        "allow_auto_add": bool(
                            proposal is not None
                            and isinstance(proposal.frozen_payload.get("details"), dict)
                            and proposal.frozen_payload["details"].get("allow_auto_add") is True
                        ),
                        "initial_quantity": (
                            None
                            if proposal is None
                            or not isinstance(proposal.frozen_payload.get("details"), dict)
                            else proposal.frozen_payload["details"].get("initial_quantity")
                        ),
                        "add_trigger_price": (
                            None
                            if proposal is None
                            or not isinstance(proposal.frozen_payload.get("details"), dict)
                            else proposal.frozen_payload["details"].get("add_trigger_price")
                        ),
                        "requested_adds": (
                            0
                            if proposal is None
                            or not isinstance(proposal.frozen_payload.get("details"), dict)
                            else proposal.frozen_payload["details"].get("requested_adds", 0)
                        ),
                        "remaining_quantity": (
                            "0"
                            if authorization is None or not authorization.active
                            else str(authorization.quantity_limit - authorization.used_quantity)
                        ),
                        "remaining_adds": (
                            0
                            if authorization is None
                            or not authorization.active
                            or authorization.add_revoked_at is not None
                            else authorization.allowed_adds - authorization.used_adds
                        ),
                    },
                }
            )
            return result

    def campaign_id_for_intent(self, user_id: UUID, intent_id: UUID) -> UUID:
        _workspace_id, team_id = self._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            intent = session.get(OrderIntent, intent_id)
            if intent is None:
                raise DomainRejected("ORDER_INTENT_NOT_FOUND", "intent does not exist")
            campaign = session.get(Campaign, intent.campaign_id)
            if campaign is None:
                raise DomainRejected("CAMPAIGN_NOT_FOUND", "intent campaign is missing")
            if campaign.team_id != team_id:
                raise DomainRejected("TEAM_SCOPE_DENIED", "intent is outside active team")
            if not self.service.can_user(user_id, "view", campaign.account_id, campaign.venue):
                raise DomainRejected("RBAC_DENIED", "intent is outside the current scope")
            return campaign.campaign_id

    def list_exceptions(self, user_id: UUID, *, now: datetime) -> list[dict[str, Any]]:
        exceptions: list[dict[str, Any]] = []
        with self.database.session_factory() as session:
            risk_policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
            max_fact_age = timedelta(
                seconds=(risk_policy.max_fact_age_seconds if risk_policy is not None else 300)
            )
        for campaign in self.list_campaigns(user_id):
            if campaign["status"] == "CLOSED":
                continue
            detail = self.campaign_detail(user_id, UUID(str(campaign["campaign_id"])))
            campaign_id = str(campaign["campaign_id"])
            campaign_occurred_at = str(campaign["updated_at"] or campaign["created_at"])
            if campaign["status"] == "UNKNOWN":
                exceptions.append(
                    self._exception(
                        campaign_id,
                        "CAMPAIGN_UNKNOWN",
                        "BLOCKING",
                        occurred_at=campaign_occurred_at,
                    )
                )
            for reservation in detail["reservations"]:
                if reservation["status"] == "UNKNOWN":
                    exceptions.append(
                        self._exception(
                            campaign_id,
                            "RISK_RESERVATION_UNKNOWN",
                            "BLOCKING",
                            object_id=str(reservation["reservation_id"]),
                            occurred_at=str(reservation["updated_at"]),
                        )
                    )
            for intent in detail["intents"]:
                if intent["status"] == "DISPATCHING":
                    exceptions.append(
                        self._exception(
                            campaign_id,
                            "ORDER_DISPATCH_UNRESOLVED",
                            "BLOCKING",
                            object_id=str(intent["intent_id"]),
                            occurred_at=str(
                                intent["dispatch"]["started_at"] or intent["updated_at"]
                            ),
                        )
                    )
                if intent["status"] == "UNKNOWN":
                    exceptions.append(
                        self._exception(
                            campaign_id,
                            "ORDER_INTENT_UNKNOWN",
                            "BLOCKING",
                            object_id=str(intent["intent_id"]),
                            occurred_at=str(intent["updated_at"]),
                        )
                    )
            position = detail["position"]
            if position is None or position["fact_status"] == "UNKNOWN":
                exceptions.append(
                    self._exception(
                        campaign_id,
                        "POSITION_UNKNOWN",
                        "BLOCKING",
                        occurred_at=campaign_occurred_at,
                    )
                )
            else:
                position_observed_at = datetime.fromisoformat(str(position["observed_at"]))
                if self.service._fact_is_stale(position_observed_at, now, max_fact_age):
                    exceptions.append(
                        self._exception(
                            campaign_id,
                            "POSITION_STALE",
                            "BLOCKING",
                            object_id=str(position["position_id"]),
                            occurred_at=(position_observed_at + max_fact_age).isoformat(),
                            details=[
                                f"observed_at={position['observed_at']}",
                                f"max_age_seconds={int(max_fact_age.total_seconds())}",
                            ],
                        )
                    )
            if (
                position is not None
                and position["fact_status"] != "UNKNOWN"
                and Decimal(str(position["quantity"])) != 0
            ):
                protection = detail["protection"]
                if protection is None or protection["status"] == "UNKNOWN":
                    exceptions.append(
                        self._exception(
                            campaign_id,
                            "PROTECTION_UNKNOWN",
                            "BLOCKING",
                            occurred_at=str(position["observed_at"]),
                        )
                    )
                else:
                    protection_observed_at = datetime.fromisoformat(str(protection["observed_at"]))
                    if self.service._fact_is_stale(protection_observed_at, now, max_fact_age):
                        exceptions.append(
                            self._exception(
                                campaign_id,
                                "PROTECTION_STALE",
                                "BLOCKING",
                                object_id=str(protection["protection_id"]),
                                occurred_at=(protection_observed_at + max_fact_age).isoformat(),
                                details=[
                                    f"observed_at={protection['observed_at']}",
                                    f"max_age_seconds={int(max_fact_age.total_seconds())}",
                                ],
                            )
                        )
                    if not protection["fully_covered"]:
                        exceptions.append(
                            self._exception(
                                campaign_id,
                                "PROTECTION_INSUFFICIENT",
                                "BLOCKING",
                                object_id=str(protection["protection_id"]),
                                occurred_at=str(protection["observed_at"]),
                            )
                        )
            reconciliation = detail["reconciliation"]
            if reconciliation is not None and reconciliation["status"] != "MATCH":
                exceptions.append(
                    self._exception(
                        campaign_id,
                        f"RECONCILIATION_{reconciliation['status']}",
                        "BLOCKING",
                        occurred_at=str(reconciliation["completed_at"]),
                        details=list(reconciliation["differences"]),
                    )
                )
            elif reconciliation is not None:
                completed_at = datetime.fromisoformat(str(reconciliation["completed_at"]))
                newer_facts: list[tuple[str, datetime]] = []
                if (
                    position is not None
                    and datetime.fromisoformat(str(position["observed_at"])) > completed_at
                ):
                    newer_facts.append(
                        (
                            "POSITION_FACT_NEWER",
                            datetime.fromisoformat(str(position["observed_at"])),
                        )
                    )
                if (
                    detail["intents"]
                    and datetime.fromisoformat(str(detail["intents"][-1]["updated_at"]))
                    > completed_at
                ):
                    newer_facts.append(
                        (
                            "ORDER_INTENT_NEWER",
                            datetime.fromisoformat(str(detail["intents"][-1]["updated_at"])),
                        )
                    )
                if newer_facts:
                    exceptions.append(
                        self._exception(
                            campaign_id,
                            "RECONCILIATION_STALE",
                            "BLOCKING",
                            object_id=str(reconciliation["reconciliation_id"]),
                            occurred_at=min(value for _name, value in newer_facts).isoformat(),
                            details=[name for name, _value in newer_facts],
                        )
                    )
        guidance = {
            "CAMPAIGN_UNKNOWN": (
                "CRITICAL",
                "交易运维",
                "任务结果未知会阻断新增风险",
                "核对交易所事实并完成计算型对账",
            ),
            "RISK_RESERVATION_UNKNOWN": (
                "CRITICAL",
                "交易运维",
                "风险占用无法安全释放",
                "核对订单结果并重新计算风险预留",
            ),
            "ORDER_INTENT_UNKNOWN": (
                "CRITICAL",
                "交易运维",
                "可能存在未确认订单结果",
                "先同步订单与成交，再处理未知意图",  # noqa: RUF001
            ),
            "POSITION_UNKNOWN": (
                "CRITICAL",
                "交易运维",
                "无法确认真实仓位",
                "同步该账户与标的的仓位事实",
            ),
            "POSITION_STALE": (
                "HIGH",
                "交易运维",
                "仓位事实可能不再代表当前状态",
                "刷新仓位后重新对账",
            ),
            "PROTECTION_UNKNOWN": (
                "CRITICAL",
                "交易运维",
                "无法确认持仓保护是否有效",
                "同步或补齐保护单事实",
            ),
            "PROTECTION_STALE": (
                "HIGH",
                "交易运维",
                "保护事实可能已经变化",
                "刷新保护单并确认足额覆盖",
            ),
            "PROTECTION_INSUFFICIENT": (
                "CRITICAL",
                "交易运维",
                "现有持仓未被足额保护",
                "按交易任务允许的降险路径补齐保护或退出",
            ),
            "RECONCILIATION_STALE": (
                "HIGH",
                "交易运维",
                "旧对账早于最新仓位或订单事实",
                "用最新事实重新运行计算型对账",
            ),
        }
        for item in exceptions:
            code = str(item["code"])
            default = ("HIGH", "交易运维", "运行事实存在不一致", "检查差异并重新运行计算型对账")
            severity, owner_role, impact, next_action = guidance.get(code, default)
            item.update(
                {
                    "severity": severity,
                    "last_checked_at": now.isoformat(),
                    "impact": impact,
                    "owner_role": owner_role,
                    "next_action": next_action,
                    "action_available": False,
                    "action_unavailable_reason": (
                        "告警详情只提供事实与路径；必须回到受影响交易任务按后端安全条件处理"  # noqa: RUF001
                    ),
                }
            )
        return exceptions

    def venue_facts(
        self,
        user_id: UUID,
        account_id: str,
        venue: str,
        environment: str,
    ) -> dict[str, Any]:
        if not self.service.can_user(user_id, "view", account_id, venue):
            raise DomainRejected("RBAC_DENIED", "venue facts are outside the current scope")
        workspace_id, team_id = self._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            account = session.scalar(
                select(ExchangeAccount.exchange_account_id).where(
                    ExchangeAccount.team_id == team_id,
                    ExchangeAccount.account_id == account_id,
                    ExchangeAccount.venue == venue,
                )
            )
            if account is None:
                raise DomainRejected(
                    "EXCHANGE_ACCOUNT_NOT_FOUND",
                    "venue facts require a registered account in the active team",
                )
            instruments = session.scalars(
                select(Instrument).where(Instrument.venue == venue).order_by(Instrument.symbol)
            ).all()
            instrument_by_id = {item.instrument_id: item for item in instruments}
            positions = session.scalars(
                select(Position).where(
                    Position.team_id == team_id,
                    Position.account_id == account_id,
                    Position.venue == venue,
                    Position.environment == environment,
                )
            ).all()
            protections = (
                session.scalars(
                    select(ProtectionOrder).where(
                        ProtectionOrder.position_id.in_([item.position_id for item in positions])
                    )
                ).all()
                if positions
                else []
            )
            protection_by_position = {item.position_id: item for item in protections}
            orders = session.scalars(
                select(VenueOrder)
                .where(
                    VenueOrder.team_id == team_id,
                    VenueOrder.account_id == account_id,
                    VenueOrder.venue == venue,
                    VenueOrder.environment == environment,
                )
                .order_by(VenueOrder.observed_at.desc())
            ).all()
            fills = session.scalars(
                select(VenueFill)
                .where(
                    VenueFill.team_id == team_id,
                    VenueFill.account_id == account_id,
                    VenueFill.venue == venue,
                    VenueFill.environment == environment,
                )
                .order_by(VenueFill.executed_at.desc())
            ).all()
            funding = session.scalars(
                select(FundingPayment)
                .where(
                    FundingPayment.team_id == team_id,
                    FundingPayment.account_id == account_id,
                    FundingPayment.venue == venue,
                    FundingPayment.environment == environment,
                )
                .order_by(FundingPayment.paid_at.desc())
            ).all()
            equity = session.scalar(
                select(AccountEquity).where(
                    AccountEquity.team_id == team_id,
                    AccountEquity.account_id == account_id,
                    AccountEquity.venue == venue,
                    AccountEquity.environment == environment,
                )
            )
            execution_scope = f"{environment}:{account_id}:{venue}"
            reconciliation = session.scalar(
                select(ReconciliationRun)
                .where(
                    ReconciliationRun.team_id == team_id,
                    ReconciliationRun.execution_scope == execution_scope,
                )
                .order_by(ReconciliationRun.completed_at.desc())
            )
            return {
                "workspace_id": str(workspace_id),
                "team_id": str(team_id),
                "account_id": account_id,
                "venue": venue,
                "environment": environment,
                "instruments": [
                    {
                        "instrument_id": str(item.instrument_id),
                        "symbol": item.symbol,
                        "tick_size": str(item.tick_size),
                        "lot_size": str(item.lot_size),
                        "minimum_notional": str(item.minimum_notional),
                        "active": item.active,
                        "updated_at": _iso(item.updated_at),
                    }
                    for item in instruments
                ],
                "positions": [
                    {
                        "position_id": str(item.position_id),
                        "instrument_id": str(item.instrument_id),
                        "symbol": instrument_by_id[item.instrument_id].symbol,
                        "quantity": str(item.quantity),
                        "average_entry_price": str(item.average_entry_price),
                        "mark_price": str(item.mark_price),
                        "fact_status": item.fact_status,
                        "observed_at": _iso(item.observed_at),
                        "protection": (
                            None
                            if item.position_id not in protection_by_position
                            else {
                                "venue_order_id": protection_by_position[
                                    item.position_id
                                ].venue_order_id,
                                "quantity": str(protection_by_position[item.position_id].quantity),
                                "trigger_price": str(
                                    protection_by_position[item.position_id].trigger_price
                                ),
                                "status": protection_by_position[item.position_id].status,
                                "fully_covered": protection_by_position[
                                    item.position_id
                                ].fully_covered,
                                "observed_at": _iso(
                                    protection_by_position[item.position_id].observed_at
                                ),
                            }
                        ),
                    }
                    for item in positions
                ],
                "orders": [
                    {
                        "venue_order_id": item.venue_order_id,
                        "client_order_id": item.client_order_id,
                        "instrument_id": str(item.instrument_id),
                        "symbol": instrument_by_id[item.instrument_id].symbol,
                        "intent_id": (
                            None if item.order_intent_id is None else str(item.order_intent_id)
                        ),
                        "status": item.status,
                        "side": item.side,
                        "order_type": item.order_type,
                        "reduce_only": item.reduce_only,
                        "ordered_quantity": str(item.ordered_quantity),
                        "filled_quantity": str(item.filled_quantity),
                        "observed_at": _iso(item.observed_at),
                    }
                    for item in orders
                ],
                "fills": [
                    {
                        "venue_fill_id": item.venue_fill_id,
                        "instrument_id": str(item.instrument_id),
                        "symbol": instrument_by_id[item.instrument_id].symbol,
                        "intent_id": (
                            None if item.order_intent_id is None else str(item.order_intent_id)
                        ),
                        "side": item.side,
                        "quantity": str(item.quantity),
                        "price": str(item.price),
                        "fee": str(item.fee),
                        "fee_currency": item.fee_currency,
                        "executed_at": _iso(item.executed_at),
                    }
                    for item in fills
                ],
                "funding": [
                    {
                        "venue_payment_id": item.venue_payment_id,
                        "instrument_id": str(item.instrument_id),
                        "symbol": instrument_by_id[item.instrument_id].symbol,
                        "amount": str(item.amount),
                        "currency": item.currency,
                        "paid_at": _iso(item.paid_at),
                    }
                    for item in funding
                ],
                "equity": None
                if equity is None
                else {
                    "equity": str(equity.equity),
                    "available_balance": str(equity.available_balance),
                    "currency": equity.currency,
                    "fact_status": equity.fact_status,
                    "observed_at": _iso(equity.observed_at),
                },
                "reconciliation": None
                if reconciliation is None
                else {
                    "reconciliation_id": str(reconciliation.reconciliation_id),
                    "status": reconciliation.status,
                    "is_computed": reconciliation.is_computed,
                    "differences": reconciliation.differences,
                    "completed_at": _iso(reconciliation.completed_at),
                },
            }

    @staticmethod
    def _exception(
        campaign_id: str,
        code: str,
        severity: str,
        *,
        object_id: str | None = None,
        details: list[str] | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        return {
            "campaign_id": campaign_id,
            "object_id": object_id or campaign_id,
            "code": code,
            "severity": severity,
            "details": details or [],
            "occurred_at": occurred_at,
        }

    @staticmethod
    def _order_summary(
        order: VenueOrder | None,
        *,
        workspace_id: UUID | None = None,
        team_id: UUID | None = None,
        account_id: str | None = None,
    ) -> dict[str, Any] | None:
        if order is None:
            return None
        return {
            "venue_order_fact_id": str(order.venue_order_fact_id),
            "workspace_id": None if workspace_id is None else str(workspace_id),
            "team_id": None if team_id is None else str(team_id),
            "account_id": account_id or order.account_id,
            "venue_order_id": order.venue_order_id,
            "client_order_id": order.client_order_id,
            "status": order.status,
            "side": order.side,
            "order_type": order.order_type,
            "reduce_only": order.reduce_only,
            "ordered_quantity": str(order.ordered_quantity),
            "filled_quantity": str(order.filled_quantity),
            "observed_at": _iso(order.observed_at),
        }

    @staticmethod
    def _campaign_summary(
        campaign: Campaign, instrument: Instrument | None = None
    ) -> dict[str, Any]:
        return {
            "campaign_id": str(campaign.campaign_id),
            "team_id": str(campaign.team_id),
            "proposal_id": str(campaign.proposal_id),
            "authorization_id": str(campaign.authorization_id),
            "account_id": campaign.account_id,
            "venue": campaign.venue,
            "environment": campaign.environment,
            "instrument_id": str(campaign.instrument_id),
            "symbol": None if instrument is None else instrument.symbol,
            "collateral_currency": (None if instrument is None else instrument.collateral_currency),
            "direction": campaign.direction,
            "status": campaign.status,
            "current_target_quantity": str(campaign.current_target_quantity),
            "target_version": campaign.target_version,
            "target_reason": campaign.target_reason,
            "target_urgency": campaign.target_urgency,
            "target_calculated_at": _iso(campaign.target_calculated_at),
            "realized_pnl": str(campaign.realized_pnl),
            "unrealized_pnl": str(campaign.unrealized_pnl),
            "final_pnl": str(campaign.final_pnl),
            "created_at": _iso(campaign.created_at),
            "updated_at": _iso(campaign.updated_at),
        }

    @staticmethod
    def _proposal_summary(
        proposal: Proposal, instrument: Instrument | None = None
    ) -> dict[str, Any]:
        details = proposal.frozen_payload.get("details", {})
        candidate = details.get("candidate", {}) if isinstance(details, dict) else {}
        reference_price = (
            details.get("trigger_price")
            or candidate.get("reference_price")
            or candidate.get("threshold_price")
            if isinstance(details, dict) and isinstance(candidate, dict)
            else None
        )
        try:
            estimated_notional = (
                None
                if reference_price is None
                else (
                    proposal.quantity
                    * Decimal(str(reference_price))
                    * (Decimal(1) if instrument is None else instrument.contract_multiplier)
                ).quantize(Decimal("0.000000000000000001"))
            )
        except (ArithmeticError, TypeError, ValueError):
            estimated_notional = None
        return {
            "proposal_id": str(proposal.proposal_id),
            "team_id": str(proposal.team_id),
            "source": proposal.source,
            "environment": proposal.environment,
            "proposer_id": str(proposal.proposer_id),
            "strategy_id": proposal.strategy_id,
            "strategy_version": proposal.strategy_version,
            "source_candidate_id": proposal.source_candidate_id,
            "source_link": proposal.source_link,
            "source_observed_at": _iso(proposal.source_observed_at),
            "source_readiness": proposal.source_readiness,
            "signal_event_id": (
                None if proposal.signal_event_id is None else str(proposal.signal_event_id)
            ),
            "status": proposal.status,
            "version": proposal.version,
            "risk_tier": proposal.risk_tier,
            "account_id": proposal.account_id,
            "venue": proposal.venue,
            "instrument_id": str(proposal.instrument_id),
            "symbol": None if instrument is None else instrument.symbol,
            "quote_currency": None if instrument is None else instrument.quote_currency,
            "collateral_currency": (None if instrument is None else instrument.collateral_currency),
            "direction": proposal.direction,
            "quantity": str(proposal.quantity),
            "estimated_notional": (None if estimated_notional is None else str(estimated_notional)),
            "max_risk": str(proposal.max_risk),
            "expires_at": _iso(proposal.expires_at),
            "created_at": _iso(proposal.created_at),
            "updated_at": _iso(proposal.updated_at),
        }
