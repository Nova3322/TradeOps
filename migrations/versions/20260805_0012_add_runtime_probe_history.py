"""preserve runtime probe success and retry evidence

Revision ID: 20260805_0012
Revises: 20260802_0011
Create Date: 2026-08-05 04:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260805_0012"
down_revision: str | None = "20260802_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runtime_source_health",
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "runtime_source_health",
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "runtime_source_health",
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_runtime_source_health_failures_nonnegative",
        "runtime_source_health",
        "consecutive_failures >= 0",
    )
    op.execute(
        "UPDATE runtime_source_health "
        "SET last_success_at = checked_at "
        "WHERE status = 'SUCCESS'"
    )
    op.alter_column(
        "runtime_source_health",
        "consecutive_failures",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_runtime_source_health_failures_nonnegative",
        "runtime_source_health",
        type_="check",
    )
    op.drop_column("runtime_source_health", "consecutive_failures")
    op.drop_column("runtime_source_health", "retry_at")
    op.drop_column("runtime_source_health", "last_success_at")
