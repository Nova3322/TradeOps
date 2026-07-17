from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import CommandChannel, CommandEnvelope, CommandResult
from trading_control_plane.database import Database
from trading_control_plane.sender_fencing import (
    FENCING_SERVICE_PRINCIPAL,
    AcquireShadowSenderLeaseRequest,
    ClaimShadowOrderIntentRequest,
    RenewShadowSenderLeaseRequest,
    SenderFencingService,
    SenderLeaseAction,
    SenderScopeBinding,
    TightenSenderLeaseRequest,
    sender_scope_id,
)

WORKER_ID = "freqtrade-binance-account-1-isolated"
WORKER_CONFIG_HASH = "a" * 64
CREDENTIAL_FINGERPRINT = "b" * 64


def make_sender_scope(**updates: str) -> SenderScopeBinding:
    values = {
        "organization_id": "org-1",
        "venue": "BINANCE",
        "execution_domain": "BINANCE_USDM",
        "account_id": "account-1",
        "account_abstraction": "UNIFIED",
        "position_mode": "ONE_WAY",
        "margin_mode": "ISOLATED",
        "collateral_scope": "ACCOUNT",
        "collateral_pool_id": "pool-usdt-1",
    }
    values.update(updates)
    return SenderScopeBinding.model_validate(values)


def acquire_envelope(
    scope: SenderScopeBinding,
    *,
    now: datetime,
    lease_id: UUID | None = None,
    owner_worker_id: str = WORKER_ID,
    expected_version: int | None = None,
    idempotency_key: str | None = None,
    worker_observed_at: datetime | None = None,
    ttl_seconds: int = 30,
    max_lifetime_seconds: int = 120,
) -> CommandEnvelope:
    lease_identity = lease_id or uuid4()
    request = AcquireShadowSenderLeaseRequest(
        lease_id=lease_identity,
        scope=scope,
        owner_worker_id=owner_worker_id,
        worker_config_hash=WORKER_CONFIG_HASH,
        credential_fingerprint=CREDENTIAL_FINGERPRINT,
        lease_ttl_seconds=ttl_seconds,
        max_lease_lifetime_seconds=max_lifetime_seconds,
        worker_observed_at=worker_observed_at or now,
        reconciliation_evidence_ref=f"test-only:reconciliation:{lease_identity}",
        risk_state_ack_ref=f"test-only:risk-state:{lease_identity}",
        reason_code="TEST_SHADOW_SENDER_ACQUIRE",
    )
    return CommandEnvelope(
        idempotency_key=idempotency_key or f"sender-acquire-{uuid4()}",
        command_type=SenderFencingService.acquire_command_type,
        object_type="ExecutionSenderScope",
        object_id=sender_scope_id(scope),
        expected_version=expected_version,
        service_principal=FENCING_SERVICE_PRINCIPAL,
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": scope.organization_id},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref="test-only:sender-fencing-service",
        payload_schema_version=1,
        reason="acquire non-dispatchable shadow sender lease",
        payload=request.model_dump(mode="json"),
    )


def renew_envelope(
    scope: SenderScopeBinding,
    lease_id: UUID,
    fencing_token: int,
    *,
    owner_worker_id: str,
    now: datetime,
    expected_version: int,
    worker_observed_at: datetime | None = None,
    ttl_seconds: int = 30,
) -> CommandEnvelope:
    request = RenewShadowSenderLeaseRequest(
        lease_id=lease_id,
        fencing_token=fencing_token,
        lease_ttl_seconds=ttl_seconds,
        worker_observed_at=worker_observed_at or now,
        renewal_evidence_ref=f"test-only:renewal:{uuid4()}",
        reason_code="TEST_SHADOW_SENDER_RENEW",
    )
    return CommandEnvelope(
        idempotency_key=f"sender-renew-{uuid4()}",
        command_type=SenderFencingService.renew_command_type,
        object_type="ExecutionSenderScope",
        object_id=sender_scope_id(scope),
        expected_version=expected_version,
        service_principal=owner_worker_id,
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": scope.organization_id},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref="test-only:sender-owner",
        payload_schema_version=1,
        reason="renew non-dispatchable shadow sender lease",
        payload=request.model_dump(mode="json"),
    )


def tighten_envelope(
    scope: SenderScopeBinding,
    *,
    action: SenderLeaseAction,
    now: datetime,
    expected_version: int,
    lease_id: UUID | None = None,
    fencing_token: int | None = None,
    owner_worker_id: str = WORKER_ID,
) -> CommandEnvelope:
    request = TightenSenderLeaseRequest(
        action=action,
        lease_id=lease_id,
        fencing_token=fencing_token,
        reason_code=f"TEST_SENDER_{action.value}",
        source_ref=f"test-only:sender-tighten:{uuid4()}",
    )
    service_principal = (
        owner_worker_id if action is SenderLeaseAction.RELEASE else FENCING_SERVICE_PRINCIPAL
    )
    return CommandEnvelope(
        idempotency_key=f"sender-tighten-{uuid4()}",
        command_type=SenderFencingService.tighten_command_type,
        object_type="ExecutionSenderScope",
        object_id=sender_scope_id(scope),
        expected_version=expected_version,
        service_principal=service_principal,
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": scope.organization_id},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref="test-only:sender-tighten",
        payload_schema_version=1,
        reason="tighten shadow sender lease",
        payload=request.model_dump(mode="json"),
    )


def claim_envelope(
    scope: SenderScopeBinding,
    order_intent_id: UUID,
    lease_id: UUID,
    fencing_token: int,
    reconciliation_run_id: UUID,
    *,
    now: datetime,
    owner_worker_id: str = WORKER_ID,
    idempotency_key: str | None = None,
) -> CommandEnvelope:
    request = ClaimShadowOrderIntentRequest(
        scope=scope,
        lease_id=lease_id,
        fencing_token=fencing_token,
        reconciliation_run_id=reconciliation_run_id,
        worker_observed_at=now,
        reason_code="TEST_SHADOW_DISPATCH_CLAIM",
    )
    return CommandEnvelope(
        idempotency_key=idempotency_key or f"shadow-dispatch-claim-{uuid4()}",
        command_type=SenderFencingService.claim_command_type,
        object_type="OrderIntent",
        object_id=str(order_intent_id),
        expected_version=1,
        service_principal=owner_worker_id,
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": scope.organization_id},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref="test-only:shadow-worker-identity",
        payload_schema_version=1,
        reason="claim shadow intent without external dispatch",
        payload=request.model_dump(mode="json"),
    )


def execute_acquire(
    database: Database, envelope: CommandEnvelope, *, now: datetime
) -> CommandResult:
    service = SenderFencingService(clock=lambda: now)
    return IdempotentCommandExecutor(database.session_factory).execute(
        envelope, service.acquire_shadow
    )


def execute_renew(database: Database, envelope: CommandEnvelope, *, now: datetime) -> CommandResult:
    service = SenderFencingService(clock=lambda: now)
    return IdempotentCommandExecutor(database.session_factory).execute(
        envelope, service.renew_shadow
    )


def execute_tighten(
    database: Database, envelope: CommandEnvelope, *, now: datetime
) -> CommandResult:
    service = SenderFencingService(clock=lambda: now)
    return IdempotentCommandExecutor(database.session_factory).execute(envelope, service.tighten)


def execute_claim(database: Database, envelope: CommandEnvelope, *, now: datetime) -> CommandResult:
    service = SenderFencingService(clock=lambda: now)
    return IdempotentCommandExecutor(database.session_factory).execute(
        envelope, service.claim_shadow_order_intent
    )
