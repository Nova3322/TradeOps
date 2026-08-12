from __future__ import annotations

# This module is the explicit dependency surface shared by the domain components.
# Imports are re-exported intentionally so each component can declare only the names it consumes.
import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, NoReturn
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from trading_control_plane.agent import (
    AGENT_TOKEN_MARKER,
    issue_agent_token,
    issue_api_client_token,
    parse_agent_token,
    parse_api_client_token,
    validate_agent_roles,
)
from trading_control_plane.binance import BinanceInstrument, BinanceReadOnlySnapshot
from trading_control_plane.binance_execution import (
    BinanceTestnetOrder,
    BinanceTestnetOrderCommand,
    BinanceTestnetProtectionCommand,
    ProtectionCancelCommand,
)
from trading_control_plane.capital import (
    CapitalTransferCommand,
    CapitalTransferSubmission,
    DirectCapitalPlan,
    evaluate_capital_automation,
)
from trading_control_plane.credentials import (
    SUPPORTED_EXCHANGE_VENUES,
)
from trading_control_plane.domain import (
    AddCandidateFacts,
    ApiClientState,
    CampaignStatus,
    CapabilityStatus,
    CapitalDirection,
    CapitalTransferStatus,
    DirectCapitalPath,
    Direction,
    DomainRejected,
    EconomicFill,
    ExecutionEnvironment,
    FactStatus,
    IdempotencyConflict,
    IntentCreation,
    IntentKind,
    OrderIntentStatus,
    PnlBreakdown,
    PrincipalType,
    ProposalSource,
    ProposalStatus,
    ProtectionStatus,
    ReconciliationStatus,
    ReservationStatus,
    ReviewDecision,
    RiskEvaluationInput,
    RiskPolicyChangeStatus,
    RiskPolicyInput,
    RiskResult,
    RiskTier,
    Role,
    ServicePrincipalKind,
    SignalEventStatus,
    SignalSourceMode,
    SystemRiskState,
    TargetCandidate,
    TargetDecision,
    TargetUrgency,
    TeamExecutionMode,
    VenueOrderStatus,
    WorkspaceRole,
    compute_pnl,
    evaluate_risk,
    select_target_position,
)
from trading_control_plane.exchange_connection import ConnectionProbeResult
from trading_control_plane.freqtrade import (
    FreqtradeEntryCommand,
    FreqtradeExitCommand,
    FreqtradeTrade,
    freqtrade_pair,
    parse_hip3_dexes,
    validate_worker_url,
)
from trading_control_plane.hyperliquid import HyperliquidInstrument, HyperliquidReadOnlySnapshot
from trading_control_plane.hyperliquid_execution import (
    HyperliquidTestnetOrder,
    HyperliquidTestnetOrderCommand,
    HyperliquidTestnetProtectionCommand,
)
from trading_control_plane.metrics import (
    FENCING_REJECTIONS,
    INTENT_TRANSITIONS,
    PROTECTION_ISSUES,
    RECONCILIATION_RESULTS,
    RISK_RESULTS,
)
from trading_control_plane.models import (
    AccountEquity,
    AccountEquityObservation,
    AnalyticsEquitySnapshot,
    AnalyticsReport,
    ApiClient,
    Approval,
    AuditEvent,
    Campaign,
    CapabilityGate,
    CapitalAutomationPolicy,
    CapitalTransfer,
    CommandReceipt,
    DirectCapitalConfiguration,
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
    ProposalDefaultConfig,
    ProtectionOrder,
    ReconciliationRun,
    RiskControlChangeRequest,
    RiskDecision,
    RiskPolicy,
    RiskReservation,
    RoleAssignment,
    RuntimeSourceHealth,
    SenderLease,
    ShadowFill,
    ShadowInstrument,
    ShadowOrder,
    ShadowPosition,
    SignalEvent,
    Team,
    TeamMembership,
    TeamShadowAccount,
    TeamSignalSource,
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
    normalize_notification_event_types,
    notification_template,
    validate_notification_configuration,
    validate_notification_payload,
)
from trading_control_plane.notilt import (
    USD_STABLE_ASSETS,
    NoTiltReceipt,
    NoTiltUnsignedTransaction,
    NoTiltVaultSnapshot,
    UsdValuation,
)
from trading_control_plane.passwords import PasswordHasher
from trading_control_plane.perptape import (
    PerptapeCandidate,
    PerptapeFeedSnapshot,
    apply_perptape_feed_delta,
    bound_perptape_feed_snapshot,
    normalize_perptape_datetime,
    perptape_snapshot_identity,
    validate_perptape_feed_payload,
)
from trading_control_plane.request_context import current_api_client_context
from trading_control_plane.shadow import (
    apply_shadow_fill,
    apply_shadow_ledger_fill,
    quantize_shadow_step,
    quote_shadow_execution,
    shadow_limit_crossed,
    shadow_protection_triggered,
)
from trading_control_plane.venue_read_only import VenueInstrument, VenueReadOnlySnapshot

