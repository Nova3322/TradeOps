from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_control_plane.database import Base


class CapabilityEvidenceBundle(Base):
    """Immutable evidence submitted for one non-dispatchable shadow certificate."""

    __tablename__ = "capability_evidence_bundles"
    __table_args__ = (
        CheckConstraint(
            "environment = 'SHADOW' AND certification_profile = 'SHADOW_NON_DISPATCH'",
            name="ck_capability_evidence_shadow_only",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) > 0",
            name="ck_capability_evidence_refs_nonempty",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence_summary) = 'object'",
            name="ck_capability_evidence_summary_object",
        ),
        CheckConstraint("length(evidence_hash) = 64", name="ck_capability_evidence_hash"),
        UniqueConstraint(
            "organization_id",
            "bundle_version",
            name="uq_capability_evidence_org_version",
        ),
        UniqueConstraint(
            "evidence_bundle_id",
            "organization_id",
            name="uq_capability_evidence_identity_binding",
        ),
        Index(
            "ix_capability_evidence_org_created",
            "organization_id",
            "created_at",
        ),
    )

    evidence_bundle_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    bundle_version: Mapped[str] = mapped_column(String(120), nullable=False)
    environment: Mapped[str] = mapped_column(String(24), nullable=False)
    certification_profile: Mapped[str] = mapped_column(String(40), nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    evidence_summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_principal: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CapabilityCertificate(Base):
    """Immutable exact-scope certificate root; WP-0007 is shadow-only by construction."""

    __tablename__ = "capability_certificates"
    __table_args__ = (
        CheckConstraint("schema_version = 1", name="ck_capability_certificates_schema"),
        CheckConstraint(
            "certificate_type IN ('STRATEGY_EVIDENCE', 'EXECUTION', "
            "'RISK_COVERAGE', 'MARGIN_NORMALIZATION')",
            name="ck_capability_certificates_type",
        ),
        CheckConstraint(
            "environment = 'SHADOW' AND real_funds_eligible = false",
            name="ck_capability_certificates_shadow_only",
        ),
        CheckConstraint(
            "jsonb_typeof(scope) = 'object' AND jsonb_typeof(policy_versions) = 'object'",
            name="ck_capability_certificates_contract_objects",
        ),
        CheckConstraint(
            "jsonb_typeof(approver_principal_ids) = 'array' "
            "AND jsonb_array_length(approver_principal_ids) > 0",
            name="ck_capability_certificates_approvers",
        ),
        CheckConstraint(
            "jsonb_typeof(invalidation_conditions) = 'array' "
            "AND jsonb_array_length(invalidation_conditions) > 0",
            name="ck_capability_certificates_invalidation_conditions",
        ),
        CheckConstraint(
            "length(scope_hash) = 64 AND length(policy_versions_hash) = 64 "
            "AND length(evidence_bundle_hash) = 64 AND length(certificate_hash) = 64",
            name="ck_capability_certificates_hashes",
        ),
        CheckConstraint(
            "max_order_notional > 0 AND max_trade_loss > 0",
            name="ck_capability_certificates_limits",
        ),
        CheckConstraint(
            "valid_from >= issued_at AND expires_at > valid_from",
            name="ck_capability_certificates_valid_window",
        ),
        ForeignKeyConstraint(
            ["evidence_bundle_id", "organization_id"],
            [
                "capability_evidence_bundles.evidence_bundle_id",
                "capability_evidence_bundles.organization_id",
            ],
            name="fk_capability_certificates_evidence_bundle",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["supersedes", "organization_id"],
            [
                "capability_certificates.certificate_id",
                "capability_certificates.organization_id",
            ],
            name="fk_capability_certificates_supersedes",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "certificate_id",
            "organization_id",
            name="uq_capability_certificates_identity_binding",
        ),
        Index(
            "ix_capability_certificates_scope_lookup",
            "organization_id",
            "certificate_type",
            "environment",
            "expires_at",
        ),
    )

    certificate_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    certificate_type: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    environment: Mapped[str] = mapped_column(String(24), nullable=False)
    real_funds_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_versions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    policy_versions_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_bundle_id: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    max_order_notional: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    max_trade_loss: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    owner_principal: Mapped[str] = mapped_column(String(255), nullable=False)
    issuer_principal: Mapped[str] = mapped_column(String(255), nullable=False)
    approver_principal_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    approval_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    monitoring_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    exit_recovery_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    invalidation_conditions: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    supersedes: Mapped[str | None] = mapped_column(String(255), nullable=True)
    certificate_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CapabilityCertificateState(Base):
    __tablename__ = "capability_certificate_states"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'REVOKED', 'EXPIRED')",
            name="ck_capability_certificate_states_status",
        ),
        CheckConstraint("version >= 1", name="ck_capability_certificate_states_version"),
    )

    certificate_id: Mapped[str] = mapped_column(
        ForeignKey("capability_certificates.certificate_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CapabilityCertificateStateHistory(Base):
    __tablename__ = "capability_certificate_state_history"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'REVOKED', 'EXPIRED')",
            name="ck_capability_certificate_history_status",
        ),
        CheckConstraint("state_version >= 1", name="ck_capability_certificate_history_version"),
        UniqueConstraint(
            "certificate_id",
            "state_version",
            name="uq_capability_certificate_history_version",
        ),
        Index(
            "ix_capability_certificate_history_time",
            "certificate_id",
            "changed_at",
        ),
    )

    history_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    certificate_id: Mapped[str] = mapped_column(
        ForeignKey("capability_certificates.certificate_id", ondelete="RESTRICT"),
        nullable=False,
    )
    from_status: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
