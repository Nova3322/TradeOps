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
    STRATEGY_EVALUATION_REGISTRATIONS,
    STRATEGY_EVALUATION_VALIDATIONS,
)
from trading_control_plane.risk_fact_set_models import RiskFactSetRecord
from trading_control_plane.strategy_evaluation_models import StrategyEvaluationRecord
from trading_control_plane.trading_authorization_models import Campaign
from trading_control_plane.venue_fact_models import (
    VenuePositionSnapshot,
    VenueProtectionSnapshot,
)

STRATEGY_EVALUATOR_SERVICE_PRINCIPAL = "strategy-evaluation-service"


class StrategyEvaluationEnvironment(StrEnum):
    SHADOW = "SHADOW"


class StrategyEvaluationKind(StrEnum):
    ADD_CONTINUATION = "ADD_CONTINUATION"


class StrategyEvaluationOutcome(StrEnum):
    PASS = "PASS"  # noqa: S105 - bounded decision status, not a credential
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class StrategyRuleId(StrEnum):
    PULLBACK_ENTRY = "PULLBACK_ENTRY"
    STRATEGY_VALIDITY = "STRATEGY_VALIDITY"
    TREND_CONTINUATION = "TREND_CONTINUATION"


class StrategyRuleStatus(StrEnum):
    PASS = "PASS"  # noqa: S105 - bounded rule status, not a credential
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


REQUIRED_STRATEGY_RULES = frozenset(StrategyRuleId)


class StrategyRuleResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: StrategyRuleId
    status: StrategyRuleStatus
    reason_code: str = Field(min_length=1, max_length=160)
    evidence_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class RegisterStrategyEvaluationDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_evaluation_id: UUID
    campaign_id: UUID
    organization_id: str = Field(min_length=1, max_length=120)
    strategy_id: str = Field(min_length=1, max_length=160)
    strategy_version: str = Field(min_length=1, max_length=120)
    strategy_parameter_version: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=80)
    execution_domain: str = Field(min_length=1, max_length=120)
    account_id: str = Field(min_length=1, max_length=160)
    canonical_instrument_id: str = Field(min_length=1, max_length=255)
    position_mode: str = Field(min_length=1, max_length=80)
    margin_mode: str = Field(min_length=1, max_length=80)
    collateral_pool_id: str = Field(min_length=1, max_length=160)
    evaluation_version: str = Field(min_length=1, max_length=120)
    evaluation_kind: StrategyEvaluationKind
    rule_results: tuple[StrategyRuleResult, ...] = Field(min_length=1)
    outcome: StrategyEvaluationOutcome
    risk_fact_set_id: UUID
    risk_fact_set_version: str = Field(min_length=1, max_length=120)
    risk_fact_set_record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    position_snapshot_id: UUID
    position_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    protection_snapshot_id: UUID
    protection_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_version: str = Field(min_length=1, max_length=120)
    environment: StrategyEvaluationEnvironment
    real_funds_eligible: bool
    evaluated_at: datetime
    valid_from: datetime
    valid_until: datetime
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    source_ref: str = Field(min_length=1, max_length=255)


class RegisterStrategyEvaluationRequest(RegisterStrategyEvaluationDraft):
    record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def record_is_canonical_and_self_consistent(self) -> Self:
        if self.environment is not StrategyEvaluationEnvironment.SHADOW or self.real_funds_eligible:
            raise ValueError("strategy evaluation must remain shadow-only")
        timestamps = (self.evaluated_at, self.valid_from, self.valid_until)
        if any(value.tzinfo is None or value.utcoffset() is None for value in timestamps):
            raise ValueError("strategy evaluation timestamps must be timezone-aware")
        if self.valid_until <= self.valid_from or self.valid_from < self.evaluated_at:
            raise ValueError("strategy evaluation validity window is invalid")
        rule_ids = tuple(item.rule_id for item in self.rule_results)
        if frozenset(rule_ids) != REQUIRED_STRATEGY_RULES or len(rule_ids) != len(
            REQUIRED_STRATEGY_RULES
        ):
            raise ValueError("strategy evaluation must cover every required rule exactly once")
        if rule_ids != tuple(sorted(rule_ids, key=lambda item: item.value)):
            raise ValueError("strategy evaluation rules must be canonically ordered")
        expected_outcome = _outcome_for(self.rule_results)
        if self.outcome is not expected_outcome:
            raise ValueError("strategy evaluation outcome disagrees with rule results")
        if self.evidence_refs != tuple(sorted(set(self.evidence_refs))) or any(
            not reference for reference in self.evidence_refs
        ):
            raise ValueError("strategy evaluation evidence must be sorted, unique, and non-empty")
        if self.record_hash != strategy_evaluation_record_hash(self):
            raise ValueError("strategy evaluation record hash mismatch")
        if self.evidence_hash != strategy_evaluation_evidence_hash(self):
            raise ValueError("strategy evaluation evidence hash mismatch")
        return self


class StrategyEvaluationValidationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_id: UUID
    organization_id: str = Field(min_length=1, max_length=120)
    strategy_id: str = Field(min_length=1, max_length=160)
    strategy_version: str = Field(min_length=1, max_length=120)
    strategy_parameter_version: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=80)
    execution_domain: str = Field(min_length=1, max_length=120)
    account_id: str = Field(min_length=1, max_length=160)
    canonical_instrument_id: str = Field(min_length=1, max_length=255)
    position_mode: str = Field(min_length=1, max_length=80)
    margin_mode: str = Field(min_length=1, max_length=80)
    collateral_pool_id: str = Field(min_length=1, max_length=160)
    risk_fact_set_id: UUID
    risk_fact_set_version: str = Field(min_length=1, max_length=120)
    risk_fact_set_record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    position_snapshot_id: UUID
    position_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    protection_snapshot_id: UUID
    protection_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_time: datetime

    @model_validator(mode="after")
    def validation_time_is_aware(self) -> Self:
        if self.validation_time.tzinfo is None or self.validation_time.utcoffset() is None:
            raise ValueError("strategy evaluation validation time must be timezone-aware")
        return self


class StrategyEvaluationValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    valid: bool
    reason_codes: tuple[str, ...]
    strategy_evaluation_id: UUID | None
    evaluation_version: str | None
    record_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evidence_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    outcome: StrategyEvaluationOutcome | None
    rule_results: tuple[StrategyRuleResult, ...]
    valid_until: datetime
    validation_snapshot: dict[str, JsonValue]


def _scope_lock_key(campaign_id: UUID) -> str:
    return f"strategy-evaluation:{campaign_id}"


def _outcome_for(rule_results: tuple[StrategyRuleResult, ...]) -> StrategyEvaluationOutcome:
    statuses = {item.status for item in rule_results}
    if StrategyRuleStatus.FAIL in statuses:
        return StrategyEvaluationOutcome.FAIL
    if StrategyRuleStatus.UNKNOWN in statuses:
        return StrategyEvaluationOutcome.UNKNOWN
    return StrategyEvaluationOutcome.PASS


def _rule_contract(rule: StrategyRuleResult) -> dict[str, str]:
    return {
        "rule_id": rule.rule_id.value,
        "status": rule.status.value,
        "reason_code": rule.reason_code,
        "evidence_payload_hash": rule.evidence_payload_hash,
    }


def strategy_evaluation_record_hash(
    request: RegisterStrategyEvaluationDraft | RegisterStrategyEvaluationRequest,
) -> str:
    return hash_json(
        {
            "strategy_evaluation_id": str(request.strategy_evaluation_id),
            "campaign_id": str(request.campaign_id),
            "organization_id": request.organization_id,
            "strategy_id": request.strategy_id,
            "strategy_version": request.strategy_version,
            "strategy_parameter_version": request.strategy_parameter_version,
            "venue": request.venue,
            "execution_domain": request.execution_domain,
            "account_id": request.account_id,
            "canonical_instrument_id": request.canonical_instrument_id,
            "position_mode": request.position_mode,
            "margin_mode": request.margin_mode,
            "collateral_pool_id": request.collateral_pool_id,
            "evaluation_version": request.evaluation_version,
            "evaluation_kind": request.evaluation_kind.value,
            "rule_results": [_rule_contract(item) for item in request.rule_results],
            "outcome": request.outcome.value,
            "risk_fact_set_id": str(request.risk_fact_set_id),
            "risk_fact_set_version": request.risk_fact_set_version,
            "risk_fact_set_record_hash": request.risk_fact_set_record_hash,
            "position_snapshot_id": str(request.position_snapshot_id),
            "position_snapshot_hash": request.position_snapshot_hash,
            "protection_snapshot_id": str(request.protection_snapshot_id),
            "protection_snapshot_hash": request.protection_snapshot_hash,
            "evaluator_version": request.evaluator_version,
            "environment": request.environment.value,
            "real_funds_eligible": request.real_funds_eligible,
            "evaluated_at": request.evaluated_at.astimezone(UTC).isoformat(),
            "valid_from": request.valid_from.astimezone(UTC).isoformat(),
            "valid_until": request.valid_until.astimezone(UTC).isoformat(),
        }
    )


