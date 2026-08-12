from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from trading_control_plane.database import Database

# ruff: noqa: F403, F405
from trading_control_plane.query_core import *
from trading_control_plane.service import TradingService


class QueryFacade(Protocol):
    def list_campaigns(self, user_id: UUID) -> list[dict[str, Any]]: ...

    def campaign_detail(self, user_id: UUID, campaign_id: UUID) -> dict[str, Any]: ...

    def _proposal_summary(
        self, proposal: Proposal, instrument: Instrument | None = None
    ) -> dict[str, Any]: ...

    def _active_scope_ids(self, user_id: UUID) -> tuple[UUID, UUID]: ...


@dataclass(frozen=True, slots=True)
class QueryRuntime:
    database: Database
    service: TradingService


class QueryComponent:
    def __init__(self, runtime: QueryRuntime, facade: QueryFacade) -> None:
        self.runtime = runtime
        self.facade = facade

    @property
    def database(self) -> Database:
        return self.runtime.database

    @property
    def service(self) -> TradingService:
        return self.runtime.service
