from __future__ import annotations

# Route modules import their explicit dependencies from this shared API surface.
import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote
from uuid import UUID

from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import ValidationError

from trading_control_plane import __version__
from trading_control_plane.adapters.facts import FactAdapterConnectionProbe
from trading_control_plane.adapters.hyperliquid_capital import (
    HYPERLIQUID_BRIDGE2_ADDRESS,
)
from trading_control_plane.api_schemas import (
    AccountEquityFactRequest,
    AgentAccessRequest,
    AgentCreateRequest,
    AgentProposalRequest,
    AgentTokenRotationRequest,
    AnalyticsReportCreateRequest,
    ApiClientCreateRequest,
    ApiClientRevokeRequest,
    ApiClientStateRequest,
    AuthorizationRequest,
    AutoAddRequest,
    AutomaticExitRequest,
    CampaignTargetRequest,
    CapitalAutomationEvaluateRequest,
    CapitalAutomationPolicyRequest,
    CapitalBalanceFactRequest,
    CapitalScopeReconciliationRequest,
    CapitalTransferCreateRequest,
    CapitalTransferObservationRequest,
    DirectCapitalBinanceReceiptRequest,
    DirectCapitalBinanceSubmissionRequest,
    DirectCapitalConfigurationRequest,
    DirectCapitalHyperliquidReceiptRequest,
    DirectCapitalOperationRequest,
    DirectCapitalTreasuryReceiptRequest,
    DirectCapitalUnsignedPlanRequest,
    DirectCapitalWalletSubmissionRequest,
    ExchangeAccountCreateRequest,
    ExchangeAccountDeleteRequest,
    ExchangeAccountStateRequest,
    ExchangeAccountUpdateRequest,
    ExchangeConnectionVerifyRequest,
    ExchangeCredentialRotateRequest,
    ExchangeRuntimeSyncRequest,
    ExchangeTradingEligibilityRequest,
    FreqtradeActionRequest,
    FreqtradeWorkerConfigureRequest,
    FreqtradeWorkerVerifyRequest,
    FundingFactRequest,
    IntentReleaseRequest,
    IntentUnknownRequest,
    ManagedReductionRequest,
    ManagedUserAccessRequest,
    ManagedUserCreateRequest,
    ManualProposalRequest,
    MockLoginRequest,
    MockStepUpRequest,
    NotificationRouteDeleteRequest,
    NotificationRouteWriteRequest,
    NotificationTestRequest,
    NoTiltReceiptRequest,
    OrderIntentRequest,
    PasswordChangeRequest,
    PasswordLoginRequest,
    PositionFactRequest,
    ProposalDefaultConfigRequest,
    ProtectionFactRequest,
    ReconciliationReasonRequest,
    ReconciliationRequest,
    ReductionIntentRequest,
    ReviewRequest,
    RiskControlChangeCreateRequest,
    RiskControlChangeExecuteRequest,
    RiskControlChangeReviewRequest,
    RiskControlDirectRestoreRequest,
    RiskDecisionRequest,
    RiskPolicyConfigureRequest,
    RiskTightenRequest,
    ScopeSelectRequest,
    SenderLeaseRequest,
    SignalProposalRequest,
    SignalSourceConfigureRequest,
    SignalSourceCreateRequest,
    SignalSourceCredentialRotateRequest,
    SignalSourceDeleteRequest,
    SignalSourceStateRequest,
    SignalSourceTestRequest,
    SignalSourceUpdateRequest,
    SystemProposalRequest,
    TeamCreateRequest,
    TeamMemberInviteRequest,
    TeamMemberRemoveRequest,
    TeamTradingModeRequest,
    TransferAuthorizationRequest,
    TransferProposalRequest,
    TransferReviewRequest,
    WebhookSignalPayload,
    WorkspaceCreateRequest,
)
from trading_control_plane.auth import SessionIdentity, SignedTokenService
from trading_control_plane.capital import MockCapitalTransferAdapter, build_direct_capital_plan
from trading_control_plane.config import Settings, get_settings
from trading_control_plane.connections import project_runtime_connections
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    AddCandidateFacts,
    CapitalDirection,
    CapitalTransferStatus,
    CapitalTreasuryProvider,
    DirectCapitalPath,
    Direction,
    DomainRejected,
    ExecutionEnvironment,
    IntentKind,
    OrderIntentStatus,
    ProposalSource,
    ProposalStatus,
    ReviewDecision,
    Role,
    SignalSourceMode,
    TargetCandidate,
)
from trading_control_plane.freqtrade import (
    FreqtradeEntryCommand,
    FreqtradeExitCommand,
    FreqtradeWorkerClient,
    FreqtradeWorkerSpec,
    freqtrade_pair,
)
from trading_control_plane.logging import configure_logging
from trading_control_plane.metrics import DATABASE_READY
from trading_control_plane.notilt import (
    SUPPORTED_NOTILT_CHAINS,
    NoTiltGateway,
    NoTiltUnsignedTransaction,
    NoTiltUsdValuator,
)
from trading_control_plane.passwords import (
    ApiClientRateLimiter,
    LoginAttemptLimiter,
    PasswordHasher,
)
from trading_control_plane.perptape import (
    PerptapeCandidate,
    PerptapeClient,
    perptape_candidate_identity_is_displayable,
    perptape_legacy_candidate_id,
)
from trading_control_plane.quantstats_adapter import (
    QuantStatsReportAdapter,
    report_metadata,
)
from trading_control_plane.queries import TradingQueries
from trading_control_plane.report_engines import render_report, report_engine_catalog
from trading_control_plane.safe_spending import SafeSpendingGateway
from trading_control_plane.service import PreparedFreqtradeWorkerBinding, TradingService
from trading_control_plane.telegram import (
    CampaignNotification,
    CapitalNotification,
    MockTelegramGateway,
    ProposalNotification,
    TelegramBotGateway,
    TelegramGateway,
    TelegramProposalReviewAction,
)


