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


class CampaignEconomicBaseline(Base):
    """Immutable SHADOW-only economic baseline frozen after the INITIAL position fact."""

    __tablename__ = "campaign_economic_baselines"
    __table_args__ = (
        CheckConstraint(
            "baseline_version = 'campaign-economic-baseline-v1'",
            name="ck_campaign_economic_baselines_version",
        ),
        CheckConstraint(
            "margin_reference_source = 'VENUE_POSITION_INITIAL_MARGIN'",
            name="ck_campaign_economic_baselines_margin_source",
        ),
        CheckConstraint(
            "margin_mode = 'ISOLATED' AND frozen_initial_margin_reference > 0",
            name="ck_campaign_economic_baselines_isolated_margin",
        ),
        CheckConstraint(
            "initial_quantity > 0 AND initial_entry_price > 0 AND initial_mark_price > 0 "
            "AND contract_multiplier > 0 "
            "AND initial_notional = initial_quantity * initial_mark_price * contract_multiplier",
            name="ck_campaign_economic_baselines_position_economics",
        ),
        CheckConstraint(
            "environment = 'SHADOW' AND real_funds_eligible = false",
            name="ck_campaign_economic_baselines_shadow_only",
        ),
        CheckConstraint(
            "facts_event_time <= recorded_at",
            name="ck_campaign_economic_baselines_time_order",
        ),
        CheckConstraint(
            "length(position_snapshot_hash) = 64 "
            "AND length(execution_fact_evidence_hash) = 64 "
            "AND length(baseline_hash) = 64",
            name="ck_campaign_economic_baselines_hashes",
        ),
        ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.campaign_id"],
            name="fk_campaign_economic_baselines_campaign",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["initial_order_intent_id"],
            ["order_intents.order_intent_id"],
            name="fk_campaign_economic_baselines_intent",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["initial_execution_fact_id"],
            ["execution_facts.execution_fact_id"],
            name="fk_campaign_economic_baselines_execution_fact",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["position_snapshot_id"],
            ["venue_position_snapshots.venue_position_snapshot_id"],
            name="fk_campaign_economic_baselines_position_snapshot",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("campaign_id", name="uq_campaign_economic_baselines_campaign"),
        UniqueConstraint(
            "initial_order_intent_id",
            name="uq_campaign_economic_baselines_initial_intent",
        ),
        UniqueConstraint(
            "initial_execution_fact_id",
            name="uq_campaign_economic_baselines_execution_fact",
        ),
        UniqueConstraint(
            "position_snapshot_id",
            name="uq_campaign_economic_baselines_position_snapshot",
        ),
        Index("ix_campaign_economic_baselines_org_time", "organization_id", "recorded_at"),
    )

    campaign_economic_baseline_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    campaign_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    initial_order_intent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    initial_execution_fact_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    position_snapshot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(80), nullable=False)
    execution_domain: Mapped[str] = mapped_column(String(120), nullable=False)
    account_id: Mapped[str] = mapped_column(String(160), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(255), nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    position_mode: Mapped[str] = mapped_column(String(80), nullable=False)
    position_side: Mapped[str] = mapped_column(String(20), nullable=False)
    margin_mode: Mapped[str] = mapped_column(String(80), nullable=False)
    collateral_scope: Mapped[str] = mapped_column(String(120), nullable=False)
    collateral_pool_id: Mapped[str] = mapped_column(String(160), nullable=False)
    settlement_currency: Mapped[str] = mapped_column(String(80), nullable=False)
    initial_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    initial_entry_price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    initial_mark_price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    contract_multiplier: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    initial_notional: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    frozen_initial_margin_reference: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False
    )
    margin_reference_source: Mapped[str] = mapped_column(String(80), nullable=False)
    position_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_fact_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    baseline_version: Mapped[str] = mapped_column(String(80), nullable=False)
    baseline_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    real_funds_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    facts_event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
