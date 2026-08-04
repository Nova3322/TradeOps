"""add proposal default configs

Revision ID: 20260802_0008
Revises: 20260802_0007
Create Date: 2026-08-02 23:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260802_0008"
down_revision: str | None = "20260802_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "proposal_default_configs",
        sa.Column("config_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=16), nullable=False),
        sa.Column("account_id", sa.String(length=120), nullable=False),
        sa.Column("risk_tier", sa.String(length=16), nullable=False),
        sa.Column("notional", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("max_risk", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("invalidation_bps", sa.Integer(), nullable=False),
        sa.Column("expires_in_minutes", sa.Integer(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("environment = 'LIVE'", name="ck_proposal_defaults_live"),
        sa.CheckConstraint(
            "risk_tier IN ('LOW','MEDIUM','HIGH')", name="ck_proposal_defaults_risk_tier"
        ),
        sa.CheckConstraint("notional > 0", name="ck_proposal_defaults_notional_positive"),
        sa.CheckConstraint("max_risk > 0", name="ck_proposal_defaults_risk_positive"),
        sa.CheckConstraint("invalidation_bps BETWEEN 1 AND 5000", name="ck_proposal_defaults_bps"),
        sa.CheckConstraint(
            "expires_in_minutes BETWEEN 5 AND 1440", name="ck_proposal_defaults_expiry"
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("config_id"),
        sa.UniqueConstraint("version", name="uq_proposal_default_configs_version"),
    )
    op.create_index(
        "uq_proposal_default_configs_active",
        "proposal_default_configs",
        ["active"],
        unique=True,
        postgresql_where=sa.text("active"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_proposal_default_configs_active",
        table_name="proposal_default_configs",
    )
    op.drop_table("proposal_default_configs")
