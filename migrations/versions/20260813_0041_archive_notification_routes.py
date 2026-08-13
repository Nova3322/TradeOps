"""Archive notification routes without deleting delivery history.

Revision ID: 20260813_0041
Revises: 20260813_0040
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260813_0041"
down_revision: str | None = "20260813_0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notification_routes",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "notification_routes",
        sa.Column("deleted_by", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_notification_routes_deleted_by",
        "notification_routes",
        "users",
        ["deleted_by"],
        ["user_id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "uq_notification_routes_team_name",
        "notification_routes",
        type_="unique",
    )
    op.create_index(
        "uq_notification_routes_team_active_name",
        "notification_routes",
        ["team_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.execute(
        "UPDATE notification_routes "
        "SET name = LEFT(name, 70) || ' [archived ' || notification_route_id::text || ']' "
        "WHERE deleted_at IS NOT NULL"
    )
    op.drop_index(
        "uq_notification_routes_team_active_name",
        table_name="notification_routes",
    )
    op.create_unique_constraint(
        "uq_notification_routes_team_name",
        "notification_routes",
        ["team_id", "name"],
    )
    op.drop_constraint(
        "fk_notification_routes_deleted_by",
        "notification_routes",
        type_="foreignkey",
    )
    op.drop_column("notification_routes", "deleted_by")
    op.drop_column("notification_routes", "deleted_at")
