"""Add immutable SHADOW-only strategy evaluations for ADD.

Revision ID: 20260718_0031
Revises: 20260718_0030
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0031"
down_revision: str | Sequence[str] | None = "20260718_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "strategy_evaluation_records",
        sa.Column("strategy_evaluation_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("strategy_id", sa.String(length=160), nullable=False),
        sa.Column("strategy_version", sa.String(length=120), nullable=False),
        sa.Column("strategy_parameter_version", sa.String(length=120), nullable=False),
        sa.Column("venue", sa.String(length=80), nullable=False),
        sa.Column("execution_domain", sa.String(length=120), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=False),
        sa.Column("canonical_instrument_id", sa.String(length=255), nullable=False),
        sa.Column("position_mode", sa.String(length=80), nullable=False),
        sa.Column("margin_mode", sa.String(length=80), nullable=False),
        sa.Column("collateral_pool_id", sa.String(length=160), nullable=False),
        sa.Column("evaluation_version", sa.String(length=120), nullable=False),
        sa.Column("evaluation_kind", sa.String(length=40), nullable=False),
        sa.Column(
            "rule_results",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("risk_fact_set_id", sa.Uuid(), nullable=False),
        sa.Column("risk_fact_set_version", sa.String(length=120), nullable=False),
        sa.Column("risk_fact_set_record_hash", sa.String(length=64), nullable=False),
        sa.Column("position_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("position_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("protection_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("protection_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("evaluator_version", sa.String(length=120), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("real_funds_eligible", sa.Boolean(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
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
            "evaluation_kind = 'ADD_CONTINUATION'",
            name="ck_strategy_evaluations_kind",
        ),
        sa.CheckConstraint(
            "outcome IN ('PASS', 'FAIL', 'UNKNOWN')",
            name="ck_strategy_evaluations_outcome",
        ),
        sa.CheckConstraint(
            "environment = 'SHADOW' AND real_funds_eligible = false",
            name="ck_strategy_evaluations_shadow_only",
        ),
        sa.CheckConstraint(
            "valid_until > valid_from AND valid_from >= evaluated_at",
            name="ck_strategy_evaluations_valid_window",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(rule_results) = 'array' AND jsonb_array_length(rule_results) = 3",
            name="ck_strategy_evaluations_complete_rules",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) > 0",
            name="ck_strategy_evaluations_evidence",
        ),
        sa.CheckConstraint(
            "length(risk_fact_set_record_hash) = 64 "
            "AND length(position_snapshot_hash) = 64 "
            "AND length(protection_snapshot_hash) = 64 "
            "AND length(record_hash) = 64 AND length(evidence_hash) = 64",
            name="ck_strategy_evaluations_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.campaign_id"],
            name="fk_strategy_evaluations_campaign_id_campaigns",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["risk_fact_set_id", "organization_id", "risk_fact_set_version"],
            [
                "risk_fact_sets.risk_fact_set_id",
                "risk_fact_sets.organization_id",
                "risk_fact_sets.fact_set_version",
            ],
            name="fk_strategy_evaluations_risk_fact_set",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["position_snapshot_id"],
            ["venue_position_snapshots.venue_position_snapshot_id"],
            name="fk_strategy_evaluations_position_snapshot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["protection_snapshot_id"],
            ["venue_protection_snapshots.venue_protection_snapshot_id"],
            name="fk_strategy_evaluations_protection_snapshot",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("strategy_evaluation_id"),
        sa.UniqueConstraint(
            "campaign_id",
            "evaluation_version",
            name="uq_strategy_evaluations_campaign_version",
        ),
        sa.UniqueConstraint(
            "strategy_evaluation_id",
            "campaign_id",
            "evaluation_version",
            name="uq_strategy_evaluations_identity_binding",
        ),
    )
    op.create_index(
        "ix_strategy_evaluations_latest_campaign",
        "strategy_evaluation_records",
        ["campaign_id", "evaluated_at"],
    )
    op.execute(
        """
        CREATE FUNCTION protect_strategy_evaluation_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            canonical_values jsonb;
            expected_outcome text;
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM jsonb_array_elements(NEW.rule_results) AS entry(value)
                WHERE jsonb_typeof(value) <> 'object'
                   OR value->>'rule_id' NOT IN (
                       'PULLBACK_ENTRY', 'STRATEGY_VALIDITY', 'TREND_CONTINUATION'
                   )
                   OR value->>'status' NOT IN ('PASS', 'FAIL', 'UNKNOWN')
                   OR coalesce(length(value->>'reason_code'), 0) = 0
                   OR coalesce(value->>'evidence_payload_hash', '') !~ '^[0-9a-f]{64}$'
            ) THEN
                RAISE EXCEPTION 'strategy evaluation rule results are invalid';
            END IF;
            IF (
                SELECT count(DISTINCT value->>'rule_id')
                FROM jsonb_array_elements(NEW.rule_results) AS entry(value)
            ) <> 3 THEN
                RAISE EXCEPTION 'strategy evaluation rule results are incomplete';
            END IF;
            SELECT jsonb_agg(value ORDER BY value->>'rule_id')
            INTO canonical_values
            FROM jsonb_array_elements(NEW.rule_results) AS entry(value);
            IF NEW.rule_results <> canonical_values THEN
                RAISE EXCEPTION 'strategy evaluation rule results are not canonical';
            END IF;

            IF EXISTS (
                SELECT 1 FROM jsonb_array_elements(NEW.rule_results) AS entry(value)
                WHERE value->>'status' = 'FAIL'
            ) THEN
                expected_outcome := 'FAIL';
            ELSIF EXISTS (
                SELECT 1 FROM jsonb_array_elements(NEW.rule_results) AS entry(value)
                WHERE value->>'status' = 'UNKNOWN'
            ) THEN
                expected_outcome := 'UNKNOWN';
            ELSE
                expected_outcome := 'PASS';
            END IF;
            IF NEW.outcome <> expected_outcome THEN
                RAISE EXCEPTION 'strategy evaluation outcome is inconsistent';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM jsonb_array_elements(NEW.evidence_refs) AS entry(value)
                WHERE jsonb_typeof(value) <> 'string'
                   OR length(trim(both '"' from value::text)) = 0
            ) THEN
                RAISE EXCEPTION 'strategy evaluation evidence references are invalid';
            END IF;
            IF (
                SELECT count(DISTINCT value)
                FROM jsonb_array_elements(NEW.evidence_refs) AS entry(value)
            ) <> jsonb_array_length(NEW.evidence_refs) THEN
                RAISE EXCEPTION 'strategy evaluation evidence references must be unique';
            END IF;
            SELECT jsonb_agg(value ORDER BY value)
            INTO canonical_values
            FROM jsonb_array_elements(NEW.evidence_refs) AS entry(value);
            IF NEW.evidence_refs <> canonical_values THEN
                RAISE EXCEPTION 'strategy evaluation evidence references are not canonical';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER strategy_evaluations_insert_guard
        BEFORE INSERT ON strategy_evaluation_records
        FOR EACH ROW EXECUTE FUNCTION protect_strategy_evaluation_insert()
        """
    )
    op.execute(
        """
        CREATE FUNCTION deny_strategy_evaluation_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'strategy_evaluation_records is immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER strategy_evaluations_immutable
        BEFORE UPDATE OR DELETE ON strategy_evaluation_records
        FOR EACH ROW EXECUTE FUNCTION deny_strategy_evaluation_change()
        """
    )

    op.add_column(
        "execution_risk_decisions",
        sa.Column("strategy_evaluation_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "execution_risk_decisions",
        sa.Column("strategy_evaluation_version", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "execution_risk_decisions",
        sa.Column("strategy_evaluation_record_hash", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_exec_risk_strategy_evaluation_binding",
        "execution_risk_decisions",
        "(strategy_evaluation_id IS NULL AND strategy_evaluation_version IS NULL "
        "AND strategy_evaluation_record_hash IS NULL) OR "
        "(strategy_evaluation_id IS NOT NULL AND strategy_evaluation_version IS NOT NULL "
        "AND length(strategy_evaluation_record_hash) = 64)",
    )
    op.create_foreign_key(
        "fk_exec_risk_strategy_evaluation",
        "execution_risk_decisions",
        "strategy_evaluation_records",
        ["strategy_evaluation_id", "campaign_id", "strategy_evaluation_version"],
        ["strategy_evaluation_id", "campaign_id", "evaluation_version"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM strategy_evaluation_records) THEN
                RAISE EXCEPTION 'cannot downgrade strategy evaluations while records remain';
            END IF;
        END;
        $$
        """
    )
    op.drop_constraint(
        "fk_exec_risk_strategy_evaluation",
        "execution_risk_decisions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_exec_risk_strategy_evaluation_binding",
        "execution_risk_decisions",
        type_="check",
    )
    op.drop_column("execution_risk_decisions", "strategy_evaluation_record_hash")
    op.drop_column("execution_risk_decisions", "strategy_evaluation_version")
    op.drop_column("execution_risk_decisions", "strategy_evaluation_id")
    op.execute(
        "DROP TRIGGER IF EXISTS strategy_evaluations_immutable ON strategy_evaluation_records"
    )
    op.execute("DROP FUNCTION IF EXISTS deny_strategy_evaluation_change()")
    op.execute(
        "DROP TRIGGER IF EXISTS strategy_evaluations_insert_guard ON strategy_evaluation_records"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_strategy_evaluation_insert()")
    op.drop_index(
        "ix_strategy_evaluations_latest_campaign",
        table_name="strategy_evaluation_records",
    )
    op.drop_table("strategy_evaluation_records")