CAPITAL_HISTORY_MIN_INTERVAL = timedelta(minutes=1)
DEFAULT_SENDER_LEASE_DURATION = timedelta(minutes=1)
DEFAULT_FREQTRADE_LEVERAGE = Decimal(1)
PASSWORD_HASHER = PasswordHasher()
SCOPE_SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")
SIGNAL_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{16,160}$")
SIGNAL_CLOCK_SKEW = timedelta(seconds=30)
CONNECTION_ERROR_CODE_PATTERN = re.compile(r"^[A-Z0-9_]{1,120}$")
TEAM_SETUP_ACTIONS = frozenset(
    {
        "team.view",
        "team.manage",
        "user.manage",
        "role.manage",
        "venue.view",
        "account.manage",
        "account.credentials.manage",
        "system.view",
        "risk_policy.manage",
        "signal.view",
        "signal.manage",
        "opportunity.view",
        "notification.view",
        "notification.manage",
    }
)

API_CLIENT_HUMAN_ONLY_ACTIONS = frozenset(
    {
        "team.manage",
        "user.manage",
        "role.manage",
        "account.manage",
        "account.credentials.manage",
        "signal.manage",
        "notification.manage",
        "risk_policy.manage",
        "risk.restore.request",
        "risk.restore.review",
        "risk.restore.execute",
        "capital.fact.record",
        "capital.propose",
        "capital.submit",
        "capital.review",
        "capital.authorize",
        "capital.execute",
        "capital.reconcile",
        "capital.policy.manage",
        "capital.automation.evaluate",
        "authorization.issue",
        "sender.manage",
    }
)

API_CLIENT_ALLOWED_BUSINESS_ACTIONS = frozenset(
    {
        "view",
        "team.view",
        "opportunity.view",
        "proposal.view",
        "proposal.create",
        "proposal.submit",
        "proposal.review",
        "operations.view",
        "system.view",
        "venue.view",
        "results.view",
        "signal.view",
        "notification.view",
        "capital.view",
    }
)

ACTIVE_INTENT_STATUSES = {
    OrderIntentStatus.PENDING.value,
    OrderIntentStatus.RESERVED.value,
    OrderIntentStatus.READY.value,
    OrderIntentStatus.DISPATCHING.value,
    OrderIntentStatus.SENT.value,
    OrderIntentStatus.PARTIALLY_FILLED.value,
    OrderIntentStatus.UNKNOWN.value,
}

RISK_RESTORE_COOLDOWN = timedelta(minutes=15)
RISK_RESTORE_TTL = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class PreparedExchangeConnectionVerification:
    exchange_account_id: UUID
    team_id: UUID
    account_id: str
    venue: str
    account_version: int
    credential_version: int
    credentials: dict[str, str] = field(repr=False)


