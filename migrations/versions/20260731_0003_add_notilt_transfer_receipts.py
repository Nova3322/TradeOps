"""add NoTilt transfer plans and receipt evidence

Revision ID: 20260731_0003
Revises: 20260731_0002
Create Date: 2026-07-31 18:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260731_0003"
down_revision: str | None = "20260731_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "capital_transfers",
        sa.Column("transport", sa.String(length=16), nullable=False, server_default="MOCK"),
    )
    op.add_column(
        "capital_transfers",
        sa.Column("chain_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "capital_transfers",
        sa.Column("transport_state", sa.String(length=48), nullable=True),
    )
    op.add_column(
        "capital_transfers",
        sa.Column(
            "planned_transactions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "capital_transfers",
        sa.Column(
            "confirmed_transaction_hashes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "capital_transfers",
        sa.Column("protocol_request_id", sa.String(length=66), nullable=True),
    )
    op.add_column(
        "capital_transfers",
        sa.Column("protocol_execute_after", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "capital_transfers",
        sa.Column("protocol_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_capital_transfers_transport",
        "capital_transfers",
        "transport IN ('MOCK','NOTILT')",
    )
    op.create_check_constraint(
        "ck_capital_transfers_chain",
        "capital_transfers",
        "chain_id IS NULL OR chain_id IN (1,56,42161)",
    )
    op.create_check_constraint(
        "ck_capital_transfers_transport_state",
        "capital_transfers",
        "transport_state IS NULL OR transport_state IN ("
        "'DEPOSIT_PLAN_READY','DEPOSIT_CONFIRMED',"
        "'RELEASE_REQUEST_PLAN_READY','RELEASE_REQUEST_CONFIRMED',"
        "'RELEASE_EXECUTION_PLAN_READY','RELEASE_EXECUTION_CONFIRMED',"
        "'RELEASE_CANCELLATION_PLAN_READY','RELEASE_CANCELLED')",
    )
    op.alter_column("capital_transfers", "transport", server_default=None)
    op.alter_column("capital_transfers", "planned_transactions", server_default=None)
    op.alter_column("capital_transfers", "confirmed_transaction_hashes", server_default=None)


def downgrade() -> None:
    op.drop_constraint(
        "ck_capital_transfers_transport_state",
        "capital_transfers",
        type_="check",
    )
    op.drop_constraint(
        "ck_capital_transfers_chain",
        "capital_transfers",
        type_="check",
    )
    op.drop_constraint(
        "ck_capital_transfers_transport",
        "capital_transfers",
        type_="check",
    )
    op.drop_column("capital_transfers", "protocol_expires_at")
    op.drop_column("capital_transfers", "protocol_execute_after")
    op.drop_column("capital_transfers", "protocol_request_id")
    op.drop_column("capital_transfers", "confirmed_transaction_hashes")
    op.drop_column("capital_transfers", "planned_transactions")
    op.drop_column("capital_transfers", "transport_state")
    op.drop_column("capital_transfers", "chain_id")
    op.drop_column("capital_transfers", "transport")
