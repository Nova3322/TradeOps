"""Add immutable SHADOW-only instrument catalog classification facts.

Revision ID: 20260718_0028
Revises: 20260718_0027
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0028"
down_revision: str | Sequence[str] | None = "20260718_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instrument_catalog_records",
        sa.Column("catalog_record_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("venue", sa.String(length=80), nullable=False),
        sa.Column("execution_domain", sa.String(length=120), nullable=False),
        sa.Column("native_instrument_id", sa.String(length=255), nullable=False),
        sa.Column("canonical_instrument_id", sa.String(length=255), nullable=False),
        sa.Column("display_symbol", sa.String(length=120), nullable=False),
        sa.Column("catalog_version", sa.String(length=120), nullable=False),
        sa.Column("metadata_version", sa.String(length=120), nullable=False),
        sa.Column("classification_version", sa.String(length=120), nullable=False),
        sa.Column("contract_type", sa.String(length=40), nullable=False),
        sa.Column("underlying_id", sa.String(length=160), nullable=False),
        sa.Column("sector", sa.String(length=80), nullable=False),
        sa.Column(
            "risk_cluster_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("quote_asset", sa.String(length=80), nullable=False),
        sa.Column("settlement_asset", sa.String(length=80), nullable=False),
        sa.Column("collateral_asset", sa.String(length=80), nullable=False),
        sa.Column("contract_multiplier", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("tick_size", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("lot_size", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("minimum_quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("minimum_notional", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("discoverable", sa.Boolean(), nullable=False),
        sa.Column("classification_complete", sa.Boolean(), nullable=False),
        sa.Column("approval_scope", sa.String(length=20), nullable=False),
        sa.Column("listing_status", sa.String(length=32), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("real_funds_eligible", sa.Boolean(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_observed_at", sa.DateTime(timezone=True), nullable=False),
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
            name="ck_instrument_catalog_records_shadow_only",
        ),
        sa.CheckConstraint(
            "contract_type = 'PERPETUAL'",
            name="ck_instrument_catalog_records_contract_type",
        ),
        sa.CheckConstraint(
            "approval_scope IN ('NONE', 'OBSERVE', 'RESEARCH')",
            name="ck_instrument_catalog_records_approval_scope",
        ),
        sa.CheckConstraint(
            "listing_status IN ('TRADING', 'REDUCE_ONLY', 'HALTED', 'DELISTING', 'RETIRED')",
            name="ck_instrument_catalog_records_listing_status",
        ),
        sa.CheckConstraint(
            "sector IN ('CRYPTO', 'EQUITY_INDEX', 'PRECIOUS_METALS', 'COMMODITY', 'UNCLASSIFIED')",
            name="ck_instrument_catalog_records_sector",
        ),
        sa.CheckConstraint(
            "contract_multiplier > 0 AND tick_size > 0 AND lot_size > 0 "
            "AND minimum_quantity > 0 AND minimum_notional >= 0",
            name="ck_instrument_catalog_records_positive_rules",
        ),
        sa.CheckConstraint(
            "valid_until > valid_from AND source_observed_at <= valid_from",
            name="ck_instrument_catalog_records_valid_window",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(risk_cluster_ids) = 'array' AND jsonb_array_length(risk_cluster_ids) > 0",
            name="ck_instrument_catalog_records_risk_clusters",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) > 0",
            name="ck_instrument_catalog_records_evidence",
        ),
        sa.CheckConstraint(
            "classification_complete = false OR sector <> 'UNCLASSIFIED'",
            name="ck_instrument_catalog_records_complete_classification",
        ),
        sa.CheckConstraint(
            "length(record_hash) = 64 AND length(evidence_hash) = 64",
            name="ck_instrument_catalog_records_hashes",
        ),
        sa.PrimaryKeyConstraint("catalog_record_id"),
        sa.UniqueConstraint(
            "organization_id",
            "venue",
            "execution_domain",
            "canonical_instrument_id",
            "catalog_version",
            "classification_version",
            name="uq_instrument_catalog_records_exact_version",
        ),
        sa.UniqueConstraint(
            "catalog_record_id",
            "organization_id",
            "catalog_version",
            "classification_version",
            name="uq_instrument_catalog_records_identity_binding",
        ),
    )
    op.create_index(
        "ix_instrument_catalog_records_lookup",
        "instrument_catalog_records",
        [
            "organization_id",
            "venue",
            "execution_domain",
            "canonical_instrument_id",
            "catalog_version",
            "classification_version",
        ],
    )
    op.execute(
        """
        CREATE FUNCTION protect_instrument_catalog_record_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            canonical_clusters jsonb;
            canonical_evidence jsonb;
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM jsonb_array_elements(NEW.risk_cluster_ids) AS entry(value)
                WHERE jsonb_typeof(value) <> 'string'
                   OR length(trim(both '"' from value::text)) = 0
            ) THEN
                RAISE EXCEPTION 'instrument risk cluster identifiers are invalid';
            END IF;
            IF (
                SELECT count(DISTINCT value)
                FROM jsonb_array_elements(NEW.risk_cluster_ids) AS entry(value)
            ) <> jsonb_array_length(NEW.risk_cluster_ids) THEN
                RAISE EXCEPTION 'instrument risk cluster identifiers must be unique';
            END IF;
            SELECT jsonb_agg(value ORDER BY value)
            INTO canonical_clusters
            FROM jsonb_array_elements(NEW.risk_cluster_ids) AS entry(value);
            IF NEW.risk_cluster_ids <> canonical_clusters THEN
                RAISE EXCEPTION 'instrument risk cluster identifiers are not canonical';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM jsonb_array_elements(NEW.evidence_refs) AS entry(value)
                WHERE jsonb_typeof(value) <> 'string'
                   OR length(trim(both '"' from value::text)) = 0
            ) THEN
                RAISE EXCEPTION 'instrument catalog evidence references are invalid';
            END IF;
            IF (
                SELECT count(DISTINCT value)
                FROM jsonb_array_elements(NEW.evidence_refs) AS entry(value)
            ) <> jsonb_array_length(NEW.evidence_refs) THEN
                RAISE EXCEPTION 'instrument catalog evidence references must be unique';
            END IF;
            SELECT jsonb_agg(value ORDER BY value)
            INTO canonical_evidence
            FROM jsonb_array_elements(NEW.evidence_refs) AS entry(value);
            IF NEW.evidence_refs <> canonical_evidence THEN
                RAISE EXCEPTION 'instrument catalog evidence references are not canonical';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER instrument_catalog_records_insert_guard
        BEFORE INSERT ON instrument_catalog_records
        FOR EACH ROW EXECUTE FUNCTION protect_instrument_catalog_record_insert()
        """
    )
    op.execute(
        """
        CREATE FUNCTION deny_instrument_catalog_record_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'instrument_catalog_records is immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER instrument_catalog_records_immutable
        BEFORE UPDATE OR DELETE ON instrument_catalog_records
        FOR EACH ROW EXECUTE FUNCTION deny_instrument_catalog_record_change()
        """
    )

    for table_name, prefix in (
        ("risk_decision_snapshots", "risk_decisions"),
        ("execution_risk_decisions", "exec_risk"),
    ):
        op.add_column(table_name, sa.Column("catalog_record_id", sa.Uuid(), nullable=True))
        op.add_column(
            table_name,
            sa.Column("catalog_version", sa.String(length=120), nullable=True),
        )
        op.add_column(
            table_name,
            sa.Column("catalog_classification_version", sa.String(length=120), nullable=True),
        )
        op.add_column(
            table_name,
            sa.Column("catalog_record_hash", sa.String(length=64), nullable=True),
        )
        op.create_check_constraint(
            f"ck_{prefix}_catalog_binding_integrity",
            table_name,
            "(catalog_record_id IS NULL AND catalog_version IS NULL "
            "AND catalog_classification_version IS NULL AND catalog_record_hash IS NULL) OR "
            "(catalog_record_id IS NOT NULL AND catalog_version IS NOT NULL "
            "AND catalog_classification_version IS NOT NULL "
            "AND length(catalog_record_hash) = 64)",
        )
        op.create_foreign_key(
            f"fk_{prefix}_instrument_catalog",
            table_name,
            "instrument_catalog_records",
            [
                "catalog_record_id",
                "organization_id",
                "catalog_version",
                "catalog_classification_version",
            ],
            [
                "catalog_record_id",
                "organization_id",
                "catalog_version",
                "classification_version",
            ],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM instrument_catalog_records) THEN
                RAISE EXCEPTION
                    'cannot downgrade instrument catalog classification while facts remain';
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
            f"fk_{prefix}_instrument_catalog",
            table_name,
            type_="foreignkey",
        )
        op.drop_constraint(
            f"ck_{prefix}_catalog_binding_integrity",
            table_name,
            type_="check",
        )
        op.drop_column(table_name, "catalog_record_hash")
        op.drop_column(table_name, "catalog_classification_version")
        op.drop_column(table_name, "catalog_version")
        op.drop_column(table_name, "catalog_record_id")
    op.execute(
        "DROP TRIGGER IF EXISTS instrument_catalog_records_immutable ON instrument_catalog_records"
    )
    op.execute("DROP FUNCTION IF EXISTS deny_instrument_catalog_record_change()")
    op.execute(
        "DROP TRIGGER IF EXISTS instrument_catalog_records_insert_guard "
        "ON instrument_catalog_records"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_instrument_catalog_record_insert()")
    op.drop_index(
        "ix_instrument_catalog_records_lookup",
        table_name="instrument_catalog_records",
    )
    op.drop_table("instrument_catalog_records")
