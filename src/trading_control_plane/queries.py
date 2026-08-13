from __future__ import annotations

from trading_control_plane.database import Database
from trading_control_plane.query_component import QueryRuntime
from trading_control_plane.query_core import _performance_metrics as _performance_metrics
from trading_control_plane.query_domains.accounts import AccountQueries
from trading_control_plane.query_domains.analytics import AnalyticsQueries
from trading_control_plane.query_domains.capital import CapitalQueries
from trading_control_plane.query_domains.execution import ExecutionQueries
from trading_control_plane.query_domains.proposals import ProposalQueries
from trading_control_plane.query_domains.risk import RiskQueries
from trading_control_plane.query_domains.signals import SignalQueries
from trading_control_plane.query_domains.workspace import WorkspaceQueries
from trading_control_plane.service import TradingService


class TradingQueries:
    """Compatibility facade over typed domain projection components."""

    def __init__(self, database: Database) -> None:
        self.runtime = QueryRuntime(database=database, service=TradingService(database))
        self._workspace = WorkspaceQueries(self.runtime, self)
        self._accounts = AccountQueries(self.runtime, self)
        self._analytics = AnalyticsQueries(self.runtime, self)
        self._signals = SignalQueries(self.runtime, self)
        self._proposals = ProposalQueries(self.runtime, self)
        self._risk = RiskQueries(self.runtime, self)
        self._execution = ExecutionQueries(self.runtime, self)
        self._capital = CapitalQueries(self.runtime, self)

        self.exchange_accounts = self._accounts.exchange_accounts
        self._exchange_account_projection = self._accounts._exchange_account_projection
        self.venue_facts = self._accounts.venue_facts
        self.treasury_reviewers_for_transfer = self._capital.treasury_reviewers_for_transfer
        self.treasury_users = self._capital.treasury_users
        self.transfer_proposal_version = self._capital.transfer_proposal_version
        self._transfer_proposal_summary = self._capital._transfer_proposal_summary
        self.transfer_proposal_detail = self._capital.transfer_proposal_detail
        self._capital_transfer_summary = self._capital._capital_transfer_summary
        self.capital_transfer_detail = self._capital.capital_transfer_detail
        self.capital_display = self._capital.capital_display
        self.capital_center = self._capital.capital_center
        self.actual_results = self._execution.actual_results
        self.analytics_report_options = self._analytics.analytics_report_options
        self.analytics_dataset = self._analytics.analytics_dataset
        self.analytics_report = self._analytics.analytics_report
        self.analytics_report_artifact = self._analytics.analytics_report_artifact
        self.audit_timeline = self._execution.audit_timeline
        self.runtime_snapshot = self._execution.runtime_snapshot
        self.runtime_source_health = self._execution.runtime_source_health
        self.list_campaigns = self._execution.list_campaigns
        self.campaign_detail = self._execution.campaign_detail
        self.campaign_id_for_intent = self._execution.campaign_id_for_intent
        self._order_summary = self._execution._order_summary
        self._campaign_summary = self._execution._campaign_summary
        self._proposal_summary = self._execution._proposal_summary
        self.list_proposals = self._proposals.list_proposals
        self.active_perptape_system_proposals = self._proposals.active_perptape_system_proposals
        self.proposal_detail = self._proposals.proposal_detail
        self.proposal_version = self._proposals.proposal_version
        self.reviewers_for_proposal = self._proposals.reviewers_for_proposal
        self.list_exceptions = self._risk.list_exceptions
        self._exception = self._risk._exception
        self.list_instruments = self._signals.list_instruments
        self.instrument_id_by_venue_symbol = self._signals.instrument_id_by_venue_symbol
        self.active_instrument_keys = self._signals.active_instrument_keys
        self.compatible_legacy_system_candidate_id = (
            self._signals.compatible_legacy_system_candidate_id
        )
        self.perptape_feed = self._signals.perptape_feed
        self._active_scope_ids = self._workspace._active_scope_ids
        self.user_by_username = self._workspace.user_by_username
        self.password_credential = self._workspace.password_credential
        self.service_principal_by_username = self._workspace.service_principal_by_username
        self.user_context = self._workspace.user_context
        self.managed_users = self._workspace.managed_users
        self.api_clients = self._workspace.api_clients
        self.api_client_scopes = self._workspace.api_client_scopes
        self.managed_agents = self._workspace.api_clients
        self.telegram_chat_id = self._workspace.telegram_chat_id
        self.telegram_user_id = self._workspace.telegram_user_id
        self.notification_center = self._workspace.notification_center

    @property
    def database(self) -> Database:
        return self.runtime.database

    @property
    def service(self) -> TradingService:
        return self.runtime.service
