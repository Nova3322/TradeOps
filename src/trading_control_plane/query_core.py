from __future__ import annotations

# Domain query modules import their explicit dependencies from this shared surface.
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
from trading_control_plane.service_core import fact_is_stale


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


class QueryMixinBase:
    """Typing surface for projections composed by the public query facade."""

    database: Database
    service: TradingService

    def _proposal_summary(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)


__all__ = [
    "NOTIFICATION_TEMPLATES",
    "ROLE_ACTIONS",
    "ROUTABLE_NOTIFICATION_EVENT_TYPES",
    "USD_STABLE_ASSETS",
    "UTC",
    "UUID",
    "AccountEquity",
    "AccountEquityObservation",
    "Any",
    "Approval",
    "AuditEvent",
    "Campaign",
    "CapabilityGate",
    "CapitalAutomationPolicy",
    "CapitalTransfer",
    "Decimal",
    "DirectCapitalOperation",
    "DomainRejected",
    "ExchangeAccount",
    "FundingPayment",
    "Instrument",
    "NotificationDelivery",
    "NotificationRoute",
    "OrderIntent",
    "PerptapeCandidate",
    "PerptapeFeed",
    "PerptapeFeedSnapshot",
    "Position",
    "PrincipalType",
    "Proposal",
    "ProtectionOrder",
    "QueryMixinBase",
    "ReconciliationRun",
    "RiskDecision",
    "RiskPolicy",
    "RiskReservation",
    "Role",
    "RoleAssignment",
    "RuntimeSourceHealth",
    "SenderLease",
    "ServicePrincipalKind",
    "Team",
    "TeamMembership",
    "TradingAuthorization",
    "TransferAuthorization",
    "TransferProposal",
    "User",
    "VenueFill",
    "VenueOrder",
    "Workspace",
    "WorkspaceMembership",
    "_effective_proposal_status",
    "_iso",
    "_performance_metrics",
    "_proposal_execution_status",
    "_report_attribution",
    "_uuid_or_none",
    "and_",
    "datetime",
    "fact_is_stale",
    "false",
    "func",
    "or_",
    "select",
    "text",
    "timedelta",
    "tuple_",
]