class ExchangeConnectionVerifier(Protocol):
    def verify(
        self,
        *,
        workspace_id: str,
        team_id: str,
        account_id: str,
        venue: str,
        environment: str,
        account_mode: str,
        credentials: Mapping[str, str],
        now: datetime,
    ) -> Any: ...

logger = logging.getLogger(__name__)
WEB_ROOT = Path(__file__).parent / "web"
SESSION_COOKIE = "trading_session"


class ReadinessDatabase(Protocol):
    def is_ready(self) -> tuple[bool, str | None]: ...

    def dispose(self) -> None: ...


def _now() -> datetime:
    return datetime.now(UTC)


def _perptape_runtime_status(
    settings: Settings,
    feed: dict[str, Any],
    *,
    now: datetime,
    configured: bool | None = None,
) -> str:
    if not (bool(settings.perptape_api_key) if configured is None else configured):
        return "NOT_CONFIGURED"
    if not feed["available"]:
        return "WAITING" if settings.runtime_sync_enabled else "ON_DEMAND"
    if feed["contract_version"] != settings.perptape_contract_version:
        return "STALE"
    try:
        fetched_at = datetime.fromisoformat(feed["fetched_at"])
    except (TypeError, ValueError):
        return "STALE"
    stale_after = timedelta(
        seconds=settings.runtime_sync_interval_seconds + int(settings.perptape_timeout_seconds)
    )
    return "STALE" if now - fetched_at > stale_after else "SUCCESS"


