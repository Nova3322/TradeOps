from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from tests.instrument_catalog_fixtures import (
    instrument_catalog_envelope,
    instrument_catalog_request_for_risk,
    register_instrument_catalog,
)
from tests.risk_fixtures import make_request
from trading_control_plane.commands import CommandStatus
from trading_control_plane.database import Database
from trading_control_plane.instrument_catalog import (
    InstrumentCatalogValidator,
    InstrumentSector,
)
from trading_control_plane.instrument_catalog_models import InstrumentCatalogRecord
from trading_control_plane.models import AuditEvent, OutboxMessage
from trading_control_plane.risk import instrument_classification_validation_request

pytestmark = pytest.mark.integration


def _database_record(
    request,
    *,
    created_at: datetime,
    **updates,
) -> InstrumentCatalogRecord:
    values = {
        "catalog_record_id": request.catalog_record_id,
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
        "contract_multiplier": request.contract_multiplier,
        "tick_size": request.tick_size,
        "lot_size": request.lot_size,
        "minimum_quantity": request.minimum_quantity,
        "minimum_notional": request.minimum_notional,
        "discoverable": request.discoverable,
        "classification_complete": request.classification_complete,
        "approval_scope": request.approval_scope.value,
        "listing_status": request.listing_status.value,
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
    return InstrumentCatalogRecord(**values)


def test_register_validate_and_audit_exact_instrument_classification(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    risk_request = make_request(now=now)
    request = instrument_catalog_request_for_risk(risk_request, now=now)
    envelope = instrument_catalog_envelope(
        request,
        now=now,
        idempotency_key="instrument-catalog-stable-key",
    )

    invalid_hash_request = request.model_copy(update={"record_hash": "0" * 64})
    denied = register_instrument_catalog(
        database,
        request,
        now=now,
        envelope=instrument_catalog_envelope(
            request,
            now=now,
            service_principal="some-other-service",
        ),
    )
    rejected = register_instrument_catalog(
        database,
        invalid_hash_request,
        now=now,
        envelope=instrument_catalog_envelope(invalid_hash_request, now=now),
    )

    original = register_instrument_catalog(database, request, now=now, envelope=envelope)
    replay = register_instrument_catalog(
        database,
        request,
        now=now,
        envelope=envelope.model_copy(update={"command_id": uuid4(), "correlation_id": uuid4()}),
    )
    duplicate_request = instrument_catalog_request_for_risk(
        risk_request,
        now=now,
        catalog_record_id=uuid4(),
    )
    duplicate = register_instrument_catalog(database, duplicate_request, now=now)
    with database.session_factory.begin() as session:
        validation = InstrumentCatalogValidator().validate(
            session,
            instrument_classification_validation_request(risk_request, now),
        )
        record = session.get(InstrumentCatalogRecord, request.catalog_record_id)
        assert record is not None
        assert record.environment == "SHADOW"
        assert record.real_funds_eligible is False
        assert session.scalar(select(func.count()).select_from(InstrumentCatalogRecord)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "InstrumentCatalogRecordRegistered")
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.event_type == "InstrumentCatalogRecordRegistered")
            )
            == 1
        )

    assert denied.status is CommandStatus.REJECTED
    assert denied.error_code == "INSTRUMENT_CATALOG_SERVICE_REQUIRED"
    assert rejected.status is CommandStatus.REJECTED
    assert rejected.error_code == "INSTRUMENT_CATALOG_RECORD_INVALID"
    assert original.status is CommandStatus.COMPLETED
    assert original.data["record_hash"] == request.record_hash
    assert replay.status is CommandStatus.ALREADY_PROCESSED
    assert duplicate.status is CommandStatus.REJECTED
    assert duplicate.error_code == "INSTRUMENT_CATALOG_VERSION_EXISTS"
    assert validation.valid is True
    assert validation.reason_codes == ()
    assert validation.catalog_record_id == request.catalog_record_id
    assert validation.record_hash == request.record_hash
    assert validation.evidence_hash == request.evidence_hash
    assert validation.valid_until == request.valid_until


