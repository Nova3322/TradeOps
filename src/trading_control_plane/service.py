from __future__ import annotations

from trading_control_plane.credentials import CredentialCipher
from trading_control_plane.database import Database
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
from trading_control_plane.service_domains.accounts import AccountServiceMixin
from trading_control_plane.service_domains.capital import CapitalServiceMixin
from trading_control_plane.service_domains.execution import ExecutionServiceMixin
from trading_control_plane.service_domains.proposals import ProposalServiceMixin
from trading_control_plane.service_domains.risk import RiskServiceMixin
from trading_control_plane.service_domains.signals import SignalServiceMixin
from trading_control_plane.service_domains.workspace import WorkspaceServiceMixin


class TradingService(
    WorkspaceServiceMixin,
    AccountServiceMixin,
    SignalServiceMixin,
    ProposalServiceMixin,
    RiskServiceMixin,
    ExecutionServiceMixin,
    CapitalServiceMixin,
):
    """Transactional facade composed from domain-focused service modules."""

    def __init__(
        self,
        database: Database,
        *,
        authoritative_live_accounts: dict[str, str] | None = None,
        credential_encryption_key: str | None = None,
    ) -> None:
        self.database = database
        self.credential_cipher = CredentialCipher(credential_encryption_key)
        self.authoritative_live_accounts = {
            venue.upper(): account_id
            for venue, account_id in (authoritative_live_accounts or {}).items()
        }
