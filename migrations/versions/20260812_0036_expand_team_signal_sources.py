"""Allow one space to retain Perptape and multiple Webhook signal sources.

Revision ID: 20260812_0036
Revises: 20260812_0035
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260812_0036"
down_revision: str | None = "20260812_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("team_signal_sources", sa.Column("name", sa.String(120), nullable=True))
    op.add_column(
        "team_signal_sources",
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "team_signal_sources",
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "team_signal_sources", sa.Column("last_error_code", sa.String(120), nullable=True)
    )
    op.add_column(
        "team_signal_sources",
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "team_signal_sources",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "team_signal_sources", sa.Column("deleted_by", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_team_signal_sources_deleted_by",
        "team_signal_sources",
        "users",
        ["deleted_by"],
        ["user_id"],
        ondelete="RESTRICT",
    )
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE team_signal_sources SET name = CASE "
            "WHEN mode = 'PERPTAPE' THEN 'Perptape' "
            "ELSE 'Webhook' END WHERE name IS NULL"
        )
    )
    op.alter_column("team_signal_sources", "name", nullable=False)
    op.drop_constraint("uq_team_signal_sources_team", "team_signal_sources", type_="unique")
    op.create_check_constraint(
        "ck_team_signal_sources_consecutive_failures",
        "team_signal_sources",
        "consecutive_failures >= 0",
    )
    op.create_index(
        "ix_team_signal_sources_team_deleted",
        "team_signal_sources",
        ["team_id", "deleted_at"],
    )
    op.create_index(
        "uq_team_signal_sources_active_perptape",
        "team_signal_sources",
        ["team_id"],
        unique=True,
        postgresql_where=sa.text("mode = 'PERPTAPE' AND deleted_at IS NULL"),
    )
    op.create_index(
        "uq_team_signal_sources_active_name",
        "team_signal_sources",
        ["team_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.alter_column("team_signal_sources", "consecutive_failures", server_default=None)


def downgrade() -> None:
    connection = op.get_bind()
    multiple = connection.execute(
        sa.text(
            "SELECT team_id FROM team_signal_sources WHERE deleted_at IS NULL "
            "GROUP BY team_id HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if multiple is not None:
        raise RuntimeError(
            "downgrade blocked: a space has multiple retained signal sources"
        )
    op.drop_index("uq_team_signal_sources_active_name", table_name="team_signal_sources")
    op.drop_index("uq_team_signal_sources_active_perptape", table_name="team_signal_sources")
    op.drop_index("ix_team_signal_sources_team_deleted", table_name="team_signal_sources")
    op.drop_constraint(
        "ck_team_signal_sources_consecutive_failures",
        "team_signal_sources",
        type_="check",
    )
    op.create_unique_constraint(
        "uq_team_signal_sources_team", "team_signal_sources", ["team_id"]
    )
    op.drop_constraint(
        "fk_team_signal_sources_deleted_by", "team_signal_sources", type_="foreignkey"
    )
    op.drop_column("team_signal_sources", "deleted_by")
    op.drop_column("team_signal_sources", "deleted_at")
    op.drop_column("team_signal_sources", "consecutive_failures")
    op.drop_column("team_signal_sources", "last_error_code")
    op.drop_column("team_signal_sources", "last_success_at")
    op.drop_column("team_signal_sources", "last_checked_at")
    op.drop_column("team_signal_sources", "name")
