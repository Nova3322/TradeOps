"""Add durable exact-scope shadow capability certificates.

Revision ID: 20260718_0007
Revises: 20260718_0006
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0007"
down_revision: str | None = "20260718_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "capability_evidence_bundles",
        sa.Column("evidence_bundle_id", sa.String(length=255), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("bundle_version", sa.String(length=120), nullable=False),
        sa.Column("environment", sa.String(length=24), nullable=False),
        sa.Column("certification_profile", sa.String(length=40), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_principal", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "environment = 'SHADOW' AND certification_profile = 'SHADOW_NON_DISPATCH'",
            name="ck_capability_evidence_shadow_only",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) > 0",
            name="ck_capability_evidence_refs_nonempty",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_summary) = 'object'",
            name="ck_capability_evidence_summary_object",
        ),
        sa.CheckConstraint("length(evidence_hash) = 64", name="ck_capability_evidence_hash"),
        sa.PrimaryKeyConstraint("evidence_bundle_id"),
        sa.UniqueConstraint(
            "organization_id",
            "bundle_version",
            name="uq_capability_evidence_org_version",
        ),
        sa.UniqueConstraint(
            "evidence_bundle_id",
            "organization_id",
            name="uq_capability_evidence_identity_binding",
        ),
    )
    op.create_index(
        "ix_capability_evidence_org_created",
        "capability_evidence_bundles",
        ["organization_id", "created_at"],
    )
    op.create_table(
        "capability_certificates",
        sa.Column("certificate_id", sa.String(length=255), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("certificate_type", sa.String(length=40), nullable=False),
        sa.Column("subject_ref", sa.String(length=255), nullable=False),
        sa.Column("environment", sa.String(length=24), nullable=False),
        sa.Column("real_funds_eligible", sa.Boolean(), nullable=False),
        sa.Column("scope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scope_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_versions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("policy_versions_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence_bundle_id", sa.String(length=255), nullable=False),
        sa.Column("evidence_bundle_hash", sa.String(length=64), nullable=False),
        sa.Column("max_order_notional", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("max_trade_loss", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("owner_principal", sa.String(length=255), nullable=False),
        sa.Column("issuer_principal", sa.String(length=255), nullable=False),
        sa.Column(
            "approver_principal_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("approval_ref", sa.String(length=255), nullable=False),
        sa.Column("monitoring_ref", sa.String(length=255), nullable=False),
        sa.Column("exit_recovery_ref", sa.String(length=255), nullable=False),
        sa.Column(
            "invalidation_conditions", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("supersedes", sa.String(length=255), nullable=True),
        sa.Column("certificate_hash", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("schema_version = 1", name="ck_capability_certificates_schema"),
        sa.CheckConstraint(
            "certificate_type IN ('STRATEGY_EVIDENCE', 'EXECUTION', "
            "'RISK_COVERAGE', 'MARGIN_NORMALIZATION')",
            name="ck_capability_certificates_type",
        ),
        sa.CheckConstraint(
            "environment = 'SHADOW' AND real_funds_eligible = false",
            name="ck_capability_certificates_shadow_only",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(scope) = 'object' AND jsonb_typeof(policy_versions) = 'object'",
            name="ck_capability_certificates_contract_objects",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(approver_principal_ids) = 'array' "
            "AND jsonb_array_length(approver_principal_ids) > 0",
            name="ck_capability_certificates_approvers",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(invalidation_conditions) = 'array' "
            "AND jsonb_array_length(invalidation_conditions) > 0",
            name="ck_capability_certificates_invalidation_conditions",
        ),
        sa.CheckConstraint(
            "length(scope_hash) = 64 AND length(policy_versions_hash) = 64 "
            "AND length(evidence_bundle_hash) = 64 AND length(certificate_hash) = 64",
            name="ck_capability_certificates_hashes",
        ),
        sa.CheckConstraint(
            "max_order_notional > 0 AND max_trade_loss > 0",
            name="ck_capability_certificates_limits",
        ),
        sa.CheckConstraint(
            "valid_from >= issued_at AND expires_at > valid_from",
            name="ck_capability_certificates_valid_window",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_bundle_id", "organization_id"],
            [
                "capability_evidence_bundles.evidence_bundle_id",
                "capability_evidence_bundles.organization_id",
            ],
            name="fk_capability_certificates_evidence_bundle",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes", "organization_id"],
            [
                "capability_certificates.certificate_id",
                "capability_certificates.organization_id",
            ],
            name="fk_capability_certificates_supersedes",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("certificate_id"),
        sa.UniqueConstraint(
            "certificate_id",
            "organization_id",
            name="uq_capability_certificates_identity_binding",
        ),
    )
    op.create_index(
        "ix_capability_certificates_scope_lookup",
        "capability_certificates",
        ["organization_id", "certificate_type", "environment", "expires_at"],
    )
    op.create_table(
        "capability_certificate_states",
        sa.Column("certificate_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=160), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'REVOKED', 'EXPIRED')",
            name="ck_capability_certificate_states_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_capability_certificate_states_version"),
        sa.ForeignKeyConstraint(
            ["certificate_id"],
            ["capability_certificates.certificate_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("certificate_id"),
    )
    op.create_table(
        "capability_certificate_state_history",
        sa.Column("history_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("certificate_id", sa.String(length=255), nullable=False),
        sa.Column("from_status", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=160), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'REVOKED', 'EXPIRED')",
            name="ck_capability_certificate_history_status",
        ),
        sa.CheckConstraint("state_version >= 1", name="ck_capability_certificate_history_version"),
        sa.ForeignKeyConstraint(
            ["certificate_id"],
            ["capability_certificates.certificate_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("history_id"),
        sa.UniqueConstraint(
            "certificate_id",
            "state_version",
            name="uq_capability_certificate_history_version",
        ),
    )
    op.create_index(
        "ix_capability_certificate_history_time",
        "capability_certificate_state_history",
        ["certificate_id", "changed_at"],
    )

    # Existing pre-WP-0007 rows are deliberately not blessed. NOT VALID leaves them readable,
    # while every new authorization/intent must reference a durable certificate.
    op.execute(
        """
        ALTER TABLE trading_authorizations
        ADD CONSTRAINT fk_trading_auth_capability_certificate
        FOREIGN KEY (capability_certificate_ref, organization_id)
        REFERENCES capability_certificates(certificate_id, organization_id)
        ON DELETE RESTRICT NOT VALID
        """
    )
    op.execute(
        """
        ALTER TABLE order_intents
        ADD CONSTRAINT fk_order_intents_capability_certificate
        FOREIGN KEY (capability_certificate_ref)
        REFERENCES capability_certificates(certificate_id)
        ON DELETE RESTRICT NOT VALID
        """
    )
    _create_certificate_guards()


def downgrade() -> None:
    _drop_certificate_guards()
    op.drop_constraint(
        "fk_order_intents_capability_certificate", "order_intents", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_trading_auth_capability_certificate",
        "trading_authorizations",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_capability_certificate_history_time",
        table_name="capability_certificate_state_history",
    )
    op.drop_table("capability_certificate_state_history")
    op.drop_table("capability_certificate_states")
    op.drop_index("ix_capability_certificates_scope_lookup", table_name="capability_certificates")
    op.drop_table("capability_certificates")
    op.drop_index("ix_capability_evidence_org_created", table_name="capability_evidence_bundles")
    op.drop_table("capability_evidence_bundles")


def _create_certificate_guards() -> None:
    op.execute(
        """
        CREATE FUNCTION deny_capability_certificate_fact_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is immutable', TG_TABLE_NAME;
        END;
        $$
        """
    )
    for table in (
        "capability_evidence_bundles",
        "capability_certificates",
        "capability_certificate_state_history",
    ):
        op.execute(
            f"""
            CREATE TRIGGER {table}_immutable
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION deny_capability_certificate_fact_change()
            """
        )

    op.execute(
        """
        CREATE FUNCTION protect_capability_certificate_state_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE allowed boolean := false;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'capability_certificate_states cannot be deleted';
            END IF;
            IF NEW.certificate_id <> OLD.certificate_id
                OR NEW.version <> OLD.version + 1 THEN
                RAISE EXCEPTION 'invalid capability certificate state identity/version change';
            END IF;
            allowed := (OLD.status = 'ACTIVE'
                AND NEW.status IN ('SUSPENDED', 'REVOKED', 'EXPIRED'))
                OR (OLD.status = 'SUSPENDED'
                AND NEW.status IN ('REVOKED', 'EXPIRED'));
            IF NOT allowed THEN
                RAISE EXCEPTION 'invalid capability certificate transition: % -> %',
                    OLD.status, NEW.status;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER capability_certificate_states_transition_guard
        BEFORE UPDATE OR DELETE ON capability_certificate_states
        FOR EACH ROW EXECUTE FUNCTION protect_capability_certificate_state_change()
        """
    )
    op.execute(
        """
        CREATE FUNCTION record_capability_certificate_state_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO capability_certificate_state_history (
                certificate_id, from_status, status, state_version,
                reason_code, source_ref, changed_at
            ) VALUES (
                NEW.certificate_id,
                CASE WHEN TG_OP = 'INSERT' THEN NEW.status ELSE OLD.status END,
                NEW.status, NEW.version, NEW.reason_code, NEW.source_ref, NEW.updated_at
            );
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER capability_certificate_states_record_transition
        AFTER INSERT OR UPDATE ON capability_certificate_states
        FOR EACH ROW EXECUTE FUNCTION record_capability_certificate_state_change()
        """
    )
    op.execute(
        """
        CREATE FUNCTION verify_order_intent_capability_binding()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE authorization_certificate text;
        BEGIN
            SELECT capability_certificate_ref INTO STRICT authorization_certificate
            FROM trading_authorizations
            WHERE authorization_id = NEW.authorization_id;
            IF NEW.capability_certificate_ref <> authorization_certificate THEN
                RAISE EXCEPTION 'order intent capability certificate disagrees with authorization';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER order_intents_capability_binding_guard
        BEFORE INSERT ON order_intents
        FOR EACH ROW EXECUTE FUNCTION verify_order_intent_capability_binding()
        """
    )


def _drop_certificate_guards() -> None:
    op.execute("DROP TRIGGER IF EXISTS order_intents_capability_binding_guard ON order_intents")
    op.execute("DROP FUNCTION IF EXISTS verify_order_intent_capability_binding()")
    op.execute(
        "DROP TRIGGER IF EXISTS capability_certificate_states_record_transition "
        "ON capability_certificate_states"
    )
    op.execute("DROP FUNCTION IF EXISTS record_capability_certificate_state_change()")
    op.execute(
        "DROP TRIGGER IF EXISTS capability_certificate_states_transition_guard "
        "ON capability_certificate_states"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_capability_certificate_state_change()")
    for table in (
        "capability_certificate_state_history",
        "capability_certificates",
        "capability_evidence_bundles",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS deny_capability_certificate_fact_change()")