def _perptape_transport_status(
    settings: Settings,
    source_health: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    websocket = source_health.get("PERPTAPE_WEBSOCKET")
    polling = source_health.get("PERPTAPE")
    freshness = timedelta(
        seconds=max(
            settings.runtime_sync_interval_seconds * 2 + int(settings.perptape_timeout_seconds),
            settings.perptape_websocket_heartbeat_timeout_seconds * 2,
        )
    )

    def fresh(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        try:
            checked_at = datetime.fromisoformat(str(value["checked_at"]))
        except (KeyError, TypeError, ValueError):
            return False
        age = now - checked_at
        return checked_at.tzinfo is not None and age >= -timedelta(seconds=30) and age <= freshness

    polling_live = (
        fresh(polling) and isinstance(polling, dict) and polling.get("status") == "SUCCESS"
    )
    websocket_fresh = fresh(websocket)
    websocket_status = websocket.get("status") if isinstance(websocket, dict) else None
    websocket_error_value = websocket.get("error_code") if isinstance(websocket, dict) else None
    websocket_error = None if websocket_error_value is None else str(websocket_error_value)
    state: str
    primary_channel: str | None
    fallback_active: bool
    error_code: str | None
    if websocket_fresh and websocket_status == "SUCCESS":
        state = "WEBSOCKET_LIVE"
        primary_channel = "WEBSOCKET"
        fallback_active = False
        error_code = None
    elif websocket_fresh and websocket_status == "SKIPPED":
        state = "WEBSOCKET_STARTING"
        primary_channel = "HTTPS_POLLING" if polling_live else None
        fallback_active = polling_live
        error_code = websocket_error or "PERPTAPE_STREAM_STARTING"
    elif isinstance(websocket, dict) and (websocket_status == "FAILED" or not websocket_fresh):
        state = "POLLING_FALLBACK" if polling_live else "WEBSOCKET_FAILED"
        primary_channel = "HTTPS_POLLING" if polling_live else None
        fallback_active = polling_live
        error_code = websocket_error if websocket_fresh else "PERPTAPE_WEBSOCKET_HEALTH_STALE"
    elif polling_live:
        state = "POLLING_ONLY"
        primary_channel = "HTTPS_POLLING"
        fallback_active = False
        error_code = None
    elif isinstance(polling, dict) and fresh(polling) and polling.get("status") == "FAILED":
        state = "POLLING_FAILED"
        primary_channel = None
        fallback_active = False
        polling_error = polling.get("error_code")
        error_code = None if polling_error is None else str(polling_error)
    else:
        state = "WAITING"
        primary_channel = None
        fallback_active = False
        error_code = None
    return {
        "state": state,
        "primary_channel": primary_channel,
        "fallback_active": fallback_active,
        "error_code": error_code,
        "websocket": websocket,
        "polling": polling,
    }


def _domain_status(code: str) -> int:
    if code in {
        "LOGIN_DENIED",
        "AUTH_TOKEN_INVALID",
        "SESSION_EXPIRED",
        "SESSION_REVOKED",
        "AGENT_TOKEN_INVALID",
        "AGENT_TOKEN_EXPIRED",
        "SIGNAL_SIGNATURE_INVALID",
    }:
        return status.HTTP_401_UNAUTHORIZED
    if code in {
        "RBAC_DENIED",
        "SELF_REVIEW_FORBIDDEN",
        "ACTION_GRANT_REQUIRED",
        "ACTION_GRANT_SCOPE_INVALID",
        "ACTION_GRANT_EXPIRED",
        "ACTION_REFERENCE_SCOPE_INVALID",
        "ACTION_REFERENCE_EXPIRED",
        "SELF_ACCESS_CHANGE_DENIED",
        "WORKSPACE_ACCESS_DENIED",
        "WORKSPACE_ADMIN_REQUIRED",
        "TEAM_ACCESS_DENIED",
        "TEAM_SCOPE_DENIED",
        "WORKSPACE_MEMBERSHIP_INACTIVE",
        "AGENT_STEP_UP_FORBIDDEN",
        "AGENT_IDENTITY_REQUIRED",
        "AGENT_PROPOSAL_ENDPOINT_REQUIRED",
        "API_CLIENT_SCOPE_DENIED",
        "HUMAN_WEB_CONFIRMATION_REQUIRED",
        "RISK_CHANGE_REVIEW_REQUIRED",
    }:
        return status.HTTP_403_FORBIDDEN
    if code == "AUTH_CREDENTIAL_AMBIGUOUS":
        return status.HTTP_400_BAD_REQUEST
    if code.endswith("_NOT_FOUND"):
        return status.HTTP_404_NOT_FOUND
    if code in {
        "PERPTAPE_RATE_LIMITED",
        "API_CLIENT_RATE_LIMITED",
        "BINANCE_RATE_LIMITED",
        "BINANCE_CAPITAL_RATE_LIMITED",
        "BINANCE_CONNECTION_WEIGHT_HEADROOM_DEFERRED",
        "BINANCE_CAPITAL_WEIGHT_HEADROOM_DEFERRED",
    }:
        return status.HTTP_429_TOO_MANY_REQUESTS
    if code in {
        "IDEMPOTENCY_CONFLICT",
        "VERSION_CONFLICT",
        "REVIEW_ALREADY_RECORDED",
        "PROPOSAL_NOT_DRAFT",
        "PROPOSAL_NOT_REVIEWABLE",
        "PROPOSAL_NOT_APPROVED",
        "INITIAL_INTENT_ALREADY_EXISTS",
        "USERNAME_CONFLICT",
        "WORKSPACE_SLUG_CONFLICT",
        "TEAM_SLUG_CONFLICT",
        "TEAM_MEMBERSHIP_CONFLICT",
        "WORKSPACE_CONTEXT_REQUIRED",
        "TEAM_CONTEXT_REQUIRED",
        "TEAM_NOT_OPERATIONAL",
        "LAST_SYSTEM_ADMIN_REQUIRED",
        "SIGNAL_REPLAY_DETECTED",
        "SIGNAL_ALREADY_CONSUMED",
        "SIGNAL_SOURCE_MODE_MISMATCH",
        "SIGNAL_SOURCE_DISABLED",
        "AUTO_PROPOSAL_SOURCE_INVALID",
        "NOTIFICATION_ROUTE_NAME_CONFLICT",
        "NOTIFICATION_ROUTE_UNAVAILABLE",
        "FREQTRADE_DISPATCH_ALREADY_STARTED",
        "API_CLIENT_NAME_CONFLICT",
        "API_CLIENT_REVOKED",
        "BINANCE_RECEIPT_CHECK_IN_PROGRESS",
    }:
        return status.HTTP_409_CONFLICT
    if code in {
        "PERPTAPE_UNAVAILABLE",
        "PERPTAPE_NOT_CONFIGURED",
        "PERPTAPE_CACHE_UNAVAILABLE",
        "PERPTAPE_CACHE_STALE",
        "BINANCE_READ_ONLY_DISABLED",
        "BINANCE_READ_ONLY_NOT_CONFIGURED",
        "BINANCE_READ_ONLY_UNAVAILABLE",
        "BINANCE_TESTNET_DISABLED",
        "BINANCE_TESTNET_NOT_CONFIGURED",
        "BINANCE_TESTNET_UNAVAILABLE",
        "BINANCE_TESTNET_OUTCOME_UNKNOWN",
        "BINANCE_LIVE_DISABLED",
        "BINANCE_LIVE_NOT_CONFIGURED",
        "BINANCE_LIVE_UNAVAILABLE",
        "BINANCE_LIVE_OUTCOME_UNKNOWN",
        "HYPERLIQUID_READ_ONLY_DISABLED",
        "HYPERLIQUID_READ_ONLY_NOT_CONFIGURED",
        "HYPERLIQUID_READ_ONLY_UNAVAILABLE",
        "HYPERLIQUID_TESTNET_DISABLED",
        "HYPERLIQUID_TESTNET_NOT_CONFIGURED",
        "HYPERLIQUID_TESTNET_UNAVAILABLE",
        "HYPERLIQUID_TESTNET_OUTCOME_UNKNOWN",
        "HYPERLIQUID_LIVE_DISABLED",
        "HYPERLIQUID_LIVE_NOT_CONFIGURED",
        "HYPERLIQUID_LIVE_UNAVAILABLE",
        "HYPERLIQUID_LIVE_OUTCOME_UNKNOWN",
        "FREQTRADE_WORKER_UNAVAILABLE",
        "FREQTRADE_LIVE_OUTCOME_UNKNOWN",
        "FREQTRADE_PROTECTION_UNCONFIRMED",
        "DEFAULT_ACCOUNT_NOT_CONFIGURED",
        "SIGNAL_SOURCE_NOT_CONFIGURED",
        "CREDENTIAL_ENCRYPTION_KEY_MISSING",
    }:
        return status.HTTP_503_SERVICE_UNAVAILABLE
    if code in {
        "PERPTAPE_RESPONSE_INVALID",
        "PERPTAPE_CACHE_INVALID",
        "BINANCE_RESPONSE_INVALID",
        "BINANCE_TESTNET_RESPONSE_INVALID",
        "BINANCE_LIVE_RESPONSE_INVALID",
        "HYPERLIQUID_RESPONSE_INVALID",
        "HYPERLIQUID_TESTNET_RESPONSE_INVALID",
        "HYPERLIQUID_LIVE_RESPONSE_INVALID",
        "FREQTRADE_WORKER_RESPONSE_INVALID",
    }:
        return status.HTTP_502_BAD_GATEWAY
    return status.HTTP_422_UNPROCESSABLE_CONTENT


__all__ = [
    "CONTENT_TYPE_LATEST",
    "DATABASE_READY",
    "HYPERLIQUID_BRIDGE2_ADDRESS",
    "ROUND_DOWN",
    "SESSION_COOKIE",
    "SUPPORTED_NOTILT_CHAINS",
    "UTC",
    "UUID",
    "WEB_ROOT",
    "AccountEquityFactRequest",
    "AddCandidateFacts",
    "AgentAccessRequest",
    "AgentCreateRequest",
    "AgentProposalRequest",
    "AgentTokenRotationRequest",
    "AnalyticsReportCreateRequest",
    "Any",
    "ApiClientCreateRequest",
    "ApiClientRateLimiter",
    "ApiClientRevokeRequest",
    "ApiClientStateRequest",
    "AsyncIterator",
    "AuthorizationRequest",
    "AutoAddRequest",
    "AutomaticExitRequest",
    "CampaignNotification",
    "CampaignTargetRequest",
    "CapitalAutomationEvaluateRequest",
    "CapitalAutomationPolicyRequest",
    "CapitalBalanceFactRequest",
    "CapitalDirection",
    "CapitalNotification",
    "CapitalScopeReconciliationRequest",
    "CapitalTransferCreateRequest",
    "CapitalTransferObservationRequest",
    "CapitalTransferStatus",
    "CapitalTreasuryProvider",
    "Cookie",
    "Database",
    "Decimal",
    "Depends",
    "DirectCapitalBinanceReceiptRequest",
    "DirectCapitalBinanceSubmissionRequest",
    "DirectCapitalConfigurationRequest",
    "DirectCapitalHyperliquidReceiptRequest",
    "DirectCapitalOperationRequest",
    "DirectCapitalPath",
    "DirectCapitalTreasuryReceiptRequest",
    "DirectCapitalUnsignedPlanRequest",
    "DirectCapitalWalletSubmissionRequest",
    "Direction",
    "DomainRejected",
    "ExchangeAccountCreateRequest",
    "ExchangeAccountDeleteRequest",
    "ExchangeAccountStateRequest",
    "ExchangeAccountUpdateRequest",
    "ExchangeConnectionVerifier",
    "ExchangeConnectionVerifyRequest",
    "ExchangeCredentialRotateRequest",
    "ExchangeRuntimeSyncRequest",
    "ExchangeTradingEligibilityRequest",
    "ExecutionEnvironment",
    "FactAdapterConnectionProbe",
    "FastAPI",
    "FileResponse",
    "FreqtradeActionRequest",
    "FreqtradeEntryCommand",
    "FreqtradeExitCommand",
    "FreqtradeWorkerClient",
    "FreqtradeWorkerConfigureRequest",
    "FreqtradeWorkerSpec",
    "FreqtradeWorkerVerifyRequest",
    "FundingFactRequest",
    "HTTPException",
    "Header",
    "IntentKind",
    "IntentReleaseRequest",
    "IntentUnknownRequest",
    "JSONResponse",
    "LoginAttemptLimiter",
    "ManagedReductionRequest",
    "ManagedUserAccessRequest",
    "ManagedUserCreateRequest",
    "ManualProposalRequest",
    "MockCapitalTransferAdapter",
    "MockLoginRequest",
    "MockStepUpRequest",
    "MockTelegramGateway",
    "NoTiltGateway",
    "NoTiltReceiptRequest",
    "NoTiltUnsignedTransaction",
    "NoTiltUsdValuator",
    "NotificationRouteDeleteRequest",
    "NotificationRouteWriteRequest",
    "NotificationTestRequest",
    "OrderIntentRequest",
    "OrderIntentStatus",
    "PasswordChangeRequest",
    "PasswordHasher",
    "PasswordLoginRequest",
    "PerptapeCandidate",
    "PerptapeClient",
    "PositionFactRequest",
    "PreparedFreqtradeWorkerBinding",
    "ProposalDefaultConfigRequest",
    "ProposalNotification",
    "ProposalSource",
    "ProposalStatus",
    "ProtectionFactRequest",
    "QuantStatsReportAdapter",
    "Query",
    "ReadinessDatabase",
    "ReconciliationReasonRequest",
    "ReconciliationRequest",
    "ReductionIntentRequest",
    "Request",
    "Response",
    "ReviewDecision",
    "ReviewRequest",
    "RiskControlChangeCreateRequest",
    "RiskControlChangeExecuteRequest",
    "RiskControlChangeReviewRequest",
    "RiskControlDirectRestoreRequest",
    "RiskDecisionRequest",
    "RiskPolicyConfigureRequest",
    "RiskTightenRequest",
    "Role",
    "SafeSpendingGateway",
    "ScopeSelectRequest",
    "SenderLeaseRequest",
    "SessionIdentity",
    "Settings",
    "SignalProposalRequest",
    "SignalSourceConfigureRequest",
    "SignalSourceCreateRequest",
    "SignalSourceCredentialRotateRequest",
    "SignalSourceDeleteRequest",
    "SignalSourceMode",
    "SignalSourceStateRequest",
    "SignalSourceTestRequest",
    "SignalSourceUpdateRequest",
    "SignedTokenService",
    "StaticFiles",
    "SystemProposalRequest",
    "TargetCandidate",
    "TeamCreateRequest",
    "TeamMemberInviteRequest",
    "TeamMemberRemoveRequest",
    "TeamTradingModeRequest",
    "TelegramBotGateway",
    "TelegramGateway",
    "TelegramProposalReviewAction",
    "TradingQueries",
    "TradingService",
    "TransferAuthorizationRequest",
    "TransferProposalRequest",
    "TransferReviewRequest",
    "ValidationError",
    "WebSocket",
    "WebSocketDisconnect",
    "WebhookSignalPayload",
    "WorkspaceCreateRequest",
    "__version__",
    "_domain_status",
    "_now",
    "_perptape_runtime_status",
    "_perptape_transport_status",
    "asynccontextmanager",
    "asyncio",
    "build_direct_capital_plan",
    "configure_logging",
    "datetime",
    "freqtrade_pair",
    "generate_latest",
    "get_settings",
    "hashlib",
    "json",
    "logger",
    "perptape_candidate_identity_is_displayable",
    "perptape_legacy_candidate_id",
    "project_runtime_connections",
    "quote",
    "render_report",
    "report_engine_catalog",
    "report_metadata",
    "status",
    "timedelta",
]
