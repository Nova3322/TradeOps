from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from tests.capability_fixtures import (
    issue_shadow_certificate,
    issue_shadow_certificate_for_proposal,
    proposal_scope_and_versions,
)
from tests.integration.test_review import seed_proposal
from tests.integration.test_trading_authorization import (
    execute_issue,
    issue_envelope,
    prepare_approved,
)
from trading_control_plane.capability_certificate_models import (
    CapabilityCertificate,
    CapabilityCertificateState,
    CapabilityCertificateStateHistory,
    CapabilityEvidenceBundle,
)
from trading_control_plane.capability_certificates import (
    CERTIFICATION_SERVICE_PRINCIPAL,
    CapabilityCertificateAction,
    CapabilityCertificateService,
    CapabilityCertificateValidator,
    CapabilityValidationRequest,
)
from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import CommandChannel, CommandEnvelope, CommandStatus
from trading_control_plane.database import Database
from trading_control_plane.models import AuditEvent, CapabilityGate, OutboxMessage
from trading_control_plane.trading_authorization_models import (
    AddAuthorizationPackageState,
    AddUnitState,
    InitialAuthorizationState,
    TradingAuthorization,
)

pytestmark = pytest.mark.integration


def count_rows(session: Session, model: type[object]) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def tighten_envelope(
    certificate_id: str,
    *,
    action: CapabilityCertificateAction = CapabilityCertificateAction.SUSPEND,
    expected_version: int = 1,
    now: datetime | None = None,
) -> CommandEnvelope:
    issued_at = now or datetime.now(UTC)
    return CommandEnvelope(
        idempotency_key=f"tighten-certificate-{uuid4()}",
        command_type=CapabilityCertificateService.tighten_command_type,
        object_type="CapabilityCertificate",
        object_id=certificate_id,
        expected_version=expected_version,
        service_principal=CERTIFICATION_SERVICE_PRINCIPAL,
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": "org-1"},
        correlation_id=uuid4(),
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=2),
        auth_context_ref="test-only:certification-tightening",
        payload_schema_version=1,
        reason="tighten test-only shadow certificate",
        payload={
            "action": action.value,
            "reason_code": f"TEST_{action.value}",
            "source_ref": f"test-only:drift:{action.value.lower()}",
        },
    )


def test_shadow_issue_persists_immutable_exact_scope_evidence_and_closed_live_gates(
    database: Database,
) -> None:
    proposal = seed_proposal(database)
    result = issue_shadow_certificate_for_proposal(database, proposal)

    assert result.status is CommandStatus.COMPLETED
    assert result.data["environment"] == "SHADOW"
    assert result.data["real_funds_eligible"] is False
    certificate_id, scope, versions = proposal_scope_and_versions(proposal)
    with database.session_factory.begin() as session:
        certificate = session.get(CapabilityCertificate, certificate_id)
        assert certificate is not None
        assert certificate.real_funds_eligible is False
        assert certificate.scope == scope.model_dump(mode="json")
        assert certificate.policy_versions == versions.model_dump(mode="json")
        assert count_rows(session, CapabilityEvidenceBundle) == 1
        assert count_rows(session, CapabilityCertificateStateHistory) == 1
        gates = tuple(
            session.execute(
                select(CapabilityGate).where(
                    CapabilityGate.capability_key.in_(
                        ("LIVE_ORDER_SEND", "CAPITAL_TRANSFER", "AUTO_ADD")
                    )
                )
            ).scalars()
        )
        assert {gate.status for gate in gates} == {"DISABLED"}
        validation = CapabilityCertificateValidator().validate(
            session,
            CapabilityValidationRequest(
                organization_id="org-1",
                certificate_id=certificate_id,
                expected_scope=scope,
                expected_policy_versions=versions,
                requested_order_notional=Decimal("1000"),
                requested_trade_loss=Decimal("500"),
                validation_time=certificate.valid_from,
            ),
        )
        assert validation.valid is True
        assert validation.status == "ACTIVE"
        assert count_rows(session, AuditEvent) == 1
        assert count_rows(session, OutboxMessage) == 1

    with pytest.raises(DBAPIError, match="capability_certificates is immutable"):
        with database.session_factory.begin() as session:
            session.execute(
                update(CapabilityCertificate)
                .where(CapabilityCertificate.certificate_id == certificate_id)
                .values(subject_ref="tampered")
            )
    with pytest.raises(DBAPIError, match="capability_evidence_bundles is immutable"):
        with database.session_factory.begin() as session:
            session.execute(update(CapabilityEvidenceBundle).values(evidence_hash="0" * 64))


