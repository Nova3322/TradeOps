"""Add immutable managed-capital account-universe manifests.

Revision ID: 20260718_0020
Revises: 20260718_0019
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0020"
down_revision: str | Sequence[str] | None = "20260718_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "managed_capital_scope_manifests",
        sa.Column("manifest_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("manifest_version", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("real_funds_eligible", sa.Boolean(), nullable=False),
        sa.Column("risk_inclusion_mode", sa.String(length=32), nullable=False),
        sa.Column("report_currency", sa.String(length=20), nullable=False),
        sa.Column(
            "account_scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("account_scope_count", sa.Integer(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
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
            name="ck_managed_capital_scope_manifests_shadow_only",
        ),
        sa.CheckConstraint(
            "risk_inclusion_mode = 'EXCHANGE_ONLY' AND report_currency = 'USD'",
            name="ck_managed_capital_scope_manifests_fixed_policy",
        ),
        sa.CheckConstraint(
            "manifest_version > 0 AND account_scope_count > 0",
            name="ck_managed_capital_scope_manifests_positive_counts",
        ),
        sa.CheckConstraint(
            "valid_until > valid_from",
            name="ck_managed_capital_scope_manifests_valid_window",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(account_scopes) = 'array' "
            "AND jsonb_array_length(account_scopes) = account_scope_count",
            name="ck_managed_capital_scope_manifests_scopes",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) > 0",
            name="ck_managed_capital_scope_manifests_evidence",
        ),
        sa.CheckConstraint(
            "length(manifest_hash) = 64 AND length(evidence_hash) = 64",
            name="ck_managed_capital_scope_manifests_hashes",
        ),
        sa.PrimaryKeyConstraint("manifest_id"),
        sa.UniqueConstraint(
            "organization_id",
            "manifest_version",
            name="uq_managed_capital_scope_manifests_org_version",
        ),
        sa.UniqueConstraint(
            "manifest_id",
            "organization_id",
            "manifest_version",
            name="uq_managed_capital_scope_manifests_identity_binding",
        ),
    )
    op.create_index(
        "ix_managed_capital_scope_manifests_lookup",
        "managed_capital_scope_manifests",
        ["organization_id", "manifest_version"],
    )
    op.execute(
        """
        CREATE FUNCTION protect_managed_capital_scope_manifest_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            canonical_scopes jsonb;
            canonical_evidence jsonb;
            scope_keys text[] := ARRAY[
                'account_id',
                'collateral_pool_id',
                'execution_domain',
                'margin_mode',
                'organization_id',
                'settlement_currency',
                'venue'
            ];
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM jsonb_array_elements(NEW.account_scopes) AS entry(scope)
                WHERE jsonb_typeof(scope) <> 'object'
                   OR ARRAY(
                        SELECT key
                        FROM jsonb_object_keys(scope) AS key
                        ORDER BY key
                   ) <> scope_keys
                   OR scope->>'organization_id' <> NEW.organization_id
                   OR coalesce(length(scope->>'venue'), 0) = 0
                   OR length(scope->>'venue') > 80
                   OR coalesce(length(scope->>'execution_domain'), 0) = 0
                   OR length(scope->>'execution_domain') > 120
                   OR coalesce(length(scope->>'account_id'), 0) = 0
                   OR length(scope->>'account_id') > 160
                   OR coalesce(length(scope->>'margin_mode'), 0) = 0
                   OR length(scope->>'margin_mode') > 80
                   OR coalesce(length(scope->>'collateral_pool_id'), 0) = 0
                   OR length(scope->>'collateral_pool_id') > 160
                   OR coalesce(length(scope->>'settlement_currency'), 0) = 0
                   OR length(scope->>'settlement_currency') > 80
            ) THEN
                RAISE EXCEPTION 'managed capital account scope contract is invalid';
            END IF;

            IF (
                SELECT count(DISTINCT scope)
                FROM jsonb_array_elements(NEW.account_scopes) AS entry(scope)
            ) <> NEW.account_scope_count THEN
                RAISE EXCEPTION 'managed capital account scopes must be unique';
            END IF;

            SELECT jsonb_agg(scope ORDER BY
                scope->>'organization_id',
                scope->>'venue',
                scope->>'execution_domain',
                scope->>'account_id',
                scope->>'margin_mode',
                scope->>'collateral_pool_id',
                scope->>'settlement_currency'
            )
            INTO canonical_scopes
            FROM jsonb_array_elements(NEW.account_scopes) AS entry(scope);
            IF NEW.account_scopes <> canonical_scopes THEN
                RAISE EXCEPTION 'managed capital account scopes are not canonical';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM jsonb_array_elements(NEW.evidence_refs) AS entry(reference)
                WHERE jsonb_typeof(reference) <> 'string'
                   OR length(trim(both '"' from reference::text)) = 0
            ) THEN
                RAISE EXCEPTION 'managed capital evidence references are invalid';
            END IF;
            IF (
                SELECT count(DISTINCT reference)
                FROM jsonb_array_elements(NEW.evidence_refs) AS entry(reference)
            ) <> jsonb_array_length(NEW.evidence_refs) THEN
                RAISE EXCEPTION 'managed capital evidence references must be unique';
            END IF;
            SELECT jsonb_agg(reference ORDER BY reference)
            INTO canonical_evidence
            FROM jsonb_array_elements(NEW.evidence_refs) AS entry(reference);
            IF NEW.evidence_refs <> canonical_evidence THEN
                RAISE EXCEPTION 'managed capital evidence references are not canonical';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER managed_capital_scope_manifests_insert_guard
        BEFORE INSERT ON managed_capital_scope_manifests
        FOR EACH ROW EXECUTE FUNCTION protect_managed_capital_scope_manifest_insert()
        """
    )
    op.execute(
        """
        CREATE FUNCTION deny_managed_capital_scope_manifest_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'managed_capital_scope_manifests is immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER managed_capital_scope_manifests_immutable
        BEFORE UPDATE OR DELETE ON managed_capital_scope_manifests
        FOR EACH ROW EXECUTE FUNCTION deny_managed_capital_scope_manifest_change()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM managed_capital_scope_manifests) THEN
                RAISE EXCEPTION
                    'cannot downgrade managed capital scope manifests while facts remain';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS managed_capital_scope_manifests_immutable "
        "ON managed_capital_scope_manifests"
    )
    op.execute("DROP FUNCTION IF EXISTS deny_managed_capital_scope_manifest_change()")
    op.execute(
        "DROP TRIGGER IF EXISTS managed_capital_scope_manifests_insert_guard "
        "ON managed_capital_scope_manifests"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_managed_capital_scope_manifest_insert()")
    op.drop_index(
        "ix_managed_capital_scope_manifests_lookup",
        table_name="managed_capital_scope_manifests",
    )
    op.drop_table("managed_capital_scope_manifests")
