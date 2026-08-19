"""persist Binance caches, schedules and capital write fences

Revision ID: 20260818_0045
Revises: 20260818_0044
Create Date: 2026-08-18 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260818_0045"
down_revision: str | None = "20260818_0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "binance_api_state",
        sa.Column("exchange_info", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "binance_api_state",
        sa.Column("exchange_info_cached_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "binance_api_state",
        sa.Column(
            "history_schedules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "direct_capital_operations",
        sa.Column("receipt_next_due_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "direct_capital_operations",
        sa.Column("receipt_attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "direct_capital_operations",
        sa.Column("receipt_last_error_code", sa.String(length=120), nullable=True),
    )
    op.create_table(
        "binance_capital_outbox",
        sa.Column("outbox_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("external_reference", sa.String(length=255), nullable=True),
        sa.Column("last_error_code", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_binance_capital_outbox_attempts"
        ),
        sa.CheckConstraint(
            "status IN ('NEVER_ATTEMPTED','ATTEMPTING','CONFIRMED','UNKNOWN')",
            name="ck_binance_capital_outbox_status",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["direct_capital_operations.operation_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["team_id"], ["teams.team_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("outbox_id"),
        sa.UniqueConstraint(
            "operation_id", "stage", name="uq_binance_capital_outbox_operation_stage"
        ),
    )
    op.create_index(
        "ix_binance_capital_outbox_status",
        "binance_capital_outbox",
        ["status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_binance_capital_outbox_status", table_name="binance_capital_outbox")
    op.drop_table("binance_capital_outbox")
    op.drop_column("direct_capital_operations", "receipt_last_error_code")
    op.drop_column("direct_capital_operations", "receipt_attempt_count")
    op.drop_column("direct_capital_operations", "receipt_next_due_at")
    op.drop_column("binance_api_state", "history_schedules")
    op.drop_column("binance_api_state", "exchange_info_cached_at")
    op.drop_column("binance_api_state", "exchange_info")