@pytest.mark.parametrize(
    ("updates", "reason_code"),
    [
        ({"discoverable": False}, "INSTRUMENT_NOT_DISCOVERABLE"),
        (
            {
                "classification_complete": False,
                "sector": InstrumentSector.UNCLASSIFIED,
            },
            "INSTRUMENT_CLASSIFICATION_INCOMPLETE",
        ),
        (
            {"risk_cluster_ids": ("CRYPTO_MAJOR", "CRYPTO_SYSTEMIC")},
            "MULTI_CLUSTER_RISK_SCOPE_UNSUPPORTED",
        ),
        ({"underlying_id": "ETH"}, "INSTRUMENT_CLASSIFICATION_SCOPE_MISMATCH"),
    ],
)
def test_catalog_validation_fails_closed_for_unusable_or_mismatched_classification(
    database: Database,
    updates: dict[str, object],
    reason_code: str,
) -> None:
    now = datetime.now(UTC)
    risk_request = make_request(now=now)
    request = instrument_catalog_request_for_risk(
        risk_request,
        now=now,
        **updates,
    )
    registered = register_instrument_catalog(database, request, now=now)
    assert registered.status is CommandStatus.COMPLETED

    with database.session_factory.begin() as session:
        validation = InstrumentCatalogValidator().validate(
            session,
            instrument_classification_validation_request(risk_request, now),
        )

    assert validation.valid is False
    assert reason_code in validation.reason_codes
    assert validation.valid_until == now


def test_catalog_validation_fails_closed_for_missing_and_bad_hash(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    risk_request = make_request(now=now)
    validation_request = instrument_classification_validation_request(risk_request, now)
    with database.session_factory.begin() as session:
        missing = InstrumentCatalogValidator().validate(session, validation_request)
    assert missing.valid is False
    assert missing.reason_codes == ("INSTRUMENT_CATALOG_RECORD_NOT_FOUND",)

    expired = instrument_catalog_request_for_risk(
        risk_request,
        now=now,
        valid_from=now - timedelta(hours=2),
        valid_until=now - timedelta(hours=1),
        source_observed_at=now - timedelta(hours=3),
    )
    with database.session_factory.begin() as session:
        session.add(_database_record(expired, created_at=now, record_hash="0" * 64))
    with database.session_factory.begin() as session:
        invalid = InstrumentCatalogValidator().validate(session, validation_request)

    assert invalid.valid is False
    assert invalid.reason_codes == ("INSTRUMENT_CATALOG_INTEGRITY_FAILED",)
    assert invalid.valid_until == now


def test_catalog_validation_fails_closed_outside_record_validity_window(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    risk_request = make_request(now=now)
    expired = instrument_catalog_request_for_risk(
        risk_request,
        now=now,
        valid_from=now - timedelta(hours=2),
        valid_until=now - timedelta(hours=1),
        source_observed_at=now - timedelta(hours=3),
    )
    registered = register_instrument_catalog(database, expired, now=now)
    assert registered.status is CommandStatus.COMPLETED

    with database.session_factory.begin() as session:
        validation = InstrumentCatalogValidator().validate(
            session,
            instrument_classification_validation_request(risk_request, now),
        )

    assert validation.valid is False
    assert validation.reason_codes == ("INSTRUMENT_CATALOG_OUTSIDE_VALID_WINDOW",)
    assert validation.valid_until == now


def test_catalog_database_guards_are_immutable_and_canonical(database: Database) -> None:
    now = datetime.now(UTC)
    risk_request = make_request(now=now)
    request = instrument_catalog_request_for_risk(risk_request, now=now)
    result = register_instrument_catalog(database, request, now=now)
    assert result.status is CommandStatus.COMPLETED

    with pytest.raises(DBAPIError, match="is immutable"):
        with database.session_factory.begin() as session:
            session.execute(
                update(InstrumentCatalogRecord)
                .where(InstrumentCatalogRecord.catalog_record_id == request.catalog_record_id)
                .values(source_ref="direct-db-tamper")
            )

    bypass = instrument_catalog_request_for_risk(
        risk_request,
        now=now,
        catalog_record_id=uuid4(),
        catalog_version="catalog-test-v2",
        classification_version="instrument-scope-test-v2",
    )
    with pytest.raises(DBAPIError, match="not canonical"):
        with database.session_factory.begin() as session:
            session.add(
                _database_record(
                    bypass,
                    created_at=now,
                    risk_cluster_ids=["Z_CLUSTER", "A_CLUSTER"],
                )
            )

    evidence_bypass = instrument_catalog_request_for_risk(
        risk_request,
        now=now,
        catalog_record_id=uuid4(),
        catalog_version="catalog-test-v3",
        classification_version="instrument-scope-test-v3",
    )
    with pytest.raises(DBAPIError, match="not canonical"):
        with database.session_factory.begin() as session:
            session.add(
                _database_record(
                    evidence_bypass,
                    created_at=now,
                    evidence_refs=["test-only:z", "test-only:a"],
                )
            )
