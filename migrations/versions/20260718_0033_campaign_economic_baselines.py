"""Add immutable isolated Campaign economic baselines.

Revision ID: 20260718_0033
Revises: 20260718_0032
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260718_0033"
down_revision: str | Sequence[str] | None = "20260718_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "campaign_economic_baselines",
        sa.Column("campaign_economic_baseline_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("initial_order_intent_id", sa.Uuid(), nullable=False),
        sa.Column("initial_execution_fact_id", sa.Uuid(), nullable=False),
        sa.Column("position_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("venue", sa.String(length=80), nullable=False),
        sa.Column("execution_domain", sa.String(length=120), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=False),
        sa.Column("instrument_id", sa.String(length=255), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("position_mode", sa.String(length=80), nullable=False),
        sa.Column("position_side", sa.String(length=20), nullable=False),
        sa.Column("margin_mode", sa.String(length=80), nullable=False),
        sa.Column("collateral_scope", sa.String(length=120), nullable=False),
        sa.Column("collateral_pool_id", sa.String(length=160), nullable=False),
        sa.Column("settlement_currency", sa.String(length=80), nullable=False),
        sa.Column("initial_quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("initial_entry_price", sa.Numeric(38, 18), nullable=False),
        sa.Column("initial_mark_price", sa.Numeric(38, 18), nullable=False),
        sa.Column("contract_multiplier", sa.Numeric(38, 18), nullable=False),
        sa.Column("initial_notional", sa.Numeric(38, 18), nullable=False),
        sa.Column("frozen_initial_margin_reference", sa.Numeric(38, 18), nullable=False),
        sa.Column("margin_reference_source", sa.String(length=80), nullable=False),
        sa.Column("position_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("execution_fact_evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("baseline_version", sa.String(length=80), nullable=False),
        sa.Column("baseline_hash", sa.String(length=64), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("real_funds_eligible", sa.Boolean(), nullable=False),
        sa.Column("facts_event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "baseline_version = 'campaign-economic-baseline-v1'",
            name="ck_campaign_economic_baselines_version",
        ),
        sa.CheckConstraint(
            "margin_reference_source = 'VENUE_POSITION_INITIAL_MARGIN'",
            name="ck_campaign_economic_baselines_margin_source",
        ),
        sa.CheckConstraint(
            "margin_mode = 'ISOLATED' AND frozen_initial_margin_reference > 0",
            name="ck_campaign_economic_baselines_isolated_margin",
        ),
        sa.CheckConstraint(
            "initial_quantity > 0 AND initial_entry_price > 0 AND initial_mark_price > 0 "
            "AND contract_multiplier > 0 "
            "AND initial_notional = initial_quantity * initial_mark_price * contract_multiplier",
            name="ck_campaign_economic_baselines_position_economics",
        ),
        sa.CheckConstraint(
            "environment = 'SHADOW' AND real_funds_eligible = false",
            name="ck_campaign_economic_baselines_shadow_only",
        ),
        sa.CheckConstraint(
            "facts_event_time <= recorded_at",
            name="ck_campaign_economic_baselines_time_order",
        ),
        sa.CheckConstraint(
            "length(position_snapshot_hash) = 64 "
            "AND length(execution_fact_evidence_hash) = 64 "
            "AND length(baseline_hash) = 64",
            name="ck_campaign_economic_baselines_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.campaign_id"],
            name="fk_campaign_economic_baselines_campaign",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["initial_order_intent_id"],
            ["order_intents.order_intent_id"],
            name="fk_campaign_economic_baselines_intent",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["initial_execution_fact_id"],
            ["execution_facts.execution_fact_id"],
            name="fk_campaign_economic_baselines_execution_fact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["position_snapshot_id"],
            ["venue_position_snapshots.venue_position_snapshot_id"],
            name="fk_campaign_economic_baselines_position_snapshot",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("campaign_economic_baseline_id"),
        sa.UniqueConstraint("campaign_id", name="uq_campaign_economic_baselines_campaign"),
        sa.UniqueConstraint(
            "initial_order_intent_id",
            name="uq_campaign_economic_baselines_initial_intent",
        ),
        sa.UniqueConstraint(
            "initial_execution_fact_id",
            name="uq_campaign_economic_baselines_execution_fact",
        ),
        sa.UniqueConstraint(
            "position_snapshot_id",
            name="uq_campaign_economic_baselines_position_snapshot",
        ),
    )
    op.create_index(
        "ix_campaign_economic_baselines_org_time",
        "campaign_economic_baselines",
        ["organization_id", "recorded_at"],
    )
    op.execute(
        """
        CREATE FUNCTION protect_campaign_economic_baseline_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            intent_row order_intents%ROWTYPE;
            fact_row execution_facts%ROWTYPE;
            position_row venue_position_snapshots%ROWTYPE;
            reservation_org text;
        BEGIN
            SELECT * INTO STRICT intent_row
            FROM order_intents
            WHERE order_intent_id = NEW.initial_order_intent_id;
            SELECT * INTO STRICT fact_row
            FROM execution_facts
            WHERE execution_fact_id = NEW.initial_execution_fact_id;
            SELECT * INTO STRICT position_row
            FROM venue_position_snapshots
            WHERE venue_position_snapshot_id = NEW.position_snapshot_id;
            SELECT organization_id INTO STRICT reservation_org
            FROM risk_reservations
            WHERE order_intent_id = NEW.initial_order_intent_id;

            IF intent_row.intent_kind <> 'INITIAL'
               OR intent_row.campaign_id <> NEW.campaign_id
               OR fact_row.order_intent_id <> intent_row.order_intent_id
               OR fact_row.fact_kind <> 'VENUE_POSITION'
               OR fact_row.target_status <> 'POSITION_RECONCILED'
               OR fact_row.venue_position_snapshot_id <> position_row.venue_position_snapshot_id
               OR fact_row.venue_fact_hash <> position_row.snapshot_hash
               OR NEW.position_snapshot_hash <> position_row.snapshot_hash
               OR NEW.execution_fact_evidence_hash <> fact_row.evidence_hash
               OR NEW.organization_id <> reservation_org
               OR NEW.organization_id <> position_row.organization_id
               OR NEW.venue <> intent_row.venue
               OR NEW.venue <> position_row.venue
               OR NEW.execution_domain <> intent_row.execution_domain
               OR NEW.execution_domain <> position_row.execution_domain
               OR NEW.account_id <> intent_row.account_id
               OR NEW.account_id <> position_row.account_id
               OR NEW.instrument_id <> intent_row.instrument_id
               OR NEW.instrument_id <> position_row.instrument_id
               OR NEW.direction <> intent_row.position_side
               OR NEW.direction <> position_row.direction
               OR NEW.position_mode <> position_row.position_mode
               OR NEW.position_side <> position_row.position_side
               OR NEW.margin_mode <> intent_row.margin_mode
               OR NEW.margin_mode <> position_row.margin_mode
               OR NEW.collateral_scope <> intent_row.collateral_scope
               OR NEW.collateral_pool_id <> intent_row.collateral_pool_id
               OR NEW.collateral_pool_id <> position_row.collateral_pool_id
               OR NEW.settlement_currency <> intent_row.risk_currency
               OR NEW.settlement_currency <> position_row.settlement_currency
               OR position_row.position_state <> 'OPEN'
               OR NEW.initial_quantity <> position_row.quantity
               OR NEW.initial_quantity <> fact_row.cumulative_filled_quantity
               OR NEW.initial_entry_price <> position_row.entry_price
               OR NEW.initial_mark_price <> position_row.mark_price
               OR NEW.contract_multiplier <> position_row.contract_multiplier
               OR NEW.initial_notional <> position_row.notional
               OR NEW.frozen_initial_margin_reference <> position_row.initial_margin
               OR NEW.facts_event_time <> position_row.event_time THEN
                RAISE EXCEPTION 'campaign economic baseline source binding mismatch';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER campaign_economic_baselines_insert_guard
        BEFORE INSERT ON campaign_economic_baselines
        FOR EACH ROW EXECUTE FUNCTION protect_campaign_economic_baseline_insert()
        """
    )
    op.execute(
        """
        CREATE FUNCTION deny_campaign_economic_baseline_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'campaign_economic_baselines is immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER campaign_economic_baselines_immutable
        BEFORE UPDATE OR DELETE ON campaign_economic_baselines
        FOR EACH ROW EXECUTE FUNCTION deny_campaign_economic_baseline_change()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM campaign_economic_baselines) THEN
                RAISE EXCEPTION 'cannot downgrade campaign economic baselines while records remain';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS campaign_economic_baselines_immutable "
        "ON campaign_economic_baselines"
    )
    op.execute("DROP FUNCTION IF EXISTS deny_campaign_economic_baseline_change()")
    op.execute(
        "DROP TRIGGER IF EXISTS campaign_economic_baselines_insert_guard "
        "ON campaign_economic_baselines"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_campaign_economic_baseline_insert()")
    op.drop_index(
        "ix_campaign_economic_baselines_org_time",
        table_name="campaign_economic_baselines",
    )
    op.drop_table("campaign_economic_baselines")
