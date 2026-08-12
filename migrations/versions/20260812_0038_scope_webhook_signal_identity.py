"""Scope Webhook signal identity to its exact signal source.

Revision ID: 20260812_0038
Revises: 20260812_0036
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260812_0038"
down_revision: str | None = "20260812_0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_signal_events_team_idempotency", "signal_events", type_="unique")
    op.drop_constraint("uq_signal_events_team_provider_external", "signal_events", type_="unique")
    op.create_unique_constraint(
        "uq_signal_events_source_idempotency",
        "signal_events",
        ["signal_source_id", "idempotency_key"],
    )
    op.create_unique_constraint(
        "uq_signal_events_source_provider_external",
        "signal_events",
        ["signal_source_id", "provider", "external_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    duplicate_external = connection.execute(
        sa.text(
            "SELECT 1 FROM signal_events GROUP BY team_id, provider, external_id "
            "HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    duplicate_idempotency = connection.execute(
        sa.text(
            "SELECT 1 FROM signal_events GROUP BY team_id, idempotency_key "
            "HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate_external is not None or duplicate_idempotency is not None:
        raise RuntimeError(
            "downgrade blocked: different Webhook sources retained overlapping identities"
        )
    op.drop_constraint("uq_signal_events_source_provider_external", "signal_events", type_="unique")
    op.drop_constraint("uq_signal_events_source_idempotency", "signal_events", type_="unique")
    op.create_unique_constraint(
        "uq_signal_events_team_provider_external",
        "signal_events",
        ["team_id", "provider", "external_id"],
    )
    op.create_unique_constraint(
        "uq_signal_events_team_idempotency",
        "signal_events",
        ["team_id", "idempotency_key"],
    )
