from __future__ import annotations

from trading_control_plane.api_core import (
    UUID,
    Any,
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
        self.connection_verification = dependencies.connection_verification
        self.service = common.service
        self.resolved_settings = common.settings

    def exchange_accounts_projection(self, actor_id: UUID) -> dict[str, Any]:
        return self.queries().exchange_accounts(actor_id)

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

        @self.app.get("/api/exchange-accounts/{exchange_account_id}/facts")
        def exchange_account_facts(
            exchange_account_id: UUID,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return {
                "source": "CCXT_PRO",
                "data": self.queries().exchange_account_facts(
                    identity.user_id,
                    exchange_account_id,
                ),
                "as_of": _now().isoformat(),
            }

        @self.app.get("/api/exchange-accounts/{exchange_account_id}/fact-health")
        def exchange_account_fact_health(
            exchange_account_id: UUID,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            return {
                "data": self.queries().exchange_account_fact_health(
                    identity.user_id,
                    exchange_account_id,
                    stale_after_seconds=self.resolved_settings.fact_adapter_stale_after_seconds,
                    now=now,
                ),
                "as_of": now.isoformat(),
            }

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
                account_mode=payload.account_mode,
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
            result = self.connection_verification.verify(
                service=self.service(),
                exchange_account_id=exchange_account_id,
                actor_id=identity.user_id,
                expected_version=payload.expected_version,
                idempotency_key=payload.idempotency_key,
                clock=_now,
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
                    worker.probe(expected_mode=binding.worker_mode)  # type: ignore[arg-type]
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


def register_accounts_routes(context: ApiRouteContext) -> None:
    """Register accounts routes from bounded lifecycle groups."""

    routes = _AccountsRoutes(context)
    routes.register_registry()