@dataclass(frozen=True, slots=True)
class PreparedRuntimeAccountBinding:
    exchange_account_id: UUID
    workspace_id: UUID
    team_id: UUID
    service_principal_id: UUID
    service_principal_username: str
    account_id: str
    venue: str
    account_version: int
    credential_version: int
    credentials: dict[str, str] = field(repr=False)


@dataclass(frozen=True, slots=True)
class PreparedFreqtradeWorkerBinding:
    exchange_account_id: UUID
    workspace_id: UUID
    team_id: UUID
    account_id: str
    venue: str
    account_version: int
    worker_name: str
    worker_url: str
    worker_mode: str
    worker_status: str
    auth_version: int
    username: str = field(repr=False)
    password: str = field(repr=False)
    hip3_dexes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedFreqtradeDispatch:
    mode: str
    external_trade_id: str | None
    intent_version: int


@dataclass(frozen=True, slots=True)
class PreparedPerptapeRuntimeBinding:
    signal_source_id: UUID
    workspace_id: UUID
    team_id: UUID
    service_principal_id: UUID
    service_principal_username: str
    source_version: int
    credential_version: int
    api_key: str = field(repr=False)


OCCUPIED_RESERVATION_STATUSES = {
    ReservationStatus.RESERVED.value,
    ReservationStatus.OPEN.value,
    ReservationStatus.UNKNOWN.value,
}

# Venue clocks and a request's wall clock can differ slightly. Read-only ingestion
# rejects observations more than 30 seconds in the future, so downstream freshness
# checks use the same bounded tolerance.
MAX_FACT_CLOCK_SKEW = timedelta(seconds=30)

RELEASABLE_INTENT_STATUSES = {
    OrderIntentStatus.PENDING.value,
    OrderIntentStatus.RESERVED.value,
    OrderIntentStatus.READY.value,
    OrderIntentStatus.SENT.value,
    OrderIntentStatus.PARTIALLY_FILLED.value,
}

ROLE_ACTIONS: dict[Role, frozenset[str]] = {
    Role.OBSERVER: frozenset(
        {
            "view",
            "opportunity.view",
            "proposal.view",
            "operations.view",
            "system.view",
            "venue.view",
            "results.view",
            "signal.view",
            "notification.view",
        }
    ),
    Role.PROPOSER: frozenset(
        {
            "view",
            "opportunity.view",
            "proposal.view",
            "proposal.create",
            "proposal.submit",
            "signal.view",
            "notification.view",
        }
    ),
    Role.REVIEWER: frozenset(
        {
            "view",
            "proposal.view",
            "proposal.review",
            "system.view",
            "risk.restore.review",
            "risk.restore.execute",
            "signal.view",
            "notification.view",
        }
    ),
    Role.OPERATOR: frozenset(
        {
            "view",
            "proposal.view",
            "operations.view",
            "system.view",
            "venue.view",
            "results.view",
            "risk.decide",
            "authorization.issue",
            "order.prepare",
            "venue.record",
            "reconcile",
            "sender.manage",
            "risk.tighten",
            "risk.restore.request",
            "signal.view",
            "notification.view",
        }
    ),
    Role.TREASURY_ADMIN: frozenset(
        {
            "capital.view",
            "capital.fact.record",
            "capital.propose",
            "capital.submit",
            "capital.review",
            "capital.authorize",
            "capital.execute",
            "capital.reconcile",
            "capital.policy.manage",
            "capital.automation.evaluate",
            "notification.view",
        }
    ),
    Role.SYSTEM_ADMIN: frozenset({"*"}),
}

MAX_ADD_UNITS: dict[RiskTier, int] = {
    RiskTier.LOW: 1,
    RiskTier.MEDIUM: 2,
    RiskTier.HIGH: 3,
}


def _reject(code: str, detail: str) -> NoReturn:
    raise DomainRejected(code, detail)


