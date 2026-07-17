"""Add immutable risk policies/decisions and monotonic system-risk history.

Revision ID: 20260718_0004
Revises: 20260718_0003
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0004"
down_revision: str | None = "20260718_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "risk_policies",
        sa.Column("risk_policy_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("policy_version", sa.String(length=120), nullable=False),
        sa.Column("policy_mode", sa.String(length=20), nullable=False),
        sa.Column(
            "parameters",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("policy_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "evidence_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(policy_hash) = 64", name="ck_risk_policies_hash_length"),
        sa.CheckConstraint("policy_mode = 'SHADOW'", name="ck_risk_policies_shadow_only"),
        sa.CheckConstraint("valid_until > valid_from", name="ck_risk_policies_valid_window"),
        sa.CheckConstraint(
            "jsonb_typeof(parameters) = 'object'",
            name="ck_risk_policies_parameters_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) > 0",
            name="ck_risk_policies_evidence_nonempty",
        ),
        sa.PrimaryKeyConstraint("risk_policy_id"),
        sa.UniqueConstraint(
            "organization_id",
            "policy_version",
            name="uq_risk_policies_organization_version",
        ),
        sa.UniqueConstraint(
            "risk_policy_id",
            "organization_id",
            "policy_version",
            name="uq_risk_policies_identity_binding",
        ),
    )
    op.create_index(
        "ix_risk_policies_lookup",
        "risk_policies",
        ["organization_id", "policy_version"],
    )

    op.create_table(
        "risk_decision_snapshots",
        sa.Column("risk_decision_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("proposal_ref", sa.String(length=255), nullable=False),
        sa.Column("decision_stage", sa.String(length=40), nullable=False),
        sa.Column("result", sa.String(length=20), nullable=False),
        sa.Column("primary_reason_code", sa.String(length=160), nullable=False),
        sa.Column("risk_tier", sa.String(length=20), nullable=False),
        sa.Column("system_risk_state", sa.String(length=32), nullable=False),
        sa.Column("risk_policy_id", sa.Uuid(), nullable=False),
        sa.Column("risk_policy_version", sa.String(length=120), nullable=False),
        sa.Column("requested_quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("max_safe_quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("final_quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("current_unrealized_pnl", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column(
            "current_portfolio_mtm_equity",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column(
            "total_capital_snapshot_0",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column("one_r_0", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column(
            "frozen_trade_loss_cap",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column(
            "dynamic_trade_loss_cap",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column(
            "effective_trade_loss_cap",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column(
            "trade_worst_case_loss_before",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column(
            "trade_worst_case_loss_after",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column(
            "input_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "decision",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("decision_hash", sa.String(length=64), nullable=False),
        sa.Column("execution_eligible", sa.Boolean(), nullable=False),
        sa.Column("reservation_created", sa.Boolean(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision_stage = 'PROPOSAL_PRECHECK'",
            name="ck_risk_decisions_precheck_only",
        ),
        sa.CheckConstraint(
            "result IN ('ALLOW', 'DENY')",
            name="ck_risk_decisions_result",
        ),
        sa.CheckConstraint(
            "risk_tier IN ('LOW', 'MEDIUM', 'HIGH')",
            name="ck_risk_decisions_tier",
        ),
        sa.CheckConstraint(
            "system_risk_state IN ('NORMAL', 'NO_PYRAMID', 'NO_NEW_POSITION', "
            "'REDUCE_ONLY', 'KILL_SWITCH', 'UNKNOWN')",
            name="ck_risk_decisions_system_state",
        ),
        sa.CheckConstraint(
            "requested_quantity > 0 AND max_safe_quantity >= 0 AND final_quantity >= 0 "
            "AND max_safe_quantity <= requested_quantity "
            "AND final_quantity <= requested_quantity",
            name="ck_risk_decisions_quantity_bounds",
        ),
        sa.CheckConstraint(
            "(result = 'ALLOW' AND max_safe_quantity = requested_quantity "
            "AND final_quantity = requested_quantity) OR "
            "(result = 'DENY' AND final_quantity = 0)",
            name="ck_risk_decisions_result_quantities",
        ),
        sa.CheckConstraint(
            "total_capital_snapshot_0 > 0 AND one_r_0 > 0 "
            "AND frozen_trade_loss_cap > 0 AND dynamic_trade_loss_cap >= 0 "
            "AND effective_trade_loss_cap >= 0 AND trade_worst_case_loss_before >= 0 "
            "AND trade_worst_case_loss_after >= trade_worst_case_loss_before",
            name="ck_risk_decisions_loss_bounds",
        ),
        sa.CheckConstraint(
            "one_r_0 = total_capital_snapshot_0 * 0.005",
            name="ck_risk_decisions_one_r_formula",
        ),
        sa.CheckConstraint(
            "(risk_tier = 'LOW' AND frozen_trade_loss_cap = one_r_0) OR "
            "(risk_tier = 'MEDIUM' AND frozen_trade_loss_cap = one_r_0 * 2) OR "
            "(risk_tier = 'HIGH' AND frozen_trade_loss_cap = one_r_0 * 3)",
            name="ck_risk_decisions_tier_loss_formula",
        ),
        sa.CheckConstraint(
            "execution_eligible = false AND reservation_created = false",
            name="ck_risk_decisions_no_execution_side_effect",
        ),
        sa.CheckConstraint(
            "length(input_hash) = 64 AND length(decision_hash) = 64",
            name="ck_risk_decisions_hash_lengths",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(input_snapshot) = 'object' AND jsonb_typeof(decision) = 'object'",
            name="ck_risk_decisions_json_objects",
        ),
        sa.CheckConstraint(
            "(result = 'ALLOW' AND valid_until >= decided_at) OR "
            "(result = 'DENY' AND valid_until = decided_at)",
            name="ck_risk_decisions_validity",
        ),
        sa.ForeignKeyConstraint(
            ["risk_policy_id", "organization_id", "risk_policy_version"],
            [
                "risk_policies.risk_policy_id",
                "risk_policies.organization_id",
                "risk_policies.policy_version",
            ],
            name="fk_risk_decisions_policy_binding",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("risk_decision_id"),
    )
    op.create_index(
        "ix_risk_decisions_proposal",
        "risk_decision_snapshots",
        ["organization_id", "proposal_ref", "decided_at"],
    )
    op.create_index(
        "ix_risk_decisions_result",
        "risk_decision_snapshots",
        ["organization_id", "result", "decided_at"],
    )

    op.add_column(
        "system_risk_states",
        sa.Column(
            "transition_source_ref",
            sa.String(length=255),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE system_risk_states
        SET transition_source_ref = 'WP-0004:legacy-initial-state'
        WHERE transition_source_ref IS NULL
        """
    )
    op.alter_column(
        "system_risk_states",
        "transition_source_ref",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.create_table(
        "system_risk_state_transitions",
        sa.Column("transition_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=False),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("transition_kind", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=160), nullable=False),
        sa.Column("policy_version", sa.String(length=120), nullable=False),
        sa.Column("source_ref", sa.String(length=255), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "from_status IN ('NORMAL', 'NO_PYRAMID', 'NO_NEW_POSITION', "
            "'REDUCE_ONLY', 'KILL_SWITCH', 'UNKNOWN')",
            name="ck_system_risk_transitions_from_status",
        ),
        sa.CheckConstraint(
            "to_status IN ('NORMAL', 'NO_PYRAMID', 'NO_NEW_POSITION', "
            "'REDUCE_ONLY', 'KILL_SWITCH', 'UNKNOWN')",
            name="ck_system_risk_transitions_to_status",
        ),
        sa.CheckConstraint(
            "transition_kind IN ('INITIAL', 'AUTOMATIC_TIGHTEN')",
            name="ck_system_risk_transitions_kind",
        ),
        sa.CheckConstraint(
            "state_version >= 1",
            name="ck_system_risk_transitions_version",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["system_risk_states.organization_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("transition_id"),
        sa.UniqueConstraint(
            "organization_id",
            "state_version",
            name="uq_system_risk_transitions_version",
        ),
    )
    op.create_index(
        "ix_system_risk_transitions_org_time",
        "system_risk_state_transitions",
        ["organization_id", "changed_at"],
    )

    op.execute(
        """
        INSERT INTO system_risk_state_transitions (
            organization_id,
            from_status,
            to_status,
            state_version,
            transition_kind,
            reason_code,
            policy_version,
            source_ref,
            changed_at
        )
        SELECT
            organization_id,
            status,
            status,
            version,
            'INITIAL',
            reason_code,
            policy_version,
            transition_source_ref,
            updated_at
        FROM system_risk_states
        """
    )

    op.execute(
        """
        CREATE FUNCTION deny_risk_policy_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'risk_policies is immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER risk_policies_immutable
        BEFORE UPDATE OR DELETE ON risk_policies
        FOR EACH ROW EXECUTE FUNCTION deny_risk_policy_change()
        """
    )
    op.execute(
        """
        CREATE FUNCTION deny_risk_decision_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'risk_decision_snapshots is immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER risk_decision_snapshots_immutable
        BEFORE UPDATE OR DELETE ON risk_decision_snapshots
        FOR EACH ROW EXECUTE FUNCTION deny_risk_decision_change()
        """
    )
    op.execute(
        """
        CREATE FUNCTION deny_system_risk_transition_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'system_risk_state_transitions is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER system_risk_state_transitions_append_only
        BEFORE UPDATE OR DELETE ON system_risk_state_transitions
        FOR EACH ROW EXECUTE FUNCTION deny_system_risk_transition_change()
        """
    )
    op.execute(
        """
        CREATE FUNCTION protect_system_risk_state_tightening()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            old_rank integer;
            new_rank integer;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'system_risk_states cannot be deleted';
            END IF;
            old_rank := CASE OLD.status
                WHEN 'NORMAL' THEN 0
                WHEN 'NO_PYRAMID' THEN 1
                WHEN 'NO_NEW_POSITION' THEN 2
                WHEN 'REDUCE_ONLY' THEN 3
                WHEN 'KILL_SWITCH' THEN 4
                WHEN 'UNKNOWN' THEN 5
            END;
            new_rank := CASE NEW.status
                WHEN 'NORMAL' THEN 0
                WHEN 'NO_PYRAMID' THEN 1
                WHEN 'NO_NEW_POSITION' THEN 2
                WHEN 'REDUCE_ONLY' THEN 3
                WHEN 'KILL_SWITCH' THEN 4
                WHEN 'UNKNOWN' THEN 5
            END;
            IF NEW.organization_id <> OLD.organization_id
               OR NEW.version <> OLD.version + 1
               OR new_rank <= old_rank
               OR length(NEW.transition_source_ref) = 0
               OR NEW.updated_at < OLD.updated_at THEN
                RAISE EXCEPTION 'invalid automatic system_risk_state transition';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER system_risk_states_tightening_guard
        BEFORE UPDATE OR DELETE ON system_risk_states
        FOR EACH ROW EXECUTE FUNCTION protect_system_risk_state_tightening()
        """
    )
    op.execute(
        """
        CREATE FUNCTION record_system_risk_state_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            INSERT INTO system_risk_state_transitions (
                organization_id,
                from_status,
                to_status,
                state_version,
                transition_kind,
                reason_code,
                policy_version,
                source_ref,
                changed_at
            ) VALUES (
                NEW.organization_id,
                CASE WHEN TG_OP = 'INSERT' THEN NEW.status ELSE OLD.status END,
                NEW.status,
                NEW.version,
                CASE WHEN TG_OP = 'INSERT' THEN 'INITIAL' ELSE 'AUTOMATIC_TIGHTEN' END,
                NEW.reason_code,
                NEW.policy_version,
                NEW.transition_source_ref,
                NEW.updated_at
            );
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER system_risk_states_record_transition
        AFTER INSERT OR UPDATE ON system_risk_states
        FOR EACH ROW EXECUTE FUNCTION record_system_risk_state_transition()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS system_risk_states_record_transition ON system_risk_states")
    op.execute("DROP FUNCTION IF EXISTS record_system_risk_state_transition()")
    op.execute("DROP TRIGGER IF EXISTS system_risk_states_tightening_guard ON system_risk_states")
    op.execute("DROP FUNCTION IF EXISTS protect_system_risk_state_tightening()")
    op.execute(
        "DROP TRIGGER IF EXISTS system_risk_state_transitions_append_only "
        "ON system_risk_state_transitions"
    )
    op.execute("DROP FUNCTION IF EXISTS deny_system_risk_transition_change()")
    op.execute(
        "DROP TRIGGER IF EXISTS risk_decision_snapshots_immutable ON risk_decision_snapshots"
    )
    op.execute("DROP FUNCTION IF EXISTS deny_risk_decision_change()")
    op.execute("DROP TRIGGER IF EXISTS risk_policies_immutable ON risk_policies")
    op.execute("DROP FUNCTION IF EXISTS deny_risk_policy_change()")
    op.drop_index(
        "ix_system_risk_transitions_org_time",
        table_name="system_risk_state_transitions",
    )
    op.drop_table("system_risk_state_transitions")
    op.drop_column("system_risk_states", "transition_source_ref")
    op.drop_index("ix_risk_decisions_result", table_name="risk_decision_snapshots")
    op.drop_index("ix_risk_decisions_proposal", table_name="risk_decision_snapshots")
    op.drop_table("risk_decision_snapshots")
    op.drop_index("ix_risk_policies_lookup", table_name="risk_policies")
    op.drop_table("risk_policies")
