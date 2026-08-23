from __future__ import annotations

from trading_control_plane.api_core import (
    UUID,
    Any,
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
    HTTPException,
    NoTiltReceiptRequest,
    SessionIdentity,
    TransferAuthorizationRequest,
    TransferProposalRequest,
    TransferReviewRequest,
    status,
)
from trading_control_plane.api_routes.context import ApiRouteContext
from trading_control_plane.capital_configuration_use_cases import (
    DirectCapitalConfigurationInput,
)


class _CapitalRoutes:
    def __init__(self, context: ApiRouteContext) -> None:
        dependencies = context.capital
        common = dependencies.common
        self.app = context.app
        self.configuration = dependencies.configuration
        self.direct = dependencies.direct
        self.identity_dependency = common.identity
        self.receipts = dependencies.receipts
        self.resolved_settings = common.settings
        self.transfers = dependencies.transfers

    def register_configuration(self) -> None:
        @self.app.get("/api/capital/direct-configurations")
        def direct_capital_configurations(
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.configuration.direct_capital_configurations(actor_id=identity.user_id)

        @self.app.get("/api/notilt/status")
        def notilt_status(
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.configuration.notilt_status(actor_id=identity.user_id)

        @self.app.get("/api/notilt/chains/{chain_id}/assignment")
        def notilt_assignment(
            chain_id: int,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.configuration.notilt_assignment(chain_id, actor_id=identity.user_id)

        @self.app.post("/api/notilt/chains/{chain_id}/sync")
        def sync_notilt_vault(
            chain_id: int,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.configuration.sync_notilt_vault(chain_id, actor_id=identity.user_id)

        @self.app.get("/api/capital")
        def capital_center(
            environment: str | None = None,
            accounts: str | None = None,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.configuration.capital_center(
                environment, accounts, actor_id=identity.user_id
            )

        @self.app.put("/api/capital/direct-configuration")
        def update_direct_capital_configuration(
            payload: DirectCapitalConfigurationRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            request = DirectCapitalConfigurationInput(
                environment=payload.environment,
                network=payload.network,
                asset=payload.asset,
                treasury_provider=payload.treasury_provider,
                vault_id=payload.vault_id,
                vault_address=payload.vault_address,
                owned_arbitrum_address=payload.owned_arbitrum_address,
                binance_account_id=payload.binance_account_id,
                binance_deposit_address=payload.binance_deposit_address,
                binance_withdrawal_address=payload.binance_withdrawal_address,
                hyperliquid_account_id=payload.hyperliquid_account_id,
                hyperliquid_bridge_address=payload.hyperliquid_bridge_address,
                safe_address=payload.safe_address,
                safe_delegate_address=payload.safe_delegate_address,
                clear_notilt_configuration=payload.clear_notilt_configuration,
                clear_safe_configuration=payload.clear_safe_configuration,
                vault_withdrawal_private_key=(
                    None
                    if payload.vault_withdrawal_private_key is None
                    else payload.vault_withdrawal_private_key.get_secret_value()
                ),
                safe_withdrawal_private_key=(
                    None
                    if payload.safe_withdrawal_private_key is None
                    else payload.safe_withdrawal_private_key.get_secret_value()
                ),
                max_amount=payload.max_amount,
                max_fee=payload.max_fee,
                idempotency_key=payload.idempotency_key,
            )
            return self.configuration.update_direct_capital_configuration(
                request,
                actor_id=identity.user_id,
            )

        @self.app.post("/api/capital/direct-operations")
        def create_direct_capital_operation(
            payload: DirectCapitalOperationRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.configuration.create_direct_capital_operation(
                payload, actor_id=identity.user_id
            )

    def register_direct_binance(self) -> None:
        @self.app.post("/api/capital/direct-operations/{operation_id}/binance-preview")
        def prepare_direct_binance_preview(
            operation_id: UUID,
            payload: DirectCapitalUnsignedPlanRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.direct.prepare_direct_binance_preview(
                operation_id, payload, actor_id=identity.user_id
            )

        @self.app.post("/api/capital/direct-operations/{operation_id}/binance-submit")
        def submit_direct_binance_withdrawal(
            operation_id: UUID,
            payload: DirectCapitalBinanceSubmissionRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.direct.submit_direct_binance_withdrawal(
                operation_id, payload, actor_id=identity.user_id
            )

        @self.app.post("/api/capital/direct-operations/{operation_id}/binance-receipt")
        def verify_direct_binance_receipt(
            operation_id: UUID,
            payload: DirectCapitalBinanceReceiptRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.receipts.verify_direct_binance_receipt(
                operation_id, payload, actor_id=identity.user_id
            )

    def register_direct_treasury(self) -> None:
        @self.app.post("/api/capital/direct-operations/{operation_id}/notilt-unsigned-preview")
        def prepare_direct_notilt_unsigned_preview(
            operation_id: UUID,
            payload: DirectCapitalUnsignedPlanRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.direct.prepare_direct_notilt_unsigned_preview(
                operation_id, payload, actor_id=identity.user_id
            )

        @self.app.post(
            "/api/capital/direct-operations/{operation_id}/notilt-release-execution-preview"
        )
        def prepare_direct_notilt_release_execution(
            operation_id: UUID,
            payload: DirectCapitalUnsignedPlanRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.direct.prepare_direct_notilt_release_execution(
                operation_id, payload, actor_id=identity.user_id
            )

        @self.app.post("/api/capital/direct-operations/{operation_id}/notilt-release-receipt")
        def verify_direct_notilt_release_receipt(
            operation_id: UUID,
            payload: DirectCapitalTreasuryReceiptRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.receipts.verify_direct_notilt_release_receipt(
                operation_id, payload, actor_id=identity.user_id
            )

        @self.app.post("/api/capital/direct-operations/{operation_id}/notilt-destination-preview")
        def prepare_direct_notilt_destination_transfer(
            operation_id: UUID,
            payload: DirectCapitalUnsignedPlanRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.direct.prepare_direct_notilt_destination_transfer(
                operation_id, payload, actor_id=identity.user_id
            )

        @self.app.post("/api/capital/direct-operations/{operation_id}/safe-spending-preview")
        def prepare_direct_safe_spending_preview(
            operation_id: UUID,
            payload: DirectCapitalUnsignedPlanRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.direct.prepare_direct_safe_spending_preview(
                operation_id, payload, actor_id=identity.user_id
            )

        @self.app.post("/api/capital/direct-operations/{operation_id}/treasury-withdrawal-receipt")
        def verify_direct_treasury_withdrawal_receipt(
            operation_id: UUID,
            payload: DirectCapitalTreasuryReceiptRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.receipts.verify_direct_treasury_withdrawal_receipt(
                operation_id, payload, actor_id=identity.user_id
            )

    def register_direct_hyperliquid(self) -> None:
        @self.app.post("/api/capital/direct-operations/{operation_id}/hyperliquid-preview")
        def prepare_direct_hyperliquid_preview(
            operation_id: UUID,
            payload: DirectCapitalUnsignedPlanRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.direct.prepare_direct_hyperliquid_preview(
                operation_id, payload, actor_id=identity.user_id
            )

        @self.app.post("/api/capital/direct-operations/{operation_id}/wallet-submission")
        def record_direct_wallet_submission(
            operation_id: UUID,
            payload: DirectCapitalWalletSubmissionRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.direct.record_direct_wallet_submission(
                operation_id, payload, actor_id=identity.user_id
            )

        @self.app.post("/api/capital/direct-operations/{operation_id}/hyperliquid-receipt")
        def verify_direct_hyperliquid_receipt(
            operation_id: UUID,
            payload: DirectCapitalHyperliquidReceiptRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.receipts.verify_direct_hyperliquid_receipt(
                operation_id, payload, actor_id=identity.user_id
            )

    def register_reconciliation_automation(self) -> None:
        @self.app.post("/api/capital/direct-operations/{operation_id}/treasury-receipt")
        def verify_direct_treasury_receipt(
            operation_id: UUID,
            payload: DirectCapitalTreasuryReceiptRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.receipts.verify_direct_treasury_receipt(
                operation_id, payload, actor_id=identity.user_id
            )

        @self.app.post("/api/capital/balances/mock")
        def record_mock_capital_balance(
            payload: CapitalBalanceFactRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            if self.resolved_settings.environment not in {"local", "test"}:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
            return self.transfers.record_mock_capital_balance(payload, actor_id=identity.user_id)

        @self.app.post("/api/capital/reconciliations")
        def reconcile_capital_scope(
            payload: CapitalScopeReconciliationRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, str]:
            return self.transfers.reconcile_capital_scope(payload, actor_id=identity.user_id)

        @self.app.post("/api/capital/automation/policies")
        def set_capital_automation_policy(
            payload: CapitalAutomationPolicyRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.transfers.set_capital_automation_policy(payload, actor_id=identity.user_id)

        @self.app.post("/api/capital/automation/policies/{policy_id}/evaluate")
        def evaluate_capital_automation_policy(
            policy_id: UUID,
            payload: CapitalAutomationEvaluateRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.transfers.evaluate_capital_automation_policy(
                policy_id, payload, actor_id=identity.user_id
            )

    def register_transfer(self) -> None:
        @self.app.post("/api/capital/proposals")
        def create_transfer_proposal(
            payload: TransferProposalRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.transfers.create_transfer_proposal(payload, actor_id=identity.user_id)

        @self.app.post("/api/capital/proposals/{transfer_proposal_id}/submit")
        def submit_transfer_proposal(
            transfer_proposal_id: UUID,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.transfers.submit_transfer_proposal(
                transfer_proposal_id, actor_id=identity.user_id
            )

        @self.app.post("/api/capital/proposals/{transfer_proposal_id}/reviews")
        def review_transfer_proposal(
            transfer_proposal_id: UUID,
            payload: TransferReviewRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.transfers.review_transfer_proposal(
                transfer_proposal_id, payload, actor_id=identity.user_id
            )

        @self.app.post("/api/capital/proposals/{transfer_proposal_id}/authorizations")
        def issue_transfer_authorization(
            transfer_proposal_id: UUID,
            payload: TransferAuthorizationRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.transfers.issue_transfer_authorization(
                transfer_proposal_id, payload, actor_id=identity.user_id
            )

        @self.app.post("/api/capital/authorizations/{transfer_authorization_id}/transfers/mock")
        def submit_mock_capital_transfer(
            transfer_authorization_id: UUID,
            payload: CapitalTransferCreateRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.transfers.submit_mock_capital_transfer(
                transfer_authorization_id, payload, actor_id=identity.user_id
            )

    def register_notilt_transfer(self) -> None:
        @self.app.post(
            "/api/capital/authorizations/{transfer_authorization_id}/transfers/notilt-plan"
        )
        def prepare_notilt_capital_transfer(
            transfer_authorization_id: UUID,
            payload: CapitalTransferCreateRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.transfers.prepare_notilt_capital_transfer(
                transfer_authorization_id, payload, actor_id=identity.user_id
            )

        @self.app.post("/api/capital/transfers/{capital_transfer_id}/notilt-release-execution-plan")
        def prepare_notilt_release_execution(
            capital_transfer_id: UUID,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.transfers.prepare_notilt_release_execution(
                capital_transfer_id, actor_id=identity.user_id
            )

        @self.app.post(
            "/api/capital/transfers/{capital_transfer_id}/notilt-release-cancellation-plan"
        )
        def prepare_notilt_release_cancellation(
            capital_transfer_id: UUID,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.transfers.prepare_notilt_release_cancellation(
                capital_transfer_id, actor_id=identity.user_id
            )

        @self.app.post("/api/capital/transfers/{capital_transfer_id}/notilt-receipt")
        def verify_notilt_capital_receipt(
            capital_transfer_id: UUID,
            payload: NoTiltReceiptRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.transfers.verify_notilt_capital_receipt(
                capital_transfer_id, payload, actor_id=identity.user_id
            )

        @self.app.get("/api/capital/transfers/{capital_transfer_id}")
        def capital_transfer_detail(
            capital_transfer_id: UUID,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.transfers.capital_transfer_detail(
                capital_transfer_id, actor_id=identity.user_id
            )

    def register_transfer_reconciliation(self) -> None:
        @self.app.post("/api/capital/transfers/{capital_transfer_id}/observations/mock")
        def observe_mock_capital_transfer(
            capital_transfer_id: UUID,
            payload: CapitalTransferObservationRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.transfers.observe_mock_capital_transfer(
                capital_transfer_id, payload, actor_id=identity.user_id
            )

        @self.app.post("/api/capital/transfers/{capital_transfer_id}/reconcile")
        def reconcile_capital_transfer(
            capital_transfer_id: UUID,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            return self.transfers.reconcile_capital_transfer(
                capital_transfer_id, actor_id=identity.user_id
            )


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
