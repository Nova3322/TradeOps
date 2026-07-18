from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from trading_control_plane.commands import (
    CommandChannel,
    CommandEnvelope,
    CommandOutcome,
    CommandRejected,
    CommandStatus,
    DomainEvent,
    hash_json,
)
from trading_control_plane.metrics import (
    PROTECTION_CAPABILITY_REGISTRATIONS,
    PROTECTION_CAPABILITY_VALIDATIONS,
)
from trading_control_plane.protection_capability_models import (
    InstrumentProtectionCapabilityRecord,
)

VENUE_CAPABILITY_CATALOG_SERVICE_PRINCIPAL = "venue-capability-catalog-service"


class ProtectionCapabilityEnvironment(StrEnum):
    SHADOW = "SHADOW"


class ProtectionCapabilityStatus(StrEnum):
    CERTIFIED = "CERTIFIED"
    VALIDATING = "VALIDATING"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    UNKNOWN = "UNKNOWN"
    EXPIRED = "EXPIRED"


class RegisterProtectionCapabilityDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    protection_capability_record_id: UUID
    organization_id: str = Field(min_length=1, max_length=120)
    catalog_record_id: UUID
    catalog_version: str = Field(min_length=1, max_length=120)
    classification_version: str = Field(min_length=1, max_length=120)
    catalog_record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    venue: str = Field(min_length=1, max_length=80)
    execution_domain: str = Field(min_length=1, max_length=120)
    canonical_instrument_id: str = Field(min_length=1, max_length=255)
    account_id: str = Field(min_length=1, max_length=160)
    account_abstraction: str = Field(min_length=1, max_length=80)
    position_mode: str = Field(min_length=1, max_length=80)
    margin_mode: str = Field(min_length=1, max_length=80)
    collateral_scope: str = Field(min_length=1, max_length=120)
    collateral_pool_id: str = Field(min_length=1, max_length=160)
    position_management_template_version: str = Field(min_length=1, max_length=120)
    execution_capability_version: str = Field(min_length=1, max_length=120)
    adapter_version: str = Field(min_length=1, max_length=120)
    worker_id: str = Field(min_length=1, max_length=160)
    worker_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    credential_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    freqtrade_worker_version: str = Field(min_length=1, max_length=120)
    account_capability_version: str = Field(min_length=1, max_length=120)
    credential_permission_profile_version: str = Field(min_length=1, max_length=120)
    venue_client_version: str = Field(min_length=1, max_length=120)
    capability_status: ProtectionCapabilityStatus
    native_protection_supported: bool
    conditional_orders_supported: bool
    reduce_only_supported: bool
    partial_fill_protection_supported: bool
    protection_replacement_supported: bool
    protection_confirmation_window_ms: int = Field(gt=0)
    supported_trigger_price_types: tuple[str, ...] = Field(min_length=1)
    supported_protection_order_types: tuple[str, ...] = Field(min_length=1)
    environment: ProtectionCapabilityEnvironment
    real_funds_eligible: bool
    valid_from: datetime
    valid_until: datetime
    source_observed_at: datetime
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    source_ref: str = Field(min_length=1, max_length=255)


class RegisterProtectionCapabilityRequest(RegisterProtectionCapabilityDraft):
    record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def record_is_canonical_and_self_consistent(self) -> Self:
        if (
            self.environment is not ProtectionCapabilityEnvironment.SHADOW
            or self.real_funds_eligible
        ):
            raise ValueError("protection capability record must remain shadow-only")
        timestamps = (self.valid_from, self.valid_until, self.source_observed_at)
        if any(value.tzinfo is None or value.utcoffset() is None for value in timestamps):
            raise ValueError("protection capability timestamps must be timezone-aware")
        if self.valid_until <= self.valid_from:
            raise ValueError("protection capability validity window is empty")
        if self.source_observed_at > self.valid_from:
            raise ValueError("source observation must precede capability validity")
        for values, label in (
            (self.supported_trigger_price_types, "trigger price types"),
            (self.supported_protection_order_types, "protection order types"),
            (self.evidence_refs, "evidence references"),
        ):
            if values != tuple(sorted(set(values))) or any(not value for value in values):
                raise ValueError(f"{label} must be sorted, unique, and non-empty")
        if self.record_hash != protection_capability_record_hash(self):
            raise ValueError("protection capability record hash mismatch")
        if self.evidence_hash != protection_capability_evidence_hash(self):
            raise ValueError("protection capability evidence hash mismatch")
        return self


class ProtectionCapabilityValidationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    organization_id: str = Field(min_length=1, max_length=120)
    expected_catalog_record_id: UUID | None
    expected_catalog_record_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    catalog_version: str = Field(min_length=1, max_length=120)
    classification_version: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=80)
    execution_domain: str = Field(min_length=1, max_length=120)
    canonical_instrument_id: str = Field(min_length=1, max_length=255)
    account_id: str = Field(min_length=1, max_length=160)
    expected_account_abstraction: str = Field(min_length=1, max_length=80)
    position_mode: str = Field(min_length=1, max_length=80)
    margin_mode: str = Field(min_length=1, max_length=80)
    expected_collateral_scope: str = Field(min_length=1, max_length=120)
    collateral_pool_id: str = Field(min_length=1, max_length=160)
    position_management_template_version: str = Field(min_length=1, max_length=120)
    expected_execution_capability_version: str = Field(min_length=1, max_length=120)
    expected_adapter_version: str = Field(min_length=1, max_length=120)
    expected_worker_id: str = Field(min_length=1, max_length=160)
    expected_worker_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_credential_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_freqtrade_worker_version: str = Field(min_length=1, max_length=120)
    expected_account_capability_version: str = Field(min_length=1, max_length=120)
    expected_credential_permission_profile_version: str = Field(
        min_length=1,
        max_length=120,
    )
    expected_venue_client_version: str = Field(min_length=1, max_length=120)
    validation_time: datetime

    @model_validator(mode="after")
    def validation_contract_is_consistent(self) -> Self:
        if self.validation_time.tzinfo is None or self.validation_time.utcoffset() is None:
            raise ValueError("protection capability validation time must be timezone-aware")
        if (self.expected_catalog_record_id is None) != (self.expected_catalog_record_hash is None):
            raise ValueError("catalog record identity and hash must be present together")
        return self


class ProtectionCapabilityValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    valid: bool
    reason_codes: tuple[str, ...]
    protection_capability_record_id: UUID | None
    position_management_template_version: str | None
    record_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evidence_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    valid_until: datetime
    validation_snapshot: dict[str, JsonValue]


def protection_capability_record_hash(
    request: RegisterProtectionCapabilityDraft | RegisterProtectionCapabilityRequest,
) -> str:
    return hash_json(
        {
            "protection_capability_record_id": str(request.protection_capability_record_id),
            "organization_id": request.organization_id,
            "catalog_record_id": str(request.catalog_record_id),
            "catalog_version": request.catalog_version,
            "classification_version": request.classification_version,
            "catalog_record_hash": request.catalog_record_hash,
            "venue": request.venue,
            "execution_domain": request.execution_domain,
            "canonical_instrument_id": request.canonical_instrument_id,
            "account_id": request.account_id,
            "account_abstraction": request.account_abstraction,
            "position_mode": request.position_mode,
            "margin_mode": request.margin_mode,
            "collateral_scope": request.collateral_scope,
            "collateral_pool_id": request.collateral_pool_id,
            "position_management_template_version": (request.position_management_template_version),
            "execution_capability_version": request.execution_capability_version,
            "adapter_version": request.adapter_version,
            "worker_id": request.worker_id,
            "worker_config_hash": request.worker_config_hash,
            "credential_fingerprint": request.credential_fingerprint,
            "freqtrade_worker_version": request.freqtrade_worker_version,
            "account_capability_version": request.account_capability_version,
            "credential_permission_profile_version": (
                request.credential_permission_profile_version
            ),
            "venue_client_version": request.venue_client_version,
            "capability_status": request.capability_status.value,
            "native_protection_supported": request.native_protection_supported,
            "conditional_orders_supported": request.conditional_orders_supported,
            "reduce_only_supported": request.reduce_only_supported,
            "partial_fill_protection_supported": (request.partial_fill_protection_supported),
            "protection_replacement_supported": request.protection_replacement_supported,
            "protection_confirmation_window_ms": (request.protection_confirmation_window_ms),
            "supported_trigger_price_types": list(request.supported_trigger_price_types),
            "supported_protection_order_types": list(request.supported_protection_order_types),
            "environment": request.environment.value,
            "real_funds_eligible": request.real_funds_eligible,
            "valid_from": request.valid_from.astimezone(UTC).isoformat(),
            "valid_until": request.valid_until.astimezone(UTC).isoformat(),
            "source_observed_at": request.source_observed_at.astimezone(UTC).isoformat(),
        }
    )


