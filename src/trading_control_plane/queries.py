from __future__ import annotations

from trading_control_plane.database import Database
from trading_control_plane.query_core import _performance_metrics as _performance_metrics
from trading_control_plane.query_domains.accounts import AccountQueryMixin
from trading_control_plane.query_domains.capital import CapitalQueryMixin
from trading_control_plane.query_domains.execution import ExecutionQueryMixin
from trading_control_plane.query_domains.proposals import ProposalQueryMixin
from trading_control_plane.query_domains.risk import RiskQueryMixin
from trading_control_plane.query_domains.signals import SignalQueryMixin
from trading_control_plane.query_domains.workspace import WorkspaceQueryMixin
from trading_control_plane.service import TradingService


class TradingQueries(
    WorkspaceQueryMixin,
    AccountQueryMixin,
    SignalQueryMixin,
    ProposalQueryMixin,
    RiskQueryMixin,
    ExecutionQueryMixin,
    CapitalQueryMixin,
):
    """Read-model facade composed from domain-focused projection modules."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.service = TradingService(database)
