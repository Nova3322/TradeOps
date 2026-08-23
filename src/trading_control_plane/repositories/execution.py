from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_control_plane.models import Position


class PositionScope(Protocol):
    team_id: UUID
    account_id: str
    venue: str
    environment: str
    instrument_id: UUID


def find_position_for_scope(
    session: Session,
    scope: PositionScope,
    *,
    for_update: bool = False,
) -> Position | None:
    """Load one position through the complete environment-isolated business key."""
    statement = select(Position).where(
        Position.team_id == scope.team_id,
        Position.account_id == scope.account_id,
        Position.venue == scope.venue,
        Position.environment == scope.environment,
        Position.instrument_id == scope.instrument_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)
