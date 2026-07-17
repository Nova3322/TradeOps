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
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_control_plane.database import Base


class FrozenProposalVersion(Base):
    __tablename__ = "proposal_versions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_proposal_versions_version_positive"),
        CheckConstraint("source IN ('MANUAL', 'SYSTEM')", name="ck_proposal_versions_source"),
        CheckConstraint(
            "proposal_purpose IN ('INITIAL_ENTRY', 'REDUCE_ONLY')",
            name="ck_proposal_versions_purpose",
        ),
        CheckConstraint(
            "risk_tier IN ('LOW', 'MEDIUM', 'HIGH')",
            name="ck_proposal_versions_risk_tier",
        ),
        CheckConstraint("direction IN ('LONG', 'SHORT')", name="ck_proposal_versions_direction"),
        CheckConstraint(
            "(proposal_purpose = 'INITIAL_ENTRY' AND reduce_only = false) OR "
            "(proposal_purpose = 'REDUCE_ONLY' AND reduce_only = true "
            "AND auto_add_enabled = false AND requested_add_count = 0)",
            name="ck_proposal_versions_reduce_only_contract",
        ),
        CheckConstraint(
            "requested_quantity > 0 AND risk_approved_quantity > 0 "
            "AND risk_approved_quantity <= requested_quantity",
            name="ck_proposal_versions_quantities_positive",
        ),
        CheckConstraint(
            "max_slippage_bps >= 0 AND initial_invalidation_price > 0",
            name="ck_proposal_versions_price_controls",
        ),
        CheckConstraint(
            "requested_max_r > 0 AND requested_max_r <= 3",
            name="ck_proposal_versions_requested_r_range",
        ),
        CheckConstraint(
            "(risk_tier = 'LOW' AND requested_max_r <= 1 AND requested_add_count <= 1) OR "
            "(risk_tier = 'MEDIUM' AND requested_max_r <= 2 AND requested_add_count <= 2) OR "
            "(risk_tier = 'HIGH' AND requested_max_r <= 3 AND requested_add_count <= 3)",
            name="ck_proposal_versions_tier_caps",
        ),
        CheckConstraint(
            "requested_add_count >= 0 AND "
            "((auto_add_enabled = false AND requested_add_count = 0) OR auto_add_enabled = true)",
            name="ck_proposal_versions_auto_add_consistency",
        ),
        CheckConstraint(
            "total_capital_snapshot_0 > 0 AND funding_envelope_0 >= 0 AND one_r_0 > 0 "
            "AND frozen_trade_loss_cap > 0 AND funding_envelope_0 <= total_capital_snapshot_0",
            name="ck_proposal_versions_frozen_risk_positive",
        ),
        CheckConstraint(
            "one_r_0 = total_capital_snapshot_0 * 0.005",
            name="ck_proposal_versions_one_r_formula",
        ),
        CheckConstraint(
            "(risk_tier = 'LOW' AND frozen_trade_loss_cap = one_r_0) OR "
            "(risk_tier = 'MEDIUM' AND frozen_trade_loss_cap = one_r_0 * 2) OR "
            "(risk_tier = 'HIGH' AND frozen_trade_loss_cap = one_r_0 * 3)",
            name="ck_proposal_versions_loss_cap_formula",
        ),
        CheckConstraint(
            "target_leverage_min > 0 AND target_leverage_max >= target_leverage_min AND "
            "((risk_tier = 'LOW' AND target_leverage_max <= 3) OR "
            "(risk_tier = 'MEDIUM' AND target_leverage_max <= 5) OR "
            "(risk_tier = 'HIGH' AND target_leverage_max <= 10))",
            name="ck_proposal_versions_leverage_caps",
        ),
        CheckConstraint(
            "valid_until > valid_from AND frozen_at >= valid_from AND frozen_at < valid_until",
            name="ck_proposal_versions_valid_window",
        ),
        CheckConstraint(
            "length(spec_hash) = 64 AND length(risk_summary_hash) = 64",
            name="ck_proposal_versions_hash_lengths",
        ),
        CheckConstraint(
            "risk_precheck_status = 'PASSED'",
            name="ck_proposal_versions_risk_precheck_passed",
        ),
        CheckConstraint(
            "(source = 'MANUAL' AND creator_principal_id IS NOT NULL "
            "AND creator_service_principal IS NULL) OR "
            "(source = 'SYSTEM' AND creator_principal_id IS NULL "
            "AND creator_service_principal IS NOT NULL "
            "AND business_owner_principal_id IS NOT NULL "
            "AND strategy_id IS NOT NULL AND strategy_version IS NOT NULL)",
            name="ck_proposal_versions_creator_contract",
        ),
        UniqueConstraint("proposal_id", "version", name="uq_proposal_versions_root_version"),
        Index("ix_proposal_versions_review_queue", "organization_id", "valid_until"),
    )

    proposal_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    proposal_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    proposal_purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    creator_principal_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    creator_service_principal: Mapped[str | None] = mapped_column(String(255), nullable=True)
    business_owner_principal_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    strategy_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    strategy_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    account_id: Mapped[str] = mapped_column(String(160), nullable=False)
    venue: Mapped[str] = mapped_column(String(80), nullable=False)
    execution_domain: Mapped[str] = mapped_column(String(120), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(255), nullable=False)
    sector: Mapped[str] = mapped_column(String(80), nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    decision_timeframe: Mapped[str] = mapped_column(String(40), nullable=False)
    order_type: Mapped[str] = mapped_column(String(40), nullable=False)
    trigger_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    max_slippage_bps: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    requested_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    risk_approved_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    initial_invalidation_price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    requested_max_r: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    risk_tier: Mapped[str] = mapped_column(String(20), nullable=False)
    auto_add_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    requested_add_count: Mapped[int] = mapped_column(Integer, nullable=False)
    target_leverage_min: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    target_leverage_max: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    supporting_reason: Mapped[str] = mapped_column(Text, nullable=False)
    counter_thesis: Mapped[str] = mapped_column(Text, nullable=False)
    data_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    market_state: Mapped[str] = mapped_column(String(80), nullable=False)
    total_capital_snapshot_0: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    funding_envelope_0: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    one_r_0: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    frozen_trade_loss_cap: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    risk_decision_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    risk_precheck_status: Mapped[str] = mapped_column(String(20), nullable=False)
    risk_policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    catalog_version: Mapped[str] = mapped_column(String(120), nullable=False)
    execution_capability_version: Mapped[str] = mapped_column(String(120), nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    spec_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    risk_summary_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProposalVersionState(Base):
    __tablename__ = "proposal_version_states"
    __table_args__ = (
        CheckConstraint(
            "status IN ('FROZEN', 'SUPERSEDED', 'EXPIRED', 'CANCELLED')",
            name="ck_proposal_version_states_status",
        ),
        CheckConstraint("version >= 1", name="ck_proposal_version_states_version_positive"),
    )

    proposal_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("proposal_versions.proposal_version_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SystemRiskStateRecord(Base):
    __tablename__ = "system_risk_states"
    __table_args__ = (
        CheckConstraint(
            "status IN ('NORMAL', 'NO_NEW_POSITION', 'NO_PYRAMID', "
            "'REDUCE_ONLY', 'KILL_SWITCH', 'UNKNOWN')",
            name="ck_system_risk_states_status",
        ),
        CheckConstraint("version >= 1", name="ck_system_risk_states_version_positive"),
    )

    organization_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    transition_source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApprovalDecision(Base):
    __tablename__ = "approval_decisions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'RETURNED', 'EXPIRED', 'ABANDONED')",
            name="ck_approval_decisions_status",
        ),
        CheckConstraint("required_quorum IN (1, 2)", name="ck_approval_decisions_quorum"),
        CheckConstraint(
            "approved_count >= 0 AND approved_count <= required_quorum",
            name="ck_approval_decisions_approved_count",
        ),
        CheckConstraint("version >= 1", name="ck_approval_decisions_version_positive"),
        CheckConstraint(
            "(status = 'PENDING' AND terminal_reason_code IS NULL AND terminal_at IS NULL) OR "
            "(status <> 'PENDING' AND terminal_reason_code IS NOT NULL "
            "AND terminal_at IS NOT NULL)",
            name="ck_approval_decisions_terminal_fields",
        ),
        UniqueConstraint("proposal_version_id", name="uq_approval_decisions_proposal_version"),
        UniqueConstraint(
            "approval_decision_id",
            "proposal_version_id",
            name="uq_approval_decisions_identity_binding",
        ),
    )

    approval_decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    proposal_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("proposal_versions.proposal_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    required_quorum: Mapped[int] = mapped_column(Integer, nullable=False)
    approved_count: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    terminal_reason_code: Mapped[str | None] = mapped_column(String(160), nullable=True)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReviewerVote(Base):
    __tablename__ = "reviewer_votes"
    __table_args__ = (
        CheckConstraint(
            "choice IN ('APPROVE', 'REJECT', 'RETURN')",
            name="ck_reviewer_votes_choice",
        ),
        UniqueConstraint(
            "proposal_version_id",
            "reviewer_principal_id",
            name="uq_reviewer_votes_reviewer_version",
        ),
        Index("ix_reviewer_votes_proposal", "proposal_version_id", "decided_at"),
    )

    vote_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    proposal_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("proposal_versions.proposal_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    reviewer_principal_id: Mapped[UUID] = mapped_column(
        ForeignKey("identity_principals.principal_id", ondelete="RESTRICT"),
        nullable=False,
    )
    choice: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    authorization_decision_id: Mapped[UUID] = mapped_column(
        ForeignKey("authorization_decisions.decision_id", ondelete="RESTRICT"),
        nullable=False,
    )
    auth_context_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    risk_summary_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
