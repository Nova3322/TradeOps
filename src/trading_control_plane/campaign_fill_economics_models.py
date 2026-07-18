from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from trading_control_plane.database import Base


class CampaignFillEconomicEntry(Base):
    """Immutable Campaign attribution of one accepted canonical venue fill."""

    __tablename__ = "campaign_fill_economic_entries"
    __table_args__ = (
        CheckConstraint(
            "intent_kind IN ('INITIAL', 'ADD') AND economic_effect = 'POSITION_INCREASE'",
            name="ck_campaign_fill_economic_entries_kind",
        ),
        CheckConstraint(
            "(intent_kind = 'INITIAL' AND add_unit_id IS NULL) OR "
            "(intent_kind = 'ADD' AND add_unit_id IS NOT NULL)",
            name="ck_campaign_fill_economic_entries_add_unit",
        ),
        CheckConstraint(
            "direction IN ('LONG', 'SHORT') AND side IN ('BUY', 'SELL') "
            "AND position_side IN ('LONG', 'SHORT', 'BOTH') AND reduce_only = false",
            name="ck_campaign_fill_economic_entries_direction",
        ),
        CheckConstraint(
            "quantity > 0 AND price > 0 AND contract_multiplier > 0 "
            "AND notional = quantity * price * contract_multiplier",
            name="ck_campaign_fill_economic_entries_economics",
        ),
        CheckConstraint(
            "liquidity_role IN ('MAKER', 'TAKER', 'UNKNOWN')",
            name="ck_campaign_fill_economic_entries_liquidity",
        ),
        CheckConstraint(
            "(fee_effect = 'CHARGE' AND fee_amount > 0) OR "
            "(fee_effect = 'REBATE' AND fee_amount < 0) OR "
            "(fee_effect = 'ZERO' AND fee_amount = 0)",
            name="ck_campaign_fill_economic_entries_fee",
        ),
        CheckConstraint(
            "(realized_pnl_status = 'KNOWN' AND realized_pnl IS NOT NULL) OR "
            "(realized_pnl_status = 'UNKNOWN' AND realized_pnl IS NULL)",
            name="ck_campaign_fill_economic_entries_realized_pnl",
        ),
        CheckConstraint(
            "entry_version = 'campaign-fill-economic-entry-v1'",
            name="ck_campaign_fill_economic_entries_version",
        ),
        CheckConstraint(
            "environment = 'SHADOW' AND real_funds_eligible = false",
            name="ck_campaign_fill_economic_entries_shadow_only",
        ),
        CheckConstraint(
            "facts_event_time <= recorded_at",
            name="ck_campaign_fill_economic_entries_time_order",
        ),
        CheckConstraint(
            "length(fill_hash) = 64 AND length(execution_fact_evidence_hash) = 64 "
            "AND length(entry_hash) = 64",
            name="ck_campaign_fill_economic_entries_hashes",
        ),
        ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.campaign_id"],
            name="fk_campaign_fill_economic_entries_campaign",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["order_intent_id"],
            ["order_intents.order_intent_id"],
            name="fk_campaign_fill_economic_entries_intent",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["execution_fact_id"],
            ["execution_facts.execution_fact_id"],
            name="fk_campaign_fill_economic_entries_fact",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["venue_fill_id"],
            ["venue_fills.venue_fill_id"],
            name="fk_campaign_fill_economic_entries_fill",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["add_unit_id"],
            ["add_units.add_unit_id"],
            name="fk_campaign_fill_economic_entries_add_unit",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "execution_fact_id",
            name="uq_campaign_fill_economic_entries_fact",
        ),
        UniqueConstraint(
            "venue_fill_id",
            name="uq_campaign_fill_economic_entries_fill",
        ),
        Index(
            "ix_campaign_fill_economic_entries_campaign_time",
            "campaign_id",
            "facts_event_time",
        ),
        Index(
            "ix_campaign_fill_economic_entries_org_time",
            "organization_id",
            "recorded_at",
        ),
    )

    campaign_fill_economic_entry_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    campaign_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    order_intent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    execution_fact_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    venue_fill_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    add_unit_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    intent_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    economic_effect: Mapped[str] = mapped_column(String(40), nullable=False)
    venue: Mapped[str] = mapped_column(String(80), nullable=False)
    execution_domain: Mapped[str] = mapped_column(String(120), nullable=False)
    account_id: Mapped[str] = mapped_column(String(160), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(255), nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    position_side: Mapped[str] = mapped_column(String(20), nullable=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    margin_mode: Mapped[str] = mapped_column(String(80), nullable=False)
    collateral_scope: Mapped[str] = mapped_column(String(120), nullable=False)
    collateral_pool_id: Mapped[str] = mapped_column(String(160), nullable=False)
    risk_currency: Mapped[str] = mapped_column(String(80), nullable=False)
    venue_order_id: Mapped[str] = mapped_column(String(255), nullable=False)
    venue_trade_id: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    contract_multiplier: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    notional: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    liquidity_role: Mapped[str] = mapped_column(String(20), nullable=False)
    fee_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    fee_currency: Mapped[str] = mapped_column(String(80), nullable=False)
    fee_effect: Mapped[str] = mapped_column(String(20), nullable=False)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    realized_pnl_status: Mapped[str] = mapped_column(String(20), nullable=False)
    settlement_currency: Mapped[str] = mapped_column(String(80), nullable=False)
    fill_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_fact_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_version: Mapped[str] = mapped_column(String(80), nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    real_funds_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    facts_event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
