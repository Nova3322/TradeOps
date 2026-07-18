from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.risk_fact_set_fixtures import (
    register_risk_fact_set,
    risk_fact_set_envelope,
    risk_fact_set_request_for_risk,
)
from tests.risk_fixtures import make_request
from trading_control_plane.commands import CommandStatus
from trading_control_plane.database import Database
from trading_control_plane.models import AuditEvent, OutboxMessage
from trading_control_plane.risk import risk_fact_set_validation_request
from trading_control_plane.risk_fact_set_models import RiskFactSetRecord
from trading_control_plane.risk_fact_sets import RiskFactSetValidator

pytestmark = pytest.mark.integration


def _database_record(request, *, created_at: datetime, **updates) -> RiskFactSetRecord:
    values = {
        "risk_fact_set_id": request.risk_fact_set_id,
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
            observation.model_dump(mode="json") for observation in request.observations
        ],
        "environment": request.environment.value,
        "real_funds_eligible": request.real_funds_eligible,
        "assembled_at": request.assembled_at,
        "valid_from": request.valid_from,
        "valid_until": request.valid_until,
        "record_hash": request.record_hash,
        "evidence_refs": list(request.evidence_refs),
        "evidence_hash": request.evidence_hash,
        "source_ref": request.source_ref,
        "created_at": created_at,
    }
    values.update(updates)
    return RiskFactSetRecord(**values)


def test_register_validate_and_audit_exact_complete_shadow_fact_set(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    risk_request = make_request(now=now)
    request = risk_fact_set_request_for_risk(risk_request, now=now)
    envelope = risk_fact_set_envelope(
        request,
        now=now,
        idempotency_key="risk-fact-set-stable-key",
    )

    denied = register_risk_fact_set(
        database,
        request,
        now=now,
        envelope=risk_fact_set_envelope(
            request,
            now=now,
            service_principal="some-other-service",
        ),
    )
    invalid_hash = request.model_copy(update={"record_hash": "0" * 64})
    rejected = register_risk_fact_set(database, invalid_hash, now=now)
    original = register_risk_fact_set(database, request, now=now, envelope=envelope)
    replay = register_risk_fact_set(
        database,
        request,
        now=now,
        envelope=envelope.model_copy(update={"command_id": uuid4(), "correlation_id": uuid4()}),
    )
    duplicate_request = risk_fact_set_request_for_risk(
        risk_request,
        now=now,
        risk_fact_set_id=uuid4(),
    )
    duplicate = register_risk_fact_set(database, duplicate_request, now=now)

    with database.session_factory.begin() as session:
        validation = RiskFactSetValidator().validate(
            session,
            risk_fact_set_validation_request(risk_request, now),
        )
        record = session.get(RiskFactSetRecord, request.risk_fact_set_id)
        assert record is not None
        assert record.environment == "SHADOW"
        assert record.real_funds_eligible is False
        assert len(record.observations) == 9
        assert session.scalar(select(func.count()).select_from(RiskFactSetRecord)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "RiskFactSetRegistered")
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.event_type == "RiskFactSetRegistered")
            )
            == 1
        )

    assert denied.status is CommandStatus.REJECTED
    assert denied.error_code == "RISK_FACT_AGGREGATOR_SERVICE_REQUIRED"
    assert rejected.status is CommandStatus.REJECTED
    assert rejected.error_code == "RISK_FACT_SET_INVALID"
    assert original.status is CommandStatus.COMPLETED
    assert original.data["record_hash"] == request.record_hash
    assert replay.status is CommandStatus.ALREADY_PROCESSED
    assert duplicate.status is CommandStatus.REJECTED
    assert duplicate.error_code == "RISK_FACT_SET_VERSION_EXISTS"
    assert validation.valid is True
    assert validation.reason_codes == ()
    assert validation.risk_fact_set_id == request.risk_fact_set_id
    assert validation.record_hash == request.record_hash
    assert validation.evidence_hash == request.evidence_hash
    assert validation.observations == request.observations
    assert validation.valid_until == request.valid_until


