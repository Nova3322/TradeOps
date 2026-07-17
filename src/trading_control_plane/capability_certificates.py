from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from trading_control_plane.capability_certificate_models import (
    CapabilityCertificate,
    CapabilityCertificateState,
    CapabilityEvidenceBundle,
)
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
    CAPABILITY_CERTIFICATE_ISSUANCE,
    CAPABILITY_CERTIFICATE_TRANSITIONS,
    CAPABILITY_CERTIFICATE_VALIDATIONS,
)
from trading_control_plane.trading_authorization_models import (
    AddAuthorizationPackage,
    AddAuthorizationPackageState,
    AddUnit,
    AddUnitState,
    InitialAuthorizationState,
    InitialOrderAuthorization,
    TradingAuthorization,
)

CERTIFICATION_SERVICE_PRINCIPAL = "capability-certification-service"


class CapabilityCertificateType(StrEnum):
    STRATEGY_EVIDENCE = "STRATEGY_EVIDENCE"
    EXECUTION = "EXECUTION"
    RISK_COVERAGE = "RISK_COVERAGE"
    MARGIN_NORMALIZATION = "MARGIN_NORMALIZATION"


class CapabilityCertificateAction(StrEnum):
    SUSPEND = "SUSPEND"
    REVOKE = "REVOKE"
    EXPIRE = "EXPIRE"


