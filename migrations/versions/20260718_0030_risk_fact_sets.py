"""Add immutable complete SHADOW-only risk fact sets.

Revision ID: 20260718_0030
Revises: 20260718_0029
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0030"
down_revision: str | Sequence[str] | None = "20260718_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "risk_fact_sets",
        sa.Column("risk_fact_set_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("venue", sa.String(length=80), nullable=False),
        sa.Column("execution_domain", sa.String(length=120), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=False),
        sa.Column("canonical_instrument_id", sa.String(length=255), nullable=False),
        sa.Column("position_mode", sa.String(length=80), nullable=False),
        sa.Column("margin_mode", sa.String(length=80), nullable=False),
        sa.Column("collateral_pool_id", sa.String(length=160), nullable=False),
        sa.Column("fact_set_version", sa.String(length=120), nullable=False),
        sa.Column(
            "observations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("real_funds_eligible", sa.Boolean(), nullable=False),
        sa.Column("assembled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("record_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "evidence_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "environment = 'SHADOW' AND real_funds_eligible = false",
            name="ck_risk_fact_sets_shadow_only",
        ),
        sa.CheckConstraint(
            "valid_until > valid_from AND valid_from >= assembled_at",
            name="ck_risk_fact_sets_valid_window",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(observations) = 'array' AND jsonb_array_length(observations) = 9",
            name="ck_risk_fact_sets_complete_observations",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) > 0",
            name="ck_risk_fact_sets_evidence",
        ),
        sa.CheckConstraint(
            "length(record_hash) = 64 AND length(evidence_hash) = 64",
            name="ck_risk_fact_sets_hashes",
        ),
        sa.PrimaryKeyConstraint("risk_fact_set_id"),
        sa.UniqueConstraint(
            "organization_id",
            "venue",
            "execution_domain",
            "account_id",
            "canonical_instrument_id",
            "position_mode",
            "margin_mode",
            "collateral_pool_id",
            "fact_set_version",
            name="uq_risk_fact_sets_exact_version",
        ),
        sa.UniqueConstraint(
            "risk_fact_set_id",
            "organization_id",
            "fact_set_version",
            name="uq_risk_fact_sets_identity_binding",
        ),
    )
    op.create_index(
        "ix_risk_fact_sets_latest_scope",
        "risk_fact_sets",
        [
            "organization_id",
            "venue",
            "execution_domain",
            "account_id",
            "canonical_instrument_id",
            "position_mode",
            "margin_mode",
            "collateral_pool_id",
            "assembled_at",
        ],
    )
    op.execute(
        """
        CREATE FUNCTION protect_risk_fact_set_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            canonical_values jsonb;
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM jsonb_array_elements(NEW.observations) AS entry(value)
                WHERE jsonb_typeof(value) <> 'object'
                   OR value->>'fact_type' NOT IN (
                       'ACCOUNT', 'CATALOG', 'LEDGER', 'MARKET', 'ORDERS',
                       'POSITIONS', 'PROTECTION', 'VAULT', 'VENUE_CAPABILITY'
                   )
                   OR value->>'status' NOT IN ('KNOWN', 'UNKNOWN')
                   OR coalesce(length(value->>'source_ref'), 0) = 0
                   OR coalesce(length(value->>'source_version'), 0) = 0
                   OR coalesce(value->>'payload_hash', '') !~ '^[0-9a-f]{64}$'
                   OR coalesce(length(value->>'event_time'), 0) = 0
                   OR coalesce(length(value->>'received_at'), 0) = 0
                   OR (value->>'received_at')::timestamptz
                      < (value->>'event_time')::timestamptz
            ) THEN
                RAISE EXCEPTION 'risk fact set observations are invalid';
            END IF;
            IF (
                SELECT count(DISTINCT value->>'fact_type')
                FROM jsonb_array_elements(NEW.observations) AS entry(value)
            ) <> 9 THEN
                RAISE EXCEPTION 'risk fact set observations are incomplete';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM jsonb_array_elements(NEW.observations) AS entry(value)
                WHERE (value->>'received_at')::timestamptz > NEW.assembled_at
            ) THEN
                RAISE EXCEPTION 'risk fact set precedes an observation';
            END IF;
            SELECT jsonb_agg(value ORDER BY value->>'fact_type')
            INTO canonical_values
            FROM jsonb_array_elements(NEW.observations) AS entry(value);
            IF NEW.observations <> canonical_values THEN
                RAISE EXCEPTION 'risk fact set observations are not canonical';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM jsonb_array_elements(NEW.evidence_refs) AS entry(value)
                WHERE jsonb_typeof(value) <> 'string'
                   OR length(trim(both '"' from value::text)) = 0
            ) THEN
                RAISE EXCEPTION 'risk fact set evidence references are invalid';
            END IF;
            IF (
                SELECT count(DISTINCT value)
                FROM jsonb_array_elements(NEW.evidence_refs) AS entry(value)
            ) <> jsonb_array_length(NEW.evidence_refs) THEN
                RAISE EXCEPTION 'risk fact set evidence references must be unique';
            END IF;
            SELECT jsonb_agg(value ORDER BY value)
            INTO canonical_values
            FROM jsonb_array_elements(NEW.evidence_refs) AS entry(value);
            IF NEW.evidence_refs <> canonical_values THEN
                RAISE EXCEPTION 'risk fact set evidence references are not canonical';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER risk_fact_sets_insert_guard
        BEFORE INSERT ON risk_fact_sets
        FOR EACH ROW EXECUTE FUNCTION protect_risk_fact_set_insert()
        """
    )
    op.execute(
        """
        CREATE FUNCTION deny_risk_fact_set_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'risk_fact_sets is immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER risk_fact_sets_immutable
        BEFORE UPDATE OR DELETE ON risk_fact_sets
        FOR EACH ROW EXECUTE FUNCTION deny_risk_fact_set_change()
        """
    )

    for table_name, prefix in (
        ("risk_decision_snapshots", "risk_decisions"),
        ("execution_risk_decisions", "exec_risk"),
    ):
        op.add_column(
            table_name,
            sa.Column("risk_fact_set_id", sa.Uuid(), nullable=True),
        )
        op.add_column(
            table_name,
            sa.Column("risk_fact_set_version", sa.String(length=120), nullable=True),
        )
        op.add_column(
            table_name,
            sa.Column("risk_fact_set_record_hash", sa.String(length=64), nullable=True),
        )
        op.create_check_constraint(
            f"ck_{prefix}_risk_fact_set_binding",
            table_name,
            "(risk_fact_set_id IS NULL AND risk_fact_set_version IS NULL "
            "AND risk_fact_set_record_hash IS NULL) OR "
            "(risk_fact_set_id IS NOT NULL AND risk_fact_set_version IS NOT NULL "
            "AND length(risk_fact_set_record_hash) = 64)",
        )
        op.create_foreign_key(
            f"fk_{prefix}_risk_fact_set",
            table_name,
            "risk_fact_sets",
            ["risk_fact_set_id", "organization_id", "risk_fact_set_version"],
            ["risk_fact_set_id", "organization_id", "fact_set_version"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM risk_fact_sets) THEN
                RAISE EXCEPTION 'cannot downgrade risk fact sets while facts remain';
            END IF;
        END;
        $$
        """
    )
    for table_name, prefix in (
        ("execution_risk_decisions", "exec_risk"),
        ("risk_decision_snapshots", "risk_decisions"),
    ):
        op.drop_constraint(
            f"fk_{prefix}_risk_fact_set",
            table_name,
            type_="foreignkey",
        )
        op.drop_constraint(
            f"ck_{prefix}_risk_fact_set_binding",
            table_name,
            type_="check",
        )
        op.drop_column(table_name, "risk_fact_set_record_hash")
        op.drop_column(table_name, "risk_fact_set_version")
        op.drop_column(table_name, "risk_fact_set_id")
    op.execute("DROP TRIGGER IF EXISTS risk_fact_sets_immutable ON risk_fact_sets")
    op.execute("DROP FUNCTION IF EXISTS deny_risk_fact_set_change()")
    op.execute("DROP TRIGGER IF EXISTS risk_fact_sets_insert_guard ON risk_fact_sets")
    op.execute("DROP FUNCTION IF EXISTS protect_risk_fact_set_insert()")
    op.drop_index("ix_risk_fact_sets_latest_scope", table_name="risk_fact_sets")
    op.drop_table("risk_fact_sets")
