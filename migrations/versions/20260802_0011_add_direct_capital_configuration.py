"""add versioned direct capital configuration

Revision ID: 20260802_0011
Revises: 20260802_0010
Create Date: 2026-08-02 23:59:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260802_0011"
down_revision: str | None = "20260802_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "direct_capital_configurations",
        sa.Column("config_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("network", sa.String(length=64), nullable=False),
        sa.Column("asset", sa.String(length=32), nullable=False),
        sa.Column("vault_id", sa.String(length=160), nullable=True),
        sa.Column("vault_address", sa.String(length=42), nullable=True),
        sa.Column("owned_arbitrum_address", sa.String(length=42), nullable=True),
        sa.Column("binance_account_id", sa.String(length=120), nullable=True),
        sa.Column("binance_deposit_address", sa.String(length=42), nullable=True),
        sa.Column("binance_withdrawal_address", sa.String(length=42), nullable=True),
        sa.Column("hyperliquid_account_id", sa.String(length=120), nullable=True),
        sa.Column("hyperliquid_bridge_address", sa.String(length=42), nullable=True),
        sa.Column("max_amount", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("max_fee", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "asset = 'USDC'", name="ck_direct_capital_configuration_asset"
        ),
        sa.CheckConstraint(
            "max_amount IS NULL OR max_amount > 0",
            name="ck_direct_capital_configuration_max_amount",
        ),
        sa.CheckConstraint(
            "max_fee IS NULL OR max_fee >= 0",
            name="ck_direct_capital_configuration_max_fee",
        ),
        sa.CheckConstraint(
            "network = 'ARBITRUM'", name="ck_direct_capital_configuration_network"
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_direct_capital_configuration_version"
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("config_id"),
        sa.UniqueConstraint(
            "version", name="uq_direct_capital_configurations_version"
        ),
    )
    op.create_index(
        "uq_direct_capital_configuration_active",
        "direct_capital_configurations",
        ["active"],
        unique=True,
        postgresql_where=sa.text("active"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_direct_capital_configuration_active",
        table_name="direct_capital_configurations",
        postgresql_where=sa.text("active"),
    )
    op.drop_table("direct_capital_configurations")
