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
from trading_control_plane.metrics import RISK_FACT_SET_REGISTRATIONS, RISK_FACT_SET_VALIDATIONS
from trading_control_plane.risk_fact_set_models import RiskFactSetRecord
from trading_control_plane.risk_facts import REQUIRED_FACT_TYPES, FactObservation

RISK_FACT_AGGREGATOR_SERVICE_PRINCIPAL = "risk-fact-aggregator-service"


def _scope_lock_key(
    *,
    organization_id: str,
    venue: str,
    execution_domain: str,
    account_id: str,
    canonical_instrument_id: str,
    position_mode: str,
    margin_mode: str,
    collateral_pool_id: str,
) -> str:
    return ":".join(
        (
            "risk-fact-set",
            organization_id,
            venue,
            execution_domain,
            account_id,
            canonical_instrument_id,
            position_mode,
            margin_mode,
            collateral_pool_id,
        )
    )


class RiskFactSetEnvironment(StrEnum):
    SHADOW = "SHADOW"


class RegisterRiskFactSetDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    risk_fact_set_id: UUID
    organization_id: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=80)
    execution_domain: str = Field(min_length=1, max_length=120)
    account_id: str = Field(min_length=1, max_length=160)
    canonical_instrument_id: str = Field(min_length=1, max_length=255)
    position_mode: str = Field(min_length=1, max_length=80)
    margin_mode: str = Field(min_length=1, max_length=80)
    collateral_pool_id: str = Field(min_length=1, max_length=160)
    fact_set_version: str = Field(min_length=1, max_length=120)
    observations: tuple[FactObservation, ...] = Field(min_length=1)
    environment: RiskFactSetEnvironment
    real_funds_eligible: bool
    assembled_at: datetime
    valid_from: datetime
    valid_until: datetime
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    source_ref: str = Field(min_length=1, max_length=255)


class RegisterRiskFactSetRequest(RegisterRiskFactSetDraft):
    record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def record_is_canonical_and_self_consistent(self) -> Self:
        if self.environment is not RiskFactSetEnvironment.SHADOW or self.real_funds_eligible:
            raise ValueError("risk fact set must remain shadow-only")
        timestamps = (self.assembled_at, self.valid_from, self.valid_until)
        if any(value.tzinfo is None or value.utcoffset() is None for value in timestamps):
            raise ValueError("risk fact set timestamps must be timezone-aware")
        if self.valid_until <= self.valid_from or self.valid_from < self.assembled_at:
            raise ValueError("risk fact set validity window is invalid")
        if max(item.received_at for item in self.observations) > self.assembled_at:
            raise ValueError("risk fact set cannot precede its observations")
        fact_types = tuple(item.fact_type for item in self.observations)
        if frozenset(fact_types) != REQUIRED_FACT_TYPES or len(fact_types) != len(
            REQUIRED_FACT_TYPES
        ):
            raise ValueError("risk fact set must cover every required fact type exactly once")
        if fact_types != tuple(sorted(fact_types, key=lambda item: item.value)):
            raise ValueError("risk fact set observations must be canonically ordered")
        if self.evidence_refs != tuple(sorted(set(self.evidence_refs))) or any(
            not reference for reference in self.evidence_refs
        ):
            raise ValueError("risk fact set evidence must be sorted, unique, and non-empty")
        if self.record_hash != risk_fact_set_record_hash(self):
            raise ValueError("risk fact set record hash mismatch")
        if self.evidence_hash != risk_fact_set_evidence_hash(self):
            raise ValueError("risk fact set evidence hash mismatch")
        return self


class RiskFactSetValidationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    organization_id: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=80)
    execution_domain: str = Field(min_length=1, max_length=120)
    account_id: str = Field(min_length=1, max_length=160)
    canonical_instrument_id: str = Field(min_length=1, max_length=255)
    position_mode: str = Field(min_length=1, max_length=80)
    margin_mode: str = Field(min_length=1, max_length=80)
    collateral_pool_id: str = Field(min_length=1, max_length=160)
    validation_time: datetime

    @model_validator(mode="after")
    def validation_time_is_aware(self) -> Self:
        if self.validation_time.tzinfo is None or self.validation_time.utcoffset() is None:
            raise ValueError("risk fact set validation time must be timezone-aware")
        return self


class RiskFactSetValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    valid: bool
    reason_codes: tuple[str, ...]
    risk_fact_set_id: UUID | None
    fact_set_version: str | None
    record_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evidence_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    observations: tuple[FactObservation, ...]
    valid_until: datetime
    validation_snapshot: dict[str, JsonValue]


def _observation_contract(observation: FactObservation) -> dict[str, str]:
    return {
        "fact_type": observation.fact_type.value,
        "status": observation.status.value,
        "source_ref": observation.source_ref,
        "source_version": observation.source_version,
        "payload_hash": observation.payload_hash,
        "event_time": observation.event_time.astimezone(UTC).isoformat(),
        "received_at": observation.received_at.astimezone(UTC).isoformat(),
    }


def risk_fact_set_record_hash(
    request: RegisterRiskFactSetDraft | RegisterRiskFactSetRequest,
) -> str:
    return hash_json(
        {
            "risk_fact_set_id": str(request.risk_fact_set_id),
            "organization_id": request.organization_id,
            "venue": request.venue,
            "execution_domain": request.execution_domain,
            "account_id": request.account_id,
            "canonical_instrument_id": request.canonical_instrument_id,
            "position_mode": request.position_mode,
            "margin_mode": request.margin_mode,
            "collateral_pool_id": request.collateral_pool_id,
            "fact_set_version": request.fact_set_version,
            "observations": [
                _observation_contract(observation) for observation in request.observations
            ],
            "environment": request.environment.value,
            "real_funds_eligible": request.real_funds_eligible,
            "assembled_at": request.assembled_at.astimezone(UTC).isoformat(),
            "valid_from": request.valid_from.astimezone(UTC).isoformat(),
            "valid_until": request.valid_until.astimezone(UTC).isoformat(),
        }
    )


def risk_fact_set_evidence_hash(
    request: RegisterRiskFactSetDraft | RegisterRiskFactSetRequest,
) -> str:
    return hash_json(
        {
            "evidence_refs": list(request.evidence_refs),
            "source_ref": request.source_ref,
        }
    )


