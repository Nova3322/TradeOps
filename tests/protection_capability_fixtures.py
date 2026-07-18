from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import CommandChannel, CommandEnvelope
from trading_control_plane.database import Database
from trading_control_plane.instrument_catalog import RegisterInstrumentCatalogRecordRequest
from trading_control_plane.protection_capability import (
    VENUE_CAPABILITY_CATALOG_SERVICE_PRINCIPAL,
    ProtectionCapabilityEnvironment,
    ProtectionCapabilityService,
    ProtectionCapabilityStatus,
    RegisterProtectionCapabilityDraft,
    RegisterProtectionCapabilityRequest,
    protection_capability_evidence_hash,
    protection_capability_record_hash,
)
from trading_control_plane.risk import RiskPrecheckRequest


def protection_capability_request_for_risk(
    risk_request: RiskPrecheckRequest,
    catalog_request: RegisterInstrumentCatalogRecordRequest,
    *,
    now: datetime,
    **updates: Any,
) -> RegisterProtectionCapabilityRequest:
    binding = risk_request.binding
    identity = ":".join(
        (
            risk_request.organization_id,
            binding.venue,
            binding.execution_domain,
            binding.instrument_identity,
            binding.account_id,
            binding.position_mode,
            binding.margin_mode,
            binding.collateral_pool_id,
            binding.catalog_version,
            binding.instrument_scope_version,
            binding.position_management_template_version,
        )
    )
    values: dict[str, Any] = {
        "protection_capability_record_id": uuid5(NAMESPACE_URL, identity),
        "organization_id": risk_request.organization_id,
        "catalog_record_id": catalog_request.catalog_record_id,
        "catalog_version": binding.catalog_version,
        "classification_version": binding.instrument_scope_version,
        "catalog_record_hash": catalog_request.record_hash,
        "venue": binding.venue,
        "execution_domain": binding.execution_domain,
        "canonical_instrument_id": binding.instrument_identity,
        "account_id": binding.account_id,
        "account_abstraction": binding.account_abstraction,
        "position_mode": binding.position_mode,
        "margin_mode": binding.margin_mode,
        "collateral_scope": binding.collateral_scope,
        "collateral_pool_id": binding.collateral_pool_id,
        "position_management_template_version": (binding.position_management_template_version),
        "execution_capability_version": binding.execution_capability_version,
        "adapter_version": binding.adapter_version,
        "worker_id": binding.worker_id,
        "worker_config_hash": binding.worker_config_hash,
        "credential_fingerprint": binding.credential_fingerprint,
        "freqtrade_worker_version": binding.freqtrade_worker_version,
        "account_capability_version": binding.account_capability_version,
        "credential_permission_profile_version": (binding.credential_permission_profile_version),
        "venue_client_version": binding.venue_client_version,
        "capability_status": ProtectionCapabilityStatus.CERTIFIED,
        "native_protection_supported": True,
        "conditional_orders_supported": True,
        "reduce_only_supported": True,
        "partial_fill_protection_supported": True,
        "protection_replacement_supported": True,
        "protection_confirmation_window_ms": 5_000,
        "supported_trigger_price_types": ("MARK_PRICE",),
        "supported_protection_order_types": ("STOP_MARKET",),
        "environment": ProtectionCapabilityEnvironment.SHADOW,
        "real_funds_eligible": False,
        "valid_from": now - timedelta(minutes=1),
        "valid_until": now + timedelta(days=1),
        "source_observed_at": now - timedelta(minutes=2),
        "evidence_refs": (
            "test-only:native-protection-contract",
            "test-only:partial-fill-protection",
        ),
        "source_ref": "test-only:shadow-protection-capability",
    }
    values.update(updates)
    draft = RegisterProtectionCapabilityDraft.model_validate(values)
    return RegisterProtectionCapabilityRequest.model_validate(
        {
            **draft.model_dump(mode="json"),
            "record_hash": protection_capability_record_hash(draft),
            "evidence_hash": protection_capability_evidence_hash(draft),
        }
    )


def protection_capability_envelope(
    request: RegisterProtectionCapabilityRequest,
    *,
    now: datetime,
    idempotency_key: str | None = None,
    service_principal: str = VENUE_CAPABILITY_CATALOG_SERVICE_PRINCIPAL,
) -> CommandEnvelope:
    return CommandEnvelope(
        idempotency_key=idempotency_key or f"protection-capability-{uuid4()}",
        command_type=ProtectionCapabilityService.command_type,
        object_type="InstrumentProtectionCapabilityRecord",
        object_id=str(request.protection_capability_record_id),
        expected_version=1,
        service_principal=service_principal,
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": request.organization_id},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref="test-only:venue-capability-catalog-service",
        payload_schema_version=1,
        reason="register SHADOW-only native protection capability",
        payload=request.model_dump(mode="json"),
    )


def register_protection_capability(
    database: Database,
    request: RegisterProtectionCapabilityRequest,
    *,
    now: datetime,
    envelope: CommandEnvelope | None = None,
):
    return IdempotentCommandExecutor(database.session_factory).execute(
        envelope or protection_capability_envelope(request, now=now),
        ProtectionCapabilityService(clock=lambda: now).register,
    )
