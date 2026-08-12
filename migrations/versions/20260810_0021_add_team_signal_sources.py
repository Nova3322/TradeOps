"""Add team-scoped Perptape and signed Webhook signal sources.

Revision ID: 20260810_0021
Revises: 20260810_0020
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260810_0021"
down_revision: str | None = "20260810_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "team_signal_sources",
        sa.Column("signal_source_id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("credential_ciphertext", sa.Text(), nullable=True),
        sa.Column(
            "credential_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("webhook_max_age_seconds", sa.Integer(), nullable=False),
        sa.Column("service_principal_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "mode IN ('PERPTAPE','WEBHOOK')", name="ck_team_signal_sources_mode"
        ),
        sa.CheckConstraint("version >= 1", name="ck_team_signal_sources_version"),
        sa.CheckConstraint(
            "credential_version >= 0", name="ck_team_signal_sources_credential_version"
        ),
        sa.CheckConstraint(
            "(credential_ciphertext IS NULL AND credential_version = 0) OR "
            "(credential_ciphertext IS NOT NULL AND credential_version >= 1)",
            name="ck_team_signal_sources_credential_envelope",
        ),
        sa.CheckConstraint(
            "webhook_max_age_seconds BETWEEN 30 AND 900",
            name="ck_team_signal_sources_max_age",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.user_id"], name="fk_team_signal_sources_created_by"
        ),
        sa.ForeignKeyConstraint(
            ["service_principal_id"],
            ["users.user_id"],
            name="fk_team_signal_sources_service_principal",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.team_id"],
            name="fk_team_signal_sources_team",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.user_id"], name="fk_team_signal_sources_updated_by"
        ),
        sa.PrimaryKeyConstraint("signal_source_id", name="pk_team_signal_sources"),
        sa.UniqueConstraint("team_id", name="uq_team_signal_sources_team"),
        sa.UniqueConstraint(
            "team_id",
            "signal_source_id",
            name="uq_team_signal_sources_team_identity",
        ),
    )
    op.create_index(
        "ix_team_signal_sources_enabled_mode",
        "team_signal_sources",
        ["enabled", "mode"],
    )

    connection = op.get_bind()
    teams = connection.execute(
        sa.text(
            "SELECT team_id, created_by, created_at, updated_at "
            "FROM teams ORDER BY created_at, team_id"
        )
    ).mappings()
    for team in teams:
        connection.execute(
            sa.text(
                "INSERT INTO team_signal_sources "
                "(signal_source_id, team_id, mode, enabled, credential_ciphertext, "
                "credential_metadata, credential_version, webhook_max_age_seconds, "
                "service_principal_id, version, created_by, updated_by, created_at, updated_at) "
                "VALUES (:signal_source_id, :team_id, 'PERPTAPE', true, NULL, "
                "CAST(:metadata AS jsonb), 0, 300, NULL, 1, :created_by, :created_by, "
                ":created_at, :updated_at)"
            ),
            {
                "signal_source_id": uuid4(),
                "team_id": team["team_id"],
                "metadata": '{"credential_source":"RUNTIME_FALLBACK","key_hint":null}',
                "created_by": team["created_by"],
                "created_at": team["created_at"],
                "updated_at": team["updated_at"],
            },
        )

    op.create_table(
        "signal_events",
        sa.Column("signal_event_id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("signal_source_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=160), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("nonce", sa.String(length=160), nullable=False),
        sa.Column("payload_version", sa.Integer(), nullable=False),
        sa.Column("venue", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=120), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("strategy_id", sa.String(length=120), nullable=False),
        sa.Column("strategy_version", sa.String(length=120), nullable=False),
        sa.Column("timeframe", sa.String(length=32), nullable=True),
        sa.Column("reference_price", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "normalized_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("semantic_hash", sa.String(length=64), nullable=False),
        sa.Column("signature_version", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "provider IN ('TRADINGVIEW','MODEL')", name="ck_signal_events_provider"
        ),
        sa.CheckConstraint("direction IN ('LONG','SHORT')", name="ck_signal_events_direction"),
        sa.CheckConstraint(
            "venue IN ('BINANCE','HYPERLIQUID','OKX','BYBIT')",
            name="ck_signal_events_venue",
        ),
        sa.CheckConstraint(
            "status IN ('RECEIVED','PROPOSAL_CREATED')", name="ck_signal_events_status"
        ),
        sa.CheckConstraint("payload_version >= 1", name="ck_signal_events_payload_version"),
        sa.CheckConstraint(
            "reference_price IS NULL OR reference_price > 0",
            name="ck_signal_events_reference_price",
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "signal_source_id"],
            ["team_signal_sources.team_id", "team_signal_sources.signal_source_id"],
            name="fk_signal_events_team_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.team_id"],
            name="fk_signal_events_team",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("signal_event_id", name="pk_signal_events"),
        sa.UniqueConstraint(
            "signal_source_id", "nonce", name="uq_signal_events_source_nonce"
        ),
        sa.UniqueConstraint(
            "team_id", "idempotency_key", name="uq_signal_events_team_idempotency"
        ),
        sa.UniqueConstraint(
            "team_id", "signal_event_id", name="uq_signal_events_team_identity"
        ),
        sa.UniqueConstraint(
            "team_id",
            "provider",
            "external_id",
            name="uq_signal_events_team_provider_external",
        ),
    )
    op.create_index(
        "ix_signal_events_team_received",
        "signal_events",
        ["team_id", "received_at"],
    )

    op.add_column("proposals", sa.Column("signal_event_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_proposals_team_signal_event",
        "proposals",
        "signal_events",
        ["team_id", "signal_event_id"],
        ["team_id", "signal_event_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_proposals_signal_event",
        "proposals",
        ["team_id", "signal_event_id"],
        unique=True,
        postgresql_where=sa.text("signal_event_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_proposals_signal_event", table_name="proposals")
    op.drop_constraint("fk_proposals_team_signal_event", "proposals", type_="foreignkey")
    op.drop_column("proposals", "signal_event_id")
    op.drop_index("ix_signal_events_team_received", table_name="signal_events")
    op.drop_table("signal_events")
    op.drop_index("ix_team_signal_sources_enabled_mode", table_name="team_signal_sources")
    op.drop_table("team_signal_sources")
