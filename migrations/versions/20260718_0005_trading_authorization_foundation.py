"""Add immutable TradingAuthorization and Campaign authorization state machines.

Revision ID: 20260718_0005
Revises: 20260718_0004
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0005"
down_revision: str | None = "20260718_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_approval_decisions_identity_binding",
        "approval_decisions",
        ["approval_decision_id", "proposal_version_id"],
    )
    op.create_table(
        "trading_authorizations",
        sa.Column("authorization_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_version_id", sa.Uuid(), nullable=False),
        sa.Column("approval_decision_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("risk_tier", sa.String(length=20), nullable=False),
        sa.Column("authorized_loss_capacity", sa.Numeric(38, 18), nullable=False),
        sa.Column("approved_initial_quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("auto_add_enabled", sa.Boolean(), nullable=False),
        sa.Column("requested_add_count", sa.Integer(), nullable=False),
        sa.Column("total_capital_snapshot_0", sa.Numeric(38, 18), nullable=False),
        sa.Column("one_r_0", sa.Numeric(38, 18), nullable=False),
        sa.Column("frozen_trade_loss_cap", sa.Numeric(38, 18), nullable=False),
        sa.Column("funding_envelope_0", sa.Numeric(38, 18), nullable=False),
        sa.Column("risk_policy_version", sa.String(length=120), nullable=False),
        sa.Column("authorization_policy_version", sa.String(length=120), nullable=False),
        sa.Column("catalog_version", sa.String(length=120), nullable=False),
        sa.Column("execution_capability_version", sa.String(length=120), nullable=False),
        sa.Column("capability_certificate_ref", sa.String(length=255), nullable=False),
        sa.Column("proposal_spec_hash", sa.String(length=64), nullable=False),
        sa.Column("risk_summary_hash", sa.String(length=64), nullable=False),
        sa.Column("authorization_mode", sa.String(length=20), nullable=False),
        sa.Column("execution_eligible", sa.Boolean(), nullable=False),
        sa.Column("issuance_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("issuance_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source IN ('MANUAL', 'SYSTEM')", name="ck_trading_auth_source"),
        sa.CheckConstraint("risk_tier IN ('LOW', 'MEDIUM', 'HIGH')", name="ck_trading_auth_tier"),
        sa.CheckConstraint(
            "authorized_loss_capacity > 0 AND approved_initial_quantity > 0 "
            "AND total_capital_snapshot_0 > 0 AND one_r_0 > 0 "
            "AND frozen_trade_loss_cap > 0 AND funding_envelope_0 >= 0 "
            "AND authorized_loss_capacity <= frozen_trade_loss_cap",
            name="ck_trading_auth_capacity_bounds",
        ),
        sa.CheckConstraint(
            "one_r_0 = total_capital_snapshot_0 * 0.005",
            name="ck_trading_auth_one_r_formula",
        ),
        sa.CheckConstraint(
            "(risk_tier = 'LOW' AND frozen_trade_loss_cap = one_r_0) OR "
            "(risk_tier = 'MEDIUM' AND frozen_trade_loss_cap = one_r_0 * 2) OR "
            "(risk_tier = 'HIGH' AND frozen_trade_loss_cap = one_r_0 * 3)",
            name="ck_trading_auth_loss_cap_formula",
        ),
        sa.CheckConstraint(
            "requested_add_count >= 0 AND requested_add_count <= 3 AND "
            "((auto_add_enabled = false AND requested_add_count = 0) OR "
            "(auto_add_enabled = true AND requested_add_count > 0))",
            name="ck_trading_auth_add_contract",
        ),
        sa.CheckConstraint(
            "authorization_mode = 'SHADOW' AND execution_eligible = false",
            name="ck_trading_auth_shadow_only",
        ),
        sa.CheckConstraint(
            "length(proposal_spec_hash) = 64 AND length(risk_summary_hash) = 64 "
            "AND length(issuance_snapshot_hash) = 64",
            name="ck_trading_auth_hash_lengths",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(issuance_snapshot) = 'object'",
            name="ck_trading_auth_snapshot_object",
        ),
        sa.CheckConstraint("valid_until > issued_at", name="ck_trading_auth_valid_window"),
        sa.ForeignKeyConstraint(
            ["proposal_version_id"],
            ["proposal_versions.proposal_version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approval_decision_id", "proposal_version_id"],
            [
                "approval_decisions.approval_decision_id",
                "approval_decisions.proposal_version_id",
            ],
            name="fk_trading_auth_approval_proposal_binding",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("authorization_id"),
        sa.UniqueConstraint("proposal_version_id", name="uq_trading_auth_proposal_version"),
        sa.UniqueConstraint("approval_decision_id", name="uq_trading_auth_approval_decision"),
        sa.UniqueConstraint(
            "authorization_id",
            "proposal_version_id",
            name="uq_trading_auth_identity_binding",
        ),
    )
    op.create_index(
        "ix_trading_auth_org_issued",
        "trading_authorizations",
        ["organization_id", "issued_at"],
    )

    op.create_table(
        "campaigns",
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("authorization_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_version_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("strategy_id", sa.String(length=160), nullable=False),
        sa.Column("strategy_version", sa.String(length=120), nullable=False),
        sa.Column("venue", sa.String(length=80), nullable=False),
        sa.Column("execution_domain", sa.String(length=120), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=False),
        sa.Column("instrument_id", sa.String(length=255), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("one_r_0", sa.Numeric(38, 18), nullable=False),
        sa.Column("funding_envelope_0", sa.Numeric(38, 18), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("direction IN ('LONG', 'SHORT')", name="ck_campaigns_direction"),
        sa.CheckConstraint("one_r_0 > 0", name="ck_campaigns_one_r_positive"),
        sa.ForeignKeyConstraint(
            ["authorization_id", "proposal_version_id"],
            [
                "trading_authorizations.authorization_id",
                "trading_authorizations.proposal_version_id",
            ],
            name="fk_campaigns_authorization_proposal_binding",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("campaign_id"),
        sa.UniqueConstraint("authorization_id", name="uq_campaigns_authorization"),
        sa.UniqueConstraint(
            "campaign_id", "authorization_id", name="uq_campaigns_identity_binding"
        ),
    )
    op.create_index("ix_campaigns_org_created", "campaigns", ["organization_id", "created_at"])

    op.create_table(
        "campaign_states",
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=160), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING_ENTRY', 'OPEN', 'CLOSING', 'CLOSED')",
            name="ck_campaign_states_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_campaign_states_version"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.campaign_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("campaign_id"),
    )

    op.create_table(
        "initial_order_authorizations",
        sa.Column("initial_authorization_id", sa.Uuid(), nullable=False),
        sa.Column("authorization_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=False),
        sa.Column("account_abstraction", sa.String(length=80), nullable=False),
        sa.Column("margin_mode", sa.String(length=80), nullable=False),
        sa.Column("collateral_scope", sa.String(length=120), nullable=False),
        sa.Column("collateral_pool_id", sa.String(length=160), nullable=False),
        sa.Column("instrument_id", sa.String(length=255), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("max_quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("authorized_loss_capacity", sa.Numeric(38, 18), nullable=False),
        sa.Column("price_reference", sa.Numeric(38, 18), nullable=False),
        sa.Column("price_lower_bound", sa.Numeric(38, 18), nullable=False),
        sa.Column("price_upper_bound", sa.Numeric(38, 18), nullable=False),
        sa.Column("position_management_template_version", sa.String(length=120), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("direction IN ('LONG', 'SHORT')", name="ck_initial_auth_direction"),
        sa.CheckConstraint(
            "max_quantity > 0 AND authorized_loss_capacity > 0",
            name="ck_initial_auth_capacity",
        ),
        sa.CheckConstraint(
            "price_reference > 0 AND price_lower_bound > 0 "
            "AND price_upper_bound >= price_lower_bound "
            "AND price_reference BETWEEN price_lower_bound AND price_upper_bound",
            name="ck_initial_auth_price_bounds",
        ),
        sa.CheckConstraint("valid_until > valid_from", name="ck_initial_auth_valid_window"),
        sa.ForeignKeyConstraint(
            ["campaign_id", "authorization_id"],
            ["campaigns.campaign_id", "campaigns.authorization_id"],
            name="fk_initial_auth_campaign_authorization_binding",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("initial_authorization_id"),
        sa.UniqueConstraint("authorization_id", name="uq_initial_auth_root"),
        sa.UniqueConstraint("campaign_id", name="uq_initial_auth_campaign"),
    )

    op.create_table(
        "initial_authorization_states",
        sa.Column("initial_authorization_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=160), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'CONSUMED', 'EXPIRED', 'REVOKED', 'INVALIDATED')",
            name="ck_initial_auth_states_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_initial_auth_states_version"),
        sa.ForeignKeyConstraint(
            ["initial_authorization_id"],
            ["initial_order_authorizations.initial_authorization_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("initial_authorization_id"),
    )

    op.create_table(
        "add_authorization_packages",
        sa.Column("add_package_id", sa.Uuid(), nullable=False),
        sa.Column("authorization_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("authorized_add_count", sa.Integer(), nullable=False),
        sa.Column("target_leverage_min", sa.Numeric(18, 8), nullable=False),
        sa.Column("target_leverage_max", sa.Numeric(18, 8), nullable=False),
        sa.Column("add_milestone_policy_version", sa.String(length=120), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("direction IN ('LONG', 'SHORT')", name="ck_add_packages_direction"),
        sa.CheckConstraint("authorized_add_count BETWEEN 1 AND 3", name="ck_add_packages_count"),
        sa.CheckConstraint(
            "target_leverage_min > 0 AND target_leverage_max >= target_leverage_min",
            name="ck_add_packages_leverage",
        ),
        sa.CheckConstraint("valid_until > valid_from", name="ck_add_packages_valid_window"),
        sa.ForeignKeyConstraint(
            ["campaign_id", "authorization_id"],
            ["campaigns.campaign_id", "campaigns.authorization_id"],
            name="fk_add_packages_campaign_authorization_binding",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("add_package_id"),
        sa.UniqueConstraint("authorization_id", name="uq_add_packages_root"),
        sa.UniqueConstraint("campaign_id", name="uq_add_packages_campaign"),
    )

    op.create_table(
        "add_authorization_package_states",
        sa.Column("add_package_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=160), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('DORMANT', 'ACTIVE', 'EXHAUSTED', 'REVOKED', 'EXPIRED', 'INVALIDATED')",
            name="ck_add_package_states_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_add_package_states_version"),
        sa.ForeignKeyConstraint(
            ["add_package_id"],
            ["add_authorization_packages.add_package_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("add_package_id"),
    )

    op.create_table(
        "add_units",
        sa.Column("add_unit_id", sa.Uuid(), nullable=False),
        sa.Column("add_package_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("unlock_milestone_pct", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("ordinal BETWEEN 1 AND 3", name="ck_add_units_ordinal"),
        sa.CheckConstraint(
            "(ordinal = 1 AND unlock_milestone_pct = 30) OR "
            "(ordinal = 2 AND unlock_milestone_pct = 50) OR "
            "(ordinal = 3 AND unlock_milestone_pct = 100)",
            name="ck_add_units_milestone",
        ),
        sa.ForeignKeyConstraint(
            ["add_package_id"],
            ["add_authorization_packages.add_package_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("add_unit_id"),
        sa.UniqueConstraint("add_package_id", "ordinal", name="uq_add_units_package_ordinal"),
    )

    op.create_table(
        "add_unit_states",
        sa.Column("add_unit_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=160), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('AVAILABLE', 'CLAIMED', 'CONSUMED', 'EXPIRED', 'INVALIDATED')",
            name="ck_add_unit_states_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_add_unit_states_version"),
        sa.ForeignKeyConstraint(["add_unit_id"], ["add_units.add_unit_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("add_unit_id"),
    )

    op.create_table(
        "authorization_state_transitions",
        sa.Column("transition_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("authorization_id", sa.Uuid(), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=False),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=160), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "subject_type IN ('CAMPAIGN', 'INITIAL', 'ADD_PACKAGE', 'ADD_UNIT')",
            name="ck_auth_state_transitions_subject_type",
        ),
        sa.CheckConstraint("state_version >= 1", name="ck_auth_state_transitions_version"),
        sa.ForeignKeyConstraint(
            ["authorization_id"],
            ["trading_authorizations.authorization_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("transition_id"),
        sa.UniqueConstraint(
            "subject_type", "subject_id", "state_version", name="uq_auth_state_transition_version"
        ),
    )
    op.create_index(
        "ix_auth_state_transitions_root_time",
        "authorization_state_transitions",
        ["authorization_id", "changed_at"],
    )

    _create_immutability_guards()
    _create_state_guards_and_history()


def _create_immutability_guards() -> None:
    op.execute(
        """
        CREATE FUNCTION deny_trading_authorization_fact_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is immutable', TG_TABLE_NAME;
        END;
        $$
        """
    )
    for table in (
        "trading_authorizations",
        "campaigns",
        "initial_order_authorizations",
        "add_authorization_packages",
        "add_units",
        "authorization_state_transitions",
    ):
        op.execute(
            f"""
            CREATE TRIGGER {table}_immutable
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION deny_trading_authorization_fact_change()
            """
        )


def _create_state_guards_and_history() -> None:
    op.execute(
        """
        CREATE FUNCTION protect_trading_authorization_state_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE allowed boolean := false;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION '% cannot be deleted', TG_TABLE_NAME;
            END IF;
            IF NEW.version <> OLD.version + 1 OR NEW.updated_at < OLD.updated_at THEN
                RAISE EXCEPTION 'invalid version or time for %', TG_TABLE_NAME;
            END IF;
            IF TG_TABLE_NAME = 'campaign_states' THEN
                allowed := (OLD.status = 'PENDING_ENTRY' AND NEW.status = 'OPEN')
                    OR (OLD.status = 'OPEN' AND NEW.status = 'CLOSING')
                    OR (OLD.status = 'CLOSING' AND NEW.status = 'CLOSED');
                IF NEW.campaign_id <> OLD.campaign_id THEN allowed := false; END IF;
            ELSIF TG_TABLE_NAME = 'initial_authorization_states' THEN
                allowed := OLD.status = 'ACTIVE'
                    AND NEW.status IN ('CONSUMED', 'EXPIRED', 'REVOKED', 'INVALIDATED');
                IF NEW.initial_authorization_id <> OLD.initial_authorization_id THEN
                    allowed := false;
                END IF;
            ELSIF TG_TABLE_NAME = 'add_authorization_package_states' THEN
                allowed := (OLD.status = 'DORMANT'
                    AND NEW.status IN ('ACTIVE', 'REVOKED', 'EXPIRED', 'INVALIDATED'))
                    OR (OLD.status = 'ACTIVE'
                    AND NEW.status IN ('EXHAUSTED', 'REVOKED', 'EXPIRED', 'INVALIDATED'));
                IF NEW.add_package_id <> OLD.add_package_id THEN allowed := false; END IF;
            ELSIF TG_TABLE_NAME = 'add_unit_states' THEN
                allowed := (OLD.status = 'AVAILABLE'
                    AND NEW.status IN ('CLAIMED', 'EXPIRED', 'INVALIDATED'))
                    OR (OLD.status = 'CLAIMED'
                    AND NEW.status IN ('CONSUMED', 'AVAILABLE', 'EXPIRED', 'INVALIDATED'));
                IF NEW.add_unit_id <> OLD.add_unit_id THEN allowed := false; END IF;
            END IF;
            IF NOT allowed THEN
                RAISE EXCEPTION 'invalid state transition for %: % -> %',
                    TG_TABLE_NAME, OLD.status, NEW.status;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    for table in (
        "campaign_states",
        "initial_authorization_states",
        "add_authorization_package_states",
        "add_unit_states",
    ):
        op.execute(
            f"""
            CREATE TRIGGER {table}_transition_guard
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION protect_trading_authorization_state_change()
            """
        )

    op.execute(
        """
        CREATE FUNCTION record_trading_authorization_state_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE root_id uuid;
        DECLARE subject_kind text;
        DECLARE subject_key uuid;
        BEGIN
            IF TG_TABLE_NAME = 'campaign_states' THEN
                SELECT authorization_id INTO STRICT root_id
                FROM campaigns WHERE campaign_id = NEW.campaign_id;
                subject_kind := 'CAMPAIGN'; subject_key := NEW.campaign_id;
            ELSIF TG_TABLE_NAME = 'initial_authorization_states' THEN
                SELECT authorization_id INTO STRICT root_id
                FROM initial_order_authorizations
                WHERE initial_authorization_id = NEW.initial_authorization_id;
                subject_kind := 'INITIAL'; subject_key := NEW.initial_authorization_id;
            ELSIF TG_TABLE_NAME = 'add_authorization_package_states' THEN
                SELECT authorization_id INTO STRICT root_id
                FROM add_authorization_packages WHERE add_package_id = NEW.add_package_id;
                subject_kind := 'ADD_PACKAGE'; subject_key := NEW.add_package_id;
            ELSIF TG_TABLE_NAME = 'add_unit_states' THEN
                SELECT p.authorization_id INTO STRICT root_id
                FROM add_units u JOIN add_authorization_packages p
                  ON p.add_package_id = u.add_package_id
                WHERE u.add_unit_id = NEW.add_unit_id;
                subject_kind := 'ADD_UNIT'; subject_key := NEW.add_unit_id;
            END IF;
            INSERT INTO authorization_state_transitions (
                authorization_id, subject_type, subject_id, from_status, to_status,
                state_version, reason_code, changed_at
            ) VALUES (
                root_id, subject_kind, subject_key,
                CASE WHEN TG_OP = 'INSERT' THEN NEW.status ELSE OLD.status END,
                NEW.status, NEW.version, NEW.reason_code, NEW.updated_at
            );
            RETURN NEW;
        END;
        $$
        """
    )
    for table in (
        "campaign_states",
        "initial_authorization_states",
        "add_authorization_package_states",
        "add_unit_states",
    ):
        op.execute(
            f"""
            CREATE TRIGGER {table}_record_transition
            AFTER INSERT OR UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION record_trading_authorization_state_change()
            """
        )


