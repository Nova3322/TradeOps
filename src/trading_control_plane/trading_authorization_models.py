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


class TradingAuthorization(Base):
    """Immutable human-approved capacity; it is never reserved/open Heat."""

    __tablename__ = "trading_authorizations"
    __table_args__ = (
        CheckConstraint("source IN ('MANUAL', 'SYSTEM')", name="ck_trading_auth_source"),
        CheckConstraint("risk_tier IN ('LOW', 'MEDIUM', 'HIGH')", name="ck_trading_auth_tier"),
        CheckConstraint(
            "authorized_loss_capacity > 0 AND approved_initial_quantity > 0 "
            "AND total_capital_snapshot_0 > 0 AND one_r_0 > 0 "
            "AND frozen_trade_loss_cap > 0 AND funding_envelope_0 >= 0 "
            "AND authorized_loss_capacity <= frozen_trade_loss_cap",
            name="ck_trading_auth_capacity_bounds",
        ),
        CheckConstraint(
            "one_r_0 = total_capital_snapshot_0 * 0.005",
            name="ck_trading_auth_one_r_formula",
        ),
        CheckConstraint(
            "(risk_tier = 'LOW' AND frozen_trade_loss_cap = one_r_0) OR "
            "(risk_tier = 'MEDIUM' AND frozen_trade_loss_cap = one_r_0 * 2) OR "
            "(risk_tier = 'HIGH' AND frozen_trade_loss_cap = one_r_0 * 3)",
            name="ck_trading_auth_loss_cap_formula",
        ),
        CheckConstraint(
            "requested_add_count >= 0 AND requested_add_count <= 3 AND "
            "((auto_add_enabled = false AND requested_add_count = 0) OR "
            "(auto_add_enabled = true AND requested_add_count > 0))",
            name="ck_trading_auth_add_contract",
        ),
        CheckConstraint(
            "authorization_mode = 'SHADOW' AND execution_eligible = false",
            name="ck_trading_auth_shadow_only",
        ),
        CheckConstraint(
            "length(proposal_spec_hash) = 64 AND length(risk_summary_hash) = 64 "
            "AND length(issuance_snapshot_hash) = 64",
            name="ck_trading_auth_hash_lengths",
        ),
        CheckConstraint(
            "jsonb_typeof(issuance_snapshot) = 'object'",
            name="ck_trading_auth_snapshot_object",
        ),
        CheckConstraint("valid_until > issued_at", name="ck_trading_auth_valid_window"),
        UniqueConstraint("proposal_version_id", name="uq_trading_auth_proposal_version"),
        UniqueConstraint("approval_decision_id", name="uq_trading_auth_approval_decision"),
        UniqueConstraint(
            "authorization_id",
            "proposal_version_id",
            name="uq_trading_auth_identity_binding",
        ),
        ForeignKeyConstraint(
            ["approval_decision_id", "proposal_version_id"],
            [
                "approval_decisions.approval_decision_id",
                "approval_decisions.proposal_version_id",
            ],
            name="fk_trading_auth_approval_proposal_binding",
            ondelete="RESTRICT",
        ),
        Index("ix_trading_auth_org_issued", "organization_id", "issued_at"),
    )

    authorization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    proposal_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("proposal_versions.proposal_version_id", ondelete="RESTRICT"), nullable=False
    )
    approval_decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    risk_tier: Mapped[str] = mapped_column(String(20), nullable=False)
    authorized_loss_capacity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    approved_initial_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    auto_add_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    requested_add_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_capital_snapshot_0: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    one_r_0: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    frozen_trade_loss_cap: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    funding_envelope_0: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    risk_policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    authorization_policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    catalog_version: Mapped[str] = mapped_column(String(120), nullable=False)
    execution_capability_version: Mapped[str] = mapped_column(String(120), nullable=False)
    capability_certificate_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    proposal_spec_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_summary_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    authorization_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    execution_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    issuance_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    issuance_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint("direction IN ('LONG', 'SHORT')", name="ck_campaigns_direction"),
        CheckConstraint("one_r_0 > 0", name="ck_campaigns_one_r_positive"),
        UniqueConstraint("authorization_id", name="uq_campaigns_authorization"),
        UniqueConstraint("campaign_id", "authorization_id", name="uq_campaigns_identity_binding"),
        ForeignKeyConstraint(
            ["authorization_id", "proposal_version_id"],
            [
                "trading_authorizations.authorization_id",
                "trading_authorizations.proposal_version_id",
            ],
            name="fk_campaigns_authorization_proposal_binding",
            ondelete="RESTRICT",
        ),
        Index("ix_campaigns_org_created", "organization_id", "created_at"),
    )

    campaign_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    authorization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    proposal_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    proposal_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(160), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(80), nullable=False)
    execution_domain: Mapped[str] = mapped_column(String(120), nullable=False)
    account_id: Mapped[str] = mapped_column(String(160), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(255), nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    one_r_0: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    funding_envelope_0: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CampaignState(Base):
    __tablename__ = "campaign_states"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING_ENTRY', 'OPEN', 'CLOSING', 'CLOSED')",
            name="ck_campaign_states_status",
        ),
        CheckConstraint("version >= 1", name="ck_campaign_states_version"),
    )

    campaign_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaigns.campaign_id", ondelete="RESTRICT"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InitialOrderAuthorization(Base):
    __tablename__ = "initial_order_authorizations"
    __table_args__ = (
        CheckConstraint("direction IN ('LONG', 'SHORT')", name="ck_initial_auth_direction"),
        CheckConstraint(
            "max_quantity > 0 AND authorized_loss_capacity > 0",
            name="ck_initial_auth_capacity",
        ),
        CheckConstraint(
            "price_reference > 0 AND price_lower_bound > 0 "
            "AND price_upper_bound >= price_lower_bound "
            "AND price_reference BETWEEN price_lower_bound AND price_upper_bound",
            name="ck_initial_auth_price_bounds",
        ),
        CheckConstraint("valid_until > valid_from", name="ck_initial_auth_valid_window"),
        UniqueConstraint("authorization_id", name="uq_initial_auth_root"),
        UniqueConstraint("campaign_id", name="uq_initial_auth_campaign"),
        UniqueConstraint(
            "initial_authorization_id",
            "campaign_id",
            "authorization_id",
            name="uq_initial_auth_identity_binding",
        ),
        ForeignKeyConstraint(
            ["campaign_id", "authorization_id"],
            ["campaigns.campaign_id", "campaigns.authorization_id"],
            name="fk_initial_auth_campaign_authorization_binding",
            ondelete="RESTRICT",
        ),
    )

    initial_authorization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    authorization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    campaign_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[str] = mapped_column(String(160), nullable=False)
    account_abstraction: Mapped[str] = mapped_column(String(80), nullable=False)
    margin_mode: Mapped[str] = mapped_column(String(80), nullable=False)
    collateral_scope: Mapped[str] = mapped_column(String(120), nullable=False)
    collateral_pool_id: Mapped[str] = mapped_column(String(160), nullable=False)
    instrument_id: Mapped[str] = mapped_column(String(255), nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    max_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    authorized_loss_capacity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    price_reference: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    price_lower_bound: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    price_upper_bound: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    position_management_template_version: Mapped[str] = mapped_column(String(120), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InitialAuthorizationState(Base):
    __tablename__ = "initial_authorization_states"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'CONSUMED', 'EXPIRED', 'REVOKED', 'INVALIDATED')",
            name="ck_initial_auth_states_status",
        ),
        CheckConstraint("version >= 1", name="ck_initial_auth_states_version"),
    )

    initial_authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("initial_order_authorizations.initial_authorization_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AddAuthorizationPackage(Base):
    __tablename__ = "add_authorization_packages"
    __table_args__ = (
        CheckConstraint("direction IN ('LONG', 'SHORT')", name="ck_add_packages_direction"),
        CheckConstraint("authorized_add_count BETWEEN 1 AND 3", name="ck_add_packages_count"),
        CheckConstraint(
            "target_leverage_min > 0 AND target_leverage_max >= target_leverage_min",
            name="ck_add_packages_leverage",
        ),
        CheckConstraint("valid_until > valid_from", name="ck_add_packages_valid_window"),
        UniqueConstraint("authorization_id", name="uq_add_packages_root"),
        UniqueConstraint("campaign_id", name="uq_add_packages_campaign"),
        UniqueConstraint(
            "add_package_id",
            "campaign_id",
            "authorization_id",
            name="uq_add_packages_identity_binding",
        ),
        ForeignKeyConstraint(
            ["campaign_id", "authorization_id"],
            ["campaigns.campaign_id", "campaigns.authorization_id"],
            name="fk_add_packages_campaign_authorization_binding",
            ondelete="RESTRICT",
        ),
    )

    add_package_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    authorization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    campaign_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    authorized_add_count: Mapped[int] = mapped_column(Integer, nullable=False)
    target_leverage_min: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    target_leverage_max: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    add_milestone_policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AddAuthorizationPackageState(Base):
    __tablename__ = "add_authorization_package_states"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DORMANT', 'ACTIVE', 'EXHAUSTED', 'REVOKED', 'EXPIRED', 'INVALIDATED')",
            name="ck_add_package_states_status",
        ),
        CheckConstraint("version >= 1", name="ck_add_package_states_version"),
    )

    add_package_id: Mapped[UUID] = mapped_column(
        ForeignKey("add_authorization_packages.add_package_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AddUnit(Base):
    __tablename__ = "add_units"
    __table_args__ = (
        CheckConstraint("ordinal BETWEEN 1 AND 3", name="ck_add_units_ordinal"),
        CheckConstraint(
            "(ordinal = 1 AND unlock_milestone_pct = 30) OR "
            "(ordinal = 2 AND unlock_milestone_pct = 50) OR "
            "(ordinal = 3 AND unlock_milestone_pct = 100)",
            name="ck_add_units_milestone",
        ),
        UniqueConstraint("add_package_id", "ordinal", name="uq_add_units_package_ordinal"),
        UniqueConstraint("add_unit_id", "add_package_id", name="uq_add_units_identity_binding"),
    )

    add_unit_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    add_package_id: Mapped[UUID] = mapped_column(
        ForeignKey("add_authorization_packages.add_package_id", ondelete="RESTRICT"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    unlock_milestone_pct: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AddUnitState(Base):
    __tablename__ = "add_unit_states"
    __table_args__ = (
        CheckConstraint(
            "status IN ('AVAILABLE', 'CLAIMED', 'CONSUMED', 'EXPIRED', 'INVALIDATED')",
            name="ck_add_unit_states_status",
        ),
        CheckConstraint("version >= 1", name="ck_add_unit_states_version"),
    )

    add_unit_id: Mapped[UUID] = mapped_column(
        ForeignKey("add_units.add_unit_id", ondelete="RESTRICT"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuthorizationStateTransition(Base):
    __tablename__ = "authorization_state_transitions"
    __table_args__ = (
        CheckConstraint(
            "subject_type IN ('CAMPAIGN', 'INITIAL', 'ADD_PACKAGE', 'ADD_UNIT')",
            name="ck_auth_state_transitions_subject_type",
        ),
        CheckConstraint("state_version >= 1", name="ck_auth_state_transitions_version"),
        UniqueConstraint(
            "subject_type", "subject_id", "state_version", name="uq_auth_state_transition_version"
        ),
        Index("ix_auth_state_transitions_root_time", "authorization_id", "changed_at"),
    )

    transition_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("trading_authorizations.authorization_id", ondelete="RESTRICT"), nullable=False
    )
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    from_status: Mapped[str] = mapped_column(String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
