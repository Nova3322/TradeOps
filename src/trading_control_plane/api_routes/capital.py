from __future__ import annotations

from trading_control_plane.api_core import (
    HYPERLIQUID_BRIDGE2_ADDRESS,
    SUPPORTED_NOTILT_CHAINS,
    UUID,
    Any,
    CapitalAutomationEvaluateRequest,
    CapitalAutomationPolicyRequest,
    CapitalBalanceFactRequest,
    CapitalDirection,
    CapitalScopeReconciliationRequest,
    CapitalTransferCreateRequest,
    CapitalTransferObservationRequest,
    CapitalTransferStatus,
    CapitalTreasuryProvider,
    Decimal,
    DirectCapitalBinanceReceiptRequest,
    DirectCapitalBinanceSubmissionRequest,
    DirectCapitalConfigurationRequest,
    DirectCapitalHyperliquidReceiptRequest,
    DirectCapitalOperationRequest,
    DirectCapitalPath,
    DirectCapitalTreasuryReceiptRequest,
    DirectCapitalUnsignedPlanRequest,
    DirectCapitalWalletSubmissionRequest,
    DomainRejected,
    ExecutionEnvironment,
    HTTPException,
    NoTiltReceiptRequest,
    NoTiltUnsignedTransaction,
    ReviewDecision,
    SessionIdentity,
    TransferAuthorizationRequest,
    TransferProposalRequest,
    TransferReviewRequest,
    _now,
    build_direct_capital_plan,
    datetime,
    resolve_hyperliquid_main_account,
    status,
    timedelta,
)
from trading_control_plane.api_routes.context import ApiRouteContext


