"""add human password credentials and session revision

Revision ID: 20260809_0015
Revises: 20260805_0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260809_0015"
down_revision: str | None = "20260805_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.String(255), nullable=True))
    op.add_column(
        "users",
        sa.Column("auth_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "users",
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column("users", "auth_version", server_default=None)


def downgrade() -> None:
    op.drop_column("users", "password_changed_at")
    op.drop_column("users", "auth_version")
    op.drop_column("users", "password_hash")
