from __future__ import annotations

from trading_control_plane.credentials import CredentialCipher
from trading_control_plane.database import Database
from trading_control_plane.service_component import ServiceRuntime
from trading_control_plane.service_core import (
    ROLE_ACTIONS as ROLE_ACTIONS,
)
from trading_control_plane.service_core import (
    PreparedExchangeConnectionVerification as PreparedExchangeConnectionVerification,
)
from trading_control_plane.service_core import (
    PreparedFreqtradeDispatch as PreparedFreqtradeDispatch,
)
from trading_control_plane.service_core import (
    PreparedFreqtradeWorkerBinding as PreparedFreqtradeWorkerBinding,
)
from trading_control_plane.service_core import (
    PreparedPerptapeRuntimeBinding as PreparedPerptapeRuntimeBinding,
)
from trading_control_plane.service_core import (
    PreparedRuntimeAccountBinding as PreparedRuntimeAccountBinding,
)
from trading_control_plane.service_domains.accounts import AccountService
from trading_control_plane.service_domains.capital_automation import AutomationCapitalService
from trading_control_plane.service_domains.capital_direct import DirectOperationCapitalService
from trading_control_plane.service_domains.capital_notilt import NoTiltCapitalService
from trading_control_plane.service_domains.capital_reconciliation import (
    ReconciliationCapitalService,
)
from trading_control_plane.service_domains.capital_transfer import TransferCapitalService
from trading_control_plane.service_domains.execution_campaign import CampaignExecutionService
from trading_control_plane.service_domains.execution_facts import FactIngestionExecutionService
from trading_control_plane.service_domains.execution_freqtrade import (
    FreqtradeRecoveryExecutionService,
)
from trading_control_plane.service_domains.execution_intent import IntentExecutionService
from trading_control_plane.service_domains.execution_shadow import ShadowExecutionService
from trading_control_plane.service_domains.execution_venue import VenueCommandExecutionService
from trading_control_plane.service_domains.proposals import ProposalService
from trading_control_plane.service_domains.risk_authorization import AuthorizationRiskService
from trading_control_plane.service_domains.risk_policy import PolicyRiskService
from trading_control_plane.service_domains.risk_reconciliation import ReconciliationRiskService
from trading_control_plane.service_domains.risk_recovery import RecoveryRiskService
from trading_control_plane.service_domains.signals import SignalService
from trading_control_plane.service_domains.workspace import WorkspaceService
from trading_control_plane.service_transactions import TransactionService