def test_validator_rejects_scope_version_limit_expiry_and_missing_certificate(
    database: Database,
) -> None:
    proposal = seed_proposal(database)
    now = datetime.now(UTC)
    certificate_id, scope, versions = proposal_scope_and_versions(proposal)
    issued = issue_shadow_certificate(
        database,
        organization_id="org-1",
        certificate_id=certificate_id,
        scope=scope,
        policy_versions=versions,
        now=now,
        expires_at=now + timedelta(minutes=10),
        max_order_notional=Decimal("1000"),
        max_trade_loss=Decimal("500"),
    )
    assert issued.status is CommandStatus.COMPLETED
    validator = CapabilityCertificateValidator()
    baseline = CapabilityValidationRequest(
        organization_id="org-1",
        certificate_id=certificate_id,
        expected_scope=scope,
        expected_policy_versions=versions,
        requested_order_notional=Decimal("1000"),
        requested_trade_loss=Decimal("500"),
        validation_time=now,
    )

    with database.session_factory.begin() as session:
        scope_mismatch = validator.validate(
            session,
            baseline.model_copy(
                update={"expected_scope": scope.model_copy(update={"account_id": "other"})}
            ),
        )
        version_mismatch = validator.validate(
            session,
            baseline.model_copy(
                update={
                    "expected_policy_versions": versions.model_copy(
                        update={"adapter_version": "other"}
                    )
                }
            ),
        )
        notional_exceeded = validator.validate(
            session,
            baseline.model_copy(update={"requested_order_notional": Decimal("1000.01")}),
        )
        loss_exceeded = validator.validate(
            session,
            baseline.model_copy(update={"requested_trade_loss": Decimal("500.01")}),
        )
        expired = validator.validate(
            session,
            baseline.model_copy(update={"validation_time": now + timedelta(minutes=10)}),
        )
        missing = validator.validate(
            session,
            baseline.model_copy(update={"certificate_id": "capability:missing-test"}),
        )

    assert scope_mismatch.reason_codes == ("CAPABILITY_CERTIFICATE_SCOPE_MISMATCH",)
    assert version_mismatch.reason_codes == ("CAPABILITY_CERTIFICATE_VERSION_MISMATCH",)
    assert notional_exceeded.reason_codes == ("CAPABILITY_CERTIFICATE_NOTIONAL_LIMIT_EXCEEDED",)
    assert loss_exceeded.reason_codes == ("CAPABILITY_CERTIFICATE_LOSS_LIMIT_EXCEEDED",)
    assert expired.reason_codes == ("CAPABILITY_CERTIFICATE_OUTSIDE_VALID_WINDOW",)
    assert missing.reason_codes == ("CAPABILITY_CERTIFICATE_NOT_FOUND",)


