"""Persist read-only QuantStats and Pyfolio report artifacts.

Revision ID: 20260812_0039
Revises: 20260812_0038
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260812_0039"
down_revision: str | None = "20260812_0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analytics_reports",
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("engine", sa.String(32), nullable=False),
        sa.Column("library_name", sa.String(80), nullable=False),
        sa.Column("library_version", sa.String(32), nullable=False),
        sa.Column("dataset_version", sa.String(64), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=True),
        sa.Column("account_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("venues", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("from_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("to_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("chart_count", sa.Integer(), nullable=False),
        sa.Column("coverage", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("artifact_html", sa.Text(), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "engine IN ('QUANTSTATS','PYFOLIO')", name="ck_analytics_reports_engine"
        ),
        sa.CheckConstraint(
            "environment IN ('SHADOW','LIVE')",
            name="ck_analytics_reports_environment",
        ),
        sa.CheckConstraint(
            "(environment = 'SHADOW' AND generation IS NOT NULL) OR "
            "(environment = 'LIVE' AND generation IS NULL)",
            name="ck_analytics_reports_generation",
        ),
        sa.CheckConstraint(
            "status IN ('READY','FAILED')", name="ck_analytics_reports_status"
        ),
        sa.CheckConstraint("chart_count >= 0", name="ck_analytics_reports_chart_count"),
        sa.CheckConstraint("version >= 1", name="ck_analytics_reports_version"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.workspace_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["team_id"], ["teams.team_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.user_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("report_id"),
        sa.UniqueConstraint(
            "team_id",
            "created_by",
            "idempotency_key",
            name="uq_analytics_reports_idempotency",
        ),
    )
    op.create_index(
        "ix_analytics_reports_scope_created",
        "analytics_reports",
        ["team_id", "environment", "generation", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_analytics_reports_scope_created", table_name="analytics_reports")
    op.drop_table("analytics_reports")
