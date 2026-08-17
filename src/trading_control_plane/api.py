from __future__ import annotations

from trading_control_plane.api_core import (
    SESSION_COOKIE,
    SUPPORTED_NOTILT_CHAINS,
    UTC,
    UUID,
    WEB_ROOT,
    Any,
    ApiClientRateLimiter,
    AsyncIterator,
    BinanceCapitalGateway,
    BinancePortfolioMarginClient,
    BinancePortfolioMarginReadOnlyClient,
    BinanceReadOnlyClient,
    BinanceTestnetClient,
    BinanceTestnetOrder,
    BinanceTestnetOrderCommand,
    BinanceTestnetProtectionCommand,
    CampaignNotification,
    CapitalNotification,
    Cookie,
    Database,
    Decimal,
    Depends,
    DomainRejected,
    ExchangeConnectionVerifier,
    ExecutionEnvironment,
    FastAPI,
    FileResponse,
    FreqtradeWorkerClient,
    FreqtradeWorkerSpec,
    Header,
    HTTPException,
    HyperliquidCapitalGateway,
    HyperliquidLiveClient,
    HyperliquidReadOnlyClient,
    HyperliquidTestnetClient,
    HyperliquidTestnetOrder,
    HyperliquidTestnetOrderCommand,
    HyperliquidTestnetProtectionCommand,
    JSONResponse,
    LoginAttemptLimiter,
    MockCapitalTransferAdapter,
    MockTelegramGateway,
    NoTiltGateway,
    NoTiltUsdValuator,
    PasswordHasher,
    PerptapeCandidate,
    PerptapeClient,
    PreparedFreqtradeWorkerBinding,
    ProposalNotification,
    ProposalStatus,
    ReadinessDatabase,
    ReadOnlyExchangeConnectionVerifier,
    Request,
    ReviewDecision,
    SafeSpendingGateway,
    SessionIdentity,
    Settings,
    SignedTokenService,
    StaticFiles,
    TelegramBotGateway,
    TelegramGateway,
    TelegramProposalReviewAction,
    TradingQueries,
    TradingService,
    __version__,
    _domain_status,
    _now,
    asynccontextmanager,
    build_hyperliquid_signer,
    configure_logging,
    datetime,
    get_settings,
    hashlib,
    json,
    perptape_candidate_identity_is_displayable,
    perptape_legacy_candidate_id,
    quote,
    resolve_hyperliquid_main_account,
    status,
    timedelta,
)
from trading_control_plane.api_core import (
    _perptape_runtime_status as _perptape_runtime_status,
)
from trading_control_plane.api_core import (
    _perptape_transport_status as _perptape_transport_status,
)
from trading_control_plane.api_routes.accounts import register_accounts_routes
from trading_control_plane.api_routes.capital import register_capital_routes
from trading_control_plane.api_routes.context import (
    AccountRouteDependencies,
    ApiRouteContext,
    AuthenticatedRouteDependencies,
    CapitalRouteDependencies,
    ExecutionRouteDependencies,
    ProposalRouteDependencies,
    RiskRouteDependencies,
    SignalRouteDependencies,
    SystemRouteDependencies,
    WorkspaceRouteDependencies,
)
from trading_control_plane.api_routes.execution import register_execution_routes
from trading_control_plane.api_routes.proposals import register_proposals_routes
from trading_control_plane.api_routes.risk import register_risk_routes
from trading_control_plane.api_routes.signals import register_signals_routes
from trading_control_plane.api_routes.system import register_system_routes
from trading_control_plane.api_routes.workspace import register_workspace_routes
from trading_control_plane.request_context import (
    ApiClientRequestContext,
    bind_api_client_context,
    reset_api_client_context,
)


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
    hyperliquid_capital_gateway: HyperliquidCapitalGateway | None = None,
    binance_capital_gateway: BinanceCapitalGateway | None = None,
    exchange_connection_verifier: ExchangeConnectionVerifier | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    resolved_settings.validate_runtime_security()
    configure_logging(resolved_settings.log_level)
    resolved_database = database or Database(resolved_settings.database_url)
    token_service = SignedTokenService(resolved_settings.session_signing_secret)
    password_hasher = PasswordHasher()
    login_limiter = LoginAttemptLimiter()
    api_client_limiter = ApiClientRateLimiter()
    resolved_perptape = perptape_client or PerptapeClient(
        base_url=resolved_settings.perptape_base_url,
        api_key=resolved_settings.perptape_api_key,
        contract_version=resolved_settings.perptape_contract_version,
        cache_ttl=timedelta(seconds=resolved_settings.perptape_cache_seconds),
        timeout_seconds=resolved_settings.perptape_timeout_seconds,
    )
    team_perptape_clients: dict[tuple[UUID, int], PerptapeClient] = {}

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
    resolved_hyperliquid_capital = hyperliquid_capital_gateway or HyperliquidCapitalGateway(
        timeout_seconds=5
    )
    resolved_binance_capital = binance_capital_gateway or BinanceCapitalGateway(
        base_url=resolved_settings.binance_capital_base_url,
        api_key=resolved_settings.binance_capital_api_key,
        api_secret=resolved_settings.binance_capital_api_secret,
        recv_window_ms=resolved_settings.binance_recv_window_ms,
        timeout_seconds=resolved_settings.binance_capital_timeout_seconds,
    )
    resolved_exchange_connection_verifier = (
        exchange_connection_verifier or ReadOnlyExchangeConnectionVerifier()
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
        openapi_url="/openapi.json",
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
    app.state.hyperliquid_capital_gateway = resolved_hyperliquid_capital
    app.state.binance_capital_gateway = resolved_binance_capital
    app.state.exchange_connection_verifier = resolved_exchange_connection_verifier

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
                        "FREQTRADE_WORKER_UNAVAILABLE",
                        "FREQTRADE_LIVE_OUTCOME_UNKNOWN",
                        "FREQTRADE_PROTECTION_UNCONFIRMED",
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
            credential_encryption_key=resolved_settings.credential_encryption_key,
        )

    def freqtrade_client_for_binding(
        binding: PreparedFreqtradeWorkerBinding,
    ) -> FreqtradeWorkerClient:
        exact = next(
            (
                worker
                for worker in resolved_freqtrade_workers
                if worker.spec.matches_scope(
                    team_id=str(binding.team_id),
                    account_id=binding.account_id,
                    venue=binding.venue,
                )
                and worker.spec.exchange_account_id == str(binding.exchange_account_id)
                and worker.spec.name == binding.worker_name
                and worker.spec.base_url == binding.worker_url
            ),
            None,
        )
        if exact is not None:
            return exact
        return FreqtradeWorkerClient(
            FreqtradeWorkerSpec(
                name=binding.worker_name,
                venue=binding.venue,  # type: ignore[arg-type]
                base_url=binding.worker_url,
                username=binding.username,
                password=binding.password,
                hip3_dexes=binding.hip3_dexes,
                exchange_account_id=str(binding.exchange_account_id),
                team_id=str(binding.team_id),
                account_id=binding.account_id,
            ),
            timeout_seconds=resolved_settings.freqtrade_timeout_seconds,
            confirmation_timeout_seconds=(resolved_settings.freqtrade_confirmation_timeout_seconds),
        )

    def effective_direct_capital_settings(
        user_id: UUID,
        environment: str = "LIVE",
    ) -> tuple[Settings, dict[str, Any] | None]:
        config = service().direct_capital_configuration(
            user_id,
            environment,
            include_sensitive_addresses=True,
        )
        if config is None:
            return resolved_settings, None
        return (
            resolved_settings.model_copy(
                update={
                    "capital_direct_network": config["network"],
                    "capital_direct_asset": config["asset"],
                    "capital_direct_treasury_provider": config["treasury_provider"],
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
        selected_provider = direct_settings.capital_direct_treasury_provider
        configured_notilt_address = (
            direct_settings.capital_direct_vault_address or configured_vault
        )
        configured_safe_address = direct_settings.capital_direct_safe_address
        selected_treasury_account_id = (
            configured_safe_address
            if selected_provider == "SAFE_SPENDING_LIMIT"
            else configured_notilt_address
        )
        safe_scope_ready = (
            direct_settings.safe_spending_enabled
            and direct_settings.safe_spending_arbitrum_rpc_url is not None
            and direct_settings.capital_direct_safe_address is not None
            and direct_settings.capital_direct_safe_delegate_address is not None
        )
        notilt_scope_ready = (
            direct_settings.notilt_enabled
            and direct_settings.notilt_agent_address is not None
            and configured_notilt_address is not None
        )
        selected_scope_ready = (
            safe_scope_ready if selected_provider == "SAFE_SPENDING_LIMIT" else notilt_scope_ready
        )
        onchain_probe: dict[str, Any] = {
            "provider": selected_provider,
            "status": "NOT_ATTEMPTED" if selected_scope_ready else "BLOCKED",
            "error_code": (
                "SAFE_SPENDING_LIMIT_NOT_CONFIGURED"
                if selected_provider == "SAFE_SPENDING_LIMIT" and not safe_scope_ready
                else "NOTILT_VAULT_NOT_CONFIGURED"
                if selected_provider == "NOTILT_VAULT" and not notilt_scope_ready
                else None
            ),
        }
        if selected_provider == "SAFE_SPENDING_LIMIT" and safe_scope_ready:
            safe_rpc_url = direct_settings.safe_spending_arbitrum_rpc_url
            safe_address = direct_settings.capital_direct_safe_address
            safe_delegate = direct_settings.capital_direct_safe_delegate_address
            assert safe_rpc_url is not None
            assert safe_address is not None
            assert safe_delegate is not None
            try:
                safe_fact = resolved_safe_spending.read_limit(
                    rpc_url=safe_rpc_url,
                    safe=safe_address,
                    delegate=safe_delegate,
                )
                scale = Decimal(10) ** 6
                observed_at = datetime.fromtimestamp(int(str(safe_fact["blockTimestamp"])), UTC)
                service().record_safe_spending_snapshot(
                    actor_id=user_id,
                    safe_address=safe_address,
                    asset="USDC",
                    balance=Decimal(str(safe_fact["balance"])) / scale,
                    available_limit=Decimal(str(safe_fact["available"])) / scale,
                    module_enabled=bool(safe_fact["moduleEnabled"]),
                    observed_at=observed_at,
                    now=_now(),
                )
            except DomainRejected as exc:
                onchain_probe.update(status="FAILED", error_code=exc.code)
            except (KeyError, TypeError, ValueError, ArithmeticError):
                onchain_probe.update(status="FAILED", error_code="SAFE_RESPONSE_INVALID")
            else:
                onchain_probe.update(status="SUCCESS", error_code=None)
        snapshot = queries().capital_center(
            user_id,
            authoritative_live_accounts=authoritative_live_accounts(),
            authoritative_live_treasury_account_id=selected_treasury_account_id,
            require_authoritative_live_treasury=True,
        )
        expected_interval = resolved_settings.runtime_sync_interval_seconds
        snapshot["net_worth"]["history_expected_interval_seconds"] = expected_interval
        snapshot["net_worth"]["history_gap_tolerance_seconds"] = max(
            180,
            expected_interval * 3,
        )
        snapshot["net_worth"]["onchain_provider"] = selected_provider
        snapshot["net_worth"]["onchain_probe"] = onchain_probe
        can_manage_direct_configuration = service().can_user(user_id, "access.manage")
        snapshot["direct_configuration"] = {
            "single_account_mode": False,
            "source": "VERSIONED_DATABASE" if saved_config is not None else "ENVIRONMENT",
            "version": None if saved_config is None else saved_config["version"],
            "effective_at": None if saved_config is None else saved_config["effective_at"],
            "updated_by_username": (
                None if saved_config is None else saved_config["updated_by_username"]
            ),
            "can_manage": can_manage_direct_configuration,
            "asset": direct_settings.capital_direct_asset,
            "network": direct_settings.capital_direct_network,
            "treasury_provider": direct_settings.capital_direct_treasury_provider,
            "configured_providers": [
                provider
                for provider, configured in (
                    ("NOTILT_VAULT", configured_notilt_address is not None),
                    ("SAFE_SPENDING_LIMIT", configured_safe_address is not None),
                )
                if configured
            ],
            "selected_onchain_account_configured": selected_treasury_account_id is not None,
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
            "binance_capital_credentials_configured": resolved_binance_capital.configured,
            "binance_capital_submission_enabled": (
                direct_settings.binance_capital_withdraw_enabled
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
                and configured_notilt_address is not None
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

    def require_registered_or_default_venue_account(
        identity: SessionIdentity,
        account_id: str,
        venue: str,
    ) -> None:
        registry = queries().exchange_accounts(identity.user_id)
        registered = any(
            item["account_id"] == account_id and item["venue"] == venue for item in registry["data"]
        )
        if not registered:
            # Preserve the legacy single-account boundary until this venue is
            # represented by the database-backed account registry.
            require_default_venue_account(account_id, venue)
        require_capability(identity, "venue.view", account_id, venue)

    async def current_identity(
        trading_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
        authorization: str | None = Header(default=None),
    ) -> AsyncIterator[SessionIdentity]:
        if trading_session is not None and authorization is not None:
            raise DomainRejected(
                "AUTH_CREDENTIAL_AMBIGUOUS",
                "send either a login session or one Bearer credential, not both",
            )
        if authorization is not None:
            scheme, separator, credential = authorization.partition(" ")
            if (
                separator != " "
                or scheme.casefold() != "bearer"
                or not credential
                or credential.strip() != credential
                or " " in credential
            ):
                raise DomainRejected("AGENT_TOKEN_INVALID", "agent API credential is invalid")
            now = _now()
            authenticated = service().authenticate_api_client_token(credential, now=now)
            retry_after = api_client_limiter.consume(
                str(authenticated["api_client_id"]),
                now=now,
            )
            if retry_after is not None:
                raise DomainRejected(
                    "API_CLIENT_RATE_LIMITED",
                    f"API Key request rate exceeded; retry after {retry_after} seconds",
                )
            api_client_id = UUID(str(authenticated["api_client_id"]))
            workspace_id = UUID(str(authenticated["workspace_id"]))
            team_id = UUID(str(authenticated["team_id"]))
            identity = SessionIdentity(
                user_id=UUID(str(authenticated["user_id"])),
                username=str(authenticated["username"]),
                expires_at=authenticated["expires_at"],
                authentication_method="api-client-token-v1",
                auth_version=int(authenticated["auth_version"]),
                api_client_id=api_client_id,
                api_client_name=str(authenticated["api_client_name"]),
                workspace_id=workspace_id,
                team_id=team_id,
            )
            request_context = ApiClientRequestContext(
                owner_user_id=identity.user_id,
                api_client_id=api_client_id,
                workspace_id=workspace_id,
                team_id=team_id,
            )
            context_token = bind_api_client_context(request_context)
            try:
                yield identity
            finally:
                reset_api_client_context(context_token)
            return
        if trading_session is None:
            raise DomainRejected("LOGIN_DENIED", "an internal login session is required")
        identity = token_service.verify_session(trading_session, now=_now())
        context = queries().user_context(identity.user_id)
        if int(context["auth_version"]) != identity.auth_version:
            raise DomainRejected("SESSION_REVOKED", "login session was revoked")
        yield identity

    def is_agent_identity(identity: SessionIdentity) -> bool:
        return identity.api_client_id is not None

    identity_dependency = Depends(current_identity)

    def notify_reviewers(
        proposal_id: UUID, proposal_version: int, environment: str = "TESTNET"
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
        actor_id: UUID,
        team_id: UUID,
        environment: str,
        account_id: str,
        venue: str,
        object_version: int,
        summary: str,
    ) -> None:
        notification_now = _now()
        service().enqueue_capital_status_notification(
            actor_id=actor_id,
            team_id=team_id,
            object_id=object_id,
            object_type=object_type,
            status=event_type,
            environment=environment,
            account_id=account_id,
            venue=venue,
            object_version=object_version,
            summary=summary,
            now=notification_now,
        )
        for recipient in queries().treasury_users(team_id, account_id, venue):
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
                    created_at=notification_now,
                )
            )

    def current_perptape_candidates(*, user_id: UUID, now: datetime) -> list[PerptapeCandidate]:
        runtime = service().perptape_source_runtime(user_id)
        if resolved_settings.runtime_sync_enabled:
            feed = queries().perptape_feed(user_id)
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
        team_api_key = runtime["api_key"]
        if team_api_key is not None:
            cache_key = (UUID(str(runtime["signal_source_id"])), int(runtime["version"]))
            client = team_perptape_clients.get(cache_key)
            if client is None:
                client = resolved_perptape.with_api_key(str(team_api_key))
                for stale_key in tuple(team_perptape_clients):
                    if stale_key[0] == cache_key[0]:
                        team_perptape_clients.pop(stale_key, None)
                team_perptape_clients[cache_key] = client
            return client.list_candidates(now=now)
        return resolved_perptape.list_candidates(now=now)

    def current_perptape_candidate(
        candidate_id: str, *, user_id: UUID, now: datetime
    ) -> PerptapeCandidate:
        candidates = current_perptape_candidates(user_id=user_id, now=now)
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
        source_candidates = current_perptape_candidates(user_id=user_id, now=now)
        candidates = [
            candidate
            for candidate in source_candidates
            if perptape_candidate_identity_is_displayable(candidate)
        ]
        feed = queries().perptape_feed(user_id) if resolved_settings.runtime_sync_enabled else None
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

    def notify_campaign(
        recipient_id: UUID,
        campaign_id: UUID,
        event_type: str,
        event_key: str,
        summary: str,
        environment: str = "TESTNET",
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

    def require_binance_testnet() -> None:
        if not resolved_settings.binance_testnet_order_send_enabled:
            raise DomainRejected(
                "BINANCE_TESTNET_DISABLED", "Binance testnet order send is explicitly disabled"
            )
        if binance_testnet_client is not None and not resolved_binance_testnet.configured:
            raise DomainRejected(
                "BINANCE_TESTNET_NOT_CONFIGURED", "Binance testnet credentials are not configured"
            )

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

    def require_hyperliquid_testnet() -> None:
        if not resolved_settings.hyperliquid_testnet_order_send_enabled:
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_DISABLED",
                "Hyperliquid Core testnet order send is explicitly disabled",
            )
        if hyperliquid_testnet_client is not None and not resolved_hyperliquid_testnet.configured:
            raise DomainRejected(
                "HYPERLIQUID_TESTNET_NOT_CONFIGURED",
                "Hyperliquid testnet account and injected signer are not configured",
            )

    def database_bound_venue_facts(
        venue: str,
        account_id: str,
        identity: SessionIdentity,
    ) -> dict[str, Any]:
        require_capability(identity, "venue.view", account_id, venue)
        return {
            "mode": "DATABASE_BOUND_READ_ONLY",
            "domain": "USDT_LINEAR_PERPETUALS",
            "data": queries().venue_facts(
                identity.user_id,
                account_id,
                venue,
                ExecutionEnvironment.LIVE.value,
            ),
            "as_of": _now().isoformat(),
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

    def require_freqtrade_live_enabled() -> None:
        if (
            resolved_settings.execution_backend != "FREQTRADE"
            or not resolved_settings.freqtrade_workers_enabled
            or not resolved_settings.freqtrade_live_order_send_enabled
        ):
            raise DomainRejected(
                "FREQTRADE_LIVE_DISABLED",
                "Freqtrade LIVE order send is explicitly disabled",
            )

    def require_freqtrade_live_worker(
        binding: PreparedFreqtradeWorkerBinding,
    ) -> FreqtradeWorkerClient:
        require_freqtrade_live_enabled()
        return freqtrade_client_for_binding(binding)

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

    authenticated_dependencies = AuthenticatedRouteDependencies(
        identity=identity_dependency,
        queries=queries,
        service=service,
        settings=resolved_settings,
        require_capability=require_capability,
    )
    route_context = ApiRouteContext(
        app=app,
        system=SystemRouteDependencies(database=resolved_database),
        workspace=WorkspaceRouteDependencies(
            common=authenticated_dependencies,
            configured_risk_scopes=configured_risk_scopes,
            is_agent_identity=is_agent_identity,
            login_limiter=login_limiter,
            password_hasher=password_hasher,
            telegram=resolved_telegram,
            token_service=token_service,
        ),
        accounts=AccountRouteDependencies(
            common=authenticated_dependencies,
            database_bound_venue_facts=database_bound_venue_facts,
            freqtrade_client_for_binding=freqtrade_client_for_binding,
            require_binance_testnet=require_binance_testnet,
            require_default_venue_account=require_default_venue_account,
            require_registered_or_default_venue_account=(
                require_registered_or_default_venue_account
            ),
            binance=resolved_binance,
            binance_live=resolved_binance_live,
            binance_testnet=resolved_binance_testnet,
            binance_testnet_reader=resolved_binance_testnet_reader,
            exchange_connection_verifier=resolved_exchange_connection_verifier,
            hyperliquid=resolved_hyperliquid,
            hyperliquid_live=resolved_hyperliquid_live,
            hyperliquid_testnet=resolved_hyperliquid_testnet,
        ),
        signals=SignalRouteDependencies(
            common=authenticated_dependencies,
            current_perptape_candidates=current_perptape_candidates,
            notify_reviewers=notify_reviewers,
        ),
        proposals=ProposalRouteDependencies(
            common=authenticated_dependencies,
            current_perptape_candidate=current_perptape_candidate,
            is_agent_identity=is_agent_identity,
            notify_reviewers=notify_reviewers,
            opportunity_snapshot=opportunity_snapshot,
            token_service=token_service,
        ),
        risk=RiskRouteDependencies(
            common=authenticated_dependencies,
            configured_risk_scopes=configured_risk_scopes,
            token_service=token_service,
        ),
        execution=ExecutionRouteDependencies(
            common=authenticated_dependencies,
            current_perptape_candidate=current_perptape_candidate,
            current_perptape_candidates=current_perptape_candidates,
            notify_campaign=notify_campaign,
            rejected_hyperliquid_order=rejected_hyperliquid_order,
            rejected_testnet_order=rejected_testnet_order,
            require_binance_live=require_binance_live,
            require_binance_testnet=require_binance_testnet,
            require_freqtrade_live_enabled=require_freqtrade_live_enabled,
            require_freqtrade_live_worker=require_freqtrade_live_worker,
            require_hyperliquid_live=require_hyperliquid_live,
            require_hyperliquid_testnet=require_hyperliquid_testnet,
            binance_live=resolved_binance_live,
            binance_testnet=resolved_binance_testnet,
            binance_testnet_uses_database_credentials=(binance_testnet_client is None),
            freqtrade_workers=resolved_freqtrade_workers,
            hyperliquid=resolved_hyperliquid,
            hyperliquid_live=resolved_hyperliquid_live,
            hyperliquid_testnet=resolved_hyperliquid_testnet,
            hyperliquid_testnet_uses_database_credentials=(hyperliquid_testnet_client is None),
            notilt=resolved_notilt,
            telegram=resolved_telegram,
            unknown_hyperliquid_protection=unknown_hyperliquid_protection,
            unknown_testnet_protection=unknown_testnet_protection,
        ),
        capital=CapitalRouteDependencies(
            common=authenticated_dependencies,
            capital_snapshot=capital_snapshot,
            configured_notilt_scope=configured_notilt_scope,
            effective_direct_capital_settings=effective_direct_capital_settings,
            notify_capital=notify_capital,
            notilt_chain_id_for_network=notilt_chain_id_for_network,
            binance_capital=resolved_binance_capital,
            capital_transfer=resolved_capital_transfer,
            hyperliquid_capital=resolved_hyperliquid_capital,
            notilt=resolved_notilt,
            safe_spending=resolved_safe_spending,
            sync_configured_notilt_vault=sync_configured_notilt_vault,
            token_service=token_service,
            verify_live_notilt_release_budget=verify_live_notilt_release_budget,
        ),
    )
    register_system_routes(route_context)
    register_workspace_routes(route_context)
    register_accounts_routes(route_context)
    register_signals_routes(route_context)
    register_proposals_routes(route_context)
    register_risk_routes(route_context)
    register_execution_routes(route_context)
    register_capital_routes(route_context)

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

        @app.get("/docs/API_KEY_QUICKSTART.md", include_in_schema=False)
        @app.get("/docs/AI_API_QUICKSTART.md", include_in_schema=False)
        def api_key_quickstart() -> FileResponse:
            packaged_quickstart = WEB_ROOT / "API_KEY_QUICKSTART.md"
            source_quickstart = WEB_ROOT.parents[2] / "docs" / "API_KEY_QUICKSTART.md"
            return FileResponse(
                packaged_quickstart if packaged_quickstart.is_file() else source_quickstart,
                media_type="text/markdown; charset=utf-8",
            )

        @app.get("/", include_in_schema=False)
        @app.get("/home", include_in_schema=False)
        @app.get("/workspaces", include_in_schema=False)
        @app.get("/profile/api-keys", include_in_schema=False)
        @app.get("/profile/api-access", include_in_schema=False)
        @app.get("/admin/agents", include_in_schema=False)
        @app.get("/admin/users", include_in_schema=False)
        @app.get("/opportunities", include_in_schema=False)
        @app.get("/webhook-signals", include_in_schema=False)
        @app.get("/signals", include_in_schema=False)
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
        @app.get("/notifications", include_in_schema=False)
        @app.get("/accounts", include_in_schema=False)
        @app.get("/team-settings", include_in_schema=False)
        @app.get("/trading-mode", include_in_schema=False)
        @app.get("/venues", include_in_schema=False)
        @app.get("/venues/binance", include_in_schema=False)
        @app.get("/venues/hyperliquid", include_in_schema=False)
        @app.get("/venues/{account_id}", include_in_schema=False)
        @app.get("/proposals/{proposal_id}", include_in_schema=False)
        @app.get("/campaigns/{campaign_id}", include_in_schema=False)
        def web_app(
            proposal_id: str | None = None,
            campaign_id: str | None = None,
            account_id: str | None = None,
        ) -> FileResponse:
            del proposal_id
            del campaign_id
            del account_id
            return FileResponse(WEB_ROOT / "index.html")

    return app
