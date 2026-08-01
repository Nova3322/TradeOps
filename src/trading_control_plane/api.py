from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote
from uuid import UUID

from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from trading_control_plane import __version__
from trading_control_plane.api_schemas import (
    AccountEquityFactRequest,
    AuthorizationRequest,
    AutoAddRequest,
    AutomaticExitRequest,
    BinanceReadOnlySyncRequest,
    BinanceTestnetActionRequest,
    BinanceTestnetProtectionRequest,
    CampaignTargetRequest,
    CapitalAutomationEvaluateRequest,
    CapitalAutomationPolicyRequest,
    CapitalBalanceFactRequest,
    CapitalScopeReconciliationRequest,
    CapitalTransferCreateRequest,
    CapitalTransferObservationRequest,
    FundingFactRequest,
    HyperliquidReadOnlySyncRequest,
    HyperliquidTestnetProtectionRequest,
    IntentReleaseRequest,
    IntentUnknownRequest,
    ManagedReductionRequest,
    ManualProposalRequest,
    MockLoginRequest,
    MockStepUpRequest,
    NoTiltReceiptRequest,
    OrderIntentRequest,
    PositionFactRequest,
    ProtectionFactRequest,
    ReconciliationReasonRequest,
    ReconciliationRequest,
    ReductionIntentRequest,
    ReviewRequest,
    RiskControlChangeCreateRequest,
    RiskControlChangeExecuteRequest,
    RiskControlChangeReviewRequest,
    RiskDecisionRequest,
    RiskTightenRequest,
    SenderLeaseRequest,
    ShadowFillRequest,
    ShadowSendRequest,
    SystemProposalRequest,
    TelegramCampaignActionRequest,
    TransferAuthorizationRequest,
    TransferProposalRequest,
    TransferReviewRequest,
)
from trading_control_plane.auth import SessionIdentity, SignedTokenService
from trading_control_plane.binance import (
    BinancePortfolioMarginReadOnlyClient,
    BinanceReadOnlyClient,
)
from trading_control_plane.binance_execution import (
    BinancePortfolioMarginClient,
    BinanceTestnetClient,
    BinanceTestnetOrder,
    BinanceTestnetOrderCommand,
    BinanceTestnetProtectionCommand,
)
from trading_control_plane.capital import MockCapitalTransferAdapter
from trading_control_plane.config import Settings, get_settings
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    AddCandidateFacts,
    CapitalDirection,
    CapitalTransferStatus,
    DomainRejected,
    ExecutionEnvironment,
    IntentKind,
    OrderIntentStatus,
    ProposalSource,
    ProposalStatus,
    ReviewDecision,
    TargetCandidate,
    TargetUrgency,
)
from trading_control_plane.hyperliquid import (
    HyperliquidReadOnlyClient,
    resolve_hyperliquid_main_account,
)
from trading_control_plane.hyperliquid_execution import (
    HyperliquidLiveClient,
    HyperliquidTestnetClient,
    HyperliquidTestnetOrder,
    HyperliquidTestnetOrderCommand,
    HyperliquidTestnetProtectionCommand,
    build_hyperliquid_signer,
)
from trading_control_plane.logging import configure_logging
from trading_control_plane.metrics import DATABASE_READY
from trading_control_plane.notilt import (
    SUPPORTED_NOTILT_CHAINS,
    NoTiltGateway,
    NoTiltUnsignedTransaction,
    NoTiltUsdValuator,
)
from trading_control_plane.perptape import (
    PerptapeCandidate,
    PerptapeClient,
    perptape_legacy_candidate_id,
)
from trading_control_plane.queries import TradingQueries
from trading_control_plane.service import TradingService
from trading_control_plane.telegram import (
    CampaignNotification,
    CapitalNotification,
    MockTelegramGateway,
    ProposalNotification,
    TelegramBotGateway,
    TelegramCampaignAction,
    TelegramGateway,
    campaign_position_reduction_available,
)

logger = logging.getLogger(__name__)
WEB_ROOT = Path(__file__).parent / "web"
SESSION_COOKIE = "trading_session"


class ReadinessDatabase(Protocol):
    def is_ready(self) -> tuple[bool, str | None]: ...

    def dispose(self) -> None: ...


def _now() -> datetime:
    return datetime.now(UTC)


def _domain_status(code: str) -> int:
    if code in {"LOGIN_DENIED", "AUTH_TOKEN_INVALID", "SESSION_EXPIRED", "SESSION_REVOKED"}:
        return status.HTTP_401_UNAUTHORIZED
    if code in {
        "RBAC_DENIED",
        "SELF_REVIEW_FORBIDDEN",
        "ACTION_GRANT_REQUIRED",
        "ACTION_GRANT_SCOPE_INVALID",
        "ACTION_GRANT_EXPIRED",
        "ACTION_REFERENCE_SCOPE_INVALID",
        "ACTION_REFERENCE_EXPIRED",
    }:
        return status.HTTP_403_FORBIDDEN
    if code.endswith("_NOT_FOUND"):
        return status.HTTP_404_NOT_FOUND
    if code == "PERPTAPE_RATE_LIMITED":
        return status.HTTP_429_TOO_MANY_REQUESTS
    if code in {
        "IDEMPOTENCY_CONFLICT",
        "VERSION_CONFLICT",
        "REVIEW_ALREADY_RECORDED",
        "PROPOSAL_NOT_DRAFT",
        "PROPOSAL_NOT_REVIEWABLE",
        "PROPOSAL_NOT_APPROVED",
        "INITIAL_INTENT_ALREADY_EXISTS",
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
    }:
        return status.HTTP_502_BAD_GATEWAY
    return status.HTTP_422_UNPROCESSABLE_CONTENT


