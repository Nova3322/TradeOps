from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_control_plane.database import Base


class ManagedCapitalScopeManifest(Base):
    """Immutable, explicit account-universe fact; migrations never seed one."""

    __tablename__ = "managed_capital_scope_manifests"
    __table_args__ = (
        CheckConstraint(
            "environment = 'SHADOW' AND real_funds_eligible = false",
            name="ck_managed_capital_scope_manifests_shadow_only",
        ),
        CheckConstraint(
            "risk_inclusion_mode = 'EXCHANGE_ONLY' AND report_currency = 'USD'",
            name="ck_managed_capital_scope_manifests_fixed_policy",
        ),
        CheckConstraint(
            "manifest_version > 0 AND account_scope_count > 0",
            name="ck_managed_capital_scope_manifests_positive_counts",
        ),
        CheckConstraint(
            "valid_until > valid_from",
            name="ck_managed_capital_scope_manifests_valid_window",
        ),
        CheckConstraint(
            "jsonb_typeof(account_scopes) = 'array' "
            "AND jsonb_array_length(account_scopes) = account_scope_count",
            name="ck_managed_capital_scope_manifests_scopes",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) > 0",
            name="ck_managed_capital_scope_manifests_evidence",
        ),
        CheckConstraint(
            "length(manifest_hash) = 64 AND length(evidence_hash) = 64",
            name="ck_managed_capital_scope_manifests_hashes",
        ),
        UniqueConstraint(
            "organization_id",
            "manifest_version",
            name="uq_managed_capital_scope_manifests_org_version",
        ),
        UniqueConstraint(
            "manifest_id",
            "organization_id",
            "manifest_version",
            name="uq_managed_capital_scope_manifests_identity_binding",
        ),
        Index(
            "ix_managed_capital_scope_manifests_lookup",
            "organization_id",
            "manifest_version",
        ),
    )

    manifest_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    manifest_version: Mapped[int] = mapped_column(Integer, nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    real_funds_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    risk_inclusion_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    report_currency: Mapped[str] = mapped_column(String(20), nullable=False)
    account_scopes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    account_scope_count: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
