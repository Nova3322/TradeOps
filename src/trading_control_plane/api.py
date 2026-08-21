from __future__ import annotations

from trading_control_plane.adapters.capital import (
    CapitalAdapterFactory,
    build_production_capital_adapter_factory,
)
from trading_control_plane.api_core import (
    SESSION_COOKIE,
    UUID,
    WEB_ROOT,
    Any,
    ApiClientRateLimiter,
    AsyncIterator,
    CampaignNotification,
    CapitalNotification,
    Cookie,
    Database,
    Depends,
    DomainRejected,
    ExchangeConnectionVerifier,
    FactAdapterConnectionProbe,
    FastAPI,
    FileResponse,
    FreqtradeWorkerClient,
    FreqtradeWorkerSpec,
    Header,
    HTTPException,
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
    configure_logging,
    datetime,
    get_settings,
    hashlib,
    json,
    perptape_candidate_identity_is_displayable,
    perptape_legacy_candidate_id,
    quote,
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
from trading_control_plane.binance_errors import BinanceRequestState
from trading_control_plane.binance_state import DatabaseBinanceRequestState
from trading_control_plane.capital_application import CapitalApplicationRuntime
from trading_control_plane.capital_configuration_use_cases import (
    CapitalConfigurationUseCases,
)
from trading_control_plane.capital_direct_use_cases import CapitalDirectUseCases
from trading_control_plane.capital_receipt_use_cases import CapitalReceiptUseCases
from trading_control_plane.capital_transfer_use_cases import CapitalTransferUseCases
from trading_control_plane.exchange_connection_verification import ExchangeConnectionVerification
from trading_control_plane.perptape import PerptapeFeedSnapshot
from trading_control_plane.request_context import (
    ApiClientRequestContext,
    bind_api_client_context,
    reset_api_client_context,
)
from trading_control_plane.service_domains.proposal_automation import (
    advance_approved_proposal,
)


def create_app(
    settings: Settings | None = None,
    database: ReadinessDatabase | None = None,
    perptape_client: PerptapeClient | None = None,
    telegram_gateway: TelegramGateway | None = None,
    freqtrade_workers: tuple[FreqtradeWorkerClient, ...] | None = None,
    capital_transfer_adapter: MockCapitalTransferAdapter | None = None,
    notilt_gateway: NoTiltGateway | None = None,
    notilt_valuator: NoTiltUsdValuator | None = None,
    safe_spending_gateway: SafeSpendingGateway | None = None,
    capital_adapter_factory: CapitalAdapterFactory | None = None,
    exchange_connection_verifier: ExchangeConnectionVerifier | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    resolved_settings.validate_runtime_security()
    configure_logging(resolved_settings.log_level)
    resolved_database = database or Database(resolved_settings.database_url)
    binance_request_state = (
        DatabaseBinanceRequestState(resolved_database)
        if isinstance(resolved_database, Database)
        else BinanceRequestState()
    )
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
                    account_id=str(item["account_id"]),
                    venue=str(item["venue"]),
                    order_type="MARKET",
                    estimated_notional=(
                        None
                        if item["estimated_notional"] is None
                        else str(item["estimated_notional"])
                    ),
                    quote_currency=(
                        None if item["quote_currency"] is None else str(item["quote_currency"])
                    ),
                    collateral_currency=(
                        None
                        if item["collateral_currency"] is None
                        else str(item["collateral_currency"])
                    ),
                    leverage=str(resolved_settings.freqtrade_live_leverage),
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
    resolved_freqtrade_workers = freqtrade_workers or ()
    resolved_capital_transfer = capital_transfer_adapter or MockCapitalTransferAdapter()
    resolved_notilt = notilt_gateway or NoTiltGateway(
        timeout_seconds=resolved_settings.notilt_gateway_timeout_seconds
    )
    resolved_notilt_valuator = notilt_valuator or NoTiltUsdValuator()
    resolved_safe_spending = safe_spending_gateway or SafeSpendingGateway(
        timeout_seconds=resolved_settings.safe_spending_gateway_timeout_seconds
    )

    def database_capital_credentials(scope: Any) -> dict[str, str]:
        if not isinstance(resolved_database, Database):
            raise DomainRejected(
                "CAPITAL_ACCOUNT_CREDENTIALS_NOT_READY",
                "database-backed capital credentials require the durable Trading database",
            )
        binding = TradingService(
            resolved_database,
            credential_encryption_key=resolved_settings.credential_encryption_key,
        ).verified_capital_account_binding(
            workspace_id=UUID(scope.workspace_id),
            team_id=UUID(scope.team_id),
            account_id=scope.account_id,
            venue=scope.venue,
            environment=scope.environment,
        )
        return binding.credentials

    resolved_capital_adapter_factory = (
        capital_adapter_factory
        or build_production_capital_adapter_factory(
            binance_account_id=resolved_settings.binance_capital_account_id,
            binance_api_key=resolved_settings.binance_capital_api_key,
            binance_api_secret=resolved_settings.binance_capital_api_secret,
            binance_base_url=resolved_settings.binance_capital_base_url,
            binance_recv_window_ms=resolved_settings.binance_recv_window_ms,
            binance_timeout_seconds=resolved_settings.binance_capital_timeout_seconds,
            binance_request_state=binance_request_state,
            credential_resolver=database_capital_credentials,
        )
    )
    resolved_exchange_connection_verifier = (
        exchange_connection_verifier
        or FactAdapterConnectionProbe(
            bootstrap_symbols={
                "BINANCE": resolved_settings.runtime_binance_symbol,
                "HYPERLIQUID": resolved_settings.runtime_hyperliquid_symbol,
                "OKX": resolved_settings.runtime_okx_symbol,
                "BYBIT": resolved_settings.runtime_bybit_symbol,
            },
            binance_request_state=binance_request_state,
        )
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
    app.state.freqtrade_workers = resolved_freqtrade_workers
    app.state.capital_transfer_adapter = resolved_capital_transfer
    app.state.notilt_gateway = resolved_notilt
    app.state.notilt_valuator = resolved_notilt_valuator
    app.state.safe_spending_gateway = resolved_safe_spending
    app.state.capital_adapter_factory = resolved_capital_adapter_factory
    app.state.exchange_connection_verifier = resolved_exchange_connection_verifier

    @app.exception_handler(DomainRejected)
    async def domain_rejected(_: Request, exc: DomainRejected) -> JSONResponse:
        return JSONResponse(
            status_code=_domain_status(exc.code),
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.detail,
                    **({"details": exc.metadata} if exc.metadata is not None else {}),
                    "retryable": exc.code
                    in {
                        "PERPTAPE_UNAVAILABLE",
                        "PERPTAPE_RATE_LIMITED",
                        "PERPTAPE_CACHE_UNAVAILABLE",
                        "PERPTAPE_CACHE_STALE",
                        "BINANCE_READ_ONLY_UNAVAILABLE",
                        "BINANCE_RATE_LIMITED",
                        "BINANCE_CONNECTION_RETRY_DEFERRED",
                        "BINANCE_CAPITAL_RATE_LIMITED",
                        "BINANCE_CONNECTION_WEIGHT_HEADROOM_DEFERRED",
                        "BINANCE_CAPITAL_WEIGHT_HEADROOM_DEFERRED",
                        "BINANCE_RECEIPT_CHECK_IN_PROGRESS",
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

    def service() -> TradingService:
        return TradingService(
            business_database(),
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
                ws_token=binding.ws_token,
                hip3_dexes=binding.hip3_dexes,
                exchange_account_id=str(binding.exchange_account_id),
                team_id=str(binding.team_id),
                account_id=binding.account_id,
            ),
            timeout_seconds=resolved_settings.freqtrade_timeout_seconds,
            confirmation_timeout_seconds=(resolved_settings.freqtrade_confirmation_timeout_seconds),
        )

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

    def configured_risk_scopes(actor_id: UUID) -> tuple[tuple[str, str, str], ...]:
        return queries().configured_risk_scopes(actor_id)

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
                    account_id=str(detail["account_id"]),
                    venue=str(detail["venue"]),
                    order_type="MARKET",
                    estimated_notional=(
                        None
                        if detail["estimated_notional"] is None
                        else str(detail["estimated_notional"])
                    ),
                    quote_currency=(
                        None
                        if detail["quote_currency"] is None
                        else str(detail["quote_currency"])
                    ),
                    collateral_currency=(
                        None
                        if detail["collateral_currency"] is None
                        else str(detail["collateral_currency"])
                    ),
                    leverage=str(resolved_settings.freqtrade_live_leverage),
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

    def current_persisted_perptape_feed(
        *, user_id: UUID, now: datetime
    ) -> PerptapeFeedSnapshot | None:
        feed = queries().perptape_feed(user_id)
        if feed is None:
            return None
        grace = timedelta(
            seconds=(
                resolved_settings.runtime_sync_interval_seconds
                + int(resolved_settings.perptape_timeout_seconds)
                + 30
            )
        )
        if (
            feed.contract_version == resolved_settings.perptape_contract_version
            and now <= feed.next_allowed_at + grace
        ):
            return feed
        if resolved_settings.runtime_sync_enabled:
            raise DomainRejected(
                "PERPTAPE_CACHE_STALE",
                "runtime Perptape feed is stale or uses another contract version",
            )
        return None

    def current_perptape_candidates(*, user_id: UUID, now: datetime) -> list[PerptapeCandidate]:
        runtime = service().perptape_source_runtime(user_id)
        feed = current_persisted_perptape_feed(user_id=user_id, now=now)
        if feed is not None:
            return list(feed.candidates)
        if resolved_settings.runtime_sync_enabled:
            raise DomainRejected(
                "PERPTAPE_CACHE_UNAVAILABLE",
                "runtime sync has not recorded a Perptape feed",
            )
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
        feed = current_persisted_perptape_feed(user_id=user_id, now=now)
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

    def require_freqtrade_enabled() -> None:
        if not resolved_settings.freqtrade_workers_enabled:
            raise DomainRejected(
                "FREQTRADE_EXECUTION_DISABLED",
                "Freqtrade execution is explicitly disabled",
            )

    def require_freqtrade_worker(
        binding: PreparedFreqtradeWorkerBinding,
    ) -> FreqtradeWorkerClient:
        require_freqtrade_enabled()
        return freqtrade_client_for_binding(binding)

    def handle_real_telegram_action(
        action: TelegramProposalReviewAction,
        update_id: int,
    ) -> str:
        del update_id
        now = _now()
        decision = (
            ReviewDecision.APPROVE if action.action == "APPROVE_PROPOSAL" else ReviewDecision.REJECT
        )
        workflow_service = service()
        try:
            result = workflow_service.review_proposal(
                action.proposal_id,
                action.recipient_id,
                decision,
                "Telegram private-chat review after explicit two-step confirmation",
                expected_version=action.proposal_version,
                automatic_risk_service_username=(
                    resolved_settings.runtime_sync_service_username
                    if decision is ReviewDecision.APPROVE
                    else None
                ),
                now=now,
            )
        except DomainRejected as exc:
            return f"未执行: {exc.code}"
        automation: dict[str, str | None] | None = None
        if result is ProposalStatus.APPROVED:
            try:
                automation = advance_approved_proposal(
                    workflow_service,
                    proposal_id=action.proposal_id,
                    fallback_service_username=(
                        resolved_settings.runtime_sync_service_username
                    ),
                    now=now,
                )
            except DomainRejected as exc:
                automation = {"status": "BLOCKED", "error_code": exc.code}
        if result is ProposalStatus.PENDING_REVIEW:
            detail = queries().proposal_detail(action.recipient_id, action.proposal_id)
            notify_reviewers(
                action.proposal_id,
                int(detail["version"]),
                str(detail["environment"]),
            )
        detail = queries().proposal_detail(action.recipient_id, action.proposal_id)
        risk = detail.get("risk_decision")
        risk_copy = ""
        if result is ProposalStatus.APPROVED and isinstance(risk, dict):
            risk_copy = f" 系统风控已自动运行: {risk['result']}。"
        workflow_copy = ""
        if automation is not None and automation.get("status") == "READY":
            workflow_copy = " 已自动签发短期授权、预留风险并创建受控交易任务。"
        elif automation is not None and automation.get("status") == "RISK_DENIED":
            workflow_copy = " 风控拒绝, 未创建授权或交易任务。"
        elif automation is not None:
            workflow_copy = f" 自动流程已安全阻断: {automation.get('error_code')}。"
        return (
            f"审核已记录: {result.value}。{risk_copy}{workflow_copy} "
            "交易所发送仅由受控执行进程在 Gate、租约与幂等边界内自动推进; "
            "审核 Bot 本身不直接发送订单或资金动作。"
        )

    authenticated_dependencies = AuthenticatedRouteDependencies(
        identity=identity_dependency,
        queries=queries,
        service=service,
        settings=resolved_settings,
        require_capability=require_capability,
    )
    capital_runtime = CapitalApplicationRuntime(
        settings=resolved_settings,
        queries=queries,
        service=service,
        clock=_now,
        notify_capital=notify_capital,
        token_service=token_service,
        adapter_resolver=resolved_capital_adapter_factory,
        transfer_adapter=resolved_capital_transfer,
        notilt=resolved_notilt,
        notilt_valuator=resolved_notilt_valuator,
        safe_spending=resolved_safe_spending,
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
            freqtrade_client_for_binding=freqtrade_client_for_binding,
            connection_verification=ExchangeConnectionVerification(
                resolved_exchange_connection_verifier
            ),
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
            require_freqtrade_enabled=require_freqtrade_enabled,
            require_freqtrade_worker=require_freqtrade_worker,
            freqtrade_workers=resolved_freqtrade_workers,
            notilt=resolved_notilt,
            telegram=resolved_telegram,
        ),
        capital=CapitalRouteDependencies(
            common=authenticated_dependencies,
            configuration=CapitalConfigurationUseCases(capital_runtime),
            direct=CapitalDirectUseCases(capital_runtime),
            receipts=CapitalReceiptUseCases(capital_runtime),
            transfers=CapitalTransferUseCases(capital_runtime),
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
        @app.get("/system", include_in_schema=False)
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
