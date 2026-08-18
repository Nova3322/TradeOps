"""persist shared Binance request state and receipt leases

Revision ID: 20260818_0044
Revises: 20260813_0043
Create Date: 2026-08-18 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260818_0044"
down_revision: str | None = "20260813_0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "binance_api_state",
        sa.Column("scope_key", sa.String(length=64), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=True),
        sa.Column("diagnostic", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "rate_limit_headers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("headers_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("clock_offset_ms", sa.Integer(), nullable=True),
        sa.Column("clock_synchronized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("probe_owner", sa.String(length=120), nullable=True),
        sa.Column("probe_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("scope_key"),
    )
    op.execute(
        "INSERT INTO binance_api_state "
        "(scope_key, rate_limit_headers, updated_at) "
        "VALUES ('BINANCE_DEPLOYMENT_IP', '{}'::jsonb, CURRENT_TIMESTAMP)"
    )
    op.add_column(
        "direct_capital_operations",
        sa.Column("receipt_poll_stage", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "direct_capital_operations",
        sa.Column("receipt_poll_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "direct_capital_operations",
        sa.Column("receipt_poll_token", sa.String(length=160), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("direct_capital_operations", "receipt_poll_token")
    op.drop_column("direct_capital_operations", "receipt_poll_started_at")
    op.drop_column("direct_capital_operations", "receipt_poll_stage")
    op.drop_table("binance_api_state")
