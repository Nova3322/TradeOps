"""Create identity and server-side authorization facts.

Revision ID: 20260718_0002
Revises: 20260718_0001
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0002"
down_revision: str | None = "20260718_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "identity_principals",
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("principal_type", sa.String(length=32), nullable=False),
        sa.Column("external_subject_ref", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "principal_type IN ('HUMAN', 'SERVICE', 'EXECUTION', 'TREASURY', 'BREAK_GLASS')",
            name="ck_identity_principals_type",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED')",
            name="ck_identity_principals_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_identity_principals_version_positive"),
        sa.PrimaryKeyConstraint("principal_id"),
        sa.UniqueConstraint(
            "organization_id",
            "external_subject_ref",
            name="uq_identity_principals_external_subject",
        ),
    )

    op.create_table(
        "role_assignments",
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("role_key", sa.String(length=40), nullable=False),
        sa.Column("policy_version", sa.String(length=120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role_key IN "
            "('OBSERVER', 'PROPOSER', 'REVIEWER', 'OPERATOR', "
            "'RISK_ADMIN', 'TREASURY_ADMIN', 'SYSTEM_ADMIN')",
            name="ck_role_assignments_role",
        ),
        sa.CheckConstraint("version >= 1", name="ck_role_assignments_version_positive"),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="ck_role_assignments_valid_window",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["identity_principals.principal_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("assignment_id"),
    )
    op.create_index(
        "ix_role_assignments_principal_active",
        "role_assignments",
        ["principal_id", "revoked_at"],
    )

    op.create_table(
        "permission_scopes",
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=True),
        sa.Column("venue", sa.String(length=80), nullable=True),
        sa.Column("sector", sa.String(length=80), nullable=True),
        sa.Column("risk_tier", sa.String(length=20), nullable=True),
        sa.Column("action_id", sa.String(length=160), nullable=True),
        sa.Column("channel", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "risk_tier IS NULL OR risk_tier IN ('LOW', 'MEDIUM', 'HIGH')",
            name="ck_permission_scopes_risk_tier",
        ),
        sa.CheckConstraint(
            "channel IS NULL OR channel IN ('WEB', 'PWA', 'TELEGRAM', 'SYSTEM', 'INTERNAL')",
            name="ck_permission_scopes_channel",
        ),
        sa.ForeignKeyConstraint(
            ["assignment_id"], ["role_assignments.assignment_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("scope_id"),
    )
    op.create_index(
        "ix_permission_scopes_assignment",
        "permission_scopes",
        ["assignment_id"],
    )

    op.create_table(
        "explicit_denies",
        sa.Column("deny_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("action_id", sa.String(length=160), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=True),
        sa.Column("venue", sa.String(length=80), nullable=True),
        sa.Column("sector", sa.String(length=80), nullable=True),
        sa.Column("risk_tier", sa.String(length=20), nullable=True),
        sa.Column("channel", sa.String(length=32), nullable=True),
        sa.Column("reason_code", sa.String(length=160), nullable=False),
        sa.Column("policy_version", sa.String(length=120), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "risk_tier IS NULL OR risk_tier IN ('LOW', 'MEDIUM', 'HIGH')",
            name="ck_explicit_denies_risk_tier",
        ),
        sa.CheckConstraint(
            "channel IS NULL OR channel IN ('WEB', 'PWA', 'TELEGRAM', 'SYSTEM', 'INTERNAL')",
            name="ck_explicit_denies_channel",
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="ck_explicit_denies_valid_window",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["identity_principals.principal_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("deny_id"),
    )
    op.create_index(
        "ix_explicit_denies_principal_active",
        "explicit_denies",
        ["principal_id", "revoked_at"],
    )

    op.create_table(
        "action_assurances",
        sa.Column("assurance_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("auth_context_ref", sa.String(length=255), nullable=False),
        sa.Column("device_ref", sa.String(length=255), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("action_id", sa.String(length=160), nullable=False),
        sa.Column("object_type", sa.String(length=120), nullable=False),
        sa.Column("object_id", sa.String(length=255), nullable=False),
        sa.Column("object_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assurance_method", sa.String(length=40), nullable=False),
        sa.Column("assurance_level", sa.String(length=40), nullable=False),
        sa.Column("verifier_ref", sa.String(length=255), nullable=False),
        sa.CheckConstraint("status IN ('VERIFIED', 'REVOKED')", name="ck_action_assurances_status"),
        sa.CheckConstraint(
            "channel IN ('WEB', 'PWA', 'TELEGRAM', 'SYSTEM', 'INTERNAL')",
            name="ck_action_assurances_channel",
        ),
        sa.CheckConstraint("expires_at > issued_at", name="ck_action_assurances_valid_window"),
        sa.CheckConstraint(
            "assurance_method = 'PASSKEY_WEBAUTHN'",
            name="ck_action_assurances_method",
        ),
        sa.CheckConstraint(
            "assurance_level = 'ACTION_STEP_UP'",
            name="ck_action_assurances_level",
        ),
        sa.CheckConstraint(
            "object_version >= 1",
            name="ck_action_assurances_object_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["identity_principals.principal_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("assurance_id"),
        sa.UniqueConstraint("auth_context_ref", name="uq_action_assurances_auth_context"),
    )
    op.create_index(
        "ix_action_assurances_principal",
        "action_assurances",
        ["principal_id", "expires_at"],
    )

    op.execute(
        """
        CREATE FUNCTION protect_action_assurance_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'action_assurances cannot be deleted';
            END IF;
            IF ROW(
                NEW.assurance_id,
                NEW.principal_id,
                NEW.auth_context_ref,
                NEW.device_ref,
                NEW.channel,
                NEW.action_id,
                NEW.object_type,
                NEW.object_id,
                NEW.object_version,
                NEW.issued_at,
                NEW.expires_at,
                NEW.assurance_method,
                NEW.assurance_level,
                NEW.verifier_ref
            ) IS DISTINCT FROM ROW(
                OLD.assurance_id,
                OLD.principal_id,
                OLD.auth_context_ref,
                OLD.device_ref,
                OLD.channel,
                OLD.action_id,
                OLD.object_type,
                OLD.object_id,
                OLD.object_version,
                OLD.issued_at,
                OLD.expires_at,
                OLD.assurance_method,
                OLD.assurance_level,
                OLD.verifier_ref
            ) THEN
                RAISE EXCEPTION 'action_assurance binding is immutable';
            END IF;
            IF OLD.used_at IS NOT NULL AND NEW.used_at IS DISTINCT FROM OLD.used_at THEN
                RAISE EXCEPTION 'action_assurance consumption is irreversible';
            END IF;
            IF OLD.status = 'REVOKED' AND NEW.status <> 'REVOKED' THEN
                RAISE EXCEPTION 'action_assurance revocation is irreversible';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER action_assurances_protected
        BEFORE UPDATE OR DELETE ON action_assurances
        FOR EACH ROW EXECUTE FUNCTION protect_action_assurance_change()
        """
    )

    op.create_table(
        "authorization_decisions",
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("command_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.String(length=160), nullable=False),
        sa.Column("object_type", sa.String(length=120), nullable=False),
        sa.Column("object_id", sa.String(length=255), nullable=False),
        sa.Column("object_version", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=True),
        sa.Column("venue", sa.String(length=80), nullable=True),
        sa.Column("sector", sa.String(length=80), nullable=True),
        sa.Column("risk_tier", sa.String(length=20), nullable=True),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("device_ref", sa.String(length=255), nullable=True),
        sa.Column("auth_context_ref", sa.String(length=255), nullable=False),
        sa.Column("result", sa.String(length=20), nullable=False),
        sa.Column("reason_code", sa.String(length=160), nullable=False),
        sa.Column("is_self_review", sa.Boolean(), nullable=False),
        sa.Column("required_quorum", sa.Integer(), nullable=True),
        sa.Column(
            "matched_assignment_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "matched_deny_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("policy_version", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("request_context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("result IN ('ALLOW', 'DENY')", name="ck_authorization_decisions_result"),
        sa.CheckConstraint(
            "risk_tier IS NULL OR risk_tier IN ('LOW', 'MEDIUM', 'HIGH')",
            name="ck_authorization_decisions_risk_tier",
        ),
        sa.CheckConstraint(
            "required_quorum IS NULL OR required_quorum IN (1, 2)",
            name="ck_authorization_decisions_quorum",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64", name="ck_authorization_decisions_hash_length"
        ),
        sa.PrimaryKeyConstraint("decision_id"),
        sa.UniqueConstraint("request_id", name="uq_authorization_decisions_request"),
    )
    op.create_index(
        "ix_authorization_decisions_principal",
        "authorization_decisions",
        ["principal_id", "decided_at"],
    )
    op.create_index(
        "ix_authorization_decisions_object",
        "authorization_decisions",
        ["object_type", "object_id", "decided_at"],
    )

    op.execute(
        """
        CREATE FUNCTION deny_immutable_authorization_decision_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'authorization_decisions is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER authorization_decisions_append_only
        BEFORE UPDATE OR DELETE ON authorization_decisions
        FOR EACH ROW EXECUTE FUNCTION deny_immutable_authorization_decision_change()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS authorization_decisions_append_only ON authorization_decisions"
    )
    op.execute("DROP FUNCTION IF EXISTS deny_immutable_authorization_decision_change()")
    op.drop_index("ix_authorization_decisions_object", table_name="authorization_decisions")
    op.drop_index("ix_authorization_decisions_principal", table_name="authorization_decisions")
    op.drop_table("authorization_decisions")
    op.execute("DROP TRIGGER IF EXISTS action_assurances_protected ON action_assurances")
    op.execute("DROP FUNCTION IF EXISTS protect_action_assurance_change()")
    op.drop_index("ix_action_assurances_principal", table_name="action_assurances")
    op.drop_table("action_assurances")
    op.drop_index("ix_explicit_denies_principal_active", table_name="explicit_denies")
    op.drop_table("explicit_denies")
    op.drop_index("ix_permission_scopes_assignment", table_name="permission_scopes")
    op.drop_table("permission_scopes")
    op.drop_index("ix_role_assignments_principal_active", table_name="role_assignments")
    op.drop_table("role_assignments")
    op.drop_table("identity_principals")
