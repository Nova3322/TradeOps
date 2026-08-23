"""freeze risk-tier leverage through the approved execution chain

Revision ID: 20260821_0049
Revises: 20260820_0048
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260821_0049"
down_revision: str | None = "20260820_0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_instruments_min_notional_nonnegative", "instruments", type_="check"
    )
    op.create_check_constraint(
        "ck_instruments_min_notional_positive", "instruments", "minimum_notional > 0"
    )
    for table in ("proposals", "risk_decisions", "trading_authorizations", "order_intents"):
        op.add_column(table, sa.Column("leverage", sa.Numeric(38, 18), nullable=True))
        op.create_check_constraint(
            f"ck_{table}_leverage_positive",
            table,
            "leverage IS NULL OR leverage > 0",
        )


def downgrade() -> None:
    for table in ("order_intents", "trading_authorizations", "risk_decisions", "proposals"):
        op.drop_constraint(f"ck_{table}_leverage_positive", table, type_="check")
        op.drop_column(table, "leverage")
    op.drop_constraint("ck_instruments_min_notional_positive", "instruments", type_="check")
    op.create_check_constraint(
        "ck_instruments_min_notional_nonnegative",
        "instruments",
        "minimum_notional >= 0",
    )
