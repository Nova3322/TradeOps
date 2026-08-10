"""Add team notification routes and durable deliveries.

Revision ID: 20260810_0023
Revises: 20260810_0022
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260810_0023"
down_revision: str | None = "20260810_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_routes",
        sa.Column("notification_route_id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("event_types", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("configuration_ciphertext", sa.Text(), nullable=False),
        sa.Column(
            "configuration_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "channel IN ('TELEGRAM','SLACK','LARK','EMAIL')",
            name="ck_notification_routes_channel",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(event_types) = 'array'",
            name="ck_notification_routes_events",
        ),
        sa.CheckConstraint("version >= 1", name="ck_notification_routes_version"),
        sa.CheckConstraint(
            "credential_version >= 1",
            name="ck_notification_routes_credential_version",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.user_id"], name="fk_notification_routes_created_by"
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.team_id"],
            name="fk_notification_routes_team",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.user_id"], name="fk_notification_routes_updated_by"
        ),
        sa.PrimaryKeyConstraint("notification_route_id", name="pk_notification_routes"),
        sa.UniqueConstraint("team_id", "name", name="uq_notification_routes_team_name"),
        sa.UniqueConstraint(
            "team_id",
            "notification_route_id",
            name="uq_notification_routes_team_identity",
        ),
    )
    op.create_index(
        "ix_notification_routes_team_enabled",
        "notification_routes",
        ["team_id", "enabled"],
    )

    op.create_table(
        "notification_deliveries",
        sa.Column("notification_delivery_id", sa.Uuid(), nullable=False),
        sa.Column("notification_event_id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("notification_route_id", sa.Uuid(), nullable=False),
        sa.Column("route_version", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column("template_key", sa.String(length=120), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("semantic_hash", sa.String(length=64), nullable=False),
        sa.Column("object_type", sa.String(length=120), nullable=False),
        sa.Column("object_id", sa.String(length=255), nullable=False),
        sa.Column("object_version", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=16), nullable=True),
        sa.Column("account_id", sa.String(length=120), nullable=True),
        sa.Column("venue", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("external_delivery_id", sa.String(length=255), nullable=True),
        sa.Column("last_error_code", sa.String(length=120), nullable=True),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "channel IN ('TELEGRAM','SLACK','LARK','EMAIL')",
            name="ck_notification_deliveries_channel",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','RETRY_WAIT','SENDING','SENT','DEAD_LETTER',"
            "'OUTCOME_UNKNOWN','CANCELLED')",
            name="ck_notification_deliveries_status",
        ),
        sa.CheckConstraint("template_version >= 1", name="ck_notification_deliveries_template"),
        sa.CheckConstraint("route_version >= 1", name="ck_notification_deliveries_route_version"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_notification_deliveries_attempt_count"),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 10",
            name="ck_notification_deliveries_max_attempts",
        ),
        sa.CheckConstraint(
            "length(semantic_hash) = 64",
            name="ck_notification_deliveries_semantic_hash",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.team_id"],
            name="fk_notification_deliveries_team",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "notification_route_id"],
            ["notification_routes.team_id", "notification_routes.notification_route_id"],
            name="fk_notification_deliveries_team_route",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("notification_delivery_id", name="pk_notification_deliveries"),
        sa.UniqueConstraint(
            "notification_route_id",
            "notification_event_id",
            name="uq_notification_deliveries_route_event",
        ),
    )
    op.create_index(
        "ix_notification_deliveries_due",
        "notification_deliveries",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_notification_deliveries_team_created",
        "notification_deliveries",
        ["team_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_deliveries_team_created",
        table_name="notification_deliveries",
    )
    op.drop_index("ix_notification_deliveries_due", table_name="notification_deliveries")
    op.drop_table("notification_deliveries")
    op.drop_index(
        "ix_notification_routes_team_enabled",
        table_name="notification_routes",
    )
    op.drop_table("notification_routes")
