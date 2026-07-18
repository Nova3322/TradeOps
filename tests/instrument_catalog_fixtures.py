from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import CommandChannel, CommandEnvelope
from trading_control_plane.database import Database
from trading_control_plane.instrument_catalog import (
    INSTRUMENT_CATALOG_SERVICE_PRINCIPAL,
    CatalogEnvironment,
    InstrumentApprovalScope,
    InstrumentCatalogService,
    InstrumentContractType,
    InstrumentListingStatus,
    InstrumentSector,
    RegisterInstrumentCatalogRecordDraft,
    RegisterInstrumentCatalogRecordRequest,
    instrument_catalog_evidence_hash,
    instrument_catalog_record_hash,
)
from trading_control_plane.risk import RiskPrecheckRequest


def instrument_catalog_request_for_risk(
    risk_request: RiskPrecheckRequest,
    *,
    now: datetime,
    **updates: Any,
) -> RegisterInstrumentCatalogRecordRequest:
    binding = risk_request.binding
    identity = ":".join(
        (
            risk_request.organization_id,
            binding.venue,
            binding.execution_domain,
            binding.instrument_identity,
            binding.catalog_version,
            binding.instrument_scope_version,
        )
    )
    values: dict[str, Any] = {
        "catalog_record_id": uuid5(NAMESPACE_URL, identity),
        "organization_id": risk_request.organization_id,
        "venue": binding.venue,
        "execution_domain": binding.execution_domain,
        "native_instrument_id": "BTCUSDT",
        "canonical_instrument_id": binding.instrument_identity,
        "display_symbol": "BTC/USDT:USDT",
        "catalog_version": binding.catalog_version,
        "metadata_version": "metadata-test-v1",
        "classification_version": binding.instrument_scope_version,
        "contract_type": InstrumentContractType.PERPETUAL,
        "underlying_id": binding.underlying_id,
        "sector": InstrumentSector(binding.sector_id),
        "risk_cluster_ids": (binding.risk_cluster_id,),
        "quote_asset": "USDT",
        "settlement_asset": binding.settlement_asset,
        "collateral_asset": "USDT",
        "contract_multiplier": binding.contract_multiplier,
        "tick_size": risk_request.market.tick_size,
        "lot_size": risk_request.requested.quantity_step,
        "minimum_quantity": risk_request.market.minimum_quantity,
        "minimum_notional": risk_request.market.minimum_notional,
        "discoverable": True,
        "classification_complete": True,
        "approval_scope": InstrumentApprovalScope.RESEARCH,
        "listing_status": InstrumentListingStatus.TRADING,
        "environment": CatalogEnvironment.SHADOW,
        "real_funds_eligible": False,
        "valid_from": now - timedelta(minutes=1),
        "valid_until": now + timedelta(days=1),
        "source_observed_at": now - timedelta(minutes=2),
        "evidence_refs": (
            "test-only:instrument-classification",
            "test-only:instrument-metadata",
        ),
        "source_ref": "test-only:instrument-catalog",
    }
    values.update(updates)
    draft = RegisterInstrumentCatalogRecordDraft.model_validate(values)
    return RegisterInstrumentCatalogRecordRequest.model_validate(
        {
            **draft.model_dump(mode="json"),
            "record_hash": instrument_catalog_record_hash(draft),
            "evidence_hash": instrument_catalog_evidence_hash(draft),
        }
    )


def instrument_catalog_envelope(
    request: RegisterInstrumentCatalogRecordRequest,
    *,
    now: datetime,
    idempotency_key: str | None = None,
    service_principal: str = INSTRUMENT_CATALOG_SERVICE_PRINCIPAL,
) -> CommandEnvelope:
    return CommandEnvelope(
        idempotency_key=idempotency_key or f"instrument-catalog-{uuid4()}",
        command_type=InstrumentCatalogService.command_type,
        object_type="InstrumentCatalogRecord",
        object_id=str(request.catalog_record_id),
        expected_version=1,
        service_principal=service_principal,
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": request.organization_id},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref="test-only:instrument-catalog-service",
        payload_schema_version=1,
        reason="register SHADOW-only instrument classification",
        payload=request.model_dump(mode="json"),
    )


def register_instrument_catalog(
    database: Database,
    request: RegisterInstrumentCatalogRecordRequest,
    *,
    now: datetime,
    envelope: CommandEnvelope | None = None,
):
    return IdempotentCommandExecutor(database.session_factory).execute(
        envelope or instrument_catalog_envelope(request, now=now),
        InstrumentCatalogService(clock=lambda: now).register,
    )