def create_app(
    settings: Settings | None = None,
    database: ReadinessDatabase | None = None,
    perptape_client: PerptapeClient | None = None,
    telegram_gateway: TelegramGateway | None = None,
    binance_client: BinanceReadOnlyClient | BinancePortfolioMarginReadOnlyClient | None = None,
    binance_live_client: BinancePortfolioMarginClient | None = None,
    binance_testnet_client: BinanceTestnetClient | None = None,
    binance_testnet_reader: BinanceReadOnlyClient | None = None,
    hyperliquid_client: HyperliquidReadOnlyClient | None = None,
    hyperliquid_live_client: HyperliquidLiveClient | None = None,
    hyperliquid_testnet_client: HyperliquidTestnetClient | None = None,
    capital_transfer_adapter: MockCapitalTransferAdapter | None = None,
    notilt_gateway: NoTiltGateway | None = None,
    notilt_valuator: NoTiltUsdValuator | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    resolved_settings.validate_runtime_security()
    configure_logging(resolved_settings.log_level)
    resolved_database = database or Database(resolved_settings.database_url)
    token_service = SignedTokenService(resolved_settings.session_signing_secret)
    resolved_perptape = perptape_client or PerptapeClient(
        base_url=resolved_settings.perptape_base_url,
        api_key=resolved_settings.perptape_api_key,
        contract_version=resolved_settings.perptape_contract_version,
        cache_ttl=timedelta(seconds=resolved_settings.perptape_cache_seconds),
        timeout_seconds=resolved_settings.perptape_timeout_seconds,
    )
    if telegram_gateway is not None:
        resolved_telegram = telegram_gateway
    elif resolved_settings.telegram_enabled:
        if not isinstance(resolved_database, Database):
            raise ValueError("real Telegram requires the durable Trading database")
        assert resolved_settings.telegram_bot_token is not None
        assert resolved_settings.telegram_allowed_username is not None
        assert resolved_settings.telegram_internal_username is not None
        resolved_telegram = TelegramBotGateway(
            token=resolved_settings.telegram_bot_token,
            allowed_username=resolved_settings.telegram_allowed_username,
            internal_username=resolved_settings.telegram_internal_username,
            binder=lambda chat_id, telegram_username, internal_username: TradingService(
                resolved_database
            ).bind_telegram_private_chat(
                internal_username=internal_username,
                telegram_username=telegram_username,
                telegram_chat_id=chat_id,
                now=_now(),
            ),
            chat_resolver=lambda user_id: TradingQueries(resolved_database).telegram_chat_id(
                user_id
            ),
            poll_timeout_seconds=resolved_settings.telegram_poll_timeout_seconds,
        )
    else:
        resolved_telegram = MockTelegramGateway()
    resolved_binance = binance_client or (
        BinancePortfolioMarginReadOnlyClient(
            base_url=resolved_settings.binance_live_base_url,
            api_key=resolved_settings.binance_api_key,
            api_secret=resolved_settings.binance_api_secret,
            recv_window_ms=resolved_settings.binance_recv_window_ms,
        )
        if resolved_settings.binance_account_mode == "PORTFOLIO_MARGIN"
        else BinanceReadOnlyClient(
            base_url=resolved_settings.binance_futures_base_url,
            api_key=resolved_settings.binance_api_key,
            api_secret=resolved_settings.binance_api_secret,
            recv_window_ms=resolved_settings.binance_recv_window_ms,
        )
    )
    resolved_binance_live = binance_live_client or BinancePortfolioMarginClient(
        base_url=resolved_settings.binance_live_base_url,
        api_key=resolved_settings.binance_api_key,
        api_secret=resolved_settings.binance_api_secret,
        recv_window_ms=resolved_settings.binance_recv_window_ms,
    )
    resolved_binance_testnet = binance_testnet_client or BinanceTestnetClient(
        base_url=resolved_settings.binance_testnet_base_url,
        api_key=resolved_settings.binance_testnet_api_key,
        api_secret=resolved_settings.binance_testnet_api_secret,
        recv_window_ms=resolved_settings.binance_recv_window_ms,
    )
    resolved_binance_testnet_reader = binance_testnet_reader or BinanceReadOnlyClient(
        base_url=resolved_settings.binance_testnet_base_url,
        api_key=resolved_settings.binance_testnet_api_key,
        api_secret=resolved_settings.binance_testnet_api_secret,
        recv_window_ms=resolved_settings.binance_recv_window_ms,
    )
    resolved_hyperliquid_account = resolved_settings.hyperliquid_account_address
    if (
        resolved_hyperliquid_account is None
        and resolved_settings.hyperliquid_api_wallet_address is not None
        and (
            resolved_settings.hyperliquid_read_only_enabled
            or resolved_settings.hyperliquid_live_order_send_enabled
        )
    ):
        resolved_hyperliquid_account = resolve_hyperliquid_main_account(
            base_url=resolved_settings.hyperliquid_base_url,
            account_address=None,
            api_wallet_address=resolved_settings.hyperliquid_api_wallet_address,
        )
    resolved_hyperliquid = hyperliquid_client or HyperliquidReadOnlyClient(
        base_url=resolved_settings.hyperliquid_base_url,
        account_address=(
            resolved_settings.hyperliquid_subaccount_address or resolved_hyperliquid_account
        ),
        dex=resolved_settings.hyperliquid_core_dex,
    )
    testnet_signer = build_hyperliquid_signer(
        resolved_settings.hyperliquid_testnet_api_wallet_private_key,
        api_wallet_address=None,
        active_pool=resolved_settings.hyperliquid_subaccount_address,
        is_mainnet=False,
    )
    resolved_hyperliquid_testnet = hyperliquid_testnet_client or HyperliquidTestnetClient(
        base_url=resolved_settings.hyperliquid_testnet_base_url,
        account_address=resolved_hyperliquid_account,
        signer=testnet_signer,
        subaccount_address=resolved_settings.hyperliquid_subaccount_address,
        dex=resolved_settings.hyperliquid_core_dex,
    )
    live_signer = build_hyperliquid_signer(
        resolved_settings.hyperliquid_api_wallet_private_key,
        api_wallet_address=resolved_settings.hyperliquid_api_wallet_address,
        active_pool=resolved_settings.hyperliquid_subaccount_address,
        is_mainnet=True,
    )
    resolved_hyperliquid_live = hyperliquid_live_client or HyperliquidLiveClient(
        base_url=resolved_settings.hyperliquid_live_base_url,
        account_address=resolved_hyperliquid_account,
        signer=live_signer,
        subaccount_address=resolved_settings.hyperliquid_subaccount_address,
        dex=resolved_settings.hyperliquid_core_dex,
    )
    resolved_capital_transfer = capital_transfer_adapter or MockCapitalTransferAdapter()
    resolved_notilt = notilt_gateway or NoTiltGateway(
        timeout_seconds=resolved_settings.notilt_gateway_timeout_seconds
    )
    resolved_notilt_valuator = notilt_valuator or NoTiltUsdValuator()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if isinstance(resolved_telegram, TelegramBotGateway):
            resolved_telegram.start()
        try:
            yield
        finally:
            if isinstance(resolved_telegram, TelegramBotGateway):
                resolved_telegram.stop()
            resolved_database.dispose()

    app = FastAPI(
        title="Trading Control Plane",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = resolved_settings
    app.state.database = resolved_database
    app.state.perptape_client = resolved_perptape
    app.state.telegram_gateway = resolved_telegram
    app.state.binance_client = resolved_binance
    app.state.binance_live_client = resolved_binance_live
    app.state.binance_testnet_client = resolved_binance_testnet
    app.state.binance_testnet_reader = resolved_binance_testnet_reader
    app.state.hyperliquid_client = resolved_hyperliquid
    app.state.hyperliquid_live_client = resolved_hyperliquid_live
    app.state.hyperliquid_testnet_client = resolved_hyperliquid_testnet
    app.state.capital_transfer_adapter = resolved_capital_transfer
    app.state.notilt_gateway = resolved_notilt
    app.state.notilt_valuator = resolved_notilt_valuator

    @app.exception_handler(DomainRejected)
    async def domain_rejected(_: Request, exc: DomainRejected) -> JSONResponse:
        return JSONResponse(
            status_code=_domain_status(exc.code),
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.detail,
                    "retryable": exc.code
                    in {
                        "PERPTAPE_UNAVAILABLE",
                        "PERPTAPE_RATE_LIMITED",
                        "PERPTAPE_CACHE_UNAVAILABLE",
                        "PERPTAPE_CACHE_STALE",
                        "BINANCE_READ_ONLY_UNAVAILABLE",
                        "BINANCE_LIVE_UNAVAILABLE",
                        "BINANCE_LIVE_OUTCOME_UNKNOWN",
                        "HYPERLIQUID_READ_ONLY_UNAVAILABLE",
                        "HYPERLIQUID_TESTNET_UNAVAILABLE",
                        "HYPERLIQUID_LIVE_UNAVAILABLE",
                        "HYPERLIQUID_LIVE_OUTCOME_UNKNOWN",
                    },
                }
            },
        )

    def business_database() -> Database:
        if not isinstance(resolved_database, Database):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": {"code": "BUSINESS_API_UNAVAILABLE"}},
            )
        return resolved_database

    def queries() -> TradingQueries:
        return TradingQueries(business_database())

    def service() -> TradingService:
        return TradingService(business_database())

    def configured_risk_scopes() -> tuple[tuple[str, str, str], ...]:
        scopes: set[tuple[str, str, str]] = set()
        if (
            resolved_settings.binance_read_only_enabled
            and resolved_settings.binance_fact_environment == "LIVE"
            and resolved_settings.runtime_binance_account_id
        ):
            scopes.add(("LIVE", resolved_settings.runtime_binance_account_id, "BINANCE"))
        if (
            resolved_settings.hyperliquid_read_only_enabled
            and resolved_settings.hyperliquid_fact_environment == "LIVE"
            and resolved_settings.runtime_hyperliquid_account_id
        ):
            scopes.add(("LIVE", resolved_settings.runtime_hyperliquid_account_id, "HYPERLIQUID"))
        return tuple(sorted(scopes))

    def current_identity(
        trading_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    ) -> SessionIdentity:
        if trading_session is None:
            raise DomainRejected("LOGIN_DENIED", "an internal login session is required")
        identity = token_service.verify_session(trading_session, now=_now())
        queries().user_context(identity.user_id)
        return identity

    identity_dependency = Depends(current_identity)

    @app.get("/health/live", tags=["health"])
    def liveness() -> dict[str, str]:
        return {"status": "live", "version": __version__}

    @app.get("/health/ready", tags=["health"])
    def readiness() -> dict[str, Any]:
        ready, error_code = resolved_database.is_ready()
        DATABASE_READY.set(1 if ready else 0)
        if not ready:
            logger.warning(
                "durable trading core is not ready",
                extra={
                    "event": "readiness_failed",
                    "error_code": error_code or "READINESS_FAILED",
                    "component": "database",
                },
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "status": "not_ready",
                    "error_code": error_code or "READINESS_FAILED",
                },
            )
        return {"status": "ready", "durable_store": "postgresql"}

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/api/auth/status")
    def auth_status() -> dict[str, Any]:
        return {
            "provider": "MANAGED_IDP_PASSKEY",
            "provider_configured": False,
            "mock_identity_available": (
                resolved_settings.allow_mock_identity
                and resolved_settings.environment in {"local", "test"}
            ),
            "environment": resolved_settings.environment,
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

    @app.post("/api/auth/mock/step-up")
    def mock_step_up(
        payload: MockStepUpRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
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
            current_version = queries().transfer_proposal_version(payload.object_id)
            detail = queries().transfer_proposal_detail(identity.user_id, payload.object_id)
            review_action = "capital.review"
        elif payload.action in {"risk.restore.review", "risk.restore.execute"}:
            current_version = service().risk_control_change_version(payload.object_id)
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

    @app.get("/api/instruments")
    def instruments(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return {"data": queries().list_instruments(identity.user_id), "as_of": _now().isoformat()}

    def notify_reviewers(
        proposal_id: UUID, proposal_version: int, environment: str = "SHADOW"
    ) -> None:
        for reviewer in queries().reviewers_for_proposal(proposal_id):
            detail = queries().proposal_detail(reviewer.user_id, proposal_id)
            code = token_service.issue_review_reference(
                user_id=reviewer.user_id,
                object_id=proposal_id,
                object_version=proposal_version,
                now=_now(),
                ttl=timedelta(seconds=resolved_settings.action_token_ttl_seconds),
            )
            review_url = (
                f"{resolved_settings.public_base_url.rstrip('/')}/proposals/{proposal_id}"
                f"?review_code={quote(code)}"
            )
            notification_key = f"{proposal_id}:{proposal_version}:{reviewer.user_id}"
            resolved_telegram.send(
                ProposalNotification(
                    notification_id="tg_"
                    + hashlib.sha256(notification_key.encode()).hexdigest()[:20],
                    reviewer_id=reviewer.user_id,
                    proposal_id=proposal_id,
                    proposal_version=proposal_version,
                    environment=environment,
                    summary="提案正在等待人工审核；完整冻结语义仅在 Web 中展示。",  # noqa: RUF001
                    review_code=code,
                    review_url=review_url,
                    created_at=_now(),
                    status=str(detail["status"]),
                    expires_at=str(detail["expires_at"]),
                )
            )

    def notify_capital(
        *,
        object_id: UUID,
        object_type: str,
        event_type: str,
        environment: str,
        account_id: str,
        venue: str,
        object_version: int,
        summary: str,
    ) -> None:
        for recipient in queries().treasury_users(account_id, venue):
            notification_key = (
                f"{object_type}:{object_id}:{event_type}:{object_version}:{recipient.user_id}"
            )
            resolved_telegram.send_capital(
                CapitalNotification(
                    notification_id="tg_"
                    + hashlib.sha256(notification_key.encode()).hexdigest()[:20],
                    recipient_id=recipient.user_id,
                    object_id=object_id,
                    object_type=object_type,
                    event_type=event_type,
                    environment=environment,
                    summary=summary,
                    object_version=object_version,
                    created_at=_now(),
                )
            )

    def current_perptape_candidates(*, now: datetime) -> list[PerptapeCandidate]:
        if resolved_settings.runtime_sync_enabled:
            feed = queries().perptape_feed()
            if feed is None:
                raise DomainRejected(
                    "PERPTAPE_CACHE_UNAVAILABLE",
                    "runtime sync has not recorded a Perptape feed",
                )
            grace = timedelta(
                seconds=(
                    resolved_settings.runtime_sync_interval_seconds
                    + int(resolved_settings.perptape_timeout_seconds)
                    + 30
                )
            )
            if (
                feed.contract_version != resolved_settings.perptape_contract_version
                or now > feed.next_allowed_at + grace
            ):
                raise DomainRejected(
                    "PERPTAPE_CACHE_STALE",
                    "runtime Perptape feed is stale or uses another contract version",
                )
            return list(feed.candidates)
        return resolved_perptape.list_candidates(now=now)

    def current_perptape_candidate(candidate_id: str, *, now: datetime) -> PerptapeCandidate:
        candidates = current_perptape_candidates(now=now)
        for candidate in candidates:
            if candidate.candidate_id == candidate_id:
                return candidate
        legacy_matches = [
            candidate
            for candidate in candidates
            if perptape_legacy_candidate_id(candidate) == candidate_id
        ]
        if len(legacy_matches) == 1:
            return legacy_matches[0]
        if legacy_matches:
            raise DomainRejected(
                "PERPTAPE_CANDIDATE_AMBIGUOUS",
                "legacy candidate identity matches more than one current contract",
            )
        raise DomainRejected("PERPTAPE_CANDIDATE_NOT_FOUND", "candidate is no longer available")

    @app.get("/api/opportunities")
    def opportunities(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        queries().user_context(identity.user_id)
        now = _now()
        candidates = current_perptape_candidates(now=now)
        active_instruments = queries().active_instrument_keys(
            {(candidate.venue, candidate.symbol) for candidate in candidates}
        )
        data: list[dict[str, Any]] = []
        for candidate in candidates:
            value = candidate.to_dict()
            proposal_eligible = (candidate.venue, candidate.symbol) in active_instruments
            value["proposal_eligible"] = proposal_eligible
            value["proposal_blocker"] = None if proposal_eligible else "INSTRUMENT_UNAVAILABLE"
            data.append(value)
        return {
            "source": "PERPTAPE",
            "source_contract_version": resolved_settings.perptape_contract_version,
            "environment": "SHADOW",
            "as_of": now.isoformat(),
            "data": data,
        }

    @app.post("/api/opportunities/{candidate_id}/proposals")
    def create_system_proposal(
        candidate_id: str,
        payload: SystemProposalRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        candidate = current_perptape_candidate(candidate_id, now=now)
        if candidate.readiness != "READY":
            raise DomainRejected(
                "PERPTAPE_CANDIDATE_NOT_READY", "candidate data is not ready for proposal review"
            )
        if not service().can_user(
            identity.user_id, "proposal.create", payload.account_id, candidate.venue
        ):
            raise DomainRejected("RBAC_DENIED", "proposal creation is outside the current scope")
        principal = queries().service_principal_by_username(
            resolved_settings.perptape_service_username
        )
        instrument_id = queries().instrument_id_by_venue_symbol(candidate.venue, candidate.symbol)
        legacy_candidate_id = perptape_legacy_candidate_id(candidate)
        source_candidate_id = (
            queries().compatible_legacy_system_candidate_id(
                legacy_candidate_id,
                candidate,
                instrument_id,
            )
            or candidate.candidate_id
        )
        proposal_id = service().create_proposal(
            actor_id=principal.user_id,
            source=ProposalSource.SYSTEM,
            risk_tier=payload.risk_tier,
            account_id=payload.account_id,
            venue=candidate.venue,
            instrument_id=instrument_id,
            direction=candidate.direction,
            quantity=payload.quantity,
            max_risk=payload.max_risk,
            expires_at=now + timedelta(minutes=payload.expires_in_minutes),
            idempotency_key=f"perptape:{source_candidate_id}",
            strategy_id="perptape",
            strategy_version=candidate.source_contract_version,
            environment=ExecutionEnvironment(payload.environment),
            source_candidate_id=source_candidate_id,
            source_link=candidate.detail_url,
            source_observed_at=candidate.observed_at,
            source_readiness=candidate.readiness,
            details={
                "candidate": candidate.to_dict(),
                "invalidation_price": str(payload.invalidation_price),
                "initial_quantity": str(
                    payload.quantity
                    if payload.initial_quantity is None
                    else payload.initial_quantity
                ),
                "allow_auto_add": payload.allow_auto_add,
                "requested_adds": payload.requested_adds,
                "add_trigger_price": (
                    None if payload.add_trigger_price is None else str(payload.add_trigger_price)
                ),
                "rationale": payload.rationale,
            },
            idempotency_payload={
                "candidate_id": source_candidate_id,
                "account_id": payload.account_id,
                "risk_tier": payload.risk_tier.value,
                "quantity": str(payload.quantity),
                "initial_quantity": (
                    None if payload.initial_quantity is None else str(payload.initial_quantity)
                ),
                "max_risk": str(payload.max_risk),
                "expires_in_minutes": payload.expires_in_minutes,
                "invalidation_price": str(payload.invalidation_price),
                "allow_auto_add": payload.allow_auto_add,
                "requested_adds": payload.requested_adds,
                "add_trigger_price": (
                    None if payload.add_trigger_price is None else str(payload.add_trigger_price)
                ),
                "rationale": payload.rationale,
            },
            now=now,
        )
        current = queries().proposal_detail(principal.user_id, proposal_id)
        if current["status"] == ProposalStatus.DRAFT.value:
            service().submit_proposal(proposal_id, principal.user_id, now=now)
            current = queries().proposal_detail(identity.user_id, proposal_id)
            notify_reviewers(proposal_id, int(current["version"]), "SHADOW")
        return current

    @app.post("/api/proposals/manual")
    def create_manual_proposal(
        payload: ManualProposalRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        proposal_id = service().create_proposal(
            actor_id=identity.user_id,
            source=ProposalSource.MANUAL,
            risk_tier=payload.risk_tier,
            account_id=payload.account_id,
            venue=payload.venue,
            instrument_id=payload.instrument_id,
            direction=payload.direction,
            quantity=payload.quantity,
            max_risk=payload.max_risk,
            expires_at=now + timedelta(minutes=payload.expires_in_minutes),
            idempotency_key=payload.idempotency_key,
            environment=ExecutionEnvironment(payload.environment),
            details={
                "trigger_price": str(payload.trigger_price),
                "limit_price": None if payload.limit_price is None else str(payload.limit_price),
                "invalidation_price": str(payload.invalidation_price),
                "initial_quantity": str(
                    payload.quantity
                    if payload.initial_quantity is None
                    else payload.initial_quantity
                ),
                "allow_auto_add": payload.allow_auto_add,
                "requested_adds": payload.requested_adds,
                "add_trigger_price": (
                    None if payload.add_trigger_price is None else str(payload.add_trigger_price)
                ),
                "rationale": payload.rationale,
            },
            idempotency_payload={
                "source": "MANUAL",
                "environment": payload.environment,
                "account_id": payload.account_id,
                "venue": payload.venue,
                "instrument_id": str(payload.instrument_id),
                "direction": payload.direction.value,
                "risk_tier": payload.risk_tier.value,
                "quantity": str(payload.quantity),
                "initial_quantity": (
                    None if payload.initial_quantity is None else str(payload.initial_quantity)
                ),
                "max_risk": str(payload.max_risk),
                "expires_in_minutes": payload.expires_in_minutes,
                "trigger_price": str(payload.trigger_price),
                "limit_price": (None if payload.limit_price is None else str(payload.limit_price)),
                "invalidation_price": str(payload.invalidation_price),
                "allow_auto_add": payload.allow_auto_add,
                "requested_adds": payload.requested_adds,
                "add_trigger_price": (
                    None if payload.add_trigger_price is None else str(payload.add_trigger_price)
                ),
                "rationale": payload.rationale,
            },
            now=now,
        )
        current = queries().proposal_detail(identity.user_id, proposal_id)
        if current["status"] == ProposalStatus.DRAFT.value:
            service().submit_proposal(proposal_id, identity.user_id, now=now)
            current = queries().proposal_detail(identity.user_id, proposal_id)
            notify_reviewers(proposal_id, int(current["version"]), payload.environment)
        return current

    @app.get("/api/proposals")
    def proposals(
        identity: SessionIdentity = identity_dependency,
        proposal_status: str | None = None,
    ) -> dict[str, Any]:
        return {
            "data": queries().list_proposals(identity.user_id, status=proposal_status),
            "as_of": _now().isoformat(),
        }

    @app.get("/api/proposals/{proposal_id}")
    def proposal_detail(
        proposal_id: UUID,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return queries().proposal_detail(identity.user_id, proposal_id)

    @app.post("/api/proposals/{proposal_id}/reviews")
    def review_proposal(
        proposal_id: UUID,
        payload: ReviewRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        if payload.decision == "APPROVE":
            if payload.action_grant is None:
                raise DomainRejected(
                    "ACTION_GRANT_REQUIRED", "proposal approval requires action-level step-up"
                )
            token_service.verify_action_grant(
                payload.action_grant,
                user_id=identity.user_id,
                action="proposal.approve",
                object_id=proposal_id,
                object_version=payload.expected_version,
                now=now,
            )
        result = service().review_proposal(
            proposal_id,
            identity.user_id,
            ReviewDecision(payload.decision),
            payload.reason,
            expected_version=payload.expected_version,
            now=now,
        )
        return {
            "proposal_id": str(proposal_id),
            "status": result.value,
            "detail": queries().proposal_detail(identity.user_id, proposal_id),
        }

    @app.post("/api/proposals/{proposal_id}/risk-decisions")
    def decide_risk(
        proposal_id: UUID,
        payload: RiskDecisionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        decision_id = service().decide_risk(
            proposal_id=proposal_id,
            actor_id=identity.user_id,
            kind=IntentKind.INITIAL,
            idempotency_key=payload.idempotency_key,
            requested_quantity=payload.requested_quantity,
            now=_now(),
        )
        return {
            "decision_id": str(decision_id),
            "detail": queries().proposal_detail(identity.user_id, proposal_id),
        }

    @app.post("/api/proposals/{proposal_id}/authorizations")
    def issue_authorization(
        proposal_id: UUID,
        payload: AuthorizationRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        proposal = queries().proposal_detail(identity.user_id, proposal_id)
        proposal_expiry = datetime.fromisoformat(str(proposal["expires_at"]))
        requested_expiry = now + timedelta(minutes=payload.expires_in_minutes)
        expires_at = min(proposal_expiry, requested_expiry)
        authorization_id = service().issue_authorization(
            proposal_id=proposal_id,
            actor_id=identity.user_id,
            expires_at=expires_at,
            allowed_adds=payload.allowed_adds,
            idempotency_key=payload.idempotency_key,
            now=now,
        )
        return {
            "authorization_id": str(authorization_id),
            "detail": queries().proposal_detail(identity.user_id, proposal_id),
        }

    def notify_campaign(
        recipient_id: UUID,
        campaign_id: UUID,
        event_type: str,
        event_key: str,
        summary: str,
        environment: str = "SHADOW",
    ) -> None:
        detail = queries().campaign_detail(recipient_id, campaign_id)
        campaign_version = int(detail["target_version"])
        action_references: list[tuple[str, str]] = []
        if service().can_user(
            recipient_id,
            "risk.tighten",
            str(detail["account_id"]),
            str(detail["venue"]),
        ):
            for action in (
                "DISABLE_CAMPAIGN_AUTO_ADD",
                "EMERGENCY_REDUCE",
                "EXIT",
            ):
                action_references.append(
                    (
                        action,
                        token_service.issue_action_reference(
                            user_id=recipient_id,
                            action=action,
                            object_id=campaign_id,
                            object_version=campaign_version,
                            now=_now(),
                            ttl=timedelta(seconds=resolved_settings.action_token_ttl_seconds),
                        ),
                    )
                )
        if service().can_user(recipient_id, "risk.tighten"):
            action_references.append(
                (
                    "PAUSE_NEW_RISK",
                    token_service.issue_action_reference(
                        user_id=recipient_id,
                        action="PAUSE_NEW_RISK",
                        object_id=campaign_id,
                        object_version=campaign_version,
                        now=_now(),
                        ttl=timedelta(seconds=resolved_settings.action_token_ttl_seconds),
                    ),
                )
            )
        notification_key = f"{campaign_id}:{event_type}:{event_key}:{recipient_id}"
        management = detail["management"]
        resolved_telegram.send_campaign(
            CampaignNotification(
                notification_id="tg_" + hashlib.sha256(notification_key.encode()).hexdigest()[:20],
                recipient_id=recipient_id,
                campaign_id=campaign_id,
                event_type=event_type,
                environment=environment,
                summary=summary,
                campaign_version=campaign_version,
                action_references=tuple(action_references),
                created_at=_now(),
                status=str(detail["status"]),
                auto_add_available=bool(
                    isinstance(management, dict)
                    and management["auto_add_gate"] == "ENABLED"
                    and management["allow_auto_add"] is True
                    and int(management["remaining_adds"]) > 0
                ),
                position_reduction_available=campaign_position_reduction_available(
                    str(detail["status"]),
                    Decimal(str(detail["current_target_quantity"])),
                ),
            )
        )

    @app.get("/api/venues/binance/status")
    def binance_read_only_status(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        del identity
        return {
            "venue": "BINANCE",
            "mode": "USER_DATA_READ_ONLY",
            "enabled": resolved_settings.binance_read_only_enabled,
            "configured": resolved_binance.configured,
            "order_send_available": (
                resolved_settings.binance_live_order_send_enabled
                and resolved_binance_live.configured
            ),
            "account_mode": resolved_settings.binance_account_mode,
            "fact_environment": resolved_settings.binance_fact_environment,
            "environment": resolved_settings.environment,
        }

    def require_binance_live() -> None:
        if not resolved_settings.binance_live_order_send_enabled:
            raise DomainRejected(
                "BINANCE_LIVE_DISABLED", "Binance LIVE order send is explicitly disabled"
            )
        if not resolved_binance_live.configured:
            raise DomainRejected(
                "BINANCE_LIVE_NOT_CONFIGURED",
                "Binance Unified Account credentials are not configured",
            )

    @app.get("/api/venues/binance/live/status")
    def binance_live_status(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        del identity
        return {
            "venue": "BINANCE",
            "environment": "LIVE",
            "account_mode": resolved_settings.binance_account_mode,
            "enabled": resolved_settings.binance_live_order_send_enabled,
            "configured": resolved_binance_live.configured,
            "capability_gate_required": "LIVE_ORDER_SEND",
            "capital_transfer": False,
        }

    def require_binance_testnet() -> None:
        if not resolved_settings.binance_testnet_order_send_enabled:
            raise DomainRejected(
                "BINANCE_TESTNET_DISABLED", "Binance testnet order send is explicitly disabled"
            )
        if not resolved_binance_testnet.configured:
            raise DomainRejected(
                "BINANCE_TESTNET_NOT_CONFIGURED", "Binance testnet credentials are not configured"
            )

    @app.get("/api/venues/binance/testnet/status")
    def binance_testnet_status(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        del identity
        return {
            "venue": "BINANCE",
            "environment": "TESTNET",
            "enabled": resolved_settings.binance_testnet_order_send_enabled,
            "configured": resolved_binance_testnet.configured,
            "live_order_send": False,
            "capital_transfer": False,
        }

    @app.get("/api/venues/binance/facts")
    def binance_read_only_facts(
        account_id: str,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return {
            "mode": "USER_DATA_READ_ONLY",
            "data": queries().venue_facts(
                identity.user_id,
                account_id,
                "BINANCE",
                resolved_settings.binance_fact_environment,
            ),
            "as_of": _now().isoformat(),
        }

    @app.post("/api/venues/binance/sync")
    def sync_binance_read_only_facts(
        payload: BinanceReadOnlySyncRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        if not resolved_settings.binance_read_only_enabled:
            raise DomainRejected(
                "BINANCE_READ_ONLY_DISABLED",
                "Binance USER_DATA read-only synchronization is disabled",
            )
        if not resolved_binance.configured:
            raise DomainRejected(
                "BINANCE_READ_ONLY_NOT_CONFIGURED",
                "Binance read-only credentials are not configured",
            )
        if not service().can_user(identity.user_id, "venue.record", payload.account_id, "BINANCE"):
            raise DomainRejected(
                "RBAC_DENIED", "Binance facts are outside the current operator scope"
            )
        snapshot = resolved_binance.read_snapshot(payload.symbol, now=_now())
        now = _now()
        persisted = service().ingest_binance_read_only_snapshot(
            payload.account_id,
            identity.user_id,
            snapshot,
            environment=ExecutionEnvironment(resolved_settings.binance_fact_environment),
            now=now,
        )
        execution_scope = (
            f"{resolved_settings.binance_fact_environment}:{payload.account_id}:BINANCE"
        )
        reconciliation_id = service().reconcile_scope(
            execution_scope,
            identity.user_id,
            now=now,
        )
        reconciliation_status = service().reconciliation_status(reconciliation_id)
        return {
            "source": "BINANCE_USER_DATA",
            "mode": "READ_ONLY",
            "environment": resolved_settings.binance_fact_environment,
            "symbol": payload.symbol,
            "observed_at": snapshot.observed_at.isoformat(),
            "persisted": persisted,
            "reconciliation": {
                "reconciliation_id": str(reconciliation_id),
                "execution_scope": execution_scope,
                "status": reconciliation_status.value,
            },
            "facts": queries().venue_facts(
                identity.user_id,
                payload.account_id,
                "BINANCE",
                resolved_settings.binance_fact_environment,
            ),
        }

    @app.post("/api/venues/binance/testnet/sync")
    def sync_binance_testnet_facts(
        payload: BinanceReadOnlySyncRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_binance_testnet()
        if not resolved_binance_testnet_reader.configured:
            raise DomainRejected(
                "BINANCE_TESTNET_NOT_CONFIGURED", "Binance testnet facts are not configured"
            )
        if not service().can_user(identity.user_id, "venue.record", payload.account_id, "BINANCE"):
            raise DomainRejected(
                "RBAC_DENIED", "Binance testnet facts are outside the current operator scope"
            )
        snapshot = resolved_binance_testnet_reader.read_snapshot(payload.symbol, now=_now())
        now = _now()
        persisted = service().ingest_binance_read_only_snapshot(
            payload.account_id,
            identity.user_id,
            snapshot,
            environment=ExecutionEnvironment.TESTNET,
            now=now,
        )
        execution_scope = f"TESTNET:{payload.account_id}:BINANCE"
        reconciliation_id = service().reconcile_scope(execution_scope, identity.user_id, now=now)
        return {
            "source": "BINANCE_TESTNET_USER_DATA",
            "environment": "TESTNET",
            "symbol": payload.symbol,
            "persisted": persisted,
            "reconciliation": {
                "reconciliation_id": str(reconciliation_id),
                "status": service().reconciliation_status(reconciliation_id).value,
            },
            "facts": queries().venue_facts(
                identity.user_id, payload.account_id, "BINANCE", "TESTNET"
            ),
        }

    @app.get("/api/venues/hyperliquid/status")
    def hyperliquid_read_only_status(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        del identity
        return {
            "venue": "HYPERLIQUID",
            "domain": "CORE",
            "dex": "",
            "mode": "INFO_READ_ONLY",
            "enabled": resolved_settings.hyperliquid_read_only_enabled,
            "configured": resolved_hyperliquid.configured,
            "order_send_available": (
                resolved_settings.hyperliquid_live_order_send_enabled
                and resolved_hyperliquid_live.configured
            ),
            "fact_environment": resolved_settings.hyperliquid_fact_environment,
            "source_environment": resolved_hyperliquid.fact_environment,
            "hip3_available": False,
            "environment": resolved_settings.environment,
        }

    def require_hyperliquid_live() -> None:
        if not resolved_settings.hyperliquid_live_order_send_enabled:
            raise DomainRejected(
                "HYPERLIQUID_LIVE_DISABLED",
                "Hyperliquid LIVE order send is explicitly disabled",
            )
        if not resolved_hyperliquid_live.configured:
            raise DomainRejected(
                "HYPERLIQUID_LIVE_NOT_CONFIGURED",
                "Hyperliquid LIVE requires the main account and API wallet signer",
            )

    @app.get("/api/venues/hyperliquid/live/status")
    def hyperliquid_live_status(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        del identity
        return {
            "venue": "HYPERLIQUID",
            "domain": "CORE",
            "environment": "LIVE",
            "enabled": resolved_settings.hyperliquid_live_order_send_enabled,
            "configured": resolved_hyperliquid_live.configured,
            "account_scope": resolved_hyperliquid_live.account_scope,
            "capability_gate_required": "LIVE_ORDER_SEND",
            "capital_transfer": False,
            "hip3_available": False,
        }

    @app.get("/api/venues/hyperliquid/testnet/status")
    def hyperliquid_testnet_status(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        del identity
        return {
            "venue": "HYPERLIQUID",
            "domain": "CORE",
            "environment": "TESTNET",
            "enabled": resolved_settings.hyperliquid_testnet_order_send_enabled,
            "configured": resolved_hyperliquid_testnet.configured,
            "signer_source": "INJECTED_RUNTIME_ONLY",
            "live_order_send": False,
            "capital_transfer": False,
            "hip3_available": False,
        }

    def require_hyperliquid_testnet() -> None:
        if not resolved_settings.hyperliquid_testnet_order_send_enabled:
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_DISABLED",
                "Hyperliquid Core testnet order send is explicitly disabled",
            )
        if not resolved_hyperliquid_testnet.configured:
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_NOT_CONFIGURED",
                "Hyperliquid testnet account and injected signer are not configured",
            )

    @app.get("/api/venues/hyperliquid/facts")
    def hyperliquid_read_only_facts(
        account_id: str,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return {
            "mode": "INFO_READ_ONLY",
            "domain": "CORE",
            "data": queries().venue_facts(
                identity.user_id,
                account_id,
                "HYPERLIQUID",
                resolved_settings.hyperliquid_fact_environment,
            ),
            "as_of": _now().isoformat(),
        }

    @app.post("/api/venues/hyperliquid/sync")
    def sync_hyperliquid_read_only_facts(
        payload: HyperliquidReadOnlySyncRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        if not resolved_settings.hyperliquid_read_only_enabled:
            raise DomainRejected(
                "HYPERLIQUID_READ_ONLY_DISABLED",
                "Hyperliquid Core Info synchronization is disabled",
            )
        if not resolved_hyperliquid.configured:
            raise DomainRejected(
                "HYPERLIQUID_READ_ONLY_NOT_CONFIGURED",
                "Hyperliquid main account address is not configured",
            )
        if resolved_hyperliquid.fact_environment != resolved_settings.hyperliquid_fact_environment:
            raise DomainRejected(
                "HYPERLIQUID_ENVIRONMENT_MISMATCH",
                "Hyperliquid API host does not match the configured fact environment",
            )
        if not service().can_user(
            identity.user_id, "venue.record", payload.account_id, "HYPERLIQUID"
        ):
            raise DomainRejected(
                "RBAC_DENIED", "Hyperliquid facts are outside the current operator scope"
            )
        snapshot = resolved_hyperliquid.read_snapshot(payload.symbol, now=_now())
        now = _now()
        environment = ExecutionEnvironment(resolved_settings.hyperliquid_fact_environment)
        persisted = service().ingest_hyperliquid_read_only_snapshot(
            payload.account_id,
            identity.user_id,
            snapshot,
            environment=environment,
            now=now,
        )
        execution_scope = f"{environment.value}:{payload.account_id}:HYPERLIQUID"
        reconciliation_id = service().reconcile_scope(execution_scope, identity.user_id, now=now)
        return {
            "source": "HYPERLIQUID_CORE_INFO",
            "mode": "READ_ONLY",
            "domain": "CORE",
            "environment": environment.value,
            "symbol": payload.symbol,
            "observed_at": snapshot.observed_at.isoformat(),
            "persisted": persisted,
            "reconciliation": {
                "reconciliation_id": str(reconciliation_id),
                "execution_scope": execution_scope,
                "status": service().reconciliation_status(reconciliation_id).value,
            },
            "facts": queries().venue_facts(
                identity.user_id,
                payload.account_id,
                "HYPERLIQUID",
                environment.value,
            ),
        }

    @app.get("/api/campaigns")
    def campaigns(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return {"data": queries().list_campaigns(identity.user_id), "as_of": _now().isoformat()}

    @app.get("/api/campaign-exceptions")
    def campaign_exceptions(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return {
            "data": queries().list_exceptions(identity.user_id),
            "as_of": _now().isoformat(),
        }

    @app.get("/api/campaigns/{campaign_id}")
    def campaign_detail(
        campaign_id: UUID,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return queries().campaign_detail(identity.user_id, campaign_id)

    @app.get("/api/campaigns/{campaign_id}/add-candidates")
    def campaign_add_candidates(
        campaign_id: UUID,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        detail = queries().campaign_detail(identity.user_id, campaign_id)
        symbol = str(detail.get("instrument", {}).get("symbol", ""))
        source_candidate_id = None
        proposal = queries().proposal_detail(identity.user_id, UUID(str(detail["proposal_id"])))
        source_candidate_id = proposal.get("source_candidate_id")
        now = _now()
        candidates = [
            item
            for item in current_perptape_candidates(now=now)
            if item.venue == detail["venue"]
            and item.symbol == symbol
            and item.direction.value == detail["direction"]
            and item.readiness == "READY"
            and source_candidate_id not in {item.candidate_id, perptape_legacy_candidate_id(item)}
        ]
        return {
            "source": "PERPTAPE",
            "source_contract_version": resolved_settings.perptape_contract_version,
            "as_of": now.isoformat(),
            "data": [item.to_dict() for item in candidates],
        }

    @app.post("/api/campaigns/{campaign_id}/auto-add")
    def create_auto_add_intent(
        campaign_id: UUID,
        payload: AutoAddRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        detail = queries().campaign_detail(identity.user_id, campaign_id)
        candidate = current_perptape_candidate(payload.candidate_id, now=now)
        created = service().create_order_intent(
            UUID(str(detail["authorization_id"])),
            identity.user_id,
            IntentKind.ADD,
            str(detail["account_id"]),
            str(detail["venue"]),
            UUID(str(detail["instrument_id"])),
            candidate.direction,
            payload.quantity,
            payload.idempotency_key,
            add_candidate=AddCandidateFacts(
                candidate_id=candidate.candidate_id,
                contract_version=candidate.source_contract_version,
                venue=candidate.venue,
                symbol=candidate.symbol,
                direction=candidate.direction,
                observed_at=candidate.observed_at,
                reference_price=candidate.reference_price,
                readiness=candidate.readiness,
                legacy_candidate_id=perptape_legacy_candidate_id(candidate),
            ),
            now=now,
        )
        notify_campaign(
            identity.user_id,
            created.campaign_id,
            "ADD_INTENT_READY",
            str(created.intent_id),
            "Perptape 加仓候选已通过冻结条件和最终风险校验。",
            str(detail["environment"]),
        )
        return {
            "campaign_id": str(created.campaign_id),
            "reservation_id": str(created.reservation_id),
            "intent_id": str(created.intent_id),
            "detail": queries().campaign_detail(identity.user_id, created.campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/managed-reductions")
    def create_managed_reduction(
        campaign_id: UUID,
        payload: ManagedReductionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        intent_id = service().create_reduction_intent(
            campaign_id,
            identity.user_id,
            payload.idempotency_key,
            candidates=(TargetCandidate(payload.target_quantity, payload.urgency, payload.reason),),
            limit_price=payload.limit_price,
            now=_now(),
        )
        detail = queries().campaign_detail(identity.user_id, campaign_id)
        notify_campaign(
            identity.user_id,
            campaign_id,
            "RISK_REDUCTION_READY",
            str(intent_id),
            "只减仓目标已就绪；完整目标数量仅在 Web 中展示。",  # noqa: RUF001
            str(detail["environment"]),
        )
        return {"intent_id": str(intent_id), "detail": detail}

    @app.post("/api/campaigns/{campaign_id}/automatic-exit")
    def evaluate_automatic_exit(
        campaign_id: UUID,
        payload: AutomaticExitRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        reason, intent_id = service().create_automatic_exit_intent(
            campaign_id,
            identity.user_id,
            payload.idempotency_key,
            limit_price=payload.limit_price,
            now=_now(),
        )
        detail = queries().campaign_detail(identity.user_id, campaign_id)
        if intent_id is not None:
            notify_campaign(
                identity.user_id,
                campaign_id,
                "AUTOMATIC_EXIT_READY",
                str(intent_id),
                "自动只减仓退出意图已就绪；请在 Web 中核对触发原因。",  # noqa: RUF001
                str(detail["environment"]),
            )
        return {
            "triggered": intent_id is not None,
            "reason": reason,
            "intent_id": None if intent_id is None else str(intent_id),
            "detail": detail,
        }

    @app.post("/api/campaigns/{campaign_id}/auto-add/disable")
    def disable_campaign_auto_add(
        campaign_id: UUID,
        payload: RiskTightenRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        allowed_adds = service().disable_campaign_auto_add(
            campaign_id,
            identity.user_id,
            payload.idempotency_key,
            reason=payload.reason,
            now=_now(),
        )
        detail = queries().campaign_detail(identity.user_id, campaign_id)
        notify_campaign(
            identity.user_id,
            campaign_id,
            "CAMPAIGN_AUTO_ADD_DISABLED",
            payload.idempotency_key,
            "此 Campaign 的后续 AddUnit 已关闭。",
            str(detail["environment"]),
        )
        return {"allowed_adds": allowed_adds, "detail": detail}

    @app.post("/api/operations/auto-add/disable")
    def disable_global_auto_add(
        payload: RiskTightenRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, str]:
        service().disable_global_auto_add(
            identity.user_id,
            payload.idempotency_key,
            reason=payload.reason,
            now=_now(),
        )
        return {"status": "DISABLED"}

    @app.post("/api/operations/pause-new-risk")
    def pause_new_risk(
        payload: RiskTightenRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, str]:
        state = service().pause_new_risk(
            identity.user_id,
            payload.idempotency_key,
            reason=payload.reason,
            now=_now(),
        )
        return {"system_state": state.value}

    @app.get("/api/risk-controls")
    def risk_controls(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return service().risk_control_status(
            identity.user_id,
            configured_risk_scopes(),
            require_live_scope=resolved_settings.environment == "production",
            now=_now(),
        )

    @app.post("/api/risk-controls/restores")
    def create_risk_control_restore(
        payload: RiskControlChangeCreateRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        request_id = service().create_risk_control_change_request(
            identity.user_id,
            payload.idempotency_key,
            reason=payload.reason,
            restore_auto_add=payload.restore_auto_add,
            configured_scopes=configured_risk_scopes(),
            require_live_scope=resolved_settings.environment == "production",
            now=_now(),
        )
        return service().risk_control_status(
            identity.user_id,
            configured_risk_scopes(),
            require_live_scope=resolved_settings.environment == "production",
            now=_now(),
        ) | {"request_id": str(request_id)}

    @app.post("/api/risk-controls/restores/{request_id}/reviews")
    def review_risk_control_restore(
        request_id: UUID,
        payload: RiskControlChangeReviewRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        if payload.decision == "APPROVE":
            if payload.action_grant is None:
                raise DomainRejected(
                    "ACTION_GRANT_REQUIRED",
                    "risk restoration approval requires action-level step-up",
                )
            token_service.verify_action_grant(
                payload.action_grant,
                user_id=identity.user_id,
                action="risk.restore.review",
                object_id=request_id,
                object_version=payload.expected_version,
                now=now,
            )
        result = service().review_risk_control_change_request(
            request_id,
            identity.user_id,
            ReviewDecision(payload.decision),
            payload.reason,
            payload.expected_version,
            payload.idempotency_key,
            now=now,
        )
        return {"request_id": str(request_id), "status": result.value}

    @app.post("/api/risk-controls/restores/{request_id}/execute")
    def execute_risk_control_restore(
        request_id: UUID,
        payload: RiskControlChangeExecuteRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        token_service.verify_action_grant(
            payload.action_grant,
            user_id=identity.user_id,
            action="risk.restore.execute",
            object_id=request_id,
            object_version=payload.expected_version,
            now=now,
        )
        policy_id = service().execute_risk_control_change_request(
            request_id,
            identity.user_id,
            payload.expected_version,
            payload.idempotency_key,
            configured_risk_scopes(),
            require_live_scope=resolved_settings.environment == "production",
            now=now,
        )
        return {"request_id": str(request_id), "policy_id": str(policy_id)}

    @app.post("/api/authorizations/{authorization_id}/intents")
    def create_order_intent(
        authorization_id: UUID,
        payload: OrderIntentRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        created = service().create_order_intent(
            authorization_id,
            identity.user_id,
            IntentKind(payload.kind),
            payload.account_id,
            payload.venue,
            payload.instrument_id,
            payload.direction,
            payload.quantity,
            payload.idempotency_key,
            now=_now(),
        )
        return {
            "campaign_id": str(created.campaign_id),
            "reservation_id": str(created.reservation_id),
            "intent_id": str(created.intent_id),
            "detail": queries().campaign_detail(identity.user_id, created.campaign_id),
        }

    @app.post("/api/facts/positions")
    def record_position(
        payload: PositionFactRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, str]:
        position_id = service().record_position(
            payload.account_id,
            payload.venue,
            payload.instrument_id,
            payload.quantity,
            payload.average_entry_price,
            payload.mark_price,
            payload.known,
            identity.user_id,
            now=_now(),
        )
        return {"position_id": str(position_id), "environment": "SHADOW"}

    @app.post("/api/facts/account-equity")
    def record_account_equity(
        payload: AccountEquityFactRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, str]:
        fact_id = service().record_account_equity(
            payload.account_id,
            payload.venue,
            payload.equity,
            payload.available_balance,
            payload.currency,
            payload.known,
            identity.user_id,
            now=_now(),
        )
        return {"account_equity_id": str(fact_id), "environment": "SHADOW"}

    @app.post("/api/sender-leases")
    def acquire_sender(
        payload: SenderLeaseRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        token = service().acquire_sender(
            payload.execution_scope,
            payload.owner_id,
            identity.user_id,
            now,
            lease_duration=timedelta(seconds=payload.lease_seconds),
        )
        return {
            "execution_scope": payload.execution_scope,
            "owner_id": payload.owner_id,
            "fencing_token": token,
            "expires_at": (now + timedelta(seconds=payload.lease_seconds)).isoformat(),
        }

    @app.post("/api/intents/{intent_id}/shadow-send")
    def shadow_send(
        intent_id: UUID,
        payload: ShadowSendRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        fact_id = service().record_shadow_order(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            payload.venue_order_id,
            now=_now(),
        )
        return {
            "venue_order_fact_id": str(fact_id),
            "environment": "SHADOW",
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    def rejected_testnet_order(
        command: BinanceTestnetOrderCommand, now: datetime
    ) -> BinanceTestnetOrder:
        return BinanceTestnetOrder(
            order_id=f"REJECTED:{command.client_order_id}",
            client_order_id=command.client_order_id,
            status="REJECTED",
            side=command.side,
            order_type="MARKET",
            ordered_quantity=command.quantity,
            filled_quantity=Decimal(0),
            stop_price=Decimal(0),
            reduce_only=command.reduce_only,
            close_position=False,
            observed_at=now,
        )

    @app.post("/api/intents/{intent_id}/binance-testnet/send")
    def send_binance_testnet_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_binance_testnet()
        now = _now()
        command = service().prepare_binance_testnet_send(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        try:
            result = resolved_binance_testnet.ensure_order(command, now=now)
        except DomainRejected as exc:
            if exc.code == "BINANCE_TESTNET_REJECTED":
                service().record_binance_testnet_order(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    rejected_testnet_order(command, now),
                    now=now,
                )
            elif exc.code != "BINANCE_TESTNET_UNAVAILABLE":
                service().record_binance_testnet_unknown(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    exc.code,
                    now=now,
                )
            raise
        fact_id = service().record_binance_testnet_order(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            command,
            result,
            now=now,
        )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "venue_order_fact_id": str(fact_id),
            "client_order_id": command.client_order_id,
            "environment": "TESTNET",
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/binance-testnet/cancel")
    def cancel_binance_testnet_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_binance_testnet()
        now = _now()
        command = service().prepare_binance_testnet_cancel(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        try:
            result = resolved_binance_testnet.cancel_order(command, now=now)
        except DomainRejected as exc:
            if exc.code != "BINANCE_TESTNET_UNAVAILABLE":
                service().record_binance_testnet_unknown(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    exc.code,
                    now=now,
                )
            raise
        if result is None:
            service().record_binance_testnet_unknown(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                "BINANCE_TESTNET_ORDER_NOT_FOUND",
                now=now,
            )
        else:
            service().record_binance_testnet_order(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                result,
                now=now,
            )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "environment": "TESTNET",
            "confirmed": result is not None,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/binance-testnet/recover")
    def recover_binance_testnet_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_binance_testnet()
        now = _now()
        command = service().prepare_binance_testnet_recovery(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        result = resolved_binance_testnet.recover_order(command, now=now)
        if result is not None:
            service().record_binance_testnet_order(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                result,
                now=now,
            )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "environment": "TESTNET",
            "recovered": result is not None,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    def unknown_testnet_protection(
        command: BinanceTestnetProtectionCommand, now: datetime
    ) -> BinanceTestnetOrder:
        return BinanceTestnetOrder(
            order_id=f"UNKNOWN:{command.client_order_id}",
            client_order_id=command.client_order_id,
            status="UNKNOWN",
            side=command.side,
            order_type="STOP_MARKET",
            ordered_quantity=Decimal(0),
            filled_quantity=Decimal(0),
            stop_price=command.trigger_price,
            reduce_only=False,
            close_position=True,
            observed_at=now,
        )

    @app.post("/api/campaigns/{campaign_id}/binance-testnet/protection")
    def create_binance_testnet_protection(
        campaign_id: UUID,
        payload: BinanceTestnetProtectionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_binance_testnet()
        now = _now()
        command = service().prepare_binance_testnet_protection(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            payload.trigger_price,
            now=now,
        )
        try:
            result = resolved_binance_testnet.ensure_protection(command, now=now)
        except DomainRejected as exc:
            if exc.code != "BINANCE_TESTNET_UNAVAILABLE":
                service().record_binance_testnet_protection(
                    campaign_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    unknown_testnet_protection(command, now),
                    now=now,
                )
            raise
        protection_id = service().record_binance_testnet_protection(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            command,
            result,
            now=now,
        )
        return {
            "protection_id": str(protection_id),
            "client_order_id": command.client_order_id,
            "environment": "TESTNET",
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    def rejected_hyperliquid_order(
        command: HyperliquidTestnetOrderCommand, now: datetime
    ) -> HyperliquidTestnetOrder:
        return HyperliquidTestnetOrder(
            order_id=f"REJECTED:{command.client_order_id}",
            client_order_id=command.client_order_id,
            status="REJECTED",
            side=command.side,
            order_type="IOC_LIMIT",
            ordered_quantity=command.quantity,
            filled_quantity=Decimal(0),
            limit_price=command.limit_price,
            stop_price=Decimal(0),
            reduce_only=command.reduce_only,
            close_position=False,
            observed_at=now,
        )

    @app.post("/api/intents/{intent_id}/hyperliquid-testnet/send")
    def send_hyperliquid_testnet_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_hyperliquid_testnet()
        now = _now()
        command = service().prepare_hyperliquid_testnet_send(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        try:
            result = resolved_hyperliquid_testnet.ensure_order(command, now=now)
        except DomainRejected as exc:
            if exc.code == "HYPERLIQUID_TESTNET_REJECTED":
                service().record_hyperliquid_testnet_order(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    rejected_hyperliquid_order(command, now),
                    now=now,
                )
            elif exc.code not in {
                "HYPERLIQUID_TESTNET_UNAVAILABLE",
                "HYPERLIQUID_TESTNET_SIGNER_INVALID",
                "HYPERLIQUID_ORDER_PRECISION_INVALID",
            }:
                service().record_hyperliquid_testnet_unknown(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    exc.code,
                    now=now,
                )
            raise
        fact_id = service().record_hyperliquid_testnet_order(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            command,
            result,
            now=now,
        )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "venue_order_fact_id": str(fact_id),
            "client_order_id": command.client_order_id,
            "environment": "TESTNET",
            "domain": "CORE",
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/hyperliquid-testnet/cancel")
    def cancel_hyperliquid_testnet_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_hyperliquid_testnet()
        now = _now()
        command = service().prepare_hyperliquid_testnet_cancel(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        try:
            result = resolved_hyperliquid_testnet.cancel_order(command, now=now)
        except DomainRejected as exc:
            if exc.code not in {
                "HYPERLIQUID_TESTNET_UNAVAILABLE",
                "HYPERLIQUID_TESTNET_SIGNER_INVALID",
            }:
                service().record_hyperliquid_testnet_unknown(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    exc.code,
                    now=now,
                )
            raise
        if result is None:
            service().record_hyperliquid_testnet_unknown(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                "HYPERLIQUID_TESTNET_ORDER_NOT_FOUND",
                now=now,
            )
        else:
            service().record_hyperliquid_testnet_order(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                result,
                now=now,
            )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "environment": "TESTNET",
            "domain": "CORE",
            "confirmed": result is not None,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/hyperliquid-testnet/recover")
    def recover_hyperliquid_testnet_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_hyperliquid_testnet()
        now = _now()
        command = service().prepare_hyperliquid_testnet_recovery(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        result = resolved_hyperliquid_testnet.recover_order(command, now=now)
        if result is not None:
            service().record_hyperliquid_testnet_order(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                result,
                now=now,
            )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "environment": "TESTNET",
            "domain": "CORE",
            "recovered": result is not None,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    def unknown_hyperliquid_protection(
        command: HyperliquidTestnetProtectionCommand, now: datetime
    ) -> HyperliquidTestnetOrder:
        return HyperliquidTestnetOrder(
            order_id=f"UNKNOWN:{command.client_order_id}",
            client_order_id=command.client_order_id,
            status="UNKNOWN",
            side=command.side,
            order_type="TRIGGER_MARKET",
            ordered_quantity=command.quantity,
            filled_quantity=Decimal(0),
            limit_price=command.limit_price,
            stop_price=command.trigger_price,
            reduce_only=True,
            close_position=False,
            observed_at=now,
        )

    @app.post("/api/campaigns/{campaign_id}/hyperliquid-testnet/protection")
    def create_hyperliquid_testnet_protection(
        campaign_id: UUID,
        payload: HyperliquidTestnetProtectionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_hyperliquid_testnet()
        now = _now()
        command = service().prepare_hyperliquid_testnet_protection(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            payload.trigger_price,
            payload.limit_price,
            now=now,
        )
        try:
            result = resolved_hyperliquid_testnet.ensure_protection(command, now=now)
        except DomainRejected as exc:
            if exc.code not in {
                "HYPERLIQUID_TESTNET_UNAVAILABLE",
                "HYPERLIQUID_TESTNET_SIGNER_INVALID",
                "HYPERLIQUID_ORDER_PRECISION_INVALID",
            }:
                service().record_hyperliquid_testnet_protection(
                    campaign_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    unknown_hyperliquid_protection(command, now),
                    now=now,
                )
            raise
        protection_id = service().record_hyperliquid_testnet_protection(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            command,
            result,
            now=now,
        )
        return {
            "protection_id": str(protection_id),
            "client_order_id": command.client_order_id,
            "environment": "TESTNET",
            "domain": "CORE",
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/binance/live/send")
    def send_binance_live_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_binance_live()
        now = _now()
        command = service().prepare_binance_live_send(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        try:
            result = resolved_binance_live.ensure_order(command, now=now)
        except DomainRejected as exc:
            if exc.code == "BINANCE_LIVE_REJECTED":
                service().record_binance_live_order(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    rejected_testnet_order(command, now),
                    now=now,
                )
            elif exc.code == "BINANCE_LIVE_OUTCOME_UNKNOWN":
                service().record_binance_live_unknown(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    exc.code,
                    now=now,
                )
            raise
        fact_id = service().record_binance_live_order(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            command,
            result,
            now=now,
        )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "venue_order_fact_id": str(fact_id),
            "client_order_id": command.client_order_id,
            "environment": "LIVE",
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/binance/live/cancel")
    def cancel_binance_live_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_binance_live()
        now = _now()
        command = service().prepare_binance_live_cancel(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        try:
            result = resolved_binance_live.cancel_order(command, now=now)
        except DomainRejected as exc:
            if exc.code == "BINANCE_LIVE_OUTCOME_UNKNOWN":
                service().record_binance_live_unknown(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    exc.code,
                    now=now,
                )
            raise
        if result is None:
            service().record_binance_live_unknown(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                "BINANCE_LIVE_ORDER_NOT_FOUND",
                now=now,
            )
        else:
            service().record_binance_live_order(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                result,
                now=now,
            )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "environment": "LIVE",
            "confirmed": result is not None,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/binance/live/recover")
    def recover_binance_live_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_binance_live()
        now = _now()
        command = service().prepare_binance_live_recovery(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        result = resolved_binance_live.recover_order(command, now=now)
        if result is not None:
            service().record_binance_live_order(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                result,
                now=now,
            )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "environment": "LIVE",
            "recovered": result is not None,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/binance/live/protection")
    def create_binance_live_protection(
        campaign_id: UUID,
        payload: BinanceTestnetProtectionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_binance_live()
        now = _now()
        command = service().prepare_binance_live_protection(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            payload.trigger_price,
            now=now,
        )
        try:
            result = resolved_binance_live.ensure_protection(command, now=now)
        except DomainRejected as exc:
            if exc.code == "BINANCE_LIVE_OUTCOME_UNKNOWN":
                unknown = BinanceTestnetOrder(
                    order_id=f"UNKNOWN:{command.client_order_id}",
                    client_order_id=command.client_order_id,
                    status="UNKNOWN",
                    side=command.side,
                    order_type="STOP_MARKET",
                    ordered_quantity=command.quantity or Decimal(0),
                    filled_quantity=Decimal(0),
                    stop_price=command.trigger_price,
                    reduce_only=True,
                    close_position=False,
                    observed_at=now,
                )
                service().record_binance_live_protection(
                    campaign_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    unknown,
                    now=now,
                )
            raise
        protection_id = service().record_binance_live_protection(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            command,
            result,
            now=now,
        )
        return {
            "protection_id": str(protection_id),
            "client_order_id": command.client_order_id,
            "environment": "LIVE",
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/binance/live/protection/cancel")
    def cancel_binance_live_protection(
        campaign_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_binance_live()
        now = _now()
        command = service().prepare_live_protection_cancel(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            venue="BINANCE",
            now=now,
        )
        result = resolved_binance_live.cancel_protection(command, now=now)
        service().record_live_protection_cancel(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            command,
            result,
            venue="BINANCE",
            now=now,
        )
        return {
            "environment": "LIVE",
            "confirmed": True,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/hyperliquid/live/send")
    def send_hyperliquid_live_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_hyperliquid_live()
        now = _now()
        command = service().prepare_hyperliquid_live_send(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        try:
            result = resolved_hyperliquid_live.ensure_order(command, now=now)
        except DomainRejected as exc:
            if exc.code == "HYPERLIQUID_LIVE_REJECTED":
                service().record_hyperliquid_live_order(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    rejected_hyperliquid_order(command, now),
                    now=now,
                )
            elif exc.code == "HYPERLIQUID_LIVE_OUTCOME_UNKNOWN":
                service().record_hyperliquid_live_unknown(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    exc.code,
                    now=now,
                )
            raise
        fact_id = service().record_hyperliquid_live_order(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            command,
            result,
            now=now,
        )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "venue_order_fact_id": str(fact_id),
            "client_order_id": command.client_order_id,
            "environment": "LIVE",
            "domain": "CORE",
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/hyperliquid/live/cancel")
    def cancel_hyperliquid_live_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_hyperliquid_live()
        now = _now()
        command = service().prepare_hyperliquid_live_cancel(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        try:
            result = resolved_hyperliquid_live.cancel_order(command, now=now)
        except DomainRejected as exc:
            if exc.code == "HYPERLIQUID_LIVE_OUTCOME_UNKNOWN":
                service().record_hyperliquid_live_unknown(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    exc.code,
                    now=now,
                )
            raise
        if result is None:
            service().record_hyperliquid_live_unknown(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                "HYPERLIQUID_LIVE_ORDER_NOT_FOUND",
                now=now,
            )
        else:
            service().record_hyperliquid_live_order(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                result,
                now=now,
            )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "environment": "LIVE",
            "domain": "CORE",
            "confirmed": result is not None,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/hyperliquid/live/recover")
    def recover_hyperliquid_live_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_hyperliquid_live()
        now = _now()
        command = service().prepare_hyperliquid_live_recovery(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        result = resolved_hyperliquid_live.recover_order(command, now=now)
        if result is not None:
            service().record_hyperliquid_live_order(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                result,
                now=now,
            )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "environment": "LIVE",
            "domain": "CORE",
            "recovered": result is not None,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/hyperliquid/live/protection")
    def create_hyperliquid_live_protection(
        campaign_id: UUID,
        payload: HyperliquidTestnetProtectionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_hyperliquid_live()
        now = _now()
        command = service().prepare_hyperliquid_live_protection(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            payload.trigger_price,
            payload.limit_price,
            now=now,
        )
        try:
            result = resolved_hyperliquid_live.ensure_protection(command, now=now)
        except DomainRejected as exc:
            if exc.code == "HYPERLIQUID_LIVE_OUTCOME_UNKNOWN":
                service().record_hyperliquid_live_protection(
                    campaign_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    unknown_hyperliquid_protection(command, now),
                    now=now,
                )
            raise
        protection_id = service().record_hyperliquid_live_protection(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            command,
            result,
            now=now,
        )
        return {
            "protection_id": str(protection_id),
            "client_order_id": command.client_order_id,
            "environment": "LIVE",
            "domain": "CORE",
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/hyperliquid/live/protection/cancel")
    def cancel_hyperliquid_live_protection(
        campaign_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_hyperliquid_live()
        now = _now()
        command = service().prepare_live_protection_cancel(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            venue="HYPERLIQUID",
            now=now,
        )
        result = resolved_hyperliquid_live.cancel_protection(command, now=now)
        service().record_live_protection_cancel(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            command,
            result,
            venue="HYPERLIQUID",
            now=now,
        )
        return {
            "environment": "LIVE",
            "domain": "CORE",
            "confirmed": True,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/fills")
    def record_fill(
        intent_id: UUID,
        payload: ShadowFillRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        fact_id = service().record_fill(
            intent_id,
            identity.user_id,
            payload.venue_fill_id,
            payload.side,
            payload.quantity,
            payload.price,
            payload.fee,
            payload.fee_currency,
            payload.slippage_cost,
            now=_now(),
        )
        notify_campaign(
            identity.user_id,
            campaign_id,
            "SHADOW_FILL_RECORDED",
            payload.venue_fill_id,
            "SHADOW 成交事实已记录；没有向交易场所发送订单。",  # noqa: RUF001
        )
        return {
            "venue_fill_fact_id": str(fact_id),
            "environment": "SHADOW",
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/unknown")
    def mark_intent_unknown(
        intent_id: UUID,
        payload: IntentUnknownRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        service().mark_intent_unknown(intent_id, identity.user_id, payload.reason, now=_now())
        notify_campaign(
            identity.user_id,
            campaign_id,
            "ORDER_INTENT_UNKNOWN",
            str(intent_id),
            "订单结果为 UNKNOWN；风险占用保持，自动重试已阻止。",  # noqa: RUF001
        )
        return queries().campaign_detail(identity.user_id, campaign_id)

    @app.post("/api/intents/{intent_id}/release")
    def release_intent(
        intent_id: UUID,
        payload: IntentReleaseRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        service().release_unfilled_intent(
            intent_id,
            identity.user_id,
            OrderIntentStatus(payload.terminal_status),
            payload.reason,
            now=_now(),
        )
        return queries().campaign_detail(identity.user_id, campaign_id)

    @app.post("/api/campaigns/{campaign_id}/protection")
    def record_protection(
        campaign_id: UUID,
        payload: ProtectionFactRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        queries().campaign_detail(identity.user_id, campaign_id)
        protection_id = service().record_protection(
            payload.position_id,
            payload.venue_order_id,
            payload.quantity,
            payload.trigger_price,
            payload.fully_covered,
            identity.user_id,
            known=payload.known,
            now=_now(),
        )
        event_type = (
            "PROTECTION_ACTIVE"
            if payload.known and payload.fully_covered
            else "PROTECTION_EXCEPTION"
        )
        notify_campaign(
            identity.user_id,
            campaign_id,
            event_type,
            f"{payload.position_id}:{payload.venue_order_id}",
            "SHADOW 仓位保护事实已记录。",
        )
        return {
            "protection_id": str(protection_id),
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/funding")
    def record_funding(
        campaign_id: UUID,
        payload: FundingFactRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        payment_id = service().record_funding(
            campaign_id,
            payload.venue,
            payload.venue_payment_id,
            payload.amount,
            payload.currency,
            identity.user_id,
            now=_now(),
        )
        return {
            "funding_payment_id": str(payment_id),
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/target")
    def update_campaign_target(
        campaign_id: UUID,
        payload: CampaignTargetRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        decision = service().update_campaign_target(
            campaign_id,
            identity.user_id,
            tuple(
                TargetCandidate(item.target_quantity, item.urgency, item.reason)
                for item in payload.candidates
            ),
            now=_now(),
        )
        return {
            "decision": {
                "target_quantity": str(decision.target_quantity),
                "urgency": decision.urgency.value,
                "reasons": list(decision.reasons),
            },
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/reduction-intents")
    def create_reduction_intent(
        campaign_id: UUID,
        payload: ReductionIntentRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        intent_id = service().create_reduction_intent(
            campaign_id,
            identity.user_id,
            payload.idempotency_key,
            limit_price=payload.limit_price,
            now=_now(),
        )
        return {
            "intent_id": str(intent_id),
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/reconcile")
    def reconcile_campaign(
        campaign_id: UUID,
        payload: ReconciliationRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        reconciliation_id = service().reconcile_campaign(
            campaign_id, payload.execution_scope, identity.user_id, now=_now()
        )
        detail = queries().campaign_detail(identity.user_id, campaign_id)
        reconciliation = detail["reconciliation"]
        if reconciliation is not None and reconciliation["status"] != "MATCH":
            notify_campaign(
                identity.user_id,
                campaign_id,
                "RECONCILIATION_EXCEPTION",
                str(reconciliation_id),
                f"对账需要人工关注；当前状态为 {reconciliation['status']}。",  # noqa: RUF001
            )
        return {"reconciliation_id": str(reconciliation_id), "detail": detail}

    @app.post("/api/reconciliations/{reconciliation_id}/manual")
    def require_manual_reconciliation(
        reconciliation_id: UUID,
        payload: ReconciliationReasonRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, str]:
        result = service().require_manual_reconciliation(
            reconciliation_id, identity.user_id, payload.reason, now=_now()
        )
        return {"reconciliation_id": str(result), "status": "MANUAL_REQUIRED"}

    @app.post("/api/reconciliations/{reconciliation_id}/resolve")
    def resolve_reconciliation(
        reconciliation_id: UUID,
        payload: ReconciliationReasonRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, str]:
        result = service().resolve_reconciliation(
            reconciliation_id, identity.user_id, payload.reason, now=_now()
        )
        return {"reconciliation_id": str(result), "status": "RESOLVED"}

    @app.post("/api/campaigns/{campaign_id}/pnl")
    def refresh_campaign_pnl(
        campaign_id: UUID,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        pnl = service().refresh_campaign_pnl(campaign_id, identity.user_id, now=_now())
        return {
            "pnl": {
                "realized_pnl": str(pnl.realized_pnl),
                "unrealized_pnl": str(pnl.unrealized_pnl),
                "fees": str(pnl.fees),
                "funding": str(pnl.funding),
                "slippage": str(pnl.slippage),
                "total_pnl": str(pnl.total_pnl),
                "open_quantity": str(pnl.open_quantity),
                "average_entry_price": str(pnl.average_entry_price),
            },
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/close")
    def close_campaign(
        campaign_id: UUID,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        service().close_campaign(campaign_id, identity.user_id, now=_now())
        notify_campaign(
            identity.user_id,
            campaign_id,
            "CAMPAIGN_CLOSED",
            str(campaign_id),
            "仓位归零且对账 MATCH，SHADOW Campaign 已关闭。",  # noqa: RUF001
        )
        return queries().campaign_detail(identity.user_id, campaign_id)

    def configured_notilt_scope(chain_id: int) -> tuple[str, str]:
        if chain_id not in SUPPORTED_NOTILT_CHAINS:
            raise DomainRejected(
                "NOTILT_CHAIN_UNSUPPORTED",
                "NoTilt only supports Ethereum, BNB Smart Chain, and Arbitrum One",
            )
        if not resolved_settings.notilt_enabled:
            raise DomainRejected("NOTILT_DISABLED", "NoTilt read-only integration is disabled")
        agent = resolved_settings.notilt_agent_address
        vault = resolved_settings.notilt_vaults.get(chain_id)
        if agent is None:
            raise DomainRejected(
                "NOTILT_NOT_CONFIGURED",
                "NoTilt public whitelist agent address is not configured",
            )
        if vault is None:
            raise DomainRejected(
                "NOTILT_VAULT_NOT_CONFIGURED",
                f"NoTilt {SUPPORTED_NOTILT_CHAINS[chain_id]} Vault is not configured",
            )
        return agent, vault

    def notilt_chain_id_for_network(network: str) -> int:
        normalized = network.upper().replace("-", "_").replace(" ", "_")
        chain_id = {
            "ETH": 1,
            "ETHEREUM": 1,
            "BNB": 56,
            "BSC": 56,
            "BNB_SMART_CHAIN": 56,
            "ARB": 42161,
            "ARBITRUM": 42161,
            "ARBITRUM_ONE": 42161,
        }.get(normalized)
        if chain_id is None:
            raise DomainRejected(
                "NOTILT_CHAIN_UNSUPPORTED",
                "NoTilt network must be Ethereum, BNB Smart Chain, or Arbitrum One",
            )
        return chain_id

    def sync_configured_notilt_vault(
        chain_id: int,
        actor_id: UUID,
        *,
        now: datetime,
    ) -> tuple[int, dict[str, Any]]:
        agent, vault = configured_notilt_scope(chain_id)
        snapshot = resolved_notilt.read_vault(chain_id, vault, agent)
        valuations = {
            budget.asset: resolved_notilt_valuator.value(
                budget.asset,
                budget.balance,
                now=now,
            )
            for budget in snapshot.budgets
        }
        fact_ids = service().record_notilt_vault_snapshot(
            actor_id=actor_id,
            snapshot=snapshot,
            valuations=valuations,
            now=now,
        )
        return len(fact_ids), queries().capital_center(actor_id)

    @app.get("/api/notilt/status")
    def notilt_status(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        queries().capital_center(identity.user_id)
        return {
            "enabled": resolved_settings.notilt_enabled,
            "gateway_available": resolved_notilt.available,
            "signing_mode": "EXTERNAL_WALLET_ONLY",
            "credential_custody": "EXTERNAL_WALLET",
            "chains": [
                {
                    "chain_id": chain_id,
                    "chain": chain,
                    "vault_configured": chain_id in resolved_settings.notilt_vaults,
                }
                for chain_id, chain in SUPPORTED_NOTILT_CHAINS.items()
            ],
        }

    @app.get("/api/notilt/chains/{chain_id}/assignment")
    def notilt_assignment(
        chain_id: int,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        queries().capital_center(identity.user_id)
        if not resolved_settings.notilt_enabled or resolved_settings.notilt_agent_address is None:
            raise DomainRejected(
                "NOTILT_NOT_CONFIGURED",
                "NoTilt public whitelist agent address is not configured",
            )
        assigned_vault, active = resolved_notilt.resolve_assignment(
            chain_id, resolved_settings.notilt_agent_address
        )
        configured_vault = resolved_settings.notilt_vaults.get(chain_id)
        return {
            "chain_id": chain_id,
            "chain": SUPPORTED_NOTILT_CHAINS.get(chain_id),
            "active": active,
            "matches_configured_vault": (
                configured_vault is not None and assigned_vault.lower() == configured_vault.lower()
            ),
            "configured_vault": configured_vault is not None,
        }

    @app.post("/api/notilt/chains/{chain_id}/sync")
    def sync_notilt_vault(
        chain_id: int,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        fact_count, capital = sync_configured_notilt_vault(
            chain_id,
            identity.user_id,
            now=now,
        )
        return {
            "transport": "NOTILT_OFFICIAL_SDK_READ_ONLY",
            "chain_id": chain_id,
            "facts_recorded": fact_count,
            "data": capital,
        }

    @app.get("/api/capital")
    def capital_center(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return {"data": queries().capital_center(identity.user_id), "as_of": _now().isoformat()}

    @app.get("/api/results")
    def actual_results(
        environment: str = Query(default="SHADOW", pattern="^(SHADOW|TESTNET|LIVE)$"),
        source: str | None = Query(default=None, pattern="^(SYSTEM|MANUAL)$"),
        source_type: str | None = Query(default=None, min_length=1, max_length=120),
        source_candidate_id: str | None = Query(default=None, min_length=1, max_length=160),
        source_version: str | None = Query(default=None, min_length=1, max_length=120),
        venue: str | None = Query(default=None, min_length=1, max_length=64),
        account_id: str | None = Query(default=None, min_length=1, max_length=120),
        instrument_id: UUID | None = None,
        direction: str | None = Query(default=None, pattern="^(LONG|SHORT)$"),
        risk_tier: str | None = Query(default=None, pattern="^(LOW|MEDIUM|HIGH)$"),
        campaign_id: UUID | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return {
            "data": queries().actual_results(
                identity.user_id,
                environment,
                source=source,
                source_type=source_type,
                source_candidate_id=source_candidate_id,
                source_version=source_version,
                venue=venue,
                account_id=account_id,
                instrument_id=instrument_id,
                direction=direction,
                risk_tier=risk_tier,
                campaign_id=campaign_id,
                from_time=from_time,
                to_time=to_time,
            ),
            "as_of": _now().isoformat(),
        }

    @app.get("/api/audit")
    def audit_timeline(
        environment: str = Query(default="SHADOW", pattern="^(SHADOW|TESTNET|LIVE)$"),
        limit: int = Query(default=200, ge=1, le=500),
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return {
            "environment": environment,
            "data": queries().audit_timeline(identity.user_id, environment, limit=limit),
            "as_of": _now().isoformat(),
        }

    @app.get("/api/runtime/status")
    def runtime_status(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        snapshot = queries().runtime_snapshot(identity.user_id)
        snapshot.update(
            {
                "application_version": __version__,
                "runtime_environment": resolved_settings.environment,
                "process_model": (
                    "FastAPI plus independent read-only sync worker and PostgreSQL"
                    if resolved_settings.runtime_sync_enabled
                    else "one FastAPI process plus PostgreSQL"
                ),
                "external_boundaries": {
                    "runtime_sync": {
                        "enabled": resolved_settings.runtime_sync_enabled,
                        "interval_seconds": resolved_settings.runtime_sync_interval_seconds,
                        "binance_target_configured": bool(
                            resolved_settings.runtime_binance_account_id
                        ),
                        "hyperliquid_target_configured": bool(
                            resolved_settings.runtime_hyperliquid_account_id
                        ),
                        "order_send_supported": False,
                        "capital_broadcast_supported": False,
                    },
                    "perptape": {
                        "configured": bool(resolved_settings.perptape_api_key),
                        "mode": "READ_ONLY",
                    },
                    "binance_read_only": {
                        "enabled": resolved_settings.binance_read_only_enabled,
                        "configured": bool(
                            resolved_settings.binance_api_key
                            and resolved_settings.binance_api_secret
                        ),
                        "fact_environment": resolved_settings.binance_fact_environment,
                    },
                    "binance_testnet_send": {
                        "enabled": resolved_settings.binance_testnet_order_send_enabled,
                        "configured": bool(
                            resolved_settings.binance_testnet_api_key
                            and resolved_settings.binance_testnet_api_secret
                        ),
                    },
                    "hyperliquid_read_only": {
                        "enabled": resolved_settings.hyperliquid_read_only_enabled,
                        "configured": resolved_hyperliquid.configured,
                        "account_scope": resolved_settings.hyperliquid_account_scope,
                        "fact_environment": resolved_settings.hyperliquid_fact_environment,
                    },
                    "hyperliquid_testnet_send": {
                        "enabled": resolved_settings.hyperliquid_testnet_order_send_enabled,
                        "signer_injected": resolved_hyperliquid_testnet.configured,
                        "account_scope": resolved_settings.hyperliquid_account_scope,
                    },
                    "notilt": {
                        "mode": "OFFICIAL_SDK_UNSIGNED_HANDOFF",
                        "enabled": resolved_settings.notilt_enabled,
                        "gateway_available": resolved_notilt.available,
                        "configured_chains": sorted(resolved_settings.notilt_vaults),
                        "receipt_verification": True,
                        "min_confirmations": resolved_settings.notilt_min_confirmations,
                        "credential_custody": "EXTERNAL_WALLET",
                        "broadcast_supported": False,
                    },
                    "capital_transfer": {
                        "mode": "MOCK_OR_NOTILT_UNSIGNED_HANDOFF",
                        "real_configured": (
                            resolved_settings.notilt_enabled
                            and bool(resolved_settings.notilt_vaults)
                        ),
                    },
                    "telegram": {
                        "mode": (
                            "BOT_API_LONG_POLLING"
                            if isinstance(resolved_telegram, TelegramBotGateway)
                            else "MOCK_ONLY"
                        ),
                        "enabled": resolved_settings.telegram_enabled,
                        "network_configured": bool(resolved_settings.telegram_bot_token),
                        "private_chat_only": True,
                    },
                },
            }
        )
        return {"data": snapshot, "as_of": _now().isoformat()}

    @app.post("/api/capital/balances/mock")
    def record_mock_capital_balance(
        payload: CapitalBalanceFactRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        if resolved_settings.environment not in {"local", "test"}:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        fact_id = service().record_capital_balance(
            actor_id=identity.user_id,
            environment=ExecutionEnvironment(payload.environment),
            location_type=payload.location_type,
            location_id=payload.location_id,
            venue=payload.venue,
            equity=payload.equity,
            available_balance=payload.available_balance,
            withdrawable_balance=payload.withdrawable_balance,
            asset=payload.asset,
            control_status=payload.control_status,
            deposit_status=payload.deposit_status,
            network=payload.network,
            address_reference=payload.address_reference,
            known=payload.known,
            observed_at=_now(),
            now=_now(),
        )
        return {
            "transport": "MOCK_READ_ONLY_FACT",
            "account_equity_id": str(fact_id),
            "data": queries().capital_center(identity.user_id),
        }

    @app.post("/api/capital/reconciliations")
    def reconcile_capital_scope(
        payload: CapitalScopeReconciliationRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, str]:
        reconciliation_id = service().record_capital_scope_reconciliation(
            actor_id=identity.user_id,
            environment=ExecutionEnvironment(payload.environment),
            account_id=payload.account_id,
            venue=payload.venue,
            now=_now(),
        )
        return {"reconciliation_id": str(reconciliation_id)}

    @app.post("/api/capital/automation/policies")
    def set_capital_automation_policy(
        payload: CapitalAutomationPolicyRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        policy_id = service().set_capital_automation_policy(
            actor_id=identity.user_id,
            environment=ExecutionEnvironment(payload.environment),
            account_id=payload.account_id,
            venue=payload.venue,
            vault_id=payload.vault_id,
            asset=payload.asset,
            network=payload.network,
            vault_destination_reference=payload.vault_destination_reference,
            venue_destination_reference=payload.venue_destination_reference,
            operating_low=payload.operating_low,
            operating_target=payload.operating_target,
            operating_high=payload.operating_high,
            vault_minimum_reserve=payload.vault_minimum_reserve,
            minimum_transfer=payload.minimum_transfer,
            maximum_transfer=payload.maximum_transfer,
            max_fee=payload.max_fee,
            idempotency_key=payload.idempotency_key,
            now=_now(),
        )
        return {
            "policy_id": str(policy_id),
            "data": queries().capital_center(identity.user_id),
        }

    @app.post("/api/capital/automation/policies/{policy_id}/evaluate")
    def evaluate_capital_automation_policy(
        policy_id: UUID,
        payload: CapitalAutomationEvaluateRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        proposal_id, reason = service().create_capital_automation_candidate(
            policy_id,
            payload.purpose,
            identity.user_id,
            payload.idempotency_key,
            now=_now(),
        )
        if proposal_id is not None:
            detail = queries().transfer_proposal_detail(identity.user_id, proposal_id)
            notify_capital(
                object_id=proposal_id,
                object_type="TransferProposal",
                event_type="PENDING_REVIEW",
                environment=str(detail["environment"]),
                account_id=str(detail["account_id"]),
                venue=str(detail["venue"]),
                object_version=int(detail["version"]),
                summary="资金候选需要两名独立 Treasury Reviewer 审核。",
            )
        return {
            "transfer_proposal_id": None if proposal_id is None else str(proposal_id),
            "reason": reason,
            "data": queries().capital_center(identity.user_id),
        }

    @app.post("/api/capital/proposals")
    def create_transfer_proposal(
        payload: TransferProposalRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        environment = ExecutionEnvironment(payload.environment)
        allow_live_unsigned = False
        if environment is ExecutionEnvironment.LIVE:
            chain_id = notilt_chain_id_for_network(payload.network)
            _, configured_vault = configured_notilt_scope(chain_id)
            if payload.vault_id.lower() != configured_vault.lower():
                raise DomainRejected(
                    "NOTILT_VAULT_SCOPE_MISMATCH",
                    "LIVE transfer proposal must use the configured Vault for its chain",
                )
            allow_live_unsigned = True
        proposal_id = service().create_transfer_proposal(
            actor_id=identity.user_id,
            environment=environment,
            direction=CapitalDirection(payload.direction),
            account_id=payload.account_id,
            venue=payload.venue,
            vault_id=payload.vault_id,
            asset=payload.asset,
            network=payload.network,
            destination_reference=payload.destination_reference,
            amount=payload.amount,
            max_fee=payload.max_fee,
            min_received=payload.min_received,
            reason=payload.reason,
            expires_at=now + timedelta(minutes=payload.expires_in_minutes),
            idempotency_key=payload.idempotency_key,
            now=now,
            allow_live_unsigned=allow_live_unsigned,
        )
        return queries().transfer_proposal_detail(identity.user_id, proposal_id)

    @app.post("/api/capital/proposals/{transfer_proposal_id}/submit")
    def submit_transfer_proposal(
        transfer_proposal_id: UUID,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        service().submit_transfer_proposal(transfer_proposal_id, identity.user_id, now=_now())
        detail = queries().transfer_proposal_detail(identity.user_id, transfer_proposal_id)
        notify_capital(
            object_id=transfer_proposal_id,
            object_type="TransferProposal",
            event_type="PENDING_REVIEW",
            environment=str(detail["environment"]),
            account_id=str(detail["account_id"]),
            venue=str(detail["venue"]),
            object_version=int(detail["version"]),
            summary="资金划转提案需要两名独立 Treasury Reviewer 审核。",
        )
        return detail

    @app.post("/api/capital/proposals/{transfer_proposal_id}/reviews")
    def review_transfer_proposal(
        transfer_proposal_id: UUID,
        payload: TransferReviewRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        if payload.decision == "APPROVE":
            if payload.action_grant is None:
                raise DomainRejected(
                    "ACTION_GRANT_REQUIRED", "capital approval requires action-level step-up"
                )
            token_service.verify_action_grant(
                payload.action_grant,
                user_id=identity.user_id,
                action="capital.approve",
                object_id=transfer_proposal_id,
                object_version=payload.expected_version,
                now=now,
            )
        service().review_transfer_proposal(
            transfer_proposal_id,
            identity.user_id,
            ReviewDecision(payload.decision),
            payload.reason,
            payload.expected_version,
            now=now,
        )
        detail = queries().transfer_proposal_detail(identity.user_id, transfer_proposal_id)
        notify_capital(
            object_id=transfer_proposal_id,
            object_type="TransferProposal",
            event_type=f"REVIEW_{payload.decision}",
            environment=str(detail["environment"]),
            account_id=str(detail["account_id"]),
            venue=str(detail["venue"]),
            object_version=int(detail["version"]),
            summary=f"资金划转审核结果已记录：{payload.decision}。",  # noqa: RUF001
        )
        return detail

    @app.post("/api/capital/proposals/{transfer_proposal_id}/authorizations")
    def issue_transfer_authorization(
        transfer_proposal_id: UUID,
        payload: TransferAuthorizationRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        proposal = queries().transfer_proposal_detail(identity.user_id, transfer_proposal_id)
        expires_at = min(
            datetime.fromisoformat(str(proposal["expires_at"])),
            now + timedelta(minutes=payload.expires_in_minutes),
        )
        authorization_id = service().issue_transfer_authorization(
            transfer_proposal_id,
            identity.user_id,
            expires_at,
            payload.idempotency_key,
            now=now,
        )
        return {
            "transfer_authorization_id": str(authorization_id),
            "detail": queries().transfer_proposal_detail(identity.user_id, transfer_proposal_id),
        }

    @app.post("/api/capital/authorizations/{transfer_authorization_id}/transfers/mock")
    def submit_mock_capital_transfer(
        transfer_authorization_id: UUID,
        payload: CapitalTransferCreateRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        if resolved_settings.environment not in {"local", "test"}:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        now = _now()
        transfer_id = service().reserve_capital_transfer(
            transfer_authorization_id,
            identity.user_id,
            payload.idempotency_key,
            now=now,
        )
        detail = queries().capital_transfer_detail(identity.user_id, transfer_id)
        if detail["status"] == CapitalTransferStatus.SOURCE_RESERVED.value:
            command = service().capital_transfer_command(transfer_id, identity.user_id, now=now)
            submission = resolved_capital_transfer.submit(command, now=now)
            service().record_capital_submission(transfer_id, identity.user_id, submission, now=now)
            detail = queries().capital_transfer_detail(identity.user_id, transfer_id)
        notify_capital(
            object_id=transfer_id,
            object_type="CapitalTransfer",
            event_type=str(detail["status"]),
            environment=str(detail["environment"]),
            account_id=str(detail["account_id"]),
            venue=str(detail["venue"]),
            object_version=int(detail["version"]),
            summary="Mock 资金划转已提交；没有移动真实资金。",  # noqa: RUF001
        )
        return {"transport": "MOCK_ONLY", "detail": detail}

    @app.post("/api/capital/authorizations/{transfer_authorization_id}/transfers/notilt-plan")
    def prepare_notilt_capital_transfer(
        transfer_authorization_id: UUID,
        payload: CapitalTransferCreateRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        transfer_id = service().reserve_capital_transfer(
            transfer_authorization_id,
            identity.user_id,
            payload.idempotency_key,
            now=now,
            allow_live_unsigned=True,
        )
        existing = queries().capital_transfer_detail(identity.user_id, transfer_id)
        if (
            existing["transport_state"]
            in {
                "DEPOSIT_PLAN_READY",
                "RELEASE_REQUEST_PLAN_READY",
            }
            and existing["planned_transactions"]
        ):
            return {
                "transport": "NOTILT_UNSIGNED_TRANSACTION_HANDOFF",
                "broadcast": False,
                "signing": "EXTERNAL_WALLET_REQUIRED",
                "capital_transfer_id": str(transfer_id),
                "reserved_gross_amount": existing["gross_amount"],
                "planned_net_amount": str(
                    service().notilt_transfer_command(transfer_id, identity.user_id).min_received
                ),
                "transactions": existing["planned_transactions"],
                "next_step": (
                    "Confirm the exact persisted transaction plan in the independent wallet."
                ),
                "detail": existing,
            }
        command = service().capital_transfer_command(
            transfer_id,
            identity.user_id,
            now=now,
        )
        if command.environment is not ExecutionEnvironment.LIVE:
            raise DomainRejected(
                "NOTILT_TRANSFER_ENVIRONMENT_INVALID",
                "NoTilt transaction plans are only available for LIVE authorizations",
            )
        chain_id = notilt_chain_id_for_network(command.network)
        agent, vault = configured_notilt_scope(chain_id)
        vault_endpoint = (
            command.source_id
            if command.direction is CapitalDirection.VAULT_TO_VENUE
            else command.destination_id
        )
        if vault_endpoint.lower() != vault.lower():
            raise DomainRejected(
                "NOTILT_VAULT_SCOPE_MISMATCH",
                "capital authorization does not reference the configured Vault",
            )
        transactions: tuple[NoTiltUnsignedTransaction, ...]
        if command.direction is CapitalDirection.VAULT_TO_VENUE:
            transactions = (
                resolved_notilt.prepare_release_request(
                    chain_id=chain_id,
                    vault=vault,
                    agent=agent,
                    asset=command.asset,
                    amount=str(command.min_received),
                ),
            )
            plan_state = "RELEASE_REQUEST_PLAN_READY"
            next_step = (
                "Confirm the release request in the independent wallet, wait for the "
                "protocol release window, then prepare and confirm release execution."
            )
        else:
            transactions = resolved_notilt.prepare_deposit(
                chain_id=chain_id,
                vault=vault,
                agent=agent,
                asset=command.asset,
                amount=str(command.min_received),
            )
            plan_state = "DEPOSIT_PLAN_READY"
            next_step = (
                "Funds must already be present in the independent wallet after the "
                "venue withdrawal; confirm each unsigned deposit transaction there."
            )
        service().record_notilt_plan(
            transfer_id,
            identity.user_id,
            chain_id=chain_id,
            transport_state=plan_state,
            transactions=transactions,
            now=now,
        )
        detail = queries().capital_transfer_detail(identity.user_id, transfer_id)
        return {
            "transport": "NOTILT_UNSIGNED_TRANSACTION_HANDOFF",
            "broadcast": False,
            "signing": "EXTERNAL_WALLET_REQUIRED",
            "capital_transfer_id": str(transfer_id),
            "reserved_gross_amount": str(command.gross_amount),
            "planned_net_amount": str(command.min_received),
            "transactions": detail["planned_transactions"],
            "next_step": next_step,
            "detail": detail,
        }

    @app.post("/api/capital/transfers/{capital_transfer_id}/notilt-release-execution-plan")
    def prepare_notilt_release_execution(
        capital_transfer_id: UUID,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        detail = queries().capital_transfer_detail(identity.user_id, capital_transfer_id)
        if detail["transport_state"] == "RELEASE_EXECUTION_PLAN_READY":
            return {
                "transport": "NOTILT_UNSIGNED_TRANSACTION_HANDOFF",
                "broadcast": False,
                "signing": "EXTERNAL_WALLET_REQUIRED",
                "transactions": detail["planned_transactions"],
                "detail": detail,
            }
        if (
            detail["transport_state"] != "RELEASE_REQUEST_CONFIRMED"
            or detail["status"] != CapitalTransferStatus.IN_FLIGHT.value
            or detail["protocol_request_id"] is None
        ):
            raise DomainRejected(
                "NOTILT_RELEASE_NOT_EXECUTABLE",
                "verified release request is not ready for execution",
            )
        execute_after = datetime.fromisoformat(str(detail["protocol_execute_after"]))
        expires_at = datetime.fromisoformat(str(detail["protocol_expires_at"]))
        if now < execute_after:
            raise DomainRejected(
                "NOTILT_RELEASE_NOT_UNLOCKED",
                f"NoTilt release unlocks at {execute_after.isoformat()}",
            )
        if now >= expires_at:
            raise DomainRejected("NOTILT_RELEASE_EXPIRED", "NoTilt release request expired")
        command = service().notilt_transfer_command(capital_transfer_id, identity.user_id)
        chain_id = notilt_chain_id_for_network(command.network)
        agent, vault = configured_notilt_scope(chain_id)
        transaction = resolved_notilt.prepare_release_execution(
            chain_id=chain_id,
            vault=vault,
            agent=agent,
            request_id=str(detail["protocol_request_id"]),
        )
        service().record_notilt_plan(
            capital_transfer_id,
            identity.user_id,
            chain_id=chain_id,
            transport_state="RELEASE_EXECUTION_PLAN_READY",
            transactions=(transaction,),
            now=now,
        )
        updated = queries().capital_transfer_detail(identity.user_id, capital_transfer_id)
        return {
            "transport": "NOTILT_UNSIGNED_TRANSACTION_HANDOFF",
            "broadcast": False,
            "signing": "EXTERNAL_WALLET_REQUIRED",
            "transactions": updated["planned_transactions"],
            "detail": updated,
        }

    @app.post("/api/capital/transfers/{capital_transfer_id}/notilt-release-cancellation-plan")
    def prepare_notilt_release_cancellation(
        capital_transfer_id: UUID,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        detail = queries().capital_transfer_detail(identity.user_id, capital_transfer_id)
        if detail["transport_state"] == "RELEASE_CANCELLATION_PLAN_READY":
            return {
                "transport": "NOTILT_UNSIGNED_TRANSACTION_HANDOFF",
                "broadcast": False,
                "signing": "EXTERNAL_WALLET_REQUIRED",
                "transactions": detail["planned_transactions"],
                "detail": detail,
            }
        if (
            detail["transport_state"] != "RELEASE_REQUEST_CONFIRMED"
            or detail["protocol_request_id"] is None
            or detail["status"]
            not in {
                CapitalTransferStatus.IN_FLIGHT.value,
                CapitalTransferStatus.MANUAL_REQUIRED.value,
            }
        ):
            raise DomainRejected(
                "NOTILT_RELEASE_NOT_CANCELLABLE",
                "verified release request is not available for cancellation",
            )
        command = service().notilt_transfer_command(capital_transfer_id, identity.user_id)
        chain_id = notilt_chain_id_for_network(command.network)
        agent, vault = configured_notilt_scope(chain_id)
        transaction = resolved_notilt.prepare_release_cancellation(
            chain_id=chain_id,
            vault=vault,
            agent=agent,
            request_id=str(detail["protocol_request_id"]),
        )
        service().record_notilt_plan(
            capital_transfer_id,
            identity.user_id,
            chain_id=chain_id,
            transport_state="RELEASE_CANCELLATION_PLAN_READY",
            transactions=(transaction,),
            now=now,
        )
        updated = queries().capital_transfer_detail(identity.user_id, capital_transfer_id)
        return {
            "transport": "NOTILT_UNSIGNED_TRANSACTION_HANDOFF",
            "broadcast": False,
            "signing": "EXTERNAL_WALLET_REQUIRED",
            "transactions": updated["planned_transactions"],
            "detail": updated,
        }

    @app.post("/api/capital/transfers/{capital_transfer_id}/notilt-receipt")
    def verify_notilt_capital_receipt(
        capital_transfer_id: UUID,
        payload: NoTiltReceiptRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        detail = queries().capital_transfer_detail(identity.user_id, capital_transfer_id)
        receipt_kind = {
            "DEPOSIT_PLAN_READY": "DEPOSIT",
            "RELEASE_REQUEST_PLAN_READY": "RELEASE_REQUEST",
            "RELEASE_EXECUTION_PLAN_READY": "RELEASE_EXECUTION",
            "RELEASE_CANCELLATION_PLAN_READY": "RELEASE_CANCELLATION",
        }.get(str(detail["transport_state"]))
        if receipt_kind is None:
            if payload.transaction_hash in detail["confirmed_transaction_hashes"]:
                return {
                    "transport": "NOTILT_VERIFIED_RECEIPT",
                    "idempotent": True,
                    "detail": detail,
                }
            raise DomainRejected(
                "NOTILT_RECEIPT_STATE_INVALID",
                "capital transfer is not waiting for a NoTilt receipt",
            )
        command = service().notilt_transfer_command(capital_transfer_id, identity.user_id)
        chain_id = notilt_chain_id_for_network(command.network)
        agent, vault = configured_notilt_scope(chain_id)
        receipt = resolved_notilt.verify_receipt(
            chain_id=chain_id,
            vault=vault,
            agent=agent,
            receipt_kind=receipt_kind,
            transaction_hash=payload.transaction_hash,
            min_confirmations=resolved_settings.notilt_min_confirmations[chain_id],
            asset=command.asset if receipt_kind in {"DEPOSIT", "RELEASE_REQUEST"} else None,
            amount=(
                str(command.min_received)
                if receipt_kind in {"DEPOSIT", "RELEASE_REQUEST"}
                else None
            ),
            request_id=(
                str(detail["protocol_request_id"])
                if receipt_kind in {"RELEASE_EXECUTION", "RELEASE_CANCELLATION"}
                else None
            ),
        )
        transport_state = service().record_notilt_receipt(
            capital_transfer_id,
            identity.user_id,
            receipt,
            now=now,
        )
        vault_sync: dict[str, Any] = {"attempted": False}
        if receipt_kind in {"DEPOSIT", "RELEASE_EXECUTION"}:
            vault_sync = {"attempted": True}
            try:
                fact_count, _ = sync_configured_notilt_vault(
                    chain_id,
                    identity.user_id,
                    now=now,
                )
                vault_sync.update({"status": "SYNCED", "facts_recorded": fact_count})
            except DomainRejected as exc:
                vault_sync.update({"status": "FAILED", "error_code": exc.code})
        updated = queries().capital_transfer_detail(identity.user_id, capital_transfer_id)
        notify_capital(
            object_id=capital_transfer_id,
            object_type="CapitalTransfer",
            event_type=f"NOTILT_{receipt_kind}_CONFIRMED",
            environment=str(updated["environment"]),
            account_id=str(updated["account_id"]),
            venue=str(updated["venue"]),
            object_version=int(updated["version"]),
            summary=f"NoTilt 回执已验证；协议状态为 {transport_state}。",  # noqa: RUF001
        )
        return {
            "transport": "NOTILT_VERIFIED_RECEIPT",
            "idempotent": False,
            "receipt": {
                "kind": receipt.receipt_kind,
                "chain_id": receipt.chain_id,
                "transaction_hash": receipt.transaction_hash,
                "block_number": receipt.block_number,
                "block_timestamp": receipt.block_timestamp.isoformat(),
                "confirmations": receipt.confirmations,
            },
            "vault_sync": vault_sync,
            "detail": updated,
        }

    @app.get("/api/capital/transfers/{capital_transfer_id}")
    def capital_transfer_detail(
        capital_transfer_id: UUID,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return queries().capital_transfer_detail(identity.user_id, capital_transfer_id)

    @app.post("/api/capital/transfers/{capital_transfer_id}/observations/mock")
    def observe_mock_capital_transfer(
        capital_transfer_id: UUID,
        payload: CapitalTransferObservationRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        if resolved_settings.environment not in {"local", "test"}:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        service().record_capital_observation(
            capital_transfer_id,
            identity.user_id,
            CapitalTransferStatus(payload.status),
            transaction_reference=payload.transaction_reference,
            fee_amount=payload.fee_amount,
            net_received=payload.net_received,
            now=_now(),
        )
        detail = queries().capital_transfer_detail(identity.user_id, capital_transfer_id)
        notify_capital(
            object_id=capital_transfer_id,
            object_type="CapitalTransfer",
            event_type=str(detail["status"]),
            environment=str(detail["environment"]),
            account_id=str(detail["account_id"]),
            venue=str(detail["venue"]),
            object_version=int(detail["version"]),
            summary=f"资金划转状态已变更为 {detail['status']}。",
        )
        return {"transport": "MOCK_ONLY", "detail": detail}

    @app.post("/api/capital/transfers/{capital_transfer_id}/reconcile")
    def reconcile_capital_transfer(
        capital_transfer_id: UUID,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        result = service().reconcile_capital_transfer(
            capital_transfer_id, identity.user_id, now=_now()
        )
        detail = queries().capital_transfer_detail(identity.user_id, capital_transfer_id)
        notify_capital(
            object_id=capital_transfer_id,
            object_type="CapitalTransfer",
            event_type=f"RECONCILIATION_{result}",
            environment=str(detail["environment"]),
            account_id=str(detail["account_id"]),
            venue=str(detail["venue"]),
            object_version=int(detail["version"]),
            summary=f"资金对账结果为 {result}。",
        )
        return {"reconciliation_status": result, "detail": detail}

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
        campaign_data = [
            {
                "notification_id": item.notification_id,
                "campaign_id": str(item.campaign_id),
                "event_type": item.event_type,
                "environment": item.environment,
                "summary": item.summary,
                "campaign_version": item.campaign_version,
                "action_references": dict(item.action_references),
                "created_at": item.created_at.isoformat(),
            }
            for item in resolved_telegram.campaign_notifications()
            if item.recipient_id == identity.user_id
        ]
        capital_data = [
            {
                "notification_id": item.notification_id,
                "object_id": str(item.object_id),
                "object_type": item.object_type,
                "event_type": item.event_type,
                "environment": item.environment,
                "summary": item.summary,
                "object_version": item.object_version,
                "created_at": item.created_at.isoformat(),
            }
            for item in resolved_telegram.capital_notifications()
            if item.recipient_id == identity.user_id
        ]
        return {
            "transport": "MOCK_ONLY",
            "data": data,
            "campaign_data": campaign_data,
            "capital_data": capital_data,
        }

    def execute_telegram_campaign_action(
        *,
        recipient_id: UUID,
        campaign_id: UUID,
        action: str,
        action_reference: str,
        campaign_version: int,
        idempotency_key: str,
        target_quantity: Decimal | None = None,
        limit_price: Decimal | None = None,
    ) -> UUID:
        now = _now()
        token_service.verify_action_reference(
            action_reference,
            user_id=recipient_id,
            action=action,
            object_id=campaign_id,
            object_version=campaign_version,
            now=now,
        )
        if action == "DISABLE_CAMPAIGN_AUTO_ADD":
            service().disable_campaign_auto_add(
                campaign_id,
                recipient_id,
                idempotency_key,
                reason="Telegram operator disabled further Campaign AddUnits",
                expected_target_version=campaign_version,
                now=now,
            )
        elif action == "PAUSE_NEW_RISK":
            service().pause_new_risk(
                recipient_id,
                idempotency_key,
                reason="Telegram operator paused new risk",
                now=now,
            )
        else:
            target = Decimal(0) if action == "EXIT" else target_quantity
            if target is None:
                raise DomainRejected(
                    "TELEGRAM_ACTION_INVALID",
                    "emergency reduction requires a predefined target quantity",
                )
            service().create_reduction_intent(
                campaign_id,
                recipient_id,
                idempotency_key,
                candidates=(
                    TargetCandidate(
                        target,
                        TargetUrgency.IMMEDIATE,
                        f"TELEGRAM_{action}",
                    ),
                ),
                expected_target_version=campaign_version,
                limit_price=limit_price,
                now=now,
            )
        return campaign_id

    def handle_real_telegram_action(action: TelegramCampaignAction, update_id: int) -> str:
        target: Decimal | None = None
        if action.action == "EMERGENCY_REDUCE":
            detail = queries().campaign_detail(action.recipient_id, action.campaign_id)
            target = Decimal(str(detail["current_target_quantity"])) / Decimal(2)
        try:
            execute_telegram_campaign_action(
                recipient_id=action.recipient_id,
                campaign_id=action.campaign_id,
                action=action.action,
                action_reference=action.action_reference,
                campaign_version=action.campaign_version,
                idempotency_key=f"telegram:{update_id}:{action.callback_key}",
                target_quantity=target,
            )
        except DomainRejected as exc:
            return f"未执行: {exc.code}"
        return "Trading 已受理；请在 Web 控制台确认最新权威状态。"  # noqa: RUF001

    if isinstance(resolved_telegram, TelegramBotGateway):
        resolved_telegram.set_action_handler(handle_real_telegram_action)

    @app.post("/api/telegram/mock/campaign-actions")
    def mock_telegram_campaign_action(
        payload: TelegramCampaignActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        if resolved_settings.environment not in {"local", "test"}:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        campaign_id: UUID | None = None
        for notification in resolved_telegram.campaign_notifications():
            references = dict(notification.action_references)
            if (
                notification.recipient_id == identity.user_id
                and references.get(payload.action) == payload.action_reference
            ):
                campaign_id = notification.campaign_id
                break
        if campaign_id is None:
            raise DomainRejected(
                "ACTION_REFERENCE_SCOPE_INVALID",
                "Telegram action reference is not bound to this internal user",
            )
        execute_telegram_campaign_action(
            recipient_id=identity.user_id,
            campaign_id=campaign_id,
            action=payload.action,
            action_reference=payload.action_reference,
            campaign_version=payload.campaign_version,
            idempotency_key=payload.idempotency_key,
            target_quantity=payload.target_quantity,
            limit_price=payload.limit_price,
        )
        return {
            "channel": "TELEGRAM_MOCK_ONLY",
            "action": payload.action,
            "campaign_id": str(campaign_id),
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    if WEB_ROOT.exists():
        app.mount("/assets", StaticFiles(directory=WEB_ROOT), name="web-assets")

        @app.get("/manifest.webmanifest", include_in_schema=False)
        def manifest() -> FileResponse:
            return FileResponse(WEB_ROOT / "manifest.webmanifest")

        @app.get("/sw.js", include_in_schema=False)
        def service_worker() -> FileResponse:
            return FileResponse(WEB_ROOT / "sw.js", media_type="application/javascript")

        @app.get("/", include_in_schema=False)
        @app.get("/opportunities", include_in_schema=False)
        @app.get("/proposals/new", include_in_schema=False)
        @app.get("/reviews", include_in_schema=False)
        @app.get("/campaigns", include_in_schema=False)
        @app.get("/positions", include_in_schema=False)
        @app.get("/orders", include_in_schema=False)
        @app.get("/risk", include_in_schema=False)
        @app.get("/exceptions", include_in_schema=False)
        @app.get("/capital", include_in_schema=False)
        @app.get("/results", include_in_schema=False)
        @app.get("/venues/binance", include_in_schema=False)
        @app.get("/proposals/{proposal_id}", include_in_schema=False)
        @app.get("/campaigns/{campaign_id}", include_in_schema=False)
        def web_app(proposal_id: str | None = None, campaign_id: str | None = None) -> FileResponse:
            del proposal_id
            del campaign_id
            return FileResponse(WEB_ROOT / "index.html")

    return app
