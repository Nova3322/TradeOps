from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Self, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from trading_control_plane.capability_certificate_models import CapabilityCertificate
from trading_control_plane.capability_certificates import (
    CapabilityCertificateValidator,
    CapabilityPolicyVersions,
    CapabilityScope,
    CapabilityValidationRequest,
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
from trading_control_plane.execution_models import (
    ExecutionRiskDecision,
    OrderIntent,
    OrderIntentState,
    RiskReservation,
)
from trading_control_plane.metrics import (
    SENDER_LEASE_OPERATIONS,
    SENDER_LEASE_VALIDATIONS,
    SHADOW_DISPATCH_CLAIMS,
)
from trading_control_plane.models import CapabilityGate
from trading_control_plane.sender_fencing_models import (
    ExecutionSenderLease,
    ExecutionSenderScope,
    ExecutionSenderScopeState,
    ShadowDispatchClaim,
)

FENCING_SERVICE_PRINCIPAL = "execution-fencing-service"
SHADOW_FENCING_POLICY_VERSION = "shadow-fencing-v1"


class SenderScopeBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    organization_id: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=80)
    execution_domain: str = Field(min_length=1, max_length=120)
    account_id: str = Field(min_length=1, max_length=160)
    account_abstraction: str = Field(min_length=1, max_length=80)
    position_mode: str = Field(min_length=1, max_length=80)
    margin_mode: str = Field(min_length=1, max_length=80)
    collateral_scope: str = Field(min_length=1, max_length=120)
    collateral_pool_id: str = Field(min_length=1, max_length=160)


def sender_scope_id(scope: SenderScopeBinding) -> str:
    return f"sender-scope:{hash_json(scope.model_dump(mode='json'))}"


class AcquireShadowSenderLeaseRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lease_id: UUID
    scope: SenderScopeBinding
    owner_worker_id: str = Field(min_length=1, max_length=160)
    worker_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    credential_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    lease_ttl_seconds: int = Field(ge=1, le=300)
    max_lease_lifetime_seconds: int = Field(ge=1, le=3600)
    worker_observed_at: datetime
    reconciliation_evidence_ref: str = Field(min_length=1, max_length=255)
    risk_state_ack_ref: str = Field(min_length=1, max_length=255)
    reason_code: str = Field(min_length=3, max_length=160)

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.worker_observed_at.tzinfo is None:
            raise ValueError("worker observation time must be timezone-aware")
        if self.max_lease_lifetime_seconds < self.lease_ttl_seconds:
            raise ValueError("maximum lease lifetime must cover the initial lease")
        return self


class RenewShadowSenderLeaseRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lease_id: UUID
    fencing_token: int = Field(gt=0)
    lease_ttl_seconds: int = Field(ge=1, le=300)
    worker_observed_at: datetime
    renewal_evidence_ref: str = Field(min_length=1, max_length=255)
    reason_code: str = Field(min_length=3, max_length=160)

    @model_validator(mode="after")
    def timestamp_is_aware(self) -> Self:
        if self.worker_observed_at.tzinfo is None:
            raise ValueError("worker observation time must be timezone-aware")
        return self


class SenderLeaseAction(StrEnum):
    RELEASE = "RELEASE"
    FENCE = "FENCE"
    EXPIRE = "EXPIRE"


class TightenSenderLeaseRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: SenderLeaseAction
    lease_id: UUID | None = None
    fencing_token: int | None = Field(default=None, gt=0)
    reason_code: str = Field(min_length=3, max_length=160)
    source_ref: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if (self.lease_id is None) != (self.fencing_token is None):
            raise ValueError("lease identity and fencing token must be supplied together")
        if self.action in {SenderLeaseAction.RELEASE, SenderLeaseAction.EXPIRE} and (
            self.lease_id is None
        ):
            raise ValueError("release and expiry require the exact current lease")
        return self


class ClaimShadowOrderIntentRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: SenderScopeBinding
    lease_id: UUID
    fencing_token: int = Field(gt=0)
    worker_observed_at: datetime
    reason_code: str = Field(min_length=3, max_length=160)

    @model_validator(mode="after")
    def timestamp_is_aware(self) -> Self:
        if self.worker_observed_at.tzinfo is None:
            raise ValueError("worker observation time must be timezone-aware")
        return self


class SenderLeaseValidationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: SenderScopeBinding
    lease_id: UUID
    fencing_token: int = Field(gt=0)
    owner_worker_id: str = Field(min_length=1, max_length=160)
    worker_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    credential_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_time: datetime

    @model_validator(mode="after")
    def timestamp_is_aware(self) -> Self:
        if self.validation_time.tzinfo is None:
            raise ValueError("validation time must be timezone-aware")
        return self


class SenderLeaseValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    valid: bool
    scope_id: str
    lease_id: UUID
    fencing_token: int
    status: str
    reason_codes: tuple[str, ...]
    scope_hash: str | None
    lease_hash: str | None
    valid_until: datetime


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _scope_contract(scope: ExecutionSenderScope) -> dict[str, Any]:
    return {
        "organization_id": scope.organization_id,
        "venue": scope.venue,
        "execution_domain": scope.execution_domain,
        "account_id": scope.account_id,
        "account_abstraction": scope.account_abstraction,
        "position_mode": scope.position_mode,
        "margin_mode": scope.margin_mode,
        "collateral_scope": scope.collateral_scope,
        "collateral_pool_id": scope.collateral_pool_id,
    }


def _lease_contract(lease: ExecutionSenderLease) -> dict[str, Any]:
    return {
        "lease_id": str(lease.lease_id),
        "scope_id": lease.scope_id,
        "organization_id": lease.organization_id,
        "fencing_token": lease.fencing_token,
        "owner_worker_id": lease.owner_worker_id,
        "worker_config_hash": lease.worker_config_hash,
        "credential_fingerprint": lease.credential_fingerprint,
        "environment": lease.environment,
        "live_dispatch_eligible": lease.live_dispatch_eligible,
        "lease_policy_version": lease.lease_policy_version,
        "reconciliation_evidence_ref": lease.reconciliation_evidence_ref,
        "risk_state_ack_ref": lease.risk_state_ack_ref,
        "worker_observed_at": _iso(lease.worker_observed_at),
        "issued_at": _iso(lease.issued_at),
        "initial_expires_at": _iso(lease.initial_expires_at),
        "max_expires_at": _iso(lease.max_expires_at),
    }


class SenderLeaseValidator:
    def validate(
        self, session: Session, request: SenderLeaseValidationRequest
    ) -> SenderLeaseValidationResult:
        expected_scope_id = sender_scope_id(request.scope)
        scope = session.get(ExecutionSenderScope, expected_scope_id)
        state = session.get(ExecutionSenderScopeState, expected_scope_id)
        lease = session.get(ExecutionSenderLease, request.lease_id)
        gate = session.get(CapabilityGate, "LIVE_ORDER_SEND")
        reasons: list[str] = []
        if scope is None:
            reasons.append("SENDER_SCOPE_NOT_FOUND")
        else:
            expected_scope = request.scope.model_dump(mode="json")
            if (
                scope.scope_id != expected_scope_id
                or _scope_contract(scope) != expected_scope
                or scope.scope_hash != hash_json(expected_scope)
            ):
                reasons.append("SENDER_SCOPE_INTEGRITY_FAILED")
            if scope.environment != "SHADOW" or scope.live_dispatch_eligible:
                reasons.append("SENDER_SCOPE_NOT_SHADOW_ONLY")
        if state is None:
            reasons.append("SENDER_SCOPE_STATE_UNKNOWN")
        elif (
            state.status != "LEASED"
            or state.active_lease_id != request.lease_id
            or state.current_fencing_token != request.fencing_token
        ):
            reasons.append("SENDER_LEASE_INACTIVE")
        elif state.lease_expires_at is None or request.validation_time >= state.lease_expires_at:
            reasons.append("SENDER_LEASE_EXPIRED")
        if lease is None:
            reasons.append("SENDER_LEASE_NOT_FOUND")
        else:
            if (
                lease.scope_id != expected_scope_id
                or lease.fencing_token != request.fencing_token
                or lease.lease_hash != hash_json(_lease_contract(lease))
            ):
                reasons.append("SENDER_LEASE_INTEGRITY_FAILED")
            if (
                lease.owner_worker_id != request.owner_worker_id
                or lease.worker_config_hash != request.worker_config_hash
                or lease.credential_fingerprint != request.credential_fingerprint
            ):
                reasons.append("SENDER_LEASE_OWNER_MISMATCH")
            if lease.environment != "SHADOW" or lease.live_dispatch_eligible:
                reasons.append("SENDER_LEASE_NOT_SHADOW_ONLY")
        if gate is None or gate.status != "DISABLED":
            reasons.append("LIVE_ORDER_SEND_NOT_DISABLED")
        unique_reasons = tuple(dict.fromkeys(reasons))
        primary = unique_reasons[0] if unique_reasons else "SENDER_LEASE_VALID"
        SENDER_LEASE_VALIDATIONS.labels("VALID" if not unique_reasons else "INVALID", primary).inc()
        valid_until = (
            state.lease_expires_at
            if state is not None and state.lease_expires_at is not None
            else request.validation_time
        )
        return SenderLeaseValidationResult(
            valid=not unique_reasons,
            scope_id=expected_scope_id,
            lease_id=request.lease_id,
            fencing_token=request.fencing_token,
            status=state.status if state is not None else "UNKNOWN",
            reason_codes=unique_reasons,
            scope_hash=scope.scope_hash if scope is not None else None,
            lease_hash=lease.lease_hash if lease is not None else None,
            valid_until=valid_until,
        )


