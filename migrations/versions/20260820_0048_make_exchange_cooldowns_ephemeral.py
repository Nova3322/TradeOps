"""keep exchange IP cooldown state out of PostgreSQL

Revision ID: 20260820_0048
Revises: 20260820_0047
Create Date: 2026-08-20 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260820_0048"
down_revision: str | None = "20260820_0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE exchange_accounts "
        "SET credential_metadata = credential_metadata - 'last_connection_error' "
        "WHERE connection_error_code LIKE '%RATE_LIMITED%' "
        "AND credential_metadata ? 'last_connection_error'"
    )
    op.execute(
        "UPDATE runtime_source_health SET retry_at = NULL "
        "WHERE source_name IN ('BINANCE','HYPERLIQUID','OKX','BYBIT') "
        "AND error_code LIKE '%RATE_LIMITED%'"
    )
    op.create_check_constraint(
        "ck_runtime_source_health_exchange_cooldown_ephemeral",
        "runtime_source_health",
        "source_name NOT IN ('BINANCE','HYPERLIQUID','OKX','BYBIT') "
        "OR error_code IS NULL OR error_code NOT LIKE '%RATE_LIMITED%' "
        "OR retry_at IS NULL",
    )
    op.drop_column("binance_api_state", "probe_started_at")
    op.drop_column("binance_api_state", "probe_owner")
    op.drop_column("binance_api_state", "headers_observed_at")
    op.drop_column("binance_api_state", "rate_limit_headers")
    op.drop_column("binance_api_state", "next_retry_at")
    op.drop_column("binance_api_state", "diagnostic")


def downgrade() -> None:
    op.add_column(
        "binance_api_state",
        sa.Column(
            "diagnostic",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "binance_api_state",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "binance_api_state",
        sa.Column(
            "rate_limit_headers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.alter_column("binance_api_state", "rate_limit_headers", server_default=None)
    op.add_column(
        "binance_api_state",
        sa.Column("headers_observed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "binance_api_state",
        sa.Column("probe_owner", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "binance_api_state",
        sa.Column("probe_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint(
        "ck_runtime_source_health_exchange_cooldown_ephemeral",
        "runtime_source_health",
        type_="check",
    )
