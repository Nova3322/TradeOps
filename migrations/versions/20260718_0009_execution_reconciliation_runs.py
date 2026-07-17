"""Add durable execution reconciliation runs and claim binding.

Revision ID: 20260718_0009
Revises: 20260718_0008
Create Date: 2026-07-18
"""

# ruff: noqa: S608

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0009"
down_revision: str | Sequence[str] | None = "20260718_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _create_reconciliation_tables()
    op.add_column(
        "shadow_dispatch_claims",
        sa.Column("reconciliation_run_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "shadow_dispatch_claims",
        sa.Column("reconciliation_result_hash", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_shadow_dispatch_claims_reconciliation",
        "shadow_dispatch_claims",
        "(reconciliation_run_id IS NULL AND reconciliation_result_hash IS NULL) OR "
        "(reconciliation_run_id IS NOT NULL AND length(reconciliation_result_hash) = 64)",
    )
    op.create_foreign_key(
        "fk_shadow_dispatch_claims_reconciliation_binding",
        "shadow_dispatch_claims",
        "execution_reconciliation_runs",
        ["reconciliation_run_id", "scope_id", "lease_id", "fencing_token"],
        ["run_id", "scope_id", "lease_id", "fencing_token"],
        ondelete="RESTRICT",
    )
    _create_reconciliation_guards()
    _replace_claim_guard_with_reconciliation()


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS shadow_dispatch_claims_contract_guard ON shadow_dispatch_claims"
    )
    op.execute("DROP FUNCTION IF EXISTS verify_shadow_dispatch_claim()")
    _drop_reconciliation_guards()
    op.drop_constraint(
        "fk_shadow_dispatch_claims_reconciliation_binding",
        "shadow_dispatch_claims",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_shadow_dispatch_claims_reconciliation",
        "shadow_dispatch_claims",
        type_="check",
    )
    op.drop_column("shadow_dispatch_claims", "reconciliation_result_hash")
    op.drop_column("shadow_dispatch_claims", "reconciliation_run_id")
    _drop_reconciliation_tables()
    _create_legacy_claim_guard()


def _create_reconciliation_tables() -> None:
    op.create_table(
        "execution_reconciliation_runs",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("scope_id", sa.String(length=96), nullable=False),
        sa.Column("lease_id", sa.Uuid(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("trigger_type", sa.String(length=40), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("live_dispatch_eligible", sa.Boolean(), nullable=False),
        sa.Column("required_source_types", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("observation_window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("supersedes_run_id", sa.Uuid(), nullable=True),
        sa.Column("initiated_by", sa.String(length=160), nullable=False),
        sa.Column("reason_code", sa.String(length=160), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint("schema_version = 1", name="ck_execution_reconciliation_runs_schema"),
        sa.CheckConstraint(
            "environment = 'SHADOW' AND live_dispatch_eligible = false",
            name="ck_execution_reconciliation_runs_shadow_only",
        ),
        sa.CheckConstraint(
            "trigger_type IN ('STARTUP', 'PRIVATE_STREAM_RECONNECT', 'ORDER_UNKNOWN', "
            "'PARTIAL_FILL', 'CAMPAIGN_CLOSE', 'MANUAL_RECOVERY')",
            name="ck_execution_reconciliation_runs_trigger",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(required_source_types) = 'array' "
            "AND jsonb_array_length(required_source_types) = 7",
            name="ck_execution_reconciliation_runs_sources",
        ),
        sa.CheckConstraint(
            "observation_window_start < observation_window_end AND started_at < deadline_at",
            name="ck_execution_reconciliation_runs_window",
        ),
        sa.CheckConstraint(
            "fencing_token > 0 AND length(run_hash) = 64",
            name="ck_execution_reconciliation_runs_integrity",
        ),
        sa.ForeignKeyConstraint(
            ["scope_id", "organization_id"],
            ["execution_sender_scopes.scope_id", "execution_sender_scopes.organization_id"],
            name="fk_execution_reconciliation_runs_scope_org",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scope_id", "lease_id", "fencing_token"],
            [
                "execution_sender_leases.scope_id",
                "execution_sender_leases.lease_id",
                "execution_sender_leases.fencing_token",
            ],
            name="fk_execution_reconciliation_runs_lease_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_run_id"],
            ["execution_reconciliation_runs.run_id"],
            name="fk_execution_reconciliation_runs_supersedes",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint(
            "run_id", "organization_id", name="uq_execution_reconciliation_runs_org_binding"
        ),
        sa.UniqueConstraint(
            "run_id",
            "scope_id",
            "lease_id",
            "fencing_token",
            name="uq_execution_reconciliation_runs_claim_binding",
        ),
    )
    op.create_index(
        "ix_execution_reconciliation_runs_scope_time",
        "execution_reconciliation_runs",
        ["scope_id", "started_at"],
    )
    op.create_table(
        "execution_reconciliation_inputs",
        sa.Column("input_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("source_type", sa.String(length=40), nullable=False),
        sa.Column("collection_status", sa.String(length=20), nullable=False),
        sa.Column("source_version", sa.String(length=160), nullable=False),
        sa.Column("watermark_type", sa.String(length=80), nullable=False),
        sa.Column("watermark_value", sa.String(length=255), nullable=False),
        sa.Column("observed_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_through", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("item_count", sa.BigInteger(), nullable=False),
        sa.Column("payload_ref", sa.String(length=255), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence_ref", sa.String(length=255), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('TRADING_LEDGER', 'VENUE_ORDERS', 'VENUE_FILLS', "
            "'VENUE_POSITIONS', 'VENUE_BALANCES', 'VENUE_PROTECTION', 'WORKER_LOCAL')",
            name="ck_execution_reconciliation_inputs_source",
        ),
        sa.CheckConstraint(
            "collection_status IN ('COMPLETE', 'UNKNOWN')",
            name="ck_execution_reconciliation_inputs_status",
        ),
        sa.CheckConstraint(
            "observed_from <= observed_through AND item_count >= 0",
            name="ck_execution_reconciliation_inputs_window",
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64 AND length(evidence_hash) = 64 AND length(input_hash) = 64",
            name="ck_execution_reconciliation_inputs_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "organization_id"],
            [
                "execution_reconciliation_runs.run_id",
                "execution_reconciliation_runs.organization_id",
            ],
            name="fk_execution_reconciliation_inputs_run_org",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("input_id"),
        sa.UniqueConstraint(
            "run_id", "source_type", name="uq_execution_reconciliation_inputs_source"
        ),
    )
    op.create_index(
        "ix_execution_reconciliation_inputs_watermark",
        "execution_reconciliation_inputs",
        ["source_type", "observed_through"],
    )
    op.create_table(
        "execution_reconciliation_findings",
        sa.Column("finding_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("finding_sequence", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("subject_type", sa.String(length=80), nullable=False),
        sa.Column("subject_id", sa.String(length=255), nullable=False),
        sa.Column("expected_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expected_hash", sa.String(length=64), nullable=False),
        sa.Column("observed_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("observed_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence_ref", sa.String(length=255), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("finding_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("finding_sequence > 0", name="ck_execution_reconciliation_findings_seq"),
        sa.CheckConstraint(
            "category IN ('MISSING_FACT', 'UNEXPLAINED_ORDER', 'POSITION_MISMATCH', "
            "'BALANCE_MISMATCH', 'PROTECTION_GAP', 'HEAT_MISMATCH', 'WORKER_DRIFT', "
            "'STALE_WATERMARK', 'OTHER')",
            name="ck_execution_reconciliation_findings_category",
        ),
        sa.CheckConstraint(
            "severity IN ('INFO', 'WARNING', 'BLOCKING', 'UNKNOWN')",
            name="ck_execution_reconciliation_findings_severity",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(expected_snapshot) = 'object' "
            "AND jsonb_typeof(observed_snapshot) = 'object' "
            "AND length(expected_hash) = 64 AND length(observed_hash) = 64 "
            "AND length(evidence_hash) = 64 AND length(finding_hash) = 64",
            name="ck_execution_reconciliation_findings_integrity",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "organization_id"],
            [
                "execution_reconciliation_runs.run_id",
                "execution_reconciliation_runs.organization_id",
            ],
            name="fk_execution_reconciliation_findings_run_org",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("finding_id"),
        sa.UniqueConstraint(
            "finding_id",
            "run_id",
            "organization_id",
            name="uq_execution_reconciliation_findings_resolution_binding",
        ),
        sa.UniqueConstraint(
            "run_id",
            "finding_sequence",
            name="uq_execution_reconciliation_findings_sequence",
        ),
    )
    op.create_index(
        "ix_execution_reconciliation_findings_run_severity",
        "execution_reconciliation_findings",
        ["run_id", "severity"],
    )
    op.create_table(
        "execution_reconciliation_finding_resolutions",
        sa.Column("resolution_id", sa.Uuid(), nullable=False),
        sa.Column("finding_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("disposition", sa.String(length=20), nullable=False),
        sa.Column("resolution_type", sa.String(length=40), nullable=False),
        sa.Column("corrective_action_ref", sa.String(length=255), nullable=False),
        sa.Column("corrective_action_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence_ref", sa.String(length=255), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("resolved_by", sa.String(length=160), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolution_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "disposition = 'RESOLVED_SAFE'",
            name="ck_execution_reconciliation_resolutions_disposition",
        ),
        sa.CheckConstraint(
            "resolution_type IN ('VENUE_FACT_CONFIRMED', 'TRADING_PROJECTION_CORRECTED', "
            "'RISK_HELD', 'NO_EXTERNAL_EFFECT_PROVED')",
            name="ck_execution_reconciliation_resolutions_type",
        ),
        sa.CheckConstraint(
            "length(corrective_action_hash) = 64 AND length(evidence_hash) = 64 "
            "AND length(resolution_hash) = 64",
            name="ck_execution_reconciliation_resolutions_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["finding_id", "run_id", "organization_id"],
            [
                "execution_reconciliation_findings.finding_id",
                "execution_reconciliation_findings.run_id",
                "execution_reconciliation_findings.organization_id",
            ],
            name="fk_execution_reconciliation_resolutions_finding",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("resolution_id"),
        sa.UniqueConstraint("finding_id", name="uq_execution_reconciliation_resolutions_finding"),
    )
    op.create_table(
        "execution_reconciliation_run_states",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("scope_id", sa.String(length=96), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("phase", sa.String(length=20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("collected_source_count", sa.Integer(), nullable=False),
        sa.Column("finding_count", sa.Integer(), nullable=False),
        sa.Column("unresolved_blocking_count", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=160), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("result_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result_hash", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'UNKNOWN', 'SUCCEEDED', 'FAILED')",
            name="ck_execution_reconciliation_states_status",
        ),
        sa.CheckConstraint(
            "phase IN ('COLLECTING', 'COMPARING', 'ADJUSTING')",
            name="ck_execution_reconciliation_states_phase",
        ),
        sa.CheckConstraint("version >= 1", name="ck_execution_reconciliation_states_version"),
        sa.CheckConstraint(
            "collected_source_count >= 0 AND finding_count >= 0 AND unresolved_blocking_count >= 0",
            name="ck_execution_reconciliation_states_counts",
        ),
        sa.CheckConstraint(
            "(status = 'RUNNING' AND completed_at IS NULL AND result_snapshot IS NULL "
            "AND result_hash IS NULL) OR "
            "(status IN ('UNKNOWN', 'SUCCEEDED', 'FAILED') AND completed_at IS NOT NULL "
            "AND result_snapshot IS NOT NULL AND result_hash IS NOT NULL "
            "AND length(result_hash) = 64)",
            name="ck_execution_reconciliation_states_terminal_result",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "organization_id"],
            [
                "execution_reconciliation_runs.run_id",
                "execution_reconciliation_runs.organization_id",
            ],
            name="fk_execution_reconciliation_states_run_org",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint(
            "run_id", "scope_id", name="uq_execution_reconciliation_states_scope_binding"
        ),
    )
    op.create_index(
        "uq_execution_reconciliation_states_active_scope",
        "execution_reconciliation_run_states",
        ["scope_id"],
        unique=True,
        postgresql_where=sa.text("status = 'RUNNING'"),
    )
    op.create_table(
        "execution_reconciliation_run_state_history",
        sa.Column("history_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("scope_id", sa.String(length=96), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("phase", sa.String(length=20), nullable=False),
        sa.Column("collected_source_count", sa.Integer(), nullable=False),
        sa.Column("finding_count", sa.Integer(), nullable=False),
        sa.Column("unresolved_blocking_count", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=160), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("state_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state_version >= 1", name="ck_execution_reconciliation_history_ver"),
        sa.CheckConstraint(
            "jsonb_typeof(state_snapshot) = 'object' AND length(state_hash) = 64",
            name="ck_execution_reconciliation_history_integrity",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "scope_id"],
            [
                "execution_reconciliation_run_states.run_id",
                "execution_reconciliation_run_states.scope_id",
            ],
            name="fk_execution_reconciliation_history_state",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("history_id"),
        sa.UniqueConstraint(
            "run_id", "state_version", name="uq_execution_reconciliation_history_version"
        ),
    )
    op.create_index(
        "ix_execution_reconciliation_history_time",
        "execution_reconciliation_run_state_history",
        ["run_id", "changed_at"],
    )


def _drop_reconciliation_tables() -> None:
    op.drop_index(
        "ix_execution_reconciliation_history_time",
        table_name="execution_reconciliation_run_state_history",
    )
    op.drop_table("execution_reconciliation_run_state_history")
    op.drop_index(
        "uq_execution_reconciliation_states_active_scope",
        table_name="execution_reconciliation_run_states",
    )
    op.drop_table("execution_reconciliation_run_states")
    op.drop_table("execution_reconciliation_finding_resolutions")
    op.drop_index(
        "ix_execution_reconciliation_findings_run_severity",
        table_name="execution_reconciliation_findings",
    )
    op.drop_table("execution_reconciliation_findings")
    op.drop_index(
        "ix_execution_reconciliation_inputs_watermark",
        table_name="execution_reconciliation_inputs",
    )
    op.drop_table("execution_reconciliation_inputs")
    op.drop_index(
        "ix_execution_reconciliation_runs_scope_time",
        table_name="execution_reconciliation_runs",
    )
    op.drop_table("execution_reconciliation_runs")


def _create_reconciliation_guards() -> None:
    op.execute(
        """
        CREATE FUNCTION deny_reconciliation_fact_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is immutable', TG_TABLE_NAME;
        END;
        $$
        """
    )
    for table in (
        "execution_reconciliation_runs",
        "execution_reconciliation_inputs",
        "execution_reconciliation_findings",
        "execution_reconciliation_finding_resolutions",
        "execution_reconciliation_run_state_history",
    ):
        op.execute(
            f"""
            CREATE TRIGGER {table}_immutable
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION deny_reconciliation_fact_change()
            """
        )
    op.execute(
        """
        CREATE FUNCTION protect_execution_reconciliation_run_insert()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE sender_state execution_sender_scope_states%ROWTYPE;
        DECLARE lease_row execution_sender_leases%ROWTYPE;
        DECLARE latest_run_id uuid;
        BEGIN
            SELECT * INTO STRICT sender_state FROM execution_sender_scope_states
            WHERE scope_id = NEW.scope_id;
            SELECT * INTO STRICT lease_row FROM execution_sender_leases
            WHERE lease_id = NEW.lease_id;
            IF sender_state.status <> 'LEASED'
                OR sender_state.active_lease_id <> NEW.lease_id
                OR sender_state.current_fencing_token <> NEW.fencing_token
                OR sender_state.lease_expires_at IS NULL
                OR NEW.started_at >= sender_state.lease_expires_at
                OR lease_row.scope_id <> NEW.scope_id
                OR lease_row.organization_id <> NEW.organization_id
                OR lease_row.fencing_token <> NEW.fencing_token
                OR NEW.started_at < lease_row.issued_at
                OR lease_row.environment <> 'SHADOW'
                OR lease_row.live_dispatch_eligible THEN
                RAISE EXCEPTION 'reconciliation run requires the exact current shadow lease';
            END IF;
            IF NEW.required_source_types <> jsonb_build_array(
                'TRADING_LEDGER', 'VENUE_ORDERS', 'VENUE_FILLS',
                'VENUE_POSITIONS', 'VENUE_BALANCES', 'VENUE_PROTECTION',
                'WORKER_LOCAL'
            ) THEN
                RAISE EXCEPTION 'reconciliation source manifest must equal the frozen set';
            END IF;
            SELECT run_id INTO latest_run_id FROM execution_reconciliation_runs
            WHERE scope_id = NEW.scope_id
            ORDER BY started_at DESC, run_id DESC LIMIT 1;
            IF latest_run_id IS NULL AND NEW.supersedes_run_id IS NOT NULL THEN
                RAISE EXCEPTION 'first reconciliation cannot supersede another run';
            ELSIF latest_run_id IS NOT NULL
                AND NEW.supersedes_run_id IS DISTINCT FROM latest_run_id THEN
                RAISE EXCEPTION 'reconciliation must supersede the latest scope run';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER execution_reconciliation_runs_insert_guard
        BEFORE INSERT ON execution_reconciliation_runs
        FOR EACH ROW EXECUTE FUNCTION protect_execution_reconciliation_run_insert()
        """
    )
    op.execute(
        """
        CREATE FUNCTION protect_execution_reconciliation_child_insert()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE state_row execution_reconciliation_run_states%ROWTYPE;
        DECLARE run_row execution_reconciliation_runs%ROWTYPE;
        DECLARE finding_run uuid;
        BEGIN
            SELECT * INTO STRICT state_row FROM execution_reconciliation_run_states
            WHERE run_id = NEW.run_id;
            SELECT * INTO STRICT run_row FROM execution_reconciliation_runs
            WHERE run_id = NEW.run_id;
            IF state_row.status <> 'RUNNING' THEN
                RAISE EXCEPTION 'terminal reconciliation cannot accept child facts';
            END IF;
            IF TG_TABLE_NAME = 'execution_reconciliation_inputs' THEN
                IF state_row.phase <> 'COLLECTING'
                    OR NOT (run_row.required_source_types ? NEW.source_type) THEN
                    RAISE EXCEPTION 'input is outside the active collection manifest';
                END IF;
            ELSIF TG_TABLE_NAME = 'execution_reconciliation_findings' THEN
                IF state_row.phase NOT IN ('COMPARING', 'ADJUSTING') THEN
                    RAISE EXCEPTION 'finding requires comparison phase';
                END IF;
            ELSIF TG_TABLE_NAME = 'execution_reconciliation_finding_resolutions' THEN
                SELECT run_id INTO STRICT finding_run
                FROM execution_reconciliation_findings WHERE finding_id = NEW.finding_id;
                IF state_row.phase <> 'ADJUSTING' OR finding_run <> NEW.run_id THEN
                    RAISE EXCEPTION 'resolution requires matching finding and adjusting phase';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    for table in (
        "execution_reconciliation_inputs",
        "execution_reconciliation_findings",
        "execution_reconciliation_finding_resolutions",
    ):
        op.execute(
            f"""
            CREATE TRIGGER {table}_insert_guard
            BEFORE INSERT ON {table}
            FOR EACH ROW EXECUTE FUNCTION protect_execution_reconciliation_child_insert()
            """
        )
    op.execute(
        """
        CREATE FUNCTION protect_execution_reconciliation_state_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE old_phase_rank integer;
        DECLARE new_phase_rank integer;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'execution_reconciliation_run_states cannot be deleted';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.version <> 1 OR NEW.status <> 'RUNNING'
                    OR NEW.phase <> 'COLLECTING'
                    OR NEW.collected_source_count <> 0 OR NEW.finding_count <> 0
                    OR NEW.unresolved_blocking_count <> 0 OR NEW.completed_at IS NOT NULL
                    OR NEW.result_snapshot IS NOT NULL OR NEW.result_hash IS NOT NULL THEN
                    RAISE EXCEPTION 'invalid initial reconciliation state';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.status <> 'RUNNING' THEN
                RAISE EXCEPTION 'terminal reconciliation state is immutable';
            END IF;
            IF NEW.run_id <> OLD.run_id OR NEW.organization_id <> OLD.organization_id
                OR NEW.scope_id <> OLD.scope_id OR NEW.version <> OLD.version + 1
                OR NEW.updated_at < OLD.updated_at
                OR NEW.collected_source_count < OLD.collected_source_count
                OR NEW.finding_count < OLD.finding_count THEN
                RAISE EXCEPTION 'invalid reconciliation identity, version, time, or count';
            END IF;
            old_phase_rank := CASE OLD.phase
                WHEN 'COLLECTING' THEN 1 WHEN 'COMPARING' THEN 2 ELSE 3 END;
            new_phase_rank := CASE NEW.phase
                WHEN 'COLLECTING' THEN 1 WHEN 'COMPARING' THEN 2 ELSE 3 END;
            IF new_phase_rank < old_phase_rank OR new_phase_rank > old_phase_rank + 1 THEN
                RAISE EXCEPTION 'reconciliation phase cannot move backward or skip';
            END IF;
            IF NEW.status = 'RUNNING' AND (
                NEW.completed_at IS NOT NULL OR NEW.result_snapshot IS NOT NULL
                OR NEW.result_hash IS NOT NULL) THEN
                RAISE EXCEPTION 'running reconciliation cannot have terminal result';
            ELSIF NEW.status IN ('UNKNOWN', 'SUCCEEDED', 'FAILED') AND (
                NEW.completed_at IS NULL OR NEW.result_snapshot IS NULL
                OR NEW.result_hash IS NULL) THEN
                RAISE EXCEPTION 'terminal reconciliation requires result evidence';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER execution_reconciliation_states_transition_guard
        BEFORE INSERT OR UPDATE OR DELETE ON execution_reconciliation_run_states
        FOR EACH ROW EXECUTE FUNCTION protect_execution_reconciliation_state_change()
        """
    )
    op.execute(
        """
        CREATE FUNCTION record_execution_reconciliation_state_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE snapshot jsonb;
        BEGIN
            snapshot := to_jsonb(NEW);
            INSERT INTO execution_reconciliation_run_state_history (
                run_id, scope_id, state_version, status, phase,
                collected_source_count, finding_count, unresolved_blocking_count,
                reason_code, source_ref, state_snapshot, state_hash, changed_at
            ) VALUES (
                NEW.run_id, NEW.scope_id, NEW.version, NEW.status, NEW.phase,
                NEW.collected_source_count, NEW.finding_count,
                NEW.unresolved_blocking_count, NEW.reason_code, NEW.source_ref,
                snapshot, encode(sha256(convert_to(snapshot::text, 'UTF8')), 'hex'),
                NEW.updated_at
            );
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER execution_reconciliation_states_record_history
        AFTER INSERT OR UPDATE ON execution_reconciliation_run_states
        FOR EACH ROW EXECUTE FUNCTION record_execution_reconciliation_state_change()
        """
    )
    op.execute(
        """
        CREATE FUNCTION verify_execution_reconciliation_graph()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE target_run_id uuid;
        DECLARE run_row execution_reconciliation_runs%ROWTYPE;
        DECLARE state_row execution_reconciliation_run_states%ROWTYPE;
        DECLARE sender_state execution_sender_scope_states%ROWTYPE;
        DECLARE input_count integer;
        DECLARE finding_count integer;
        DECLARE unresolved_count integer;
        DECLARE complete_count integer;
        BEGIN
            target_run_id := COALESCE(NEW.run_id, OLD.run_id);
            SELECT * INTO STRICT run_row FROM execution_reconciliation_runs
            WHERE run_id = target_run_id;
            SELECT * INTO STRICT state_row FROM execution_reconciliation_run_states
            WHERE run_id = target_run_id;
            SELECT count(*) INTO input_count FROM execution_reconciliation_inputs
            WHERE run_id = target_run_id;
            SELECT count(*) INTO complete_count FROM execution_reconciliation_inputs
            WHERE run_id = target_run_id AND collection_status = 'COMPLETE';
            SELECT count(*) INTO finding_count FROM execution_reconciliation_findings
            WHERE run_id = target_run_id;
            SELECT count(*) INTO unresolved_count
            FROM execution_reconciliation_findings f
            LEFT JOIN execution_reconciliation_finding_resolutions r
                ON r.finding_id = f.finding_id
            WHERE f.run_id = target_run_id
              AND f.severity IN ('BLOCKING', 'UNKNOWN')
              AND r.finding_id IS NULL;
            IF state_row.organization_id <> run_row.organization_id
                OR state_row.scope_id <> run_row.scope_id
                OR state_row.collected_source_count <> input_count
                OR state_row.finding_count <> finding_count
                OR state_row.unresolved_blocking_count <> unresolved_count THEN
                RAISE EXCEPTION 'reconciliation state counts or binding disagree with facts';
            END IF;
            IF state_row.status IN ('UNKNOWN', 'SUCCEEDED', 'FAILED') AND (
                state_row.result_snapshot ->> 'run_id' <> run_row.run_id::text
                OR state_row.result_snapshot ->> 'scope_id' <> run_row.scope_id
                OR state_row.result_snapshot ->> 'lease_id' <> run_row.lease_id::text
                OR state_row.result_snapshot ->> 'fencing_token' <> run_row.fencing_token::text
                OR state_row.result_snapshot ->> 'status' <> state_row.status
                OR state_row.result_snapshot ->> 'no_historical_replay' <> 'true'
                OR state_row.result_snapshot ->> 'external_send_permitted' <> 'false'
            ) THEN
                RAISE EXCEPTION 'terminal reconciliation result violates frozen binding';
            END IF;
            IF state_row.phase IN ('COMPARING', 'ADJUSTING')
                OR state_row.status = 'SUCCEEDED' THEN
                IF input_count <> jsonb_array_length(run_row.required_source_types)
                    OR complete_count <> input_count
                    OR EXISTS (
                        SELECT 1 FROM jsonb_array_elements_text(run_row.required_source_types) s
                        WHERE NOT EXISTS (
                            SELECT 1 FROM execution_reconciliation_inputs i
                            WHERE i.run_id = target_run_id AND i.source_type = s.value
                        )
                    ) THEN
                    RAISE EXCEPTION 'advanced reconciliation lacks complete frozen inputs';
                END IF;
            END IF;
            IF state_row.status = 'SUCCEEDED' THEN
                SELECT * INTO STRICT sender_state FROM execution_sender_scope_states
                WHERE scope_id = run_row.scope_id;
                IF unresolved_count <> 0
                    OR sender_state.status <> 'LEASED'
                    OR sender_state.active_lease_id <> run_row.lease_id
                    OR sender_state.current_fencing_token <> run_row.fencing_token
                    OR sender_state.lease_expires_at IS NULL
                    OR state_row.completed_at >= sender_state.lease_expires_at THEN
                    RAISE EXCEPTION 'successful reconciliation lacks current fenced authority';
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    for table in (
        "execution_reconciliation_inputs",
        "execution_reconciliation_findings",
        "execution_reconciliation_finding_resolutions",
        "execution_reconciliation_run_states",
    ):
        op.execute(
            f"""
            CREATE CONSTRAINT TRIGGER {table}_graph_guard
            AFTER INSERT OR UPDATE OR DELETE ON {table}
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION verify_execution_reconciliation_graph()
            """
        )


def _drop_reconciliation_guards() -> None:
    for table in (
        "execution_reconciliation_run_states",
        "execution_reconciliation_finding_resolutions",
        "execution_reconciliation_findings",
        "execution_reconciliation_inputs",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_graph_guard ON {table}")
    op.execute("DROP FUNCTION IF EXISTS verify_execution_reconciliation_graph()")
    op.execute(
        "DROP TRIGGER IF EXISTS execution_reconciliation_states_record_history "
        "ON execution_reconciliation_run_states"
    )
    op.execute("DROP FUNCTION IF EXISTS record_execution_reconciliation_state_change()")
    op.execute(
        "DROP TRIGGER IF EXISTS execution_reconciliation_states_transition_guard "
        "ON execution_reconciliation_run_states"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_execution_reconciliation_state_change()")
    for table in (
        "execution_reconciliation_finding_resolutions",
        "execution_reconciliation_findings",
        "execution_reconciliation_inputs",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_insert_guard ON {table}")
    op.execute("DROP FUNCTION IF EXISTS protect_execution_reconciliation_child_insert()")
    op.execute(
        "DROP TRIGGER IF EXISTS execution_reconciliation_runs_insert_guard "
        "ON execution_reconciliation_runs"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_execution_reconciliation_run_insert()")
    for table in (
        "execution_reconciliation_run_state_history",
        "execution_reconciliation_finding_resolutions",
        "execution_reconciliation_findings",
        "execution_reconciliation_inputs",
        "execution_reconciliation_runs",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS deny_reconciliation_fact_change()")


def _replace_claim_guard_with_reconciliation() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS shadow_dispatch_claims_contract_guard ON shadow_dispatch_claims"
    )
    op.execute("DROP FUNCTION IF EXISTS verify_shadow_dispatch_claim()")
    op.execute(_claim_guard_sql(require_reconciliation=True))
    op.execute(
        """
        CREATE TRIGGER shadow_dispatch_claims_contract_guard
        BEFORE INSERT ON shadow_dispatch_claims
        FOR EACH ROW EXECUTE FUNCTION verify_shadow_dispatch_claim()
        """
    )


def _create_legacy_claim_guard() -> None:
    op.execute(_claim_guard_sql(require_reconciliation=False))
    op.execute(
        """
        CREATE TRIGGER shadow_dispatch_claims_contract_guard
        BEFORE INSERT ON shadow_dispatch_claims
        FOR EACH ROW EXECUTE FUNCTION verify_shadow_dispatch_claim()
        """
    )


def _claim_guard_sql(*, require_reconciliation: bool) -> str:
    reconciliation_declarations = (
        ""
        if not require_reconciliation
        else """
        DECLARE reconciliation_run_row execution_reconciliation_runs%ROWTYPE;
        DECLARE reconciliation_state_row execution_reconciliation_run_states%ROWTYPE;
        DECLARE latest_reconciliation_run_id uuid;
    """
    )
    reconciliation_selects = (
        ""
        if not require_reconciliation
        else """
            IF NEW.reconciliation_run_id IS NULL OR NEW.reconciliation_result_hash IS NULL THEN
                RAISE EXCEPTION 'shadow claim requires durable successful reconciliation';
            END IF;
            SELECT * INTO STRICT reconciliation_run_row FROM execution_reconciliation_runs
            WHERE run_id = NEW.reconciliation_run_id;
            SELECT * INTO STRICT reconciliation_state_row
            FROM execution_reconciliation_run_states
            WHERE run_id = NEW.reconciliation_run_id;
            SELECT run_id INTO STRICT latest_reconciliation_run_id
            FROM execution_reconciliation_runs
            WHERE scope_id = NEW.scope_id
              AND lease_id = NEW.lease_id
              AND fencing_token = NEW.fencing_token
            ORDER BY started_at DESC, run_id DESC LIMIT 1;
    """
    )
    reconciliation_checks = (
        ""
        if not require_reconciliation
        else """
                OR reconciliation_run_row.organization_id <> NEW.organization_id
                OR latest_reconciliation_run_id <> NEW.reconciliation_run_id
                OR reconciliation_run_row.scope_id <> NEW.scope_id
                OR reconciliation_run_row.lease_id <> NEW.lease_id
                OR reconciliation_run_row.fencing_token <> NEW.fencing_token
                OR reconciliation_run_row.started_at < lease_row.issued_at
                OR reconciliation_run_row.environment <> 'SHADOW'
                OR reconciliation_run_row.live_dispatch_eligible
                OR reconciliation_state_row.status <> 'SUCCEEDED'
                OR reconciliation_state_row.result_hash <> NEW.reconciliation_result_hash
                OR reconciliation_state_row.completed_at IS NULL
                OR reconciliation_state_row.completed_at > NEW.claimed_at
                OR reconciliation_state_row.result_snapshot ->> 'run_id'
                    <> reconciliation_run_row.run_id::text
                OR reconciliation_state_row.result_snapshot ->> 'scope_id' <> NEW.scope_id
                OR reconciliation_state_row.result_snapshot ->> 'lease_id' <> NEW.lease_id::text
                OR reconciliation_state_row.result_snapshot ->> 'fencing_token'
                    <> NEW.fencing_token::text
                OR reconciliation_state_row.result_snapshot ->> 'status' <> 'SUCCEEDED'
                OR reconciliation_state_row.result_snapshot ->> 'no_historical_replay' <> 'true'
                OR reconciliation_state_row.result_snapshot ->> 'external_send_permitted'
                    <> 'false'
    """
    )
    return f"""
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
        {reconciliation_declarations}
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
            {reconciliation_selects}
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
                OR NEW.live_gate_status <> 'DISABLED'
                {reconciliation_checks} THEN
                RAISE EXCEPTION
                    'shadow dispatch claim violates current fenced non-dispatch contract';
            END IF;
            RETURN NEW;
        END;
        $$
    """
