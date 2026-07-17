from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from trading_control_plane.capability_certificates import (
    CERTIFICATION_SERVICE_PRINCIPAL,
    CapabilityCertificateService,
    CapabilityCertificateType,
    CapabilityPolicyVersions,
    CapabilityScope,
    IssueShadowCertificateRequest,
)
from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import CommandChannel, CommandEnvelope, CommandResult
from trading_control_plane.database import Database
from trading_control_plane.proposal_models import FrozenProposalVersion
from trading_control_plane.risk import RiskPrecheckRequest, capability_validation_request
from trading_control_plane.trading_authorization import FrozenAuthorizationBinding


def issue_shadow_certificate(
    database: Database,
    *,
    organization_id: str,
    certificate_id: str,
    scope: CapabilityScope,
    policy_versions: CapabilityPolicyVersions,
    now: datetime | None = None,
    expires_at: datetime | None = None,
    max_order_notional: Decimal = Decimal("1000000000"),
    max_trade_loss: Decimal = Decimal("1000000000"),
    supersedes: str | None = None,
) -> CommandResult:
    issued_at = now or datetime.now(UTC)
    expiry = expires_at or issued_at + timedelta(days=1)
    request = IssueShadowCertificateRequest(
        certificate_id=certificate_id,
        certificate_type=CapabilityCertificateType.EXECUTION,
        subject_ref=f"test-only:subject:{certificate_id}",
        scope=scope,
        policy_versions=policy_versions,
        evidence_bundle_version=f"test-only:bundle:{certificate_id}",
        evidence_refs=(
            f"test-only:engineering:{certificate_id}",
            f"test-only:shadow-controls:{certificate_id}",
        ),
        evidence_summary={
            "fixture": True,
            "production_evidence": False,
            "dispatch_enabled": False,
        },
        max_order_notional=max_order_notional,
        max_trade_loss=max_trade_loss,
        owner_principal="principal:test-capability-owner",
        issuer_principal="principal:test-certificate-issuer",
        approver_principal_ids=("principal:test-independent-approver",),
        approval_ref=f"test-only:approval:{certificate_id}",
        monitoring_ref=f"test-only:monitoring:{certificate_id}",
        exit_recovery_ref=f"test-only:recovery:{certificate_id}",
        invalidation_conditions=(
            "ACCOUNT_OR_SCOPE_DRIFT",
            "EVIDENCE_OR_VERSION_EXPIRED",
            "EXECUTION_INCIDENT",
        ),
        supersedes=supersedes,
        valid_from=issued_at,
        expires_at=expiry,
    )
    envelope = CommandEnvelope(
        idempotency_key=f"issue-shadow-certificate-{uuid4()}",
        command_type=CapabilityCertificateService.issue_command_type,
        object_type="CapabilityCertificate",
        object_id=certificate_id,
        expected_version=None,
        service_principal=CERTIFICATION_SERVICE_PRINCIPAL,
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": organization_id},
        correlation_id=uuid4(),
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=2),
        auth_context_ref="test-only:certification-service",
        payload_schema_version=1,
        reason="issue test-only non-dispatchable shadow certificate",
        payload=request.model_dump(mode="json"),
    )
    service = CapabilityCertificateService(clock=lambda: issued_at)
    return IdempotentCommandExecutor(database.session_factory).execute(
        envelope, service.issue_shadow
    )


def proposal_scope_and_versions(
    proposal: FrozenProposalVersion,
) -> tuple[str, CapabilityScope, CapabilityPolicyVersions]:
    binding = FrozenAuthorizationBinding.model_validate(proposal.spec)
    return (
        binding.capability_certificate_ref,
        CapabilityScope(
            proposal_source=proposal.source,
            strategy_id=binding.strategy_id,
            strategy_version=binding.strategy_version,
            venue=proposal.venue,
            execution_domain=proposal.execution_domain,
            account_id=proposal.account_id,
            account_abstraction=binding.account_abstraction,
            position_mode=binding.position_mode,
            margin_mode=binding.margin_mode,
            collateral_scope=binding.collateral_scope,
            collateral_pool_id=binding.collateral_pool_id,
            instrument_id=proposal.instrument_id,
            contract_multiplier=binding.contract_multiplier,
            underlying_id=binding.underlying_id,
            sector_id=proposal.sector,
            risk_cluster_id=binding.risk_cluster_id,
            direction=proposal.direction,
            risk_tier=proposal.risk_tier,
            max_add_count=proposal.requested_add_count,
            settlement_asset=binding.settlement_asset,
            worker_id=binding.worker_id,
            worker_config_hash=binding.worker_config_hash,
            credential_fingerprint=binding.credential_fingerprint,
            capital_transfer_capability="NOT_APPLICABLE",
        ),
        CapabilityPolicyVersions(
            strategy_parameter_version=binding.strategy_parameter_version,
            risk_policy_version=proposal.risk_policy_version,
            authorization_policy_version=binding.authorization_policy_version,
            catalog_version=proposal.catalog_version,
            execution_capability_version=proposal.execution_capability_version,
            adapter_version=binding.adapter_version,
            freqtrade_worker_version=binding.freqtrade_worker_version,
            account_capability_version=binding.account_capability_version,
            credential_permission_profile_version=(binding.credential_permission_profile_version),
            venue_client_version=binding.venue_client_version,
            instrument_scope_version=binding.instrument_scope_version,
            position_management_template_version=(binding.position_management_template_version),
            add_milestone_policy_version=binding.add_milestone_policy_version,
        ),
    )


def issue_shadow_certificate_for_proposal(
    database: Database,
    proposal: FrozenProposalVersion,
) -> CommandResult:
    certificate_id, scope, versions = proposal_scope_and_versions(proposal)
    now = datetime.now(UTC)
    return issue_shadow_certificate(
        database,
        organization_id=proposal.organization_id,
        certificate_id=certificate_id,
        scope=scope,
        policy_versions=versions,
        now=now,
        expires_at=max(proposal.valid_until, now + timedelta(hours=1)),
    )


def issue_shadow_certificate_for_risk_request(
    database: Database,
    request: RiskPrecheckRequest,
    *,
    now: datetime | None = None,
) -> CommandResult:
    validation_time = now or datetime.now(UTC)
    expected = capability_validation_request(request, validation_time)
    return issue_shadow_certificate(
        database,
        organization_id=request.organization_id,
        certificate_id=expected.certificate_id,
        scope=expected.expected_scope,
        policy_versions=expected.expected_policy_versions,
        now=validation_time,
        expires_at=validation_time + timedelta(days=1),
    )