class RiskFactSetService:
    command_type = "risk.fact-set.register.v1"
    payload_schema_version = 1

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def register(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        if envelope.command_type != self.command_type:
            raise CommandRejected("COMMAND_TYPE_MISMATCH", "unexpected command type")
        if envelope.payload_schema_version != self.payload_schema_version:
            raise CommandRejected(
                "PAYLOAD_SCHEMA_VERSION_MISMATCH",
                "risk fact set payload schema version is unsupported",
            )
        if (
            envelope.channel is not CommandChannel.INTERNAL
            or envelope.service_principal != RISK_FACT_AGGREGATOR_SERVICE_PRINCIPAL
        ):
            raise CommandRejected(
                "RISK_FACT_AGGREGATOR_SERVICE_REQUIRED",
                "only the exact risk fact aggregator may register sets",
            )
        if envelope.object_type != "RiskFactSet" or envelope.object_id is None:
            raise CommandRejected(
                "OBJECT_BINDING_MISMATCH",
                "RiskFactSet binding is required",
            )
        try:
            request = RegisterRiskFactSetRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected(
                "RISK_FACT_SET_INVALID",
                "risk fact set is invalid",
            ) from exc
        if envelope.object_id != str(request.risk_fact_set_id):
            raise CommandRejected("OBJECT_BINDING_MISMATCH", "fact set identity changed")
        if envelope.expected_version != 1:
            raise CommandRejected("VERSION_CONFLICT", "risk fact set version changed")
        if envelope.scope.get("organization_id") != request.organization_id:
            raise CommandRejected("SCOPE_MISMATCH", "organization scope changed")

        lock_key = _scope_lock_key(
            organization_id=request.organization_id,
            venue=request.venue,
            execution_domain=request.execution_domain,
            account_id=request.account_id,
            canonical_instrument_id=request.canonical_instrument_id,
            position_mode=request.position_mode,
            margin_mode=request.margin_mode,
            collateral_pool_id=request.collateral_pool_id,
        )
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )
        existing = session.execute(
            select(RiskFactSetRecord).where(
                RiskFactSetRecord.organization_id == request.organization_id,
                RiskFactSetRecord.venue == request.venue,
                RiskFactSetRecord.execution_domain == request.execution_domain,
                RiskFactSetRecord.account_id == request.account_id,
                RiskFactSetRecord.canonical_instrument_id == request.canonical_instrument_id,
                RiskFactSetRecord.position_mode == request.position_mode,
                RiskFactSetRecord.margin_mode == request.margin_mode,
                RiskFactSetRecord.collateral_pool_id == request.collateral_pool_id,
                RiskFactSetRecord.fact_set_version == request.fact_set_version,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise CommandRejected(
                "RISK_FACT_SET_VERSION_EXISTS",
                "exact risk fact set version already exists",
            )
        if session.get(RiskFactSetRecord, request.risk_fact_set_id) is not None:
            raise CommandRejected(
                "RISK_FACT_SET_ID_EXISTS",
                "risk fact set identity already exists",
            )

        created_at = self._clock()
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise CommandRejected(
                "RISK_FACT_SET_CLOCK_INVALID",
                "risk fact set clock must be timezone-aware",
            )
        session.add(
            RiskFactSetRecord(
                risk_fact_set_id=request.risk_fact_set_id,
                organization_id=request.organization_id,
                venue=request.venue,
                execution_domain=request.execution_domain,
                account_id=request.account_id,
                canonical_instrument_id=request.canonical_instrument_id,
                position_mode=request.position_mode,
                margin_mode=request.margin_mode,
                collateral_pool_id=request.collateral_pool_id,
                fact_set_version=request.fact_set_version,
                observations=[
                    _observation_contract(observation) for observation in request.observations
                ],
                environment=request.environment.value,
                real_funds_eligible=request.real_funds_eligible,
                assembled_at=request.assembled_at,
                valid_from=request.valid_from,
                valid_until=request.valid_until,
                record_hash=request.record_hash,
                evidence_refs=list(request.evidence_refs),
                evidence_hash=request.evidence_hash,
                source_ref=request.source_ref,
                created_at=created_at,
            )
        )
        RISK_FACT_SET_REGISTRATIONS.labels("REGISTERED").inc()
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="RiskFactSet",
            object_id=str(request.risk_fact_set_id),
            object_version=1,
            data={
                "risk_fact_set_id": str(request.risk_fact_set_id),
                "fact_set_version": request.fact_set_version,
                "record_hash": request.record_hash,
                "environment": request.environment.value,
                "real_funds_eligible": request.real_funds_eligible,
            },
            events=(
                DomainEvent(
                    event_type="RiskFactSetRegistered",
                    aggregate_type="RiskFactSet",
                    aggregate_id=str(request.risk_fact_set_id),
                    payload={
                        "organization_id": request.organization_id,
                        "venue": request.venue,
                        "execution_domain": request.execution_domain,
                        "account_id": request.account_id,
                        "canonical_instrument_id": request.canonical_instrument_id,
                        "fact_set_version": request.fact_set_version,
                        "record_hash": request.record_hash,
                        "environment": request.environment.value,
                        "real_funds_eligible": request.real_funds_eligible,
                    },
                ),
            ),
        )


class RiskFactSetValidator:
    """Resolves the latest complete exact-scope fact set instead of caller observations."""

    def validate(
        self,
        session: Session,
        request: RiskFactSetValidationRequest,
        *,
        lock: bool = False,
    ) -> RiskFactSetValidationResult:
        if lock:
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {
                    "lock_key": _scope_lock_key(
                        organization_id=request.organization_id,
                        venue=request.venue,
                        execution_domain=request.execution_domain,
                        account_id=request.account_id,
                        canonical_instrument_id=request.canonical_instrument_id,
                        position_mode=request.position_mode,
                        margin_mode=request.margin_mode,
                        collateral_pool_id=request.collateral_pool_id,
                    )
                },
            )
        query = (
            select(RiskFactSetRecord)
            .where(
                RiskFactSetRecord.organization_id == request.organization_id,
                RiskFactSetRecord.venue == request.venue,
                RiskFactSetRecord.execution_domain == request.execution_domain,
                RiskFactSetRecord.account_id == request.account_id,
                RiskFactSetRecord.canonical_instrument_id == request.canonical_instrument_id,
                RiskFactSetRecord.position_mode == request.position_mode,
                RiskFactSetRecord.margin_mode == request.margin_mode,
                RiskFactSetRecord.collateral_pool_id == request.collateral_pool_id,
            )
            .order_by(
                RiskFactSetRecord.assembled_at.desc(),
                RiskFactSetRecord.risk_fact_set_id.desc(),
            )
            .limit(1)
        )
        if lock:
            query = query.with_for_update()
        record = session.execute(query).scalars().first()
        reasons: list[str] = []
        validated: RegisterRiskFactSetRequest | None = None
        if record is None:
            reasons.append("RISK_FACT_SET_RECORD_NOT_FOUND")
        else:
            try:
                validated = RegisterRiskFactSetRequest.model_validate(_record_contract(record))
            except ValidationError:
                reasons.append("RISK_FACT_SET_INTEGRITY_FAILED")
            if validated is not None and not (
                validated.valid_from <= request.validation_time < validated.valid_until
            ):
                reasons.append("RISK_FACT_SET_OUTSIDE_VALID_WINDOW")

        valid = not reasons
        observations = validated.observations if validated is not None and valid else ()
        valid_until = (
            validated.valid_until if validated is not None and valid else request.validation_time
        )
        snapshot: dict[str, JsonValue] = {
            "risk_fact_set_id": str(record.risk_fact_set_id) if record is not None else None,
            "fact_set_version": record.fact_set_version if record is not None else None,
            "record_hash": record.record_hash if record is not None else None,
            "evidence_hash": record.evidence_hash if record is not None else None,
            "assembled_at": (record.assembled_at.isoformat() if record is not None else None),
            "valid_until": valid_until.isoformat(),
            "validated_at": request.validation_time.isoformat(),
            "observation_count": len(observations),
            "valid": valid,
            "reason_codes": list[JsonValue](reasons),
        }
        result = RiskFactSetValidationResult(
            valid=valid,
            reason_codes=tuple(reasons),
            risk_fact_set_id=record.risk_fact_set_id if record is not None else None,
            fact_set_version=record.fact_set_version if record is not None else None,
            record_hash=record.record_hash if record is not None else None,
            evidence_hash=record.evidence_hash if record is not None else None,
            observations=observations,
            valid_until=valid_until,
            validation_snapshot=snapshot,
        )
        RISK_FACT_SET_VALIDATIONS.labels(
            "VALID" if valid else "INVALID",
            reasons[0] if reasons else "RISK_FACT_SET_VALID",
        ).inc()
        return result


def _record_contract(record: RiskFactSetRecord) -> dict[str, object]:
    return {
        "risk_fact_set_id": record.risk_fact_set_id,
        "organization_id": record.organization_id,
        "venue": record.venue,
        "execution_domain": record.execution_domain,
        "account_id": record.account_id,
        "canonical_instrument_id": record.canonical_instrument_id,
        "position_mode": record.position_mode,
        "margin_mode": record.margin_mode,
        "collateral_pool_id": record.collateral_pool_id,
        "fact_set_version": record.fact_set_version,
        "observations": record.observations,
        "environment": record.environment,
        "real_funds_eligible": record.real_funds_eligible,
        "assembled_at": record.assembled_at,
        "valid_from": record.valid_from,
        "valid_until": record.valid_until,
        "record_hash": record.record_hash,
        "evidence_refs": record.evidence_refs,
        "evidence_hash": record.evidence_hash,
        "source_ref": record.source_ref,
    }
