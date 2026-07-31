"""add shared Perptape feed cache

Revision ID: 20260731_0004
Revises: 20260731_0003
Create Date: 2026-07-31 17:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260731_0004"
down_revision: str | None = "20260731_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "perptape_feeds",
        sa.Column("feed_key", sa.String(length=32), nullable=False),
        sa.Column("contract_version", sa.String(length=120), nullable=False),
        sa.Column(
            "candidates",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_allowed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(candidates) = 'array'",
            name="ck_perptape_feeds_candidates_array",
        ),
        sa.CheckConstraint(
            "next_allowed_at >= generated_at",
            name="ck_perptape_feeds_refresh_window",
        ),
        sa.CheckConstraint("version >= 1", name="ck_perptape_feeds_version"),
        sa.PrimaryKeyConstraint("feed_key"),
    )


def downgrade() -> None:
    op.drop_table("perptape_feeds")
