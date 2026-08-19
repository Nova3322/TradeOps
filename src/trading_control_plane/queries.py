from __future__ import annotations

from datetime import datetime
from uuid import UUID

from trading_control_plane.credentials import CredentialCipher
from trading_control_plane.database import Database
from trading_control_plane.query_component import QueryRuntime
from trading_control_plane.query_domains.accounts import AccountQueries
from trading_control_plane.query_domains.analytics import AnalyticsQueries
from trading_control_plane.query_domains.capital import CapitalQueries
from trading_control_plane.query_domains.execution import ExecutionQueries, performance_metrics
from trading_control_plane.query_domains.proposals import ProposalQueries
from trading_control_plane.query_domains.risk import list_exceptions
from trading_control_plane.query_domains.signals import SignalQueries
from trading_control_plane.query_domains.workspace import WorkspaceQueries
from trading_control_plane.service_transactions import TransactionService

_performance_metrics = performance_metrics


class TradingQueries(
    WorkspaceQueries,
    AccountQueries,
    AnalyticsQueries,
    SignalQueries,
    ProposalQueries,
    ExecutionQueries,
    CapitalQueries,
):
    """Single projection surface composed from domain-focused query implementations."""

    def list_exceptions(self, user_id: UUID, *, now: datetime) -> list[dict[str, object]]:
        return list_exceptions(self, self, user_id, now=now)

    def __init__(self, database: Database) -> None:
        self.runtime = QueryRuntime(
            database=database,
            access_policy=TransactionService(database, CredentialCipher(None)),
        )
