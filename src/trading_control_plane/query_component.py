from __future__ import annotations

from dataclasses import dataclass

from trading_control_plane.database import Database
from trading_control_plane.service import TradingService


@dataclass(frozen=True, slots=True)
class QueryRuntime:
    database: Database
    service: TradingService


class QueryComponent:
    """Shared runtime access for the projection methods composed by TradingQueries."""

    runtime: QueryRuntime

    @property
    def database(self) -> Database:
        return self.runtime.database

    @property
    def service(self) -> TradingService:
        return self.runtime.service
