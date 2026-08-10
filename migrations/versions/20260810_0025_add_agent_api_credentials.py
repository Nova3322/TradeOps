"""Add Agent API credential lifecycle to the existing User identity.

Revision ID: 20260810_0025
Revises: 20260810_0024
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260810_0025"
down_revision: str | None = "20260810_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("service_kind", sa.String(length=16), nullable=True))
    op.add_column("users", sa.Column("agent_token_digest", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("agent_token_hint", sa.String(length=32), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "agent_token_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "users",
        sa.Column("agent_token_created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("agent_token_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("agent_token_last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE users SET service_kind = 'INTERNAL' WHERE principal_type = 'SERVICE'")
    op.create_check_constraint(
        "ck_users_service_kind",
        "users",
        "(principal_type = 'HUMAN' AND service_kind IS NULL) OR "
        "(principal_type = 'SERVICE' AND service_kind IN ('INTERNAL','AGENT'))",
    )
    op.create_check_constraint(
        "ck_users_agent_token_version",
        "users",
        "agent_token_version >= 0",
    )
    op.create_check_constraint(
        "ck_users_agent_token_shape",
        "users",
        "(service_kind = 'AGENT' AND agent_token_version >= 1 "
        "AND agent_token_digest IS NOT NULL AND agent_token_hint IS NOT NULL "
        "AND agent_token_created_at IS NOT NULL AND agent_token_expires_at IS NOT NULL) OR "
        "(service_kind IS DISTINCT FROM 'AGENT' AND agent_token_version = 0 "
        "AND agent_token_digest IS NULL AND agent_token_hint IS NULL "
        "AND agent_token_created_at IS NULL AND agent_token_expires_at IS NULL "
        "AND agent_token_last_used_at IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_agent_token_shape", "users", type_="check")
    op.drop_constraint("ck_users_agent_token_version", "users", type_="check")
    op.drop_constraint("ck_users_service_kind", "users", type_="check")
    op.drop_column("users", "agent_token_last_used_at")
    op.drop_column("users", "agent_token_expires_at")
    op.drop_column("users", "agent_token_created_at")
    op.drop_column("users", "agent_token_version")
    op.drop_column("users", "agent_token_hint")
    op.drop_column("users", "agent_token_digest")
    op.drop_column("users", "service_kind")
