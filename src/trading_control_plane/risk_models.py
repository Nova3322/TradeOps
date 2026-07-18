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


class RiskPolicyRecord(Base):
    """Immutable, explicitly supplied policy fact; migrations never seed one."""

    __tablename__ = "risk_policies"
    __table_args__ = (
        CheckConstraint("length(policy_hash) = 64", name="ck_risk_policies_hash_length"),
        CheckConstraint("policy_mode = 'SHADOW'", name="ck_risk_policies_shadow_only"),
        CheckConstraint("valid_until > valid_from", name="ck_risk_policies_valid_window"),
        CheckConstraint(
            "jsonb_typeof(parameters) = 'object'",
            name="ck_risk_policies_parameters_object",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) > 0",
            name="ck_risk_policies_evidence_nonempty",
        ),
        UniqueConstraint(
            "organization_id",
            "policy_version",
            name="uq_risk_policies_organization_version",
        ),
        UniqueConstraint(
            "risk_policy_id",
            "organization_id",
            "policy_version",
            name="uq_risk_policies_identity_binding",
        ),
        Index("ix_risk_policies_lookup", "organization_id", "policy_version"),
    )

    risk_policy_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    policy_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RiskDecisionSnapshot(Base):
    """Immutable input/output evidence for one non-executable proposal precheck."""

    __tablename__ = "risk_decision_snapshots"
    __table_args__ = (
        CheckConstraint(
            "decision_stage = 'PROPOSAL_PRECHECK'",
            name="ck_risk_decisions_precheck_only",
        ),
        CheckConstraint(
            "result IN ('ALLOW', 'DENY')",
            name="ck_risk_decisions_result",
        ),
        CheckConstraint(
            "risk_tier IN ('LOW', 'MEDIUM', 'HIGH')",
            name="ck_risk_decisions_tier",
        ),
        CheckConstraint(
            "system_risk_state IN ('NORMAL', 'NO_PYRAMID', 'NO_NEW_POSITION', "
            "'REDUCE_ONLY', 'KILL_SWITCH', 'UNKNOWN')",
            name="ck_risk_decisions_system_state",
        ),
        CheckConstraint(
            "requested_quantity > 0 AND max_safe_quantity >= 0 AND final_quantity >= 0 "
            "AND max_safe_quantity <= requested_quantity "
            "AND final_quantity <= requested_quantity",
            name="ck_risk_decisions_quantity_bounds",
        ),
        CheckConstraint(
            "(result = 'ALLOW' AND max_safe_quantity = requested_quantity "
            "AND final_quantity = requested_quantity) OR "
            "(result = 'DENY' AND final_quantity = 0)",
            name="ck_risk_decisions_result_quantities",
        ),
        CheckConstraint(
            "total_capital_snapshot_0 > 0 AND one_r_0 > 0 "
            "AND frozen_trade_loss_cap > 0 AND dynamic_trade_loss_cap >= 0 "
            "AND effective_trade_loss_cap >= 0 AND trade_worst_case_loss_before >= 0 "
            "AND trade_worst_case_loss_after >= trade_worst_case_loss_before",
            name="ck_risk_decisions_loss_bounds",
        ),
        CheckConstraint(
            "one_r_0 = total_capital_snapshot_0 * 0.005",
            name="ck_risk_decisions_one_r_formula",
        ),
        CheckConstraint(
            "(risk_tier = 'LOW' AND frozen_trade_loss_cap = one_r_0) OR "
            "(risk_tier = 'MEDIUM' AND frozen_trade_loss_cap = one_r_0 * 2) OR "
            "(risk_tier = 'HIGH' AND frozen_trade_loss_cap = one_r_0 * 3)",
            name="ck_risk_decisions_tier_loss_formula",
        ),
        CheckConstraint(
            "execution_eligible = false AND reservation_created = false",
            name="ck_risk_decisions_no_execution_side_effect",
        ),
        CheckConstraint(
            "length(input_hash) = 64 AND length(decision_hash) = 64",
            name="ck_risk_decisions_hash_lengths",
        ),
        CheckConstraint(
            "length(capital_scope_manifest_hash) = 64 "
            "AND length(capital_projection_hash) = 64 "
            "AND capital_projection_version ~ '^portfolio-mtm-v[0-9]+$'",
            name="ck_risk_decisions_capital_binding_integrity",
        ),
        CheckConstraint(
            "jsonb_typeof(input_snapshot) = 'object' AND jsonb_typeof(decision) = 'object'",
            name="ck_risk_decisions_json_objects",
        ),
        CheckConstraint(
            "(result = 'ALLOW' AND valid_until >= decided_at) OR "
            "(result = 'DENY' AND valid_until = decided_at)",
            name="ck_risk_decisions_validity",
        ),
        ForeignKeyConstraint(
            ["risk_policy_id", "organization_id", "risk_policy_version"],
            [
                "risk_policies.risk_policy_id",
                "risk_policies.organization_id",
                "risk_policies.policy_version",
            ],
            name="fk_risk_decisions_policy_binding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "capital_scope_manifest_id",
                "organization_id",
                "capital_scope_manifest_version",
            ],
            [
                "managed_capital_scope_manifests.manifest_id",
                "managed_capital_scope_manifests.organization_id",
                "managed_capital_scope_manifests.manifest_version",
            ],
            name="fk_risk_decisions_capital_scope_manifest",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_risk_decisions_proposal",
            "organization_id",
            "proposal_ref",
            "decided_at",
        ),
        Index(
            "ix_risk_decisions_result",
            "organization_id",
            "result",
            "decided_at",
        ),
    )

    risk_decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    proposal_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    decision_stage: Mapped[str] = mapped_column(String(40), nullable=False)
    result: Mapped[str] = mapped_column(String(20), nullable=False)
    primary_reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    risk_tier: Mapped[str] = mapped_column(String(20), nullable=False)
    system_risk_state: Mapped[str] = mapped_column(String(32), nullable=False)
    risk_policy_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    risk_policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    capital_scope_manifest_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    capital_scope_manifest_version: Mapped[int] = mapped_column(Integer, nullable=False)
    capital_scope_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    capital_projection_version: Mapped[str] = mapped_column(String(40), nullable=False)
    capital_projection_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    max_safe_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    final_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    current_unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    current_portfolio_mtm_equity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    total_capital_snapshot_0: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    one_r_0: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    frozen_trade_loss_cap: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    dynamic_trade_loss_cap: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    effective_trade_loss_cap: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    trade_worst_case_loss_before: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    trade_worst_case_loss_after: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    decision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reservation_created: Mapped[bool] = mapped_column(Boolean, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SystemRiskStateTransition(Base):
    """Append-only history projected automatically from current-state updates."""

    __tablename__ = "system_risk_state_transitions"
    __table_args__ = (
        CheckConstraint(
            "from_status IN ('NORMAL', 'NO_PYRAMID', 'NO_NEW_POSITION', "
            "'REDUCE_ONLY', 'KILL_SWITCH', 'UNKNOWN')",
            name="ck_system_risk_transitions_from_status",
        ),
        CheckConstraint(
            "to_status IN ('NORMAL', 'NO_PYRAMID', 'NO_NEW_POSITION', "
            "'REDUCE_ONLY', 'KILL_SWITCH', 'UNKNOWN')",
            name="ck_system_risk_transitions_to_status",
        ),
        CheckConstraint(
            "transition_kind IN ('INITIAL', 'AUTOMATIC_TIGHTEN')",
            name="ck_system_risk_transitions_kind",
        ),
        CheckConstraint("state_version >= 1", name="ck_system_risk_transitions_version"),
        UniqueConstraint(
            "organization_id",
            "state_version",
            name="uq_system_risk_transitions_version",
        ),
        Index(
            "ix_system_risk_transitions_org_time",
            "organization_id",
            "changed_at",
        ),
    )

    transition_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("system_risk_states.organization_id", ondelete="RESTRICT"),
        nullable=False,
    )
    from_status: Mapped[str] = mapped_column(String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    transition_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
