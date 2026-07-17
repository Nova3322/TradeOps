from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from trading_control_plane.commands import (
    CommandChannel,
    CommandEnvelope,
    CommandOutcome,
    CommandStatus,
)


def make_envelope(**overrides: object) -> CommandEnvelope:
    issued_at = datetime.now(UTC)
    values: dict[str, object] = {
        "idempotency_key": "disable-live-order-0001",
        "command_type": "capability.disable.v1",
        "object_type": "CapabilityGate",
        "object_id": "LIVE_ORDER_SEND",
        "expected_version": 1,
        "actor_id": "operator-1",
        "channel": CommandChannel.WEB,
        "scope": {"organization_id": "org-1"},
        "correlation_id": uuid4(),
        "issued_at": issued_at,
        "expires_at": issued_at + timedelta(minutes=5),
        "auth_context_ref": "auth-session:example",
        "payload_schema_version": 1,
        "reason": "risk tightening",
        "payload": {"reason_code": "RISK_TIGHTENING"},
    }
    values.update(overrides)
    return CommandEnvelope.model_validate(values)


def test_envelope_requires_exactly_one_principal() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        make_envelope(actor_id=None)

    with pytest.raises(ValidationError, match="exactly one"):
        make_envelope(service_principal="worker-1")


def test_envelope_requires_complete_object_reference() -> None:
    with pytest.raises(ValidationError, match="supplied together"):
        make_envelope(object_id=None)


def test_semantic_hash_ignores_retry_transport_metadata() -> None:
    first = make_envelope()
    second = make_envelope(
        command_id=uuid4(),
        correlation_id=uuid4(),
        causation_id=uuid4(),
        issued_at=first.issued_at + timedelta(seconds=1),
        expires_at=first.expires_at + timedelta(seconds=1),
    )

    assert first.semantic_hash() == second.semantic_hash()


def test_semantic_hash_changes_when_business_payload_changes() -> None:
    first = make_envelope()
    second = make_envelope(payload={"reason_code": "MANUAL_KILL_SWITCH"})

    assert first.semantic_hash() != second.semantic_hash()


def test_handler_outcome_rejects_transport_only_status() -> None:
    with pytest.raises(ValidationError, match="handlers may only return"):
        CommandOutcome(status=CommandStatus.ALREADY_PROCESSED)