class SenderFencingService:
    acquire_command_type = "execution.sender-lease.acquire-shadow.v1"
    renew_command_type = "execution.sender-lease.renew-shadow.v1"
    tighten_command_type = "execution.sender-lease.tighten.v1"
    claim_command_type = "execution.order-intent.claim-shadow.v1"

    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        *,
        max_clock_skew: timedelta = timedelta(seconds=5),
    ) -> None:
        self._clock = clock
        self._max_clock_skew = max_clock_skew

    def acquire_shadow(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        self._require_fencing_service(envelope, self.acquire_command_type)
        try:
            request = AcquireShadowSenderLeaseRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("SENDER_LEASE_INPUT_INVALID", str(exc)) from exc
        scope_id = sender_scope_id(request.scope)
        if envelope.object_id != scope_id:
            raise CommandRejected("OBJECT_BINDING_MISMATCH", "sender scope identity changed")
        self._require_organization(envelope, request.scope.organization_id)
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": scope_id},
        )
        scope = session.get(ExecutionSenderScope, scope_id)
        state = session.execute(
            select(ExecutionSenderScopeState)
            .where(ExecutionSenderScopeState.scope_id == scope_id)
            .with_for_update()
        ).scalar_one_or_none()
        now = self._now(session)
        self._require_worker_clock(request.worker_observed_at, now)
        scope_contract = request.scope.model_dump(mode="json")
        if scope is None:
            if envelope.expected_version is not None:
                raise CommandRejected("VERSION_CONFLICT", "sender scope does not exist")
            scope = ExecutionSenderScope(
                scope_id=scope_id,
                schema_version=1,
                environment="SHADOW",
                live_dispatch_eligible=False,
                scope_hash=hash_json(scope_contract),
                created_at=now,
                **scope_contract,
            )
            session.add(scope)
            session.flush()
        elif (
            scope.scope_hash != hash_json(scope_contract)
            or _scope_contract(scope) != scope_contract
        ):
            raise CommandRejected("SENDER_SCOPE_INTEGRITY_FAILED", "sender scope drifted")

        if state is not None:
            if envelope.expected_version != state.version:
                raise CommandRejected("VERSION_CONFLICT", "sender scope version changed")
            if (
                state.status == "LEASED"
                and state.lease_expires_at is not None
                and now < state.lease_expires_at
            ):
                raise CommandRejected(
                    "SENDER_ALREADY_ACTIVE", "an unexpired sender already owns this scope"
                )
        elif envelope.expected_version is not None:
            raise CommandRejected("VERSION_CONFLICT", "sender scope state is unavailable")
        if session.get(ExecutionSenderLease, request.lease_id) is not None:
            raise CommandRejected("SENDER_LEASE_ALREADY_EXISTS", "lease identity already exists")

        fencing_token = (state.current_fencing_token if state is not None else 0) + 1
        initial_expires_at = now + timedelta(seconds=request.lease_ttl_seconds)
        max_expires_at = now + timedelta(seconds=request.max_lease_lifetime_seconds)
        lease = ExecutionSenderLease(
            lease_id=request.lease_id,
            scope_id=scope_id,
            organization_id=request.scope.organization_id,
            fencing_token=fencing_token,
            owner_worker_id=request.owner_worker_id,
            worker_config_hash=request.worker_config_hash,
            credential_fingerprint=request.credential_fingerprint,
            environment="SHADOW",
            live_dispatch_eligible=False,
            lease_policy_version=SHADOW_FENCING_POLICY_VERSION,
            reconciliation_evidence_ref=request.reconciliation_evidence_ref,
            risk_state_ack_ref=request.risk_state_ack_ref,
            worker_observed_at=request.worker_observed_at,
            issued_at=now,
            initial_expires_at=initial_expires_at,
            max_expires_at=max_expires_at,
            lease_hash="0" * 64,
        )
        lease.lease_hash = hash_json(_lease_contract(lease))
        session.add(lease)
        session.flush()
        if state is None:
            state = ExecutionSenderScopeState(
                scope_id=scope_id,
                status="LEASED",
                version=1,
                current_fencing_token=fencing_token,
                active_lease_id=lease.lease_id,
                lease_expires_at=initial_expires_at,
                reason_code=request.reason_code,
                source_ref=request.reconciliation_evidence_ref,
                updated_at=now,
            )
            session.add(state)
        else:
            state.status = "LEASED"
            state.version += 1
            state.current_fencing_token = fencing_token
            state.active_lease_id = lease.lease_id
            state.lease_expires_at = initial_expires_at
            state.reason_code = request.reason_code
            state.source_ref = request.reconciliation_evidence_ref
            state.updated_at = now
        session.flush()
        SENDER_LEASE_OPERATIONS.labels("ACQUIRE_SHADOW", "COMPLETED").inc()
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="ExecutionSenderScope",
            object_id=scope_id,
            object_version=state.version,
            data={
                "scope_id": scope_id,
                "lease_id": str(lease.lease_id),
                "fencing_token": fencing_token,
                "status": "LEASED",
                "environment": "SHADOW",
                "live_dispatch_eligible": False,
                "lease_expires_at": initial_expires_at.isoformat(),
                "lease_hash": lease.lease_hash,
            },
            events=(
                DomainEvent(
                    event_type="ShadowSenderLeaseAcquired",
                    aggregate_type="ExecutionSenderScope",
                    aggregate_id=scope_id,
                    payload={
                        "lease_id": str(lease.lease_id),
                        "fencing_token": fencing_token,
                        "owner_worker_id": lease.owner_worker_id,
                        "lease_expires_at": initial_expires_at.isoformat(),
                        "live_dispatch_eligible": False,
                    },
                ),
            ),
        )

    def renew_shadow(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        self._require_object(envelope, self.renew_command_type, "ExecutionSenderScope")
        try:
            request = RenewShadowSenderLeaseRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("SENDER_LEASE_INPUT_INVALID", str(exc)) from exc
        state, lease = self._load_current_lease(session, envelope.object_id)
        self._require_organization(envelope, lease.organization_id)
        if envelope.expected_version != state.version:
            raise CommandRejected("VERSION_CONFLICT", "sender scope version changed")
        if (
            request.lease_id != lease.lease_id
            or request.fencing_token != lease.fencing_token
            or state.active_lease_id != lease.lease_id
            or state.current_fencing_token != lease.fencing_token
            or state.status != "LEASED"
        ):
            raise CommandRejected("SENDER_LEASE_INACTIVE", "lease no longer owns this scope")
        if (
            envelope.channel is not CommandChannel.INTERNAL
            or envelope.service_principal != lease.owner_worker_id
        ):
            raise CommandRejected("SENDER_OWNER_REQUIRED", "lease owner principal is required")
        now = self._now(session)
        self._require_worker_clock(request.worker_observed_at, now)
        if state.lease_expires_at is None or now >= state.lease_expires_at:
            raise CommandRejected("SENDER_LEASE_EXPIRED", "expired lease cannot be renewed")
        new_expires_at = now + timedelta(seconds=request.lease_ttl_seconds)
        if new_expires_at <= state.lease_expires_at:
            raise CommandRejected("SENDER_LEASE_NOT_EXTENDED", "renewal must extend the lease")
        if new_expires_at > lease.max_expires_at:
            raise CommandRejected(
                "SENDER_LEASE_MAX_LIFETIME_EXCEEDED", "renewal exceeds maximum lifetime"
            )
        state.version += 1
        state.lease_expires_at = new_expires_at
        state.reason_code = request.reason_code
        state.source_ref = request.renewal_evidence_ref
        state.updated_at = now
        session.flush()
        SENDER_LEASE_OPERATIONS.labels("RENEW_SHADOW", "COMPLETED").inc()
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="ExecutionSenderScope",
            object_id=state.scope_id,
            object_version=state.version,
            data={
                "scope_id": state.scope_id,
                "lease_id": str(lease.lease_id),
                "fencing_token": lease.fencing_token,
                "status": state.status,
                "lease_expires_at": new_expires_at.isoformat(),
                "live_dispatch_eligible": False,
            },
            events=(
                DomainEvent(
                    event_type="ShadowSenderLeaseRenewed",
                    aggregate_type="ExecutionSenderScope",
                    aggregate_id=state.scope_id,
                    payload={
                        "lease_id": str(lease.lease_id),
                        "fencing_token": lease.fencing_token,
                        "lease_expires_at": new_expires_at.isoformat(),
                    },
                ),
            ),
        )

    def tighten(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        self._require_object(envelope, self.tighten_command_type, "ExecutionSenderScope")
        try:
            request = TightenSenderLeaseRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("SENDER_LEASE_INPUT_INVALID", str(exc)) from exc
        state = session.execute(
            select(ExecutionSenderScopeState)
            .where(ExecutionSenderScopeState.scope_id == envelope.object_id)
            .with_for_update()
        ).scalar_one_or_none()
        scope = session.get(ExecutionSenderScope, envelope.object_id)
        if state is None or scope is None:
            raise CommandRejected("SENDER_SCOPE_NOT_FOUND", "sender scope is unavailable")
        self._require_organization(envelope, scope.organization_id)
        if envelope.expected_version != state.version:
            raise CommandRejected("VERSION_CONFLICT", "sender scope version changed")
        lease = (
            session.get(ExecutionSenderLease, state.active_lease_id)
            if state.active_lease_id is not None
            else None
        )
        if request.action is SenderLeaseAction.FENCE:
            if (
                envelope.channel not in {CommandChannel.INTERNAL, CommandChannel.SYSTEM}
                or envelope.service_principal != FENCING_SERVICE_PRINCIPAL
            ):
                raise CommandRejected(
                    "FENCING_SERVICE_REQUIRED", "fencing service principal is required"
                )
            if request.lease_id is not None and (
                lease is None
                or request.lease_id != lease.lease_id
                or request.fencing_token != lease.fencing_token
            ):
                raise CommandRejected("SENDER_LEASE_INACTIVE", "lease binding changed")
        else:
            if (
                lease is None
                or request.lease_id != lease.lease_id
                or request.fencing_token != lease.fencing_token
                or state.status != "LEASED"
            ):
                raise CommandRejected("SENDER_LEASE_INACTIVE", "lease no longer owns this scope")
            if request.action is SenderLeaseAction.RELEASE:
                if (
                    envelope.channel is not CommandChannel.INTERNAL
                    or envelope.service_principal != lease.owner_worker_id
                ):
                    raise CommandRejected(
                        "SENDER_OWNER_REQUIRED", "lease owner principal is required"
                    )
            elif (
                envelope.channel not in {CommandChannel.INTERNAL, CommandChannel.SYSTEM}
                or envelope.service_principal != FENCING_SERVICE_PRINCIPAL
            ):
                raise CommandRejected(
                    "FENCING_SERVICE_REQUIRED", "fencing service principal is required"
                )
        now = self._now(session)
        if request.action is SenderLeaseAction.EXPIRE and (
            state.lease_expires_at is None or now < state.lease_expires_at
        ):
            raise CommandRejected("SENDER_LEASE_NOT_EXPIRED", "lease validity has not elapsed")
        state.status = "FENCED" if request.action is SenderLeaseAction.FENCE else "UNOWNED"
        state.version += 1
        state.current_fencing_token += 1
        state.active_lease_id = None
        state.lease_expires_at = None
        state.reason_code = request.reason_code
        state.source_ref = request.source_ref
        state.updated_at = now
        session.flush()
        SENDER_LEASE_OPERATIONS.labels(request.action.value, "COMPLETED").inc()
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="ExecutionSenderScope",
            object_id=state.scope_id,
            object_version=state.version,
            data={
                "scope_id": state.scope_id,
                "status": state.status,
                "fencing_token": state.current_fencing_token,
                "active_lease_id": None,
                "live_dispatch_eligible": False,
            },
            events=(
                DomainEvent(
                    event_type="SenderLeaseTightened",
                    aggregate_type="ExecutionSenderScope",
                    aggregate_id=state.scope_id,
                    payload={
                        "action": request.action.value,
                        "status": state.status,
                        "fencing_token": state.current_fencing_token,
                        "reason_code": request.reason_code,
                    },
                ),
            ),
        )

    def claim_shadow_order_intent(
        self, session: Session, envelope: CommandEnvelope
    ) -> CommandOutcome:
        self._require_object(envelope, self.claim_command_type, "OrderIntent")
        try:
            order_intent_id = UUID(cast(str, envelope.object_id))
            request = ClaimShadowOrderIntentRequest.model_validate(envelope.payload)
        except (ValueError, ValidationError) as exc:
            raise CommandRejected(
                "SHADOW_DISPATCH_CLAIM_INVALID", "claim input is invalid"
            ) from exc
        scope_id = sender_scope_id(request.scope)
        self._require_organization(envelope, request.scope.organization_id)
        scope_state = session.execute(
            select(ExecutionSenderScopeState)
            .where(ExecutionSenderScopeState.scope_id == scope_id)
            .with_for_update()
        ).scalar_one_or_none()
        if scope_state is None:
            raise CommandRejected("SENDER_SCOPE_NOT_FOUND", "sender scope is unavailable")
        lease = session.get(ExecutionSenderLease, request.lease_id)
        if lease is None:
            raise CommandRejected("SENDER_LEASE_NOT_FOUND", "sender lease is unavailable")
        if (
            envelope.channel is not CommandChannel.INTERNAL
            or envelope.service_principal != lease.owner_worker_id
        ):
            raise CommandRejected("SENDER_OWNER_REQUIRED", "lease owner principal is required")
        now = self._now(session)
        self._require_worker_clock(request.worker_observed_at, now)
        lease_validation = SenderLeaseValidator().validate(
            session,
            SenderLeaseValidationRequest(
                scope=request.scope,
                lease_id=request.lease_id,
                fencing_token=request.fencing_token,
                owner_worker_id=lease.owner_worker_id,
                worker_config_hash=lease.worker_config_hash,
                credential_fingerprint=lease.credential_fingerprint,
                validation_time=now,
            ),
        )
        if not lease_validation.valid:
            raise CommandRejected(
                lease_validation.reason_codes[0], "sender lease validation failed closed"
            )
        if (
            session.execute(
                select(ShadowDispatchClaim).where(
                    ShadowDispatchClaim.order_intent_id == order_intent_id
                )
            ).scalar_one_or_none()
            is not None
        ):
            raise CommandRejected(
                "ORDER_INTENT_ALREADY_CLAIMED", "order intent already has a shadow claim"
            )
        intent = session.execute(
            select(OrderIntent)
            .where(OrderIntent.order_intent_id == order_intent_id)
            .with_for_update()
        ).scalar_one_or_none()
        if intent is None:
            raise CommandRejected("ORDER_INTENT_NOT_FOUND", "order intent is unavailable")
        intent_state = session.execute(
            select(OrderIntentState)
            .where(OrderIntentState.order_intent_id == order_intent_id)
            .with_for_update()
        ).scalar_one_or_none()
        if intent_state is None or intent_state.status != "INTENT_CREATED":
            raise CommandRejected(
                "ORDER_INTENT_NOT_CLAIMABLE", "only a fresh intent can be shadow claimed"
            )
        if envelope.expected_version != intent_state.version:
            raise CommandRejected("VERSION_CONFLICT", "order intent state version changed")
        decision = session.get(ExecutionRiskDecision, intent.execution_risk_decision_id)
        reservation = session.execute(
            select(RiskReservation).where(RiskReservation.order_intent_id == order_intent_id)
        ).scalar_one_or_none()
        certificate = session.get(CapabilityCertificate, intent.capability_certificate_ref)
        if decision is None or reservation is None or certificate is None:
            raise CommandRejected(
                "ORDER_INTENT_DURABLE_GRAPH_INCOMPLETE", "intent bindings are unavailable"
            )
        if decision.organization_id != request.scope.organization_id:
            raise CommandRejected("SCOPE_MISMATCH", "organization scope changed")
        if now < intent.valid_from or now >= intent.valid_until:
            raise CommandRejected("ORDER_INTENT_EXPIRED", "order intent is outside its window")
        if intent.execution_mode != "SHADOW" or intent.dispatch_eligible:
            raise CommandRejected(
                "REAL_DISPATCH_PATH_FORBIDDEN", "this service accepts shadow-only intents"
            )
        certificate_scope = CapabilityScope.model_validate(certificate.scope)
        certificate_versions = CapabilityPolicyVersions.model_validate(certificate.policy_versions)
        self._validate_intent_scope(intent, request.scope, lease, certificate_scope)
        certificate_validation = CapabilityCertificateValidator().validate(
            session,
            CapabilityValidationRequest(
                organization_id=request.scope.organization_id,
                certificate_id=certificate.certificate_id,
                expected_scope=certificate_scope,
                expected_policy_versions=certificate_versions,
                requested_order_notional=(
                    intent.expected_quantity
                    * intent.price_reference
                    * certificate_scope.contract_multiplier
                ),
                requested_trade_loss=reservation.reserved_heat,
                validation_time=now,
            ),
        )
        if not certificate_validation.valid:
            raise CommandRejected(
                certificate_validation.reason_codes[0],
                "capability certificate validation failed closed",
            )
        gate = session.get(CapabilityGate, "LIVE_ORDER_SEND")
        if gate is None or gate.status != "DISABLED":
            raise CommandRejected(
                "LIVE_ORDER_SEND_NOT_DISABLED", "shadow claim requires the live gate closed"
            )
        if scope_state.lease_expires_at is None:  # pragma: no cover - validator rejects above
            raise RuntimeError("active lease expiry is unavailable")
        client_order_id = (
            f"shadow-{hash_json({'scope_id': scope_id, 'intent': str(order_intent_id)})[:48]}"
        )
        claim_values: dict[str, Any] = {
            "claim_id": str(envelope.command_id),
            "organization_id": request.scope.organization_id,
            "order_intent_id": str(order_intent_id),
            "scope_id": scope_id,
            "lease_id": str(lease.lease_id),
            "fencing_token": lease.fencing_token,
            "client_order_id": client_order_id,
            "owner_worker_id": lease.owner_worker_id,
            "worker_config_hash": lease.worker_config_hash,
            "credential_fingerprint": lease.credential_fingerprint,
            "capability_certificate_ref": certificate.certificate_id,
            "execution_mode": "SHADOW",
            "external_send_permitted": False,
            "live_gate_status": "DISABLED",
            "intent_snapshot_hash": intent.intent_snapshot_hash,
            "capability_certificate_hash": certificate.certificate_hash,
            "scope_hash": cast(str, lease_validation.scope_hash),
            "lease_hash": cast(str, lease_validation.lease_hash),
            "lease_expires_at": _iso(scope_state.lease_expires_at),
            "worker_observed_at": _iso(request.worker_observed_at),
            "claimed_at": _iso(now),
            "reason_code": request.reason_code,
        }
        claim = ShadowDispatchClaim(
            claim_id=envelope.command_id,
            organization_id=request.scope.organization_id,
            order_intent_id=order_intent_id,
            scope_id=scope_id,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            client_order_id=client_order_id,
            owner_worker_id=lease.owner_worker_id,
            worker_config_hash=lease.worker_config_hash,
            credential_fingerprint=lease.credential_fingerprint,
            capability_certificate_ref=certificate.certificate_id,
            execution_mode="SHADOW",
            external_send_permitted=False,
            live_gate_status="DISABLED",
            intent_snapshot_hash=intent.intent_snapshot_hash,
            capability_certificate_hash=certificate.certificate_hash,
            scope_hash=cast(str, lease_validation.scope_hash),
            lease_hash=cast(str, lease_validation.lease_hash),
            lease_expires_at=scope_state.lease_expires_at,
            worker_observed_at=request.worker_observed_at,
            claimed_at=now,
            reason_code=request.reason_code,
            claim_hash=hash_json(claim_values),
        )
        session.add(claim)
        session.flush()
        SHADOW_DISPATCH_CLAIMS.labels("COMPLETED").inc()
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="ShadowDispatchClaim",
            object_id=str(claim.claim_id),
            object_version=1,
            data={
                "claim_id": str(claim.claim_id),
                "order_intent_id": str(order_intent_id),
                "scope_id": scope_id,
                "lease_id": str(lease.lease_id),
                "fencing_token": lease.fencing_token,
                "client_order_id": client_order_id,
                "execution_mode": "SHADOW",
                "external_send_permitted": False,
                "live_gate_status": "DISABLED",
                "order_intent_status": intent_state.status,
            },
            events=(
                DomainEvent(
                    event_type="ShadowDispatchClaimRecorded",
                    aggregate_type="OrderIntent",
                    aggregate_id=str(order_intent_id),
                    payload={
                        "claim_id": str(claim.claim_id),
                        "scope_id": scope_id,
                        "lease_id": str(lease.lease_id),
                        "fencing_token": lease.fencing_token,
                        "client_order_id": client_order_id,
                        "external_send_permitted": False,
                    },
                ),
            ),
        )

    @staticmethod
    def _validate_intent_scope(
        intent: OrderIntent,
        scope: SenderScopeBinding,
        lease: ExecutionSenderLease,
        certificate_scope: CapabilityScope,
    ) -> None:
        if (
            intent.venue != scope.venue
            or intent.execution_domain != scope.execution_domain
            or intent.account_id != scope.account_id
            or intent.margin_mode != scope.margin_mode
            or intent.collateral_scope != scope.collateral_scope
            or intent.collateral_pool_id != scope.collateral_pool_id
            or intent.worker_id != lease.owner_worker_id
            or certificate_scope.venue != scope.venue
            or certificate_scope.execution_domain != scope.execution_domain
            or certificate_scope.account_id != scope.account_id
            or certificate_scope.account_abstraction != scope.account_abstraction
            or certificate_scope.position_mode != scope.position_mode
            or certificate_scope.margin_mode != scope.margin_mode
            or certificate_scope.collateral_scope != scope.collateral_scope
            or certificate_scope.collateral_pool_id != scope.collateral_pool_id
            or certificate_scope.worker_id != lease.owner_worker_id
            or certificate_scope.worker_config_hash != lease.worker_config_hash
            or certificate_scope.credential_fingerprint != lease.credential_fingerprint
            or certificate_scope.instrument_id != intent.instrument_id
            or certificate_scope.direction != intent.position_side
        ):
            raise CommandRejected(
                "SENDER_INTENT_SCOPE_MISMATCH", "intent, lease, and certificate scope disagree"
            )

    @staticmethod
    def _require_object(envelope: CommandEnvelope, command_type: str, object_type: str) -> None:
        if envelope.command_type != command_type:
            raise CommandRejected("COMMAND_TYPE_MISMATCH", "unexpected command type")
        if envelope.object_type != object_type or envelope.object_id is None:
            raise CommandRejected("OBJECT_BINDING_MISMATCH", f"{object_type} binding is required")

    @classmethod
    def _require_fencing_service(cls, envelope: CommandEnvelope, command_type: str) -> None:
        cls._require_object(envelope, command_type, "ExecutionSenderScope")
        if (
            envelope.channel not in {CommandChannel.INTERNAL, CommandChannel.SYSTEM}
            or envelope.service_principal != FENCING_SERVICE_PRINCIPAL
        ):
            raise CommandRejected(
                "FENCING_SERVICE_REQUIRED", "fencing service principal is required"
            )

    @staticmethod
    def _require_organization(envelope: CommandEnvelope, organization_id: str) -> None:
        if envelope.scope.get("organization_id") != organization_id:
            raise CommandRejected("SCOPE_MISMATCH", "organization scope changed")

    def _now(self, session: Session) -> datetime:
        if self._clock is not None:
            return self._clock()
        return cast(datetime, session.execute(select(func.clock_timestamp())).scalar_one())

    def _require_worker_clock(self, worker_observed_at: datetime, now: datetime) -> None:
        if abs(worker_observed_at - now) > self._max_clock_skew:
            raise CommandRejected(
                "WORKER_CLOCK_SKEW_EXCEEDED", "worker and fencing authority clocks disagree"
            )

    @staticmethod
    def _load_current_lease(
        session: Session, scope_id: str | None
    ) -> tuple[ExecutionSenderScopeState, ExecutionSenderLease]:
        if scope_id is None:  # pragma: no cover - object contract rejects above
            raise RuntimeError("sender scope identity is missing")
        state = session.execute(
            select(ExecutionSenderScopeState)
            .where(ExecutionSenderScopeState.scope_id == scope_id)
            .with_for_update()
        ).scalar_one_or_none()
        if state is None or state.active_lease_id is None:
            raise CommandRejected("SENDER_LEASE_INACTIVE", "sender scope has no active lease")
        lease = session.get(ExecutionSenderLease, state.active_lease_id)
        if lease is None:
            raise CommandRejected("SENDER_LEASE_NOT_FOUND", "sender lease is unavailable")
        return state, lease
