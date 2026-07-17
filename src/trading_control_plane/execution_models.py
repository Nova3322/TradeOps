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

ORDER_INTENT_STATUSES = (
    "INTENT_CREATED",
    "DISPATCHING",
    "VENUE_ACKNOWLEDGED",
    "PARTIALLY_FILLED",
    "FILLED",
    "CANCEL_PENDING",
    "CANCELLED_ZERO_FILL",
    "CANCELLED_PARTIAL",
    "REJECTED_ZERO_FILL",
    "RESULT_UNKNOWN",
    "POSITION_RECONCILED",
    "PROTECTION_CONFIRMED",
    "COMPLETED",
    "FAILED_SAFE",
)


def _authorization_binding_constraints(prefix: str) -> tuple[ForeignKeyConstraint, ...]:
    return (
        ForeignKeyConstraint(
            ["campaign_id", "authorization_id"],
            ["campaigns.campaign_id", "campaigns.authorization_id"],
            name=f"fk_{prefix}_campaign_authorization",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["initial_authorization_id", "campaign_id", "authorization_id"],
            [
                "initial_order_authorizations.initial_authorization_id",
                "initial_order_authorizations.campaign_id",
                "initial_order_authorizations.authorization_id",
            ],
            name=f"fk_{prefix}_initial_binding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["add_package_id", "campaign_id", "authorization_id"],
            [
                "add_authorization_packages.add_package_id",
                "add_authorization_packages.campaign_id",
                "add_authorization_packages.authorization_id",
            ],
            name=f"fk_{prefix}_add_package_binding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["add_unit_id", "add_package_id"],
            ["add_units.add_unit_id", "add_units.add_package_id"],
            name=f"fk_{prefix}_add_unit_binding",
            ondelete="RESTRICT",
        ),
    )