def strategy_evaluation_evidence_hash(
    request: RegisterStrategyEvaluationDraft | RegisterStrategyEvaluationRequest,
) -> str:
    return hash_json(
        {
            "evidence_refs": list(request.evidence_refs),
            "source_ref": request.source_ref,
        }
    )


class StrategyEvaluationService:
    command_type = "strategy.evaluation.register.v1"
    payload_schema_version = 1

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def register(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        if envelope.command_type != self.command_type:
            raise CommandRejected("COMMAND_TYPE_MISMATCH", "unexpected command type")
        if envelope.payload_schema_version != self.payload_schema_version:
            raise CommandRejected(
                "PAYLOAD_SCHEMA_VERSION_MISMATCH",
                "strategy evaluation payload schema version is unsupported",
            )
        if (
            envelope.channel is not CommandChannel.INTERNAL
            or envelope.service_principal != STRATEGY_EVALUATOR_SERVICE_PRINCIPAL
        ):
            raise CommandRejected(
                "STRATEGY_EVALUATOR_SERVICE_REQUIRED",
                "only the exact strategy evaluator may register evaluations",
            )
        if envelope.object_type != "StrategyEvaluation" or envelope.object_id is None:
            raise CommandRejected(
                "OBJECT_BINDING_MISMATCH",
                "StrategyEvaluation binding is required",
            )
        try:
            request = RegisterStrategyEvaluationRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected(
                "STRATEGY_EVALUATION_INVALID",
                "strategy evaluation is invalid",
            ) from exc
        if envelope.object_id != str(request.strategy_evaluation_id):
            raise CommandRejected("OBJECT_BINDING_MISMATCH", "strategy evaluation identity changed")
        if envelope.expected_version != 1:
            raise CommandRejected("VERSION_CONFLICT", "strategy evaluation version changed")
        if envelope.scope.get("organization_id") != request.organization_id:
            raise CommandRejected("SCOPE_MISMATCH", "organization scope changed")

        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": _scope_lock_key(request.campaign_id)},
        )
        self._validate_exact_source_bindings(session, request)
        existing_version = session.execute(
            select(StrategyEvaluationRecord).where(
                StrategyEvaluationRecord.campaign_id == request.campaign_id,
                StrategyEvaluationRecord.evaluation_version == request.evaluation_version,
            )
        ).scalar_one_or_none()
        if existing_version is not None:
            raise CommandRejected(
                "STRATEGY_EVALUATION_VERSION_EXISTS",
                "exact campaign evaluation version already exists",
            )
        if session.get(StrategyEvaluationRecord, request.strategy_evaluation_id) is not None:
            raise CommandRejected(
                "STRATEGY_EVALUATION_ID_EXISTS",
                "strategy evaluation identity already exists",
            )

        created_at = self._clock()
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise CommandRejected(
                "STRATEGY_EVALUATION_CLOCK_INVALID",
                "strategy evaluation clock must be timezone-aware",
            )
        session.add(
            StrategyEvaluationRecord(
                strategy_evaluation_id=request.strategy_evaluation_id,
                campaign_id=request.campaign_id,
                organization_id=request.organization_id,
                strategy_id=request.strategy_id,
                strategy_version=request.strategy_version,
                strategy_parameter_version=request.strategy_parameter_version,
                venue=request.venue,
                execution_domain=request.execution_domain,
                account_id=request.account_id,
                canonical_instrument_id=request.canonical_instrument_id,
                position_mode=request.position_mode,
                margin_mode=request.margin_mode,
                collateral_pool_id=request.collateral_pool_id,
                evaluation_version=request.evaluation_version,
                evaluation_kind=request.evaluation_kind.value,
                rule_results=[_rule_contract(item) for item in request.rule_results],
                outcome=request.outcome.value,
                risk_fact_set_id=request.risk_fact_set_id,
                risk_fact_set_version=request.risk_fact_set_version,
                risk_fact_set_record_hash=request.risk_fact_set_record_hash,
                position_snapshot_id=request.position_snapshot_id,
                position_snapshot_hash=request.position_snapshot_hash,
                protection_snapshot_id=request.protection_snapshot_id,
                protection_snapshot_hash=request.protection_snapshot_hash,
                evaluator_version=request.evaluator_version,
                environment=request.environment.value,
                real_funds_eligible=request.real_funds_eligible,
                evaluated_at=request.evaluated_at,
                valid_from=request.valid_from,
                valid_until=request.valid_until,
                record_hash=request.record_hash,
                evidence_refs=list(request.evidence_refs),
                evidence_hash=request.evidence_hash,
                source_ref=request.source_ref,
                created_at=created_at,
            )
        )
        STRATEGY_EVALUATION_REGISTRATIONS.labels("REGISTERED").inc()
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="StrategyEvaluation",
            object_id=str(request.strategy_evaluation_id),
            object_version=1,
            data={
                "strategy_evaluation_id": str(request.strategy_evaluation_id),
                "evaluation_version": request.evaluation_version,
                "outcome": request.outcome.value,
                "record_hash": request.record_hash,
                "environment": request.environment.value,
                "real_funds_eligible": request.real_funds_eligible,
            },
            events=(
                DomainEvent(
                    event_type="StrategyEvaluationRegistered",
                    aggregate_type="StrategyEvaluation",
                    aggregate_id=str(request.strategy_evaluation_id),
                    payload={
                        "campaign_id": str(request.campaign_id),
                        "organization_id": request.organization_id,
                        "evaluation_version": request.evaluation_version,
                        "outcome": request.outcome.value,
                        "record_hash": request.record_hash,
                        "environment": request.environment.value,
                        "real_funds_eligible": request.real_funds_eligible,
                    },
                ),
            ),
        )

    @staticmethod
    def _validate_exact_source_bindings(
        session: Session,
        request: RegisterStrategyEvaluationRequest,
    ) -> None:
        campaign = session.get(Campaign, request.campaign_id)
        if campaign is None:
            raise CommandRejected(
                "STRATEGY_EVALUATION_CAMPAIGN_NOT_FOUND",
                "strategy evaluation campaign is unavailable",
            )
        if (
            campaign.organization_id != request.organization_id
            or campaign.strategy_id != request.strategy_id
            or campaign.strategy_version != request.strategy_version
            or campaign.venue != request.venue
            or campaign.execution_domain != request.execution_domain
            or campaign.account_id != request.account_id
            or campaign.instrument_id != request.canonical_instrument_id
        ):
            raise CommandRejected(
                "STRATEGY_EVALUATION_CAMPAIGN_MISMATCH",
                "strategy evaluation campaign binding changed",
            )
        fact_set = session.get(RiskFactSetRecord, request.risk_fact_set_id)
        position = session.get(VenuePositionSnapshot, request.position_snapshot_id)
        protection = session.get(VenueProtectionSnapshot, request.protection_snapshot_id)
        if fact_set is None or position is None or protection is None:
            raise CommandRejected(
                "STRATEGY_EVALUATION_SOURCE_FACT_NOT_FOUND",
                "strategy evaluation source fact is unavailable",
            )
        if request.evaluated_at < max(
            fact_set.assembled_at,
            position.recorded_at,
            protection.recorded_at,
        ):
            raise CommandRejected(
                "STRATEGY_EVALUATION_PRECEDES_SOURCE_FACT",
                "strategy evaluation cannot precede its source facts",
            )
        exact_scope = (
            request.organization_id,
            request.venue,
            request.execution_domain,
            request.account_id,
            request.canonical_instrument_id,
            request.position_mode,
            request.margin_mode,
            request.collateral_pool_id,
        )
        if (
            (
                fact_set.organization_id,
                fact_set.venue,
                fact_set.execution_domain,
                fact_set.account_id,
                fact_set.canonical_instrument_id,
                fact_set.position_mode,
                fact_set.margin_mode,
                fact_set.collateral_pool_id,
            )
            != exact_scope
            or fact_set.fact_set_version != request.risk_fact_set_version
            or fact_set.record_hash != request.risk_fact_set_record_hash
            or not (fact_set.valid_from <= request.evaluated_at < fact_set.valid_until)
            or (
                position.organization_id,
                position.venue,
                position.execution_domain,
                position.account_id,
                position.instrument_id,
                position.position_mode,
                position.margin_mode,
                position.collateral_pool_id,
            )
            != exact_scope
            or position.snapshot_hash != request.position_snapshot_hash
            or position.position_state != "OPEN"
            or (
                protection.organization_id,
                protection.venue,
                protection.execution_domain,
                protection.account_id,
                protection.instrument_id,
                protection.position_mode,
                protection.margin_mode,
                protection.collateral_pool_id,
            )
            != exact_scope
            or protection.snapshot_hash != request.protection_snapshot_hash
            or protection.protection_state != "CONFIRMED"
            or protection.venue_position_snapshot_id != position.venue_position_snapshot_id
        ):
            raise CommandRejected(
                "STRATEGY_EVALUATION_SOURCE_FACT_MISMATCH",
                "strategy evaluation source fact binding changed",
            )


