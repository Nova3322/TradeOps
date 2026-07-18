"""Add immutable SHADOW-only native protection capability facts.

Revision ID: 20260718_0029
Revises: 20260718_0028
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0029"
down_revision: str | Sequence[str] | None = "20260718_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instrument_protection_capability_records",
        sa.Column("protection_capability_record_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("catalog_record_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_version", sa.String(length=120), nullable=False),
        sa.Column("classification_version", sa.String(length=120), nullable=False),
        sa.Column("catalog_record_hash", sa.String(length=64), nullable=False),
        sa.Column("venue", sa.String(length=80), nullable=False),
        sa.Column("execution_domain", sa.String(length=120), nullable=False),
        sa.Column("canonical_instrument_id", sa.String(length=255), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=False),
        sa.Column("account_abstraction", sa.String(length=80), nullable=False),
        sa.Column("position_mode", sa.String(length=80), nullable=False),
        sa.Column("margin_mode", sa.String(length=80), nullable=False),
        sa.Column("collateral_scope", sa.String(length=120), nullable=False),
        sa.Column("collateral_pool_id", sa.String(length=160), nullable=False),
        sa.Column(
            "position_management_template_version",
            sa.String(length=120),
            nullable=False,
        ),
        sa.Column("execution_capability_version", sa.String(length=120), nullable=False),
        sa.Column("adapter_version", sa.String(length=120), nullable=False),
        sa.Column("worker_id", sa.String(length=160), nullable=False),
        sa.Column("worker_config_hash", sa.String(length=64), nullable=False),
        sa.Column("credential_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("freqtrade_worker_version", sa.String(length=120), nullable=False),
        sa.Column("account_capability_version", sa.String(length=120), nullable=False),
        sa.Column(
            "credential_permission_profile_version",
            sa.String(length=120),
            nullable=False,
        ),
        sa.Column("venue_client_version", sa.String(length=120), nullable=False),
        sa.Column("capability_status", sa.String(length=32), nullable=False),
        sa.Column("native_protection_supported", sa.Boolean(), nullable=False),
        sa.Column("conditional_orders_supported", sa.Boolean(), nullable=False),
        sa.Column("reduce_only_supported", sa.Boolean(), nullable=False),
        sa.Column("partial_fill_protection_supported", sa.Boolean(), nullable=False),
        sa.Column("protection_replacement_supported", sa.Boolean(), nullable=False),
        sa.Column("protection_confirmation_window_ms", sa.Integer(), nullable=False),
        sa.Column(
            "supported_trigger_price_types",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "supported_protection_order_types",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
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
            name="ck_instrument_protection_capabilities_shadow_only",
        ),
        sa.CheckConstraint(
            "capability_status IN "
            "('CERTIFIED', 'VALIDATING', 'NOT_SUPPORTED', 'UNKNOWN', 'EXPIRED')",
            name="ck_instrument_protection_capabilities_status",
        ),
        sa.CheckConstraint(
            "protection_confirmation_window_ms > 0",
            name="ck_instrument_protection_capabilities_positive_window",
        ),
        sa.CheckConstraint(
            "valid_until > valid_from AND source_observed_at <= valid_from",
            name="ck_instrument_protection_capabilities_valid_window",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(supported_trigger_price_types) = 'array' "
            "AND jsonb_array_length(supported_trigger_price_types) > 0",
            name="ck_instrument_protection_capabilities_trigger_types",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(supported_protection_order_types) = 'array' "
            "AND jsonb_array_length(supported_protection_order_types) > 0",
            name="ck_instrument_protection_capabilities_order_types",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) > 0",
            name="ck_instrument_protection_capabilities_evidence",
        ),
        sa.CheckConstraint(
            "length(catalog_record_hash) = 64 "
            "AND length(worker_config_hash) = 64 "
            "AND length(credential_fingerprint) = 64 "
            "AND length(record_hash) = 64 AND length(evidence_hash) = 64",
            name="ck_instrument_protection_capabilities_hashes",
        ),
        sa.ForeignKeyConstraint(
            [
                "catalog_record_id",
                "organization_id",
                "catalog_version",
                "classification_version",
            ],
            [
                "instrument_catalog_records.catalog_record_id",
                "instrument_catalog_records.organization_id",
                "instrument_catalog_records.catalog_version",
                "instrument_catalog_records.classification_version",
            ],
            name="fk_instrument_protection_capabilities_catalog",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("protection_capability_record_id"),
        sa.UniqueConstraint(
            "organization_id",
            "venue",
            "execution_domain",
            "canonical_instrument_id",
            "account_id",
            "position_mode",
            "margin_mode",
            "collateral_pool_id",
            "catalog_version",
            "classification_version",
            "position_management_template_version",
            name="uq_instrument_protection_capabilities_exact_version",
        ),
        sa.UniqueConstraint(
            "protection_capability_record_id",
            "organization_id",
            "position_management_template_version",
            name="uq_instrument_protection_capabilities_identity_binding",
        ),
    )
    op.create_index(
        "ix_instrument_protection_capabilities_lookup",
        "instrument_protection_capability_records",
        [
            "organization_id",
            "venue",
            "execution_domain",
            "canonical_instrument_id",
            "account_id",
            "position_mode",
            "margin_mode",
            "collateral_pool_id",
            "position_management_template_version",
        ],
    )
    op.execute(
        """
        CREATE FUNCTION protect_instrument_protection_capability_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            canonical_values jsonb;
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM jsonb_array_elements(NEW.supported_trigger_price_types) AS entry(value)
                WHERE jsonb_typeof(value) <> 'string'
                   OR length(trim(both '"' from value::text)) = 0
            ) THEN
                RAISE EXCEPTION 'protection trigger price types are invalid';
            END IF;
            IF (
                SELECT count(DISTINCT value)
                FROM jsonb_array_elements(NEW.supported_trigger_price_types) AS entry(value)
            ) <> jsonb_array_length(NEW.supported_trigger_price_types) THEN
                RAISE EXCEPTION 'protection trigger price types must be unique';
            END IF;
            SELECT jsonb_agg(value ORDER BY value)
            INTO canonical_values
            FROM jsonb_array_elements(NEW.supported_trigger_price_types) AS entry(value);
            IF NEW.supported_trigger_price_types <> canonical_values THEN
                RAISE EXCEPTION 'protection trigger price types are not canonical';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM jsonb_array_elements(NEW.supported_protection_order_types) AS entry(value)
                WHERE jsonb_typeof(value) <> 'string'
                   OR length(trim(both '"' from value::text)) = 0
            ) THEN
                RAISE EXCEPTION 'protection order types are invalid';
            END IF;
            IF (
                SELECT count(DISTINCT value)
                FROM jsonb_array_elements(NEW.supported_protection_order_types) AS entry(value)
            ) <> jsonb_array_length(NEW.supported_protection_order_types) THEN
                RAISE EXCEPTION 'protection order types must be unique';
            END IF;
            SELECT jsonb_agg(value ORDER BY value)
            INTO canonical_values
            FROM jsonb_array_elements(NEW.supported_protection_order_types) AS entry(value);
            IF NEW.supported_protection_order_types <> canonical_values THEN
                RAISE EXCEPTION 'protection order types are not canonical';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM jsonb_array_elements(NEW.evidence_refs) AS entry(value)
                WHERE jsonb_typeof(value) <> 'string'
                   OR length(trim(both '"' from value::text)) = 0
            ) THEN
                RAISE EXCEPTION 'protection capability evidence references are invalid';
            END IF;
            IF (
                SELECT count(DISTINCT value)
                FROM jsonb_array_elements(NEW.evidence_refs) AS entry(value)
            ) <> jsonb_array_length(NEW.evidence_refs) THEN
                RAISE EXCEPTION 'protection capability evidence references must be unique';
            END IF;
            SELECT jsonb_agg(value ORDER BY value)
            INTO canonical_values
            FROM jsonb_array_elements(NEW.evidence_refs) AS entry(value);
            IF NEW.evidence_refs <> canonical_values THEN
                RAISE EXCEPTION 'protection capability evidence references are not canonical';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER instrument_protection_capabilities_insert_guard
        BEFORE INSERT ON instrument_protection_capability_records
        FOR EACH ROW EXECUTE FUNCTION protect_instrument_protection_capability_insert()
        """
    )
    op.execute(
        """
        CREATE FUNCTION deny_instrument_protection_capability_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'instrument_protection_capability_records is immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER instrument_protection_capabilities_immutable
        BEFORE UPDATE OR DELETE ON instrument_protection_capability_records
        FOR EACH ROW EXECUTE FUNCTION deny_instrument_protection_capability_change()
        """
    )

    for table_name, prefix in (
        ("risk_decision_snapshots", "risk_decisions"),
        ("execution_risk_decisions", "exec_risk"),
    ):
        op.add_column(
            table_name,
            sa.Column("protection_capability_record_id", sa.Uuid(), nullable=True),
        )
        op.add_column(
            table_name,
            sa.Column(
                "protection_capability_version",
                sa.String(length=120),
                nullable=True,
            ),
        )
        op.add_column(
            table_name,
            sa.Column(
                "protection_capability_record_hash",
                sa.String(length=64),
                nullable=True,
            ),
        )
        op.create_check_constraint(
            f"ck_{prefix}_protection_capability_binding",
            table_name,
            "(protection_capability_record_id IS NULL "
            "AND protection_capability_version IS NULL "
            "AND protection_capability_record_hash IS NULL) OR "
            "(protection_capability_record_id IS NOT NULL "
            "AND protection_capability_version IS NOT NULL "
            "AND length(protection_capability_record_hash) = 64)",
        )
        op.create_foreign_key(
            f"fk_{prefix}_protection_capability",
            table_name,
            "instrument_protection_capability_records",
            [
                "protection_capability_record_id",
                "organization_id",
                "protection_capability_version",
            ],
            [
                "protection_capability_record_id",
                "organization_id",
                "position_management_template_version",
            ],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM instrument_protection_capability_records) THEN
                RAISE EXCEPTION
                    'cannot downgrade protection capabilities while facts remain';
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
            f"fk_{prefix}_protection_capability",
            table_name,
            type_="foreignkey",
        )
        op.drop_constraint(
            f"ck_{prefix}_protection_capability_binding",
            table_name,
            type_="check",
        )
        op.drop_column(table_name, "protection_capability_record_hash")
        op.drop_column(table_name, "protection_capability_version")
        op.drop_column(table_name, "protection_capability_record_id")
    op.execute(
        "DROP TRIGGER IF EXISTS instrument_protection_capabilities_immutable "
        "ON instrument_protection_capability_records"
    )
    op.execute("DROP FUNCTION IF EXISTS deny_instrument_protection_capability_change()")
    op.execute(
        "DROP TRIGGER IF EXISTS instrument_protection_capabilities_insert_guard "
        "ON instrument_protection_capability_records"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_instrument_protection_capability_insert()")
    op.drop_index(
        "ix_instrument_protection_capabilities_lookup",
        table_name="instrument_protection_capability_records",
    )
    op.drop_table("instrument_protection_capability_records")