def test_suspension_atomically_invalidates_initial_and_add_authorization_capacity(
    database: Database,
) -> None:
    proposal, decision = prepare_approved(
        database,
        auto_add_enabled=True,
        requested_add_count=1,
    )
    issued = execute_issue(
        database,
        issue_envelope(
            proposal.proposal_version_id,
            proposal.version,
            proposal.spec_hash,
            proposal.risk_summary_hash,
            decision.approval_decision_id,
        ),
    )
    assert issued.status is CommandStatus.COMPLETED
    authorization_id = UUID(str(issued.data["authorization_id"]))
    certificate_id = str(proposal.spec["capability_certificate_ref"])

    result = IdempotentCommandExecutor(database.session_factory).execute(
        tighten_envelope(certificate_id),
        CapabilityCertificateService().tighten,
    )

    assert result.status is CommandStatus.COMPLETED
    assert result.data["status"] == "SUSPENDED"
    assert result.data["invalidated_authorizations"] == 1
    assert result.data["invalidated_initial_authorizations"] == 1
    assert result.data["invalidated_add_packages"] == 1
    assert result.data["invalidated_add_units"] == 1
    with database.session_factory.begin() as session:
        root = session.get(TradingAuthorization, authorization_id)
        assert root is not None
        assert session.execute(select(InitialAuthorizationState)).scalar_one().status == (
            "INVALIDATED"
        )
        assert session.execute(select(AddAuthorizationPackageState)).scalar_one().status == (
            "INVALIDATED"
        )
        assert session.execute(select(AddUnitState)).scalar_one().status == "INVALIDATED"
        state = session.get(CapabilityCertificateState, certificate_id)
        assert state is not None
        assert state.status == "SUSPENDED"
        assert state.version == 2
        assert count_rows(session, CapabilityCertificateStateHistory) == 2

    _old_id, scope, versions = proposal_scope_and_versions(proposal)
    recertified = issue_shadow_certificate(
        database,
        organization_id="org-1",
        certificate_id="capability:test-shadow-recertified",
        scope=scope,
        policy_versions=versions,
        supersedes=certificate_id,
    )
    assert recertified.status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        assert session.execute(select(InitialAuthorizationState)).scalar_one().status == (
            "INVALIDATED"
        )
        old_state = session.get(CapabilityCertificateState, certificate_id)
        new_state = session.get(CapabilityCertificateState, "capability:test-shadow-recertified")
        assert old_state is not None and old_state.status == "SUSPENDED"
        assert new_state is not None and new_state.status == "ACTIVE"

    with pytest.raises(
        DBAPIError,
        match="order intent capability certificate disagrees with authorization",
    ):
        with database.session_factory.begin() as session:
            session.execute(
                text(
                    """
                    INSERT INTO order_intents (
                        order_intent_id, authorization_id, capability_certificate_ref
                    ) VALUES (
                        :order_intent_id, :authorization_id, :certificate_id
                    )
                    """
                ),
                {
                    "order_intent_id": uuid4(),
                    "authorization_id": authorization_id,
                    "certificate_id": "capability:test-shadow-recertified",
                },
            )

    with pytest.raises(DBAPIError, match="invalid capability certificate transition"):
        with database.session_factory.begin() as session:
            session.execute(
                update(CapabilityCertificateState)
                .where(CapabilityCertificateState.certificate_id == certificate_id)
                .values(status="ACTIVE", version=3)
            )


def test_expiry_transition_is_time_bound_and_never_opens_real_funds(
    database: Database,
) -> None:
    proposal = seed_proposal(database)
    certificate_id, scope, versions = proposal_scope_and_versions(proposal)
    now = datetime.now(UTC)
    issued = issue_shadow_certificate(
        database,
        organization_id="org-1",
        certificate_id=certificate_id,
        scope=scope,
        policy_versions=versions,
        now=now,
        expires_at=now + timedelta(minutes=5),
    )
    assert issued.status is CommandStatus.COMPLETED

    early = IdempotentCommandExecutor(database.session_factory).execute(
        tighten_envelope(
            certificate_id,
            action=CapabilityCertificateAction.EXPIRE,
            now=now,
        ),
        CapabilityCertificateService(clock=lambda: now).tighten,
    )
    late_time = now + timedelta(minutes=5)
    expired = IdempotentCommandExecutor(database.session_factory).execute(
        tighten_envelope(
            certificate_id,
            action=CapabilityCertificateAction.EXPIRE,
            now=late_time,
        ),
        CapabilityCertificateService(clock=lambda: late_time).tighten,
    )

    assert early.status is CommandStatus.REJECTED
    assert early.error_code == "CAPABILITY_CERTIFICATE_NOT_EXPIRED"
    assert expired.status is CommandStatus.COMPLETED
    assert expired.data["status"] == "EXPIRED"
    assert expired.data["real_funds_eligible"] is False
    with database.session_factory.begin() as session:
        state = session.get(CapabilityCertificateState, certificate_id)
        assert state is not None and state.status == "EXPIRED"
        assert count_rows(session, CapabilityCertificateStateHistory) == 2


def test_concurrent_shadow_issuance_serializes_one_certificate_and_evidence_bundle(
    database: Database,
) -> None:
    proposal = seed_proposal(database)
    certificate_id, scope, versions = proposal_scope_and_versions(proposal)
    now = datetime.now(UTC)

    def issue() -> object:
        return issue_shadow_certificate(
            database,
            organization_id="org-1",
            certificate_id=certificate_id,
            scope=scope,
            policy_versions=versions,
            now=now,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _item: issue(), range(2)))

    assert sorted(result.status.value for result in results) == ["COMPLETED", "REJECTED"]
    assert {result.error_code for result in results if result.status is CommandStatus.REJECTED} == {
        "CAPABILITY_CERTIFICATE_ALREADY_EXISTS"
    }
    with database.session_factory.begin() as session:
        assert count_rows(session, CapabilityCertificate) == 1
        assert count_rows(session, CapabilityEvidenceBundle) == 1
        assert count_rows(session, CapabilityCertificateStateHistory) == 1
