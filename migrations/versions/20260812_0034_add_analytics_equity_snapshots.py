"""Add report-library-neutral equity snapshots.

Revision ID: 20260812_0034
Revises: 20260812_0033
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260812_0034"
down_revision: str | None = "20260812_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AMOUNT = sa.Numeric(38, 18)


def upgrade() -> None:
    op.create_table(
        "analytics_equity_snapshots",
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("account_id", sa.String(120), nullable=False),
        sa.Column("venue", sa.String(64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=True),
        sa.Column("equity", AMOUNT, nullable=False),
        sa.Column("currency", sa.String(32), nullable=False),
        sa.Column("source_kind", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "environment IN ('SHADOW','LIVE')",
            name="ck_analytics_equity_snapshots_environment",
        ),
        sa.CheckConstraint(
            "(environment = 'SHADOW' AND generation IS NOT NULL) OR "
            "(environment = 'LIVE' AND generation IS NULL)",
            name="ck_analytics_equity_snapshots_generation",
        ),
        sa.CheckConstraint(
            "equity >= 0", name="ck_analytics_equity_snapshots_equity"
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_analytics_equity_snapshots_version"
        ),
        sa.ForeignKeyConstraint(["team_id"], ["teams.team_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("snapshot_id"),
        sa.UniqueConstraint(
            "team_id",
            "environment",
            "source_kind",
            "source_id",
            name="uq_analytics_equity_snapshots_source",
        ),
    )
    op.create_index(
        "ix_analytics_equity_snapshots_scope_time",
        "analytics_equity_snapshots",
        ["team_id", "environment", "generation", "observed_at"],
        unique=False,
    )
    op.execute(
        """
        INSERT INTO analytics_equity_snapshots (
            snapshot_id, team_id, environment, account_id, venue, generation,
            equity, currency, source_kind, source_id, version, metadata,
            observed_at, recorded_at
        )
        SELECT gen_random_uuid(), team_id, 'SHADOW', 'TEAM_SHADOW', 'TRADINGOPS',
               generation, equity, 'U', 'TEAM_SHADOW_ACCOUNT',
               shadow_account_id::text || ':' || version::text, version,
               jsonb_build_object('shadow_account_id', shadow_account_id::text),
               updated_at, now()
        FROM team_shadow_accounts
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_analytics_equity_snapshots_scope_time",
        table_name="analytics_equity_snapshots",
    )
    op.drop_table("analytics_equity_snapshots")
