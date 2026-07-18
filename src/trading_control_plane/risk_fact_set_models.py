from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_control_plane.database import Base


class RiskFactSetRecord(Base):
    """Immutable SHADOW-only complete fact-health set for one exact risk scope."""

    __tablename__ = "risk_fact_sets"
    __table_args__ = (
        CheckConstraint(
            "environment = 'SHADOW' AND real_funds_eligible = false",
            name="ck_risk_fact_sets_shadow_only",
        ),
        CheckConstraint(
            "valid_until > valid_from AND valid_from >= assembled_at",
            name="ck_risk_fact_sets_valid_window",
        ),
        CheckConstraint(
            "jsonb_typeof(observations) = 'array' AND jsonb_array_length(observations) = 9",
            name="ck_risk_fact_sets_complete_observations",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) > 0",
            name="ck_risk_fact_sets_evidence",
        ),
        CheckConstraint(
            "length(record_hash) = 64 AND length(evidence_hash) = 64",
            name="ck_risk_fact_sets_hashes",
        ),
        UniqueConstraint(
            "organization_id",
            "venue",
            "execution_domain",
            "account_id",
            "canonical_instrument_id",
            "position_mode",
            "margin_mode",
            "collateral_pool_id",
            "fact_set_version",
            name="uq_risk_fact_sets_exact_version",
        ),
        UniqueConstraint(
            "risk_fact_set_id",
            "organization_id",
            "fact_set_version",
            name="uq_risk_fact_sets_identity_binding",
        ),
        Index(
            "ix_risk_fact_sets_latest_scope",
            "organization_id",
            "venue",
            "execution_domain",
            "account_id",
            "canonical_instrument_id",
            "position_mode",
            "margin_mode",
            "collateral_pool_id",
            "assembled_at",
        ),
    )

    risk_fact_set_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(80), nullable=False)
    execution_domain: Mapped[str] = mapped_column(String(120), nullable=False)
    account_id: Mapped[str] = mapped_column(String(160), nullable=False)
    canonical_instrument_id: Mapped[str] = mapped_column(String(255), nullable=False)
    position_mode: Mapped[str] = mapped_column(String(80), nullable=False)
    margin_mode: Mapped[str] = mapped_column(String(80), nullable=False)
    collateral_pool_id: Mapped[str] = mapped_column(String(160), nullable=False)
    fact_set_version: Mapped[str] = mapped_column(String(120), nullable=False)
    observations: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    real_funds_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    assembled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    record_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
