"""add capital equity history

Revision ID: 20260802_0006
Revises: 20260801_0005
Create Date: 2026-08-02 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260802_0006"
down_revision: str | None = "20260801_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account_equity_observations",
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("account_equity_id", sa.Uuid(), nullable=False),
        sa.Column("environment", sa.String(length=16), nullable=False),
        sa.Column("location_type", sa.String(length=16), nullable=False),
        sa.Column("account_id", sa.String(length=120), nullable=False),
        sa.Column("venue", sa.String(length=64), nullable=False),
        sa.Column("currency", sa.String(length=32), nullable=False),
        sa.Column("equity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("available_balance", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("usd_equity", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "environment IN ('SHADOW','TESTNET','LIVE')",
            name="ck_account_equity_observations_environment",
        ),
        sa.CheckConstraint(
            "location_type IN ('VENUE','VAULT')",
            name="ck_account_equity_observations_location_type",
        ),
        sa.CheckConstraint("equity >= 0", name="ck_account_equity_observations_equity"),
        sa.CheckConstraint(
            "available_balance >= 0",
            name="ck_account_equity_observations_available_balance",
        ),
        sa.CheckConstraint(
            "usd_equity IS NULL OR usd_equity >= 0",
            name="ck_account_equity_observations_usd_equity",
        ),
        sa.ForeignKeyConstraint(
            ["account_equity_id"],
            ["account_equities.account_equity_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("observation_id"),
        sa.UniqueConstraint(
            "account_equity_id",
            "observed_at",
            name="uq_account_equity_observations_fact_time",
        ),
    )
    op.create_index(
        "ix_account_equity_observations_scope_time",
        "account_equity_observations",
        ["environment", "location_type", "venue", "account_id", "observed_at"],
        unique=False,
    )
    op.execute(
        """
        INSERT INTO account_equity_observations (
            observation_id,
            account_equity_id,
            environment,
            location_type,
            account_id,
            venue,
            currency,
            equity,
            available_balance,
            usd_equity,
            observed_at,
            recorded_at
        )
        SELECT
            gen_random_uuid(),
            account_equity_id,
            environment,
            location_type,
            account_id,
            venue,
            currency,
            equity,
            available_balance,
            CASE
                WHEN upper(currency) IN ('USD', 'USDC', 'USDT', 'USDT0') THEN equity
                ELSE valuation_equity
            END,
            observed_at,
            updated_at
        FROM account_equities
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_account_equity_observations_scope_time",
        table_name="account_equity_observations",
    )
    op.drop_table("account_equity_observations")
