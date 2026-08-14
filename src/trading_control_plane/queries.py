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


class TradingQueries(
    WorkspaceQueries,
    AccountQueries,
    AnalyticsQueries,
    SignalQueries,
    ProposalQueries,
    RiskQueries,
    ExecutionQueries,
    CapitalQueries,
):
    """Single projection surface composed from domain-focused query implementations."""

    managed_agents = WorkspaceQueries.api_clients

    def __init__(self, database: Database) -> None:
        self.runtime = QueryRuntime(database=database, service=TradingService(database))