class StrategyEvaluationValidator:
    """Resolves the latest exact Campaign evaluation and fails closed without PASS."""

    def validate(
        self,
        session: Session,
        request: StrategyEvaluationValidationRequest,
        *,
        lock: bool = False,
    ) -> StrategyEvaluationValidationResult:
        if lock:
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": _scope_lock_key(request.campaign_id)},
            )
        query = (
            select(StrategyEvaluationRecord)
            .where(
                StrategyEvaluationRecord.campaign_id == request.campaign_id,
                StrategyEvaluationRecord.organization_id == request.organization_id,
                StrategyEvaluationRecord.strategy_id == request.strategy_id,
                StrategyEvaluationRecord.strategy_version == request.strategy_version,
                StrategyEvaluationRecord.strategy_parameter_version
                == request.strategy_parameter_version,
                StrategyEvaluationRecord.venue == request.venue,
                StrategyEvaluationRecord.execution_domain == request.execution_domain,
                StrategyEvaluationRecord.account_id == request.account_id,
                StrategyEvaluationRecord.canonical_instrument_id == request.canonical_instrument_id,
                StrategyEvaluationRecord.position_mode == request.position_mode,
                StrategyEvaluationRecord.margin_mode == request.margin_mode,
                StrategyEvaluationRecord.collateral_pool_id == request.collateral_pool_id,
                StrategyEvaluationRecord.evaluation_kind
                == StrategyEvaluationKind.ADD_CONTINUATION.value,
            )
            .order_by(
                StrategyEvaluationRecord.evaluated_at.desc(),
                StrategyEvaluationRecord.strategy_evaluation_id.desc(),
            )
            .limit(1)
        )
        if lock:
            query = query.with_for_update()
        record = session.execute(query).scalars().first()
        reasons: list[str] = []
        validated: RegisterStrategyEvaluationRequest | None = None
        if record is None:
            reasons.append("STRATEGY_EVALUATION_RECORD_NOT_FOUND")
        else:
            try:
                validated = RegisterStrategyEvaluationRequest.model_validate(
                    _record_contract(record)
                )
            except ValidationError:
                reasons.append("STRATEGY_EVALUATION_INTEGRITY_FAILED")
            if validated is not None:
                if (
                    validated.risk_fact_set_id != request.risk_fact_set_id
                    or validated.risk_fact_set_version != request.risk_fact_set_version
                    or validated.risk_fact_set_record_hash != request.risk_fact_set_record_hash
                    or validated.position_snapshot_id != request.position_snapshot_id
                    or validated.position_snapshot_hash != request.position_snapshot_hash
                    or validated.protection_snapshot_id != request.protection_snapshot_id
                    or validated.protection_snapshot_hash != request.protection_snapshot_hash
                ):
                    reasons.append("STRATEGY_EVALUATION_FACT_BINDING_MISMATCH")
                if not (validated.valid_from <= request.validation_time < validated.valid_until):
                    reasons.append("STRATEGY_EVALUATION_OUTSIDE_VALID_WINDOW")
                if validated.outcome is not StrategyEvaluationOutcome.PASS:
                    reasons.append("STRATEGY_EVALUATION_OUTCOME_NOT_PASS")

        valid = not reasons
        valid_until = (
            validated.valid_until if validated is not None and valid else request.validation_time
        )
        snapshot: dict[str, JsonValue] = {
            "strategy_evaluation_id": (
                str(record.strategy_evaluation_id) if record is not None else None
            ),
            "evaluation_version": record.evaluation_version if record is not None else None,
            "outcome": record.outcome if record is not None else None,
            "record_hash": record.record_hash if record is not None else None,
            "evidence_hash": record.evidence_hash if record is not None else None,
            "evaluated_at": record.evaluated_at.isoformat() if record is not None else None,
            "valid_until": valid_until.isoformat(),
            "validated_at": request.validation_time.isoformat(),
            "valid": valid,
            "reason_codes": list[JsonValue](reasons),
        }
        result = StrategyEvaluationValidationResult(
            valid=valid,
            reason_codes=tuple(reasons),
            strategy_evaluation_id=(record.strategy_evaluation_id if record is not None else None),
            evaluation_version=record.evaluation_version if record is not None else None,
            record_hash=record.record_hash if record is not None else None,
            evidence_hash=record.evidence_hash if record is not None else None,
            outcome=(StrategyEvaluationOutcome(record.outcome) if record is not None else None),
            rule_results=(validated.rule_results if validated is not None else ()),
            valid_until=valid_until,
            validation_snapshot=snapshot,
        )
        STRATEGY_EVALUATION_VALIDATIONS.labels(
            "VALID" if valid else "INVALID",
            reasons[0] if reasons else "STRATEGY_EVALUATION_VALID",
        ).inc()
        return result