class CapabilityScope(BaseModel):
    """Every execution-relevant dimension is explicit; no wildcard is accepted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_source: str = Field(pattern=r"^(SYSTEM|MANUAL)$")
    strategy_id: str = Field(min_length=1, max_length=160)
    strategy_version: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=80)
    execution_domain: str = Field(min_length=1, max_length=120)
    account_id: str = Field(min_length=1, max_length=160)
    account_abstraction: str = Field(min_length=1, max_length=80)
    position_mode: str = Field(min_length=1, max_length=80)
    margin_mode: str = Field(min_length=1, max_length=80)
    collateral_scope: str = Field(min_length=1, max_length=120)
    collateral_pool_id: str = Field(min_length=1, max_length=160)
    instrument_id: str = Field(min_length=1, max_length=255)
    contract_multiplier: Decimal = Field(gt=0)
    underlying_id: str = Field(min_length=1, max_length=160)
    sector_id: str = Field(min_length=1, max_length=160)
    risk_cluster_id: str = Field(min_length=1, max_length=160)
    direction: str = Field(pattern=r"^(LONG|SHORT)$")
    risk_tier: str = Field(pattern=r"^(LOW|MEDIUM|HIGH)$")
    max_add_count: int = Field(ge=0, le=3)
    settlement_asset: str = Field(min_length=1, max_length=80)
    worker_id: str = Field(min_length=1, max_length=160)
    worker_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    credential_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    capital_transfer_capability: str = Field(pattern=r"^NOT_APPLICABLE$")


class CapabilityPolicyVersions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_parameter_version: str = Field(min_length=1, max_length=120)
    risk_policy_version: str = Field(min_length=1, max_length=120)
    authorization_policy_version: str = Field(min_length=1, max_length=120)
    catalog_version: str = Field(min_length=1, max_length=120)
    execution_capability_version: str = Field(min_length=1, max_length=120)
    adapter_version: str = Field(min_length=1, max_length=120)
    freqtrade_worker_version: str = Field(min_length=1, max_length=120)
    account_capability_version: str = Field(min_length=1, max_length=120)
    credential_permission_profile_version: str = Field(min_length=1, max_length=120)
    venue_client_version: str = Field(min_length=1, max_length=120)
    instrument_scope_version: str = Field(min_length=1, max_length=120)
    position_management_template_version: str = Field(min_length=1, max_length=120)
    add_milestone_policy_version: str = Field(min_length=1, max_length=120)


class IssueShadowCertificateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    certificate_id: str = Field(min_length=8, max_length=255)
    certificate_type: CapabilityCertificateType
    subject_ref: str = Field(min_length=1, max_length=255)
    scope: CapabilityScope
    policy_versions: CapabilityPolicyVersions
    evidence_bundle_version: str = Field(min_length=1, max_length=120)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    evidence_summary: dict[str, JsonValue] = Field(min_length=1)
    max_order_notional: Decimal = Field(gt=0)
    max_trade_loss: Decimal = Field(gt=0)
    owner_principal: str = Field(min_length=1, max_length=255)
    issuer_principal: str = Field(min_length=1, max_length=255)
    approver_principal_ids: tuple[str, ...] = Field(min_length=1)
    approval_ref: str = Field(min_length=1, max_length=255)
    monitoring_ref: str = Field(min_length=1, max_length=255)
    exit_recovery_ref: str = Field(min_length=1, max_length=255)
    invalidation_conditions: tuple[str, ...] = Field(min_length=1)
    supersedes: str | None = Field(default=None, min_length=8, max_length=255)
    valid_from: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_shadow_issuance_contract(self) -> Self:
        if self.valid_from.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("certificate timestamps must be timezone-aware")
        if self.expires_at <= self.valid_from:
            raise ValueError("certificate expiry must be after validity start")
        if len(set(self.evidence_refs)) != len(self.evidence_refs) or any(
            not ref for ref in self.evidence_refs
        ):
            raise ValueError("evidence references must be unique and non-empty")
        if len(set(self.approver_principal_ids)) != len(self.approver_principal_ids) or any(
            not principal for principal in self.approver_principal_ids
        ):
            raise ValueError("approver principals must be unique")
        if self.issuer_principal in self.approver_principal_ids:
            raise ValueError("certificate issuer cannot approve its own certificate")
        if self.scope.worker_id in {
            self.issuer_principal,
            *self.approver_principal_ids,
        }:
            raise ValueError("the certified execution worker cannot issue or approve itself")
        if len(set(self.invalidation_conditions)) != len(self.invalidation_conditions) or any(
            not condition for condition in self.invalidation_conditions
        ):
            raise ValueError("invalidation conditions must be unique and non-empty")
        return self


class TightenCapabilityCertificateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: CapabilityCertificateAction
    reason_code: str = Field(min_length=3, max_length=160)
    source_ref: str = Field(min_length=1, max_length=255)


class CapabilityValidationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    organization_id: str = Field(min_length=1, max_length=120)
    certificate_id: str = Field(min_length=1, max_length=255)
    required_certificate_type: CapabilityCertificateType = CapabilityCertificateType.EXECUTION
    expected_scope: CapabilityScope
    expected_policy_versions: CapabilityPolicyVersions
    requested_order_notional: Decimal = Field(gt=0)
    requested_trade_loss: Decimal = Field(gt=0)
    validation_time: datetime

    @model_validator(mode="after")
    def timestamp_is_aware(self) -> Self:
        if self.validation_time.tzinfo is None:
            raise ValueError("validation time must be timezone-aware")
        return self


class CapabilityValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    valid: bool
    certificate_id: str
    status: str
    reason_codes: tuple[str, ...]
    certificate_hash: str | None
    scope_hash: str | None
    policy_versions_hash: str | None
    evidence_bundle_hash: str | None
    valid_until: datetime
    validation_snapshot: dict[str, JsonValue]


def _evidence_contract(
    *,
    evidence_bundle_id: str,
    organization_id: str,
    bundle_version: str,
    evidence_refs: list[str],
    evidence_summary: dict[str, Any],
    created_by_principal: str,
    created_at: datetime,
) -> dict[str, Any]:
    return {
        "evidence_bundle_id": evidence_bundle_id,
        "organization_id": organization_id,
        "bundle_version": bundle_version,
        "environment": "SHADOW",
        "certification_profile": "SHADOW_NON_DISPATCH",
        "evidence_refs": evidence_refs,
        "evidence_summary": evidence_summary,
        "created_by_principal": created_by_principal,
        "created_at": created_at,
    }


def _decimal_contract(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _certificate_contract(certificate: CapabilityCertificate) -> dict[str, Any]:
    return {
        "certificate_id": certificate.certificate_id,
        "schema_version": certificate.schema_version,
        "organization_id": certificate.organization_id,
        "certificate_type": certificate.certificate_type,
        "subject_ref": certificate.subject_ref,
        "environment": certificate.environment,
        "real_funds_eligible": certificate.real_funds_eligible,
        "scope": certificate.scope,
        "scope_hash": certificate.scope_hash,
        "policy_versions": certificate.policy_versions,
        "policy_versions_hash": certificate.policy_versions_hash,
        "evidence_bundle_id": certificate.evidence_bundle_id,
        "evidence_bundle_hash": certificate.evidence_bundle_hash,
        "max_order_notional": _decimal_contract(certificate.max_order_notional),
        "max_trade_loss": _decimal_contract(certificate.max_trade_loss),
        "owner_principal": certificate.owner_principal,
        "issuer_principal": certificate.issuer_principal,
        "approver_principal_ids": certificate.approver_principal_ids,
        "approval_ref": certificate.approval_ref,
        "monitoring_ref": certificate.monitoring_ref,
        "exit_recovery_ref": certificate.exit_recovery_ref,
        "invalidation_conditions": certificate.invalidation_conditions,
        "supersedes": certificate.supersedes,
        "issued_at": certificate.issued_at,
        "valid_from": certificate.valid_from,
        "expires_at": certificate.expires_at,
    }


class CapabilityCertificateValidator:
    """Derives certificate validity from durable facts, never from caller assertions."""

    def validate(
        self,
        session: Session,
        request: CapabilityValidationRequest,
        *,
        lock: bool = False,
    ) -> CapabilityValidationResult:
        query = select(CapabilityCertificate).where(
            CapabilityCertificate.certificate_id == request.certificate_id
        )
        certificate = session.execute(
            query.with_for_update() if lock else query
        ).scalar_one_or_none()
        reasons: list[str] = []
        state: CapabilityCertificateState | None = None
        evidence: CapabilityEvidenceBundle | None = None
        if certificate is None:
            reasons.append("CAPABILITY_CERTIFICATE_NOT_FOUND")
        else:
            state_query = select(CapabilityCertificateState).where(
                CapabilityCertificateState.certificate_id == certificate.certificate_id
            )
            state = session.execute(
                state_query.with_for_update() if lock else state_query
            ).scalar_one_or_none()
            evidence = session.get(CapabilityEvidenceBundle, certificate.evidence_bundle_id)
            self._validate_durable_facts(request, certificate, state, evidence, reasons)

        valid = not reasons
        if certificate is not None and valid:
            valid_until = certificate.expires_at
        else:
            valid_until = request.validation_time
        snapshot: dict[str, JsonValue] = {
            "certificate_id": request.certificate_id,
            "required_certificate_type": request.required_certificate_type.value,
            "status": state.status if state is not None else "UNKNOWN",
            "state_version": state.version if state is not None else None,
            "environment": certificate.environment if certificate is not None else None,
            "real_funds_eligible": (
                certificate.real_funds_eligible if certificate is not None else None
            ),
            "scope_hash": certificate.scope_hash if certificate is not None else None,
            "policy_versions_hash": (
                certificate.policy_versions_hash if certificate is not None else None
            ),
            "evidence_bundle_hash": (
                certificate.evidence_bundle_hash if certificate is not None else None
            ),
            "certificate_hash": certificate.certificate_hash if certificate is not None else None,
            "requested_order_notional": str(request.requested_order_notional),
            "requested_trade_loss": str(request.requested_trade_loss),
            "validated_at": request.validation_time.isoformat(),
            "valid_until": valid_until.isoformat(),
            "valid": valid,
            "reason_codes": list[JsonValue](reasons),
        }
        result = CapabilityValidationResult(
            valid=valid,
            certificate_id=request.certificate_id,
            status=state.status if state is not None else "UNKNOWN",
            reason_codes=tuple(reasons),
            certificate_hash=certificate.certificate_hash if certificate is not None else None,
            scope_hash=certificate.scope_hash if certificate is not None else None,
            policy_versions_hash=(
                certificate.policy_versions_hash if certificate is not None else None
            ),
            evidence_bundle_hash=(
                certificate.evidence_bundle_hash if certificate is not None else None
            ),
            valid_until=valid_until,
            validation_snapshot=snapshot,
        )
        CAPABILITY_CERTIFICATE_VALIDATIONS.labels(
            "VALID" if valid else "INVALID",
            reasons[0] if reasons else "CAPABILITY_CERTIFICATE_VALID",
        ).inc()
        return result

    @staticmethod
    def _validate_durable_facts(
        request: CapabilityValidationRequest,
        certificate: CapabilityCertificate,
        state: CapabilityCertificateState | None,
        evidence: CapabilityEvidenceBundle | None,
        reasons: list[str],
    ) -> None:
        now = request.validation_time
        if certificate.organization_id != request.organization_id:
            reasons.append("CAPABILITY_CERTIFICATE_ORGANIZATION_MISMATCH")
        if certificate.certificate_type != request.required_certificate_type.value:
            reasons.append("CAPABILITY_CERTIFICATE_TYPE_MISMATCH")
        if certificate.environment != "SHADOW" or certificate.real_funds_eligible:
            reasons.append("CAPABILITY_CERTIFICATE_ENVIRONMENT_INVALID")
        if state is None or state.status != "ACTIVE":
            reasons.append("CAPABILITY_CERTIFICATE_INACTIVE")
        if not (certificate.valid_from <= now < certificate.expires_at):
            reasons.append("CAPABILITY_CERTIFICATE_OUTSIDE_VALID_WINDOW")
        expected_scope = request.expected_scope.model_dump(mode="json")
        if certificate.scope != expected_scope or certificate.scope_hash != hash_json(
            certificate.scope
        ):
            reasons.append("CAPABILITY_CERTIFICATE_SCOPE_MISMATCH")
        expected_versions = request.expected_policy_versions.model_dump(mode="json")
        if (
            certificate.policy_versions != expected_versions
            or certificate.policy_versions_hash != hash_json(certificate.policy_versions)
        ):
            reasons.append("CAPABILITY_CERTIFICATE_VERSION_MISMATCH")
        if request.requested_order_notional > certificate.max_order_notional:
            reasons.append("CAPABILITY_CERTIFICATE_NOTIONAL_LIMIT_EXCEEDED")
        if request.requested_trade_loss > certificate.max_trade_loss:
            reasons.append("CAPABILITY_CERTIFICATE_LOSS_LIMIT_EXCEEDED")
        if certificate.certificate_hash != hash_json(_certificate_contract(certificate)):
            reasons.append("CAPABILITY_CERTIFICATE_INTEGRITY_FAILED")
        if evidence is None:
            reasons.append("CAPABILITY_EVIDENCE_MISSING")
        else:
            evidence_contract = _evidence_contract(
                evidence_bundle_id=evidence.evidence_bundle_id,
                organization_id=evidence.organization_id,
                bundle_version=evidence.bundle_version,
                evidence_refs=evidence.evidence_refs,
                evidence_summary=evidence.evidence_summary,
                created_by_principal=evidence.created_by_principal,
                created_at=evidence.created_at,
            )
            if (
                evidence.organization_id != certificate.organization_id
                or evidence.environment != "SHADOW"
                or evidence.certification_profile != "SHADOW_NON_DISPATCH"
                or not evidence.evidence_refs
                or evidence.evidence_hash != hash_json(evidence_contract)
                or certificate.evidence_bundle_hash != evidence.evidence_hash
            ):
                reasons.append("CAPABILITY_EVIDENCE_INTEGRITY_FAILED")


class CapabilityCertificateService:
    issue_command_type = "capability.certificate.issue-shadow.v1"
    tighten_command_type = "capability.certificate.tighten.v1"

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def issue_shadow(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        self._require_internal(envelope, self.issue_command_type)
        try:
            request = IssueShadowCertificateRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("CAPABILITY_CERTIFICATE_INPUT_INVALID", str(exc)) from exc
        if envelope.object_id != request.certificate_id:
            raise CommandRejected("OBJECT_BINDING_MISMATCH", "certificate identity changed")
        organization_value = envelope.scope.get("organization_id")
        if not isinstance(organization_value, str) or not organization_value:
            raise CommandRejected("SCOPE_MISMATCH", "organization scope is required")
        organization_id = organization_value
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"capability-certificate:{request.certificate_id}"},
        )
        if session.get(CapabilityCertificate, request.certificate_id) is not None:
            raise CommandRejected(
                "CAPABILITY_CERTIFICATE_ALREADY_EXISTS", "certificate identity already exists"
            )
        now = self._clock()
        if request.valid_from < now:
            raise CommandRejected(
                "CAPABILITY_CERTIFICATE_VALIDITY_INVALID",
                "certificate validity cannot begin before durable issuance",
            )
        if request.supersedes is not None:
            self._validate_superseded_certificate(session, request, organization_id)

        evidence_bundle_id = f"evidence:{hash_json(request.certificate_id)}"
        evidence_refs = list(request.evidence_refs)
        evidence_summary = dict(request.evidence_summary)
        evidence_contract = _evidence_contract(
            evidence_bundle_id=evidence_bundle_id,
            organization_id=organization_id,
            bundle_version=request.evidence_bundle_version,
            evidence_refs=evidence_refs,
            evidence_summary=evidence_summary,
            created_by_principal=request.issuer_principal,
            created_at=now,
        )
        evidence_hash = hash_json(evidence_contract)
        evidence = CapabilityEvidenceBundle(
            evidence_bundle_id=evidence_bundle_id,
            organization_id=organization_id,
            bundle_version=request.evidence_bundle_version,
            environment="SHADOW",
            certification_profile="SHADOW_NON_DISPATCH",
            evidence_refs=evidence_refs,
            evidence_summary=evidence_summary,
            evidence_hash=evidence_hash,
            created_by_principal=request.issuer_principal,
            created_at=now,
        )
        session.add(evidence)
        session.flush()

        scope = request.scope.model_dump(mode="json")
        policy_versions = request.policy_versions.model_dump(mode="json")
        certificate = CapabilityCertificate(
            certificate_id=request.certificate_id,
            schema_version=1,
            organization_id=organization_id,
            certificate_type=request.certificate_type.value,
            subject_ref=request.subject_ref,
            environment="SHADOW",
            real_funds_eligible=False,
            scope=scope,
            scope_hash=hash_json(scope),
            policy_versions=policy_versions,
            policy_versions_hash=hash_json(policy_versions),
            evidence_bundle_id=evidence_bundle_id,
            evidence_bundle_hash=evidence_hash,
            max_order_notional=request.max_order_notional,
            max_trade_loss=request.max_trade_loss,
            owner_principal=request.owner_principal,
            issuer_principal=request.issuer_principal,
            approver_principal_ids=list(request.approver_principal_ids),
            approval_ref=request.approval_ref,
            monitoring_ref=request.monitoring_ref,
            exit_recovery_ref=request.exit_recovery_ref,
            invalidation_conditions=list(request.invalidation_conditions),
            supersedes=request.supersedes,
            certificate_hash="0" * 64,
            issued_at=now,
            valid_from=request.valid_from,
            expires_at=request.expires_at,
        )
        certificate.certificate_hash = hash_json(_certificate_contract(certificate))
        session.add(certificate)
        session.flush()
        session.add(
            CapabilityCertificateState(
                certificate_id=certificate.certificate_id,
                status="ACTIVE",
                version=1,
                reason_code="SHADOW_CERTIFICATE_ISSUED",
                source_ref=request.approval_ref,
                updated_at=now,
            )
        )
        session.flush()
        CAPABILITY_CERTIFICATE_ISSUANCE.labels(
            request.certificate_type.value, "SHADOW", "ISSUED"
        ).inc()
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="CapabilityCertificate",
            object_id=certificate.certificate_id,
            object_version=1,
            data={
                "certificate_id": certificate.certificate_id,
                "certificate_type": certificate.certificate_type,
                "environment": "SHADOW",
                "real_funds_eligible": False,
                "status": "ACTIVE",
                "scope_hash": certificate.scope_hash,
                "evidence_bundle_hash": certificate.evidence_bundle_hash,
                "certificate_hash": certificate.certificate_hash,
            },
            events=(
                DomainEvent(
                    event_type="ShadowCapabilityCertificateIssued",
                    aggregate_type="CapabilityCertificate",
                    aggregate_id=certificate.certificate_id,
                    payload={
                        "certificate_type": certificate.certificate_type,
                        "environment": "SHADOW",
                        "real_funds_eligible": False,
                        "status": "ACTIVE",
                        "scope_hash": certificate.scope_hash,
                    },
                ),
            ),
        )

    def tighten(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        self._require_internal(envelope, self.tighten_command_type)
        if envelope.object_id is None:  # pragma: no cover - required above
            raise RuntimeError("certificate identity missing")
        try:
            request = TightenCapabilityCertificateRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("CAPABILITY_CERTIFICATE_INPUT_INVALID", str(exc)) from exc
        certificate = session.execute(
            select(CapabilityCertificate)
            .where(CapabilityCertificate.certificate_id == envelope.object_id)
            .with_for_update()
        ).scalar_one_or_none()
        if certificate is None:
            raise CommandRejected(
                "CAPABILITY_CERTIFICATE_NOT_FOUND", "capability certificate is unavailable"
            )
        if envelope.scope.get("organization_id") != certificate.organization_id:
            raise CommandRejected("SCOPE_MISMATCH", "organization scope changed")
        state = session.execute(
            select(CapabilityCertificateState)
            .where(CapabilityCertificateState.certificate_id == certificate.certificate_id)
            .with_for_update()
        ).scalar_one_or_none()
        if state is None:
            raise CommandRejected(
                "CAPABILITY_CERTIFICATE_STATE_UNKNOWN", "certificate state is unavailable"
            )
        if envelope.expected_version != state.version:
            raise CommandRejected("VERSION_CONFLICT", "certificate state version changed")
        now = self._clock()
        target = {
            CapabilityCertificateAction.SUSPEND: "SUSPENDED",
            CapabilityCertificateAction.REVOKE: "REVOKED",
            CapabilityCertificateAction.EXPIRE: "EXPIRED",
        }[request.action]
        if request.action is CapabilityCertificateAction.EXPIRE and certificate.expires_at > now:
            raise CommandRejected(
                "CAPABILITY_CERTIFICATE_NOT_EXPIRED", "certificate validity has not elapsed"
            )
        if state.status in {"REVOKED", "EXPIRED"} and state.status != target:
            raise CommandRejected(
                "CAPABILITY_CERTIFICATE_TERMINAL", "terminal certificate state cannot change"
            )
        changed = state.status != target
        invalidated_authorizations = 0
        invalidated_initials = 0
        invalidated_add_packages = 0
        invalidated_add_units = 0
        if changed:
            state.status = target
            state.version += 1
            state.reason_code = request.reason_code
            state.source_ref = request.source_ref
            state.updated_at = now
            (
                invalidated_authorizations,
                invalidated_initials,
                invalidated_add_packages,
                invalidated_add_units,
            ) = self._propagate_invalidation(
                session,
                certificate.certificate_id,
                f"CAPABILITY_CERTIFICATE_{target}",
                now,
            )
            session.flush()
        CAPABILITY_CERTIFICATE_TRANSITIONS.labels(
            request.action.value, target, "CHANGED" if changed else "UNCHANGED"
        ).inc()
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="CapabilityCertificate",
            object_id=certificate.certificate_id,
            object_version=state.version,
            data={
                "certificate_id": certificate.certificate_id,
                "status": state.status,
                "changed": changed,
                "invalidated_authorizations": invalidated_authorizations,
                "invalidated_initial_authorizations": invalidated_initials,
                "invalidated_add_packages": invalidated_add_packages,
                "invalidated_add_units": invalidated_add_units,
                "real_funds_eligible": False,
            },
            events=(
                DomainEvent(
                    event_type="CapabilityCertificateTightened",
                    aggregate_type="CapabilityCertificate",
                    aggregate_id=certificate.certificate_id,
                    payload={
                        "status": state.status,
                        "version": state.version,
                        "reason_code": request.reason_code,
                        "invalidated_authorizations": invalidated_authorizations,
                    },
                ),
            ),
        )

    @staticmethod
    def _require_internal(envelope: CommandEnvelope, command_type: str) -> None:
        if envelope.command_type != command_type:
            raise CommandRejected("COMMAND_TYPE_MISMATCH", "unexpected command type")
        if envelope.object_type != "CapabilityCertificate" or envelope.object_id is None:
            raise CommandRejected(
                "OBJECT_BINDING_MISMATCH", "CapabilityCertificate binding is required"
            )
        if (
            envelope.service_principal != CERTIFICATION_SERVICE_PRINCIPAL
            or envelope.channel not in {CommandChannel.INTERNAL, CommandChannel.SYSTEM}
        ):
            raise CommandRejected(
                "INTERNAL_SERVICE_REQUIRED", "certification service principal is required"
            )

    @staticmethod
    def _validate_superseded_certificate(
        session: Session,
        request: IssueShadowCertificateRequest,
        organization_id: str,
    ) -> None:
        prior = session.execute(
            select(CapabilityCertificate)
            .where(CapabilityCertificate.certificate_id == request.supersedes)
            .with_for_update()
        ).scalar_one_or_none()
        if prior is None:
            raise CommandRejected(
                "SUPERSEDED_CERTIFICATE_NOT_FOUND", "superseded certificate is unavailable"
            )
        state = session.execute(
            select(CapabilityCertificateState)
            .where(CapabilityCertificateState.certificate_id == prior.certificate_id)
            .with_for_update()
        ).scalar_one_or_none()
        if (
            prior.organization_id != organization_id
            or prior.certificate_type != request.certificate_type.value
            or prior.scope != request.scope.model_dump(mode="json")
            or state is None
            or state.status not in {"SUSPENDED", "REVOKED", "EXPIRED"}
        ):
            raise CommandRejected(
                "SUPERSEDED_CERTIFICATE_INVALID",
                "recertification must replace a terminal exact-scope certificate",
            )

    @staticmethod
    def _propagate_invalidation(
        session: Session,
        certificate_id: str,
        reason_code: str,
        now: datetime,
    ) -> tuple[int, int, int, int]:
        roots = tuple(
            session.execute(
                select(TradingAuthorization)
                .where(TradingAuthorization.capability_certificate_ref == certificate_id)
                .with_for_update()
            ).scalars()
        )
        initial_count = 0
        package_count = 0
        unit_count = 0
        for root in roots:
            initial_state = session.execute(
                select(InitialAuthorizationState)
                .join(
                    InitialOrderAuthorization,
                    InitialOrderAuthorization.initial_authorization_id
                    == InitialAuthorizationState.initial_authorization_id,
                )
                .where(InitialOrderAuthorization.authorization_id == root.authorization_id)
                .with_for_update()
            ).scalar_one_or_none()
            if initial_state is not None and initial_state.status == "ACTIVE":
                initial_state.status = "INVALIDATED"
                initial_state.version += 1
                initial_state.reason_code = reason_code
                initial_state.updated_at = now
                initial_count += 1

            package = session.execute(
                select(AddAuthorizationPackage)
                .where(AddAuthorizationPackage.authorization_id == root.authorization_id)
                .with_for_update()
            ).scalar_one_or_none()
            if package is None:
                continue
            package_state = session.execute(
                select(AddAuthorizationPackageState)
                .where(AddAuthorizationPackageState.add_package_id == package.add_package_id)
                .with_for_update()
            ).scalar_one()
            if package_state.status in {"DORMANT", "ACTIVE"}:
                package_state.status = "INVALIDATED"
                package_state.version += 1
                package_state.reason_code = reason_code
                package_state.updated_at = now
                package_count += 1
            unit_states = tuple(
                session.execute(
                    select(AddUnitState)
                    .join(AddUnit, AddUnit.add_unit_id == AddUnitState.add_unit_id)
                    .where(AddUnit.add_package_id == package.add_package_id)
                    .with_for_update()
                ).scalars()
            )
            for unit_state in unit_states:
                if unit_state.status in {"AVAILABLE", "CLAIMED"}:
                    unit_state.status = "INVALIDATED"
                    unit_state.version += 1
                    unit_state.reason_code = reason_code
                    unit_state.updated_at = now
                    unit_count += 1
        return len(roots), initial_count, package_count, unit_count
