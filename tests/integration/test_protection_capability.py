from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.instrument_catalog_fixtures import (
    instrument_catalog_request_for_risk,
    register_instrument_catalog,
)
from tests.protection_capability_fixtures import (
    protection_capability_envelope,
    protection_capability_request_for_risk,
    register_protection_capability,
)
from tests.risk_fixtures import make_request
from trading_control_plane.commands import CommandStatus
from trading_control_plane.database import Database
from trading_control_plane.instrument_catalog import InstrumentCatalogValidator
from trading_control_plane.models import AuditEvent, OutboxMessage
from trading_control_plane.protection_capability import ProtectionCapabilityValidator
from trading_control_plane.protection_capability_models import (
    InstrumentProtectionCapabilityRecord,
)
from trading_control_plane.risk import (
    instrument_classification_validation_request,
    protection_capability_validation_request,
)

pytestmark = pytest.mark.integration


def _database_record(request, *, created_at: datetime, **updates):
    values = {
        "protection_capability_record_id": request.protection_capability_record_id,
        "organization_id": request.organization_id,
        "catalog_record_id": request.catalog_record_id,
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
        "credential_permission_profile_version": (request.credential_permission_profile_version),
        "venue_client_version": request.venue_client_version,
        "capability_status": request.capability_status.value,
        "native_protection_supported": request.native_protection_supported,
        "conditional_orders_supported": request.conditional_orders_supported,
        "reduce_only_supported": request.reduce_only_supported,
        "partial_fill_protection_supported": request.partial_fill_protection_supported,
        "protection_replacement_supported": request.protection_replacement_supported,
        "protection_confirmation_window_ms": request.protection_confirmation_window_ms,
        "supported_trigger_price_types": list(request.supported_trigger_price_types),
        "supported_protection_order_types": list(request.supported_protection_order_types),
        "environment": request.environment.value,
        "real_funds_eligible": request.real_funds_eligible,
        "valid_from": request.valid_from,
        "valid_until": request.valid_until,
        "source_observed_at": request.source_observed_at,
        "record_hash": request.record_hash,
        "evidence_refs": list(request.evidence_refs),
        "evidence_hash": request.evidence_hash,
        "source_ref": request.source_ref,
        "created_at": created_at,
    }
    values.update(updates)
    return InstrumentProtectionCapabilityRecord(**values)


def _requests(now: datetime):
    risk_request = make_request(now=now)
    catalog_request = instrument_catalog_request_for_risk(risk_request, now=now)
    capability_request = protection_capability_request_for_risk(
        risk_request,
        catalog_request,
        now=now,
    )
    return risk_request, catalog_request, capability_request


def test_register_validate_and_audit_exact_shadow_protection_capability(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    risk_request, catalog_request, request = _requests(now)
    assert (
        register_instrument_catalog(database, catalog_request, now=now).status
        is CommandStatus.COMPLETED
    )
    envelope = protection_capability_envelope(
        request,
        now=now,
        idempotency_key="protection-capability-stable-key",
    )

    denied = register_protection_capability(
        database,
        request,
        now=now,
        envelope=protection_capability_envelope(
            request,
            now=now,
            service_principal="some-other-service",
        ),
    )
    invalid_hash = request.model_copy(update={"record_hash": "0" * 64})
    rejected = register_protection_capability(database, invalid_hash, now=now)
    original = register_protection_capability(
        database,
        request,
        now=now,
        envelope=envelope,
    )
    replay = register_protection_capability(
        database,
        request,
        now=now,
        envelope=envelope.model_copy(update={"command_id": uuid4(), "correlation_id": uuid4()}),
    )
    duplicate_request = protection_capability_request_for_risk(
        risk_request,
        catalog_request,
        now=now,
        protection_capability_record_id=uuid4(),
    )
    duplicate = register_protection_capability(database, duplicate_request, now=now)

    with database.session_factory.begin() as session:
        classification = InstrumentCatalogValidator().validate(
            session,
            instrument_classification_validation_request(risk_request, now),
        )
        validation = ProtectionCapabilityValidator().validate(
            session,
            protection_capability_validation_request(
                risk_request,
                classification,
                now,
            ),
        )
        record = session.get(
            InstrumentProtectionCapabilityRecord,
            request.protection_capability_record_id,
        )
        assert record is not None
        assert record.environment == "SHADOW"
        assert record.real_funds_eligible is False
        assert (
            session.scalar(select(func.count()).select_from(InstrumentProtectionCapabilityRecord))
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "InstrumentProtectionCapabilityRegistered")
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.event_type == "InstrumentProtectionCapabilityRegistered")
            )
            == 1
        )

    assert denied.status is CommandStatus.REJECTED
    assert denied.error_code == "VENUE_CAPABILITY_CATALOG_SERVICE_REQUIRED"
    assert rejected.status is CommandStatus.REJECTED
    assert rejected.error_code == "PROTECTION_CAPABILITY_RECORD_INVALID"
    assert original.status is CommandStatus.COMPLETED
    assert original.data["record_hash"] == request.record_hash
    assert replay.status is CommandStatus.ALREADY_PROCESSED
    assert duplicate.status is CommandStatus.REJECTED
    assert duplicate.error_code == "PROTECTION_CAPABILITY_VERSION_EXISTS"
    assert validation.valid is True
    assert validation.reason_codes == ()
    assert validation.protection_capability_record_id == (request.protection_capability_record_id)
    assert validation.record_hash == request.record_hash
    assert validation.evidence_hash == request.evidence_hash
    assert validation.valid_until == request.valid_until


