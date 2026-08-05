"""add Safe Spending Limits as a direct capital provider

Revision ID: 20260805_0013
Revises: 20260805_0012
"""

from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "20260805_0013"
down_revision: str | None = "20260805_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("direct_capital_configurations", sa.Column("safe_address", sa.String(42)))
    op.add_column("direct_capital_configurations", sa.Column("safe_delegate_address", sa.String(42)))
    op.add_column(
        "direct_capital_operations",
        sa.Column("treasury_provider", sa.String(32), nullable=False, server_default="NOTILT_VAULT"),
    )
    op.create_check_constraint(
        "ck_direct_capital_treasury_provider",
        "direct_capital_operations",
        "treasury_provider IN ('NOTILT_VAULT','SAFE_SPENDING_LIMIT')",
    )
    op.alter_column("direct_capital_operations", "treasury_provider", server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_direct_capital_treasury_provider", "direct_capital_operations", type_="check")
    op.drop_column("direct_capital_operations", "treasury_provider")
    op.drop_column("direct_capital_configurations", "safe_delegate_address")
    op.drop_column("direct_capital_configurations", "safe_address")
