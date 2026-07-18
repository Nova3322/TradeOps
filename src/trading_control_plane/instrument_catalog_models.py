from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_control_plane.database import Base


class InstrumentCatalogRecord(Base):
    """Immutable SHADOW-only instrument identity and classification fact."""

    __tablename__ = "instrument_catalog_records"
    __table_args__ = (
        CheckConstraint(
            "environment = 'SHADOW' AND real_funds_eligible = false",
            name="ck_instrument_catalog_records_shadow_only",
        ),
        CheckConstraint(
            "contract_type = 'PERPETUAL'",
            name="ck_instrument_catalog_records_contract_type",
        ),
        CheckConstraint(
            "approval_scope IN ('NONE', 'OBSERVE', 'RESEARCH')",
            name="ck_instrument_catalog_records_approval_scope",
        ),
        CheckConstraint(
            "listing_status IN ('TRADING', 'REDUCE_ONLY', 'HALTED', 'DELISTING', 'RETIRED')",
            name="ck_instrument_catalog_records_listing_status",
        ),
        CheckConstraint(
            "sector IN ('CRYPTO', 'EQUITY_INDEX', 'PRECIOUS_METALS', 'COMMODITY', 'UNCLASSIFIED')",
            name="ck_instrument_catalog_records_sector",
        ),
        CheckConstraint(
            "contract_multiplier > 0 AND tick_size > 0 AND lot_size > 0 "
            "AND minimum_quantity > 0 AND minimum_notional >= 0",
            name="ck_instrument_catalog_records_positive_rules",
        ),
        CheckConstraint(
            "valid_until > valid_from AND source_observed_at <= valid_from",
            name="ck_instrument_catalog_records_valid_window",
        ),
        CheckConstraint(
            "jsonb_typeof(risk_cluster_ids) = 'array' AND jsonb_array_length(risk_cluster_ids) > 0",
            name="ck_instrument_catalog_records_risk_clusters",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) > 0",
            name="ck_instrument_catalog_records_evidence",
        ),
        CheckConstraint(
            "classification_complete = false OR sector <> 'UNCLASSIFIED'",
            name="ck_instrument_catalog_records_complete_classification",
        ),
        CheckConstraint(
            "length(record_hash) = 64 AND length(evidence_hash) = 64",
            name="ck_instrument_catalog_records_hashes",
        ),
        UniqueConstraint(
            "organization_id",
            "venue",
            "execution_domain",
            "canonical_instrument_id",
            "catalog_version",
            "classification_version",
            name="uq_instrument_catalog_records_exact_version",
        ),
        UniqueConstraint(
            "catalog_record_id",
            "organization_id",
            "catalog_version",
            "classification_version",
            name="uq_instrument_catalog_records_identity_binding",
        ),
        Index(
            "ix_instrument_catalog_records_lookup",
            "organization_id",
            "venue",
            "execution_domain",
            "canonical_instrument_id",
            "catalog_version",
            "classification_version",
        ),
    )

    catalog_record_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(80), nullable=False)
    execution_domain: Mapped[str] = mapped_column(String(120), nullable=False)
    native_instrument_id: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_instrument_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_symbol: Mapped[str] = mapped_column(String(120), nullable=False)
    catalog_version: Mapped[str] = mapped_column(String(120), nullable=False)
    metadata_version: Mapped[str] = mapped_column(String(120), nullable=False)
    classification_version: Mapped[str] = mapped_column(String(120), nullable=False)
    contract_type: Mapped[str] = mapped_column(String(40), nullable=False)
    underlying_id: Mapped[str] = mapped_column(String(160), nullable=False)
    sector: Mapped[str] = mapped_column(String(80), nullable=False)
    risk_cluster_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    quote_asset: Mapped[str] = mapped_column(String(80), nullable=False)
    settlement_asset: Mapped[str] = mapped_column(String(80), nullable=False)
    collateral_asset: Mapped[str] = mapped_column(String(80), nullable=False)
    contract_multiplier: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    tick_size: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    lot_size: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    minimum_quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    minimum_notional: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    discoverable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    classification_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    approval_scope: Mapped[str] = mapped_column(String(20), nullable=False)
    listing_status: Mapped[str] = mapped_column(String(32), nullable=False)
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


InstrumentCatalogJson = dict[str, Any]