@pytest.mark.parametrize(
    ("updates", "reason_code"),
    [
        ({"native_protection_supported": False}, "NATIVE_PROTECTION_UNSUPPORTED"),
        ({"conditional_orders_supported": False}, "CONDITIONAL_ORDER_UNSUPPORTED"),
        ({"reduce_only_supported": False}, "REDUCE_ONLY_PROTECTION_UNSUPPORTED"),
        (
            {"partial_fill_protection_supported": False},
            "PARTIAL_FILL_PROTECTION_UNSUPPORTED",
        ),
        (
            {"protection_replacement_supported": False},
            "PROTECTION_REPLACEMENT_UNSUPPORTED",
        ),
        ({"capability_status": "VALIDATING"}, "PROTECTION_CAPABILITY_NOT_CERTIFIED"),
        ({"adapter_version": "other-adapter-v1"}, "PROTECTION_CAPABILITY_SCOPE_MISMATCH"),
        ({"credential_fingerprint": "c" * 64}, "PROTECTION_CAPABILITY_SCOPE_MISMATCH"),
    ],
)
def test_validation_fails_closed_for_unsupported_or_mismatched_capability(
    database: Database,
    updates: dict[str, object],
    reason_code: str,
) -> None:
    now = datetime.now(UTC)
    risk_request, catalog_request, _ = _requests(now)
    assert (
        register_instrument_catalog(database, catalog_request, now=now).status
        is CommandStatus.COMPLETED
    )
    request = protection_capability_request_for_risk(
        risk_request,
        catalog_request,
        now=now,
        **updates,
    )
    assert (
        register_protection_capability(database, request, now=now).status is CommandStatus.COMPLETED
    )
    with database.session_factory.begin() as session:
        classification = InstrumentCatalogValidator().validate(
            session,
            instrument_classification_validation_request(risk_request, now),
        )
        validation = ProtectionCapabilityValidator().validate(
            session,
            protection_capability_validation_request(
                risk_request,
                classification,
                now,
            ),
        )

    assert validation.valid is False
    assert reason_code in validation.reason_codes
    assert validation.valid_until == now


def test_validation_fails_closed_for_missing_expired_and_bad_hash(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    risk_request, catalog_request, _ = _requests(now)
    assert (
        register_instrument_catalog(database, catalog_request, now=now).status
        is CommandStatus.COMPLETED
    )
    with database.session_factory.begin() as session:
        classification = InstrumentCatalogValidator().validate(
            session,
            instrument_classification_validation_request(risk_request, now),
        )
        validation_request = protection_capability_validation_request(
            risk_request,
            classification,
            now,
        )
        missing = ProtectionCapabilityValidator().validate(
            session,
            validation_request,
        )
    assert missing.valid is False
    assert missing.reason_codes == ("PROTECTION_CAPABILITY_RECORD_NOT_FOUND",)

    expired = protection_capability_request_for_risk(
        risk_request,
        catalog_request,
        now=now,
        valid_from=now - timedelta(hours=2),
        valid_until=now - timedelta(hours=1),
        source_observed_at=now - timedelta(hours=3),
    )
    with database.session_factory.begin() as session:
        session.add(_database_record(expired, created_at=now, record_hash="0" * 64))
    with database.session_factory.begin() as session:
        invalid = ProtectionCapabilityValidator().validate(
            session,
            validation_request,
        )
    assert invalid.valid is False
    assert invalid.reason_codes == ("PROTECTION_CAPABILITY_INTEGRITY_FAILED",)
    assert invalid.valid_until == now


def test_database_enforces_shadow_only_immutability_and_catalog_binding(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    _, catalog_request, request = _requests(now)
    assert (
        register_instrument_catalog(database, catalog_request, now=now).status
        is CommandStatus.COMPLETED
    )
    assert (
        register_protection_capability(database, request, now=now).status is CommandStatus.COMPLETED
    )

    with pytest.raises(DBAPIError, match="immutable"):
        with database.session_factory.begin() as session:
            session.execute(
                update(InstrumentProtectionCapabilityRecord)
                .where(
                    InstrumentProtectionCapabilityRecord.protection_capability_record_id
                    == request.protection_capability_record_id
                )
                .values(capability_status="EXPIRED")
            )

    real_funds = request.model_copy(
        update={
            "protection_capability_record_id": uuid4(),
            "position_management_template_version": "position-template-test-v2",
        }
    )
    with pytest.raises(IntegrityError, match="shadow_only"):
        with database.session_factory.begin() as session:
            session.add(
                _database_record(
                    real_funds,
                    created_at=now,
                    real_funds_eligible=True,
                )
            )

    wrong_catalog = request.model_copy(
        update={
            "protection_capability_record_id": uuid4(),
            "position_management_template_version": "position-template-test-v3",
            "catalog_record_id": uuid4(),
        }
    )
    with pytest.raises(IntegrityError, match="catalog"):
        with database.session_factory.begin() as session:
            session.add(_database_record(wrong_catalog, created_at=now))