def test_registration_rejects_incomplete_and_noncanonical_observations(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    request = risk_fact_set_request_for_risk(make_request(now=now), now=now)
    envelope = risk_fact_set_envelope(request, now=now)

    incomplete_payload = dict(envelope.payload)
    incomplete_payload["observations"] = incomplete_payload["observations"][:-1]
    incomplete = register_risk_fact_set(
        database,
        request,
        now=now,
        envelope=envelope.model_copy(
            update={
                "command_id": uuid4(),
                "idempotency_key": "risk-fact-set-incomplete",
                "payload": incomplete_payload,
            }
        ),
    )

    noncanonical_payload = dict(envelope.payload)
    noncanonical_payload["observations"] = list(reversed(noncanonical_payload["observations"]))
    noncanonical = register_risk_fact_set(
        database,
        request,
        now=now,
        envelope=envelope.model_copy(
            update={
                "command_id": uuid4(),
                "idempotency_key": "risk-fact-set-noncanonical",
                "payload": noncanonical_payload,
            }
        ),
    )

    assert incomplete.status is CommandStatus.REJECTED
    assert incomplete.error_code == "RISK_FACT_SET_INVALID"
    assert noncanonical.status is CommandStatus.REJECTED
    assert noncanonical.error_code == "RISK_FACT_SET_INVALID"
    with database.session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(RiskFactSetRecord)) == 0


def test_validation_fails_closed_for_missing_expired_and_latest_bad_hash(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    risk_request = make_request(now=now)
    validation_request = risk_fact_set_validation_request(risk_request, now)
    with database.session_factory.begin() as session:
        missing = RiskFactSetValidator().validate(session, validation_request)
    assert missing.valid is False
    assert missing.reason_codes == ("RISK_FACT_SET_RECORD_NOT_FOUND",)

    expired = risk_fact_set_request_for_risk(
        risk_request,
        now=now - timedelta(hours=3),
        fact_set_version="risk-fact-set-expired-v1",
        valid_from=now - timedelta(hours=2),
        valid_until=now - timedelta(hours=1),
    )
    assert register_risk_fact_set(database, expired, now=now).status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        outside_window = RiskFactSetValidator().validate(session, validation_request)
    assert outside_window.valid is False
    assert outside_window.reason_codes == ("RISK_FACT_SET_OUTSIDE_VALID_WINDOW",)
    assert outside_window.valid_until == now

    healthy = risk_fact_set_request_for_risk(
        risk_request,
        now=now - timedelta(minutes=2),
        fact_set_version="risk-fact-set-healthy-v2",
        valid_until=now + timedelta(hours=1),
    )
    assert register_risk_fact_set(database, healthy, now=now).status is CommandStatus.COMPLETED
    latest = risk_fact_set_request_for_risk(
        risk_request,
        now=now - timedelta(minutes=1),
        fact_set_version="risk-fact-set-invalid-v3",
        risk_fact_set_id=uuid4(),
        valid_until=now + timedelta(hours=1),
    )
    with database.session_factory.begin() as session:
        session.add(_database_record(latest, created_at=now, record_hash="0" * 64))
    with database.session_factory.begin() as session:
        integrity_failure = RiskFactSetValidator().validate(session, validation_request)

    assert integrity_failure.valid is False
    assert integrity_failure.reason_codes == ("RISK_FACT_SET_INTEGRITY_FAILED",)
    assert integrity_failure.risk_fact_set_id == latest.risk_fact_set_id
    assert integrity_failure.observations == ()
    assert integrity_failure.valid_until == now


def test_database_enforces_shadow_only_immutability_and_canonical_observations(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    risk_request = make_request(now=now)
    request = risk_fact_set_request_for_risk(risk_request, now=now)
    assert register_risk_fact_set(database, request, now=now).status is CommandStatus.COMPLETED

    with pytest.raises(DBAPIError, match="immutable"):
        with database.session_factory.begin() as session:
            session.execute(
                update(RiskFactSetRecord)
                .where(RiskFactSetRecord.risk_fact_set_id == request.risk_fact_set_id)
                .values(source_ref="direct-db-tamper")
            )

    real_funds = risk_fact_set_request_for_risk(
        risk_request,
        now=now,
        fact_set_version="risk-fact-set-real-funds-v2",
        risk_fact_set_id=uuid4(),
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

    noncanonical = risk_fact_set_request_for_risk(
        risk_request,
        now=now,
        fact_set_version="risk-fact-set-noncanonical-v3",
        risk_fact_set_id=uuid4(),
    )
    with pytest.raises(DBAPIError, match="not canonical"):
        with database.session_factory.begin() as session:
            session.add(
                _database_record(
                    noncanonical,
                    created_at=now,
                    observations=list(
                        reversed(
                            [
                                observation.model_dump(mode="json")
                                for observation in noncanonical.observations
                            ]
                        )
                    ),
                )
            )
