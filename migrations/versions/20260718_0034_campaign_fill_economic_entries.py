"""Add immutable Campaign fill economic entries.

Revision ID: 20260718_0034
Revises: 20260718_0033
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260718_0034"
down_revision: str | Sequence[str] | None = "20260718_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "campaign_fill_economic_entries",
        sa.Column("campaign_fill_economic_entry_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("order_intent_id", sa.Uuid(), nullable=False),
        sa.Column("execution_fact_id", sa.Uuid(), nullable=False),
        sa.Column("venue_fill_id", sa.Uuid(), nullable=False),
        sa.Column("add_unit_id", sa.Uuid(), nullable=True),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("intent_kind", sa.String(length=20), nullable=False),
        sa.Column("economic_effect", sa.String(length=40), nullable=False),
        sa.Column("venue", sa.String(length=80), nullable=False),
        sa.Column("execution_domain", sa.String(length=120), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=False),
        sa.Column("instrument_id", sa.String(length=255), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("position_side", sa.String(length=20), nullable=False),
        sa.Column("reduce_only", sa.Boolean(), nullable=False),
        sa.Column("margin_mode", sa.String(length=80), nullable=False),
        sa.Column("collateral_scope", sa.String(length=120), nullable=False),
        sa.Column("collateral_pool_id", sa.String(length=160), nullable=False),
        sa.Column("risk_currency", sa.String(length=80), nullable=False),
        sa.Column("venue_order_id", sa.String(length=255), nullable=False),
        sa.Column("venue_trade_id", sa.String(length=255), nullable=False),
        sa.Column("quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("price", sa.Numeric(38, 18), nullable=False),
        sa.Column("contract_multiplier", sa.Numeric(38, 18), nullable=False),
        sa.Column("notional", sa.Numeric(38, 18), nullable=False),
        sa.Column("liquidity_role", sa.String(length=20), nullable=False),
        sa.Column("fee_amount", sa.Numeric(38, 18), nullable=False),
        sa.Column("fee_currency", sa.String(length=80), nullable=False),
        sa.Column("fee_effect", sa.String(length=20), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(38, 18), nullable=True),
        sa.Column("realized_pnl_status", sa.String(length=20), nullable=False),
        sa.Column("settlement_currency", sa.String(length=80), nullable=False),
        sa.Column("fill_hash", sa.String(length=64), nullable=False),
        sa.Column("execution_fact_evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("entry_version", sa.String(length=80), nullable=False),
        sa.Column("entry_hash", sa.String(length=64), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("real_funds_eligible", sa.Boolean(), nullable=False),
        sa.Column("facts_event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "intent_kind IN ('INITIAL', 'ADD') AND economic_effect = 'POSITION_INCREASE'",
            name="ck_campaign_fill_economic_entries_kind",
        ),
        sa.CheckConstraint(
            "(intent_kind = 'INITIAL' AND add_unit_id IS NULL) OR "
            "(intent_kind = 'ADD' AND add_unit_id IS NOT NULL)",
            name="ck_campaign_fill_economic_entries_add_unit",
        ),
        sa.CheckConstraint(
            "direction IN ('LONG', 'SHORT') AND side IN ('BUY', 'SELL') "
            "AND position_side IN ('LONG', 'SHORT', 'BOTH') AND reduce_only = false",
            name="ck_campaign_fill_economic_entries_direction",
        ),
        sa.CheckConstraint(
            "quantity > 0 AND price > 0 AND contract_multiplier > 0 "
            "AND notional = quantity * price * contract_multiplier",
            name="ck_campaign_fill_economic_entries_economics",
        ),
        sa.CheckConstraint(
            "liquidity_role IN ('MAKER', 'TAKER', 'UNKNOWN')",
            name="ck_campaign_fill_economic_entries_liquidity",
        ),
        sa.CheckConstraint(
            "(fee_effect = 'CHARGE' AND fee_amount > 0) OR "
            "(fee_effect = 'REBATE' AND fee_amount < 0) OR "
            "(fee_effect = 'ZERO' AND fee_amount = 0)",
            name="ck_campaign_fill_economic_entries_fee",
        ),
        sa.CheckConstraint(
            "(realized_pnl_status = 'KNOWN' AND realized_pnl IS NOT NULL) OR "
            "(realized_pnl_status = 'UNKNOWN' AND realized_pnl IS NULL)",
            name="ck_campaign_fill_economic_entries_realized_pnl",
        ),
        sa.CheckConstraint(
            "entry_version = 'campaign-fill-economic-entry-v1'",
            name="ck_campaign_fill_economic_entries_version",
        ),
        sa.CheckConstraint(
            "environment = 'SHADOW' AND real_funds_eligible = false",
            name="ck_campaign_fill_economic_entries_shadow_only",
        ),
        sa.CheckConstraint(
            "facts_event_time <= recorded_at",
            name="ck_campaign_fill_economic_entries_time_order",
        ),
        sa.CheckConstraint(
            "length(fill_hash) = 64 AND length(execution_fact_evidence_hash) = 64 "
            "AND length(entry_hash) = 64",
            name="ck_campaign_fill_economic_entries_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.campaign_id"],
            name="fk_campaign_fill_economic_entries_campaign",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_intent_id"],
            ["order_intents.order_intent_id"],
            name="fk_campaign_fill_economic_entries_intent",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["execution_fact_id"],
            ["execution_facts.execution_fact_id"],
            name="fk_campaign_fill_economic_entries_fact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["venue_fill_id"],
            ["venue_fills.venue_fill_id"],
            name="fk_campaign_fill_economic_entries_fill",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["add_unit_id"],
            ["add_units.add_unit_id"],
            name="fk_campaign_fill_economic_entries_add_unit",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("campaign_fill_economic_entry_id"),
        sa.UniqueConstraint(
            "execution_fact_id",
            name="uq_campaign_fill_economic_entries_fact",
        ),
        sa.UniqueConstraint(
            "venue_fill_id",
            name="uq_campaign_fill_economic_entries_fill",
        ),
    )
    op.create_index(
        "ix_campaign_fill_economic_entries_campaign_time",
        "campaign_fill_economic_entries",
        ["campaign_id", "facts_event_time"],
    )
    op.create_index(
        "ix_campaign_fill_economic_entries_org_time",
        "campaign_fill_economic_entries",
        ["organization_id", "recorded_at"],
    )
    op.execute(
        """
        CREATE FUNCTION protect_campaign_fill_economic_entry_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            intent_row order_intents%ROWTYPE;
            fact_row execution_facts%ROWTYPE;
            fill_row venue_fills%ROWTYPE;
            reservation_org text;
        BEGIN
            SELECT * INTO STRICT intent_row
            FROM order_intents
            WHERE order_intent_id = NEW.order_intent_id;
            SELECT * INTO STRICT fact_row
            FROM execution_facts
            WHERE execution_fact_id = NEW.execution_fact_id;
            SELECT * INTO STRICT fill_row
            FROM venue_fills
            WHERE venue_fill_id = NEW.venue_fill_id;
            SELECT organization_id INTO STRICT reservation_org
            FROM risk_reservations
            WHERE order_intent_id = NEW.order_intent_id;

            IF intent_row.intent_kind NOT IN ('INITIAL', 'ADD')
               OR intent_row.reduce_only
               OR intent_row.campaign_id <> NEW.campaign_id
               OR intent_row.intent_kind <> NEW.intent_kind
               OR intent_row.add_unit_id IS DISTINCT FROM NEW.add_unit_id
               OR fact_row.order_intent_id <> intent_row.order_intent_id
               OR fact_row.fact_kind <> 'VENUE_FILL'
               OR fact_row.venue_fill_id <> fill_row.venue_fill_id
               OR fact_row.venue_fact_hash <> fill_row.fill_hash
               OR NEW.fill_hash <> fill_row.fill_hash
               OR NEW.execution_fact_evidence_hash <> fact_row.evidence_hash
               OR NEW.organization_id <> reservation_org
               OR NEW.organization_id <> fill_row.organization_id
               OR NEW.venue <> intent_row.venue
               OR NEW.venue <> fill_row.venue
               OR NEW.execution_domain <> intent_row.execution_domain
               OR NEW.execution_domain <> fill_row.execution_domain
               OR NEW.account_id <> intent_row.account_id
               OR NEW.account_id <> fill_row.account_id
               OR NEW.instrument_id <> intent_row.instrument_id
               OR NEW.instrument_id <> fill_row.instrument_id
               OR NEW.direction <> intent_row.position_side
               OR NEW.side <> intent_row.side
               OR NEW.side <> fill_row.side
               OR NEW.position_side <> fill_row.position_side
               OR NEW.reduce_only <> intent_row.reduce_only
               OR NEW.reduce_only <> fill_row.reduce_only
               OR NEW.margin_mode <> intent_row.margin_mode
               OR NEW.collateral_scope <> intent_row.collateral_scope
               OR NEW.collateral_pool_id <> intent_row.collateral_pool_id
               OR NEW.risk_currency <> intent_row.risk_currency
               OR NEW.venue_order_id <> fill_row.venue_order_id
               OR NEW.venue_trade_id <> fill_row.venue_trade_id
               OR NEW.quantity <> fill_row.quantity
               OR NEW.price <> fill_row.price
               OR NEW.contract_multiplier <> fill_row.contract_multiplier
               OR NEW.notional <> fill_row.notional
               OR NEW.liquidity_role <> fill_row.liquidity_role
               OR NEW.fee_amount <> fill_row.fee_amount
               OR NEW.fee_currency <> fill_row.fee_currency
               OR NEW.fee_effect <> fill_row.fee_effect
               OR NEW.realized_pnl IS DISTINCT FROM fill_row.realized_pnl
               OR NEW.settlement_currency <> fill_row.settlement_currency
               OR NEW.facts_event_time <> fill_row.event_time THEN
                RAISE EXCEPTION 'campaign fill economic entry source binding mismatch';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER campaign_fill_economic_entries_insert_guard
        BEFORE INSERT ON campaign_fill_economic_entries
        FOR EACH ROW EXECUTE FUNCTION protect_campaign_fill_economic_entry_insert()
        """
    )
    op.execute(
        """
        CREATE FUNCTION deny_campaign_fill_economic_entry_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'campaign_fill_economic_entries is immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER campaign_fill_economic_entries_immutable
        BEFORE UPDATE OR DELETE ON campaign_fill_economic_entries
        FOR EACH ROW EXECUTE FUNCTION deny_campaign_fill_economic_entry_change()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM campaign_fill_economic_entries) THEN
                RAISE EXCEPTION
                    'cannot downgrade campaign fill economic entries while records remain';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS campaign_fill_economic_entries_immutable "
        "ON campaign_fill_economic_entries"
    )
    op.execute("DROP FUNCTION IF EXISTS deny_campaign_fill_economic_entry_change()")
    op.execute(
        "DROP TRIGGER IF EXISTS campaign_fill_economic_entries_insert_guard "
        "ON campaign_fill_economic_entries"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_campaign_fill_economic_entry_insert()")
    op.drop_index(
        "ix_campaign_fill_economic_entries_org_time",
        table_name="campaign_fill_economic_entries",
    )
    op.drop_index(
        "ix_campaign_fill_economic_entries_campaign_time",
        table_name="campaign_fill_economic_entries",
    )
    op.drop_table("campaign_fill_economic_entries")