def _record_contract(record: StrategyEvaluationRecord) -> dict[str, object]:
    return {
        "strategy_evaluation_id": record.strategy_evaluation_id,
        "campaign_id": record.campaign_id,
        "organization_id": record.organization_id,
        "strategy_id": record.strategy_id,
        "strategy_version": record.strategy_version,
        "strategy_parameter_version": record.strategy_parameter_version,
        "venue": record.venue,
        "execution_domain": record.execution_domain,
        "account_id": record.account_id,
        "canonical_instrument_id": record.canonical_instrument_id,
        "position_mode": record.position_mode,
        "margin_mode": record.margin_mode,
        "collateral_pool_id": record.collateral_pool_id,
        "evaluation_version": record.evaluation_version,
        "evaluation_kind": record.evaluation_kind,
        "rule_results": record.rule_results,
        "outcome": record.outcome,
        "risk_fact_set_id": record.risk_fact_set_id,
        "risk_fact_set_version": record.risk_fact_set_version,
        "risk_fact_set_record_hash": record.risk_fact_set_record_hash,
        "position_snapshot_id": record.position_snapshot_id,
        "position_snapshot_hash": record.position_snapshot_hash,
        "protection_snapshot_id": record.protection_snapshot_id,
        "protection_snapshot_hash": record.protection_snapshot_hash,
        "evaluator_version": record.evaluator_version,
        "environment": record.environment,
        "real_funds_eligible": record.real_funds_eligible,
        "evaluated_at": record.evaluated_at,
        "valid_from": record.valid_from,
        "valid_until": record.valid_until,
        "record_hash": record.record_hash,
        "evidence_refs": record.evidence_refs,
        "evidence_hash": record.evidence_hash,
        "source_ref": record.source_ref,
    }
