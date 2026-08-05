from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
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

from trading_control_plane import __version__
from trading_control_plane.api_schemas import (
    AccountEquityFactRequest,
    AdminDirectApproveRequest,
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
    DirectCapitalConfigurationRequest,
    DirectCapitalOperationRequest,
    DirectCapitalUnsignedPlanRequest,
    FundingFactRequest,
    HyperliquidReadOnlySyncRequest,
    HyperliquidTestnetProtectionRequest,
    IntentReleaseRequest,
    IntentUnknownRequest,
    ManagedReductionRequest,
    ManagedUserAccessRequest,
    ManagedUserCreateRequest,
    ManualProposalRequest,
    MockLoginRequest,
    MockStepUpRequest,
    NoTiltReceiptRequest,
    OrderIntentRequest,
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
    RiskTightenRequest,
    SenderLeaseRequest,
    ShadowFillRequest,
    ShadowSendRequest,
    SystemProposalRequest,
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
from trading_control_plane.capital import MockCapitalTransferAdapter, build_direct_capital_plan
from trading_control_plane.config import Settings, get_settings
from trading_control_plane.connections import project_runtime_connections
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    AddCandidateFacts,
    CapitalDirection,
    CapitalTransferStatus,
    DirectCapitalPath,
    DomainRejected,
    ExecutionEnvironment,
    IntentKind,
    OrderIntentStatus,
    ProposalSource,
    ProposalStatus,
    ReviewDecision,
    Role,
    TargetCandidate,
)
from trading_control_plane.freqtrade import (
    FreqtradeEntryCommand,
    FreqtradeExitCommand,
    FreqtradeWorkerClient,
    FreqtradeWorkerSpec,
    freqtrade_pair,
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
    perptape_candidate_identity_is_displayable,
    perptape_legacy_candidate_id,
)
from trading_control_plane.queries import TradingQueries
from trading_control_plane.safe_spending import SafeSpendingGateway
from trading_control_plane.service import TradingService
from trading_control_plane.telegram import (
    CampaignNotification,
    CapitalNotification,
    MockTelegramGateway,
    ProposalNotification,
    TelegramBotGateway,
    TelegramGateway,
    TelegramProposalReviewAction,
)

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
) -> str:
    if not settings.perptape_api_key:
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
        "SELF_ACCESS_CHANGE_DENIED",
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
        "USERNAME_CONFLICT",
        "LAST_SYSTEM_ADMIN_REQUIRED",
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
        "DEFAULT_ACCOUNT_NOT_CONFIGURED",
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
    freqtrade_workers: tuple[FreqtradeWorkerClient, ...] | None = None,
    capital_transfer_adapter: MockCapitalTransferAdapter | None = None,
    notilt_gateway: NoTiltGateway | None = None,
    notilt_valuator: NoTiltUsdValuator | None = None,
    safe_spending_gateway: SafeSpendingGateway | None = None,
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

    def telegram_review_todos(chat_id: str) -> list[ProposalNotification]:
        if not isinstance(resolved_database, Database):
            return []
        query = TradingQueries(resolved_database)
        reviewer_id = query.telegram_user_id(chat_id)
        if reviewer_id is None:
            return []
        now = _now()
        todos: list[ProposalNotification] = []
        for item in query.list_proposals(
            reviewer_id,
            status=ProposalStatus.PENDING_REVIEW.value,
            now=now,
        ):
            if datetime.fromisoformat(str(item["expires_at"])) <= now:
                continue
            if not item["actionable_for_current_user"]:
                continue
            proposal_id = UUID(str(item["proposal_id"]))
            todos.append(
                ProposalNotification(
                    notification_id=f"todo:{proposal_id}:{item['version']}",
                    reviewer_id=reviewer_id,
                    proposal_id=proposal_id,
                    proposal_version=int(item["version"]),
                    environment=str(item["environment"]),
                    summary="冻结提案等待你的独立判断。",
                    review_code="",
                    review_url=(
                        f"{resolved_settings.public_base_url.rstrip('/')}/proposals/{proposal_id}"
                    ),
                    created_at=now,
                    status=str(item["status"]),
                    expires_at=str(item["expires_at"]),
                    symbol=None if item["symbol"] is None else str(item["symbol"]),
                    direction=str(item["direction"]),
                    risk_tier=str(item["risk_tier"]),
                    quantity=str(item["quantity"]),
                    max_risk=str(item["max_risk"]),
                )
            )
        return todos

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
            todo_resolver=telegram_review_todos,
            review_queue_url=f"{resolved_settings.public_base_url.rstrip('/')}/reviews",
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
    direct_execution_enabled = resolved_settings.execution_backend == "DIRECT_LEGACY"
    resolved_binance_live = binance_live_client or BinancePortfolioMarginClient(
        base_url=resolved_settings.binance_live_base_url,
        api_key=(resolved_settings.binance_api_key if direct_execution_enabled else None),
        api_secret=(resolved_settings.binance_api_secret if direct_execution_enabled else None),
        recv_window_ms=resolved_settings.binance_recv_window_ms,
    )
    resolved_binance_testnet = binance_testnet_client or BinanceTestnetClient(
        base_url=resolved_settings.binance_testnet_base_url,
        api_key=(resolved_settings.binance_testnet_api_key if direct_execution_enabled else None),
        api_secret=(
            resolved_settings.binance_testnet_api_secret if direct_execution_enabled else None
        ),
        recv_window_ms=resolved_settings.binance_recv_window_ms,
    )
    resolved_binance_testnet_reader = binance_testnet_reader or BinanceReadOnlyClient(
        base_url=resolved_settings.binance_testnet_base_url,
        api_key=resolved_settings.binance_testnet_api_key,
        api_secret=resolved_settings.binance_testnet_api_secret,
        recv_window_ms=resolved_settings.binance_recv_window_ms,
    )
    resolved_hyperliquid = hyperliquid_client or HyperliquidReadOnlyClient(
        base_url=resolved_settings.hyperliquid_base_url,
        account_address=(
            resolved_settings.hyperliquid_subaccount_address
            or resolved_settings.hyperliquid_account_address
        ),
        api_wallet_address=(
            resolved_settings.hyperliquid_api_wallet_address
            if resolved_settings.hyperliquid_read_only_enabled
            else None
        ),
        dex=resolved_settings.hyperliquid_core_dex,
        hip3_dexes=resolved_settings.hyperliquid_hip3_dexes,
    )
    testnet_signer = (
        build_hyperliquid_signer(
            resolved_settings.hyperliquid_testnet_api_wallet_private_key,
            api_wallet_address=None,
            active_pool=resolved_settings.hyperliquid_subaccount_address,
            is_mainnet=False,
        )
        if direct_execution_enabled
        else None
    )
    resolved_hyperliquid_testnet = hyperliquid_testnet_client or HyperliquidTestnetClient(
        base_url=resolved_settings.hyperliquid_testnet_base_url,
        account_address=resolved_settings.hyperliquid_account_address,
        signer=testnet_signer,
        subaccount_address=resolved_settings.hyperliquid_subaccount_address,
        dex=resolved_settings.hyperliquid_core_dex,
    )
    live_signer = (
        build_hyperliquid_signer(
            resolved_settings.hyperliquid_api_wallet_private_key,
            api_wallet_address=resolved_settings.hyperliquid_api_wallet_address,
            active_pool=resolved_settings.hyperliquid_subaccount_address,
            is_mainnet=True,
        )
        if direct_execution_enabled
        else None
    )
    resolved_hyperliquid_live_account = resolved_settings.hyperliquid_account_address
    if (
        direct_execution_enabled
        and resolved_hyperliquid_live_account is None
        and resolved_settings.hyperliquid_api_wallet_address is not None
    ):
        resolved_hyperliquid_live_account = resolve_hyperliquid_main_account(
            base_url=resolved_settings.hyperliquid_base_url,
            account_address=None,
            api_wallet_address=resolved_settings.hyperliquid_api_wallet_address,
        )
    resolved_hyperliquid_live = hyperliquid_live_client or HyperliquidLiveClient(
        base_url=resolved_settings.hyperliquid_live_base_url,
        account_address=resolved_hyperliquid_live_account,
        signer=live_signer,
        subaccount_address=resolved_settings.hyperliquid_subaccount_address,
        dex=resolved_settings.hyperliquid_core_dex,
    )
    resolved_freqtrade_workers = freqtrade_workers or (
        FreqtradeWorkerClient(
            FreqtradeWorkerSpec(
                name="binance-default",
                venue="BINANCE",
                base_url=resolved_settings.freqtrade_binance_worker_url,
                username=resolved_settings.freqtrade_api_username,
                password=resolved_settings.freqtrade_api_password,
            ),
            timeout_seconds=resolved_settings.freqtrade_timeout_seconds,
            confirmation_timeout_seconds=(resolved_settings.freqtrade_confirmation_timeout_seconds),
        ),
        FreqtradeWorkerClient(
            FreqtradeWorkerSpec(
                name="hyperliquid-default",
                venue="HYPERLIQUID",
                base_url=resolved_settings.freqtrade_hyperliquid_worker_url,
                username=resolved_settings.freqtrade_api_username,
                password=resolved_settings.freqtrade_api_password,
                hip3_dexes=resolved_settings.hyperliquid_hip3_dexes,
            ),
            timeout_seconds=resolved_settings.freqtrade_timeout_seconds,
            confirmation_timeout_seconds=(resolved_settings.freqtrade_confirmation_timeout_seconds),
        ),
    )
    resolved_capital_transfer = capital_transfer_adapter or MockCapitalTransferAdapter()
    resolved_notilt = notilt_gateway or NoTiltGateway(
        timeout_seconds=resolved_settings.notilt_gateway_timeout_seconds
    )
    resolved_notilt_valuator = notilt_valuator or NoTiltUsdValuator()
    resolved_safe_spending = safe_spending_gateway or SafeSpendingGateway(
        timeout_seconds=resolved_settings.safe_spending_gateway_timeout_seconds
    )

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
    app.state.freqtrade_workers = resolved_freqtrade_workers
    app.state.capital_transfer_adapter = resolved_capital_transfer
    app.state.notilt_gateway = resolved_notilt
    app.state.notilt_valuator = resolved_notilt_valuator
    app.state.safe_spending_gateway = resolved_safe_spending

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

    def authoritative_live_accounts() -> dict[str, str]:
        return {
            venue: account_id
            for venue, account_id in (
                (
                    "BINANCE",
                    resolved_settings.runtime_binance_account_id,
                ),
                (
                    "HYPERLIQUID",
                    resolved_settings.runtime_hyperliquid_account_id,
                ),
            )
            if account_id
        }

    def service() -> TradingService:
        return TradingService(
            business_database(),
            authoritative_live_accounts=authoritative_live_accounts(),
        )

    def effective_direct_capital_settings(user_id: UUID) -> tuple[Settings, dict[str, Any] | None]:
        config = service().direct_capital_configuration(user_id)
        if config is None:
            return resolved_settings, None
        return (
            resolved_settings.model_copy(
                update={
                    "capital_direct_network": config["network"],
                    "capital_direct_asset": config["asset"],
                    "capital_direct_vault_id": config["vault_id"],
                    "capital_direct_vault_address": config["vault_address"],
                    "capital_direct_owned_arbitrum_address": config["owned_arbitrum_address"],
                    "capital_direct_binance_account_id": config["binance_account_id"],
                    "capital_direct_binance_deposit_address": config["binance_deposit_address"],
                    "capital_direct_binance_withdrawal_address": config[
                        "binance_withdrawal_address"
                    ],
                    "capital_direct_hyperliquid_account_id": config["hyperliquid_account_id"],
                    "capital_direct_hyperliquid_bridge_address": config[
                        "hyperliquid_bridge_address"
                    ],
                    "capital_direct_safe_address": config["safe_address"],
                    "capital_direct_safe_delegate_address": config["safe_delegate_address"],
                    "capital_direct_max_amount": (
                        None if config["max_amount"] is None else Decimal(str(config["max_amount"]))
                    ),
                    "capital_direct_max_fee": (
                        None if config["max_fee"] is None else Decimal(str(config["max_fee"]))
                    ),
                }
            ),
            config,
        )

    def capital_snapshot(user_id: UUID) -> dict[str, Any]:
        direct_settings, saved_config = effective_direct_capital_settings(user_id)
        snapshot = queries().capital_center(
            user_id,
            authoritative_live_accounts=authoritative_live_accounts(),
        )
        expected_interval = resolved_settings.runtime_sync_interval_seconds
        snapshot["net_worth"]["history_expected_interval_seconds"] = expected_interval
        snapshot["net_worth"]["history_gap_tolerance_seconds"] = max(
            180,
            expected_interval * 3,
        )
        configured_chain_id: int | None
        try:
            configured_chain_id = notilt_chain_id_for_network(
                direct_settings.capital_direct_network
            )
        except DomainRejected:
            configured_chain_id = None
        configured_vault = (
            None
            if configured_chain_id is None
            else resolved_settings.notilt_vaults.get(configured_chain_id)
        )
        snapshot["direct_configuration"] = {
            "single_account_mode": True,
            "source": "VERSIONED_DATABASE" if saved_config is not None else "ENVIRONMENT",
            "version": None if saved_config is None else saved_config["version"],
            "effective_at": None if saved_config is None else saved_config["effective_at"],
            "updated_by_username": (
                None if saved_config is None else saved_config["updated_by_username"]
            ),
            "can_manage": service().can_user(user_id, "access.manage"),
            "asset": direct_settings.capital_direct_asset,
            "network": direct_settings.capital_direct_network,
            "vault_id_configured": direct_settings.capital_direct_vault_id is not None,
            "vault_address_configured": (direct_settings.capital_direct_vault_address is not None),
            "owned_arbitrum_address_configured": (
                direct_settings.capital_direct_owned_arbitrum_address is not None
            ),
            "binance_account_configured": (
                direct_settings.capital_direct_binance_account_id is not None
            ),
            "binance_whitelist_destination_configured": (
                direct_settings.capital_direct_binance_deposit_address is not None
            ),
            "binance_withdrawal_destination_configured": (
                direct_settings.capital_direct_binance_withdrawal_address is not None
            ),
            "hyperliquid_account_configured": (
                direct_settings.capital_direct_hyperliquid_account_id is not None
            ),
            "hyperliquid_contract_configured": (
                direct_settings.capital_direct_hyperliquid_bridge_address is not None
            ),
            "limits_configured": (
                direct_settings.capital_direct_max_amount is not None
                and direct_settings.capital_direct_max_fee is not None
            ),
            "notilt_sdk_available": resolved_notilt.available,
            "notilt_scope_configured": (
                resolved_settings.notilt_enabled
                and configured_vault is not None
                and resolved_settings.notilt_agent_address is not None
            ),
            "safe_spending_enabled": direct_settings.safe_spending_enabled,
            "safe_gateway_available": resolved_safe_spending.available,
            "safe_spending_scope_configured": (
                direct_settings.safe_spending_enabled
                and direct_settings.safe_spending_arbitrum_rpc_url is not None
                and direct_settings.capital_direct_safe_address is not None
                and direct_settings.capital_direct_safe_delegate_address is not None
            ),
            "safe_address_configured": direct_settings.capital_direct_safe_address is not None,
            "safe_delegate_configured": (
                direct_settings.capital_direct_safe_delegate_address is not None
            ),
            "signing": False,
            "broadcast": False,
        }
        return snapshot

    def require_capability(
        identity: SessionIdentity,
        action: str,
        account_id: str | None = None,
        venue: str | None = None,
    ) -> None:
        assignments = queries().user_context(identity.user_id)["roles"]
        allowed = any(
            service().can_user(
                identity.user_id,
                action,
                account_id if account_id is not None else assignment["account_scope"],
                venue if venue is not None else assignment["venue_scope"],
            )
            for assignment in assignments
            if venue is None
            or assignment["venue_scope"] is None
            or assignment["venue_scope"] == venue
        )
        if not allowed:
            raise DomainRejected("RBAC_DENIED", f"{action} is not assigned to this user")

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

    def require_default_venue_account(account_id: str, venue: str) -> None:
        expected = (
            resolved_settings.runtime_binance_account_id
            if venue == "BINANCE"
            else resolved_settings.runtime_hyperliquid_account_id
        )
        if expected is None:
            raise DomainRejected(
                "DEFAULT_ACCOUNT_NOT_CONFIGURED",
                f"{venue} production facts require a configured default account",
            )
        if account_id != expected:
            raise DomainRejected(
                "DEFAULT_ACCOUNT_REQUIRED",
                f"{venue} production facts are restricted to the configured default account",
            )

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

    @app.get("/api/admin/users")
    def managed_users(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return {"data": queries().managed_users(identity.user_id), "as_of": _now().isoformat()}

    @app.post("/api/admin/users")
    def create_managed_user(
        payload: ManagedUserCreateRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        user_id = service().create_managed_user(
            payload.username,
            [Role(value) for value in payload.roles],
            identity.user_id,
            payload.account_scope,
            payload.venue_scope,
            now=_now(),
        )
        return {
            "user_id": str(user_id),
            "data": queries().managed_users(identity.user_id),
        }

    @app.put("/api/admin/users/{user_id}/access")
    def update_managed_user_access(
        user_id: UUID,
        payload: ManagedUserAccessRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        service().update_managed_user_access(
            user_id,
            [Role(value) for value in payload.roles],
            payload.active,
            identity.user_id,
            payload.account_scope,
            payload.venue_scope,
            now=_now(),
        )
        return {"user_id": str(user_id), "data": queries().managed_users(identity.user_id)}

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
        if payload.action in {"proposal.approve", "proposal.admin_approve"}:
            current_version = queries().proposal_version(payload.object_id)
            detail = queries().proposal_detail(identity.user_id, payload.object_id)
            review_action = (
                "proposal.review"
                if payload.action == "proposal.approve"
                else "proposal.admin_approve"
            )
        elif payload.action == "capital.approve":
            current_version = queries().transfer_proposal_version(payload.object_id)
            detail = queries().transfer_proposal_detail(identity.user_id, payload.object_id)
            review_action = "capital.review"
        elif payload.action in {"risk.restore.review", "risk.restore.execute"}:
            current_version = service().risk_control_change_version(payload.object_id)
            detail = {"account_id": None, "venue": None}
            review_action = payload.action
        elif payload.action == "risk.restore.direct":
            status_detail = service().risk_control_status(
                identity.user_id,
                configured_risk_scopes(),
                require_live_scope=True,
                now=_now(),
            )
            current_version = int(status_detail["policy"]["revision"])
            if payload.object_id != UUID(str(status_detail["policy"]["policy_id"])):
                raise DomainRejected("VERSION_CONFLICT", "risk policy changed before step-up")
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
        return {
            "data": queries().list_instruments(identity.user_id),
            "catalog_scope": {
                "contract_family": "U_MARGINED_PERPETUAL",
                "strategy_allowlist_applied": False,
                "exchange_trading_status_required": True,
            },
            "as_of": _now().isoformat(),
        }

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
                    symbol=None if detail["symbol"] is None else str(detail["symbol"]),
                    direction=str(detail["direction"]),
                    risk_tier=str(detail["risk_tier"]),
                    quantity=str(detail["quantity"]),
                    max_risk=str(detail["max_risk"]),
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

    def opportunity_snapshot(*, user_id: UUID, now: datetime) -> dict[str, Any]:
        source_candidates = current_perptape_candidates(now=now)
        candidates = [
            candidate
            for candidate in source_candidates
            if perptape_candidate_identity_is_displayable(candidate)
        ]
        feed = queries().perptape_feed() if resolved_settings.runtime_sync_enabled else None
        active_instrument_keys = queries().active_instrument_keys(
            {
                (candidate.venue, candidate.symbol)
                for candidate in candidates
                if candidate.venue in {"BINANCE", "HYPERLIQUID"}
            }
        )
        active_proposals = {
            (item["venue"], item["symbol"], item["direction"]): item
            for item in queries().active_perptape_system_proposals(user_id, now=now)
        }
        data: list[dict[str, Any]] = []
        for candidate in candidates:
            value = candidate.to_dict()
            value["active_proposal"] = active_proposals.get(
                (candidate.venue, candidate.symbol, candidate.direction.value)
            )
            instrument_available = (candidate.venue, candidate.symbol) in active_instrument_keys
            proposal_eligible = (
                candidate.venue in {"BINANCE", "HYPERLIQUID"}
                and candidate.readiness == "READY"
                and candidate.data_health == "CURRENT"
                and instrument_available
            )
            value["proposal_eligible"] = proposal_eligible
            if candidate.venue not in {"BINANCE", "HYPERLIQUID"}:
                blocker = "VENUE_UNSUPPORTED"
            elif candidate.readiness == "INCOMPLETE":
                blocker = "PERPTAPE_REQUIRED_FIELDS_MISSING"
            elif candidate.readiness != "READY" or candidate.data_health != "CURRENT":
                blocker = "PERPTAPE_CANDIDATE_NOT_CURRENT"
            elif not instrument_available:
                blocker = "INSTRUMENT_UNAVAILABLE"
            else:
                blocker = None
            value["proposal_blocker"] = blocker
            missing_fields: list[str] = []
            if candidate.threshold is None:
                missing_fields.append("threshold")
            if candidate.readiness != "READY":
                missing_fields.append("klineReadiness.status=ready")
            if candidate.data_health != "CURRENT":
                missing_fields.append("data_health=CURRENT")
            if not instrument_available:
                missing_fields.append("active Instrument Catalog match")
            value["missing_fields"] = missing_fields
            value["missing_field_labels"] = [
                {
                    "threshold": "突破阈值",
                    "klineReadiness.status=ready": "K 线就绪状态",
                    "data_health=CURRENT": "实时完整数据",
                    "active Instrument Catalog match": "可交易合约目录",
                }[field]
                for field in missing_fields
            ]
            value["last_complete_at"] = (
                candidate.observed_at.isoformat()
                if candidate.readiness == "READY" and candidate.data_health == "CURRENT"
                else None
            )
            data.append(value)
        snapshot_id = hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {
            "source": "PERPTAPE",
            "source_contract_version": resolved_settings.perptape_contract_version,
            "environment": "LIVE",
            "snapshot_id": snapshot_id,
            "snapshot_generated_at": (None if feed is None else feed.generated_at.isoformat()),
            "retry_at": None if feed is None else feed.next_allowed_at.isoformat(),
            "as_of": now.isoformat(),
            "discarded_candidate_count": len(source_candidates) - len(candidates),
            "data": data,
        }

    @app.get("/api/opportunities")
    def opportunities(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_capability(identity, "opportunity.view")
        return opportunity_snapshot(user_id=identity.user_id, now=_now())

    @app.get("/api/proposal-defaults")
    def proposal_defaults(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_capability(identity, "proposal.create")
        context = queries().user_context(identity.user_id)
        config = service().proposal_default_config(identity.user_id)
        return {
            "configured": config is not None,
            "can_manage": any(item["role"] == Role.SYSTEM_ADMIN.value for item in context["roles"]),
            "data": config,
            "as_of": _now().isoformat(),
        }

    @app.put("/api/proposal-defaults")
    def update_proposal_defaults(
        payload: ProposalDefaultConfigRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        service().set_proposal_default_config(
            identity.user_id,
            payload.idempotency_key,
            account_id=payload.account_id,
            risk_tier=payload.risk_tier,
            notional=payload.notional,
            max_risk=payload.max_risk,
            invalidation_bps=payload.invalidation_bps,
            expires_in_minutes=payload.expires_in_minutes,
            rationale=payload.rationale,
            auto_proposal_enabled=payload.auto_proposal_enabled,
            auto_proposal_min_timeframes=payload.auto_proposal_min_timeframes,
            now=_now(),
        )
        return {
            "configured": True,
            "can_manage": True,
            "data": service().proposal_default_config(identity.user_id),
            "as_of": _now().isoformat(),
        }

    @app.websocket("/ws/opportunities")
    async def opportunity_stream(websocket: WebSocket) -> None:
        session_token = websocket.cookies.get(SESSION_COOKIE)
        if session_token is None:
            await websocket.close(code=4401)
            return
        try:
            identity = token_service.verify_session(session_token, now=_now())
            queries().user_context(identity.user_id)
            require_capability(identity, "opportunity.view")
        except DomainRejected as exc:
            await websocket.close(code=4403 if exc.code == "RBAC_DENIED" else 4401)
            return

        await websocket.accept()
        last_digest: str | None = None
        last_error: str | None = None
        last_heartbeat = 0.0
        loop = asyncio.get_running_loop()
        try:
            while True:
                try:
                    snapshot = opportunity_snapshot(user_id=identity.user_id, now=_now())
                except DomainRejected as exc:
                    if last_error != exc.code:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "error": {"code": exc.code, "message": exc.detail},
                            }
                        )
                        last_error = exc.code
                else:
                    digest = hashlib.sha256(
                        json.dumps(snapshot["data"], sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()
                    monotonic_now = loop.time()
                    if digest != last_digest:
                        await websocket.send_json({"type": "snapshot", **snapshot})
                        last_digest = digest
                        last_error = None
                        last_heartbeat = monotonic_now
                    elif monotonic_now - last_heartbeat >= 15:
                        await websocket.send_json({"type": "heartbeat", "as_of": snapshot["as_of"]})
                        last_heartbeat = monotonic_now
                await asyncio.sleep(2)
        except WebSocketDisconnect:
            return

    @app.post("/api/opportunities/{candidate_id}/proposals")
    def create_system_proposal(
        candidate_id: str,
        payload: SystemProposalRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        candidate = current_perptape_candidate(candidate_id, now=now)
        if candidate.readiness != "READY" or candidate.data_health != "CURRENT":
            raise DomainRejected(
                "PERPTAPE_CANDIDATE_NOT_READY", "candidate data is not ready for proposal review"
            )
        default_config: dict[str, Any] | None = None
        if payload.configuration_mode == "DEFAULT":
            default_config = service().proposal_default_config(identity.user_id)
            if default_config is None:
                raise DomainRejected(
                    "PROPOSAL_DEFAULT_NOT_CONFIGURED",
                    "an administrator must configure one-click proposal defaults",
                )
            expected_quantity = (
                Decimal(str(default_config["notional"])) / candidate.reference_price
            ).quantize(Decimal("0.000000000000000001"), rounding=ROUND_DOWN)
            invalidation_factor = Decimal(int(default_config["invalidation_bps"])) / Decimal(10_000)
            expected_invalidation = (
                candidate.reference_price
                * (
                    Decimal(1) - invalidation_factor
                    if candidate.direction.value == "LONG"
                    else Decimal(1) + invalidation_factor
                )
            ).quantize(Decimal("0.000000000000000001"), rounding=ROUND_DOWN)
            if (
                payload.default_config_version != default_config["version"]
                or payload.environment != default_config["environment"]
                or payload.account_id != default_config["account_id"]
                or payload.risk_tier.value != default_config["risk_tier"]
                or payload.quantity != expected_quantity
                or payload.initial_quantity is not None
                or payload.max_risk != Decimal(str(default_config["max_risk"]))
                or payload.invalidation_price != expected_invalidation
                or payload.allow_auto_add
                or payload.requested_adds != 0
                or payload.add_trigger_price is not None
                or payload.expires_in_minutes != default_config["expires_in_minutes"]
                or payload.rationale != default_config["rationale"]
            ):
                raise DomainRejected(
                    "PROPOSAL_DEFAULT_VERSION_MISMATCH",
                    "one-click payload does not match the active server default version",
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
        idempotency_payload = {
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
        }
        if payload.configuration_mode == "DEFAULT":
            idempotency_payload.update(
                {
                    "configuration_mode": payload.configuration_mode,
                    "default_config_version": payload.default_config_version,
                }
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
                "configuration_mode": payload.configuration_mode,
                "default_config_id": (
                    None if default_config is None else default_config["config_id"]
                ),
                "default_config_version": payload.default_config_version,
            },
            idempotency_payload=idempotency_payload,
            deduplicate_active_system_scope=True,
            now=now,
        )
        current = queries().proposal_detail(principal.user_id, proposal_id)
        if current["status"] == ProposalStatus.DRAFT.value:
            service().submit_proposal(proposal_id, principal.user_id, now=now)
            current = queries().proposal_detail(identity.user_id, proposal_id)
            notify_reviewers(proposal_id, int(current["version"]), current["environment"])
        return current

    @app.post("/api/opportunities/{candidate_id}/proposals/default")
    def create_default_system_proposal(
        candidate_id: str,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        candidate = current_perptape_candidate(candidate_id, now=now)
        config = service().proposal_default_config(identity.user_id)
        if config is None:
            raise DomainRejected(
                "PROPOSAL_DEFAULT_NOT_CONFIGURED",
                "an administrator must configure one-click proposal defaults",
            )
        quantity = (Decimal(str(config["notional"])) / candidate.reference_price).quantize(
            Decimal("0.000000000000000001"), rounding=ROUND_DOWN
        )
        factor = Decimal(int(config["invalidation_bps"])) / Decimal(10_000)
        invalidation = (
            candidate.reference_price
            * (Decimal(1) - factor if candidate.direction.value == "LONG" else Decimal(1) + factor)
        ).quantize(Decimal("0.000000000000000001"), rounding=ROUND_DOWN)
        return create_system_proposal(
            candidate_id,
            SystemProposalRequest(
                environment=config["environment"],
                account_id=config["account_id"],
                risk_tier=config["risk_tier"],
                quantity=quantity,
                initial_quantity=None,
                max_risk=Decimal(str(config["max_risk"])),
                invalidation_price=invalidation,
                allow_auto_add=False,
                requested_adds=0,
                add_trigger_price=None,
                expires_in_minutes=int(config["expires_in_minutes"]),
                rationale=str(config["rationale"]),
                configuration_mode="DEFAULT",
                default_config_version=int(config["version"]),
            ),
            identity,
        )

    @app.post("/api/proposals/manual")
    def create_manual_proposal(
        payload: ManualProposalRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        instrument = next(
            (
                item
                for item in queries().list_instruments(identity.user_id)
                if item["instrument_id"] == str(payload.instrument_id)
            ),
            None,
        )
        if instrument is None or instrument["venue"] != payload.venue:
            raise DomainRejected(
                "INSTRUMENT_UNAVAILABLE",
                "instrument is outside the current U-margined venue catalog and permission scope",
            )
        quantity = payload.quantity
        initial_quantity = payload.initial_quantity
        resolved_position_notional: Decimal | None = None
        position_notional_currency: str | None = None
        if payload.max_position_notional is not None:
            quote_currency = str(instrument["quote_currency"])
            collateral_currency = str(instrument["collateral_currency"])
            if quote_currency != collateral_currency or quote_currency not in {"USDT", "USDC"}:
                raise DomainRejected(
                    "POSITION_NOTIONAL_CURRENCY_UNSUPPORTED",
                    "manual position amount requires one matching U-margined quote "
                    "and collateral currency",
                )
            lot_size = Decimal(str(instrument["lot_size"]))
            contract_multiplier = Decimal(str(instrument["contract_multiplier"]))
            minimum_notional = Decimal(str(instrument["minimum_notional"]))

            def quantity_from_notional(value: Decimal, *, error_code: str) -> Decimal:
                raw_quantity = value / (payload.trigger_price * contract_multiplier)
                steps = (raw_quantity / lot_size).to_integral_value(rounding=ROUND_DOWN)
                resolved = steps * lot_size
                resolved_notional = resolved * payload.trigger_price * contract_multiplier
                if resolved <= 0 or resolved_notional < minimum_notional:
                    raise DomainRejected(
                        error_code,
                        "position amount is below the current contract lot size "
                        "or minimum notional",
                    )
                return resolved

            quantity = quantity_from_notional(
                payload.max_position_notional,
                error_code="POSITION_NOTIONAL_TOO_SMALL",
            )
            initial_quantity = (
                None
                if payload.initial_position_notional is None
                else quantity_from_notional(
                    payload.initial_position_notional,
                    error_code="INITIAL_POSITION_NOTIONAL_TOO_SMALL",
                )
            )
            if initial_quantity is not None and initial_quantity > quantity:
                raise DomainRejected(
                    "INITIAL_POSITION_EXCEEDS_CAP",
                    "resolved initial position exceeds the maximum position amount",
                )
            if (
                payload.allow_auto_add
                and initial_quantity is not None
                and initial_quantity >= quantity
            ):
                raise DomainRejected(
                    "INITIAL_POSITION_EXHAUSTS_CAP",
                    "resolved initial position leaves no quantity capacity for AUTO_ADD",
                )
            resolved_position_notional = quantity * payload.trigger_price * contract_multiplier
            position_notional_currency = quote_currency
        assert quantity is not None
        proposal_id = service().create_proposal(
            actor_id=identity.user_id,
            source=ProposalSource.MANUAL,
            risk_tier=payload.risk_tier,
            account_id=payload.account_id,
            venue=payload.venue,
            instrument_id=payload.instrument_id,
            direction=payload.direction,
            quantity=quantity,
            max_risk=payload.max_risk,
            expires_at=now + timedelta(minutes=payload.expires_in_minutes),
            idempotency_key=payload.idempotency_key,
            environment=ExecutionEnvironment(payload.environment),
            details={
                "trigger_price": str(payload.trigger_price),
                "limit_price": None if payload.limit_price is None else str(payload.limit_price),
                "invalidation_price": str(payload.invalidation_price),
                "initial_quantity": str(quantity if initial_quantity is None else initial_quantity),
                "requested_max_position_notional": (
                    None
                    if payload.max_position_notional is None
                    else str(payload.max_position_notional)
                ),
                "resolved_position_notional": (
                    None if resolved_position_notional is None else str(resolved_position_notional)
                ),
                "position_notional_currency": position_notional_currency,
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
                "quantity": str(quantity),
                "max_position_notional": (
                    None
                    if payload.max_position_notional is None
                    else str(payload.max_position_notional)
                ),
                "initial_quantity": (None if initial_quantity is None else str(initial_quantity)),
                "initial_position_notional": (
                    None
                    if payload.initial_position_notional is None
                    else str(payload.initial_position_notional)
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
            deduplicate_active_manual_semantics=True,
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
        require_capability(identity, "proposal.view")
        now = _now()
        return {
            "data": queries().list_proposals(
                identity.user_id,
                status=proposal_status,
                now=now,
            ),
            "as_of": now.isoformat(),
        }

    @app.get("/api/proposals/{proposal_id}")
    def proposal_detail(
        proposal_id: UUID,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_capability(identity, "proposal.view")
        return queries().proposal_detail(identity.user_id, proposal_id, now=_now())

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
        detail = queries().proposal_detail(identity.user_id, proposal_id, now=now)
        if result is ProposalStatus.PENDING_REVIEW:
            notify_reviewers(
                proposal_id,
                int(detail["version"]),
                str(detail["environment"]),
            )
        return {
            "proposal_id": str(proposal_id),
            "status": result.value,
            "detail": detail,
        }

    @app.post("/api/proposals/{proposal_id}/admin-approve")
    def admin_direct_approve_proposal(
        proposal_id: UUID,
        payload: AdminDirectApproveRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        token_service.verify_action_grant(
            payload.action_grant,
            user_id=identity.user_id,
            action="proposal.admin_approve",
            object_id=proposal_id,
            object_version=payload.expected_version,
            now=now,
        )
        result = service().admin_direct_approve_proposal(
            proposal_id,
            identity.user_id,
            payload.reason,
            payload.expected_version,
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
        notification_key = f"{campaign_id}:{event_type}:{event_key}:{recipient_id}"
        resolved_telegram.send_campaign(
            CampaignNotification(
                notification_id="tg_" + hashlib.sha256(notification_key.encode()).hexdigest()[:20],
                recipient_id=recipient_id,
                campaign_id=campaign_id,
                event_type=event_type,
                environment=environment,
                summary=summary,
                campaign_version=campaign_version,
                action_references=(),
                created_at=_now(),
                status=str(detail["status"]),
                auto_add_available=False,
                position_reduction_available=False,
            )
        )

    @app.get("/api/venues/binance/status")
    def binance_read_only_status(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_capability(identity, "venue.view", venue="BINANCE")
        return {
            "venue": "BINANCE",
            "mode": "USER_DATA_READ_ONLY",
            "enabled": resolved_settings.binance_read_only_enabled,
            "configured": resolved_binance.configured,
            "execution_backend": resolved_settings.execution_backend,
            "order_send_available": False,
            "worker_configured": resolved_settings.freqtrade_workers_enabled,
            "account_mode": resolved_settings.binance_account_mode,
            "fact_environment": resolved_settings.binance_fact_environment,
            "automatic_sync_enabled": (
                resolved_settings.runtime_sync_enabled
                and resolved_settings.runtime_binance_account_id is not None
            ),
            "automatic_sync_interval_seconds": resolved_settings.runtime_sync_interval_seconds,
            "default_account_id": resolved_settings.runtime_binance_account_id,
            "environment": resolved_settings.environment,
        }

    def require_binance_live() -> None:
        if resolved_settings.execution_backend != "DIRECT_LEGACY":
            raise DomainRejected(
                "DIRECT_EXECUTION_RETIRED",
                "direct Binance sending is retired; execution belongs to the Freqtrade worker",
            )
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
        require_capability(identity, "venue.view", venue="BINANCE")
        return {
            "venue": "BINANCE",
            "environment": "LIVE",
            "account_mode": resolved_settings.binance_account_mode,
            "execution_backend": resolved_settings.execution_backend,
            "enabled": resolved_settings.binance_live_order_send_enabled,
            "configured": resolved_binance_live.configured,
            "capability_gate_required": "LIVE_ORDER_SEND",
            "capital_transfer": False,
        }

    def require_binance_testnet() -> None:
        if resolved_settings.execution_backend != "DIRECT_LEGACY":
            raise DomainRejected(
                "DIRECT_EXECUTION_RETIRED",
                "direct Binance sending is retired; execution belongs to the Freqtrade worker",
            )
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
        require_capability(identity, "venue.view", venue="BINANCE")
        return {
            "venue": "BINANCE",
            "environment": "TESTNET",
            "execution_backend": resolved_settings.execution_backend,
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
        require_default_venue_account(account_id, "BINANCE")
        require_capability(identity, "venue.view", account_id, "BINANCE")
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
        require_default_venue_account(payload.account_id, "BINANCE")
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
        require_capability(identity, "venue.view", venue="HYPERLIQUID")
        return {
            "venue": "HYPERLIQUID",
            "domain": "CORE_AND_CONFIGURED_HIP3",
            "dex": "",
            "mode": "INFO_READ_ONLY",
            "enabled": resolved_settings.hyperliquid_read_only_enabled,
            "configured": resolved_hyperliquid.configured,
            "execution_backend": resolved_settings.execution_backend,
            "order_send_available": False,
            "worker_configured": resolved_settings.freqtrade_workers_enabled,
            "fact_environment": resolved_settings.hyperliquid_fact_environment,
            "source_environment": resolved_hyperliquid.fact_environment,
            "hip3_available": bool(resolved_settings.hyperliquid_hip3_dexes),
            "hip3_dexes": list(resolved_settings.hyperliquid_hip3_dexes),
            "automatic_sync_enabled": (
                resolved_settings.runtime_sync_enabled
                and resolved_settings.runtime_hyperliquid_account_id is not None
            ),
            "automatic_sync_interval_seconds": resolved_settings.runtime_sync_interval_seconds,
            "default_account_id": resolved_settings.runtime_hyperliquid_account_id,
            "environment": resolved_settings.environment,
        }

    def require_hyperliquid_live() -> None:
        if resolved_settings.execution_backend != "DIRECT_LEGACY":
            raise DomainRejected(
                "DIRECT_EXECUTION_RETIRED",
                "direct Hyperliquid sending is retired; execution belongs to the Freqtrade worker",
            )
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
        require_capability(identity, "venue.view", venue="HYPERLIQUID")
        return {
            "venue": "HYPERLIQUID",
            "domain": "CORE",
            "environment": "LIVE",
            "execution_backend": resolved_settings.execution_backend,
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
        require_capability(identity, "venue.view", venue="HYPERLIQUID")
        return {
            "venue": "HYPERLIQUID",
            "domain": "CORE",
            "environment": "TESTNET",
            "execution_backend": resolved_settings.execution_backend,
            "enabled": resolved_settings.hyperliquid_testnet_order_send_enabled,
            "configured": resolved_hyperliquid_testnet.configured,
            "signer_source": "INJECTED_RUNTIME_ONLY",
            "live_order_send": False,
            "capital_transfer": False,
            "hip3_available": False,
        }

    def require_hyperliquid_testnet() -> None:
        if resolved_settings.execution_backend != "DIRECT_LEGACY":
            raise DomainRejected(
                "DIRECT_EXECUTION_RETIRED",
                "direct Hyperliquid sending is retired; execution belongs to the Freqtrade worker",
            )
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
        require_default_venue_account(account_id, "HYPERLIQUID")
        require_capability(identity, "venue.view", account_id, "HYPERLIQUID")
        return {
            "mode": "INFO_READ_ONLY",
            "domain": "CORE_AND_CONFIGURED_HIP3",
            "hip3_dexes": list(resolved_settings.hyperliquid_hip3_dexes),
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
        require_default_venue_account(payload.account_id, "HYPERLIQUID")
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
        symbol_dex = payload.symbol.split(":", 1)[0] if ":" in payload.symbol else ""
        return {
            "source": "HYPERLIQUID_INFO",
            "mode": "READ_ONLY",
            "domain": "CORE" if not symbol_dex else f"HIP3:{symbol_dex}",
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
        require_capability(identity, "operations.view")
        return {"data": queries().list_campaigns(identity.user_id), "as_of": _now().isoformat()}

    @app.get("/api/campaign-exceptions")
    def campaign_exceptions(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_capability(identity, "operations.view")
        now = _now()
        return {
            "data": queries().list_exceptions(identity.user_id, now=now),
            "as_of": now.isoformat(),
        }

    @app.get("/api/campaigns/{campaign_id}")
    def campaign_detail(
        campaign_id: UUID,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_capability(identity, "operations.view")
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
        require_capability(identity, "system.view")
        return service().risk_control_status(
            identity.user_id,
            configured_risk_scopes(),
            require_live_scope=True,
            now=_now(),
        )

    @app.post("/api/risk-controls/restore-direct")
    def direct_risk_control_restore(
        payload: RiskControlDirectRestoreRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        current = service().risk_control_status(
            identity.user_id,
            configured_risk_scopes(),
            require_live_scope=True,
            now=now,
        )
        policy_id = UUID(str(current["policy"]["policy_id"]))
        policy_revision = int(current["policy"]["revision"])
        token_service.verify_action_grant(
            payload.action_grant,
            user_id=identity.user_id,
            action="risk.restore.direct",
            object_id=policy_id,
            object_version=policy_revision,
            now=now,
        )
        restored_policy_id = service().direct_restore_risk_controls(
            identity.user_id,
            payload.idempotency_key,
            reason=payload.reason,
            configured_scopes=configured_risk_scopes(),
            require_live_scope=True,
            now=now,
        )
        return {"policy_id": str(restored_policy_id)}

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
            require_live_scope=True,
            now=_now(),
        )
        return service().risk_control_status(
            identity.user_id,
            configured_risk_scopes(),
            require_live_scope=True,
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
            require_live_scope=True,
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

    @app.post("/api/campaigns/{campaign_id}/freqtrade/emergency-exit/recover")
    def recover_freqtrade_emergency_exit(
        campaign_id: UUID,
        payload: ReconciliationReasonRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        intent_id = service().recover_freqtrade_emergency_exit(
            campaign_id,
            identity.user_id,
            payload.reason,
            now=_now(),
        )
        return {
            "intent_id": str(intent_id),
            "sent_order": False,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

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

    def verify_live_notilt_release_budget(
        *,
        chain_id: int,
        vault: str,
        agent: str,
        asset: str,
        amount: Decimal,
        max_fact_age_seconds: int,
        now: datetime,
    ) -> None:
        snapshot = resolved_notilt.read_vault(chain_id, vault, agent)
        budget = next(
            (item for item in snapshot.budgets if item.asset == asset.upper()),
            None,
        )
        if budget is None:
            raise DomainRejected(
                "NOTILT_RELEASE_BUDGET_MISSING",
                "NoTilt release requires a live budget for the configured asset",
            )
        if (
            snapshot.vault.lower() != vault.lower()
            or snapshot.agent.lower() != agent.lower()
            or budget.vault.lower() != vault.lower()
            or budget.agent.lower() != agent.lower()
        ):
            raise DomainRejected(
                "NOTILT_RELEASE_SCOPE_MISMATCH",
                "NoTilt live budget does not match the configured Vault and Agent scope",
            )
        if not budget.is_official_vault:
            raise DomainRejected(
                "NOTILT_VAULT_UNTRUSTED",
                "NoTilt release requires an official Vault from the trusted deployment catalog",
            )
        if (
            not budget.is_active_whitelist
            or budget.assigned_whitelist_vault.lower() != vault.lower()
        ):
            raise DomainRejected(
                "NOTILT_WHITELIST_INACTIVE",
                "NoTilt release requires an active whitelist assigned to the configured Vault",
            )
        if budget.owner.lower() == agent.lower():
            raise DomainRejected(
                "NOTILT_AGENT_OWNER_FORBIDDEN",
                "NoTilt Agent budget cannot use the Vault owner identity",
            )
        if budget.panic_locked:
            raise DomainRejected("NOTILT_PANIC_LOCKED", "NoTilt Vault is panic locked")
        fact_age = now - budget.block_timestamp
        if fact_age < timedelta(0) or fact_age > timedelta(seconds=max_fact_age_seconds):
            raise DomainRejected(
                "NOTILT_FACT_STALE",
                "NoTilt live budget is outside the active production freshness window",
            )
        if amount > budget.max_release_net:
            raise DomainRejected(
                "NOTILT_RELEASE_LIMIT_EXCEEDED",
                "NoTilt release amount exceeds the current live maxReleaseNet allowance",
            )

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
        return len(fact_ids), capital_snapshot(actor_id)

    @app.get("/api/notilt/status")
    def notilt_status(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        capital_snapshot(identity.user_id)
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
        capital_snapshot(identity.user_id)
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
        return {"data": capital_snapshot(identity.user_id), "as_of": _now().isoformat()}

    @app.put("/api/capital/direct-configuration")
    def update_direct_capital_configuration(
        payload: DirectCapitalConfigurationRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        direct_settings, _ = effective_direct_capital_settings(identity.user_id)
        supplied = payload.model_dump(exclude={"idempotency_key"}, exclude_none=True)
        field_map = {
            "network": "capital_direct_network",
            "asset": "capital_direct_asset",
            "vault_id": "capital_direct_vault_id",
            "vault_address": "capital_direct_vault_address",
            "owned_arbitrum_address": "capital_direct_owned_arbitrum_address",
            "binance_account_id": "capital_direct_binance_account_id",
            "binance_deposit_address": "capital_direct_binance_deposit_address",
            "binance_withdrawal_address": "capital_direct_binance_withdrawal_address",
            "hyperliquid_account_id": "capital_direct_hyperliquid_account_id",
            "hyperliquid_bridge_address": "capital_direct_hyperliquid_bridge_address",
            "safe_address": "capital_direct_safe_address",
            "safe_delegate_address": "capital_direct_safe_delegate_address",
            "max_amount": "capital_direct_max_amount",
            "max_fee": "capital_direct_max_fee",
        }
        merged = {
            field: supplied.get(field, getattr(direct_settings, setting_name))
            for field, setting_name in field_map.items()
        }
        trusted_vault = resolved_settings.notilt_vaults.get(42161)
        direct_vault = merged["vault_address"]
        if (
            trusted_vault is not None
            and direct_vault is not None
            and str(direct_vault).lower() != trusted_vault.lower()
        ):
            raise DomainRejected(
                "NOTILT_VAULT_SCOPE_MISMATCH",
                "direct capital Vault must match the configured trusted NoTilt scope",
            )
        for venue, configured_account, runtime_account in (
            (
                "BINANCE",
                merged["binance_account_id"],
                resolved_settings.runtime_binance_account_id,
            ),
            (
                "HYPERLIQUID",
                merged["hyperliquid_account_id"],
                resolved_settings.runtime_hyperliquid_account_id,
            ),
        ):
            if (
                configured_account is not None
                and runtime_account is not None
                and configured_account != runtime_account
            ):
                raise DomainRejected(
                    "DEFAULT_ACCOUNT_REQUIRED",
                    f"{venue} capital account must match the single configured default account",
                )
        config_id = service().set_direct_capital_configuration(
            identity.user_id,
            payload.idempotency_key,
            network=str(merged["network"]),
            asset=str(merged["asset"]),
            vault_id=None if merged["vault_id"] is None else str(merged["vault_id"]),
            vault_address=(
                None if merged["vault_address"] is None else str(merged["vault_address"])
            ),
            owned_arbitrum_address=(
                None
                if merged["owned_arbitrum_address"] is None
                else str(merged["owned_arbitrum_address"])
            ),
            binance_account_id=(
                None if merged["binance_account_id"] is None else str(merged["binance_account_id"])
            ),
            binance_deposit_address=(
                None
                if merged["binance_deposit_address"] is None
                else str(merged["binance_deposit_address"])
            ),
            binance_withdrawal_address=(
                None
                if merged["binance_withdrawal_address"] is None
                else str(merged["binance_withdrawal_address"])
            ),
            hyperliquid_account_id=(
                None
                if merged["hyperliquid_account_id"] is None
                else str(merged["hyperliquid_account_id"])
            ),
            hyperliquid_bridge_address=(
                None
                if merged["hyperliquid_bridge_address"] is None
                else str(merged["hyperliquid_bridge_address"])
            ),
            safe_address=(None if merged["safe_address"] is None else str(merged["safe_address"])),
            safe_delegate_address=(
                None
                if merged["safe_delegate_address"] is None
                else str(merged["safe_delegate_address"])
            ),
            max_amount=(
                None if merged["max_amount"] is None else Decimal(str(merged["max_amount"]))
            ),
            max_fee=None if merged["max_fee"] is None else Decimal(str(merged["max_fee"])),
            now=_now(),
        )
        return {
            "config_id": str(config_id),
            "data": capital_snapshot(identity.user_id),
        }

    @app.post("/api/capital/direct-operations")
    def create_direct_capital_operation(
        payload: DirectCapitalOperationRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        center = capital_snapshot(identity.user_id)
        direct_settings, _ = effective_direct_capital_settings(identity.user_id)
        plan = build_direct_capital_plan(
            path=DirectCapitalPath(payload.path),
            treasury_provider=payload.treasury_provider,
            amount=payload.amount,
            settings=direct_settings,
            capital_transfer_gate=center["real_transfer_gate"],
            now=now,
        )
        operation_id = service().create_direct_capital_operation(
            actor_id=identity.user_id,
            plan=plan,
            final_confirmed=payload.final_confirmed,
            idempotency_key=payload.idempotency_key,
            now=now,
        )
        return {
            "operation_id": str(operation_id),
            "status": plan.status,
            "treasury_provider": plan.treasury_provider.value,
            "blockers": list(plan.blockers),
            "data": capital_snapshot(identity.user_id),
        }

    @app.post("/api/capital/direct-operations/{operation_id}/notilt-unsigned-preview")
    def prepare_direct_notilt_unsigned_preview(
        operation_id: UUID,
        payload: DirectCapitalUnsignedPlanRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        context = service().direct_capital_operation_context(
            operation_id,
            identity.user_id,
            now=now,
        )
        if int(context["version"]) != payload.expected_version:
            raise DomainRejected(
                "VERSION_CONFLICT",
                "direct capital operation changed; refresh before SDK preflight",
            )
        path = DirectCapitalPath(str(context["path"]))
        if context["treasury_provider"] != "NOTILT_VAULT":
            raise DomainRejected(
                "NOTILT_PLAN_SCOPE_MISMATCH",
                "operation selected Safe Spending Limits instead of NoTilt Vault",
            )
        chain_id = notilt_chain_id_for_network(str(context["network"]))
        agent, vault = configured_notilt_scope(chain_id)
        direct_vault = (
            context["source_reference"]
            if path
            in {
                DirectCapitalPath.VAULT_TO_BINANCE,
                DirectCapitalPath.VAULT_TO_HYPERLIQUID,
            }
            else context["destination_reference"]
        )
        if direct_vault is None or direct_vault.lower() != vault.lower():
            raise DomainRejected(
                "NOTILT_VAULT_SCOPE_MISMATCH",
                "direct capital path and official NoTilt scope do not match",
            )
        amount = str(context["min_received"])
        transactions: tuple[NoTiltUnsignedTransaction, ...]
        if path in {
            DirectCapitalPath.VAULT_TO_BINANCE,
            DirectCapitalPath.VAULT_TO_HYPERLIQUID,
        }:
            max_fact_age_seconds = int(
                capital_snapshot(identity.user_id)["net_worth"]["max_fact_age_seconds"]
            )
            verify_live_notilt_release_budget(
                chain_id=chain_id,
                vault=vault,
                agent=agent,
                asset=str(context["asset"]),
                amount=Decimal(amount),
                max_fact_age_seconds=max_fact_age_seconds,
                now=now,
            )
            transactions = (
                resolved_notilt.prepare_release_request(
                    chain_id=chain_id,
                    vault=vault,
                    agent=agent,
                    asset=str(context["asset"]),
                    amount=amount,
                ),
            )
            preview_kind = "AGENT_RELEASE_REQUEST"
        else:
            depositor = context["source_reference"]
            if depositor is None:
                raise DomainRejected(
                    "CAPITAL_OWNED_ARBITRUM_ADDRESS_MISSING",
                    "NoTilt deposit preview requires the authorized owned wallet",
                )
            vault_snapshot = resolved_notilt.read_vault(chain_id, vault, depositor)
            asset_budget = next(
                (
                    item
                    for item in vault_snapshot.budgets
                    if item.asset == str(context["asset"]).upper()
                ),
                None,
            )
            if asset_budget is None or not asset_budget.is_official_vault:
                raise DomainRejected(
                    "NOTILT_VAULT_UNTRUSTED",
                    "NoTilt deposit requires a live official Vault fact",
                )
            if asset_budget.panic_locked:
                raise DomainRejected(
                    "NOTILT_PANIC_LOCKED",
                    "NoTilt Vault is panic locked",
                )
            transactions = resolved_notilt.prepare_deposit(
                chain_id=chain_id,
                vault=vault,
                agent=depositor,
                asset=str(context["asset"]),
                amount=amount,
            )
            preview_kind = "SDK_DEPOSIT_SEQUENCE"
        version = service().record_direct_capital_unsigned_preview(
            operation_id,
            identity.user_id,
            expected_version=payload.expected_version,
            final_confirmed=payload.final_confirmed,
            transactions=transactions,
            idempotency_key=payload.idempotency_key,
            now=now,
        )
        blockers = list(context["blockers"])
        return {
            "operation_id": str(operation_id),
            "version": version,
            "preview_kind": preview_kind,
            "transport": "NOTILT_OFFICIAL_SDK_UNSIGNED_PREVIEW",
            "signing": False,
            "broadcast": False,
            "execution_blocked": bool(blockers),
            "blockers": blockers,
            "transactions": [item.to_dict() for item in transactions],
            "next_step": (
                "Resolve every blocker and re-read live source receipts before a human wallet "
                "may confirm any transaction."
            ),
            "data": capital_snapshot(identity.user_id),
        }

    @app.post("/api/capital/direct-operations/{operation_id}/safe-spending-preview")
    def prepare_direct_safe_spending_preview(
        operation_id: UUID,
        payload: DirectCapitalUnsignedPlanRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        context = service().direct_capital_operation_context(
            operation_id, identity.user_id, now=now
        )
        if int(context["version"]) != payload.expected_version:
            raise DomainRejected("VERSION_CONFLICT", "direct capital operation changed; refresh")
        if context["treasury_provider"] != "SAFE_SPENDING_LIMIT":
            raise DomainRejected(
                "SAFE_PLAN_SCOPE_MISMATCH", "operation did not select Safe Spending Limits"
            )
        path = DirectCapitalPath(str(context["path"]))
        outbound = path in {
            DirectCapitalPath.VAULT_TO_BINANCE,
            DirectCapitalPath.VAULT_TO_HYPERLIQUID,
        }
        direct_settings, _ = effective_direct_capital_settings(identity.user_id)
        rpc_url = direct_settings.safe_spending_arbitrum_rpc_url
        safe = direct_settings.capital_direct_safe_address
        delegate = direct_settings.capital_direct_safe_delegate_address
        counterparty = context["destination_reference"] if outbound else context["source_reference"]
        required_scope = (
            (rpc_url, safe, delegate, counterparty) if outbound else (rpc_url, safe, counterparty)
        )
        if not direct_settings.safe_spending_enabled or not all(required_scope):
            raise DomainRejected(
                "SAFE_SPENDING_LIMIT_NOT_CONFIGURED",
                "Safe RPC, account, delegate and destination scope are required",
            )
        if outbound:
            artifact = resolved_safe_spending.prepare_spend(
                rpc_url=str(rpc_url),
                safe=str(safe),
                delegate=str(delegate),
                recipient=str(counterparty),
                amount=str(context["min_received"]),
            )
        else:
            artifact = resolved_safe_spending.prepare_deposit(
                rpc_url=str(rpc_url),
                safe=str(safe),
                sender=str(counterparty),
                amount=str(context["min_received"]),
            )
        version = service().record_direct_capital_safe_preview(
            operation_id,
            identity.user_id,
            expected_version=payload.expected_version,
            final_confirmed=payload.final_confirmed,
            signature_request=artifact,
            idempotency_key=payload.idempotency_key,
            now=now,
        )
        blockers = list(context["blockers"])
        return {
            "operation_id": str(operation_id),
            "version": version,
            "preview_kind": artifact["kind"],
            "transport": (
                "SAFE_OFFICIAL_ALLOWANCE_MODULE_HUMAN_HANDOFF"
                if outbound
                else "SAFE_EXACT_USDC_TRANSFER_HUMAN_HANDOFF"
            ),
            "signing": False,
            "broadcast": False,
            "execution_blocked": bool(blockers),
            "blockers": blockers,
            "signature_request": artifact,
            "next_step": (
                "A human-controlled delegate wallet must review and sign the exact hash; "
                "this service cannot sign or broadcast."
            ),
            "data": capital_snapshot(identity.user_id),
        }

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
        require_capability(identity, "results.view")
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
        require_capability(identity, "results.view")
        return {
            "environment": environment,
            "data": queries().audit_timeline(identity.user_id, environment, limit=limit),
            "as_of": _now().isoformat(),
        }

    @app.get("/api/runtime/status")
    def runtime_status(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_capability(identity, "system.view")
        snapshot = queries().runtime_snapshot(identity.user_id)
        perptape_feed = snapshot["perptape_feed"]
        connection_states = project_runtime_connections(
            resolved_settings,
            snapshot["source_health"],
        )
        perptape_configured = bool(resolved_settings.perptape_api_key)
        perptape_status = _perptape_runtime_status(
            resolved_settings,
            perptape_feed,
            now=_now(),
        )
        telegram_polling = (
            resolved_telegram.polling_health()
            if isinstance(resolved_telegram, TelegramBotGateway)
            else {
                "state": "DISABLED",
                "running": False,
                "last_success_at": None,
                "last_error_at": None,
                "last_error_code": None,
                "consecutive_failures": 0,
            }
        )
        snapshot.update(
            {
                "application_version": __version__,
                "runtime_environment": resolved_settings.environment,
                "process_model": (
                    "FastAPI plus independent read-only sync worker and PostgreSQL"
                    if resolved_settings.runtime_sync_enabled
                    else "one FastAPI process plus PostgreSQL"
                ),
                "connections": connection_states,
                "external_boundaries": {
                    "execution": {
                        "backend": resolved_settings.execution_backend,
                        "workers_enabled": resolved_settings.freqtrade_workers_enabled,
                        "worker_count": len(resolved_freqtrade_workers),
                        "venues": ["BINANCE", "HYPERLIQUID"],
                        "hyperliquid_hip3_dexes": list(resolved_settings.hyperliquid_hip3_dexes),
                        "direct_venue_send": False,
                        "live_order_send": False,
                    },
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
                        "configured": perptape_configured,
                        "mode": "READ_ONLY",
                        "status": perptape_status,
                        "contract_version": resolved_settings.perptape_contract_version,
                        "feed_available": perptape_feed["available"],
                        "candidate_count": perptape_feed["candidate_count"],
                        "last_fetched_at": perptape_feed["fetched_at"],
                        "last_generated_at": perptape_feed["generated_at"],
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
                        "polling": telegram_polling,
                    },
                },
            }
        )
        return {"data": snapshot, "as_of": _now().isoformat()}

    @app.get("/api/execution/freqtrade/status")
    def freqtrade_worker_status(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_capability(identity, "system.view")
        workers: list[dict[str, Any]] = []
        for worker in resolved_freqtrade_workers:
            if not resolved_settings.freqtrade_workers_enabled:
                workers.append(
                    {
                        "name": worker.spec.name,
                        "venue": worker.spec.venue,
                        "backend": "FREQTRADE",
                        "status": "DISABLED",
                        "reason_code": "FREQTRADE_WORKERS_DISABLED",
                        "hip3_dexes": list(worker.spec.hip3_dexes),
                        "order_send": False,
                    }
                )
                continue
            try:
                workers.append(
                    worker.probe(
                        expected_mode=(
                            "LIVE"
                            if resolved_settings.freqtrade_live_order_send_enabled
                            else "DRY_RUN"
                        )
                    )
                )
            except DomainRejected as exc:
                workers.append(
                    {
                        "name": worker.spec.name,
                        "venue": worker.spec.venue,
                        "backend": "FREQTRADE",
                        "status": "BLOCKED",
                        "reason_code": exc.code,
                        "hip3_dexes": list(worker.spec.hip3_dexes),
                        "order_send": False,
                    }
                )
        return {
            "backend": resolved_settings.execution_backend,
            "workers_enabled": resolved_settings.freqtrade_workers_enabled,
            "direct_venue_send": False,
            "live_order_send": resolved_settings.freqtrade_live_order_send_enabled,
            "workers": workers,
            "as_of": _now().isoformat(),
        }

    def require_freqtrade_live_worker(venue: str) -> FreqtradeWorkerClient:
        if (
            resolved_settings.execution_backend != "FREQTRADE"
            or not resolved_settings.freqtrade_workers_enabled
            or not resolved_settings.freqtrade_live_order_send_enabled
        ):
            raise DomainRejected(
                "FREQTRADE_LIVE_DISABLED",
                "Freqtrade LIVE order send is explicitly disabled",
            )
        worker = next(
            (item for item in resolved_freqtrade_workers if item.spec.venue == venue),
            None,
        )
        if worker is None:
            raise DomainRejected(
                "FREQTRADE_WORKER_NOT_CONFIGURED",
                "the required venue-scoped Freqtrade worker is not configured",
            )
        return worker

    @app.post("/api/intents/{intent_id}/freqtrade/live/send")
    def send_freqtrade_live_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        parts = payload.execution_scope.split(":")
        if len(parts) != 3 or parts[0] != ExecutionEnvironment.LIVE.value:
            raise DomainRejected(
                "FREQTRADE_LIVE_SCOPE_REQUIRED",
                "Freqtrade LIVE sender requires an explicit LIVE scope",
            )
        venue = parts[2].upper()
        worker = require_freqtrade_live_worker(venue)
        now = _now()
        command = service().prepare_freqtrade_live_order(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            hip3_dexes=resolved_settings.hyperliquid_hip3_dexes,
            leverage=resolved_settings.freqtrade_live_leverage,
            now=now,
        )
        worker.probe(expected_mode="LIVE", required_pair=command.pair)
        try:
            if isinstance(command, FreqtradeEntryCommand):
                trade = worker.force_enter(command)
            else:
                assert isinstance(command, FreqtradeExitCommand)
                current = worker.find_open_trade(pair=command.pair)
                if current is None:
                    raise DomainRejected(
                        "FREQTRADE_POSITION_NOT_FOUND",
                        "Freqtrade has no unique open trade for the controlled exit",
                    )
                if current.amount > command.max_quantity:
                    raise DomainRejected(
                        "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                        "Freqtrade open amount exceeds the frozen exit boundary",
                    )
                trade = worker.force_exit(current.trade_id, pair=command.pair)
        except DomainRejected as exc:
            if exc.code in {
                "FREQTRADE_LIVE_OUTCOME_UNKNOWN",
                "FREQTRADE_PROTECTION_UNCONFIRMED",
            }:
                service().record_freqtrade_live_unknown(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    exc.code,
                    now=_now(),
                )
            raise
        fact_id = service().record_freqtrade_live_order(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            command,
            trade,
            now=_now(),
        )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        protection_id = None
        if isinstance(command, FreqtradeEntryCommand):
            protection_id = service().record_freqtrade_live_protection(
                campaign_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                trade,
                now=_now(),
            )
        return {
            "venue_order_fact_id": str(fact_id),
            "protection_id": None if protection_id is None else str(protection_id),
            "backend": "FREQTRADE",
            "environment": "LIVE",
            "worker": worker.spec.name,
            "trade_id": trade.trade_id,
            "pair": trade.pair,
            "is_open": trade.is_open,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/freqtrade/live/protection")
    def sync_freqtrade_live_protection(
        campaign_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        detail = queries().campaign_detail(identity.user_id, campaign_id)
        venue = str(detail["venue"])
        worker = require_freqtrade_live_worker(venue)
        pair = freqtrade_pair(
            venue,
            str(detail["instrument"]["symbol"]),
            hip3_dexes=resolved_settings.hyperliquid_hip3_dexes,
        )
        worker.probe(expected_mode="LIVE", required_pair=pair)
        trade = worker.find_open_trade(pair=pair)
        if trade is None:
            raise DomainRejected(
                "FREQTRADE_POSITION_NOT_FOUND",
                "Freqtrade has no unique open trade to verify protection",
            )
        protection_id = service().record_freqtrade_live_protection(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            trade,
            now=_now(),
        )
        return {
            "protection_id": str(protection_id),
            "backend": "FREQTRADE",
            "environment": "LIVE",
            "worker": worker.spec.name,
            "trade_id": trade.trade_id,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

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
            "data": capital_snapshot(identity.user_id),
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
            "data": capital_snapshot(identity.user_id),
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
            "data": capital_snapshot(identity.user_id),
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
        return {
            "transport": "MOCK_ONLY",
            "scope": "PROPOSAL_REVIEW_ONLY",
            "data": data,
        }

    def handle_real_telegram_action(
        action: TelegramProposalReviewAction,
        update_id: int,
    ) -> str:
        del update_id
        decision = (
            ReviewDecision.APPROVE if action.action == "APPROVE_PROPOSAL" else ReviewDecision.REJECT
        )
        try:
            result = service().review_proposal(
                action.proposal_id,
                action.recipient_id,
                decision,
                "Telegram private-chat review after explicit two-step confirmation",
                expected_version=action.proposal_version,
                now=_now(),
            )
        except DomainRejected as exc:
            return f"未执行: {exc.code}"
        if result is ProposalStatus.PENDING_REVIEW:
            detail = queries().proposal_detail(action.recipient_id, action.proposal_id)
            notify_reviewers(
                action.proposal_id,
                int(detail["version"]),
                str(detail["environment"]),
            )
        return f"审核已记录: {result.value}。未创建授权、订单或资金动作。"

    if isinstance(resolved_telegram, TelegramBotGateway):
        resolved_telegram.set_action_handler(handle_real_telegram_action)

    if WEB_ROOT.exists():
        app.mount("/assets", StaticFiles(directory=WEB_ROOT), name="web-assets")

        @app.get("/manifest.webmanifest", include_in_schema=False)
        def manifest() -> FileResponse:
            return FileResponse(WEB_ROOT / "manifest.webmanifest")

        @app.get("/sw.js", include_in_schema=False)
        def service_worker() -> FileResponse:
            return FileResponse(WEB_ROOT / "sw.js", media_type="application/javascript")

        @app.get("/admin/users", include_in_schema=False)
        def managed_users_web(
            identity: SessionIdentity = identity_dependency,
        ) -> FileResponse:
            require_capability(identity, "access.manage")
            return FileResponse(WEB_ROOT / "index.html")

        @app.get("/", include_in_schema=False)
        @app.get("/opportunities", include_in_schema=False)
        @app.get("/opportunities/defaults", include_in_schema=False)
        @app.get("/proposals/new", include_in_schema=False)
        @app.get("/proposals", include_in_schema=False)
        @app.get("/reviews", include_in_schema=False)
        @app.get("/campaigns", include_in_schema=False)
        @app.get("/campaigns/alerts", include_in_schema=False)
        @app.get("/positions", include_in_schema=False)
        @app.get("/orders", include_in_schema=False)
        @app.get("/risk", include_in_schema=False)
        @app.get("/exceptions", include_in_schema=False)
        @app.get("/capital", include_in_schema=False)
        @app.get("/results", include_in_schema=False)
        @app.get("/venues", include_in_schema=False)
        @app.get("/venues/binance", include_in_schema=False)
        @app.get("/venues/hyperliquid", include_in_schema=False)
        @app.get("/proposals/{proposal_id}", include_in_schema=False)
        @app.get("/campaigns/{campaign_id}", include_in_schema=False)
        def web_app(proposal_id: str | None = None, campaign_id: str | None = None) -> FileResponse:
            del proposal_id
            del campaign_id
            return FileResponse(WEB_ROOT / "index.html")

    return app