def _normalize_venue_scope(venue_scope: str | None) -> str | None:
    if venue_scope is None:
        return None
    normalized = venue_scope.strip().upper()
    if normalized not in SUPPORTED_EXCHANGE_VENUES:
        _reject("VENUE_SCOPE_UNSUPPORTED", "venue scope is unsupported")
    return normalized


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _semantic_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _manual_execution_key(
    *,
    environment: str,
    account_id: str,
    venue: str,
    instrument_id: UUID,
    direction: str,
    risk_tier: str,
    quantity: Decimal,
    max_risk: Decimal,
    expires_in_minutes: int,
    details: dict[str, Any],
) -> tuple[Any, ...]:
    """Compare frozen trade instructions while leaving human commentary out of the key."""

    def decimal_detail(name: str, fallback: Decimal | None = None) -> Decimal | None:
        value = details.get(name)
        return fallback if value is None else Decimal(str(value))

    return (
        environment,
        account_id,
        venue,
        instrument_id,
        direction,
        risk_tier,
        quantity,
        max_risk,
        expires_in_minutes,
        decimal_detail("trigger_price"),
        decimal_detail("limit_price"),
        decimal_detail("invalidation_price"),
        decimal_detail("initial_quantity", quantity),
        bool(details.get("allow_auto_add", False)),
        int(details.get("requested_adds", 0)),
        decimal_detail("add_trigger_price"),
    )


def _proposal_manual_execution_key(proposal: Proposal) -> tuple[Any, ...]:
    return _manual_execution_key(
        environment=proposal.environment,
        account_id=proposal.account_id,
        venue=proposal.venue,
        instrument_id=proposal.instrument_id,
        direction=proposal.direction,
        risk_tier=proposal.risk_tier,
        quantity=proposal.quantity,
        max_risk=proposal.max_risk,
        expires_in_minutes=round((proposal.expires_at - proposal.created_at).total_seconds() / 60),
        details=dict(proposal.frozen_payload.get("details") or {}),
    )


def _system_proposal_strategy_family(strategy_id: str) -> tuple[str, tuple[str, ...]]:
    """Treat one-click and automatic Perptape proposal entry points as one signal family."""

    if strategy_id in {"perptape", "perptape-resonance"}:
        return "perptape", ("perptape", "perptape-resonance")
    return strategy_id, (strategy_id,)


def _is_manual_proposal_originator(
    session: Session,
    proposal: Proposal,
    user_id: UUID,
) -> bool:
    if proposal.proposer_id == user_id:
        return True
    return (
        session.scalar(
            select(AuditEvent.audit_event_id)
            .where(
                AuditEvent.event_type == "PROPOSAL_DUPLICATE_REUSED",
                AuditEvent.object_type == "Proposal",
                AuditEvent.object_id == str(proposal.proposal_id),
                AuditEvent.actor_id == str(user_id),
            )
            .limit(1)
        )
        is not None
    )


def _advisory_lock_key(caller_id: str, operation: str, key: str) -> int:
    digest = hashlib.sha256(f"{caller_id}:{operation}:{key}".encode()).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


RISK_CAPACITY_LOCK_KEY = _advisory_lock_key("trading", "risk-capacity", "global")

OCCUPIED_CAPITAL_STATUSES = {
    CapitalTransferStatus.SOURCE_RESERVED.value,
    CapitalTransferStatus.SUBMITTED.value,
    CapitalTransferStatus.IN_FLIGHT.value,
    CapitalTransferStatus.DESTINATION_CONFIRMED.value,
    CapitalTransferStatus.UNKNOWN.value,
    CapitalTransferStatus.MANUAL_REQUIRED.value,
}


def _as_uuid(value: str) -> UUID:
    return UUID(value)


def _scope_key(environment: str, account_id: str, venue: str) -> str:
    return (
        f"{account_id}:{venue}"
        if environment == ExecutionEnvironment.SHADOW.value
        else f"{environment}:{account_id}:{venue}"
    )