class ExecutionRiskDecision(Base):
    """Immutable final pre-send risk decision; only ALLOW may own a reservation/intent."""

    __tablename__ = "execution_risk_decisions"
    __table_args__ = (
        CheckConstraint("decision_stage = 'ORDER_PRECHECK'", name="ck_exec_risk_stage"),
        CheckConstraint("intent_kind IN ('INITIAL', 'ADD')", name="ck_exec_risk_intent_kind"),
        CheckConstraint("result IN ('ALLOW', 'DENY')", name="ck_exec_risk_result"),
        CheckConstraint(
            "system_risk_state IN ('NORMAL', 'NO_PYRAMID', 'NO_NEW_POSITION', "
            "'REDUCE_ONLY', 'KILL_SWITCH', 'UNKNOWN')",
            name="ck_exec_risk_system_state",
        ),
        CheckConstraint(
            "requested_quantity > 0 AND max_safe_quantity >= 0 AND final_quantity >= 0 "
            "AND max_safe_quantity <= requested_quantity AND final_quantity <= requested_quantity",
            name="ck_exec_risk_quantity_bounds",
        ),
        CheckConstraint(
            "(result = 'ALLOW' AND max_safe_quantity = requested_quantity "
            "AND final_quantity = requested_quantity AND approved_reserved_heat > 0 "
            "AND reservation_created = true AND order_intent_created = true "
            "AND valid_until > decided_at) OR "
            "(result = 'DENY' AND final_quantity = 0 AND approved_reserved_heat = 0 "
            "AND approved_funding = 0 AND approved_margin = 0 "
            "AND reservation_created = false AND order_intent_created = false "
            "AND valid_until = decided_at)",
            name="ck_exec_risk_result_contract",
        ),
        CheckConstraint(
            "approved_reserved_heat >= 0 AND approved_funding >= 0 AND approved_margin >= 0 "
            "AND current_portfolio_mtm_equity IS NOT NULL "
            "AND current_unrealized_pnl IS NOT NULL",
            name="ck_exec_risk_amounts",
        ),
        CheckConstraint(
            "(intent_kind = 'INITIAL' AND initial_authorization_id IS NOT NULL "
            "AND add_package_id IS NULL AND add_unit_id IS NULL) OR "
            "(intent_kind = 'ADD' AND initial_authorization_id IS NULL "
            "AND add_package_id IS NOT NULL AND add_unit_id IS NOT NULL)",
            name="ck_exec_risk_authorization_kind",
        ),
        CheckConstraint(
            "length(input_hash) = 64 AND length(decision_hash) = 64 "
            "AND jsonb_typeof(input_snapshot) = 'object' "
            "AND jsonb_typeof(decision) = 'object'",
            name="ck_exec_risk_snapshot_integrity",
        ),
        CheckConstraint("execution_eligible = false", name="ck_exec_risk_shadow_only"),
        ForeignKeyConstraint(
            ["risk_policy_id", "organization_id", "risk_policy_version"],
            [
                "risk_policies.risk_policy_id",
                "risk_policies.organization_id",
                "risk_policies.policy_version",
            ],
            name="fk_exec_risk_policy_binding",
            ondelete="RESTRICT",
        ),
        *_authorization_binding_constraints("exec_risk"),
        Index("ix_exec_risk_campaign_time", "campaign_id", "decided_at"),
    )

    execution_risk_decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    decision_stage: Mapped[str] = mapped_column(String(40), nullable=False)
    intent_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    authorization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    campaign_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    initial_authorization_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    add_package_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    add_unit_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    risk_policy_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    risk_policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    system_risk_state: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str] = mapped_column(String(20), nullable=False)
    primary_reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    requested_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    max_safe_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    final_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    approved_reserved_heat: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    approved_funding: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    approved_margin: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    current_portfolio_mtm_equity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    current_unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    decision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reservation_created: Mapped[bool] = mapped_column(Boolean, nullable=False)
    order_intent_created: Mapped[bool] = mapped_column(Boolean, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OrderIntent(Base):
    __tablename__ = "order_intents"
    __table_args__ = (
        CheckConstraint("intent_kind IN ('INITIAL', 'ADD')", name="ck_order_intents_kind"),
        CheckConstraint("side IN ('BUY', 'SELL')", name="ck_order_intents_side"),
        CheckConstraint(
            "position_side IN ('LONG', 'SHORT')", name="ck_order_intents_position_side"
        ),
        CheckConstraint("reduce_only = false", name="ck_order_intents_increase_only"),
        CheckConstraint(
            "current_position_quantity >= 0 AND target_position_quantity > 0 "
            "AND expected_quantity > 0 AND max_quantity >= expected_quantity "
            "AND target_position_quantity - current_position_quantity >= expected_quantity",
            name="ck_order_intents_quantity_contract",
        ),
        CheckConstraint(
            "quantity_source IN ('INITIAL_RISK_APPROVED', 'TARGET_LEVERAGE_DELTA')",
            name="ck_order_intents_quantity_source",
        ),
        CheckConstraint(
            "price_reference > 0 AND price_lower_bound > 0 "
            "AND price_upper_bound >= price_lower_bound "
            "AND price_reference BETWEEN price_lower_bound AND price_upper_bound "
            "AND max_slippage_bps >= 0",
            name="ck_order_intents_price_contract",
        ),
        CheckConstraint("valid_until > valid_from", name="ck_order_intents_valid_window"),
        CheckConstraint(
            "execution_mode = 'SHADOW' AND dispatch_eligible = false",
            name="ck_order_intents_shadow_only",
        ),
        CheckConstraint(
            "(intent_kind = 'INITIAL' AND initial_authorization_id IS NOT NULL "
            "AND add_package_id IS NULL AND add_unit_id IS NULL "
            "AND quantity_source = 'INITIAL_RISK_APPROVED') OR "
            "(intent_kind = 'ADD' AND initial_authorization_id IS NULL "
            "AND add_package_id IS NOT NULL AND add_unit_id IS NOT NULL "
            "AND quantity_source = 'TARGET_LEVERAGE_DELTA')",
            name="ck_order_intents_authorization_kind",
        ),
        CheckConstraint(
            "length(candidate_hash) = 64 AND length(intent_snapshot_hash) = 64 "
            "AND jsonb_typeof(intent_snapshot) = 'object'",
            name="ck_order_intents_hashes",
        ),
        UniqueConstraint("execution_risk_decision_id", name="uq_order_intents_risk_decision"),
        UniqueConstraint("campaign_id", "candidate_ref", name="uq_order_intents_candidate"),
        UniqueConstraint(
            "order_intent_id",
            "campaign_id",
            "authorization_id",
            name="uq_order_intents_identity_binding",
        ),
        ForeignKeyConstraint(
            ["execution_risk_decision_id"],
            ["execution_risk_decisions.execution_risk_decision_id"],
            name="fk_order_intents_risk_decision",
            ondelete="RESTRICT",
        ),
        *_authorization_binding_constraints("order_intents"),
        Index("ix_order_intents_campaign_created", "campaign_id", "created_at"),
        Index("ix_order_intents_execution_route", "venue", "execution_domain", "account_id"),
    )

    order_intent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    execution_risk_decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    proposal_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    proposal_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    authorization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    campaign_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    initial_authorization_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    add_package_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    add_unit_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    intent_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    candidate_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    candidate_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_owner: Mapped[str] = mapped_column(String(160), nullable=False)
    venue: Mapped[str] = mapped_column(String(80), nullable=False)
    execution_domain: Mapped[str] = mapped_column(String(120), nullable=False)
    account_id: Mapped[str] = mapped_column(String(160), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(160), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(255), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    position_side: Mapped[str] = mapped_column(String(20), nullable=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    current_position_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    target_position_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    expected_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    max_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    quantity_source: Mapped[str] = mapped_column(String(40), nullable=False)
    order_type: Mapped[str] = mapped_column(String(40), nullable=False)
    time_in_force: Mapped[str] = mapped_column(String(40), nullable=False)
    trigger_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    price_reference: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    price_lower_bound: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    price_upper_bound: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    max_slippage_bps: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    risk_currency: Mapped[str] = mapped_column(String(80), nullable=False)
    margin_mode: Mapped[str] = mapped_column(String(80), nullable=False)
    collateral_scope: Mapped[str] = mapped_column(String(120), nullable=False)
    collateral_pool_id: Mapped[str] = mapped_column(String(160), nullable=False)
    capability_certificate_ref: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("capability_certificates.certificate_id", ondelete="RESTRICT"),
        nullable=False,
    )
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    dispatch_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    intent_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    intent_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OrderIntentState(Base):
    __tablename__ = "order_intent_states"
    __table_args__ = (
        CheckConstraint(
            f"status IN {ORDER_INTENT_STATUSES!s}", name="ck_order_intent_states_status"
        ),
        CheckConstraint("version >= 1", name="ck_order_intent_states_version"),
        CheckConstraint(
            "intent_quantity > 0 AND cumulative_filled_quantity >= 0 "
            "AND known_remaining_quantity >= 0 "
            "AND cumulative_filled_quantity <= intent_quantity "
            "AND known_remaining_quantity <= intent_quantity",
            name="ck_order_intent_states_quantities",
        ),
        CheckConstraint(
            "(last_fact_sequence = 0 AND last_fact_hash IS NULL) OR "
            "(last_fact_sequence > 0 AND length(last_fact_hash) = 64)",
            name="ck_order_intent_states_fact_binding",
        ),
    )

    order_intent_id: Mapped[UUID] = mapped_column(
        ForeignKey("order_intents.order_intent_id", ondelete="RESTRICT"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    intent_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    cumulative_filled_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    known_remaining_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    zero_fill_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    venue_order_terminal: Mapped[bool] = mapped_column(Boolean, nullable=False)
    position_reconciled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    protection_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    last_fact_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    last_fact_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RiskReservation(Base):
    __tablename__ = "risk_reservations"
    __table_args__ = (
        CheckConstraint("intent_kind IN ('INITIAL', 'ADD')", name="ck_risk_reservations_kind"),
        CheckConstraint(
            "reserved_quantity > 0 AND reserved_heat > 0 "
            "AND funding_reserved >= 0 AND margin_reserved >= 0",
            name="ck_risk_reservations_amounts",
        ),
        CheckConstraint("valid_until > created_at", name="ck_risk_reservations_valid_window"),
        CheckConstraint(
            "jsonb_typeof(scope_allocations) = 'array' "
            "AND jsonb_array_length(scope_allocations) = 7",
            name="ck_risk_reservations_scope_allocations",
        ),
        CheckConstraint(
            "(intent_kind = 'INITIAL' AND initial_authorization_id IS NOT NULL "
            "AND add_package_id IS NULL AND add_unit_id IS NULL) OR "
            "(intent_kind = 'ADD' AND initial_authorization_id IS NULL "
            "AND add_package_id IS NOT NULL AND add_unit_id IS NOT NULL)",
            name="ck_risk_reservations_authorization_kind",
        ),
        UniqueConstraint("order_intent_id", name="uq_risk_reservations_order_intent"),
        UniqueConstraint("execution_risk_decision_id", name="uq_risk_reservations_risk_decision"),
        ForeignKeyConstraint(
            ["order_intent_id", "campaign_id", "authorization_id"],
            [
                "order_intents.order_intent_id",
                "order_intents.campaign_id",
                "order_intents.authorization_id",
            ],
            name="fk_risk_reservations_order_binding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["execution_risk_decision_id"],
            ["execution_risk_decisions.execution_risk_decision_id"],
            name="fk_risk_reservations_risk_decision",
            ondelete="RESTRICT",
        ),
        *_authorization_binding_constraints("risk_reservations"),
        Index("ix_risk_reservations_org_created", "organization_id", "created_at"),
    )

    risk_reservation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    execution_risk_decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    order_intent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    authorization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    campaign_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    initial_authorization_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    add_package_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    add_unit_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    intent_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    account_id: Mapped[str] = mapped_column(String(160), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(255), nullable=False)
    collateral_pool_id: Mapped[str] = mapped_column(String(160), nullable=False)
    risk_currency: Mapped[str] = mapped_column(String(80), nullable=False)
    valuation_price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    valuation_price_source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    reserved_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    reserved_heat: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    funding_reserved: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    margin_reserved: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    scope_allocations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RiskExposureState(Base):
    __tablename__ = "risk_exposure_states"
    __table_args__ = (
        CheckConstraint(
            "status IN ('RESERVED', 'PARTIAL', 'UNKNOWN', 'OPEN', 'RELEASED')",
            name="ck_risk_exposure_states_status",
        ),
        CheckConstraint("version >= 1 AND ledger_sequence >= 1", name="ck_risk_exposure_version"),
        CheckConstraint(
            "total_quantity > 0 AND total_heat > 0 AND total_funding >= 0 AND total_margin >= 0",
            name="ck_risk_exposure_totals",
        ),
        CheckConstraint(
            "reserved_quantity >= 0 AND open_quantity >= 0 AND unknown_quantity >= 0 "
            "AND released_quantity >= 0 AND "
            "reserved_quantity + open_quantity + unknown_quantity + released_quantity "
            "= total_quantity",
            name="ck_risk_exposure_quantity_conservation",
        ),
        CheckConstraint(
            "reserved_heat >= 0 AND open_heat >= 0 AND unknown_heat >= 0 "
            "AND released_heat >= 0 AND "
            "reserved_heat + open_heat + unknown_heat + released_heat = total_heat",
            name="ck_risk_exposure_heat_conservation",
        ),
        CheckConstraint(
            "funding_reserved >= 0 AND funding_used >= 0 AND funding_unknown >= 0 "
            "AND funding_released >= 0 AND "
            "funding_reserved + funding_used + funding_unknown + funding_released = total_funding",
            name="ck_risk_exposure_funding_conservation",
        ),
        CheckConstraint(
            "margin_reserved >= 0 AND margin_used >= 0 AND margin_unknown >= 0 "
            "AND margin_released >= 0 AND "
            "margin_reserved + margin_used + margin_unknown + margin_released = total_margin",
            name="ck_risk_exposure_margin_conservation",
        ),
        CheckConstraint("length(last_evidence_hash) = 64", name="ck_risk_exposure_evidence"),
    )

    risk_reservation_id: Mapped[UUID] = mapped_column(
        ForeignKey("risk_reservations.risk_reservation_id", ondelete="RESTRICT"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    ledger_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    total_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    reserved_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    open_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    unknown_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    released_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    total_heat: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    reserved_heat: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    open_heat: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    unknown_heat: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    released_heat: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    total_funding: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    funding_reserved: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    funding_used: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    funding_unknown: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    funding_released: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    total_margin: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    margin_reserved: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    margin_used: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    margin_unknown: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    margin_released: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    last_evidence_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    last_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RiskLedgerEntry(Base):
    __tablename__ = "risk_ledger_entries"
    __table_args__ = (
        CheckConstraint(
            "entry_type IN ('RESERVE', 'MIGRATE', 'RELEASE')",
            name="ck_risk_ledger_entries_type",
        ),
        CheckConstraint(
            "from_bucket IN ('AUTHORIZED', 'RESERVED', 'OPEN', 'UNKNOWN', 'RELEASED') "
            "AND to_bucket IN ('AUTHORIZED', 'RESERVED', 'OPEN', 'UNKNOWN', 'RELEASED') "
            "AND from_bucket <> to_bucket",
            name="ck_risk_ledger_entries_buckets",
        ),
        CheckConstraint(
            "quantity > 0 AND heat > 0 AND funding >= 0 AND margin >= 0",
            name="ck_risk_ledger_entries_amounts",
        ),
        CheckConstraint("length(evidence_hash) = 64", name="ck_risk_ledger_entries_evidence"),
        UniqueConstraint(
            "risk_reservation_id", "entry_sequence", name="uq_risk_ledger_entry_sequence"
        ),
        Index("ix_risk_ledger_entries_intent_time", "order_intent_id", "occurred_at"),
    )

    risk_ledger_entry_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    risk_reservation_id: Mapped[UUID] = mapped_column(
        ForeignKey("risk_reservations.risk_reservation_id", ondelete="RESTRICT"), nullable=False
    )
    order_intent_id: Mapped[UUID] = mapped_column(
        ForeignKey("order_intents.order_intent_id", ondelete="RESTRICT"), nullable=False
    )
    execution_fact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("execution_facts.execution_fact_id", ondelete="RESTRICT"), nullable=True
    )
    entry_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_type: Mapped[str] = mapped_column(String(20), nullable=False)
    from_bucket: Mapped[str] = mapped_column(String(20), nullable=False)
    to_bucket: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    heat: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    funding: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    margin: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExecutionFact(Base):
    __tablename__ = "execution_facts"
    __table_args__ = (
        CheckConstraint(
            f"target_status IN {ORDER_INTENT_STATUSES!s}", name="ck_execution_facts_status"
        ),
        CheckConstraint("fact_sequence >= 1", name="ck_execution_facts_sequence"),
        CheckConstraint(
            "cumulative_filled_quantity >= 0 AND known_remaining_quantity >= 0",
            name="ck_execution_facts_quantities",
        ),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object' AND event_time <= received_at "
            "AND received_at <= recorded_at",
            name="ck_execution_facts_evidence_contract",
        ),
        CheckConstraint(
            "length(payload_hash) = 64 AND length(evidence_hash) = 64",
            name="ck_execution_facts_hashes",
        ),
        UniqueConstraint(
            "order_intent_id", "fact_sequence", name="uq_execution_facts_intent_sequence"
        ),
        UniqueConstraint(
            "venue",
            "execution_domain",
            "account_id",
            "external_fact_id",
            name="uq_execution_facts_external_identity",
        ),
        Index("ix_execution_facts_intent_time", "order_intent_id", "event_time"),
    )

    execution_fact_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    order_intent_id: Mapped[UUID] = mapped_column(
        ForeignKey("order_intents.order_intent_id", ondelete="RESTRICT"), nullable=False
    )
    fact_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    target_status: Mapped[str] = mapped_column(String(32), nullable=False)
    venue: Mapped[str] = mapped_column(String(80), nullable=False)
    execution_domain: Mapped[str] = mapped_column(String(120), nullable=False)
    account_id: Mapped[str] = mapped_column(String(160), nullable=False)
    external_fact_id: Mapped[str] = mapped_column(String(255), nullable=False)
    cumulative_filled_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    known_remaining_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    zero_fill_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    venue_order_terminal: Mapped[bool] = mapped_column(Boolean, nullable=False)
    position_reconciled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    protection_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reconciliation_run_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    source_version: Mapped[str] = mapped_column(String(120), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OrderIntentStateHistory(Base):
    __tablename__ = "order_intent_state_history"
    __table_args__ = (
        CheckConstraint("state_version >= 1", name="ck_order_intent_history_version"),
        CheckConstraint(
            "jsonb_typeof(state_snapshot) = 'object' AND length(state_hash) = 64",
            name="ck_order_intent_history_snapshot",
        ),
        UniqueConstraint(
            "order_intent_id", "state_version", name="uq_order_intent_history_version"
        ),
    )

    history_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_intent_id: Mapped[UUID] = mapped_column(
        ForeignKey("order_intents.order_intent_id", ondelete="RESTRICT"), nullable=False
    )
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    state_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RiskExposureStateHistory(Base):
    __tablename__ = "risk_exposure_state_history"
    __table_args__ = (
        CheckConstraint("state_version >= 1", name="ck_risk_exposure_history_version"),
        CheckConstraint(
            "jsonb_typeof(state_snapshot) = 'object' AND length(state_hash) = 64",
            name="ck_risk_exposure_history_snapshot",
        ),
        UniqueConstraint(
            "risk_reservation_id", "state_version", name="uq_risk_exposure_history_version"
        ),
    )

    history_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    risk_reservation_id: Mapped[UUID] = mapped_column(
        ForeignKey("risk_reservations.risk_reservation_id", ondelete="RESTRICT"), nullable=False
    )
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    state_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
