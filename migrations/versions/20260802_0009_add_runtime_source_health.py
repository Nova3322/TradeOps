"""add runtime source health

Revision ID: 20260802_0009
Revises: 20260802_0008
Create Date: 2026-08-02 23:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260802_0009"
down_revision: str | None = "20260802_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runtime_source_health",
        sa.Column("source_name", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("items_observed", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('SUCCESS','FAILED','SKIPPED')",
            name="ck_runtime_source_health_status",
        ),
        sa.CheckConstraint(
            "items_observed >= 0",
            name="ck_runtime_source_health_items_nonnegative",
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("source_name"),
    )


def downgrade() -> None:
    op.drop_table("runtime_source_health")
