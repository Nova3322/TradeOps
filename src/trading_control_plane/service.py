from __future__ import annotations

from trading_control_plane.credentials import CredentialCipher
from trading_control_plane.database import Database
from trading_control_plane.service_component import ServiceRuntime
from trading_control_plane.service_core import ROLE_ACTIONS as ROLE_ACTIONS
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
from trading_control_plane.service_domains.analytics_reports import AnalyticsReportService
from trading_control_plane.service_domains.api_clients import ApiClientService
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
from trading_control_plane.service_domains.proposals import ProposalService
from trading_control_plane.service_domains.risk_authorization import AuthorizationRiskService
from trading_control_plane.service_domains.risk_policy import PolicyRiskService
from trading_control_plane.service_domains.risk_reconciliation import ReconciliationRiskService
from trading_control_plane.service_domains.risk_recovery import RecoveryRiskService
from trading_control_plane.service_domains.signals import SignalService
from trading_control_plane.service_domains.trading_mode import TradingModeService
from trading_control_plane.service_domains.workspace import WorkspaceService
from trading_control_plane.service_transactions import TransactionService


class TradingService(
    WorkspaceService,
    AccountService,
    AnalyticsReportService,
    ApiClientService,
    SignalService,
    ProposalService,
    PolicyRiskService,
    AuthorizationRiskService,
    RecoveryRiskService,
    ReconciliationRiskService,
    IntentExecutionService,
    FactIngestionExecutionService,
    CampaignExecutionService,
    TradingModeService,
    FreqtradeRecoveryExecutionService,
    DirectOperationCapitalService,
    TransferCapitalService,
    AutomationCapitalService,
    NoTiltCapitalService,
    ReconciliationCapitalService,
    TransactionService,
):
    """Single business service composed from lifecycle-focused domain implementations."""

    persist_analytics_report = AnalyticsReportService.persist_report

    def __init__(
        self,
        database: Database,
        *,
        credential_encryption_key: str | None = None,
    ) -> None:
        credential_cipher = CredentialCipher(credential_encryption_key)
        self.runtime = ServiceRuntime(
            database=database,
            credential_cipher=credential_cipher,
        )
