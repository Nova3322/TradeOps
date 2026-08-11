from __future__ import annotations

from collections.abc import Callable
from typing import cast

from trading_control_plane.api_core import (
    UUID,
    Any,
    BinanceReadOnlySyncRequest,
    DomainRejected,
    ExchangeAccountCreateRequest,
    ExchangeConnectionVerifyRequest,
    ExchangeCredentialRotateRequest,
    ExchangeRuntimeSyncRequest,
    ExchangeTradingEligibilityRequest,
    ExecutionEnvironment,
    FreqtradeWorkerConfigureRequest,
    FreqtradeWorkerVerifyRequest,
    HyperliquidReadOnlySyncRequest,
    SessionIdentity,
    _now,
)
from trading_control_plane.api_routes.context import ApiRouteContext


def register_accounts_routes(context: ApiRouteContext) -> None:
    """Register accounts routes against one application dependency context."""

    app = context.app
    database_bound_venue_facts = context.require("database_bound_venue_facts")
    freqtrade_client_for_binding = context.require("freqtrade_client_for_binding")
    identity_dependency = context.require("identity_dependency")
    queries = context.require("queries")
    require_binance_testnet = context.require("require_binance_testnet")
    require_capability = context.require("require_capability")
    require_default_venue_account = context.require("require_default_venue_account")
    require_registered_or_default_venue_account = context.require(
        "require_registered_or_default_venue_account"
    )
    resolved_binance = context.require("resolved_binance")
    resolved_binance_live = context.require("resolved_binance_live")
    resolved_binance_testnet = context.require("resolved_binance_testnet")
    resolved_binance_testnet_reader = context.require("resolved_binance_testnet_reader")
    resolved_exchange_connection_verifier = context.require("resolved_exchange_connection_verifier")
    resolved_hyperliquid = context.require("resolved_hyperliquid")
    resolved_hyperliquid_live = context.require("resolved_hyperliquid_live")
    resolved_hyperliquid_testnet = context.require("resolved_hyperliquid_testnet")
    resolved_settings = context.require("resolved_settings")
    service = context.require("service")

    def reconciled_venue_sync(
        *,
        identity: SessionIdentity,
        account_id: str,
        venue: str,
        environment: str,
        symbol: str,
        source: str,
        persist: Callable[[Any], dict[str, Any]],
        observed_at: str | None = None,
        mode: str | None = None,
        domain: str | None = None,
        include_execution_scope: bool = True,
    ) -> dict[str, Any]:
        """Persist, reconcile, and project one read-only venue snapshot atomically by scope."""

        now = _now()
        persisted = persist(now)
        execution_scope = f"{environment}:{account_id}:{venue}"
        reconciliation_id = service().reconcile_scope(
            execution_scope,
            identity.user_id,
            now=now,
        )
        reconciliation = {
            "reconciliation_id": str(reconciliation_id),
            "status": service().reconciliation_status(reconciliation_id).value,
        }
        if include_execution_scope:
            reconciliation["execution_scope"] = execution_scope
        result: dict[str, Any] = {
            "source": source,
            "environment": environment,
            "symbol": symbol,
            "persisted": persisted,
            "reconciliation": reconciliation,
            "facts": queries().venue_facts(
                identity.user_id,
                account_id,
                venue,
                environment,
            ),
        }
        if observed_at is not None:
            result["observed_at"] = observed_at
        if mode is not None:
            result["mode"] = mode
        if domain is not None:
            result["domain"] = domain
        return result

    @app.get("/api/exchange-accounts")
    def exchange_accounts(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        try:
            require_capability(identity, "venue.view")
        except DomainRejected as exc:
            if exc.code != "RBAC_DENIED":
                raise
            require_capability(identity, "proposal.create")
        result = queries().exchange_accounts(identity.user_id)
        return {"data": result, "as_of": _now().isoformat()}

    @app.post("/api/exchange-accounts")
    def create_exchange_account(
        payload: ExchangeAccountCreateRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        exchange_account_id = service().create_exchange_account(
            actor_id=identity.user_id,
            account_id=payload.account_id,
            venue=payload.venue,
            label=payload.label,
            credentials=(None if payload.credentials is None else payload.credentials.plaintext()),
            idempotency_key=payload.idempotency_key,
            now=_now(),
        )
        return {
            "exchange_account_id": str(exchange_account_id),
            "data": queries().exchange_accounts(identity.user_id),
        }

    @app.put("/api/exchange-accounts/{exchange_account_id}/credentials")
    def rotate_exchange_account_credentials(
        exchange_account_id: UUID,
        payload: ExchangeCredentialRotateRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        version = service().rotate_exchange_account_credentials(
            exchange_account_id,
            actor_id=identity.user_id,
            credentials=payload.credentials.plaintext(),
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            now=_now(),
        )
        return {
            "exchange_account_id": str(exchange_account_id),
            "version": version,
            "data": queries().exchange_accounts(identity.user_id),
        }

    @app.post("/api/exchange-accounts/{exchange_account_id}/connection-verifications")
    def verify_exchange_account_connection(
        exchange_account_id: UUID,
        payload: ExchangeConnectionVerifyRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        account_service = service()
        command, replay = account_service.prepare_exchange_account_connection_verification(
            exchange_account_id,
            actor_id=identity.user_id,
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
        )
        if replay is not None:
            result = replay
        else:
            assert command is not None
            outcome = resolved_exchange_connection_verifier.verify(
                venue=command.venue,
                credentials=command.credentials,
                now=_now(),
            )
            result = account_service.record_exchange_account_connection_verification(
                command,
                outcome,
                actor_id=identity.user_id,
                idempotency_key=payload.idempotency_key,
                now=_now(),
            )
        return {
            **result,
            "data": queries().exchange_accounts(identity.user_id),
        }

    @app.put("/api/exchange-accounts/{exchange_account_id}/runtime-sync")
    def configure_exchange_account_runtime_sync(
        exchange_account_id: UUID,
        payload: ExchangeRuntimeSyncRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        result = service().configure_exchange_account_runtime_sync(
            exchange_account_id,
            actor_id=identity.user_id,
            enabled=payload.enabled,
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            now=_now(),
        )
        return {
            **result,
            "data": queries().exchange_accounts(identity.user_id),
        }

    @app.put("/api/exchange-accounts/{exchange_account_id}/trading-eligibility")
    def configure_exchange_account_trading_eligibility(
        exchange_account_id: UUID,
        payload: ExchangeTradingEligibilityRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        result = service().configure_exchange_account_trading(
            exchange_account_id,
            actor_id=identity.user_id,
            enabled=payload.enabled,
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            now=_now(),
        )
        return {
            **result,
            "data": queries().exchange_accounts(identity.user_id),
        }

    @app.put("/api/exchange-accounts/{exchange_account_id}/freqtrade-worker")
    def configure_exchange_account_freqtrade_worker(
        exchange_account_id: UUID,
        payload: FreqtradeWorkerConfigureRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        result = service().configure_exchange_account_freqtrade_worker(
            exchange_account_id,
            actor_id=identity.user_id,
            mode=payload.mode,
            name=payload.name,
            base_url=payload.base_url,
            username=payload.plaintext_username(),
            password=payload.plaintext_password(),
            hip3_dexes=tuple(payload.hip3_dexes),
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            now=_now(),
        )
        return {
            **result,
            "data": queries().exchange_accounts(identity.user_id),
        }

    @app.post("/api/exchange-accounts/{exchange_account_id}/freqtrade-worker/verifications")
    def verify_exchange_account_freqtrade_worker(
        exchange_account_id: UUID,
        payload: FreqtradeWorkerVerifyRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        account_service = service()
        binding, replay = account_service.prepare_exchange_account_freqtrade_verification(
            exchange_account_id,
            actor_id=identity.user_id,
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
        )
        if replay is not None:
            result = replay
        else:
            assert binding is not None
            worker = freqtrade_client_for_binding(binding)
            try:
                worker.probe(expected_mode=("LIVE" if binding.worker_mode == "LIVE" else "DRY_RUN"))
                error_code = None
            except DomainRejected as exc:
                error_code = exc.code
            result = account_service.record_exchange_account_freqtrade_verification(
                binding,
                actor_id=identity.user_id,
                error_code=error_code,
                idempotency_key=payload.idempotency_key,
                now=_now(),
            )
        return {
            **result,
            "data": queries().exchange_accounts(identity.user_id),
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
        require_registered_or_default_venue_account(identity, account_id, "BINANCE")
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
        environment = resolved_settings.binance_fact_environment
        return reconciled_venue_sync(
            identity=identity,
            account_id=payload.account_id,
            venue="BINANCE",
            environment=environment,
            symbol=payload.symbol,
            source="BINANCE_USER_DATA",
            mode="READ_ONLY",
            observed_at=snapshot.observed_at.isoformat(),
            persist=lambda now: service().ingest_binance_read_only_snapshot(
                payload.account_id,
                identity.user_id,
                snapshot,
                environment=ExecutionEnvironment(environment),
                now=now,
            ),
        )

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
        return reconciled_venue_sync(
            identity=identity,
            account_id=payload.account_id,
            venue="BINANCE",
            environment="TESTNET",
            symbol=payload.symbol,
            source="BINANCE_TESTNET_USER_DATA",
            include_execution_scope=False,
            persist=lambda now: service().ingest_binance_read_only_snapshot(
                payload.account_id,
                identity.user_id,
                snapshot,
                environment=ExecutionEnvironment.TESTNET,
                now=now,
            ),
        )

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

    @app.get("/api/venues/hyperliquid/facts")
    def hyperliquid_read_only_facts(
        account_id: str,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_registered_or_default_venue_account(identity, account_id, "HYPERLIQUID")
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

    @app.get("/api/venues/okx/facts")
    def okx_read_only_facts(
        account_id: str,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return cast(dict[str, Any], database_bound_venue_facts("OKX", account_id, identity))

    @app.get("/api/venues/bybit/facts")
    def bybit_read_only_facts(
        account_id: str,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return cast(dict[str, Any], database_bound_venue_facts("BYBIT", account_id, identity))

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
        environment = ExecutionEnvironment(resolved_settings.hyperliquid_fact_environment)
        symbol_dex = payload.symbol.split(":", 1)[0] if ":" in payload.symbol else ""
        return reconciled_venue_sync(
            identity=identity,
            account_id=payload.account_id,
            venue="HYPERLIQUID",
            environment=environment.value,
            symbol=payload.symbol,
            source="HYPERLIQUID_INFO",
            mode="READ_ONLY",
            domain="CORE" if not symbol_dex else f"HIP3:{symbol_dex}",
            observed_at=snapshot.observed_at.isoformat(),
            persist=lambda now: service().ingest_hyperliquid_read_only_snapshot(
                payload.account_id,
                identity.user_id,
                snapshot,
                environment=environment,
                now=now,
            ),
        )
