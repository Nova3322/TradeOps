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


class CampaignTargetPositionFactRecord(Base):
    """Immutable Campaign-owned result of pure target-position arbitration."""

    __tablename__ = "campaign_target_position_facts"
    __table_args__ = (
        CheckConstraint("target_version > 0", name="ck_campaign_target_facts_version"),
        CheckConstraint(
            "current_quantity > 0 AND target_quantity >= 0 "
            "AND target_quantity <= current_quantity "
            "AND reduction_quantity = current_quantity - target_quantity",
            name="ck_campaign_target_facts_quantities",
        ),
        CheckConstraint(
            "(action = 'HOLD' AND target_quantity = current_quantity "
            "AND reduction_quantity = 0 AND requires_order = false AND urgency = 'NONE') OR "
            "(action = 'REDUCE' AND target_quantity > 0 "
            "AND target_quantity < current_quantity AND reduction_quantity > 0 "
            "AND requires_order = true AND urgency IN ('ORDERLY', 'URGENT', 'IMMEDIATE')) OR "
            "(action = 'EXIT' AND target_quantity = 0 AND reduction_quantity > 0 "
            "AND requires_order = true AND urgency IN ('ORDERLY', 'URGENT', 'IMMEDIATE'))",
            name="ck_campaign_target_facts_action",
        ),
        CheckConstraint(
            "reduce_only_required = true",
            name="ck_campaign_target_facts_reduce_only",
        ),
        CheckConstraint(
            "jsonb_typeof(selected_target_source_refs) = 'array' "
            "AND jsonb_typeof(selected_urgency_source_refs) = 'array' "
            "AND jsonb_typeof(target_reason_codes) = 'array' "
            "AND jsonb_typeof(urgency_reason_codes) = 'array' "
            "AND jsonb_typeof(all_reason_codes) = 'array' "
            "AND jsonb_typeof(input_candidate_hashes) = 'array' "
            "AND jsonb_typeof(decision_payload) = 'object'",
            name="ck_campaign_target_facts_arrays",
        ),
        CheckConstraint(
            "decision_facts_as_of <= decision_evaluated_at "
            "AND decision_evaluated_at <= recorded_at",
            name="ck_campaign_target_facts_time_order",
        ),
        CheckConstraint(
            "decision_version = 'target-position-decision-v1' "
            "AND record_version = 'campaign-target-position-fact-v1'",
            name="ck_campaign_target_facts_contract_versions",
        ),
        CheckConstraint(
            "environment = 'SHADOW' AND live_order_eligible = false",
            name="ck_campaign_target_facts_shadow_only",
        ),
        CheckConstraint(
            "length(current_position_binding_hash) = 64 "
            "AND length(current_position_snapshot_hash) = 64 "
            "AND length(decision_hash) = 64 "
            "AND length(target_semantic_hash) = 64 "
            "AND length(record_hash) = 64",
            name="ck_campaign_target_facts_hashes",
        ),
        UniqueConstraint(
            "campaign_id",
            "target_version",
            name="uq_campaign_target_facts_campaign_version",
        ),
        UniqueConstraint(
            "campaign_id",
            "target_semantic_hash",
            name="uq_campaign_target_facts_campaign_semantic",
        ),
        Index(
            "ix_campaign_target_facts_latest",
            "campaign_id",
            "target_version",
        ),
    )

    campaign_target_position_fact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    campaign_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaigns.campaign_id", ondelete="RESTRICT"), nullable=False
    )
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    target_version: Mapped[int] = mapped_column(Integer, nullable=False)
    current_position_binding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    current_position_snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("venue_position_snapshots.venue_position_snapshot_id", ondelete="RESTRICT"),
        nullable=False,
    )
    current_position_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    current_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    target_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    reduction_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    requires_order: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reduce_only_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    urgency: Mapped[str] = mapped_column(String(20), nullable=False)
    selected_target_source_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    selected_urgency_source_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    target_reason_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    urgency_reason_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    all_reason_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    input_candidate_hashes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    decision_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    decision_facts_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision_evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision_version: Mapped[str] = mapped_column(String(80), nullable=False)
    decision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    record_version: Mapped[str] = mapped_column(String(80), nullable=False)
    record_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    live_order_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def contract(self) -> dict[str, Any]:
        return {
            column.name: getattr(self, column.name)
            for column in CampaignTargetPositionFactRecord.__table__.columns
        }