class _CapitalRoutes:
    def __init__(self, context: ApiRouteContext) -> None:
        dependencies = context.capital
        common = dependencies.common
        self.app = context.app
        self.capital_snapshot = dependencies.capital_snapshot
        self.effective_direct_capital_settings = dependencies.effective_direct_capital_settings
        self.identity_dependency = common.identity
        self.resolved_notilt = dependencies.notilt
        self.resolved_settings = common.settings
        self.service = common.service
        self.sync_configured_notilt_vault = dependencies.sync_configured_notilt_vault
        self.resolved_binance_capital = dependencies.binance_capital
        self.resolved_hyperliquid_capital = dependencies.hyperliquid_capital
        self.configured_notilt_scope = dependencies.configured_notilt_scope
        self.notilt_chain_id_for_network = dependencies.notilt_chain_id_for_network
        self.resolved_safe_spending = dependencies.safe_spending
        self.verify_live_notilt_release_budget = dependencies.verify_live_notilt_release_budget
        self.notify_capital = dependencies.notify_capital
        self.queries = common.queries
        self.resolved_capital_transfer = dependencies.capital_transfer
        self.token_service = dependencies.token_service

    def register_configuration(self) -> None:
        @self.app.get("/api/notilt/status")
        def notilt_status(
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.capital_snapshot(identity.user_id)
            return {
                "enabled": self.resolved_settings.notilt_enabled,
                "gateway_available": self.resolved_notilt.available,
                "signing_mode": "EXTERNAL_WALLET_ONLY",
                "credential_custody": "EXTERNAL_WALLET",
                "chains": [
                    {
                        "chain_id": chain_id,
                        "chain": chain,
                        "vault_configured": chain_id in self.resolved_settings.notilt_vaults,
                    }
                    for chain_id, chain in SUPPORTED_NOTILT_CHAINS.items()
                ],
            }

        @self.app.get("/api/notilt/chains/{chain_id}/assignment")
        def notilt_assignment(
            chain_id: int,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.capital_snapshot(identity.user_id)
            if (
                not self.resolved_settings.notilt_enabled
                or self.resolved_settings.notilt_agent_address is None
            ):
                raise DomainRejected(
                    "NOTILT_NOT_CONFIGURED",
                    "NoTilt public whitelist agent address is not configured",
                )
            assigned_vault, active = self.resolved_notilt.resolve_assignment(
                chain_id, self.resolved_settings.notilt_agent_address
            )
            configured_vault = self.resolved_settings.notilt_vaults.get(chain_id)
            return {
                "chain_id": chain_id,
                "chain": SUPPORTED_NOTILT_CHAINS.get(chain_id),
                "active": active,
                "matches_configured_vault": (
                    configured_vault is not None
                    and assigned_vault.lower() == configured_vault.lower()
                ),
                "configured_vault": configured_vault is not None,
            }

        @self.app.post("/api/notilt/chains/{chain_id}/sync")
        def sync_notilt_vault(
            chain_id: int,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            fact_count, capital = self.sync_configured_notilt_vault(
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

        @self.app.get("/api/capital")
        def capital_center(
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return {"data": self.capital_snapshot(identity.user_id), "as_of": _now().isoformat()}

        @self.app.put("/api/capital/direct-configuration")
        def update_direct_capital_configuration(
            payload: DirectCapitalConfigurationRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            direct_settings, _ = self.effective_direct_capital_settings(identity.user_id)
            supplied = payload.model_dump(exclude={"idempotency_key"}, exclude_none=True)
            field_map = {
                "network": "capital_direct_network",
                "asset": "capital_direct_asset",
                "treasury_provider": "capital_direct_treasury_provider",
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
            selected_provider = CapitalTreasuryProvider(str(merged["treasury_provider"]))
            trusted_vault = self.resolved_settings.notilt_vaults.get(42161)
            direct_vault = merged["vault_address"]
            if (
                selected_provider is CapitalTreasuryProvider.NOTILT_VAULT
                and trusted_vault is not None
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
                    self.resolved_settings.runtime_binance_account_id,
                ),
                (
                    "HYPERLIQUID",
                    merged["hyperliquid_account_id"],
                    self.resolved_settings.runtime_hyperliquid_account_id,
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
            config_id = self.service().set_direct_capital_configuration(
                identity.user_id,
                payload.idempotency_key,
                network=str(merged["network"]),
                asset=str(merged["asset"]),
                treasury_provider=selected_provider.value,
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
                    None
                    if merged["binance_account_id"] is None
                    else str(merged["binance_account_id"])
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
                safe_address=(
                    None if merged["safe_address"] is None else str(merged["safe_address"])
                ),
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
                "data": self.capital_snapshot(identity.user_id),
            }

        @self.app.post("/api/capital/direct-operations")
        def create_direct_capital_operation(
            payload: DirectCapitalOperationRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            center = self.capital_snapshot(identity.user_id)
            direct_settings, _ = self.effective_direct_capital_settings(identity.user_id)
            selected_provider = CapitalTreasuryProvider(
                direct_settings.capital_direct_treasury_provider
            )
            if (
                payload.treasury_provider is not None
                and payload.treasury_provider is not selected_provider
            ):
                raise DomainRejected(
                    "CAPITAL_TREASURY_PROVIDER_MISMATCH",
                    "capital operation must use the administrator-selected funding provider",
                )
            plan = build_direct_capital_plan(
                path=DirectCapitalPath(payload.path),
                treasury_provider=selected_provider,
                amount=payload.amount,
                settings=direct_settings,
                capital_transfer_gate=center["real_transfer_gate"],
                now=now,
            )
            operation_id = self.service().create_direct_capital_operation(
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
                "data": self.capital_snapshot(identity.user_id),
            }

    def register_direct_binance(self) -> None:
        @self.app.post("/api/capital/direct-operations/{operation_id}/binance-preview")
        def prepare_direct_binance_preview(
            operation_id: UUID,
            payload: DirectCapitalUnsignedPlanRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            context = self.service().direct_capital_operation_context(
                operation_id, identity.user_id, now=now
            )
            if int(context["version"]) != payload.expected_version:
                raise DomainRejected(
                    "VERSION_CONFLICT", "direct capital operation changed; refresh"
                )
            path = DirectCapitalPath(str(context["path"]))
            if path not in {
                DirectCapitalPath.VAULT_TO_BINANCE,
                DirectCapitalPath.BINANCE_TO_VAULT,
            }:
                raise DomainRejected(
                    "BINANCE_CAPITAL_PATH_INVALID", "this operation does not contain a Binance leg"
                )
            direct_settings, _ = self.effective_direct_capital_settings(identity.user_id)
            if path is DirectCapitalPath.VAULT_TO_BINANCE:
                destination = direct_settings.capital_direct_binance_deposit_address
                source = context["source_reference"]
                if destination is None or source is None:
                    raise DomainRejected(
                        "BINANCE_CAPITAL_SCOPE_MISSING",
                        "frozen treasury source and Binance deposit address are required",
                    )
                artifact = self.resolved_binance_capital.prepare_deposit(
                    expected_address=destination,
                    amount=Decimal(str(context["min_received"])),
                    source_address=str(source),
                    now=now,
                )
            else:
                destination = direct_settings.capital_direct_binance_withdrawal_address
                max_fee = context["max_fee"]
                if destination is None or max_fee is None:
                    raise DomainRejected(
                        "BINANCE_CAPITAL_SCOPE_MISSING",
                        "allowlisted treasury destination and fee limit are required",
                    )
                if str(context["destination_reference"]).lower() != destination.lower():
                    raise DomainRejected(
                        "BINANCE_CAPITAL_DESTINATION_MISMATCH",
                        "frozen treasury destination does not match Binance configuration",
                    )
                artifact = self.resolved_binance_capital.prepare_withdrawal(
                    destination=destination,
                    amount=Decimal(str(context["amount"])),
                    max_fee=Decimal(str(max_fee)),
                    operation_id=str(operation_id),
                    now=now,
                )
            version = self.service().record_direct_capital_binance_preview(
                operation_id,
                identity.user_id,
                expected_version=payload.expected_version,
                artifact=artifact,
                idempotency_key=payload.idempotency_key,
                now=now,
            )
            return {
                "operation_id": str(operation_id),
                "version": version,
                "artifact": artifact,
                "credentials_configured": self.resolved_binance_capital.configured,
                "submission_enabled": direct_settings.binance_capital_withdraw_enabled,
                "signing_material_returned": False,
                "transfer_submitted": False,
                "data": self.capital_snapshot(identity.user_id),
            }

        @self.app.post("/api/capital/direct-operations/{operation_id}/binance-submit")
        def submit_direct_binance_withdrawal(
            operation_id: UUID,
            payload: DirectCapitalBinanceSubmissionRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            context = self.service().direct_capital_operation_context(
                operation_id, identity.user_id, now=now
            )
            if int(context["version"]) != payload.expected_version:
                raise DomainRejected(
                    "VERSION_CONFLICT", "direct capital operation changed; refresh"
                )
            if context["path"] != DirectCapitalPath.BINANCE_TO_VAULT.value:
                raise DomainRejected(
                    "BINANCE_CAPITAL_DIRECTION_INVALID",
                    "only Binance withdrawals use this endpoint",
                )
            direct_settings, _ = self.effective_direct_capital_settings(identity.user_id)
            if not direct_settings.binance_capital_withdraw_enabled:
                raise DomainRejected(
                    "BINANCE_CAPITAL_SUBMISSION_DISABLED",
                    "Binance capital withdrawal transport is explicitly disabled",
                )
            if self.capital_snapshot(identity.user_id)["real_transfer_gate"] != "ENABLED":
                raise DomainRejected(
                    "CAPITAL_TRANSFER_GATE_DISABLED",
                    "durable CAPITAL_TRANSFER gate is disabled",
                )
            preflight = next(
                (
                    stage.get("artifact")
                    for stage in reversed(context["stages"])
                    if stage.get("code") == "BINANCE_RESTRICTED_WITHDRAWAL_PREFLIGHT_READY"
                ),
                None,
            )
            if not isinstance(preflight, dict):
                raise DomainRejected(
                    "BINANCE_CAPITAL_PREFLIGHT_REQUIRED", "current live preflight is required"
                )
            submission = self.resolved_binance_capital.submit_withdrawal(preflight, now=now)
            version = self.service().record_direct_capital_binance_submission(
                operation_id,
                identity.user_id,
                expected_version=payload.expected_version,
                submission=submission,
                idempotency_key=payload.idempotency_key,
                now=now,
            )
            return {
                "operation_id": str(operation_id),
                "version": version,
                "submission": submission,
                "credentials_returned": False,
                "data": self.capital_snapshot(identity.user_id),
            }

        @self.app.post("/api/capital/direct-operations/{operation_id}/binance-receipt")
        def verify_direct_binance_receipt(
            operation_id: UUID,
            payload: DirectCapitalBinanceReceiptRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            context = self.service().direct_capital_operation_context(
                operation_id, identity.user_id, now=now
            )
            if int(context["version"]) != payload.expected_version:
                raise DomainRejected(
                    "VERSION_CONFLICT", "direct capital operation changed; refresh"
                )
            direct_settings, _ = self.effective_direct_capital_settings(identity.user_id)
            if payload.stage == "BINANCE_DEPOSIT":
                if context["path"] != DirectCapitalPath.VAULT_TO_BINANCE.value:
                    raise DomainRejected(
                        "BINANCE_CAPITAL_RECEIPT_STAGE_INVALID",
                        "deposit receipt does not match path",
                    )
                assert payload.transaction_hash is not None
                destination = direct_settings.capital_direct_binance_deposit_address
                if destination is None:
                    raise DomainRejected(
                        "BINANCE_CAPITAL_SCOPE_MISSING", "Binance deposit address is missing"
                    )
                evidence = self.resolved_binance_capital.verify_deposit(
                    transaction_hash=payload.transaction_hash,
                    destination=destination,
                    amount=Decimal(str(context["min_received"])),
                )
            else:
                if context["path"] != DirectCapitalPath.BINANCE_TO_VAULT.value:
                    raise DomainRejected(
                        "BINANCE_CAPITAL_RECEIPT_STAGE_INVALID",
                        "withdrawal receipt does not match path",
                    )
                destination = direct_settings.capital_direct_binance_withdrawal_address
                rpc_url = (
                    direct_settings.capital_arbitrum_rpc_url
                    or direct_settings.safe_spending_arbitrum_rpc_url
                )
                if destination is None or rpc_url is None:
                    raise DomainRejected(
                        "BINANCE_CAPITAL_SCOPE_MISSING",
                        "frozen treasury destination and trusted Arbitrum RPC are required",
                    )
                withdrawal = self.resolved_binance_capital.verify_withdrawal(
                    order_id=str(operation_id),
                    destination=destination,
                    amount=Decimal(str(context["amount"])),
                )
                transaction_hash = str(withdrawal["transactionHash"])
                chain = (
                    self.resolved_hyperliquid_capital.verify_arbitrum_usdc_credit_from_any_sender(
                        rpc_url=rpc_url,
                        transaction_hash=transaction_hash,
                        recipient=destination,
                        amount=str(context["amount"]),
                        min_confirmations=direct_settings.notilt_arbitrum_min_confirmations,
                    )
                )
                evidence = {"binance": withdrawal, "arbitrum": chain}
            version = self.service().record_direct_capital_binance_receipt(
                operation_id,
                identity.user_id,
                expected_version=payload.expected_version,
                stage=payload.stage,
                evidence=evidence,
                idempotency_key=payload.idempotency_key,
                now=now,
            )
            return {
                "operation_id": str(operation_id),
                "version": version,
                "receipt": evidence,
                "settlement": "CONFIRMED",
                "data": self.capital_snapshot(identity.user_id),
            }

    def register_direct_treasury(self) -> None:
        @self.app.post("/api/capital/direct-operations/{operation_id}/notilt-unsigned-preview")
        def prepare_direct_notilt_unsigned_preview(
            operation_id: UUID,
            payload: DirectCapitalUnsignedPlanRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            context = self.service().direct_capital_operation_context(
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
            if path is DirectCapitalPath.BINANCE_TO_VAULT:
                raise DomainRejected(
                    "BINANCE_DIRECT_TREASURY_WITHDRAWAL_REQUIRED",
                    "Binance return uses the restricted withdrawal API directly to the selected "
                    "NoTilt Vault; no second wallet deposit may be built",
                )
            if context["treasury_provider"] != "NOTILT_VAULT":
                raise DomainRejected(
                    "NOTILT_PLAN_SCOPE_MISMATCH",
                    "operation selected Safe Spending Limits instead of NoTilt Vault",
                )
            chain_id = self.notilt_chain_id_for_network(str(context["network"]))
            agent, vault = self.configured_notilt_scope(chain_id)
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
                    self.capital_snapshot(identity.user_id)["net_worth"]["max_fact_age_seconds"]
                )
                self.verify_live_notilt_release_budget(
                    chain_id=chain_id,
                    vault=vault,
                    agent=agent,
                    asset=str(context["asset"]),
                    amount=Decimal(amount),
                    max_fact_age_seconds=max_fact_age_seconds,
                    now=now,
                )
                transactions = (
                    self.resolved_notilt.prepare_release_request(
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
                vault_snapshot = self.resolved_notilt.read_vault(chain_id, vault, depositor)
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
                transactions = self.resolved_notilt.prepare_deposit(
                    chain_id=chain_id,
                    vault=vault,
                    agent=depositor,
                    asset=str(context["asset"]),
                    amount=amount,
                )
                preview_kind = "SDK_DEPOSIT_SEQUENCE"
            version = self.service().record_direct_capital_unsigned_preview(
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
                "data": self.capital_snapshot(identity.user_id),
            }

        @self.app.post("/api/capital/direct-operations/{operation_id}/safe-spending-preview")
        def prepare_direct_safe_spending_preview(
            operation_id: UUID,
            payload: DirectCapitalUnsignedPlanRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            context = self.service().direct_capital_operation_context(
                operation_id, identity.user_id, now=now
            )
            if int(context["version"]) != payload.expected_version:
                raise DomainRejected(
                    "VERSION_CONFLICT", "direct capital operation changed; refresh"
                )
            if context["treasury_provider"] != "SAFE_SPENDING_LIMIT":
                raise DomainRejected(
                    "SAFE_PLAN_SCOPE_MISMATCH", "operation did not select Safe Spending Limits"
                )
            path = DirectCapitalPath(str(context["path"]))
            if path is DirectCapitalPath.BINANCE_TO_VAULT:
                raise DomainRejected(
                    "BINANCE_DIRECT_TREASURY_WITHDRAWAL_REQUIRED",
                    "Binance return uses the restricted withdrawal API directly to the selected "
                    "Safe; no second wallet deposit may be built",
                )
            outbound = path in {
                DirectCapitalPath.VAULT_TO_BINANCE,
                DirectCapitalPath.VAULT_TO_HYPERLIQUID,
            }
            direct_settings, _ = self.effective_direct_capital_settings(identity.user_id)
            rpc_url = direct_settings.safe_spending_arbitrum_rpc_url
            safe = direct_settings.capital_direct_safe_address
            delegate = direct_settings.capital_direct_safe_delegate_address
            counterparty = (
                context["destination_reference"] if outbound else context["source_reference"]
            )
            required_scope = (
                (rpc_url, safe, delegate, counterparty)
                if outbound
                else (rpc_url, safe, counterparty)
            )
            if not direct_settings.safe_spending_enabled or not all(required_scope):
                raise DomainRejected(
                    "SAFE_SPENDING_LIMIT_NOT_CONFIGURED",
                    "Safe RPC, account, delegate and destination scope are required",
                )
            if outbound:
                artifact = self.resolved_safe_spending.prepare_spend(
                    rpc_url=str(rpc_url),
                    safe=str(safe),
                    delegate=str(delegate),
                    recipient=str(counterparty),
                    amount=str(context["min_received"]),
                )
            else:
                artifact = self.resolved_safe_spending.prepare_deposit(
                    rpc_url=str(rpc_url),
                    safe=str(safe),
                    sender=str(counterparty),
                    amount=str(context["min_received"]),
                )
            version = self.service().record_direct_capital_safe_preview(
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
                "data": self.capital_snapshot(identity.user_id),
            }

    def register_direct_hyperliquid(self) -> None:
        @self.app.post("/api/capital/direct-operations/{operation_id}/hyperliquid-preview")
        def prepare_direct_hyperliquid_preview(
            operation_id: UUID,
            payload: DirectCapitalUnsignedPlanRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            context = self.service().direct_capital_operation_context(
                operation_id, identity.user_id, now=now
            )
            if int(context["version"]) != payload.expected_version:
                raise DomainRejected(
                    "VERSION_CONFLICT", "direct capital operation changed; refresh"
                )
            path = DirectCapitalPath(str(context["path"]))
            if path not in {
                DirectCapitalPath.VAULT_TO_HYPERLIQUID,
                DirectCapitalPath.HYPERLIQUID_TO_VAULT,
            }:
                raise DomainRejected(
                    "HYPERLIQUID_CAPITAL_PATH_INVALID",
                    "this capital operation does not contain a Hyperliquid leg",
                )
            direct_settings, _ = self.effective_direct_capital_settings(identity.user_id)
            bridge = direct_settings.capital_direct_hyperliquid_bridge_address
            owned = direct_settings.capital_direct_owned_arbitrum_address
            if bridge is None or owned is None:
                raise DomainRejected(
                    "HYPERLIQUID_CAPITAL_SCOPE_MISSING",
                    "official Bridge2 and the authorized Arbitrum wallet must be configured",
                )
            if bridge.lower() != HYPERLIQUID_BRIDGE2_ADDRESS:
                raise DomainRejected(
                    "HYPERLIQUID_BRIDGE_UNTRUSTED",
                    "configured bridge does not match the official Arbitrum Bridge2 deployment",
                )
            main_account = resolve_hyperliquid_main_account(
                base_url=direct_settings.hyperliquid_base_url,
                account_address=direct_settings.hyperliquid_account_address,
                api_wallet_address=direct_settings.hyperliquid_api_wallet_address,
            )
            if main_account is None:
                raise DomainRejected(
                    "HYPERLIQUID_MAIN_ACCOUNT_MISSING",
                    "a Hyperliquid main account or resolvable authorized API wallet is required",
                )
            if path is DirectCapitalPath.VAULT_TO_HYPERLIQUID:
                artifact = self.resolved_hyperliquid_capital.prepare_deposit(
                    base_url=direct_settings.hyperliquid_base_url,
                    main_account=main_account,
                    api_wallet_address=direct_settings.hyperliquid_api_wallet_address,
                    owned_arbitrum_address=owned,
                    bridge_address=bridge,
                    amount=str(context["min_received"]),
                    now=now,
                )
            else:
                artifact = self.resolved_hyperliquid_capital.prepare_withdrawal(
                    base_url=direct_settings.hyperliquid_base_url,
                    main_account=main_account,
                    api_wallet_address=direct_settings.hyperliquid_api_wallet_address,
                    destination=owned,
                    amount=str(context["amount"]),
                    max_fee=context["max_fee"],
                    now=now,
                )
            version = self.service().record_direct_capital_hyperliquid_preview(
                operation_id,
                identity.user_id,
                expected_version=payload.expected_version,
                final_confirmed=payload.final_confirmed,
                artifact=artifact,
                idempotency_key=payload.idempotency_key,
                now=now,
            )
            return {
                "operation_id": str(operation_id),
                "version": version,
                "preview_kind": artifact["kind"],
                "transport": "HYPERLIQUID_OFFICIAL_PROTOCOL_HUMAN_WALLET_HANDOFF",
                "agent_wallet": artifact["agentWallet"],
                "automatic_fallback": True,
                "fallback_reason": artifact["fallbackReason"],
                "signing": False,
                "broadcast": False,
                "artifact": artifact,
                "next_step": (
                    "The main account or valid multisig wallet must re-check chain, destination, "
                    "amount, fee and method before signing. This service stores no signature "
                    "material."
                ),
                "data": self.capital_snapshot(identity.user_id),
            }

        @self.app.post("/api/capital/direct-operations/{operation_id}/wallet-submission")
        def record_direct_wallet_submission(
            operation_id: UUID,
            payload: DirectCapitalWalletSubmissionRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            version = self.service().record_direct_capital_wallet_submission(
                operation_id,
                identity.user_id,
                expected_version=payload.expected_version,
                stage=payload.stage,
                outcome=payload.outcome,
                transaction_hash=payload.transaction_hash,
                action_hash=payload.action_hash,
                nonce=payload.nonce,
                final_confirmed=payload.final_confirmed,
                idempotency_key=payload.idempotency_key,
                now=_now(),
            )
            return {
                "operation_id": str(operation_id),
                "version": version,
                "outcome": payload.outcome,
                "signing_material_stored": False,
                "data": self.capital_snapshot(identity.user_id),
            }

        @self.app.post("/api/capital/direct-operations/{operation_id}/hyperliquid-receipt")
        def verify_direct_hyperliquid_receipt(
            operation_id: UUID,
            payload: DirectCapitalHyperliquidReceiptRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            context = self.service().direct_capital_operation_context(
                operation_id, identity.user_id, now=now
            )
            if int(context["version"]) != payload.expected_version:
                raise DomainRejected(
                    "VERSION_CONFLICT", "direct capital operation changed; refresh"
                )
            direct_settings, _ = self.effective_direct_capital_settings(identity.user_id)
            main_account = resolve_hyperliquid_main_account(
                base_url=direct_settings.hyperliquid_base_url,
                account_address=direct_settings.hyperliquid_account_address,
                api_wallet_address=direct_settings.hyperliquid_api_wallet_address,
            )
            owned = direct_settings.capital_direct_owned_arbitrum_address
            bridge = direct_settings.capital_direct_hyperliquid_bridge_address
            if main_account is None or owned is None or bridge is None:
                raise DomainRejected(
                    "HYPERLIQUID_CAPITAL_SCOPE_MISSING",
                    "receipt verification requires the frozen main account, owned wallet and "
                    "Bridge2",
                )
            artifact_stage = next(
                (
                    stage
                    for stage in reversed(context["stages"])
                    if isinstance(stage, dict)
                    and isinstance(stage.get("artifact"), dict)
                    and str(stage["artifact"].get("kind", "")).startswith("HYPERLIQUID_")
                ),
                None,
            )
            if artifact_stage is None:
                raise DomainRejected(
                    "HYPERLIQUID_CAPITAL_PREFLIGHT_REQUIRED",
                    "prepare a current unsigned Hyperliquid wallet request before verifying "
                    "receipts",
                )
            artifact = dict(artifact_stage["artifact"])
            try:
                prepared_at = datetime.fromisoformat(str(artifact["preparedAt"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise DomainRejected(
                    "HYPERLIQUID_CAPITAL_PLAN_INVALID", "stored Hyperliquid preflight is invalid"
                ) from exc
            expected_submission_code = (
                "HYPERLIQUID_DEPOSIT_SUBMITTED_BY_HUMAN_WALLET"
                if payload.stage.startswith("HYPERLIQUID_DEPOSIT")
                else "HYPERLIQUID_CLASS_TRANSFER_SUBMITTED_BY_HUMAN_WALLET"
                if payload.stage == "HYPERLIQUID_CLASS_TRANSFER_LEDGER"
                else "HYPERLIQUID_WITHDRAWAL_SUBMITTED_BY_HUMAN_WALLET"
            )
            submission = next(
                (
                    stage
                    for stage in reversed(context["stages"])
                    if isinstance(stage, dict) and stage.get("code") == expected_submission_code
                ),
                None,
            )
            if submission is None:
                raise DomainRejected(
                    "HYPERLIQUID_WALLET_SUBMISSION_REQUIRED",
                    "record the human wallet submission before verifying public receipts",
                )
            if payload.stage.startswith("HYPERLIQUID_DEPOSIT"):
                submitted_hash = submission.get("transaction_hash")
                supplied_hash = payload.transaction_hash or payload.action_hash
                if (
                    submitted_hash is None
                    or str(submitted_hash).lower() != str(supplied_hash).lower()
                ):
                    raise DomainRejected(
                        "HYPERLIQUID_RECEIPT_REFERENCE_MISMATCH",
                        "deposit receipt reference does not match the recorded wallet submission",
                    )
            elif payload.stage in {
                "HYPERLIQUID_WITHDRAWAL_LEDGER",
                "HYPERLIQUID_CLASS_TRANSFER_LEDGER",
            } and (
                str(submission.get("action_hash", "")).lower() != str(payload.action_hash).lower()
                or submission.get("nonce") != payload.nonce
            ):
                raise DomainRejected(
                    "HYPERLIQUID_RECEIPT_REFERENCE_MISMATCH",
                    "withdrawal ledger evidence does not match the recorded signed action",
                )
            if payload.stage.endswith("LEDGER"):
                evidence = self.resolved_hyperliquid_capital.verify_hyperliquid_ledger(
                    base_url=direct_settings.hyperliquid_base_url,
                    main_account=main_account,
                    receipt_kind=(
                        "DEPOSIT"
                        if "DEPOSIT" in payload.stage
                        else "CLASS_TRANSFER"
                        if "CLASS_TRANSFER" in payload.stage
                        else "WITHDRAWAL"
                    ),
                    amount=str(artifact["amount"]),
                    prepared_at=prepared_at,
                    nonce=payload.nonce,
                    action_hash=payload.action_hash,
                    now=now,
                )
            else:
                rpc_url = (
                    direct_settings.capital_arbitrum_rpc_url
                    or direct_settings.safe_spending_arbitrum_rpc_url
                )
                if rpc_url is None:
                    raise DomainRejected(
                        "ARBITRUM_RPC_NOT_CONFIGURED",
                        "a trusted Arbitrum RPC is required for public receipt verification",
                    )
                if payload.stage == "HYPERLIQUID_DEPOSIT_ARBITRUM":
                    evidence = self.resolved_hyperliquid_capital.verify_arbitrum_usdc_transfer(
                        rpc_url=rpc_url,
                        transaction_hash=str(payload.transaction_hash),
                        sender=owned,
                        recipient=bridge,
                        amount=str(artifact["amount"]),
                        min_confirmations=direct_settings.notilt_arbitrum_min_confirmations,
                    )
                else:
                    evidence = self.resolved_hyperliquid_capital.verify_arbitrum_usdc_credit(
                        rpc_url=rpc_url,
                        transaction_hash=str(payload.transaction_hash),
                        sender=bridge,
                        recipient=owned,
                        amount=str(artifact["amount"]),
                        min_confirmations=direct_settings.notilt_arbitrum_min_confirmations,
                    )
            version = self.service().record_direct_capital_hyperliquid_receipt(
                operation_id,
                identity.user_id,
                expected_version=payload.expected_version,
                stage=payload.stage,
                evidence=evidence,
                idempotency_key=payload.idempotency_key,
                now=now,
            )
            return {
                "operation_id": str(operation_id),
                "version": version,
                "receipt": evidence,
                "settlement": "HYPERLIQUID_LEG_CONFIRMED_TREASURY_RECEIPT_STILL_REQUIRED",
                "data": self.capital_snapshot(identity.user_id),
            }

    def register_reconciliation_automation(self) -> None:
        @self.app.post("/api/capital/direct-operations/{operation_id}/treasury-receipt")
        def verify_direct_treasury_receipt(
            operation_id: UUID,
            payload: DirectCapitalTreasuryReceiptRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            context = self.service().direct_capital_operation_context(
                operation_id, identity.user_id, now=now
            )
            if int(context["version"]) != payload.expected_version:
                raise DomainRejected(
                    "VERSION_CONFLICT", "direct capital operation changed; refresh"
                )
            if context["path"] != DirectCapitalPath.HYPERLIQUID_TO_VAULT.value:
                raise DomainRejected(
                    "TREASURY_RECEIPT_STAGE_INVALID",
                    "treasury receipt is only valid after a Hyperliquid withdrawal",
                )
            submitted = next(
                (
                    stage
                    for stage in reversed(context["stages"])
                    if stage.get("code") == "TREASURY_DEPOSIT_SUBMITTED_BY_HUMAN_WALLET"
                ),
                None,
            )
            if submitted is None or str(submitted.get("transaction_hash", "")).lower() != (
                payload.transaction_hash.lower()
            ):
                raise DomainRejected(
                    "TREASURY_RECEIPT_REFERENCE_MISMATCH",
                    "treasury receipt does not match a recorded human wallet submission",
                )
            direct_settings, _ = self.effective_direct_capital_settings(identity.user_id)
            owned = direct_settings.capital_direct_owned_arbitrum_address
            if owned is None:
                raise DomainRejected(
                    "CAPITAL_OWNED_ARBITRUM_ADDRESS_MISSING",
                    "treasury receipt verification requires the authorized owned wallet",
                )
            if context["treasury_provider"] == "NOTILT_VAULT":
                chain_id = self.notilt_chain_id_for_network(str(context["network"]))
                _, vault = self.configured_notilt_scope(chain_id)
                receipt = self.resolved_notilt.verify_receipt(
                    chain_id=chain_id,
                    vault=vault,
                    agent=owned,
                    receipt_kind="DEPOSIT",
                    transaction_hash=payload.transaction_hash,
                    min_confirmations=direct_settings.notilt_min_confirmations[chain_id],
                    asset=str(context["asset"]),
                    amount=str(context["min_received"]),
                )
                evidence = {
                    "kind": "NOTILT_DEPOSIT_RECEIPT",
                    "transaction_hash": receipt.transaction_hash,
                    "block_number": receipt.block_number,
                    "confirmations": receipt.confirmations,
                    "amount": (
                        None if receipt.credited_amount is None else str(receipt.credited_amount)
                    ),
                }
            else:
                safe = direct_settings.capital_direct_safe_address
                rpc_url = (
                    direct_settings.capital_arbitrum_rpc_url
                    or direct_settings.safe_spending_arbitrum_rpc_url
                )
                if safe is None or rpc_url is None:
                    raise DomainRejected(
                        "SAFE_SPENDING_LIMIT_NOT_CONFIGURED",
                        "Safe address and trusted Arbitrum RPC are required for receipt "
                        "verification",
                    )
                evidence = self.resolved_hyperliquid_capital.verify_arbitrum_usdc_transfer(
                    rpc_url=rpc_url,
                    transaction_hash=payload.transaction_hash,
                    sender=owned,
                    recipient=safe,
                    amount=str(context["min_received"]),
                    min_confirmations=direct_settings.notilt_arbitrum_min_confirmations,
                )
            version = self.service().record_direct_capital_treasury_receipt(
                operation_id,
                identity.user_id,
                expected_version=payload.expected_version,
                evidence=evidence,
                idempotency_key=payload.idempotency_key,
                now=now,
            )
            return {
                "operation_id": str(operation_id),
                "version": version,
                "receipt": evidence,
                "settlement": "SETTLED_IF_ALL_HYPERLIQUID_AND_TREASURY_RECEIPTS_CONFIRMED",
                "data": self.capital_snapshot(identity.user_id),
            }

        @self.app.post("/api/capital/balances/mock")
        def record_mock_capital_balance(
            payload: CapitalBalanceFactRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            if self.resolved_settings.environment not in {"local", "test"}:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
            fact_id = self.service().record_capital_balance(
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
                "data": self.capital_snapshot(identity.user_id),
            }

        @self.app.post("/api/capital/reconciliations")
        def reconcile_capital_scope(
            payload: CapitalScopeReconciliationRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, str]:
            reconciliation_id = self.service().record_capital_scope_reconciliation(
                actor_id=identity.user_id,
                environment=ExecutionEnvironment(payload.environment),
                account_id=payload.account_id,
                venue=payload.venue,
                now=_now(),
            )
            return {"reconciliation_id": str(reconciliation_id)}

        @self.app.post("/api/capital/automation/policies")
        def set_capital_automation_policy(
            payload: CapitalAutomationPolicyRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            policy_id = self.service().set_capital_automation_policy(
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
                "data": self.capital_snapshot(identity.user_id),
            }

        @self.app.post("/api/capital/automation/policies/{policy_id}/evaluate")
        def evaluate_capital_automation_policy(
            policy_id: UUID,
            payload: CapitalAutomationEvaluateRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            proposal_id, reason = self.service().create_capital_automation_candidate(
                policy_id,
                payload.purpose,
                identity.user_id,
                payload.idempotency_key,
                now=_now(),
            )
            if proposal_id is not None:
                detail = self.queries().transfer_proposal_detail(identity.user_id, proposal_id)
                self.notify_capital(
                    object_id=proposal_id,
                    object_type="TransferProposal",
                    event_type="PENDING_REVIEW",
                    actor_id=identity.user_id,
                    team_id=UUID(str(detail["team_id"])),
                    environment=str(detail["environment"]),
                    account_id=str(detail["account_id"]),
                    venue=str(detail["venue"]),
                    object_version=int(detail["version"]),
                    summary="资金候选需要两名独立 Treasury Reviewer 审核。",
                )
            return {
                "transfer_proposal_id": None if proposal_id is None else str(proposal_id),
                "reason": reason,
                "data": self.capital_snapshot(identity.user_id),
            }

    def register_transfer(self) -> None:
        @self.app.post("/api/capital/proposals")
        def create_transfer_proposal(
            payload: TransferProposalRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            environment = ExecutionEnvironment(payload.environment)
            allow_live_unsigned = False
            if environment is ExecutionEnvironment.LIVE:
                chain_id = self.notilt_chain_id_for_network(payload.network)
                _, configured_vault = self.configured_notilt_scope(chain_id)
                if payload.vault_id.lower() != configured_vault.lower():
                    raise DomainRejected(
                        "NOTILT_VAULT_SCOPE_MISMATCH",
                        "LIVE transfer proposal must use the configured Vault for its chain",
                    )
                allow_live_unsigned = True
            proposal_id = self.service().create_transfer_proposal(
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
            return self.queries().transfer_proposal_detail(identity.user_id, proposal_id)

        @self.app.post("/api/capital/proposals/{transfer_proposal_id}/submit")
        def submit_transfer_proposal(
            transfer_proposal_id: UUID,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.service().submit_transfer_proposal(
                transfer_proposal_id, identity.user_id, now=_now()
            )
            detail = self.queries().transfer_proposal_detail(identity.user_id, transfer_proposal_id)
            self.notify_capital(
                object_id=transfer_proposal_id,
                object_type="TransferProposal",
                event_type="PENDING_REVIEW",
                actor_id=identity.user_id,
                team_id=UUID(str(detail["team_id"])),
                environment=str(detail["environment"]),
                account_id=str(detail["account_id"]),
                venue=str(detail["venue"]),
                object_version=int(detail["version"]),
                summary="资金划转提案需要两名独立 Treasury Reviewer 审核。",
            )
            return detail

        @self.app.post("/api/capital/proposals/{transfer_proposal_id}/reviews")
        def review_transfer_proposal(
            transfer_proposal_id: UUID,
            payload: TransferReviewRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            if payload.decision == "APPROVE":
                if payload.action_grant is None:
                    raise DomainRejected(
                        "ACTION_GRANT_REQUIRED", "capital approval requires action-level step-up"
                    )
                self.token_service.verify_action_grant(
                    payload.action_grant,
                    user_id=identity.user_id,
                    action="capital.approve",
                    object_id=transfer_proposal_id,
                    object_version=payload.expected_version,
                    now=now,
                )
            self.service().review_transfer_proposal(
                transfer_proposal_id,
                identity.user_id,
                ReviewDecision(payload.decision),
                payload.reason,
                payload.expected_version,
                now=now,
            )
            detail = self.queries().transfer_proposal_detail(identity.user_id, transfer_proposal_id)
            self.notify_capital(
                object_id=transfer_proposal_id,
                object_type="TransferProposal",
                event_type=f"REVIEW_{payload.decision}",
                actor_id=identity.user_id,
                team_id=UUID(str(detail["team_id"])),
                environment=str(detail["environment"]),
                account_id=str(detail["account_id"]),
                venue=str(detail["venue"]),
                object_version=int(detail["version"]),
                summary=f"资金划转审核结果已记录：{payload.decision}。",  # noqa: RUF001
            )
            return detail

        @self.app.post("/api/capital/proposals/{transfer_proposal_id}/authorizations")
        def issue_transfer_authorization(
            transfer_proposal_id: UUID,
            payload: TransferAuthorizationRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            proposal = self.queries().transfer_proposal_detail(
                identity.user_id, transfer_proposal_id
            )
            expires_at = min(
                datetime.fromisoformat(str(proposal["expires_at"])),
                now + timedelta(minutes=payload.expires_in_minutes),
            )
            authorization_id = self.service().issue_transfer_authorization(
                transfer_proposal_id,
                identity.user_id,
                expires_at,
                payload.idempotency_key,
                now=now,
            )
            return {
                "transfer_authorization_id": str(authorization_id),
                "detail": self.queries().transfer_proposal_detail(
                    identity.user_id, transfer_proposal_id
                ),
            }

        @self.app.post("/api/capital/authorizations/{transfer_authorization_id}/transfers/mock")
        def submit_mock_capital_transfer(
            transfer_authorization_id: UUID,
            payload: CapitalTransferCreateRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            if self.resolved_settings.environment not in {"local", "test"}:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
            now = _now()
            transfer_id = self.service().reserve_capital_transfer(
                transfer_authorization_id,
                identity.user_id,
                payload.idempotency_key,
                now=now,
            )
            detail = self.queries().capital_transfer_detail(identity.user_id, transfer_id)
            if detail["status"] == CapitalTransferStatus.SOURCE_RESERVED.value:
                command = self.service().capital_transfer_command(
                    transfer_id, identity.user_id, now=now
                )
                submission = self.resolved_capital_transfer.submit(command, now=now)
                self.service().record_capital_submission(
                    transfer_id, identity.user_id, submission, now=now
                )
                detail = self.queries().capital_transfer_detail(identity.user_id, transfer_id)
            self.notify_capital(
                object_id=transfer_id,
                object_type="CapitalTransfer",
                event_type=str(detail["status"]),
                actor_id=identity.user_id,
                team_id=UUID(str(detail["team_id"])),
                environment=str(detail["environment"]),
                account_id=str(detail["account_id"]),
                venue=str(detail["venue"]),
                object_version=int(detail["version"]),
                summary="Mock 资金划转已提交；没有移动真实资金。",  # noqa: RUF001
            )
            return {"transport": "MOCK_ONLY", "detail": detail}

    def register_notilt_transfer(self) -> None:
        @self.app.post(
            "/api/capital/authorizations/{transfer_authorization_id}/transfers/notilt-plan"
        )
        def prepare_notilt_capital_transfer(
            transfer_authorization_id: UUID,
            payload: CapitalTransferCreateRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            transfer_id = self.service().reserve_capital_transfer(
                transfer_authorization_id,
                identity.user_id,
                payload.idempotency_key,
                now=now,
                allow_live_unsigned=True,
            )
            existing = self.queries().capital_transfer_detail(identity.user_id, transfer_id)
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
                        self.service()
                        .notilt_transfer_command(transfer_id, identity.user_id)
                        .min_received
                    ),
                    "transactions": existing["planned_transactions"],
                    "next_step": (
                        "Confirm the exact persisted transaction plan in the independent wallet."
                    ),
                    "detail": existing,
                }
            command = self.service().capital_transfer_command(
                transfer_id,
                identity.user_id,
                now=now,
            )
            if command.environment is not ExecutionEnvironment.LIVE:
                raise DomainRejected(
                    "NOTILT_TRANSFER_ENVIRONMENT_INVALID",
                    "NoTilt transaction plans are only available for LIVE authorizations",
                )
            chain_id = self.notilt_chain_id_for_network(command.network)
            agent, vault = self.configured_notilt_scope(chain_id)
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
                    self.resolved_notilt.prepare_release_request(
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
                transactions = self.resolved_notilt.prepare_deposit(
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
            self.service().record_notilt_plan(
                transfer_id,
                identity.user_id,
                chain_id=chain_id,
                transport_state=plan_state,
                transactions=transactions,
                now=now,
            )
            detail = self.queries().capital_transfer_detail(identity.user_id, transfer_id)
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

        @self.app.post("/api/capital/transfers/{capital_transfer_id}/notilt-release-execution-plan")
        def prepare_notilt_release_execution(
            capital_transfer_id: UUID,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            detail = self.queries().capital_transfer_detail(identity.user_id, capital_transfer_id)
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
            command = self.service().notilt_transfer_command(capital_transfer_id, identity.user_id)
            chain_id = self.notilt_chain_id_for_network(command.network)
            agent, vault = self.configured_notilt_scope(chain_id)
            transaction = self.resolved_notilt.prepare_release_execution(
                chain_id=chain_id,
                vault=vault,
                agent=agent,
                request_id=str(detail["protocol_request_id"]),
            )
            self.service().record_notilt_plan(
                capital_transfer_id,
                identity.user_id,
                chain_id=chain_id,
                transport_state="RELEASE_EXECUTION_PLAN_READY",
                transactions=(transaction,),
                now=now,
            )
            updated = self.queries().capital_transfer_detail(identity.user_id, capital_transfer_id)
            return {
                "transport": "NOTILT_UNSIGNED_TRANSACTION_HANDOFF",
                "broadcast": False,
                "signing": "EXTERNAL_WALLET_REQUIRED",
                "transactions": updated["planned_transactions"],
                "detail": updated,
            }

        @self.app.post(
            "/api/capital/transfers/{capital_transfer_id}/notilt-release-cancellation-plan"
        )
        def prepare_notilt_release_cancellation(
            capital_transfer_id: UUID,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            detail = self.queries().capital_transfer_detail(identity.user_id, capital_transfer_id)
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
            command = self.service().notilt_transfer_command(capital_transfer_id, identity.user_id)
            chain_id = self.notilt_chain_id_for_network(command.network)
            agent, vault = self.configured_notilt_scope(chain_id)
            transaction = self.resolved_notilt.prepare_release_cancellation(
                chain_id=chain_id,
                vault=vault,
                agent=agent,
                request_id=str(detail["protocol_request_id"]),
            )
            self.service().record_notilt_plan(
                capital_transfer_id,
                identity.user_id,
                chain_id=chain_id,
                transport_state="RELEASE_CANCELLATION_PLAN_READY",
                transactions=(transaction,),
                now=now,
            )
            updated = self.queries().capital_transfer_detail(identity.user_id, capital_transfer_id)
            return {
                "transport": "NOTILT_UNSIGNED_TRANSACTION_HANDOFF",
                "broadcast": False,
                "signing": "EXTERNAL_WALLET_REQUIRED",
                "transactions": updated["planned_transactions"],
                "detail": updated,
            }

        @self.app.post("/api/capital/transfers/{capital_transfer_id}/notilt-receipt")
        def verify_notilt_capital_receipt(
            capital_transfer_id: UUID,
            payload: NoTiltReceiptRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            detail = self.queries().capital_transfer_detail(identity.user_id, capital_transfer_id)
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
            command = self.service().notilt_transfer_command(capital_transfer_id, identity.user_id)
            chain_id = self.notilt_chain_id_for_network(command.network)
            agent, vault = self.configured_notilt_scope(chain_id)
            receipt = self.resolved_notilt.verify_receipt(
                chain_id=chain_id,
                vault=vault,
                agent=agent,
                receipt_kind=receipt_kind,
                transaction_hash=payload.transaction_hash,
                min_confirmations=self.resolved_settings.notilt_min_confirmations[chain_id],
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
            transport_state = self.service().record_notilt_receipt(
                capital_transfer_id,
                identity.user_id,
                receipt,
                now=now,
            )
            vault_sync: dict[str, Any] = {"attempted": False}
            if receipt_kind in {"DEPOSIT", "RELEASE_EXECUTION"}:
                vault_sync = {"attempted": True}
                try:
                    fact_count, _ = self.sync_configured_notilt_vault(
                        chain_id,
                        identity.user_id,
                        now=now,
                    )
                    vault_sync.update({"status": "SYNCED", "facts_recorded": fact_count})
                except DomainRejected as exc:
                    vault_sync.update({"status": "FAILED", "error_code": exc.code})
            updated = self.queries().capital_transfer_detail(identity.user_id, capital_transfer_id)
            self.notify_capital(
                object_id=capital_transfer_id,
                object_type="CapitalTransfer",
                event_type=f"NOTILT_{receipt_kind}_CONFIRMED",
                actor_id=identity.user_id,
                team_id=UUID(str(updated["team_id"])),
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

        @self.app.get("/api/capital/transfers/{capital_transfer_id}")
        def capital_transfer_detail(
            capital_transfer_id: UUID,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.queries().capital_transfer_detail(identity.user_id, capital_transfer_id)

    def register_transfer_reconciliation(self) -> None:
        @self.app.post("/api/capital/transfers/{capital_transfer_id}/observations/mock")
        def observe_mock_capital_transfer(
            capital_transfer_id: UUID,
            payload: CapitalTransferObservationRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            if self.resolved_settings.environment not in {"local", "test"}:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
            self.service().record_capital_observation(
                capital_transfer_id,
                identity.user_id,
                CapitalTransferStatus(payload.status),
                transaction_reference=payload.transaction_reference,
                fee_amount=payload.fee_amount,
                net_received=payload.net_received,
                now=_now(),
            )
            detail = self.queries().capital_transfer_detail(identity.user_id, capital_transfer_id)
            self.notify_capital(
                object_id=capital_transfer_id,
                object_type="CapitalTransfer",
                event_type=str(detail["status"]),
                actor_id=identity.user_id,
                team_id=UUID(str(detail["team_id"])),
                environment=str(detail["environment"]),
                account_id=str(detail["account_id"]),
                venue=str(detail["venue"]),
                object_version=int(detail["version"]),
                summary=f"资金划转状态已变更为 {detail['status']}。",
            )
            return {"transport": "MOCK_ONLY", "detail": detail}

        @self.app.post("/api/capital/transfers/{capital_transfer_id}/reconcile")
        def reconcile_capital_transfer(
            capital_transfer_id: UUID,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            result = self.service().reconcile_capital_transfer(
                capital_transfer_id, identity.user_id, now=_now()
            )
            detail = self.queries().capital_transfer_detail(identity.user_id, capital_transfer_id)
            self.notify_capital(
                object_id=capital_transfer_id,
                object_type="CapitalTransfer",
                event_type=f"RECONCILIATION_{result}",
                actor_id=identity.user_id,
                team_id=UUID(str(detail["team_id"])),
                environment=str(detail["environment"]),
                account_id=str(detail["account_id"]),
                venue=str(detail["venue"]),
                object_version=int(detail["version"]),
                summary=f"资金对账结果为 {result}。",
            )
            return {"reconciliation_status": result, "detail": detail}


def register_capital_routes(context: ApiRouteContext) -> None:
    """Register capital routes from bounded lifecycle groups."""

    routes = _CapitalRoutes(context)
    routes.register_configuration()
    routes.register_direct_binance()
    routes.register_direct_treasury()
    routes.register_direct_hyperliquid()
    routes.register_reconciliation_automation()
    routes.register_transfer()
    routes.register_notilt_transfer()
    routes.register_transfer_reconciliation()
