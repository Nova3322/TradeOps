"""add managed capital valuation

Revision ID: 20260731_0002
Revises: 20260718_0001
Create Date: 2026-07-31 16:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260731_0002"
down_revision: str | None = "20260718_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "account_equities",
        sa.Column("valuation_currency", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "account_equities",
        sa.Column("valuation_price", sa.Numeric(precision=38, scale=18), nullable=True),
    )
    op.add_column(
        "account_equities",
        sa.Column("valuation_equity", sa.Numeric(precision=38, scale=18), nullable=True),
    )
    op.add_column(
        "account_equities",
        sa.Column("valuation_observed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_account_equities_valuation_price",
        "account_equities",
        "valuation_price IS NULL OR valuation_price > 0",
    )
    op.create_check_constraint(
        "ck_account_equities_valuation_equity",
        "account_equities",
        "valuation_equity IS NULL OR valuation_equity >= 0",
    )
    op.drop_constraint(
        "uq_account_equities_scope",
        "account_equities",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_account_equities_scope",
        "account_equities",
        ["environment", "account_id", "venue", "currency"],
    )
    op.execute(
        """
        UPDATE account_equities
        SET valuation_currency = 'USD',
            valuation_price = 1,
            valuation_equity = equity,
            valuation_observed_at = observed_at
        WHERE upper(currency) IN ('USD', 'USDC', 'USDT', 'USDT0')
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_account_equities_scope",
        "account_equities",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_account_equities_scope",
        "account_equities",
        ["environment", "account_id", "venue"],
    )
    op.drop_constraint(
        "ck_account_equities_valuation_equity",
        "account_equities",
        type_="check",
    )
    op.drop_constraint(
        "ck_account_equities_valuation_price",
        "account_equities",
        type_="check",
    )
    op.drop_column("account_equities", "valuation_observed_at")
    op.drop_column("account_equities", "valuation_equity")
    op.drop_column("account_equities", "valuation_price")
    op.drop_column("account_equities", "valuation_currency")
