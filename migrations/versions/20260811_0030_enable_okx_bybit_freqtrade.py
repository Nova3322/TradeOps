"""Enable account-bound Freqtrade workers for OKX and Bybit.

Revision ID: 20260811_0030
Revises: 20260811_0029
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_0030"
down_revision: str | None = "20260811_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _worker_shape(venues: str) -> str:
    return (
        "(freqtrade_worker_mode = 'UNCONFIGURED' "
        "AND freqtrade_worker_status = 'UNCONFIGURED' "
        "AND freqtrade_worker_name IS NULL AND freqtrade_worker_url IS NULL "
        "AND freqtrade_auth_ciphertext IS NULL AND freqtrade_auth_version = 0) OR "
        "(freqtrade_worker_mode IN ('DRY_RUN','LIVE') "
        "AND freqtrade_worker_status <> 'UNCONFIGURED' "
        "AND freqtrade_worker_name IS NOT NULL AND freqtrade_worker_url IS NOT NULL "
        "AND freqtrade_auth_ciphertext IS NOT NULL AND freqtrade_auth_version >= 1 "
        f"AND venue IN ({venues}))"
    )


def upgrade() -> None:
    op.drop_constraint(
        "ck_exchange_accounts_freqtrade_worker_shape",
        "exchange_accounts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_exchange_accounts_freqtrade_worker_shape",
        "exchange_accounts",
        _worker_shape("'BINANCE','HYPERLIQUID','OKX','BYBIT'"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    configured = connection.execute(
        sa.text(
            "SELECT count(*) FROM exchange_accounts "
            "WHERE venue IN ('OKX','BYBIT') "
            "AND freqtrade_worker_mode <> 'UNCONFIGURED'"
        )
    ).scalar_one()
    if int(configured) > 0:
        raise RuntimeError(
            "0030 downgrade requires clearing every OKX/Bybit account-bound "
            "Freqtrade worker first"
        )
    op.drop_constraint(
        "ck_exchange_accounts_freqtrade_worker_shape",
        "exchange_accounts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_exchange_accounts_freqtrade_worker_shape",
        "exchange_accounts",
        _worker_shape("'BINANCE','HYPERLIQUID'"),
    )
