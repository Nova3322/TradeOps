from __future__ import annotations

from datetime import UTC, datetime

from trading_control_plane.binance_errors import BinanceRequestState
from trading_control_plane.database import Database
from trading_control_plane.models import BinanceApiState

BINANCE_DEPLOYMENT_SCOPE = "BINANCE_DEPLOYMENT_IP"


class DatabaseBinanceRequestState(BinanceRequestState):
    """Persist only IP-independent state; Binance request limits stay ephemeral."""

    def __init__(self, database: Database) -> None:
        super().__init__()
        self.database = database

    @staticmethod
    def _new(now: datetime) -> BinanceApiState:
        return BinanceApiState(
            scope_key=BINANCE_DEPLOYMENT_SCOPE,
            host=None,
            clock_offset_ms=None,
            clock_synchronized_at=None,
            updated_at=now,
        )

    def current_time_offset(self) -> tuple[int, datetime] | None:
        with self.database.session_factory() as session:
            row = session.get(BinanceApiState, BINANCE_DEPLOYMENT_SCOPE)
            if row is None or row.clock_offset_ms is None or row.clock_synchronized_at is None:
                return None
            return row.clock_offset_ms, row.clock_synchronized_at

    def record_time_offset(
        self, offset_ms: int, *, synchronized_at: datetime | None = None
    ) -> None:
        now = (synchronized_at or datetime.now(UTC)).astimezone(UTC)
        with self.database.session_factory.begin() as session:
            row = session.get(BinanceApiState, BINANCE_DEPLOYMENT_SCOPE, with_for_update=True)
            if row is None:
                row = self._new(now)
                session.add(row)
            row.clock_offset_ms = int(offset_ms)
            row.clock_synchronized_at = now
            row.updated_at = now
