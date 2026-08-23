"""bind routed Telegram reviews to durable internal recipients

Revision ID: 20260822_0050
Revises: 20260821_0049
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260822_0050"
down_revision: str | None = "20260821_0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("notification_routes", "notification_deliveries"):
        op.add_column(table, sa.Column("recipient_user_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_recipient_user_id_users",
            table,
            "users",
            ["recipient_user_id"],
            ["user_id"],
            ondelete="RESTRICT",
        )
    op.create_index(
        "ix_notification_deliveries_recipient",
        "notification_deliveries",
        ["recipient_user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_deliveries_recipient",
        table_name="notification_deliveries",
    )
    for table in ("notification_deliveries", "notification_routes"):
        op.drop_constraint(
            f"fk_{table}_recipient_user_id_users",
            table,
            type_="foreignkey",
        )
        op.drop_column(table, "recipient_user_id")
