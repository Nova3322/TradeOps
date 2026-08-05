"""select one direct capital treasury provider

Revision ID: 20260805_0014
Revises: 20260805_0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260805_0014"
down_revision: str | None = "20260805_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "direct_capital_configurations",
        sa.Column(
            "treasury_provider",
            sa.String(32),
            nullable=False,
            server_default="NOTILT_VAULT",
        ),
    )
    op.create_check_constraint(
        "ck_direct_capital_configuration_treasury_provider",
        "direct_capital_configurations",
        "treasury_provider IN ('NOTILT_VAULT','SAFE_SPENDING_LIMIT')",
    )
    op.alter_column("direct_capital_configurations", "treasury_provider", server_default=None)


def downgrade() -> None:
    op.drop_constraint(
        "ck_direct_capital_configuration_treasury_provider",
        "direct_capital_configurations",
        type_="check",
    )
    op.drop_column("direct_capital_configurations", "treasury_provider")