def downgrade() -> None:
    for table in (
        "add_unit_states",
        "add_authorization_package_states",
        "initial_authorization_states",
        "campaign_states",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_record_transition ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_transition_guard ON {table}")
    op.execute("DROP FUNCTION IF EXISTS record_trading_authorization_state_change()")
    op.execute("DROP FUNCTION IF EXISTS protect_trading_authorization_state_change()")
    for table in (
        "authorization_state_transitions",
        "add_units",
        "add_authorization_packages",
        "initial_order_authorizations",
        "campaigns",
        "trading_authorizations",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS deny_trading_authorization_fact_change()")
    op.drop_index(
        "ix_auth_state_transitions_root_time", table_name="authorization_state_transitions"
    )
    op.drop_table("authorization_state_transitions")
    op.drop_table("add_unit_states")
    op.drop_table("add_units")
    op.drop_table("add_authorization_package_states")
    op.drop_table("add_authorization_packages")
    op.drop_table("initial_authorization_states")
    op.drop_table("initial_order_authorizations")
    op.drop_table("campaign_states")
    op.drop_index("ix_campaigns_org_created", table_name="campaigns")
    op.drop_table("campaigns")
    op.drop_index("ix_trading_auth_org_issued", table_name="trading_authorizations")
    op.drop_table("trading_authorizations")
    op.drop_constraint(
        "uq_approval_decisions_identity_binding",
        "approval_decisions",
        type_="unique",
    )
