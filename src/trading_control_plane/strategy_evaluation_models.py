from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_control_plane.database import Base


class StrategyEvaluationRecord(Base):
    """Immutable SHADOW-only strategy judgment over exact current ADD facts."""

    __tablename__ = "strategy_evaluation_records"
    __table_args__ = (
        CheckConstraint(
            "evaluation_kind = 'ADD_CONTINUATION'",
            name="ck_strategy_evaluations_kind",
        ),
        CheckConstraint(
            "outcome IN ('PASS', 'FAIL', 'UNKNOWN')",
            name="ck_strategy_evaluations_outcome",
        ),
        CheckConstraint(
            "environment = 'SHADOW' AND real_funds_eligible = false",
            name="ck_strategy_evaluations_shadow_only",
        ),
        CheckConstraint(
            "valid_until > valid_from AND valid_from >= evaluated_at",
            name="ck_strategy_evaluations_valid_window",
        ),
        CheckConstraint(
            "jsonb_typeof(rule_results) = 'array' AND jsonb_array_length(rule_results) = 3",
            name="ck_strategy_evaluations_complete_rules",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) > 0",
            name="ck_strategy_evaluations_evidence",
        ),
        CheckConstraint(
            "length(risk_fact_set_record_hash) = 64 "
            "AND length(position_snapshot_hash) = 64 "
            "AND length(protection_snapshot_hash) = 64 "
            "AND length(record_hash) = 64 AND length(evidence_hash) = 64",
            name="ck_strategy_evaluations_hashes",
        ),
        ForeignKeyConstraint(
            ["risk_fact_set_id", "organization_id", "risk_fact_set_version"],
            [
                "risk_fact_sets.risk_fact_set_id",
                "risk_fact_sets.organization_id",
                "risk_fact_sets.fact_set_version",
            ],
            name="fk_strategy_evaluations_risk_fact_set",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "campaign_id",
            "evaluation_version",
            name="uq_strategy_evaluations_campaign_version",
        ),
        UniqueConstraint(
            "strategy_evaluation_id",
            "campaign_id",
            "evaluation_version",
            name="uq_strategy_evaluations_identity_binding",
        ),
        Index(
            "ix_strategy_evaluations_latest_campaign",
            "campaign_id",
            "evaluated_at",
        ),
    )

    strategy_evaluation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    campaign_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaigns.campaign_id", ondelete="RESTRICT"), nullable=False
    )
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(160), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    strategy_parameter_version: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(80), nullable=False)
    execution_domain: Mapped[str] = mapped_column(String(120), nullable=False)
    account_id: Mapped[str] = mapped_column(String(160), nullable=False)
    canonical_instrument_id: Mapped[str] = mapped_column(String(255), nullable=False)
    position_mode: Mapped[str] = mapped_column(String(80), nullable=False)
    margin_mode: Mapped[str] = mapped_column(String(80), nullable=False)
    collateral_pool_id: Mapped[str] = mapped_column(String(160), nullable=False)
    evaluation_version: Mapped[str] = mapped_column(String(120), nullable=False)
    evaluation_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    rule_results: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    risk_fact_set_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    risk_fact_set_version: Mapped[str] = mapped_column(String(120), nullable=False)
    risk_fact_set_record_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    position_snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("venue_position_snapshots.venue_position_snapshot_id", ondelete="RESTRICT"),
        nullable=False,
    )
    position_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    protection_snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("venue_protection_snapshots.venue_protection_snapshot_id", ondelete="RESTRICT"),
        nullable=False,
    )
    protection_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluator_version: Mapped[str] = mapped_column(String(120), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    real_funds_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    record_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
