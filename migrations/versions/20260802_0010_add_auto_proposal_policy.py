"""add configurable automatic proposal policy

Revision ID: 20260802_0010
Revises: 20260802_0009
Create Date: 2026-08-02 23:55:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260802_0010"
down_revision: str | None = "20260802_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "proposal_default_configs",
        sa.Column(
            "auto_proposal_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "proposal_default_configs",
        sa.Column(
            "auto_proposal_min_timeframes",
            sa.Integer(),
            nullable=False,
            server_default="3",
        ),
    )
    op.create_check_constraint(
        "ck_proposal_defaults_auto_timeframes",
        "proposal_default_configs",
        "auto_proposal_min_timeframes IN (3, 4)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_proposal_defaults_auto_timeframes",
        "proposal_default_configs",
        type_="check",
    )
    op.drop_column("proposal_default_configs", "auto_proposal_min_timeframes")
    op.drop_column("proposal_default_configs", "auto_proposal_enabled")
