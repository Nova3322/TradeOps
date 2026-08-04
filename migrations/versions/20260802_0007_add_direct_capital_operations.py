"""add direct capital operations

Revision ID: 20260802_0007
Revises: 20260802_0006
Create Date: 2026-08-02 23:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260802_0007"
down_revision: str | None = "20260802_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "direct_capital_operations",
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("receipt_status", sa.String(length=32), nullable=False),
        sa.Column("account_id", sa.String(length=120), nullable=True),
        sa.Column("venue", sa.String(length=64), nullable=False),
        sa.Column("vault_id", sa.String(length=160), nullable=True),
        sa.Column("asset", sa.String(length=32), nullable=False),
        sa.Column("network", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("max_fee", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("min_received", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("source_reference", sa.String(length=255), nullable=True),
        sa.Column("destination_reference", sa.String(length=255), nullable=True),
        sa.Column("stages", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("blockers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("execute_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("final_confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "path IN ('VAULT_TO_BINANCE','VAULT_TO_HYPERLIQUID',"
            "'BINANCE_TO_VAULT','HYPERLIQUID_TO_VAULT')",
            name="ck_direct_capital_operations_path",
        ),
        sa.CheckConstraint("venue IN ('BINANCE','HYPERLIQUID')", name="ck_direct_capital_venue"),
        sa.CheckConstraint(
            "status IN ('BLOCKED','UNSIGNED_PLAN_READY','AWAITING_RECEIPT','SETTLED','UNKNOWN')",
            name="ck_direct_capital_status",
        ),
        sa.CheckConstraint(
            "receipt_status IN ('NOT_SUBMITTED','PENDING','CONFIRMED','UNKNOWN')",
            name="ck_direct_capital_receipt_status",
        ),
        sa.CheckConstraint("amount > 0", name="ck_direct_capital_amount_positive"),
        sa.CheckConstraint(
            "max_fee IS NULL OR max_fee >= 0", name="ck_direct_capital_fee_nonnegative"
        ),
        sa.CheckConstraint(
            "min_received IS NULL OR (min_received > 0 AND min_received <= amount)",
            name="ck_direct_capital_min_received",
        ),
        sa.CheckConstraint("jsonb_typeof(stages) = 'array'", name="ck_direct_capital_stages_array"),
        sa.CheckConstraint(
            "jsonb_typeof(blockers) = 'array'", name="ck_direct_capital_blockers_array"
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("operation_id"),
    )
    op.create_index(
        "ix_direct_capital_operations_updated",
        "direct_capital_operations",
        ["updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_direct_capital_operations_updated",
        table_name="direct_capital_operations",
    )
    op.drop_table("direct_capital_operations")
