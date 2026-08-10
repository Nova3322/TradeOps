"""Add explicit team execution mode for setup, shadow, and live isolation.

Revision ID: 20260810_0024
Revises: 20260810_0023
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260810_0024"
down_revision: str | None = "20260810_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "teams",
        sa.Column(
            "execution_mode",
            sa.String(length=16),
            nullable=False,
            server_default="SETUP",
        ),
    )
    op.execute(
        "UPDATE teams SET execution_mode = CASE WHEN trading_enabled THEN 'LIVE' ELSE 'SETUP' END"
    )
    op.create_check_constraint(
        "ck_teams_execution_mode",
        "teams",
        "execution_mode IN ('SETUP','SHADOW','LIVE')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_teams_execution_mode", "teams", type_="check")
    op.drop_column("teams", "execution_mode")
