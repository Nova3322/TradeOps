"""Add durable shadow sender fencing and non-dispatchable claims.

Revision ID: 20260718_0008
Revises: 20260718_0007
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0008"
down_revision: str | Sequence[str] | None = "20260718_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_sender_scopes",
        sa.Column("scope_id", sa.String(length=96), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("venue", sa.String(length=80), nullable=False),
        sa.Column("execution_domain", sa.String(length=120), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=False),
        sa.Column("account_abstraction", sa.String(length=80), nullable=False),
        sa.Column("position_mode", sa.String(length=80), nullable=False),
        sa.Column("margin_mode", sa.String(length=80), nullable=False),
        sa.Column("collateral_scope", sa.String(length=120), nullable=False),
        sa.Column("collateral_pool_id", sa.String(length=160), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("live_dispatch_eligible", sa.Boolean(), nullable=False),
        sa.Column("scope_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("schema_version = 1", name="ck_execution_sender_scopes_schema"),
        sa.CheckConstraint(
            "environment = 'SHADOW' AND live_dispatch_eligible = false",
            name="ck_execution_sender_scopes_shadow_only",
        ),
        sa.CheckConstraint("length(scope_hash) = 64", name="ck_execution_sender_scopes_hash"),
        sa.PrimaryKeyConstraint("scope_id"),
        sa.UniqueConstraint(
            "organization_id",
            "venue",
            "execution_domain",
            "account_id",
            "account_abstraction",
            "position_mode",
            "margin_mode",
            "collateral_scope",
            "collateral_pool_id",
            name="uq_execution_sender_scopes_exact_scope",
        ),
        sa.UniqueConstraint(
            "scope_id", "organization_id", name="uq_execution_sender_scopes_org_binding"
        ),
    )
    op.create_table(
        "execution_sender_leases",
        sa.Column("lease_id", sa.Uuid(), nullable=False),
        sa.Column("scope_id", sa.String(length=96), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("owner_worker_id", sa.String(length=160), nullable=False),
        sa.Column("worker_config_hash", sa.String(length=64), nullable=False),
        sa.Column("credential_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("live_dispatch_eligible", sa.Boolean(), nullable=False),
        sa.Column("lease_policy_version", sa.String(length=120), nullable=False),
        sa.Column("reconciliation_evidence_ref", sa.String(length=255), nullable=False),
        sa.Column("risk_state_ack_ref", sa.String(length=255), nullable=False),
        sa.Column("worker_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("initial_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint("fencing_token > 0", name="ck_execution_sender_leases_token"),
        sa.CheckConstraint(
            "environment = 'SHADOW' AND live_dispatch_eligible = false",
            name="ck_execution_sender_leases_shadow_only",
        ),
        sa.CheckConstraint(
            "issued_at < initial_expires_at AND initial_expires_at <= max_expires_at",
            name="ck_execution_sender_leases_validity",
        ),
        sa.CheckConstraint(
            "length(worker_config_hash) = 64 AND length(credential_fingerprint) = 64 "
            "AND length(lease_hash) = 64",
            name="ck_execution_sender_leases_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["scope_id", "organization_id"],
            ["execution_sender_scopes.scope_id", "execution_sender_scopes.organization_id"],
            name="fk_execution_sender_leases_scope_org",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("lease_id"),
        sa.UniqueConstraint(
            "scope_id", "fencing_token", name="uq_execution_sender_leases_scope_token"
        ),
        sa.UniqueConstraint(
            "scope_id",
            "lease_id",
            "fencing_token",
            name="uq_execution_sender_leases_state_binding",
        ),
    )
    op.create_index(
        "ix_execution_sender_leases_owner",
        "execution_sender_leases",
        ["owner_worker_id", "issued_at"],
    )
    op.create_table(
        "execution_sender_scope_states",
        sa.Column("scope_id", sa.String(length=96), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("current_fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("active_lease_id", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason_code", sa.String(length=160), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('UNOWNED', 'LEASED', 'FENCED')",
            name="ck_execution_sender_scope_states_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_execution_sender_scope_states_version"),
        sa.CheckConstraint(
            "current_fencing_token >= 0", name="ck_execution_sender_scope_states_token"
        ),
        sa.CheckConstraint(
            "(status = 'LEASED' AND active_lease_id IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND current_fencing_token > 0) OR "
            "(status IN ('UNOWNED', 'FENCED') AND active_lease_id IS NULL "
            "AND lease_expires_at IS NULL)",
            name="ck_execution_sender_scope_states_active_binding",
        ),
        sa.ForeignKeyConstraint(
            ["scope_id"],
            ["execution_sender_scopes.scope_id"],
            name="fk_execution_sender_scope_states_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scope_id", "active_lease_id", "current_fencing_token"],
            [
                "execution_sender_leases.scope_id",
                "execution_sender_leases.lease_id",
                "execution_sender_leases.fencing_token",
            ],
            name="fk_execution_sender_scope_states_active_lease",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("scope_id"),
    )
    op.create_table(
        "execution_sender_scope_state_history",
        sa.Column("history_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope_id", sa.String(length=96), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("active_lease_id", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason_code", sa.String(length=160), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("state_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state_version >= 1", name="ck_execution_sender_history_version"),
        sa.CheckConstraint(
            "status IN ('UNOWNED', 'LEASED', 'FENCED')",
            name="ck_execution_sender_history_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(state_snapshot) = 'object' AND length(state_hash) = 64",
            name="ck_execution_sender_history_snapshot",
        ),
        sa.ForeignKeyConstraint(
            ["scope_id"],
            ["execution_sender_scopes.scope_id"],
            name="fk_execution_sender_history_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("history_id"),
        sa.UniqueConstraint(
            "scope_id", "state_version", name="uq_execution_sender_history_version"
        ),
    )
    op.create_index(
        "ix_execution_sender_history_time",
        "execution_sender_scope_state_history",
        ["scope_id", "changed_at"],
    )
    op.create_table(
        "shadow_dispatch_claims",
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("order_intent_id", sa.Uuid(), nullable=False),
        sa.Column("scope_id", sa.String(length=96), nullable=False),
        sa.Column("lease_id", sa.Uuid(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("client_order_id", sa.String(length=80), nullable=False),
        sa.Column("owner_worker_id", sa.String(length=160), nullable=False),
        sa.Column("worker_config_hash", sa.String(length=64), nullable=False),
        sa.Column("credential_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("capability_certificate_ref", sa.String(length=255), nullable=False),
        sa.Column("execution_mode", sa.String(length=20), nullable=False),
        sa.Column("external_send_permitted", sa.Boolean(), nullable=False),
        sa.Column("live_gate_status", sa.String(length=20), nullable=False),
        sa.Column("intent_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("capability_certificate_hash", sa.String(length=64), nullable=False),
        sa.Column("scope_hash", sa.String(length=64), nullable=False),
        sa.Column("lease_hash", sa.String(length=64), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("worker_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason_code", sa.String(length=160), nullable=False),
        sa.Column("claim_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "execution_mode = 'SHADOW' AND external_send_permitted = false "
            "AND live_gate_status = 'DISABLED'",
            name="ck_shadow_dispatch_claims_non_dispatchable",
        ),
        sa.CheckConstraint(
            "fencing_token > 0 AND claimed_at < lease_expires_at",
            name="ck_shadow_dispatch_claims_lease_window",
        ),
        sa.CheckConstraint(
            "length(worker_config_hash) = 64 AND length(credential_fingerprint) = 64 "
            "AND length(intent_snapshot_hash) = 64 AND length(capability_certificate_hash) = 64 "
            "AND length(scope_hash) = 64 AND length(lease_hash) = 64 "
            "AND length(claim_hash) = 64",
            name="ck_shadow_dispatch_claims_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["order_intent_id"],
            ["order_intents.order_intent_id"],
            name="fk_shadow_dispatch_claims_order_intent",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scope_id", "organization_id"],
            ["execution_sender_scopes.scope_id", "execution_sender_scopes.organization_id"],
            name="fk_shadow_dispatch_claims_scope_org",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scope_id", "lease_id", "fencing_token"],
            [
                "execution_sender_leases.scope_id",
                "execution_sender_leases.lease_id",
                "execution_sender_leases.fencing_token",
            ],
            name="fk_shadow_dispatch_claims_lease_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["capability_certificate_ref", "organization_id"],
            ["capability_certificates.certificate_id", "capability_certificates.organization_id"],
            name="fk_shadow_dispatch_claims_certificate_org",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("claim_id"),
        sa.UniqueConstraint("order_intent_id", name="uq_shadow_dispatch_claims_order_intent"),
        sa.UniqueConstraint(
            "scope_id", "client_order_id", name="uq_shadow_dispatch_claims_client_order_id"
        ),
    )
    op.create_index(
        "ix_shadow_dispatch_claims_scope_time",
        "shadow_dispatch_claims",
        ["scope_id", "claimed_at"],
    )
    _create_sender_fencing_guards()


def downgrade() -> None:
    _drop_sender_fencing_guards()
    op.drop_index("ix_shadow_dispatch_claims_scope_time", table_name="shadow_dispatch_claims")
    op.drop_table("shadow_dispatch_claims")
    op.drop_index(
        "ix_execution_sender_history_time",
        table_name="execution_sender_scope_state_history",
    )
    op.drop_table("execution_sender_scope_state_history")
    op.drop_table("execution_sender_scope_states")
    op.drop_index("ix_execution_sender_leases_owner", table_name="execution_sender_leases")
    op.drop_table("execution_sender_leases")
    op.drop_table("execution_sender_scopes")


def _create_sender_fencing_guards() -> None:
    op.execute(
        """
        CREATE FUNCTION deny_sender_fencing_fact_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is immutable', TG_TABLE_NAME;
        END;
        $$
        """
    )
    for table in (
        "execution_sender_scopes",
        "execution_sender_leases",
        "execution_sender_scope_state_history",
        "shadow_dispatch_claims",
    ):
        op.execute(
            f"""
            CREATE TRIGGER {table}_immutable
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION deny_sender_fencing_fact_change()
            """
        )

    op.execute(
        """
        CREATE FUNCTION protect_execution_sender_scope_state_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'execution_sender_scope_states cannot be deleted';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.version <> 1 OR NEW.status <> 'LEASED'
                    OR NEW.current_fencing_token <> 1
                    OR NEW.active_lease_id IS NULL
                    OR NEW.lease_expires_at IS NULL
                    OR NEW.lease_expires_at <= NEW.updated_at THEN
                    RAISE EXCEPTION 'initial sender scope state must be a valid first lease';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.scope_id <> OLD.scope_id
                OR NEW.version <> OLD.version + 1
                OR NEW.updated_at < OLD.updated_at
                OR NEW.current_fencing_token < OLD.current_fencing_token THEN
                RAISE EXCEPTION 'invalid sender scope identity, version, time, or token';
            END IF;
            IF OLD.active_lease_id IS NOT NULL
                AND NEW.active_lease_id = OLD.active_lease_id THEN
                IF OLD.status <> 'LEASED' OR NEW.status <> 'LEASED'
                    OR NEW.current_fencing_token <> OLD.current_fencing_token
                    OR NEW.lease_expires_at IS NULL
                    OR OLD.lease_expires_at IS NULL
                    OR NEW.lease_expires_at <= OLD.lease_expires_at THEN
                    RAISE EXCEPTION 'same lease may only renew forward without changing token';
                END IF;
            ELSIF NEW.current_fencing_token <= OLD.current_fencing_token THEN
                RAISE EXCEPTION 'ownership change or invalidation must advance fencing token';
            END IF;
            IF NEW.status = 'LEASED' AND (
                NEW.active_lease_id IS NULL OR NEW.lease_expires_at IS NULL
                OR NEW.lease_expires_at <= NEW.updated_at) THEN
                RAISE EXCEPTION 'leased sender scope requires a future active lease';
            ELSIF NEW.status IN ('UNOWNED', 'FENCED') AND (
                NEW.active_lease_id IS NOT NULL OR NEW.lease_expires_at IS NOT NULL) THEN
                RAISE EXCEPTION 'inactive sender scope cannot retain lease authority';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER execution_sender_scope_states_transition_guard
        BEFORE INSERT OR UPDATE OR DELETE ON execution_sender_scope_states
        FOR EACH ROW EXECUTE FUNCTION protect_execution_sender_scope_state_change()
        """
    )
    op.execute(
        """
        CREATE FUNCTION record_execution_sender_scope_state_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE snapshot jsonb;
        BEGIN
            snapshot := to_jsonb(NEW);
            INSERT INTO execution_sender_scope_state_history (
                scope_id, state_version, status, fencing_token, active_lease_id,
                lease_expires_at, reason_code, source_ref, state_snapshot,
                state_hash, changed_at
            ) VALUES (
                NEW.scope_id, NEW.version, NEW.status, NEW.current_fencing_token,
                NEW.active_lease_id, NEW.lease_expires_at, NEW.reason_code,
                NEW.source_ref, snapshot,
                encode(sha256(convert_to(snapshot::text, 'UTF8')), 'hex'),
                NEW.updated_at
            );
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER execution_sender_scope_states_record_history
        AFTER INSERT OR UPDATE ON execution_sender_scope_states
        FOR EACH ROW EXECUTE FUNCTION record_execution_sender_scope_state_change()
        """
    )

    op.execute(
        """
        CREATE FUNCTION verify_shadow_dispatch_claim()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE scope_row execution_sender_scopes%ROWTYPE;
        DECLARE state_row execution_sender_scope_states%ROWTYPE;
        DECLARE lease_row execution_sender_leases%ROWTYPE;
        DECLARE intent_row order_intents%ROWTYPE;
        DECLARE certificate_row capability_certificates%ROWTYPE;
        DECLARE decision_org text;
        DECLARE intent_status text;
        DECLARE certificate_status text;
        DECLARE gate_status text;
        BEGIN
            SELECT * INTO STRICT scope_row FROM execution_sender_scopes
            WHERE scope_id = NEW.scope_id;
            SELECT * INTO STRICT state_row FROM execution_sender_scope_states
            WHERE scope_id = NEW.scope_id;
            SELECT * INTO STRICT lease_row FROM execution_sender_leases
            WHERE lease_id = NEW.lease_id;
            SELECT * INTO STRICT intent_row FROM order_intents
            WHERE order_intent_id = NEW.order_intent_id;
            SELECT organization_id INTO STRICT decision_org FROM execution_risk_decisions
            WHERE execution_risk_decision_id = intent_row.execution_risk_decision_id;
            SELECT status INTO STRICT intent_status FROM order_intent_states
            WHERE order_intent_id = NEW.order_intent_id;
            SELECT * INTO STRICT certificate_row FROM capability_certificates
            WHERE certificate_id = NEW.capability_certificate_ref;
            SELECT status INTO STRICT certificate_status FROM capability_certificate_states
            WHERE certificate_id = NEW.capability_certificate_ref;
            SELECT status INTO STRICT gate_status FROM capability_gates
            WHERE capability_key = 'LIVE_ORDER_SEND';

            IF scope_row.organization_id <> NEW.organization_id
                OR decision_org <> NEW.organization_id
                OR scope_row.environment <> 'SHADOW'
                OR scope_row.live_dispatch_eligible
                OR scope_row.scope_hash <> NEW.scope_hash
                OR state_row.status <> 'LEASED'
                OR state_row.active_lease_id <> NEW.lease_id
                OR state_row.current_fencing_token <> NEW.fencing_token
                OR state_row.lease_expires_at <> NEW.lease_expires_at
                OR NEW.claimed_at >= state_row.lease_expires_at
                OR lease_row.scope_id <> NEW.scope_id
                OR lease_row.organization_id <> NEW.organization_id
                OR lease_row.fencing_token <> NEW.fencing_token
                OR lease_row.owner_worker_id <> NEW.owner_worker_id
                OR lease_row.worker_config_hash <> NEW.worker_config_hash
                OR lease_row.credential_fingerprint <> NEW.credential_fingerprint
                OR lease_row.lease_hash <> NEW.lease_hash
                OR lease_row.environment <> 'SHADOW'
                OR lease_row.live_dispatch_eligible
                OR intent_row.execution_mode <> 'SHADOW'
                OR intent_row.dispatch_eligible
                OR intent_status <> 'INTENT_CREATED'
                OR NEW.claimed_at < intent_row.valid_from
                OR NEW.claimed_at >= intent_row.valid_until
                OR intent_row.venue <> scope_row.venue
                OR intent_row.execution_domain <> scope_row.execution_domain
                OR intent_row.account_id <> scope_row.account_id
                OR intent_row.margin_mode <> scope_row.margin_mode
                OR intent_row.collateral_scope <> scope_row.collateral_scope
                OR intent_row.collateral_pool_id <> scope_row.collateral_pool_id
                OR intent_row.worker_id <> NEW.owner_worker_id
                OR intent_row.intent_snapshot_hash <> NEW.intent_snapshot_hash
                OR intent_row.capability_certificate_ref <> NEW.capability_certificate_ref
                OR certificate_row.organization_id <> NEW.organization_id
                OR certificate_row.environment <> 'SHADOW'
                OR certificate_row.real_funds_eligible
                OR certificate_row.certificate_hash <> NEW.capability_certificate_hash
                OR certificate_status <> 'ACTIVE'
                OR NEW.claimed_at < certificate_row.valid_from
                OR NEW.claimed_at >= certificate_row.expires_at
                OR certificate_row.scope ->> 'venue' <> scope_row.venue
                OR certificate_row.scope ->> 'execution_domain' <> scope_row.execution_domain
                OR certificate_row.scope ->> 'account_id' <> scope_row.account_id
                OR certificate_row.scope ->> 'account_abstraction' <> scope_row.account_abstraction
                OR certificate_row.scope ->> 'position_mode' <> scope_row.position_mode
                OR certificate_row.scope ->> 'margin_mode' <> scope_row.margin_mode
                OR certificate_row.scope ->> 'collateral_scope' <> scope_row.collateral_scope
                OR certificate_row.scope ->> 'collateral_pool_id' <> scope_row.collateral_pool_id
                OR certificate_row.scope ->> 'worker_id' <> NEW.owner_worker_id
                OR certificate_row.scope ->> 'worker_config_hash' <> NEW.worker_config_hash
                OR certificate_row.scope ->> 'credential_fingerprint' <> NEW.credential_fingerprint
                OR gate_status <> 'DISABLED'
                OR NEW.execution_mode <> 'SHADOW'
                OR NEW.external_send_permitted
                OR NEW.live_gate_status <> 'DISABLED' THEN
                RAISE EXCEPTION
                    'shadow dispatch claim violates current fenced non-dispatch contract';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER shadow_dispatch_claims_contract_guard
        BEFORE INSERT ON shadow_dispatch_claims
        FOR EACH ROW EXECUTE FUNCTION verify_shadow_dispatch_claim()
        """
    )


def _drop_sender_fencing_guards() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS shadow_dispatch_claims_contract_guard ON shadow_dispatch_claims"
    )
    op.execute("DROP FUNCTION IF EXISTS verify_shadow_dispatch_claim()")
    op.execute(
        "DROP TRIGGER IF EXISTS execution_sender_scope_states_record_history "
        "ON execution_sender_scope_states"
    )
    op.execute("DROP FUNCTION IF EXISTS record_execution_sender_scope_state_change()")
    op.execute(
        "DROP TRIGGER IF EXISTS execution_sender_scope_states_transition_guard "
        "ON execution_sender_scope_states"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_execution_sender_scope_state_change()")
    for table in (
        "shadow_dispatch_claims",
        "execution_sender_scope_state_history",
        "execution_sender_leases",
        "execution_sender_scopes",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS deny_sender_fencing_fact_change()")
