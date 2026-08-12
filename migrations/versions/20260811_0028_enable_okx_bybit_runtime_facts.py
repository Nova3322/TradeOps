"""Enable database-bound OKX and Bybit read-only runtime facts.

Revision ID: 20260811_0028
Revises: 20260811_0027
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_0028"
down_revision: str | None = "20260811_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _runtime_constraint(venues: str) -> str:
    return (
        "NOT runtime_sync_enabled OR (active AND connection_status = 'VERIFIED' "
        f"AND credential_version >= 1 AND venue IN ({venues}) "
        "AND runtime_service_principal_id IS NOT NULL)"
    )


def upgrade() -> None:
    op.drop_constraint(
        "ck_exchange_accounts_runtime_sync_ready",
        "exchange_accounts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_exchange_accounts_runtime_sync_ready",
        "exchange_accounts",
        _runtime_constraint("'BINANCE','HYPERLIQUID','OKX','BYBIT'"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    unsupported = connection.execute(
        sa.text(
            "SELECT count(*) FROM exchange_accounts WHERE runtime_sync_enabled "
            "AND venue IN ('OKX','BYBIT')"
        )
    ).scalar_one()
    if int(unsupported) > 0:
        raise RuntimeError(
            "0028 downgrade requires disabling every OKX and Bybit runtime binding first"
        )
    op.drop_constraint(
        "ck_exchange_accounts_runtime_sync_ready",
        "exchange_accounts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_exchange_accounts_runtime_sync_ready",
        "exchange_accounts",
        _runtime_constraint("'BINANCE','HYPERLIQUID'"),
    )
