from __future__ import annotations

from collections.abc import Callable

from trading_control_plane.api_core import (
    UUID,
    Any,
    BinanceReadOnlySyncRequest,
    DomainRejected,
    ExchangeAccountCreateRequest,
    ExchangeAccountDeleteRequest,
    ExchangeAccountStateRequest,
    ExchangeAccountUpdateRequest,
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


class _AccountsRoutes:
    def __init__(self, context: ApiRouteContext) -> None:
        dependencies = context.accounts
        common = dependencies.common
        self.app = context.app
        self.freqtrade_client_for_binding = dependencies.freqtrade_client_for_binding
        self.identity_dependency = common.identity
        self.queries = common.queries
        self.require_capability = common.require_capability
        self.resolved_exchange_connection_verifier = dependencies.exchange_connection_verifier
        self.service = common.service
        self.require_binance_testnet = dependencies.require_binance_testnet
        self.require_default_venue_account = dependencies.require_default_venue_account
        self.require_registered_or_default_venue_account = (
            dependencies.require_registered_or_default_venue_account
        )
        self.resolved_binance = dependencies.binance
        self.resolved_binance_live = dependencies.binance_live
        self.resolved_binance_testnet = dependencies.binance_testnet
        self.resolved_binance_testnet_reader = dependencies.binance_testnet_reader
        self.resolved_settings = common.settings
        self.database_bound_venue_facts = dependencies.database_bound_venue_facts
        self.resolved_hyperliquid = dependencies.hyperliquid
        self.resolved_hyperliquid_live = dependencies.hyperliquid_live
        self.resolved_hyperliquid_testnet = dependencies.hyperliquid_testnet

    def exchange_accounts_projection(self, actor_id: UUID) -> dict[str, Any]:
        """Add deployment-owned Worker defaults without weakening account scope."""

        result = self.queries().exchange_accounts(actor_id)
        default_endpoints = {
            "BINANCE": self.resolved_settings.freqtrade_binance_worker_url,
            "HYPERLIQUID": self.resolved_settings.freqtrade_hyperliquid_worker_url,
        }
        for item in result["data"]:
            if not item["permissions"]["can_manage_worker"]:
                continue
            endpoint = default_endpoints.get(item["venue"])
            if endpoint is not None:
                item["execution_worker"]["default_endpoint"] = endpoint
        return result

    def reconciled_venue_sync(
        self,
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
        reconciliation_id = self.service().reconcile_scope(
            execution_scope,
            identity.user_id,
            now=now,
        )
        reconciliation = {
            "reconciliation_id": str(reconciliation_id),
            "status": self.service().reconciliation_status(reconciliation_id).value,
        }
        if include_execution_scope:
            reconciliation["execution_scope"] = execution_scope
        result: dict[str, Any] = {
            "source": source,
            "environment": environment,
            "symbol": symbol,
            "persisted": persisted,
            "reconciliation": reconciliation,
            "facts": self.queries().venue_facts(
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

    def register_registry(self) -> None:
        @self.app.get("/api/positions")
        def current_positions(
            environment: ExecutionEnvironment = ExecutionEnvironment.LIVE,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_capability(identity, "venue.view")
            return {
                "data": self.queries().current_positions(identity.user_id, environment.value),
                "as_of": _now().isoformat(),
            }

        @self.app.get("/api/exchange-accounts")
        def exchange_accounts(
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            try:
                self.require_capability(identity, "venue.view")
            except DomainRejected as exc:
                if exc.code != "RBAC_DENIED":
                    raise
                self.require_capability(identity, "proposal.create")
            result = self.exchange_accounts_projection(identity.user_id)
            return {"data": result, "as_of": _now().isoformat()}

        @self.app.post("/api/exchange-accounts")
        def create_exchange_account(
            payload: ExchangeAccountCreateRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            exchange_account_id = self.service().create_exchange_account(
                actor_id=identity.user_id,
                environment=payload.environment,
                account_id=payload.account_id,
                venue=payload.venue,
                label=payload.label,
                credentials=payload.credentials.plaintext(),
                idempotency_key=payload.idempotency_key,
                now=_now(),
            )
            return {
                "exchange_account_id": str(exchange_account_id),
                "data": self.exchange_accounts_projection(identity.user_id),
            }

        @self.app.delete("/api/exchange-accounts/{exchange_account_id}")
        def delete_exchange_account(
            exchange_account_id: UUID,
            payload: ExchangeAccountDeleteRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            result = self.service().delete_exchange_account(
                exchange_account_id,
                actor_id=identity.user_id,
                confirmation=payload.confirmation,
                expected_version=payload.expected_version,
                idempotency_key=payload.idempotency_key,
                now=_now(),
            )
            return {
                **result,
                "data": self.exchange_accounts_projection(identity.user_id),
            }

        @self.app.put("/api/exchange-accounts/{exchange_account_id}")
        def update_exchange_account(
            exchange_account_id: UUID,
            payload: ExchangeAccountUpdateRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            result = self.service().update_exchange_account(
                exchange_account_id,
                actor_id=identity.user_id,
                label=payload.label,
                expected_version=payload.expected_version,
                idempotency_key=payload.idempotency_key,
                now=_now(),
            )
            return {**result, "data": self.exchange_accounts_projection(identity.user_id)}

        @self.app.put("/api/exchange-accounts/{exchange_account_id}/state")
        def set_exchange_account_state(
            exchange_account_id: UUID,
            payload: ExchangeAccountStateRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            result = self.service().set_exchange_account_state(
                exchange_account_id,
                actor_id=identity.user_id,
                enabled=payload.enabled,
                confirmation=payload.confirmation,
                expected_version=payload.expected_version,
                idempotency_key=payload.idempotency_key,
                now=_now(),
            )
            return {**result, "data": self.exchange_accounts_projection(identity.user_id)}

        @self.app.put("/api/exchange-accounts/{exchange_account_id}/credentials")
        def rotate_exchange_account_credentials(
            exchange_account_id: UUID,
            payload: ExchangeCredentialRotateRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            version = self.service().rotate_exchange_account_credentials(
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
                "data": self.exchange_accounts_projection(identity.user_id),
            }

        @self.app.post("/api/exchange-accounts/{exchange_account_id}/connection-verifications")
        def verify_exchange_account_connection(
            exchange_account_id: UUID,
            payload: ExchangeConnectionVerifyRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            account_service = self.service()
            command, replay = account_service.prepare_exchange_account_connection_verification(
                exchange_account_id,
                actor_id=identity.user_id,
                expected_version=payload.expected_version,
                idempotency_key=payload.idempotency_key,
                now=_now(),
            )
            if replay is not None:
                result = replay
            else:
                assert command is not None
                outcome = self.resolved_exchange_connection_verifier.verify(
                    venue=command.venue,
                    environment=command.environment,
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
                "data": self.exchange_accounts_projection(identity.user_id),
            }

        @self.app.put("/api/exchange-accounts/{exchange_account_id}/runtime-sync")
        def configure_exchange_account_runtime_sync(
            exchange_account_id: UUID,
            payload: ExchangeRuntimeSyncRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            result = self.service().configure_exchange_account_runtime_sync(
                exchange_account_id,
                actor_id=identity.user_id,
                enabled=payload.enabled,
                expected_version=payload.expected_version,
                idempotency_key=payload.idempotency_key,
                now=_now(),
            )
            return {
                **result,
                "data": self.exchange_accounts_projection(identity.user_id),
            }

        @self.app.put("/api/exchange-accounts/{exchange_account_id}/trading-eligibility")
        def configure_exchange_account_trading_eligibility(
            exchange_account_id: UUID,
            payload: ExchangeTradingEligibilityRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            result = self.service().configure_exchange_account_trading(
                exchange_account_id,
                actor_id=identity.user_id,
                enabled=payload.enabled,
                expected_version=payload.expected_version,
                idempotency_key=payload.idempotency_key,
                now=_now(),
            )
            return {
                **result,
                "data": self.exchange_accounts_projection(identity.user_id),
            }

        @self.app.put("/api/exchange-accounts/{exchange_account_id}/freqtrade-worker")
        def configure_exchange_account_freqtrade_worker(
            exchange_account_id: UUID,
            payload: FreqtradeWorkerConfigureRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            result = self.service().configure_exchange_account_freqtrade_worker(
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
                ws_token=payload.plaintext_ws_token(),
            )
            return {
                **result,
                "data": self.exchange_accounts_projection(identity.user_id),
            }

        @self.app.post(
            "/api/exchange-accounts/{exchange_account_id}/freqtrade-worker/verifications"
        )
        def verify_exchange_account_freqtrade_worker(
            exchange_account_id: UUID,
            payload: FreqtradeWorkerVerifyRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            account_service = self.service()
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
                worker = self.freqtrade_client_for_binding(binding)
                try:
                    worker.probe(
                        expected_mode=("LIVE" if binding.worker_mode == "LIVE" else "DRY_RUN")
                    )
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
                "data": self.exchange_accounts_projection(identity.user_id),
            }

        @self.app.get("/api/instruments")
        def instruments(
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return {
                "data": self.queries().list_instruments(identity.user_id),
                "catalog_scope": {
                    "contract_family": "U_MARGINED_PERPETUAL",
                    "strategy_allowlist_applied": False,
                    "exchange_trading_status_required": True,
                },
                "as_of": _now().isoformat(),
            }

    def register_binance(self) -> None:
        @self.app.get("/api/venues/binance/status")
        def binance_read_only_status(
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_capability(identity, "venue.view", venue="BINANCE")
            return {
                "venue": "BINANCE",
                "mode": "USER_DATA_READ_ONLY",
                "enabled": self.resolved_settings.binance_read_only_enabled,
                "configured": self.resolved_binance.configured,
                "execution_backend": self.resolved_settings.execution_backend,
                "order_send_available": False,
                "worker_configured": self.resolved_settings.freqtrade_workers_enabled,
                "account_mode": self.resolved_settings.binance_account_mode,
                "fact_environment": self.resolved_settings.binance_fact_environment,
                "automatic_sync_enabled": (
                    self.resolved_settings.runtime_sync_enabled
                    and self.resolved_settings.runtime_binance_account_id is not None
                ),
                "automatic_sync_interval_seconds": (
                    self.resolved_settings.runtime_sync_interval_seconds
                ),
                "default_account_id": self.resolved_settings.runtime_binance_account_id,
                "environment": self.resolved_settings.environment,
            }

        @self.app.get("/api/venues/binance/live/status")
        def binance_live_status(
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_capability(identity, "venue.view", venue="BINANCE")
            return {
                "venue": "BINANCE",
                "environment": "LIVE",
                "account_mode": self.resolved_settings.binance_account_mode,
                "execution_backend": self.resolved_settings.execution_backend,
                "enabled": self.resolved_settings.binance_live_order_send_enabled,
                "configured": self.resolved_binance_live.configured,
                "capability_gate_required": "LIVE_ORDER_SEND",
                "capital_transfer": False,
            }

        @self.app.get("/api/venues/binance/testnet/status")
        def binance_testnet_status(
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_capability(identity, "venue.view", venue="BINANCE")
            return {
                "venue": "BINANCE",
                "environment": "TESTNET",
                "execution_backend": self.resolved_settings.execution_backend,
                "enabled": self.resolved_settings.binance_testnet_order_send_enabled,
                "configured": self.resolved_binance_testnet.configured,
                "live_order_send": False,
                "capital_transfer": False,
            }

        @self.app.get("/api/venues/binance/facts")
        def binance_read_only_facts(
            account_id: str,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_registered_or_default_venue_account(identity, account_id, "BINANCE")
            return {
                "mode": "USER_DATA_READ_ONLY",
                "data": self.queries().venue_facts(
                    identity.user_id,
                    account_id,
                    "BINANCE",
                    self.resolved_settings.binance_fact_environment,
                ),
                "as_of": _now().isoformat(),
            }

        @self.app.post("/api/venues/binance/sync")
        def sync_binance_read_only_facts(
            payload: BinanceReadOnlySyncRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            if self.resolved_settings.fact_adapter_enabled:
                raise DomainRejected(
                    "LEGACY_FACT_SYNC_RETIRED",
                    "manual venue polling is retired while the account-scoped "
                    "fact adapter is active",
                )
            self.require_default_venue_account(payload.account_id, "BINANCE")
            if not self.resolved_settings.binance_read_only_enabled:
                raise DomainRejected(
                    "BINANCE_READ_ONLY_DISABLED",
                    "Binance USER_DATA read-only synchronization is disabled",
                )
            if not self.resolved_binance.configured:
                raise DomainRejected(
                    "BINANCE_READ_ONLY_NOT_CONFIGURED",
                    "Binance read-only credentials are not configured",
                )
            if not self.service().can_user(
                identity.user_id, "venue.record", payload.account_id, "BINANCE"
            ):
                raise DomainRejected(
                    "RBAC_DENIED", "Binance facts are outside the current operator scope"
                )
            snapshot = self.resolved_binance.read_snapshot(payload.symbol, now=_now())
            environment = self.resolved_settings.binance_fact_environment
            return self.reconciled_venue_sync(
                identity=identity,
                account_id=payload.account_id,
                venue="BINANCE",
                environment=environment,
                symbol=payload.symbol,
                source="BINANCE_USER_DATA",
                mode="READ_ONLY",
                observed_at=snapshot.observed_at.isoformat(),
                persist=lambda now: self.service().ingest_binance_read_only_snapshot(
                    payload.account_id,
                    identity.user_id,
                    snapshot,
                    environment=ExecutionEnvironment(environment),
                    now=now,
                ),
            )

        @self.app.post("/api/venues/binance/testnet/sync")
        def sync_binance_testnet_facts(
            payload: BinanceReadOnlySyncRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            if self.resolved_settings.fact_adapter_enabled:
                raise DomainRejected(
                    "LEGACY_FACT_SYNC_RETIRED",
                    "manual venue polling is retired while the account-scoped "
                    "fact adapter is active",
                )
            self.require_binance_testnet()
            if not self.resolved_binance_testnet_reader.configured:
                raise DomainRejected(
                    "BINANCE_TESTNET_NOT_CONFIGURED", "Binance testnet facts are not configured"
                )
            if not self.service().can_user(
                identity.user_id, "venue.record", payload.account_id, "BINANCE"
            ):
                raise DomainRejected(
                    "RBAC_DENIED", "Binance testnet facts are outside the current operator scope"
                )
            snapshot = self.resolved_binance_testnet_reader.read_snapshot(
                payload.symbol, now=_now()
            )
            return self.reconciled_venue_sync(
                identity=identity,
                account_id=payload.account_id,
                venue="BINANCE",
                environment="TESTNET",
                symbol=payload.symbol,
                source="BINANCE_TESTNET_USER_DATA",
                include_execution_scope=False,
                persist=lambda now: self.service().ingest_binance_read_only_snapshot(
                    payload.account_id,
                    identity.user_id,
                    snapshot,
                    environment=ExecutionEnvironment.TESTNET,
                    now=now,
                ),
            )

    def register_other_venues(self) -> None:
        @self.app.get("/api/venues/hyperliquid/status")
        def hyperliquid_read_only_status(
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_capability(identity, "venue.view", venue="HYPERLIQUID")
            return {
                "venue": "HYPERLIQUID",
                "domain": "CORE_AND_CONFIGURED_HIP3",
                "dex": "",
                "mode": "INFO_READ_ONLY",
                "enabled": self.resolved_settings.hyperliquid_read_only_enabled,
                "configured": self.resolved_hyperliquid.configured,
                "execution_backend": self.resolved_settings.execution_backend,
                "order_send_available": False,
                "worker_configured": self.resolved_settings.freqtrade_workers_enabled,
                "fact_environment": self.resolved_settings.hyperliquid_fact_environment,
                "source_environment": self.resolved_hyperliquid.fact_environment,
                "hip3_available": bool(self.resolved_settings.hyperliquid_hip3_dexes),
                "hip3_dexes": list(self.resolved_settings.hyperliquid_hip3_dexes),
                "automatic_sync_enabled": (
                    self.resolved_settings.runtime_sync_enabled
                    and self.resolved_settings.runtime_hyperliquid_account_id is not None
                ),
                "automatic_sync_interval_seconds": (
                    self.resolved_settings.runtime_sync_interval_seconds
                ),
                "default_account_id": self.resolved_settings.runtime_hyperliquid_account_id,
                "environment": self.resolved_settings.environment,
            }

        @self.app.get("/api/venues/hyperliquid/live/status")
        def hyperliquid_live_status(
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_capability(identity, "venue.view", venue="HYPERLIQUID")
            return {
                "venue": "HYPERLIQUID",
                "domain": "CORE",
                "environment": "LIVE",
                "execution_backend": self.resolved_settings.execution_backend,
                "enabled": self.resolved_settings.hyperliquid_live_order_send_enabled,
                "configured": self.resolved_hyperliquid_live.configured,
                "account_scope": self.resolved_hyperliquid_live.account_scope,
                "capability_gate_required": "LIVE_ORDER_SEND",
                "capital_transfer": False,
                "hip3_available": False,
            }

        @self.app.get("/api/venues/hyperliquid/testnet/status")
        def hyperliquid_testnet_status(
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_capability(identity, "venue.view", venue="HYPERLIQUID")
            return {
                "venue": "HYPERLIQUID",
                "domain": "CORE",
                "environment": "TESTNET",
                "execution_backend": self.resolved_settings.execution_backend,
                "enabled": self.resolved_settings.hyperliquid_testnet_order_send_enabled,
                "configured": self.resolved_hyperliquid_testnet.configured,
                "signer_source": "INJECTED_RUNTIME_ONLY",
                "live_order_send": False,
                "capital_transfer": False,
                "hip3_available": False,
            }

        @self.app.get("/api/venues/hyperliquid/facts")
        def hyperliquid_read_only_facts(
            account_id: str,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_registered_or_default_venue_account(identity, account_id, "HYPERLIQUID")
            return {
                "mode": "INFO_READ_ONLY",
                "domain": "CORE_AND_CONFIGURED_HIP3",
                "hip3_dexes": list(self.resolved_settings.hyperliquid_hip3_dexes),
                "data": self.queries().venue_facts(
                    identity.user_id,
                    account_id,
                    "HYPERLIQUID",
                    self.resolved_settings.hyperliquid_fact_environment,
                ),
                "as_of": _now().isoformat(),
            }

        @self.app.get("/api/venues/okx/facts")
        def okx_read_only_facts(
            account_id: str,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.database_bound_venue_facts("OKX", account_id, identity)

        @self.app.get("/api/venues/bybit/facts")
        def bybit_read_only_facts(
            account_id: str,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.database_bound_venue_facts("BYBIT", account_id, identity)

        @self.app.post("/api/venues/hyperliquid/sync")
        def sync_hyperliquid_read_only_facts(
            payload: HyperliquidReadOnlySyncRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            if self.resolved_settings.fact_adapter_enabled:
                raise DomainRejected(
                    "LEGACY_FACT_SYNC_RETIRED",
                    "manual venue polling is retired while the account-scoped "
                    "fact adapter is active",
                )
            self.require_default_venue_account(payload.account_id, "HYPERLIQUID")
            if not self.resolved_settings.hyperliquid_read_only_enabled:
                raise DomainRejected(
                    "HYPERLIQUID_READ_ONLY_DISABLED",
                    "Hyperliquid Core Info synchronization is disabled",
                )
            if not self.resolved_hyperliquid.configured:
                raise DomainRejected(
                    "HYPERLIQUID_READ_ONLY_NOT_CONFIGURED",
                    "Hyperliquid main account address is not configured",
                )
            if (
                self.resolved_hyperliquid.fact_environment
                != self.resolved_settings.hyperliquid_fact_environment
            ):
                raise DomainRejected(
                    "HYPERLIQUID_ENVIRONMENT_MISMATCH",
                    "Hyperliquid API host does not match the configured fact environment",
                )
            if not self.service().can_user(
                identity.user_id, "venue.record", payload.account_id, "HYPERLIQUID"
            ):
                raise DomainRejected(
                    "RBAC_DENIED", "Hyperliquid facts are outside the current operator scope"
                )
            snapshot = self.resolved_hyperliquid.read_snapshot(payload.symbol, now=_now())
            environment = ExecutionEnvironment(self.resolved_settings.hyperliquid_fact_environment)
            symbol_dex = payload.symbol.split(":", 1)[0] if ":" in payload.symbol else ""
            return self.reconciled_venue_sync(
                identity=identity,
                account_id=payload.account_id,
                venue="HYPERLIQUID",
                environment=environment.value,
                symbol=payload.symbol,
                source="HYPERLIQUID_INFO",
                mode="READ_ONLY",
                domain="CORE" if not symbol_dex else f"HIP3:{symbol_dex}",
                observed_at=snapshot.observed_at.isoformat(),
                persist=lambda now: self.service().ingest_hyperliquid_read_only_snapshot(
                    payload.account_id,
                    identity.user_id,
                    snapshot,
                    environment=environment,
                    now=now,
                ),
            )


def register_accounts_routes(context: ApiRouteContext) -> None:
    """Register accounts routes from bounded lifecycle groups."""

    routes = _AccountsRoutes(context)
    routes.register_registry()
    routes.register_binance()
    routes.register_other_venues()
