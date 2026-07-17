"""Create frozen proposal review facts and quorum aggregation state.

Revision ID: 20260718_0003
Revises: 20260718_0002
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0003"
down_revision: str | None = "20260718_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "proposal_versions",
        sa.Column("proposal_version_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("proposal_purpose", sa.String(length=32), nullable=False),
        sa.Column("creator_principal_id", sa.Uuid(), nullable=True),
        sa.Column("creator_service_principal", sa.String(length=255), nullable=True),
        sa.Column("business_owner_principal_id", sa.Uuid(), nullable=True),
        sa.Column("strategy_id", sa.String(length=160), nullable=True),
        sa.Column("strategy_version", sa.String(length=120), nullable=True),
        sa.Column("account_id", sa.String(length=160), nullable=False),
        sa.Column("venue", sa.String(length=80), nullable=False),
        sa.Column("execution_domain", sa.String(length=120), nullable=False),
        sa.Column("instrument_id", sa.String(length=255), nullable=False),
        sa.Column("sector", sa.String(length=80), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("decision_timeframe", sa.String(length=40), nullable=False),
        sa.Column("order_type", sa.String(length=40), nullable=False),
        sa.Column("trigger_price", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("limit_price", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("max_slippage_bps", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("requested_quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("risk_approved_quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("reduce_only", sa.Boolean(), nullable=False),
        sa.Column(
            "initial_invalidation_price",
            sa.Numeric(precision=38, scale=18),
            nullable=False,
        ),
        sa.Column("requested_max_r", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("risk_tier", sa.String(length=20), nullable=False),
        sa.Column("auto_add_enabled", sa.Boolean(), nullable=False),
        sa.Column("requested_add_count", sa.Integer(), nullable=False),
        sa.Column("target_leverage_min", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("target_leverage_max", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("supporting_reason", sa.Text(), nullable=False),
        sa.Column("counter_thesis", sa.Text(), nullable=False),
        sa.Column("data_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("market_state", sa.String(length=80), nullable=False),
        sa.Column("total_capital_snapshot_0", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("funding_envelope_0", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("one_r_0", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("frozen_trade_loss_cap", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("risk_decision_ref", sa.String(length=255), nullable=False),
        sa.Column("risk_precheck_status", sa.String(length=20), nullable=False),
        sa.Column("risk_policy_version", sa.String(length=120), nullable=False),
        sa.Column("catalog_version", sa.String(length=120), nullable=False),
        sa.Column("execution_capability_version", sa.String(length=120), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("spec_hash", sa.String(length=64), nullable=False),
        sa.Column("risk_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("risk_summary_hash", sa.String(length=64), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_proposal_versions_version_positive"),
        sa.CheckConstraint("source IN ('MANUAL', 'SYSTEM')", name="ck_proposal_versions_source"),
        sa.CheckConstraint(
            "proposal_purpose IN ('INITIAL_ENTRY', 'REDUCE_ONLY')",
            name="ck_proposal_versions_purpose",
        ),
        sa.CheckConstraint(
            "risk_tier IN ('LOW', 'MEDIUM', 'HIGH')",
            name="ck_proposal_versions_risk_tier",
        ),
        sa.CheckConstraint("direction IN ('LONG', 'SHORT')", name="ck_proposal_versions_direction"),
        sa.CheckConstraint(
            "(proposal_purpose = 'INITIAL_ENTRY' AND reduce_only = false) OR "
            "(proposal_purpose = 'REDUCE_ONLY' AND reduce_only = true "
            "AND auto_add_enabled = false AND requested_add_count = 0)",
            name="ck_proposal_versions_reduce_only_contract",
        ),
        sa.CheckConstraint(
            "requested_quantity > 0 AND risk_approved_quantity > 0 "
            "AND risk_approved_quantity <= requested_quantity",
            name="ck_proposal_versions_quantities_positive",
        ),
        sa.CheckConstraint(
            "max_slippage_bps >= 0 AND initial_invalidation_price > 0",
            name="ck_proposal_versions_price_controls",
        ),
        sa.CheckConstraint(
            "requested_max_r > 0 AND requested_max_r <= 3",
            name="ck_proposal_versions_requested_r_range",
        ),
        sa.CheckConstraint(
            "(risk_tier = 'LOW' AND requested_max_r <= 1 AND requested_add_count <= 1) OR "
            "(risk_tier = 'MEDIUM' AND requested_max_r <= 2 AND requested_add_count <= 2) OR "
            "(risk_tier = 'HIGH' AND requested_max_r <= 3 AND requested_add_count <= 3)",
            name="ck_proposal_versions_tier_caps",
        ),
        sa.CheckConstraint(
            "requested_add_count >= 0 AND "
            "((auto_add_enabled = false AND requested_add_count = 0) OR "
            "auto_add_enabled = true)",
            name="ck_proposal_versions_auto_add_consistency",
        ),
        sa.CheckConstraint(
            "total_capital_snapshot_0 > 0 AND funding_envelope_0 >= 0 AND "
            "one_r_0 > 0 AND frozen_trade_loss_cap > 0 "
            "AND funding_envelope_0 <= total_capital_snapshot_0",
            name="ck_proposal_versions_frozen_risk_positive",
        ),
        sa.CheckConstraint(
            "one_r_0 = total_capital_snapshot_0 * 0.005",
            name="ck_proposal_versions_one_r_formula",
        ),
        sa.CheckConstraint(
            "(risk_tier = 'LOW' AND frozen_trade_loss_cap = one_r_0) OR "
            "(risk_tier = 'MEDIUM' AND frozen_trade_loss_cap = one_r_0 * 2) OR "
            "(risk_tier = 'HIGH' AND frozen_trade_loss_cap = one_r_0 * 3)",
            name="ck_proposal_versions_loss_cap_formula",
        ),
        sa.CheckConstraint(
            "target_leverage_min > 0 AND target_leverage_max >= target_leverage_min AND "
            "((risk_tier = 'LOW' AND target_leverage_max <= 3) OR "
            "(risk_tier = 'MEDIUM' AND target_leverage_max <= 5) OR "
            "(risk_tier = 'HIGH' AND target_leverage_max <= 10))",
            name="ck_proposal_versions_leverage_caps",
        ),
        sa.CheckConstraint(
            "valid_until > valid_from AND frozen_at >= valid_from AND frozen_at < valid_until",
            name="ck_proposal_versions_valid_window",
        ),
        sa.CheckConstraint(
            "length(spec_hash) = 64 AND length(risk_summary_hash) = 64",
            name="ck_proposal_versions_hash_lengths",
        ),
        sa.CheckConstraint(
            "risk_precheck_status = 'PASSED'",
            name="ck_proposal_versions_risk_precheck_passed",
        ),
        sa.CheckConstraint(
            "(source = 'MANUAL' AND creator_principal_id IS NOT NULL "
            "AND creator_service_principal IS NULL) OR "
            "(source = 'SYSTEM' AND creator_principal_id IS NULL "
            "AND creator_service_principal IS NOT NULL "
            "AND business_owner_principal_id IS NOT NULL "
            "AND strategy_id IS NOT NULL AND strategy_version IS NOT NULL)",
            name="ck_proposal_versions_creator_contract",
        ),
        sa.PrimaryKeyConstraint("proposal_version_id"),
        sa.UniqueConstraint("proposal_id", "version", name="uq_proposal_versions_root_version"),
    )
    op.create_index(
        "ix_proposal_versions_review_queue",
        "proposal_versions",
        ["organization_id", "valid_until"],
    )

    op.create_table(
        "proposal_version_states",
        sa.Column("proposal_version_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=160), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('FROZEN', 'SUPERSEDED', 'EXPIRED', 'CANCELLED')",
            name="ck_proposal_version_states_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_proposal_version_states_version_positive"),
        sa.ForeignKeyConstraint(
            ["proposal_version_id"],
            ["proposal_versions.proposal_version_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("proposal_version_id"),
    )

    op.create_table(
        "system_risk_states",
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=160), nullable=False),
        sa.Column("policy_version", sa.String(length=120), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('NORMAL', 'NO_NEW_POSITION', 'NO_PYRAMID', "
            "'REDUCE_ONLY', 'KILL_SWITCH', 'UNKNOWN')",
            name="ck_system_risk_states_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_system_risk_states_version_positive"),
        sa.PrimaryKeyConstraint("organization_id"),
    )

    op.create_table(
        "approval_decisions",
        sa.Column("approval_decision_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_version_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("required_quorum", sa.Integer(), nullable=False),
        sa.Column("approved_count", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("terminal_reason_code", sa.String(length=160), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'RETURNED', 'EXPIRED', 'ABANDONED')",
            name="ck_approval_decisions_status",
        ),
        sa.CheckConstraint("required_quorum IN (1, 2)", name="ck_approval_decisions_quorum"),
        sa.CheckConstraint(
            "approved_count >= 0 AND approved_count <= required_quorum",
            name="ck_approval_decisions_approved_count",
        ),
        sa.CheckConstraint("version >= 1", name="ck_approval_decisions_version_positive"),
        sa.CheckConstraint(
            "(status = 'PENDING' AND terminal_reason_code IS NULL AND terminal_at IS NULL) OR "
            "(status <> 'PENDING' AND terminal_reason_code IS NOT NULL "
            "AND terminal_at IS NOT NULL)",
            name="ck_approval_decisions_terminal_fields",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_version_id"],
            ["proposal_versions.proposal_version_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("approval_decision_id"),
        sa.UniqueConstraint("proposal_version_id", name="uq_approval_decisions_proposal_version"),
    )

    op.create_table(
        "reviewer_votes",
        sa.Column("vote_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_version_id", sa.Uuid(), nullable=False),
        sa.Column("reviewer_principal_id", sa.Uuid(), nullable=False),
        sa.Column("choice", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("authorization_decision_id", sa.Uuid(), nullable=False),
        sa.Column("auth_context_ref", sa.String(length=255), nullable=False),
        sa.Column("risk_summary_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=120), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "choice IN ('APPROVE', 'REJECT', 'RETURN')",
            name="ck_reviewer_votes_choice",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_version_id"],
            ["proposal_versions.proposal_version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_principal_id"],
            ["identity_principals.principal_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["authorization_decision_id"],
            ["authorization_decisions.decision_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("vote_id"),
        sa.UniqueConstraint(
            "proposal_version_id",
            "reviewer_principal_id",
            name="uq_reviewer_votes_reviewer_version",
        ),
    )
    op.create_index(
        "ix_reviewer_votes_proposal",
        "reviewer_votes",
        ["proposal_version_id", "decided_at"],
    )

    op.execute(
        """
        CREATE FUNCTION deny_frozen_proposal_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'proposal_versions is immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER proposal_versions_immutable
        BEFORE UPDATE OR DELETE ON proposal_versions
        FOR EACH ROW EXECUTE FUNCTION deny_frozen_proposal_change()
        """
    )
    op.execute(
        """
        CREATE FUNCTION protect_proposal_version_state_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'proposal_version_states cannot be deleted';
            END IF;
            IF OLD.status <> 'FROZEN' THEN
                RAISE EXCEPTION 'terminal proposal_version_state is immutable';
            END IF;
            IF NEW.status = 'FROZEN' THEN
                RAISE EXCEPTION 'proposal_version_state cannot remain frozen on transition';
            END IF;
            IF NEW.proposal_version_id <> OLD.proposal_version_id
               OR NEW.version <> OLD.version + 1 THEN
                RAISE EXCEPTION 'invalid proposal_version_state transition';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER proposal_version_states_transition_guard
        BEFORE UPDATE OR DELETE ON proposal_version_states
        FOR EACH ROW EXECUTE FUNCTION protect_proposal_version_state_transition()
        """
    )
    op.execute(
        """
        CREATE FUNCTION deny_reviewer_vote_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'reviewer_votes is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER reviewer_votes_append_only
        BEFORE UPDATE OR DELETE ON reviewer_votes
        FOR EACH ROW EXECUTE FUNCTION deny_reviewer_vote_change()
        """
    )
    op.execute(
        """
        CREATE FUNCTION protect_approval_decision_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'approval_decisions cannot be deleted';
            END IF;
            IF OLD.status <> 'PENDING' THEN
                RAISE EXCEPTION 'terminal approval_decision is immutable';
            END IF;
            IF NEW.approval_decision_id <> OLD.approval_decision_id
               OR NEW.proposal_version_id <> OLD.proposal_version_id
               OR NEW.required_quorum <> OLD.required_quorum
               OR NEW.valid_until <> OLD.valid_until
               OR NEW.created_at <> OLD.created_at THEN
                RAISE EXCEPTION 'approval_decision identity is immutable';
            END IF;
            IF NEW.version <> OLD.version + 1 THEN
                RAISE EXCEPTION 'approval_decision version must increase by one';
            END IF;
            IF NEW.approved_count < OLD.approved_count THEN
                RAISE EXCEPTION 'approval count cannot decrease';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER approval_decisions_transition_guard
        BEFORE UPDATE OR DELETE ON approval_decisions
        FOR EACH ROW EXECUTE FUNCTION protect_approval_decision_transition()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS approval_decisions_transition_guard ON approval_decisions")
    op.execute("DROP FUNCTION IF EXISTS protect_approval_decision_transition()")
    op.execute("DROP TRIGGER IF EXISTS reviewer_votes_append_only ON reviewer_votes")
    op.execute("DROP FUNCTION IF EXISTS deny_reviewer_vote_change()")
    op.execute(
        "DROP TRIGGER IF EXISTS proposal_version_states_transition_guard ON proposal_version_states"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_proposal_version_state_transition()")
    op.execute("DROP TRIGGER IF EXISTS proposal_versions_immutable ON proposal_versions")
    op.execute("DROP FUNCTION IF EXISTS deny_frozen_proposal_change()")
    op.drop_index("ix_reviewer_votes_proposal", table_name="reviewer_votes")
    op.drop_table("reviewer_votes")
    op.drop_table("approval_decisions")
    op.drop_table("system_risk_states")
    op.drop_table("proposal_version_states")
    op.drop_index("ix_proposal_versions_review_queue", table_name="proposal_versions")
    op.drop_table("proposal_versions")