class TradingService:
    """Compatibility facade over typed, lifecycle-focused service components."""

    _validate_add_candidate = staticmethod(
        AuthorizationRiskService._validate_add_candidate
    )
    _canonical_restore_scopes = staticmethod(
        RecoveryRiskService._canonical_restore_scopes
    )

    def __init__(
        self,
        database: Database,
        *,
        authoritative_live_accounts: dict[str, str] | None = None,
        credential_encryption_key: str | None = None,
    ) -> None:
        credential_cipher = CredentialCipher(credential_encryption_key)
        transactions = TransactionService(database, credential_cipher)
        self.runtime = ServiceRuntime(
            database=database,
            credential_cipher=credential_cipher,
            authoritative_live_accounts={
                venue.upper(): account_id
                for venue, account_id in (authoritative_live_accounts or {}).items()
            },
            transactions=transactions,
        )
        self._workspace = WorkspaceService(self.runtime, self)
        self._accounts = AccountService(self.runtime, self)
        self._signals = SignalService(self.runtime, self)
        self._proposals = ProposalService(self.runtime, self)
        self._risk_policy = PolicyRiskService(self.runtime, self)
        self._risk_authorization = AuthorizationRiskService(self.runtime, self)
        self._risk_recovery = RecoveryRiskService(self.runtime, self)
        self._risk_reconciliation = ReconciliationRiskService(self.runtime, self)
        self._execution_intent = IntentExecutionService(self.runtime, self)
        self._execution_venue = VenueCommandExecutionService(self.runtime, self)
        self._execution_facts = FactIngestionExecutionService(self.runtime, self)
        self._execution_campaign = CampaignExecutionService(self.runtime, self)
        self._execution_shadow = ShadowExecutionService(self.runtime, self)
        self._execution_freqtrade = FreqtradeRecoveryExecutionService(self.runtime, self)
        self._capital_direct = DirectOperationCapitalService(self.runtime, self)
        self._capital_transfer = TransferCapitalService(self.runtime, self)
        self._capital_automation = AutomationCapitalService(self.runtime, self)
        self._capital_notilt = NoTiltCapitalService(self.runtime, self)
        self._capital_reconciliation = ReconciliationCapitalService(self.runtime, self)

        self._exchange_account_definition = self._accounts._exchange_account_definition
        self._ensure_exchange_account_reference = self._accounts._ensure_exchange_account_reference
        self.create_exchange_account = self._accounts.create_exchange_account
        self.rotate_exchange_account_credentials = (
            self._accounts.rotate_exchange_account_credentials
        )
        self._set_internal_principal_active = self._accounts._set_internal_principal_active
        self._require_exact_runtime_principal = self._accounts._require_exact_runtime_principal
        self._ensure_account_runtime_service_principal = (
            self._accounts._ensure_account_runtime_service_principal
        )
        self.configure_exchange_account_runtime_sync = (
            self._accounts.configure_exchange_account_runtime_sync
        )
        self.configure_exchange_account_trading = self._accounts.configure_exchange_account_trading
        self._freqtrade_auth_payload = self._accounts._freqtrade_auth_payload
        self._parse_freqtrade_auth_payload = self._accounts._parse_freqtrade_auth_payload
        self.configure_exchange_account_freqtrade_worker = (
            self._accounts.configure_exchange_account_freqtrade_worker
        )
        self._freqtrade_verification_payload = self._accounts._freqtrade_verification_payload
        self.prepare_exchange_account_freqtrade_verification = (
            self._accounts.prepare_exchange_account_freqtrade_verification
        )
        self._prepared_freqtrade_worker_binding = self._accounts._prepared_freqtrade_worker_binding
        self.record_exchange_account_freqtrade_verification = (
            self._accounts.record_exchange_account_freqtrade_verification
        )
        self.freqtrade_live_worker_binding = self._accounts.freqtrade_live_worker_binding
        self.validate_freqtrade_worker_binding = self._accounts.validate_freqtrade_worker_binding
        self.start_freqtrade_live_dispatch = self._accounts.start_freqtrade_live_dispatch
        self.freqtrade_dispatch_external_id = self._accounts.freqtrade_dispatch_external_id
        self.runtime_account_bindings = self._accounts.runtime_account_bindings
        self.perptape_runtime_bindings = self._accounts.perptape_runtime_bindings
        self.validate_runtime_account_binding = self._accounts.validate_runtime_account_binding
        self.validate_perptape_runtime_binding = self._accounts.validate_perptape_runtime_binding
        self._lock_runtime_account_binding = self._accounts._lock_runtime_account_binding
        self._lock_perptape_runtime_binding = self._accounts._lock_perptape_runtime_binding
        self._connection_verification_payload = self._accounts._connection_verification_payload
        self.prepare_exchange_account_connection_verification = (
            self._accounts.prepare_exchange_account_connection_verification
        )
        self.record_exchange_account_connection_verification = (
            self._accounts.record_exchange_account_connection_verification
        )
        self._direct_capital_configuration_payload = (
            self._capital_direct._direct_capital_configuration_payload
        )
        self.direct_capital_configuration = self._capital_direct.direct_capital_configuration
        self.set_direct_capital_configuration = (
            self._capital_direct.set_direct_capital_configuration
        )
        self.create_direct_capital_operation = self._capital_direct.create_direct_capital_operation
        self.direct_capital_operation_context = (
            self._capital_direct.direct_capital_operation_context
        )
        self.record_direct_capital_unsigned_preview = (
            self._capital_direct.record_direct_capital_unsigned_preview
        )
        self.record_direct_capital_safe_preview = (
            self._capital_direct.record_direct_capital_safe_preview
        )
        self.record_direct_capital_hyperliquid_preview = (
            self._capital_direct.record_direct_capital_hyperliquid_preview
        )
        self.record_direct_capital_wallet_submission = (
            self._capital_direct.record_direct_capital_wallet_submission
        )
        self.record_direct_capital_treasury_receipt = (
            self._capital_direct.record_direct_capital_treasury_receipt
        )
        self.record_direct_capital_hyperliquid_receipt = (
            self._capital_direct.record_direct_capital_hyperliquid_receipt
        )
        self.record_direct_capital_binance_preview = (
            self._capital_direct.record_direct_capital_binance_preview
        )
        self.record_direct_capital_binance_submission = (
            self._capital_direct.record_direct_capital_binance_submission
        )
        self.record_direct_capital_binance_receipt = (
            self._capital_direct.record_direct_capital_binance_receipt
        )
        self.record_capital_balance = self._capital_reconciliation.record_capital_balance
        self.record_notilt_vault_snapshot = self._capital_notilt.record_notilt_vault_snapshot
        self.record_safe_spending_snapshot = self._capital_notilt.record_safe_spending_snapshot
        self.set_capital_automation_policy = self._capital_automation.set_capital_automation_policy
        self.create_capital_automation_candidate = (
            self._capital_automation.create_capital_automation_candidate
        )
        self.create_transfer_proposal = self._capital_transfer.create_transfer_proposal
        self.submit_transfer_proposal = self._capital_transfer.submit_transfer_proposal
        self.review_transfer_proposal = self._capital_transfer.review_transfer_proposal
        self.issue_transfer_authorization = self._capital_transfer.issue_transfer_authorization
        self._capital_balance = self._capital_reconciliation._capital_balance
        self.record_capital_scope_reconciliation = (
            self._capital_reconciliation.record_capital_scope_reconciliation
        )
        self._assert_capital_scope_flat = self._capital_reconciliation._assert_capital_scope_flat
        self.reserve_capital_transfer = self._capital_transfer.reserve_capital_transfer
        self.capital_transfer_command = self._capital_transfer.capital_transfer_command
        self.notilt_transfer_command = self._capital_notilt.notilt_transfer_command
        self.record_notilt_plan = self._capital_notilt.record_notilt_plan
        self.record_notilt_receipt = self._capital_notilt.record_notilt_receipt
        self.record_capital_submission = self._capital_transfer.record_capital_submission
        self.record_capital_observation = self._capital_transfer.record_capital_observation
        self.reconcile_capital_transfer = self._capital_reconciliation.reconcile_capital_transfer
        self.set_capability_gate = self._capital_automation.set_capability_gate
        self.create_order_intent = self._execution_intent.create_order_intent
        self._consume_add_unit = self._execution_intent._consume_add_unit
        self.mark_intent_unknown = self._execution_intent.mark_intent_unknown
        self.release_unfilled_intent = self._execution_intent.release_unfilled_intent
        self.acquire_sender = self._execution_intent.acquire_sender
        self._validate_sender = self._execution_intent._validate_sender
        self.validate_sender = self._execution_intent.validate_sender
        self._require_exchange_account_live_ready = (
            self._execution_intent._require_exchange_account_live_ready
        )
        self._binance_client_order_id = self._execution_venue._binance_client_order_id
        self._binance_protection_client_order_id = (
            self._execution_venue._binance_protection_client_order_id
        )
        self._hyperliquid_client_order_id = self._execution_venue._hyperliquid_client_order_id
        self._hyperliquid_protection_client_order_id = (
            self._execution_venue._hyperliquid_protection_client_order_id
        )
        self._binance_testnet_command = self._execution_venue._binance_testnet_command
        self.prepare_binance_testnet_send = self._execution_venue.prepare_binance_testnet_send
        self.prepare_binance_testnet_cancel = self._execution_venue.prepare_binance_testnet_cancel
        self.prepare_binance_testnet_recovery = (
            self._execution_venue.prepare_binance_testnet_recovery
        )
        self.prepare_binance_live_send = self._execution_venue.prepare_binance_live_send
        self.prepare_freqtrade_live_order = self._execution_freqtrade.prepare_freqtrade_live_order
        self.prepare_binance_live_cancel = self._execution_venue.prepare_binance_live_cancel
        self.prepare_binance_live_recovery = self._execution_venue.prepare_binance_live_recovery
        self._hyperliquid_testnet_command = self._execution_venue._hyperliquid_testnet_command
        self.prepare_hyperliquid_testnet_send = (
            self._execution_venue.prepare_hyperliquid_testnet_send
        )
        self.prepare_hyperliquid_testnet_cancel = (
            self._execution_venue.prepare_hyperliquid_testnet_cancel
        )
        self.prepare_hyperliquid_testnet_recovery = (
            self._execution_venue.prepare_hyperliquid_testnet_recovery
        )
        self.prepare_hyperliquid_live_send = self._execution_venue.prepare_hyperliquid_live_send
        self.prepare_hyperliquid_live_cancel = self._execution_venue.prepare_hyperliquid_live_cancel
        self.prepare_hyperliquid_live_recovery = (
            self._execution_venue.prepare_hyperliquid_live_recovery
        )
        self._validate_binance_order_result = self._execution_venue._validate_binance_order_result
        self._release_zero_fill_in_session = self._execution_venue._release_zero_fill_in_session
        self.record_binance_testnet_order = self._execution_venue.record_binance_testnet_order
        self.record_hyperliquid_testnet_order = (
            self._execution_venue.record_hyperliquid_testnet_order
        )
        self.record_binance_testnet_unknown = self._execution_venue.record_binance_testnet_unknown
        self.record_hyperliquid_testnet_unknown = (
            self._execution_venue.record_hyperliquid_testnet_unknown
        )
        self.prepare_binance_testnet_protection = (
            self._execution_venue.prepare_binance_testnet_protection
        )
        self.record_binance_testnet_protection = (
            self._execution_venue.record_binance_testnet_protection
        )
        self.prepare_hyperliquid_testnet_protection = (
            self._execution_venue.prepare_hyperliquid_testnet_protection
        )
        self.record_hyperliquid_testnet_protection = (
            self._execution_venue.record_hyperliquid_testnet_protection
        )
        self.record_binance_live_order = self._execution_venue.record_binance_live_order
        self.record_freqtrade_live_order = self._execution_freqtrade.record_freqtrade_live_order
        self.record_freqtrade_live_unknown = self._execution_freqtrade.record_freqtrade_live_unknown
        self.record_freqtrade_live_protection = (
            self._execution_freqtrade.record_freqtrade_live_protection
        )
        self.record_binance_live_unknown = self._execution_venue.record_binance_live_unknown
        self.record_hyperliquid_live_order = self._execution_venue.record_hyperliquid_live_order
        self.record_hyperliquid_live_unknown = self._execution_venue.record_hyperliquid_live_unknown
        self.prepare_binance_live_protection = self._execution_venue.prepare_binance_live_protection
        self.record_binance_live_protection = self._execution_venue.record_binance_live_protection
        self.prepare_hyperliquid_live_protection = (
            self._execution_venue.prepare_hyperliquid_live_protection
        )
        self.record_hyperliquid_live_protection = (
            self._execution_venue.record_hyperliquid_live_protection
        )
        self.prepare_live_protection_cancel = self._execution_venue.prepare_live_protection_cancel
        self.record_live_protection_cancel = self._execution_venue.record_live_protection_cancel
        self.initialize_shadow_scope = self._execution_shadow.initialize_shadow_scope
        self.simulate_shadow_execution = self._execution_shadow.simulate_shadow_execution
        self.record_shadow_order = self._execution_shadow.record_shadow_order
        self.record_fill = self._execution_facts.record_fill
        self.record_position = self._execution_facts.record_position
        self.record_protection = self._execution_facts.record_protection
        self._record_account_equity_observation = (
            self._execution_facts._record_account_equity_observation
        )
        self.record_account_equity = self._execution_facts.record_account_equity
        self.record_funding = self._execution_facts.record_funding
        self.ingest_binance_read_only_snapshot = (
            self._execution_facts.ingest_binance_read_only_snapshot
        )
        self.ingest_hyperliquid_read_only_snapshot = (
            self._execution_facts.ingest_hyperliquid_read_only_snapshot
        )
        self.ingest_binance_read_only_account_snapshot = (
            self._execution_facts.ingest_binance_read_only_account_snapshot
        )
        self.ingest_hyperliquid_read_only_account_snapshot = (
            self._execution_facts.ingest_hyperliquid_read_only_account_snapshot
        )
        self.ingest_okx_read_only_account_snapshot = (
            self._execution_facts.ingest_okx_read_only_account_snapshot
        )
        self.ingest_bybit_read_only_account_snapshot = (
            self._execution_facts.ingest_bybit_read_only_account_snapshot
        )
        self._ingest_read_only_account_snapshot = (
            self._execution_facts._ingest_read_only_account_snapshot
        )
        self._cover_absent_positions = self._execution_facts._cover_absent_positions
        self._intent_id_from_client_order = self._execution_facts._intent_id_from_client_order
        self._ingest_read_only_snapshot = self._execution_facts._ingest_read_only_snapshot
        self.update_campaign_target = self._execution_campaign.update_campaign_target
        self.create_reduction_intent = self._execution_campaign.create_reduction_intent
        self.recover_freqtrade_emergency_exit = (
            self._execution_freqtrade.recover_freqtrade_emergency_exit
        )
        self.create_automatic_exit_intent = self._execution_campaign.create_automatic_exit_intent
        self.close_campaign = self._execution_campaign.close_campaign
        self.refresh_campaign_pnl = self._execution_campaign.refresh_campaign_pnl
        self._apply_shadow_pnl_delta = self._execution_campaign._apply_shadow_pnl_delta
        self._update_campaign_pnl = self._execution_campaign._update_campaign_pnl
        self.proposal_default_config = self._proposals.proposal_default_config
        self._proposal_default_payload = self._proposals._proposal_default_payload
        self.proposal_automation_config = self._proposals.proposal_automation_config
        self.set_proposal_default_config = self._proposals.set_proposal_default_config
        self.create_proposal = self._proposals.create_proposal
        self.expire_duplicate_active_manual_proposals = (
            self._proposals.expire_duplicate_active_manual_proposals
        )
        self.expire_duplicate_active_system_proposals = (
            self._proposals.expire_duplicate_active_system_proposals
        )
        self.submit_proposal = self._proposals.submit_proposal
        self.review_proposal = self._proposals.review_proposal
        self.set_risk_policy = self._risk_policy.set_risk_policy
        self.configure_risk_policy = self._risk_policy.configure_risk_policy
        self._lock_risk_capacity = self.runtime.transactions._lock_risk_capacity
        self._occupied_risk = self._risk_policy._occupied_risk
        self._active_risk_policy = self._risk_policy._active_risk_policy
        self._risk_policy_input = self._risk_policy._risk_policy_input
        self._consecutive_loss_snapshot = self._risk_policy._consecutive_loss_snapshot
        self._loss_limit_context = self._risk_policy._loss_limit_context
        self._managed_capital_context = self._risk_policy._managed_capital_context
        self._server_risk_context = self._risk_policy._server_risk_context
        self.decide_risk = self._risk_policy.decide_risk
        self.issue_authorization = self._risk_authorization.issue_authorization
        self._intent_creation = self._risk_authorization._intent_creation
        self._proposal_limit_price = self._risk_authorization._proposal_limit_price
        self._proposal_detail_decimal = self._risk_authorization._proposal_detail_decimal
        self._validate_add_candidate = self._risk_authorization._validate_add_candidate
        self.disable_campaign_auto_add = self._risk_authorization.disable_campaign_auto_add
        self.disable_global_auto_add = self._risk_authorization.disable_global_auto_add
        self.pause_new_risk = self._risk_authorization.pause_new_risk
        self._canonical_restore_scopes = self._risk_recovery._canonical_restore_scopes
        self._risk_restore_blockers = self._risk_recovery._risk_restore_blockers
        self._risk_restore_condition_details = self._risk_recovery._risk_restore_condition_details
        self._risk_restore_request_drifted = self._risk_recovery._risk_restore_request_drifted
        self.risk_control_status = self._risk_recovery.risk_control_status
        self.risk_control_change_version = self._risk_recovery.risk_control_change_version
        self.create_risk_control_change_request = (
            self._risk_recovery.create_risk_control_change_request
        )
        self.review_risk_control_change_request = (
            self._risk_recovery.review_risk_control_change_request
        )
        self.execute_risk_control_change_request = (
            self._risk_recovery.execute_risk_control_change_request
        )
        self.direct_restore_risk_controls = self._risk_recovery.direct_restore_risk_controls
        self.record_scope_reconciliation = self._risk_reconciliation.record_scope_reconciliation
        self.require_manual_reconciliation = self._risk_reconciliation.require_manual_reconciliation
        self.resolve_reconciliation = self._risk_reconciliation.resolve_reconciliation
        self.reconciliation_status = self._risk_reconciliation.reconciliation_status
        self._fact_is_stale = self._risk_reconciliation._fact_is_stale
        self.reconcile_scope = self._risk_reconciliation.reconcile_scope
        self.reconcile_campaign = self._risk_reconciliation.reconcile_campaign
        self._signal_source_payload = self._signals._signal_source_payload
        self.signal_source_status = self._signals.signal_source_status
        self._ensure_signal_service_principal = self._signals._ensure_signal_service_principal
        self.configure_signal_source = self._signals.configure_signal_source
        self.perptape_source_runtime = self._signals.perptape_source_runtime
        self.signal_service_principal = self._signals.signal_service_principal
        self.ingest_webhook_signal = self._signals.ingest_webhook_signal
        self._signal_event_payload = self._signals._signal_event_payload
        self.signal_event = self._signals.signal_event
        self.list_signal_events = self._signals.list_signal_events
        self.record_runtime_source_health = self._signals.record_runtime_source_health
        self.record_perptape_feed = self._signals.record_perptape_feed
        self.register_instrument = self._signals.register_instrument
        self.upsert_venue_instrument = self._signals.upsert_venue_instrument
        self.synchronize_active_venue_instruments = (
            self._signals.synchronize_active_venue_instruments
        )
        self._audit = self.runtime.transactions._audit
        self._idempotency = self.runtime.transactions._idempotency
        self._save_receipt = self.runtime.transactions._save_receipt
        self._active_scope = self.runtime.transactions._active_scope
        self._require_workspace_admin = self.runtime.transactions._require_workspace_admin
        self._require_role = self.runtime.transactions._require_role
        self._require_team_environment = self.runtime.transactions._require_team_environment
        self.can_user = self.runtime.transactions.can_user
        self._require_agent_scope = self._workspace._require_agent_scope
        self._agent_token_digest = self._workspace._agent_token_digest
        self.authenticate_agent_token = self._workspace.authenticate_agent_token
        self._require_action_assignment = self.runtime.transactions._require_action_assignment
        self.configure_notification_route = self.runtime.transactions.configure_notification_route
        self._enqueue_notification_event = self.runtime.transactions._enqueue_notification_event
        self._enqueue_proposal_review_notification = (
            self.runtime.transactions._enqueue_proposal_review_notification
        )
        self._enqueue_campaign_status_notification = (
            self.runtime.transactions._enqueue_campaign_status_notification
        )
        self.enqueue_notification_event = self.runtime.transactions.enqueue_notification_event
        self.enqueue_test_notification = self.runtime.transactions.enqueue_test_notification
        self.enqueue_capital_status_notification = (
            self.runtime.transactions.enqueue_capital_status_notification
        )
        self.bootstrap_admin = self._workspace.bootstrap_admin
        self._scope_slug = self._workspace._scope_slug
        self.create_workspace = self._workspace.create_workspace
        self.create_team = self._workspace.create_team
        self._shadow_activation_blockers = self._workspace._shadow_activation_blockers
        self.shadow_activation_status = self._workspace.shadow_activation_status
        self.activate_team_shadow_mode = self._workspace.activate_team_shadow_mode
        self.select_scope = self._workspace.select_scope
        self.create_user = self._workspace.create_user
        self.create_managed_user = self._workspace.create_managed_user
        self.add_team_member = self._workspace.add_team_member
        self.update_managed_user_access = self._workspace.update_managed_user_access
        self.change_own_password = self._workspace.change_own_password
        self.ensure_local_human_password = self._workspace.ensure_local_human_password
        self.bind_telegram_private_chat = self._workspace.bind_telegram_private_chat
        self.create_agent = self._workspace.create_agent
        self.update_agent_access = self._workspace.update_agent_access
        self.rotate_agent_token = self._workspace.rotate_agent_token
        self.create_service_principal = self._workspace.create_service_principal
        self.assign_role = self._workspace.assign_role

    @property
    def database(self) -> Database:
        return self.runtime.database

    @property
    def credential_cipher(self) -> CredentialCipher:
        return self.runtime.credential_cipher

    @property
    def authoritative_live_accounts(self) -> dict[str, str]:
        return self.runtime.authoritative_live_accounts
