from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
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
from trading_control_plane.instrument_catalog_models import InstrumentCatalogRecord
from trading_control_plane.metrics import (
    INSTRUMENT_CATALOG_REGISTRATIONS,
    INSTRUMENT_CATALOG_VALIDATIONS,
)

INSTRUMENT_CATALOG_SERVICE_PRINCIPAL = "instrument-catalog-service"


class CatalogEnvironment(StrEnum):
    SHADOW = "SHADOW"


class InstrumentContractType(StrEnum):
    PERPETUAL = "PERPETUAL"


class InstrumentSector(StrEnum):
    CRYPTO = "CRYPTO"
    EQUITY_INDEX = "EQUITY_INDEX"
    PRECIOUS_METALS = "PRECIOUS_METALS"
    COMMODITY = "COMMODITY"
    UNCLASSIFIED = "UNCLASSIFIED"


class InstrumentApprovalScope(StrEnum):
    NONE = "NONE"
    OBSERVE = "OBSERVE"
    RESEARCH = "RESEARCH"


class InstrumentListingStatus(StrEnum):
    TRADING = "TRADING"
    REDUCE_ONLY = "REDUCE_ONLY"
    HALTED = "HALTED"
    DELISTING = "DELISTING"
    RETIRED = "RETIRED"


class RegisterInstrumentCatalogRecordDraft(BaseModel):
    """Hash-free authoring contract for an immutable classification fact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    catalog_record_id: UUID
    organization_id: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=80)
    execution_domain: str = Field(min_length=1, max_length=120)
    native_instrument_id: str = Field(min_length=1, max_length=255)
    canonical_instrument_id: str = Field(min_length=1, max_length=255)
    display_symbol: str = Field(min_length=1, max_length=120)
    catalog_version: str = Field(min_length=1, max_length=120)
    metadata_version: str = Field(min_length=1, max_length=120)
    classification_version: str = Field(min_length=1, max_length=120)
    contract_type: InstrumentContractType
    underlying_id: str = Field(min_length=1, max_length=160)
    sector: InstrumentSector
    risk_cluster_ids: tuple[str, ...] = Field(min_length=1)
    quote_asset: str = Field(min_length=1, max_length=80)
    settlement_asset: str = Field(min_length=1, max_length=80)
    collateral_asset: str = Field(min_length=1, max_length=80)
    contract_multiplier: Decimal = Field(gt=0)
    tick_size: Decimal = Field(gt=0)
    lot_size: Decimal = Field(gt=0)
    minimum_quantity: Decimal = Field(gt=0)
    minimum_notional: Decimal = Field(ge=0)
    discoverable: bool
    classification_complete: bool
    approval_scope: InstrumentApprovalScope
    listing_status: InstrumentListingStatus
    environment: CatalogEnvironment
    real_funds_eligible: bool
    valid_from: datetime
    valid_until: datetime
    source_observed_at: datetime
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    source_ref: str = Field(min_length=1, max_length=255)


class RegisterInstrumentCatalogRecordRequest(RegisterInstrumentCatalogRecordDraft):
    record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def record_is_canonical_and_self_consistent(self) -> Self:
        if self.environment is not CatalogEnvironment.SHADOW or self.real_funds_eligible:
            raise ValueError("instrument catalog record must remain shadow-only")
        timestamps = (self.valid_from, self.valid_until, self.source_observed_at)
        if any(value.tzinfo is None or value.utcoffset() is None for value in timestamps):
            raise ValueError("instrument catalog timestamps must be timezone-aware")
        if self.valid_until <= self.valid_from:
            raise ValueError("instrument catalog validity window is empty")
        if self.source_observed_at > self.valid_from:
            raise ValueError("source observation must precede catalog validity")
        if self.risk_cluster_ids != tuple(sorted(set(self.risk_cluster_ids))) or any(
            not cluster_id for cluster_id in self.risk_cluster_ids
        ):
            raise ValueError("risk cluster identifiers must be sorted, unique, and non-empty")
        if self.evidence_refs != tuple(sorted(set(self.evidence_refs))) or any(
            not reference for reference in self.evidence_refs
        ):
            raise ValueError("catalog evidence references must be sorted, unique, and non-empty")
        if self.classification_complete and self.sector is InstrumentSector.UNCLASSIFIED:
            raise ValueError("complete catalog classification cannot be UNCLASSIFIED")
        if self.record_hash != instrument_catalog_record_hash(self):
            raise ValueError("instrument catalog record hash mismatch")
        if self.evidence_hash != instrument_catalog_evidence_hash(self):
            raise ValueError("instrument catalog evidence hash mismatch")
        return self


class InstrumentClassificationValidationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    organization_id: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=80)
    execution_domain: str = Field(min_length=1, max_length=120)
    canonical_instrument_id: str = Field(min_length=1, max_length=255)
    catalog_version: str = Field(min_length=1, max_length=120)
    classification_version: str = Field(min_length=1, max_length=120)
    expected_underlying_id: str = Field(min_length=1, max_length=160)
    expected_sector: str = Field(min_length=1, max_length=80)
    expected_risk_cluster_id: str = Field(min_length=1, max_length=160)
    expected_settlement_asset: str = Field(min_length=1, max_length=80)
    expected_contract_multiplier: Decimal = Field(gt=0)
    validation_time: datetime

    @model_validator(mode="after")
    def timestamp_is_aware(self) -> Self:
        if self.validation_time.tzinfo is None or self.validation_time.utcoffset() is None:
            raise ValueError("catalog validation time must be timezone-aware")
        return self


class InstrumentClassificationValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    valid: bool
    reason_codes: tuple[str, ...]
    catalog_record_id: UUID | None
    record_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evidence_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    valid_until: datetime
    validation_snapshot: dict[str, JsonValue]


def _decimal_contract(value: Decimal) -> str:
    return format(value.normalize(), "f")


def instrument_catalog_record_hash(
    request: RegisterInstrumentCatalogRecordDraft | RegisterInstrumentCatalogRecordRequest,
) -> str:
    return hash_json(
        {
            "catalog_record_id": str(request.catalog_record_id),
            "organization_id": request.organization_id,
            "venue": request.venue,
            "execution_domain": request.execution_domain,
            "native_instrument_id": request.native_instrument_id,
            "canonical_instrument_id": request.canonical_instrument_id,
            "display_symbol": request.display_symbol,
            "catalog_version": request.catalog_version,
            "metadata_version": request.metadata_version,
            "classification_version": request.classification_version,
            "contract_type": request.contract_type.value,
            "underlying_id": request.underlying_id,
            "sector": request.sector.value,
            "risk_cluster_ids": list(request.risk_cluster_ids),
            "quote_asset": request.quote_asset,
            "settlement_asset": request.settlement_asset,
            "collateral_asset": request.collateral_asset,
            "contract_multiplier": _decimal_contract(request.contract_multiplier),
            "tick_size": _decimal_contract(request.tick_size),
            "lot_size": _decimal_contract(request.lot_size),
            "minimum_quantity": _decimal_contract(request.minimum_quantity),
            "minimum_notional": _decimal_contract(request.minimum_notional),
            "discoverable": request.discoverable,
            "classification_complete": request.classification_complete,
            "approval_scope": request.approval_scope.value,
            "listing_status": request.listing_status.value,
            "environment": request.environment.value,
            "real_funds_eligible": request.real_funds_eligible,
            "valid_from": request.valid_from.astimezone(UTC).isoformat(),
            "valid_until": request.valid_until.astimezone(UTC).isoformat(),
            "source_observed_at": request.source_observed_at.astimezone(UTC).isoformat(),
        }
    )


def instrument_catalog_evidence_hash(
    request: RegisterInstrumentCatalogRecordDraft | RegisterInstrumentCatalogRecordRequest,
) -> str:
    return hash_json(
        {
            "evidence_refs": list(request.evidence_refs),
            "source_ref": request.source_ref,
        }
    )


class InstrumentCatalogService:
    command_type = "instrument_catalog.record.register.v1"
    payload_schema_version = 1

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def register(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        if envelope.command_type != self.command_type:
            raise CommandRejected("COMMAND_TYPE_MISMATCH", "unexpected command type")
        if envelope.payload_schema_version != self.payload_schema_version:
            raise CommandRejected(
                "PAYLOAD_SCHEMA_VERSION_MISMATCH",
                "instrument catalog payload schema version is unsupported",
            )
        if (
            envelope.channel is not CommandChannel.INTERNAL
            or envelope.service_principal != INSTRUMENT_CATALOG_SERVICE_PRINCIPAL
        ):
            raise CommandRejected(
                "INSTRUMENT_CATALOG_SERVICE_REQUIRED",
                "only the exact internal instrument catalog service may register records",
            )
        if envelope.object_type != "InstrumentCatalogRecord" or envelope.object_id is None:
            raise CommandRejected(
                "OBJECT_BINDING_MISMATCH", "InstrumentCatalogRecord binding is required"
            )
        try:
            request = RegisterInstrumentCatalogRecordRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected(
                "INSTRUMENT_CATALOG_RECORD_INVALID",
                "instrument catalog record is invalid",
            ) from exc
        if envelope.object_id != str(request.catalog_record_id):
            raise CommandRejected("OBJECT_BINDING_MISMATCH", "catalog record identity changed")
        if envelope.expected_version != 1:
            raise CommandRejected("VERSION_CONFLICT", "catalog record version changed")
        if envelope.scope.get("organization_id") != request.organization_id:
            raise CommandRejected("SCOPE_MISMATCH", "organization scope changed")

        lock_key = ":".join(
            (
                "instrument-catalog",
                request.organization_id,
                request.venue,
                request.execution_domain,
                request.canonical_instrument_id,
                request.catalog_version,
                request.classification_version,
            )
        )
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )
        existing = session.execute(
            select(InstrumentCatalogRecord).where(
                InstrumentCatalogRecord.organization_id == request.organization_id,
                InstrumentCatalogRecord.venue == request.venue,
                InstrumentCatalogRecord.execution_domain == request.execution_domain,
                InstrumentCatalogRecord.canonical_instrument_id == request.canonical_instrument_id,
                InstrumentCatalogRecord.catalog_version == request.catalog_version,
                InstrumentCatalogRecord.classification_version == request.classification_version,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise CommandRejected(
                "INSTRUMENT_CATALOG_VERSION_EXISTS",
                "exact instrument catalog version already exists",
            )
        if session.get(InstrumentCatalogRecord, request.catalog_record_id) is not None:
            raise CommandRejected(
                "INSTRUMENT_CATALOG_ID_EXISTS", "instrument catalog identity already exists"
            )

        created_at = self._clock()
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise CommandRejected(
                "INSTRUMENT_CATALOG_CLOCK_INVALID",
                "instrument catalog clock must be timezone-aware",
            )
        session.add(
            InstrumentCatalogRecord(
                catalog_record_id=request.catalog_record_id,
                organization_id=request.organization_id,
                venue=request.venue,
                execution_domain=request.execution_domain,
                native_instrument_id=request.native_instrument_id,
                canonical_instrument_id=request.canonical_instrument_id,
                display_symbol=request.display_symbol,
                catalog_version=request.catalog_version,
                metadata_version=request.metadata_version,
                classification_version=request.classification_version,
                contract_type=request.contract_type.value,
                underlying_id=request.underlying_id,
                sector=request.sector.value,
                risk_cluster_ids=list(request.risk_cluster_ids),
                quote_asset=request.quote_asset,
                settlement_asset=request.settlement_asset,
                collateral_asset=request.collateral_asset,
                contract_multiplier=request.contract_multiplier,
                tick_size=request.tick_size,
                lot_size=request.lot_size,
                minimum_quantity=request.minimum_quantity,
                minimum_notional=request.minimum_notional,
                discoverable=request.discoverable,
                classification_complete=request.classification_complete,
                approval_scope=request.approval_scope.value,
                listing_status=request.listing_status.value,
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
        INSTRUMENT_CATALOG_REGISTRATIONS.labels("REGISTERED").inc()
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="InstrumentCatalogRecord",
            object_id=str(request.catalog_record_id),
            object_version=1,
            data={
                "catalog_record_id": str(request.catalog_record_id),
                "catalog_version": request.catalog_version,
                "classification_version": request.classification_version,
                "record_hash": request.record_hash,
                "environment": request.environment.value,
                "real_funds_eligible": request.real_funds_eligible,
            },
            events=(
                DomainEvent(
                    event_type="InstrumentCatalogRecordRegistered",
                    aggregate_type="InstrumentCatalogRecord",
                    aggregate_id=str(request.catalog_record_id),
                    payload={
                        "organization_id": request.organization_id,
                        "venue": request.venue,
                        "execution_domain": request.execution_domain,
                        "canonical_instrument_id": request.canonical_instrument_id,
                        "catalog_version": request.catalog_version,
                        "classification_version": request.classification_version,
                        "record_hash": request.record_hash,
                        "environment": request.environment.value,
                        "real_funds_eligible": request.real_funds_eligible,
                    },
                ),
            ),
        )


class InstrumentCatalogValidator:
    """Resolves exact durable instrument classification; never trusts caller booleans."""

    def validate(
        self,
        session: Session,
        request: InstrumentClassificationValidationRequest,
        *,
        lock: bool = False,
    ) -> InstrumentClassificationValidationResult:
        query = select(InstrumentCatalogRecord).where(
            InstrumentCatalogRecord.organization_id == request.organization_id,
            InstrumentCatalogRecord.venue == request.venue,
            InstrumentCatalogRecord.execution_domain == request.execution_domain,
            InstrumentCatalogRecord.canonical_instrument_id == request.canonical_instrument_id,
            InstrumentCatalogRecord.catalog_version == request.catalog_version,
            InstrumentCatalogRecord.classification_version == request.classification_version,
        )
        record = session.execute(query.with_for_update() if lock else query).scalar_one_or_none()
        reasons: list[str] = []
        validated: RegisterInstrumentCatalogRecordRequest | None = None
        if record is None:
            reasons.append("INSTRUMENT_CATALOG_RECORD_NOT_FOUND")
        else:
            try:
                validated = RegisterInstrumentCatalogRecordRequest.model_validate(
                    {
                        "catalog_record_id": record.catalog_record_id,
                        "organization_id": record.organization_id,
                        "venue": record.venue,
                        "execution_domain": record.execution_domain,
                        "native_instrument_id": record.native_instrument_id,
                        "canonical_instrument_id": record.canonical_instrument_id,
                        "display_symbol": record.display_symbol,
                        "catalog_version": record.catalog_version,
                        "metadata_version": record.metadata_version,
                        "classification_version": record.classification_version,
                        "contract_type": record.contract_type,
                        "underlying_id": record.underlying_id,
                        "sector": record.sector,
                        "risk_cluster_ids": record.risk_cluster_ids,
                        "quote_asset": record.quote_asset,
                        "settlement_asset": record.settlement_asset,
                        "collateral_asset": record.collateral_asset,
                        "contract_multiplier": record.contract_multiplier,
                        "tick_size": record.tick_size,
                        "lot_size": record.lot_size,
                        "minimum_quantity": record.minimum_quantity,
                        "minimum_notional": record.minimum_notional,
                        "discoverable": record.discoverable,
                        "classification_complete": record.classification_complete,
                        "approval_scope": record.approval_scope,
                        "listing_status": record.listing_status,
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
                )
            except ValidationError:
                reasons.append("INSTRUMENT_CATALOG_INTEGRITY_FAILED")
            if validated is not None:
                self._validate_classification(request, validated, reasons)

        valid = not reasons
        valid_until = (
            validated.valid_until if validated is not None and valid else request.validation_time
        )
        snapshot: dict[str, JsonValue] = {
            "catalog_record_id": str(record.catalog_record_id) if record is not None else None,
            "catalog_version": request.catalog_version,
            "classification_version": request.classification_version,
            "record_hash": record.record_hash if record is not None else None,
            "evidence_hash": record.evidence_hash if record is not None else None,
            "environment": record.environment if record is not None else None,
            "real_funds_eligible": record.real_funds_eligible if record is not None else None,
            "risk_cluster_ids": (
                list[JsonValue](record.risk_cluster_ids) if record is not None else None
            ),
            "validated_at": request.validation_time.isoformat(),
            "valid_until": valid_until.isoformat(),
            "valid": valid,
            "reason_codes": list[JsonValue](reasons),
        }
        result = InstrumentClassificationValidationResult(
            valid=valid,
            reason_codes=tuple(reasons),
            catalog_record_id=record.catalog_record_id if record is not None else None,
            record_hash=record.record_hash if record is not None else None,
            evidence_hash=record.evidence_hash if record is not None else None,
            valid_until=valid_until,
            validation_snapshot=snapshot,
        )
        INSTRUMENT_CATALOG_VALIDATIONS.labels(
            "VALID" if valid else "INVALID",
            reasons[0] if reasons else "INSTRUMENT_CLASSIFICATION_VALID",
        ).inc()
        return result

    @staticmethod
    def _validate_classification(
        request: InstrumentClassificationValidationRequest,
        record: RegisterInstrumentCatalogRecordRequest,
        reasons: list[str],
    ) -> None:
        now = request.validation_time
        if not (record.valid_from <= now < record.valid_until):
            reasons.append("INSTRUMENT_CATALOG_OUTSIDE_VALID_WINDOW")
        if not record.discoverable:
            reasons.append("INSTRUMENT_NOT_DISCOVERABLE")
        if not record.classification_complete:
            reasons.append("INSTRUMENT_CLASSIFICATION_INCOMPLETE")
        if record.sector is InstrumentSector.UNCLASSIFIED:
            reasons.append("INSTRUMENT_SECTOR_UNCLASSIFIED")
        if len(record.risk_cluster_ids) != 1:
            reasons.append("MULTI_CLUSTER_RISK_SCOPE_UNSUPPORTED")
        expected_binding = (
            request.expected_underlying_id,
            request.expected_sector,
            (request.expected_risk_cluster_id,),
            request.expected_settlement_asset,
            request.expected_contract_multiplier,
        )
        actual_binding = (
            record.underlying_id,
            record.sector.value,
            record.risk_cluster_ids,
            record.settlement_asset,
            record.contract_multiplier,
        )
        if actual_binding != expected_binding:
            reasons.append("INSTRUMENT_CLASSIFICATION_SCOPE_MISMATCH")