def _scope_parts(execution_scope: str) -> tuple[ExecutionEnvironment, str, str]:
    parts = execution_scope.split(":")
    if len(parts) == 2:
        environment = ExecutionEnvironment.SHADOW
        account_id, venue = parts
    elif len(parts) == 3:
        try:
            environment = ExecutionEnvironment(parts[0])
        except ValueError:
            _reject("EXECUTION_SCOPE_INVALID", "execution scope environment is invalid")
        account_id, venue = parts[1:]
    else:
        _reject(
            "EXECUTION_SCOPE_INVALID",
            "execution scope must be account:venue or environment:account:venue",
        )
    if not account_id or not venue or account_id.strip() != account_id or venue.strip() != venue:
        _reject("EXECUTION_SCOPE_INVALID", "execution scope must contain non-empty exact parts")
    return environment, account_id, venue


def fact_is_stale(observed_at: datetime, now: datetime, max_age: timedelta) -> bool:
    """Apply the single bounded-clock freshness rule used by writes and projections."""

    return observed_at > now + MAX_FACT_CLOCK_SKEW or now - observed_at > max_age


__all__ = [
    "ACTIVE_INTENT_STATUSES",
    "AGENT_TOKEN_MARKER",
    "API_CLIENT_ALLOWED_BUSINESS_ACTIONS",
    "API_CLIENT_HUMAN_ONLY_ACTIONS",
    "CAPITAL_HISTORY_MIN_INTERVAL",
    "CONNECTION_ERROR_CODE_PATTERN",
    "DEFAULT_FREQTRADE_LEVERAGE",
    "DEFAULT_SENDER_LEASE_DURATION",
    "FENCING_REJECTIONS",
    "INTENT_TRANSITIONS",
    "MAX_ADD_UNITS",
    "MAX_FACT_CLOCK_SKEW",
    "NAMESPACE_URL",
    "OCCUPIED_CAPITAL_STATUSES",
    "OCCUPIED_RESERVATION_STATUSES",
    "PASSWORD_HASHER",
    "PROTECTION_ISSUES",
    "RECONCILIATION_RESULTS",
    "RELEASABLE_INTENT_STATUSES",
    "RISK_CAPACITY_LOCK_KEY",
    "RISK_RESTORE_COOLDOWN",
    "RISK_RESTORE_TTL",
    "RISK_RESULTS",
    "ROLE_ACTIONS",
    "SCOPE_SLUG_PATTERN",
    "SIGNAL_CLOCK_SKEW",
    "SIGNAL_NONCE_PATTERN",
    "SUPPORTED_EXCHANGE_VENUES",
    "TEAM_SETUP_ACTIONS",
    "USD_STABLE_ASSETS",
    "UTC",
    "UUID",
    "AbstractContextManager",
    "AccountEquity",
    "AccountEquityObservation",
    "AddCandidateFacts",
    "AnalyticsEquitySnapshot",
    "AnalyticsReport",
    "Any",
    "ApiClient",
    "ApiClientState",
    "Approval",
    "AuditEvent",
    "BinanceInstrument",
    "BinanceReadOnlySnapshot",
    "BinanceTestnetOrder",
    "BinanceTestnetOrderCommand",
    "BinanceTestnetProtectionCommand",
    "Campaign",
    "CampaignStatus",
    "CapabilityGate",
    "CapabilityStatus",
    "CapitalAutomationPolicy",
    "CapitalDirection",
    "CapitalTransfer",
    "CapitalTransferCommand",
    "CapitalTransferStatus",
    "CapitalTransferSubmission",
    "CommandReceipt",
    "ConnectionProbeResult",
    "Decimal",
    "DirectCapitalConfiguration",
    "DirectCapitalOperation",
    "DirectCapitalPath",
    "DirectCapitalPlan",
    "Direction",
    "DomainRejected",
    "EconomicFill",
    "ExchangeAccount",
    "ExecutionEnvironment",
    "FactStatus",
    "FreqtradeEntryCommand",
    "FreqtradeExitCommand",
    "FreqtradeTrade",
    "FundingPayment",
    "HyperliquidInstrument",
    "HyperliquidReadOnlySnapshot",
    "HyperliquidTestnetOrder",
    "HyperliquidTestnetOrderCommand",
    "HyperliquidTestnetProtectionCommand",
    "IdempotencyConflict",
    "Instrument",
    "IntentCreation",
    "IntentKind",
    "NoTiltReceipt",
    "NoTiltUnsignedTransaction",
    "NoTiltVaultSnapshot",
    "NotificationDelivery",
    "NotificationRoute",
    "OrderIntent",
    "OrderIntentStatus",
    "PerptapeCandidate",
    "PerptapeFeed",
    "PerptapeFeedSnapshot",
    "PnlBreakdown",
    "Position",
    "PreparedExchangeConnectionVerification",
    "PreparedFreqtradeDispatch",
    "PreparedFreqtradeWorkerBinding",
    "PreparedPerptapeRuntimeBinding",
    "PreparedRuntimeAccountBinding",
    "PrincipalType",
    "Proposal",
    "ProposalDefaultConfig",
    "ProposalSource",
    "ProposalStatus",
    "ProtectionCancelCommand",
    "ProtectionOrder",
    "ProtectionStatus",
    "ReconciliationRun",
    "ReconciliationStatus",
    "ReservationStatus",
    "ReviewDecision",
    "RiskControlChangeRequest",
    "RiskDecision",
    "RiskEvaluationInput",
    "RiskPolicy",
    "RiskPolicyChangeStatus",
    "RiskPolicyInput",
    "RiskReservation",
    "RiskResult",
    "RiskTier",
    "Role",
    "RoleAssignment",
    "RuntimeSourceHealth",
    "SenderLease",
    "Sequence",
    "ServicePrincipalKind",
    "Session",
    "ShadowFill",
    "ShadowInstrument",
    "ShadowOrder",
    "ShadowPosition",
    "SignalEvent",
    "SignalEventStatus",
    "SignalSourceMode",
    "SystemRiskState",
    "TargetCandidate",
    "TargetDecision",
    "TargetUrgency",
    "Team",
    "TeamExecutionMode",
    "TeamMembership",
    "TeamShadowAccount",
    "TeamSignalSource",
    "TradingAuthorization",
    "TransferAuthorization",
    "TransferProposal",
    "UsdValuation",
    "User",
    "VenueFill",
    "VenueInstrument",
    "VenueOrder",
    "VenueOrderStatus",
    "VenueReadOnlySnapshot",
    "Workspace",
    "WorkspaceMembership",
    "WorkspaceRole",
    "_advisory_lock_key",
    "_as_uuid",
    "_canonical",
    "_is_manual_proposal_originator",
    "_manual_execution_key",
    "_normalize_venue_scope",
    "_proposal_manual_execution_key",
    "_reject",
    "_scope_key",
    "_scope_parts",
    "_semantic_hash",
    "_system_proposal_strategy_family",
    "apply_perptape_feed_delta",
    "apply_shadow_fill",
    "apply_shadow_ledger_fill",
    "base64",
    "binascii",
    "bound_perptape_feed_snapshot",
    "compute_pnl",
    "current_api_client_context",
    "datetime",
    "delete",
    "evaluate_capital_automation",
    "evaluate_risk",
    "fact_is_stale",
    "freqtrade_pair",
    "func",
    "hashlib",
    "hmac",
    "issue_agent_token",
    "issue_api_client_token",
    "json",
    "normalize_notification_event_types",
    "normalize_perptape_datetime",
    "notification_template",
    "nullcontext",
    "parse_agent_token",
    "parse_api_client_token",
    "parse_hip3_dexes",
    "perptape_snapshot_identity",
    "quantize_shadow_step",
    "quote_shadow_execution",
    "re",
    "select",
    "select_target_position",
    "shadow_limit_crossed",
    "shadow_protection_triggered",
    "text",
    "timedelta",
    "uuid4",
    "uuid5",
    "validate_agent_roles",
    "validate_notification_configuration",
    "validate_notification_payload",
    "validate_perptape_feed_payload",
    "validate_worker_url",
]