def protection_capability_evidence_hash(
    request: RegisterProtectionCapabilityDraft | RegisterProtectionCapabilityRequest,
) -> str:
    return hash_json(
        {
            "evidence_refs": list(request.evidence_refs),
            "source_ref": request.source_ref,
        }
    )


class ProtectionCapabilityService:
    command_type = "instrument_catalog.protection-capability.register.v1"
    payload_schema_version = 1

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def register(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        if envelope.command_type != self.command_type:
            raise CommandRejected("COMMAND_TYPE_MISMATCH", "unexpected command type")
        if envelope.payload_schema_version != self.payload_schema_version:
            raise CommandRejected(
                "PAYLOAD_SCHEMA_VERSION_MISMATCH",
                "protection capability payload schema version is unsupported",
            )
        if (
            envelope.channel is not CommandChannel.INTERNAL
            or envelope.service_principal != VENUE_CAPABILITY_CATALOG_SERVICE_PRINCIPAL
        ):
            raise CommandRejected(
                "VENUE_CAPABILITY_CATALOG_SERVICE_REQUIRED",
                "only the exact venue capability catalog service may register records",
            )
        if (
            envelope.object_type != "InstrumentProtectionCapabilityRecord"
            or envelope.object_id is None
        ):
            raise CommandRejected(
                "OBJECT_BINDING_MISMATCH",
                "InstrumentProtectionCapabilityRecord binding is required",
            )
        try:
            request = RegisterProtectionCapabilityRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected(
                "PROTECTION_CAPABILITY_RECORD_INVALID",
                "protection capability record is invalid",
            ) from exc
        if envelope.object_id != str(request.protection_capability_record_id):
            raise CommandRejected("OBJECT_BINDING_MISMATCH", "capability identity changed")
        if envelope.expected_version != 1:
            raise CommandRejected("VERSION_CONFLICT", "capability record version changed")
        if envelope.scope.get("organization_id") != request.organization_id:
            raise CommandRejected("SCOPE_MISMATCH", "organization scope changed")

        lock_key = ":".join(
            (
                "instrument-protection-capability",
                request.organization_id,
                request.venue,
                request.execution_domain,
                request.canonical_instrument_id,
                request.account_id,
                request.position_mode,
                request.margin_mode,
                request.collateral_pool_id,
                request.catalog_version,
                request.classification_version,
                request.position_management_template_version,
            )
        )
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )
        existing = session.execute(
            select(InstrumentProtectionCapabilityRecord).where(
                InstrumentProtectionCapabilityRecord.organization_id == request.organization_id,
                InstrumentProtectionCapabilityRecord.venue == request.venue,
                InstrumentProtectionCapabilityRecord.execution_domain == request.execution_domain,
                InstrumentProtectionCapabilityRecord.canonical_instrument_id
                == request.canonical_instrument_id,
                InstrumentProtectionCapabilityRecord.account_id == request.account_id,
                InstrumentProtectionCapabilityRecord.position_mode == request.position_mode,
                InstrumentProtectionCapabilityRecord.margin_mode == request.margin_mode,
                InstrumentProtectionCapabilityRecord.collateral_pool_id
                == request.collateral_pool_id,
                InstrumentProtectionCapabilityRecord.catalog_version == request.catalog_version,
                InstrumentProtectionCapabilityRecord.classification_version
                == request.classification_version,
                InstrumentProtectionCapabilityRecord.position_management_template_version
                == request.position_management_template_version,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise CommandRejected(
                "PROTECTION_CAPABILITY_VERSION_EXISTS",
                "exact protection capability version already exists",
            )
        if (
            session.get(
                InstrumentProtectionCapabilityRecord,
                request.protection_capability_record_id,
            )
            is not None
        ):
            raise CommandRejected(
                "PROTECTION_CAPABILITY_ID_EXISTS",
                "protection capability identity already exists",
            )

        created_at = self._clock()
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise CommandRejected(
                "PROTECTION_CAPABILITY_CLOCK_INVALID",
                "protection capability clock must be timezone-aware",
            )
        session.add(
            InstrumentProtectionCapabilityRecord(
                protection_capability_record_id=(request.protection_capability_record_id),
                organization_id=request.organization_id,
                catalog_record_id=request.catalog_record_id,
                catalog_version=request.catalog_version,
                classification_version=request.classification_version,
                catalog_record_hash=request.catalog_record_hash,
                venue=request.venue,
                execution_domain=request.execution_domain,
                canonical_instrument_id=request.canonical_instrument_id,
                account_id=request.account_id,
                account_abstraction=request.account_abstraction,
                position_mode=request.position_mode,
                margin_mode=request.margin_mode,
                collateral_scope=request.collateral_scope,
                collateral_pool_id=request.collateral_pool_id,
                position_management_template_version=(request.position_management_template_version),
                execution_capability_version=request.execution_capability_version,
                adapter_version=request.adapter_version,
                worker_id=request.worker_id,
                worker_config_hash=request.worker_config_hash,
                credential_fingerprint=request.credential_fingerprint,
                freqtrade_worker_version=request.freqtrade_worker_version,
                account_capability_version=request.account_capability_version,
                credential_permission_profile_version=(
                    request.credential_permission_profile_version
                ),
                venue_client_version=request.venue_client_version,
                capability_status=request.capability_status.value,
                native_protection_supported=request.native_protection_supported,
                conditional_orders_supported=request.conditional_orders_supported,
                reduce_only_supported=request.reduce_only_supported,
                partial_fill_protection_supported=(request.partial_fill_protection_supported),
                protection_replacement_supported=(request.protection_replacement_supported),
                protection_confirmation_window_ms=(request.protection_confirmation_window_ms),
                supported_trigger_price_types=list(request.supported_trigger_price_types),
                supported_protection_order_types=list(request.supported_protection_order_types),
                environment=request.environment.value,
                real_funds_eligible=request.real_funds_eligible,
                valid_from=request.valid_from,
                valid_until=request.valid_until,
                source_observed_at=request.source_observed_at,
                record_hash=request.record_hash,
                evidence_refs=list(request.evidence_refs),
                evidence_hash=request.evidence_hash,
                source_ref=request.source_ref,
                created_at=created_at,
            )
        )
        PROTECTION_CAPABILITY_REGISTRATIONS.labels("REGISTERED").inc()
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="InstrumentProtectionCapabilityRecord",
            object_id=str(request.protection_capability_record_id),
            object_version=1,
            data={
                "protection_capability_record_id": str(request.protection_capability_record_id),
                "position_management_template_version": (
                    request.position_management_template_version
                ),
                "capability_status": request.capability_status.value,
                "record_hash": request.record_hash,
                "environment": request.environment.value,
                "real_funds_eligible": request.real_funds_eligible,
            },
            events=(
                DomainEvent(
                    event_type="InstrumentProtectionCapabilityRegistered",
                    aggregate_type="InstrumentProtectionCapabilityRecord",
                    aggregate_id=str(request.protection_capability_record_id),
                    payload={
                        "organization_id": request.organization_id,
                        "catalog_record_id": str(request.catalog_record_id),
                        "venue": request.venue,
                        "execution_domain": request.execution_domain,
                        "canonical_instrument_id": request.canonical_instrument_id,
                        "account_id": request.account_id,
                        "position_management_template_version": (
                            request.position_management_template_version
                        ),
                        "capability_status": request.capability_status.value,
                        "record_hash": request.record_hash,
                        "environment": request.environment.value,
                        "real_funds_eligible": request.real_funds_eligible,
                    },
                ),
            ),
        )


class ProtectionCapabilityValidator:
    """Resolves exact durable protection capability instead of caller assertions."""

    def validate(
        self,
        session: Session,
        request: ProtectionCapabilityValidationRequest,
        *,
        lock: bool = False,
    ) -> ProtectionCapabilityValidationResult:
        query = select(InstrumentProtectionCapabilityRecord).where(
            InstrumentProtectionCapabilityRecord.organization_id == request.organization_id,
            InstrumentProtectionCapabilityRecord.venue == request.venue,
            InstrumentProtectionCapabilityRecord.execution_domain == request.execution_domain,
            InstrumentProtectionCapabilityRecord.canonical_instrument_id
            == request.canonical_instrument_id,
            InstrumentProtectionCapabilityRecord.account_id == request.account_id,
            InstrumentProtectionCapabilityRecord.position_mode == request.position_mode,
            InstrumentProtectionCapabilityRecord.margin_mode == request.margin_mode,
            InstrumentProtectionCapabilityRecord.collateral_pool_id == request.collateral_pool_id,
            InstrumentProtectionCapabilityRecord.catalog_version == request.catalog_version,
            InstrumentProtectionCapabilityRecord.classification_version
            == request.classification_version,
            InstrumentProtectionCapabilityRecord.position_management_template_version
            == request.position_management_template_version,
        )
        record = session.execute(query.with_for_update() if lock else query).scalar_one_or_none()
        reasons: list[str] = []
        validated: RegisterProtectionCapabilityRequest | None = None
        if record is None:
            reasons.append("PROTECTION_CAPABILITY_RECORD_NOT_FOUND")
        else:
            try:
                validated = RegisterProtectionCapabilityRequest.model_validate(
                    _record_contract(record)
                )
            except ValidationError:
                reasons.append("PROTECTION_CAPABILITY_INTEGRITY_FAILED")
            if validated is not None:
                self._validate_capability(request, validated, reasons)

        valid = not reasons
        valid_until = (
            validated.valid_until if validated is not None and valid else request.validation_time
        )
        snapshot: dict[str, JsonValue] = {
            "protection_capability_record_id": (
                str(record.protection_capability_record_id) if record is not None else None
            ),
            "position_management_template_version": (
                record.position_management_template_version
                if record is not None
                else request.position_management_template_version
            ),
            "capability_status": record.capability_status if record is not None else "UNKNOWN",
            "record_hash": record.record_hash if record is not None else None,
            "evidence_hash": record.evidence_hash if record is not None else None,
            "catalog_record_id": (str(record.catalog_record_id) if record is not None else None),
            "environment": record.environment if record is not None else None,
            "real_funds_eligible": (record.real_funds_eligible if record is not None else None),
            "validated_at": request.validation_time.isoformat(),
            "valid_until": valid_until.isoformat(),
            "valid": valid,
            "reason_codes": list[JsonValue](reasons),
        }
        result = ProtectionCapabilityValidationResult(
            valid=valid,
            reason_codes=tuple(reasons),
            protection_capability_record_id=(
                record.protection_capability_record_id if record is not None else None
            ),
            position_management_template_version=(
                record.position_management_template_version if record is not None else None
            ),
            record_hash=record.record_hash if record is not None else None,
            evidence_hash=record.evidence_hash if record is not None else None,
            valid_until=valid_until,
            validation_snapshot=snapshot,
        )
        PROTECTION_CAPABILITY_VALIDATIONS.labels(
            "VALID" if valid else "INVALID",
            reasons[0] if reasons else "PROTECTION_CAPABILITY_VALID",
        ).inc()
        return result

    @staticmethod
    def _validate_capability(
        request: ProtectionCapabilityValidationRequest,
        record: RegisterProtectionCapabilityRequest,
        reasons: list[str],
    ) -> None:
        if (
            request.expected_catalog_record_id is None
            or request.expected_catalog_record_hash is None
        ):
            reasons.append("PROTECTION_CATALOG_BINDING_UNAVAILABLE")
        elif (
            record.catalog_record_id != request.expected_catalog_record_id
            or record.catalog_record_hash != request.expected_catalog_record_hash
        ):
            reasons.append("PROTECTION_CATALOG_BINDING_MISMATCH")
        if not (record.valid_from <= request.validation_time < record.valid_until):
            reasons.append("PROTECTION_CAPABILITY_OUTSIDE_VALID_WINDOW")
        if record.capability_status is not ProtectionCapabilityStatus.CERTIFIED:
            reasons.append("PROTECTION_CAPABILITY_NOT_CERTIFIED")
        for supported, reason in (
            (record.native_protection_supported, "NATIVE_PROTECTION_UNSUPPORTED"),
            (record.conditional_orders_supported, "CONDITIONAL_ORDER_UNSUPPORTED"),
            (record.reduce_only_supported, "REDUCE_ONLY_PROTECTION_UNSUPPORTED"),
            (
                record.partial_fill_protection_supported,
                "PARTIAL_FILL_PROTECTION_UNSUPPORTED",
            ),
            (
                record.protection_replacement_supported,
                "PROTECTION_REPLACEMENT_UNSUPPORTED",
            ),
        ):
            if not supported:
                reasons.append(reason)
        expected_scope = (
            request.expected_account_abstraction,
            request.expected_collateral_scope,
            request.expected_execution_capability_version,
            request.expected_adapter_version,
            request.expected_worker_id,
            request.expected_worker_config_hash,
            request.expected_credential_fingerprint,
            request.expected_freqtrade_worker_version,
            request.expected_account_capability_version,
            request.expected_credential_permission_profile_version,
            request.expected_venue_client_version,
        )
        actual_scope = (
            record.account_abstraction,
            record.collateral_scope,
            record.execution_capability_version,
            record.adapter_version,
            record.worker_id,
            record.worker_config_hash,
            record.credential_fingerprint,
            record.freqtrade_worker_version,
            record.account_capability_version,
            record.credential_permission_profile_version,
            record.venue_client_version,
        )
        if actual_scope != expected_scope:
            reasons.append("PROTECTION_CAPABILITY_SCOPE_MISMATCH")


def _record_contract(record: InstrumentProtectionCapabilityRecord) -> dict[str, object]:
    return {
        "protection_capability_record_id": record.protection_capability_record_id,
        "organization_id": record.organization_id,
        "catalog_record_id": record.catalog_record_id,
        "catalog_version": record.catalog_version,
        "classification_version": record.classification_version,
        "catalog_record_hash": record.catalog_record_hash,
        "venue": record.venue,
        "execution_domain": record.execution_domain,
        "canonical_instrument_id": record.canonical_instrument_id,
        "account_id": record.account_id,
        "account_abstraction": record.account_abstraction,
        "position_mode": record.position_mode,
        "margin_mode": record.margin_mode,
        "collateral_scope": record.collateral_scope,
        "collateral_pool_id": record.collateral_pool_id,
        "position_management_template_version": (record.position_management_template_version),
        "execution_capability_version": record.execution_capability_version,
        "adapter_version": record.adapter_version,
        "worker_id": record.worker_id,
        "worker_config_hash": record.worker_config_hash,
        "credential_fingerprint": record.credential_fingerprint,
        "freqtrade_worker_version": record.freqtrade_worker_version,
        "account_capability_version": record.account_capability_version,
        "credential_permission_profile_version": (record.credential_permission_profile_version),
        "venue_client_version": record.venue_client_version,
        "capability_status": record.capability_status,
        "native_protection_supported": record.native_protection_supported,
        "conditional_orders_supported": record.conditional_orders_supported,
        "reduce_only_supported": record.reduce_only_supported,
        "partial_fill_protection_supported": (record.partial_fill_protection_supported),
        "protection_replacement_supported": record.protection_replacement_supported,
        "protection_confirmation_window_ms": (record.protection_confirmation_window_ms),
        "supported_trigger_price_types": record.supported_trigger_price_types,
        "supported_protection_order_types": record.supported_protection_order_types,
        "environment": record.environment,
        "real_funds_eligible": record.real_funds_eligible,
        "valid_from": record.valid_from,
        "valid_until": record.valid_until,
        "source_observed_at": record.source_observed_at,
        "record_hash": record.record_hash,
        "evidence_refs": record.evidence_refs,
        "evidence_hash": record.evidence_hash,
        "source_ref": record.source_ref,
    }
