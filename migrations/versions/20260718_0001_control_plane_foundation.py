"""Create the durable control-plane foundation.

Revision ID: 20260718_0001
Revises: None
Create Date: 2026-07-18
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "command_receipts",
        sa.Column("receipt_id", sa.Uuid(), nullable=False),
        sa.Column("command_id", sa.Uuid(), nullable=False),
        sa.Column("caller_id", sa.String(length=255), nullable=False),
        sa.Column("command_type", sa.String(length=160), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(request_hash) = 64", name="ck_command_receipts_hash_length"),
        sa.CheckConstraint("state IN ('COMPLETED', 'REJECTED')", name="ck_command_receipts_state"),
        sa.PrimaryKeyConstraint("receipt_id"),
        sa.UniqueConstraint("command_id"),
        sa.UniqueConstraint(
            "caller_id",
            "command_type",
            "idempotency_key",
            name="uq_command_receipts_idempotency_scope",
        ),
    )

    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=160), nullable=False),
        sa.Column("aggregate_type", sa.String(length=120), nullable=False),
        sa.Column("aggregate_id", sa.String(length=255), nullable=False),
        sa.Column("command_id", sa.Uuid(), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("causation_id", sa.Uuid(), nullable=True),
        sa.Column("caller_id", sa.String(length=255), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("payload_schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(payload_hash) = 64", name="ck_audit_events_hash_length"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_audit_events_aggregate",
        "audit_events",
        ["aggregate_type", "aggregate_id", "occurred_at"],
    )
    op.create_index(
        "ix_audit_events_correlation", "audit_events", ["correlation_id", "occurred_at"]
    )

    op.create_table(
        "outbox_messages",
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("topic", sa.String(length=255), nullable=False),
        sa.Column("message_key", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=160), nullable=False),
        sa.Column("payload_schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("headers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publish_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error_code", sa.String(length=160), nullable=True),
        sa.CheckConstraint("publish_attempts >= 0", name="ck_outbox_publish_attempts_nonnegative"),
        sa.PrimaryKeyConstraint("message_id"),
    )
    op.create_index(
        "ix_outbox_unpublished",
        "outbox_messages",
        ["occurred_at"],
        unique=False,
        postgresql_where=sa.text("published_at IS NULL"),
    )

    op.create_table(
        "inbox_receipts",
        sa.Column("receipt_id", sa.Uuid(), nullable=False),
        sa.Column("consumer_name", sa.String(length=160), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(payload_hash) = 64", name="ck_inbox_receipts_hash_length"),
        sa.PrimaryKeyConstraint("receipt_id"),
        sa.UniqueConstraint("consumer_name", "message_id", name="uq_inbox_consumer_message"),
    )

    capability_gates = op.create_table(
        "capability_gates",
        sa.Column("capability_key", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.String(length=120), nullable=False),
        sa.Column("certificate_ref", sa.String(length=255), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('DISABLED', 'SHADOW', 'ENABLED')",
            name="ck_capability_gates_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_capability_gates_version_positive"),
        sa.PrimaryKeyConstraint("capability_key"),
    )

    seeded_at = datetime.now(UTC)
    op.bulk_insert(
        capability_gates,
        [
            {
                "capability_key": "LIVE_ORDER_SEND",
                "status": "DISABLED",
                "version": 1,
                "reason": "No execution certificate or real-money evidence",
                "policy_version": "WP-0001",
                "certificate_ref": None,
                "updated_at": seeded_at,
            },
            {
                "capability_key": "CAPITAL_TRANSFER",
                "status": "DISABLED",
                "version": 1,
                "reason": "Vault deployment facts and funding certificate are unavailable",
                "policy_version": "WP-0001",
                "certificate_ref": None,
                "updated_at": seeded_at,
            },
            {
                "capability_key": "AUTO_ADD",
                "status": "DISABLED",
                "version": 1,
                "reason": "Automatic Add is default-off and uncertified",
                "policy_version": "WP-0001",
                "certificate_ref": None,
                "updated_at": seeded_at,
            },
        ],
    )

    op.execute(
        """
        CREATE FUNCTION deny_immutable_audit_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_append_only
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION deny_immutable_audit_change()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS deny_immutable_audit_change()")
    op.drop_table("capability_gates")
    op.drop_table("inbox_receipts")
    op.drop_index("ix_outbox_unpublished", table_name="outbox_messages")
    op.drop_table("outbox_messages")
    op.drop_index("ix_audit_events_correlation", table_name="audit_events")
    op.drop_index("ix_audit_events_aggregate", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("command_receipts")
