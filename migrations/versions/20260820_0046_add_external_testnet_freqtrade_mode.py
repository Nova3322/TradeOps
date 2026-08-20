"""Add a distinct external TESTNET Freqtrade worker mode.

Revision ID: 20260820_0046
Revises: 20260818_0045
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_0046"
down_revision: str | None = "20260818_0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


WORKER_SHAPE = (
    "(freqtrade_worker_mode = 'UNCONFIGURED' "
    "AND freqtrade_worker_status = 'UNCONFIGURED' "
    "AND freqtrade_worker_name IS NULL AND freqtrade_worker_url IS NULL "
    "AND freqtrade_auth_ciphertext IS NULL AND freqtrade_auth_version = 0) OR "
    "(freqtrade_worker_mode IN ('DRY_RUN','TESTNET','LIVE') "
    "AND freqtrade_worker_status <> 'UNCONFIGURED' "
    "AND freqtrade_worker_name IS NOT NULL AND freqtrade_worker_url IS NOT NULL "
    "AND freqtrade_auth_ciphertext IS NOT NULL AND freqtrade_auth_version >= 1 "
    "AND venue IN ('BINANCE','HYPERLIQUID','OKX','BYBIT'))"
)

LEGACY_WORKER_SHAPE = WORKER_SHAPE.replace("'DRY_RUN','TESTNET','LIVE'", "'DRY_RUN','LIVE'")


def upgrade() -> None:
    op.drop_constraint(
        "ck_exchange_accounts_freqtrade_worker_shape",
        "exchange_accounts",
        type_="check",
    )
    op.drop_constraint(
        "ck_exchange_accounts_freqtrade_worker_mode",
        "exchange_accounts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_exchange_accounts_freqtrade_worker_mode",
        "exchange_accounts",
        "freqtrade_worker_mode IN ('UNCONFIGURED','DRY_RUN','TESTNET','LIVE')",
    )
    op.create_check_constraint(
        "ck_exchange_accounts_freqtrade_worker_shape",
        "exchange_accounts",
        WORKER_SHAPE,
    )


def downgrade() -> None:
    connection = op.get_bind()
    configured = connection.execute(
        sa.text(
            "SELECT count(*) FROM exchange_accounts "
            "WHERE freqtrade_worker_mode = 'TESTNET'"
        )
    ).scalar_one()
    if int(configured) > 0:
        raise RuntimeError(
            "0046 downgrade requires clearing every external TESTNET Freqtrade worker first"
        )
    op.drop_constraint(
        "ck_exchange_accounts_freqtrade_worker_shape",
        "exchange_accounts",
        type_="check",
    )
    op.drop_constraint(
        "ck_exchange_accounts_freqtrade_worker_mode",
        "exchange_accounts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_exchange_accounts_freqtrade_worker_mode",
        "exchange_accounts",
        "freqtrade_worker_mode IN ('UNCONFIGURED','DRY_RUN','LIVE')",
    )
    op.create_check_constraint(
        "ck_exchange_accounts_freqtrade_worker_shape",
        "exchange_accounts",
        LEGACY_WORKER_SHAPE,
    )
