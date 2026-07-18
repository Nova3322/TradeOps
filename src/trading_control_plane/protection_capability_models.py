from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_control_plane.database import Base


class InstrumentProtectionCapabilityRecord(Base):
    """Immutable SHADOW-only native protection capability fact."""

    __tablename__ = "instrument_protection_capability_records"
    __table_args__ = (
        CheckConstraint(
            "environment = 'SHADOW' AND real_funds_eligible = false",
            name="ck_instrument_protection_capabilities_shadow_only",
        ),
        CheckConstraint(
            "capability_status IN "
            "('CERTIFIED', 'VALIDATING', 'NOT_SUPPORTED', 'UNKNOWN', 'EXPIRED')",
            name="ck_instrument_protection_capabilities_status",
        ),
        CheckConstraint(
            "protection_confirmation_window_ms > 0",
            name="ck_instrument_protection_capabilities_positive_window",
        ),
        CheckConstraint(
            "valid_until > valid_from AND source_observed_at <= valid_from",
            name="ck_instrument_protection_capabilities_valid_window",
        ),
        CheckConstraint(
            "jsonb_typeof(supported_trigger_price_types) = 'array' "
            "AND jsonb_array_length(supported_trigger_price_types) > 0",
            name="ck_instrument_protection_capabilities_trigger_types",
        ),
        CheckConstraint(
            "jsonb_typeof(supported_protection_order_types) = 'array' "
            "AND jsonb_array_length(supported_protection_order_types) > 0",
            name="ck_instrument_protection_capabilities_order_types",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) > 0",
            name="ck_instrument_protection_capabilities_evidence",
        ),
        CheckConstraint(
            "length(catalog_record_hash) = 64 "
            "AND length(worker_config_hash) = 64 "
            "AND length(credential_fingerprint) = 64 "
            "AND length(record_hash) = 64 AND length(evidence_hash) = 64",
            name="ck_instrument_protection_capabilities_hashes",
        ),
        ForeignKeyConstraint(
            [
                "catalog_record_id",
                "organization_id",
                "catalog_version",
                "classification_version",
            ],
            [
                "instrument_catalog_records.catalog_record_id",
                "instrument_catalog_records.organization_id",
                "instrument_catalog_records.catalog_version",
                "instrument_catalog_records.classification_version",
            ],
            name="fk_instrument_protection_capabilities_catalog",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "organization_id",
            "venue",
            "execution_domain",
            "canonical_instrument_id",
            "account_id",
            "position_mode",
            "margin_mode",
            "collateral_pool_id",
            "catalog_version",
            "classification_version",
            "position_management_template_version",
            name="uq_instrument_protection_capabilities_exact_version",
        ),
        UniqueConstraint(
            "protection_capability_record_id",
            "organization_id",
            "position_management_template_version",
            name="uq_instrument_protection_capabilities_identity_binding",
        ),
        Index(
            "ix_instrument_protection_capabilities_lookup",
            "organization_id",
            "venue",
            "execution_domain",
            "canonical_instrument_id",
            "account_id",
            "position_mode",
            "margin_mode",
            "collateral_pool_id",
            "position_management_template_version",
        ),
    )

    protection_capability_record_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    catalog_record_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    catalog_version: Mapped[str] = mapped_column(String(120), nullable=False)
    classification_version: Mapped[str] = mapped_column(String(120), nullable=False)
    catalog_record_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    venue: Mapped[str] = mapped_column(String(80), nullable=False)
    execution_domain: Mapped[str] = mapped_column(String(120), nullable=False)
    canonical_instrument_id: Mapped[str] = mapped_column(String(255), nullable=False)
    account_id: Mapped[str] = mapped_column(String(160), nullable=False)
    account_abstraction: Mapped[str] = mapped_column(String(80), nullable=False)
    position_mode: Mapped[str] = mapped_column(String(80), nullable=False)
    margin_mode: Mapped[str] = mapped_column(String(80), nullable=False)
    collateral_scope: Mapped[str] = mapped_column(String(120), nullable=False)
    collateral_pool_id: Mapped[str] = mapped_column(String(160), nullable=False)
    position_management_template_version: Mapped[str] = mapped_column(String(120), nullable=False)
    execution_capability_version: Mapped[str] = mapped_column(String(120), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(120), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(160), nullable=False)
    worker_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    freqtrade_worker_version: Mapped[str] = mapped_column(String(120), nullable=False)
    account_capability_version: Mapped[str] = mapped_column(String(120), nullable=False)
    credential_permission_profile_version: Mapped[str] = mapped_column(String(120), nullable=False)
    venue_client_version: Mapped[str] = mapped_column(String(120), nullable=False)
    capability_status: Mapped[str] = mapped_column(String(32), nullable=False)
    native_protection_supported: Mapped[bool] = mapped_column(Boolean, nullable=False)
    conditional_orders_supported: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reduce_only_supported: Mapped[bool] = mapped_column(Boolean, nullable=False)
    partial_fill_protection_supported: Mapped[bool] = mapped_column(Boolean, nullable=False)
    protection_replacement_supported: Mapped[bool] = mapped_column(Boolean, nullable=False)
    protection_confirmation_window_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    supported_trigger_price_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    supported_protection_order_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    real_funds_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    record_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
