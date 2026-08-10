"""Bind Freqtrade workers to exact exchange accounts.

Revision ID: 20260811_0029
Revises: 20260811_0028
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260811_0029"
down_revision: str | None = "20260811_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "exchange_accounts",
        sa.Column("freqtrade_worker_name", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "exchange_accounts",
        sa.Column("freqtrade_worker_url", sa.String(length=2_048), nullable=True),
    )
    op.add_column(
        "exchange_accounts",
        sa.Column(
            "freqtrade_worker_mode",
            sa.String(length=16),
            nullable=False,
            server_default="UNCONFIGURED",
        ),
    )
    op.add_column(
        "exchange_accounts",
        sa.Column(
            "freqtrade_worker_status",
            sa.String(length=32),
            nullable=False,
            server_default="UNCONFIGURED",
        ),
    )
    op.add_column(
        "exchange_accounts",
        sa.Column("freqtrade_auth_ciphertext", sa.Text(), nullable=True),
    )
    op.add_column(
        "exchange_accounts",
        sa.Column(
            "freqtrade_auth_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "exchange_accounts",
        sa.Column(
            "freqtrade_auth_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "exchange_accounts",
        sa.Column(
            "freqtrade_hip3_dexes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "exchange_accounts",
        sa.Column("freqtrade_error_code", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "exchange_accounts",
        sa.Column("freqtrade_last_check_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "exchange_accounts",
        sa.Column("freqtrade_last_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_exchange_accounts_freqtrade_worker_mode",
        "exchange_accounts",
        "freqtrade_worker_mode IN ('UNCONFIGURED','DRY_RUN','LIVE')",
    )
    op.create_check_constraint(
        "ck_exchange_accounts_freqtrade_worker_status",
        "exchange_accounts",
        "freqtrade_worker_status IN "
        "('UNCONFIGURED','NOT_VERIFIED','VERIFIED','FAILED','STALE')",
    )
    op.create_check_constraint(
        "ck_exchange_accounts_freqtrade_auth_version",
        "exchange_accounts",
        "freqtrade_auth_version >= 0",
    )
    op.create_check_constraint(
        "ck_exchange_accounts_freqtrade_worker_shape",
        "exchange_accounts",
        "(freqtrade_worker_mode = 'UNCONFIGURED' "
        "AND freqtrade_worker_status = 'UNCONFIGURED' "
        "AND freqtrade_worker_name IS NULL AND freqtrade_worker_url IS NULL "
        "AND freqtrade_auth_ciphertext IS NULL AND freqtrade_auth_version = 0) OR "
        "(freqtrade_worker_mode IN ('DRY_RUN','LIVE') "
        "AND freqtrade_worker_status <> 'UNCONFIGURED' "
        "AND freqtrade_worker_name IS NOT NULL AND freqtrade_worker_url IS NOT NULL "
        "AND freqtrade_auth_ciphertext IS NOT NULL AND freqtrade_auth_version >= 1 "
        "AND venue IN ('BINANCE','HYPERLIQUID'))",
    )
    for column in (
        "freqtrade_worker_mode",
        "freqtrade_worker_status",
        "freqtrade_auth_metadata",
        "freqtrade_auth_version",
        "freqtrade_hip3_dexes",
    ):
        op.alter_column("exchange_accounts", column, server_default=None)


def downgrade() -> None:
    connection = op.get_bind()
    configured = connection.execute(
        sa.text(
            "SELECT count(*) FROM exchange_accounts "
            "WHERE freqtrade_worker_mode <> 'UNCONFIGURED'"
        )
    ).scalar_one()
    if int(configured) > 0:
        raise RuntimeError(
            "0029 downgrade requires clearing every account-bound Freqtrade worker first"
        )
    op.drop_constraint(
        "ck_exchange_accounts_freqtrade_worker_shape",
        "exchange_accounts",
        type_="check",
    )
    op.drop_constraint(
        "ck_exchange_accounts_freqtrade_auth_version",
        "exchange_accounts",
        type_="check",
    )
    op.drop_constraint(
        "ck_exchange_accounts_freqtrade_worker_status",
        "exchange_accounts",
        type_="check",
    )
    op.drop_constraint(
        "ck_exchange_accounts_freqtrade_worker_mode",
        "exchange_accounts",
        type_="check",
    )
    for column in (
        "freqtrade_last_verified_at",
        "freqtrade_last_check_at",
        "freqtrade_error_code",
        "freqtrade_hip3_dexes",
        "freqtrade_auth_version",
        "freqtrade_auth_metadata",
        "freqtrade_auth_ciphertext",
        "freqtrade_worker_status",
        "freqtrade_worker_mode",
        "freqtrade_worker_url",
        "freqtrade_worker_name",
    ):
        op.drop_column("exchange_accounts", column)
