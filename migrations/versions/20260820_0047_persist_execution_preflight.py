"""Persist Freqtrade runtime identity and automatic execution blockers.

Revision ID: 20260820_0047
Revises: 20260820_0046
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260820_0047"
down_revision: str | None = "20260820_0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "exchange_accounts",
        sa.Column("freqtrade_runtime_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "exchange_accounts",
        sa.Column(
            "freqtrade_runtime_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "order_intents",
        sa.Column("execution_blocker_code", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "order_intents",
        sa.Column("execution_blocker_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "order_intents",
        sa.Column("execution_blocker_component", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "order_intents",
        sa.Column("execution_blocker_next_action", sa.Text(), nullable=True),
    )
    op.add_column(
        "order_intents",
        sa.Column("execution_blocked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "order_intents",
        sa.Column("execution_last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "order_intents",
        sa.Column("execution_retry_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    for column in (
        "execution_retry_at",
        "execution_last_checked_at",
        "execution_blocked_at",
        "execution_blocker_next_action",
        "execution_blocker_component",
        "execution_blocker_reason",
        "execution_blocker_code",
    ):
        op.drop_column("order_intents", column)
    op.drop_column("exchange_accounts", "freqtrade_runtime_metadata")
    op.drop_column("exchange_accounts", "freqtrade_runtime_fingerprint")
