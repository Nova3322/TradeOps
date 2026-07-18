from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_control_plane.database import Base


class VenueOrderObservation(Base):
    """One globally deduplicated private-venue order lifecycle observation."""

    __tablename__ = "venue_order_observations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('OPEN', 'PARTIALLY_FILLED', 'FILLED', 'CANCEL_PENDING', "
            "'CANCELLED', 'REJECTED', 'EXPIRED', 'UNKNOWN')",
            name="ck_venue_order_observations_status",
        ),
        CheckConstraint("side IN ('BUY', 'SELL')", name="ck_venue_order_observations_side"),
        CheckConstraint(
            "position_side IN ('LONG', 'SHORT', 'BOTH')",
            name="ck_venue_order_observations_position_side",
        ),
        CheckConstraint(
            "original_quantity > 0 AND cumulative_filled_quantity >= 0 "
            "AND known_remaining_quantity >= 0 "
            "AND cumulative_filled_quantity + known_remaining_quantity <= original_quantity",
            name="ck_venue_order_observations_quantities",
        ),
        CheckConstraint(
            "(status = 'OPEN' AND cumulative_filled_quantity = 0 "
            "AND known_remaining_quantity = original_quantity AND NOT terminal) OR "
            "(status = 'PARTIALLY_FILLED' AND cumulative_filled_quantity > 0 "
            "AND known_remaining_quantity > 0 "
            "AND cumulative_filled_quantity + known_remaining_quantity = original_quantity "
            "AND NOT terminal) OR "
            "(status = 'FILLED' AND cumulative_filled_quantity = original_quantity "
            "AND known_remaining_quantity = 0 AND terminal) OR "
            "(status = 'CANCEL_PENDING' "
            "AND cumulative_filled_quantity + known_remaining_quantity = original_quantity "
            "AND NOT terminal) OR "
            "(status IN ('CANCELLED', 'EXPIRED') AND known_remaining_quantity = 0 "
            "AND terminal AND zero_fill_confirmed = (cumulative_filled_quantity = 0)) OR "
            "(status = 'REJECTED' AND cumulative_filled_quantity = 0 "
            "AND known_remaining_quantity = 0 AND terminal AND zero_fill_confirmed) OR "
            "(status = 'UNKNOWN' AND NOT terminal AND NOT zero_fill_confirmed)",
            name="ck_venue_order_observations_status_semantics",
        ),
        CheckConstraint(
            "event_time <= venue_observed_at AND venue_observed_at <= first_received_at "
            "AND first_received_at <= recorded_at",
            name="ck_venue_order_observations_time_order",
        ),
        CheckConstraint(
            "fact_authority = 'VENUE_PRIVATE' AND environment = 'SHADOW' "
            "AND live_dispatch_eligible = false",
            name="ck_venue_order_observations_authority",
        ),
        CheckConstraint(
            "jsonb_typeof(normalized_payload) = 'object' "
            "AND length(raw_payload_hash) = 64 AND length(evidence_hash) = 64 "
            "AND length(observation_hash) = 64",
            name="ck_venue_order_observations_integrity",
        ),
        ForeignKeyConstraint(
            ["first_seen_run_id", "organization_id"],
            [
                "execution_reconciliation_runs.run_id",
                "execution_reconciliation_runs.organization_id",
            ],
            name="fk_venue_order_observations_first_run_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "organization_id",
            "venue",
            "execution_domain",
            "account_id",
            "venue_order_id",
            "venue_update_id",
            name="uq_venue_order_observations_external_update",
        ),
        Index(
            "ix_venue_order_observations_order_time",
            "venue",
            "execution_domain",
            "account_id",
            "venue_order_id",
            "event_time",
        ),
        Index(
            "ix_venue_order_observations_client_identity",
            "venue",
            "execution_domain",
            "account_id",
            "observed_client_order_id",
        ),
    )

    venue_order_observation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    first_seen_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    first_seen_input_id: Mapped[UUID] = mapped_column(
        ForeignKey("execution_reconciliation_inputs.input_id", ondelete="RESTRICT"),
        nullable=False,
    )
    venue: Mapped[str] = mapped_column(String(80), nullable=False)
    execution_domain: Mapped[str] = mapped_column(String(120), nullable=False)
    account_id: Mapped[str] = mapped_column(String(160), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(255), nullable=False)
    observed_client_order_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    venue_order_id: Mapped[str] = mapped_column(String(255), nullable=False)
    venue_update_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    position_side: Mapped[str] = mapped_column(String(20), nullable=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    order_type: Mapped[str] = mapped_column(String(40), nullable=False)
    time_in_force: Mapped[str] = mapped_column(String(40), nullable=False)
    original_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    cumulative_filled_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    known_remaining_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    zero_fill_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    terminal: Mapped[bool] = mapped_column(Boolean, nullable=False)
    fact_authority: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    live_dispatch_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_version: Mapped[str] = mapped_column(String(160), nullable=False)
    normalization_version: Mapped[str] = mapped_column(String(160), nullable=False)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    raw_payload_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    venue_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VenueFill(Base):
    """One globally unique, venue-confirmed fill; the only future position-change input."""

    __tablename__ = "venue_fills"
    __table_args__ = (
        CheckConstraint("side IN ('BUY', 'SELL')", name="ck_venue_fills_side"),
        CheckConstraint(
            "position_side IN ('LONG', 'SHORT', 'BOTH')",
            name="ck_venue_fills_position_side",
        ),
        CheckConstraint(
            "quantity > 0 AND price > 0 AND contract_multiplier > 0 "
            "AND notional = quantity * price * contract_multiplier",
            name="ck_venue_fills_economics",
        ),
        CheckConstraint(
            "liquidity_role IN ('MAKER', 'TAKER', 'UNKNOWN')",
            name="ck_venue_fills_liquidity",
        ),
        CheckConstraint(
            "(fee_effect = 'CHARGE' AND fee_amount > 0) OR "
            "(fee_effect = 'REBATE' AND fee_amount < 0) OR "
            "(fee_effect = 'ZERO' AND fee_amount = 0)",
            name="ck_venue_fills_fee_effect",
        ),
        CheckConstraint(
            "event_time <= venue_observed_at AND venue_observed_at <= first_received_at "
            "AND first_received_at <= recorded_at",
            name="ck_venue_fills_time_order",
        ),
        CheckConstraint(
            "venue_confirmed AND fact_authority = 'VENUE_PRIVATE' "
            "AND environment = 'SHADOW' AND live_dispatch_eligible = false",
            name="ck_venue_fills_authority",
        ),
        CheckConstraint(
            "jsonb_typeof(normalized_payload) = 'object' "
            "AND length(raw_payload_hash) = 64 AND length(evidence_hash) = 64 "
            "AND length(fill_hash) = 64",
            name="ck_venue_fills_integrity",
        ),
        ForeignKeyConstraint(
            ["first_seen_run_id", "organization_id"],
            [
                "execution_reconciliation_runs.run_id",
                "execution_reconciliation_runs.organization_id",
            ],
            name="fk_venue_fills_first_run_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "organization_id",
            "venue",
            "execution_domain",
            "account_id",
            "venue_trade_id",
            name="uq_venue_fills_external_trade",
        ),
        Index(
            "ix_venue_fills_order_time",
            "venue",
            "execution_domain",
            "account_id",
            "venue_order_id",
            "event_time",
        ),
        Index(
            "ix_venue_fills_instrument_time",
            "venue",
            "execution_domain",
            "account_id",
            "instrument_id",
            "event_time",
        ),
    )

    venue_fill_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    first_seen_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    first_seen_input_id: Mapped[UUID] = mapped_column(
        ForeignKey("execution_reconciliation_inputs.input_id", ondelete="RESTRICT"),
        nullable=False,
    )
    venue: Mapped[str] = mapped_column(String(80), nullable=False)
    execution_domain: Mapped[str] = mapped_column(String(120), nullable=False)
    account_id: Mapped[str] = mapped_column(String(160), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(255), nullable=False)
    observed_client_order_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    venue_order_id: Mapped[str] = mapped_column(String(255), nullable=False)
    venue_trade_id: Mapped[str] = mapped_column(String(255), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    position_side: Mapped[str] = mapped_column(String(20), nullable=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    contract_multiplier: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    notional: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    liquidity_role: Mapped[str] = mapped_column(String(20), nullable=False)
    fee_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    fee_currency: Mapped[str] = mapped_column(String(80), nullable=False)
    fee_effect: Mapped[str] = mapped_column(String(20), nullable=False)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    settlement_currency: Mapped[str] = mapped_column(String(80), nullable=False)
    venue_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    fact_authority: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    live_dispatch_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_version: Mapped[str] = mapped_column(String(160), nullable=False)
    normalization_version: Mapped[str] = mapped_column(String(160), nullable=False)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    raw_payload_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fill_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    venue_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VenuePositionSnapshot(Base):
    """One immutable private-venue position line at a source event/update."""

    __tablename__ = "venue_position_snapshots"
    __table_args__ = (
        CheckConstraint(
            "position_state IN ('OPEN', 'FLAT', 'UNKNOWN')",
            name="ck_venue_position_snapshots_state",
        ),
        CheckConstraint(
            "position_mode IN ('ONE_WAY', 'HEDGE') "
            "AND position_side IN ('LONG', 'SHORT', 'BOTH') "
            "AND direction IN ('LONG', 'SHORT', 'FLAT', 'UNKNOWN')",
            name="ck_venue_position_snapshots_shape",
        ),
        CheckConstraint(
            "(position_mode = 'ONE_WAY' AND position_side = 'BOTH') OR "
            "(position_mode = 'HEDGE' AND position_side IN ('LONG', 'SHORT'))",
            name="ck_venue_position_snapshots_mode_side",
        ),
        CheckConstraint(
            "contract_multiplier > 0 AND ("
            "(position_state = 'OPEN' AND direction IN ('LONG', 'SHORT') "
            "AND quantity > 0 AND entry_price > 0 AND mark_price > 0 "
            "AND notional = quantity * mark_price * contract_multiplier "
            "AND unrealized_pnl IS NOT NULL "
            "AND (liquidation_price IS NULL OR liquidation_price > 0) "
            "AND (leverage IS NULL OR leverage > 0) "
            "AND (initial_margin IS NULL OR initial_margin >= 0) "
            "AND (maintenance_margin IS NULL OR maintenance_margin >= 0)) OR "
            "(position_state = 'FLAT' AND direction = 'FLAT' AND quantity = 0 "
            "AND entry_price IS NULL AND (mark_price IS NULL OR mark_price > 0) "
            "AND notional = 0 AND unrealized_pnl = 0 AND liquidation_price IS NULL "
            "AND leverage IS NULL AND (initial_margin IS NULL OR initial_margin = 0) "
            "AND (maintenance_margin IS NULL OR maintenance_margin = 0)) OR "
            "(position_state = 'UNKNOWN' AND direction = 'UNKNOWN' "
            "AND quantity IS NULL AND entry_price IS NULL AND mark_price IS NULL "
            "AND notional IS NULL AND unrealized_pnl IS NULL "
            "AND liquidation_price IS NULL AND leverage IS NULL "
            "AND initial_margin IS NULL AND maintenance_margin IS NULL))",
            name="ck_venue_position_snapshots_economics",
        ),
        CheckConstraint(
            "(position_mode = 'ONE_WAY') OR position_state <> 'OPEN' OR direction = position_side",
            name="ck_venue_position_snapshots_hedge_direction",
        ),
        CheckConstraint(
            "event_time <= venue_observed_at AND venue_observed_at <= first_received_at "
            "AND first_received_at <= recorded_at",
            name="ck_venue_position_snapshots_time_order",
        ),
        CheckConstraint(
            "venue_confirmed AND fact_authority = 'VENUE_PRIVATE' "
            "AND environment = 'SHADOW' AND live_dispatch_eligible = false",
            name="ck_venue_position_snapshots_authority",
        ),
        CheckConstraint(
            "jsonb_typeof(normalized_payload) = 'object' "
            "AND length(raw_payload_hash) = 64 AND length(evidence_hash) = 64 "
            "AND length(snapshot_hash) = 64",
            name="ck_venue_position_snapshots_integrity",
        ),
        ForeignKeyConstraint(
            ["first_seen_run_id", "organization_id"],
            [
                "execution_reconciliation_runs.run_id",
                "execution_reconciliation_runs.organization_id",
            ],
            name="fk_venue_position_snapshots_first_run_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "organization_id",
            "venue",
            "execution_domain",
            "account_id",
            "instrument_id",
            "position_mode",
            "position_side",
            "margin_mode",
            "collateral_pool_id",
            "venue_update_id",
            name="uq_venue_position_snapshots_external_update",
        ),
        Index(
            "ix_venue_position_snapshots_scope_time",
            "venue",
            "execution_domain",
            "account_id",
            "instrument_id",
            "position_side",
            "event_time",
        ),
    )

    venue_position_snapshot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    first_seen_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    first_seen_input_id: Mapped[UUID] = mapped_column(
        ForeignKey("execution_reconciliation_inputs.input_id", ondelete="RESTRICT"),
        nullable=False,
    )
    venue: Mapped[str] = mapped_column(String(80), nullable=False)
    execution_domain: Mapped[str] = mapped_column(String(120), nullable=False)
    account_id: Mapped[str] = mapped_column(String(160), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(255), nullable=False)
    venue_update_id: Mapped[str] = mapped_column(String(255), nullable=False)
    position_mode: Mapped[str] = mapped_column(String(80), nullable=False)
    position_side: Mapped[str] = mapped_column(String(20), nullable=False)
    margin_mode: Mapped[str] = mapped_column(String(80), nullable=False)
    collateral_pool_id: Mapped[str] = mapped_column(String(160), nullable=False)
    position_state: Mapped[str] = mapped_column(String(20), nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    mark_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    contract_multiplier: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    notional: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    liquidation_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    leverage: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    initial_margin: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    maintenance_margin: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    settlement_currency: Mapped[str] = mapped_column(String(80), nullable=False)
    venue_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    fact_authority: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    live_dispatch_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_version: Mapped[str] = mapped_column(String(160), nullable=False)
    normalization_version: Mapped[str] = mapped_column(String(160), nullable=False)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    raw_payload_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    venue_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VenueAccountEquitySnapshot(Base):
    """One immutable private-venue account/collateral-pool equity snapshot."""

    __tablename__ = "venue_account_equity_snapshots"
    __table_args__ = (
        CheckConstraint(
            "equity_state IN ('CONFIRMED', 'UNKNOWN')",
            name="ck_venue_account_equity_snapshots_state",
        ),
        CheckConstraint(
            "(equity_state = 'CONFIRMED' "
            "AND wallet_balance IS NOT NULL AND exchange_margin_equity IS NOT NULL "
            "AND available_margin IS NOT NULL AND total_unrealized_pnl IS NOT NULL "
            "AND total_initial_margin >= 0 AND total_maintenance_margin >= 0 "
            "AND total_liability >= 0 AND unsettled_fee IS NOT NULL "
            "AND unsettled_funding IS NOT NULL AND includes_unrealized_pnl) OR "
            "(equity_state = 'UNKNOWN' AND wallet_balance IS NULL "
            "AND exchange_margin_equity IS NULL AND available_margin IS NULL "
            "AND total_unrealized_pnl IS NULL AND total_initial_margin IS NULL "
            "AND total_maintenance_margin IS NULL AND total_liability IS NULL "
            "AND unsettled_fee IS NULL AND unsettled_funding IS NULL "
            "AND NOT includes_unrealized_pnl)",
            name="ck_venue_account_equity_snapshots_economics",
        ),
        CheckConstraint(
            "event_time <= venue_observed_at AND venue_observed_at <= first_received_at "
            "AND first_received_at <= recorded_at",
            name="ck_venue_account_equity_snapshots_time_order",
        ),
        CheckConstraint(
            "venue_confirmed AND fact_authority = 'VENUE_PRIVATE' "
            "AND environment = 'SHADOW' AND live_dispatch_eligible = false",
            name="ck_venue_account_equity_snapshots_authority",
        ),
        CheckConstraint(
            "jsonb_typeof(normalized_payload) = 'object' "
            "AND length(raw_payload_hash) = 64 AND length(evidence_hash) = 64 "
            "AND length(snapshot_hash) = 64",
            name="ck_venue_account_equity_snapshots_integrity",
        ),
        ForeignKeyConstraint(
            ["first_seen_run_id", "organization_id"],
            [
                "execution_reconciliation_runs.run_id",
                "execution_reconciliation_runs.organization_id",
            ],
            name="fk_venue_account_equity_snapshots_first_run_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "organization_id",
            "venue",
            "execution_domain",
            "account_id",
            "margin_mode",
            "collateral_pool_id",
            "settlement_currency",
            "venue_update_id",
            name="uq_venue_account_equity_snapshots_external_update",
        ),
        Index(
            "ix_venue_account_equity_snapshots_scope_time",
            "venue",
            "execution_domain",
            "account_id",
            "margin_mode",
            "collateral_pool_id",
            "settlement_currency",
            "event_time",
        ),
    )

    venue_account_equity_snapshot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    first_seen_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    first_seen_input_id: Mapped[UUID] = mapped_column(
        ForeignKey("execution_reconciliation_inputs.input_id", ondelete="RESTRICT"),
        nullable=False,
    )
    venue: Mapped[str] = mapped_column(String(80), nullable=False)
    execution_domain: Mapped[str] = mapped_column(String(120), nullable=False)
    account_id: Mapped[str] = mapped_column(String(160), nullable=False)
    venue_update_id: Mapped[str] = mapped_column(String(255), nullable=False)
    margin_mode: Mapped[str] = mapped_column(String(80), nullable=False)
    collateral_pool_id: Mapped[str] = mapped_column(String(160), nullable=False)
    settlement_currency: Mapped[str] = mapped_column(String(80), nullable=False)
    equity_state: Mapped[str] = mapped_column(String(20), nullable=False)
    wallet_balance: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    exchange_margin_equity: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    available_margin: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    total_unrealized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    total_initial_margin: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    total_maintenance_margin: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    total_liability: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    unsettled_fee: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    unsettled_funding: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    includes_unrealized_pnl: Mapped[bool] = mapped_column(Boolean, nullable=False)
    venue_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    fact_authority: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    live_dispatch_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_version: Mapped[str] = mapped_column(String(160), nullable=False)
    normalization_version: Mapped[str] = mapped_column(String(160), nullable=False)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    raw_payload_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    venue_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VenueProtectionSnapshot(Base):
    """One immutable private-venue snapshot of the active native protection set."""

    __tablename__ = "venue_protection_snapshots"
    __table_args__ = (
        CheckConstraint(
            "protection_state IN ('CONFIRMED', 'DEGRADED', 'UNKNOWN')",
            name="ck_venue_protection_snapshots_state",
        ),
        CheckConstraint(
            "position_mode IN ('ONE_WAY', 'HEDGE') "
            "AND position_side IN ('LONG', 'SHORT', 'BOTH') "
            "AND protected_direction IN ('LONG', 'SHORT', 'UNKNOWN')",
            name="ck_venue_protection_snapshots_shape",
        ),
        CheckConstraint(
            "(position_mode = 'ONE_WAY' AND position_side = 'BOTH') OR "
            "(position_mode = 'HEDGE' AND position_side IN ('LONG', 'SHORT'))",
            name="ck_venue_protection_snapshots_mode_side",
        ),
        CheckConstraint(
            "(protection_state = 'CONFIRMED' "
            "AND protected_direction IN ('LONG', 'SHORT') "
            "AND position_quantity > 0 AND covered_quantity = position_quantity "
            "AND uncovered_quantity = 0 AND active_stop_order_count >= 1 "
            "AND worst_active_trigger_price IS NOT NULL "
            "AND worst_active_trigger_price > 0 "
            "AND venue_native AND reduce_only_confirmed AND NOT replacement_in_progress) OR "
            "(protection_state = 'DEGRADED' "
            "AND protected_direction IN ('LONG', 'SHORT') "
            "AND position_quantity > 0 AND covered_quantity >= 0 "
            "AND uncovered_quantity >= 0 "
            "AND covered_quantity + uncovered_quantity = position_quantity "
            "AND active_stop_order_count >= 0 "
            "AND (worst_active_trigger_price IS NULL OR worst_active_trigger_price > 0) "
            "AND (uncovered_quantity > 0 OR active_stop_order_count = 0 "
            "OR NOT venue_native OR NOT reduce_only_confirmed OR replacement_in_progress)) OR "
            "(protection_state = 'UNKNOWN' AND protected_direction = 'UNKNOWN' "
            "AND position_quantity IS NULL AND covered_quantity IS NULL "
            "AND uncovered_quantity IS NULL AND active_stop_order_count IS NULL "
            "AND worst_active_trigger_price IS NULL "
            "AND NOT venue_native AND NOT reduce_only_confirmed "
            "AND NOT replacement_in_progress)",
            name="ck_venue_protection_snapshots_coverage",
        ),
        CheckConstraint(
            "event_time <= venue_observed_at AND venue_observed_at <= first_received_at "
            "AND first_received_at <= recorded_at",
            name="ck_venue_protection_snapshots_time_order",
        ),
        CheckConstraint(
            "venue_confirmed AND fact_authority = 'VENUE_PRIVATE' "
            "AND environment = 'SHADOW' AND live_dispatch_eligible = false",
            name="ck_venue_protection_snapshots_authority",
        ),
        CheckConstraint(
            "jsonb_typeof(normalized_payload) = 'object' "
            "AND length(order_set_hash) = 64 AND length(raw_payload_hash) = 64 "
            "AND length(evidence_hash) = 64 AND length(snapshot_hash) = 64",
            name="ck_venue_protection_snapshots_integrity",
        ),
        ForeignKeyConstraint(
            ["first_seen_run_id", "organization_id"],
            [
                "execution_reconciliation_runs.run_id",
                "execution_reconciliation_runs.organization_id",
            ],
            name="fk_venue_protection_snapshots_first_run_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "organization_id",
            "venue",
            "execution_domain",
            "account_id",
            "instrument_id",
            "position_mode",
            "position_side",
            "margin_mode",
            "collateral_pool_id",
            "venue_update_id",
            name="uq_venue_protection_snapshots_external_update",
        ),
        Index(
            "ix_venue_protection_snapshots_scope_time",
            "venue",
            "execution_domain",
            "account_id",
            "instrument_id",
            "position_side",
            "event_time",
        ),
    )

    venue_protection_snapshot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    first_seen_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    first_seen_input_id: Mapped[UUID] = mapped_column(
        ForeignKey("execution_reconciliation_inputs.input_id", ondelete="RESTRICT"),
        nullable=False,
    )
    venue_position_snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("venue_position_snapshots.venue_position_snapshot_id", ondelete="RESTRICT"),
        nullable=False,
    )
    venue: Mapped[str] = mapped_column(String(80), nullable=False)
    execution_domain: Mapped[str] = mapped_column(String(120), nullable=False)
    account_id: Mapped[str] = mapped_column(String(160), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(255), nullable=False)
    venue_update_id: Mapped[str] = mapped_column(String(255), nullable=False)
    position_mode: Mapped[str] = mapped_column(String(80), nullable=False)
    position_side: Mapped[str] = mapped_column(String(20), nullable=False)
    margin_mode: Mapped[str] = mapped_column(String(80), nullable=False)
    collateral_pool_id: Mapped[str] = mapped_column(String(160), nullable=False)
    protection_state: Mapped[str] = mapped_column(String(20), nullable=False)
    protected_direction: Mapped[str] = mapped_column(String(20), nullable=False)
    position_quantity: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    covered_quantity: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    uncovered_quantity: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    active_stop_order_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    worst_active_trigger_price: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 18), nullable=True
    )
    venue_native: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reduce_only_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    replacement_in_progress: Mapped[bool] = mapped_column(Boolean, nullable=False)
    order_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    venue_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    fact_authority: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    live_dispatch_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_version: Mapped[str] = mapped_column(String(160), nullable=False)
    normalization_version: Mapped[str] = mapped_column(String(160), nullable=False)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    raw_payload_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    venue_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VenueFactInputLink(Base):
    """Immutable membership of one canonical venue fact in one reconciliation input."""

    __tablename__ = "venue_fact_input_links"
    __table_args__ = (
        CheckConstraint(
            "(source_type = 'VENUE_ORDERS' AND venue_order_observation_id IS NOT NULL "
            "AND venue_fill_id IS NULL AND venue_position_snapshot_id IS NULL "
            "AND venue_protection_snapshot_id IS NULL "
            "AND venue_account_equity_snapshot_id IS NULL) OR "
            "(source_type = 'VENUE_FILLS' AND venue_order_observation_id IS NULL "
            "AND venue_fill_id IS NOT NULL AND venue_position_snapshot_id IS NULL "
            "AND venue_protection_snapshot_id IS NULL "
            "AND venue_account_equity_snapshot_id IS NULL) OR "
            "(source_type = 'VENUE_POSITIONS' AND venue_order_observation_id IS NULL "
            "AND venue_fill_id IS NULL AND venue_position_snapshot_id IS NOT NULL "
            "AND venue_protection_snapshot_id IS NULL "
            "AND venue_account_equity_snapshot_id IS NULL) OR "
            "(source_type = 'VENUE_PROTECTION' AND venue_order_observation_id IS NULL "
            "AND venue_fill_id IS NULL AND venue_position_snapshot_id IS NULL "
            "AND venue_protection_snapshot_id IS NOT NULL "
            "AND venue_account_equity_snapshot_id IS NULL) OR "
            "(source_type = 'VENUE_BALANCES' AND venue_order_observation_id IS NULL "
            "AND venue_fill_id IS NULL AND venue_position_snapshot_id IS NULL "
            "AND venue_protection_snapshot_id IS NULL "
            "AND venue_account_equity_snapshot_id IS NOT NULL)",
            name="ck_venue_fact_input_links_exact_fact",
        ),
        CheckConstraint(
            "observed_at <= received_at AND received_at <= linked_at",
            name="ck_venue_fact_input_links_time_order",
        ),
        CheckConstraint(
            "length(input_hash) = 64 AND length(fact_hash) = 64 "
            "AND length(raw_payload_hash) = 64 AND length(evidence_hash) = 64 "
            "AND length(link_hash) = 64",
            name="ck_venue_fact_input_links_integrity",
        ),
        ForeignKeyConstraint(
            ["run_id", "organization_id"],
            [
                "execution_reconciliation_runs.run_id",
                "execution_reconciliation_runs.organization_id",
            ],
            name="fk_venue_fact_input_links_run_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "reconciliation_input_id",
            "venue_order_observation_id",
            name="uq_venue_fact_input_links_order_fact",
        ),
        UniqueConstraint(
            "reconciliation_input_id",
            "venue_fill_id",
            name="uq_venue_fact_input_links_fill_fact",
        ),
        UniqueConstraint(
            "reconciliation_input_id",
            "venue_position_snapshot_id",
            name="uq_venue_fact_input_links_position_fact",
        ),
        UniqueConstraint(
            "reconciliation_input_id",
            "venue_protection_snapshot_id",
            name="uq_venue_fact_input_links_protection_fact",
        ),
        UniqueConstraint(
            "reconciliation_input_id",
            "venue_account_equity_snapshot_id",
            name="uq_venue_fact_input_links_account_equity_fact",
        ),
        Index("ix_venue_fact_input_links_run_source", "run_id", "source_type"),
    )

    venue_fact_input_link_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reconciliation_input_id: Mapped[UUID] = mapped_column(
        ForeignKey("execution_reconciliation_inputs.input_id", ondelete="RESTRICT"),
        nullable=False,
    )
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    venue_order_observation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("venue_order_observations.venue_order_observation_id", ondelete="RESTRICT"),
        nullable=True,
    )
    venue_fill_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("venue_fills.venue_fill_id", ondelete="RESTRICT"), nullable=True
    )
    venue_position_snapshot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("venue_position_snapshots.venue_position_snapshot_id", ondelete="RESTRICT"),
        nullable=True,
    )
    venue_protection_snapshot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("venue_protection_snapshots.venue_protection_snapshot_id", ondelete="RESTRICT"),
        nullable=True,
    )
    venue_account_equity_snapshot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "venue_account_equity_snapshots.venue_account_equity_snapshot_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_payload_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    link_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
