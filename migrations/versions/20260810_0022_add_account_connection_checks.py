"""Record exact exchange-account connection check time.

Revision ID: 20260810_0022
Revises: 20260810_0021
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260810_0022"
down_revision: str | None = "20260810_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "exchange_accounts",
        sa.Column("last_connection_check_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("exchange_accounts", "last_connection_check_at")
