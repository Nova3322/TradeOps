"""Persist the Freqtrade dispatch handoff before an external write.

Revision ID: 20260811_0031
Revises: 20260811_0030
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_0031"
down_revision: str | None = "20260811_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ACTIVE_STATUSES = (
    "status IN ('PENDING','RESERVED','READY','DISPATCHING','SENT',"
    "'PARTIALLY_FILLED','UNKNOWN')"
)
_LEGACY_ACTIVE_STATUSES = (
    "status IN ('PENDING','RESERVED','READY','SENT','PARTIALLY_FILLED','UNKNOWN')"
)


def upgrade() -> None:
    op.add_column(
        "order_intents",
        sa.Column("dispatch_backend", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "order_intents",
        sa.Column("dispatch_account_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "order_intents",
        sa.Column("dispatch_auth_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "order_intents",
        sa.Column("dispatch_owner_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "order_intents",
        sa.Column("dispatch_fencing_token", sa.Integer(), nullable=True),
    )
    op.add_column(
        "order_intents",
        sa.Column("dispatch_external_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "order_intents",
        sa.Column("dispatch_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint("ck_order_intents_status", "order_intents", type_="check")
    op.create_check_constraint(
        "ck_order_intents_status",
        "order_intents",
        "status IN ('PENDING','RESERVED','READY','DISPATCHING','SENT',"
        "'PARTIALLY_FILLED','FILLED','CANCELLED','REJECTED','UNKNOWN')",
    )
    op.create_check_constraint(
        "ck_order_intents_dispatch_backend",
        "order_intents",
        "dispatch_backend IS NULL OR dispatch_backend = 'FREQTRADE'",
    )
    op.create_check_constraint(
        "ck_order_intents_dispatch_account_version",
        "order_intents",
        "dispatch_account_version IS NULL OR dispatch_account_version >= 1",
    )
    op.create_check_constraint(
        "ck_order_intents_dispatch_auth_version",
        "order_intents",
        "dispatch_auth_version IS NULL OR dispatch_auth_version >= 1",
    )
    op.create_check_constraint(
        "ck_order_intents_dispatch_fencing_token",
        "order_intents",
        "dispatch_fencing_token IS NULL OR dispatch_fencing_token >= 1",
    )
    op.create_check_constraint(
        "ck_order_intents_dispatch_shape",
        "order_intents",
        "(dispatch_backend IS NULL AND dispatch_account_version IS NULL "
        "AND dispatch_auth_version IS NULL AND dispatch_owner_id IS NULL "
        "AND dispatch_fencing_token IS NULL AND dispatch_started_at IS NULL "
        "AND dispatch_external_id IS NULL) OR "
        "(dispatch_backend = 'FREQTRADE' AND dispatch_account_version IS NOT NULL "
        "AND dispatch_auth_version IS NOT NULL AND dispatch_owner_id IS NOT NULL "
        "AND dispatch_fencing_token IS NOT NULL AND dispatch_started_at IS NOT NULL)",
    )
    op.drop_index("uq_order_intents_one_active_campaign", table_name="order_intents")
    op.create_index(
        "uq_order_intents_one_active_campaign",
        "order_intents",
        ["campaign_id"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE_STATUSES),
    )


def downgrade() -> None:
    connection = op.get_bind()
    durable_dispatches = connection.execute(
        sa.text(
            "SELECT count(*) FROM order_intents "
            "WHERE status = 'DISPATCHING' OR dispatch_backend IS NOT NULL"
        )
    ).scalar_one()
    if int(durable_dispatches) > 0:
        raise RuntimeError(
            "0031 downgrade requires resolving and removing every durable "
            "Freqtrade dispatch snapshot first"
        )
    op.drop_index("uq_order_intents_one_active_campaign", table_name="order_intents")
    op.create_index(
        "uq_order_intents_one_active_campaign",
        "order_intents",
        ["campaign_id"],
        unique=True,
        postgresql_where=sa.text(_LEGACY_ACTIVE_STATUSES),
    )
    op.drop_constraint("ck_order_intents_dispatch_shape", "order_intents", type_="check")
    op.drop_constraint(
        "ck_order_intents_dispatch_fencing_token", "order_intents", type_="check"
    )
    op.drop_constraint(
        "ck_order_intents_dispatch_auth_version", "order_intents", type_="check"
    )
    op.drop_constraint(
        "ck_order_intents_dispatch_account_version", "order_intents", type_="check"
    )
    op.drop_constraint("ck_order_intents_dispatch_backend", "order_intents", type_="check")
    op.drop_constraint("ck_order_intents_status", "order_intents", type_="check")
    op.create_check_constraint(
        "ck_order_intents_status",
        "order_intents",
        "status IN ('PENDING','RESERVED','READY','SENT','PARTIALLY_FILLED','FILLED',"
        "'CANCELLED','REJECTED','UNKNOWN')",
    )
    op.drop_column("order_intents", "dispatch_started_at")
    op.drop_column("order_intents", "dispatch_external_id")
    op.drop_column("order_intents", "dispatch_fencing_token")
    op.drop_column("order_intents", "dispatch_owner_id")
    op.drop_column("order_intents", "dispatch_auth_version")
    op.drop_column("order_intents", "dispatch_account_version")
    op.drop_column("order_intents", "dispatch_backend")
