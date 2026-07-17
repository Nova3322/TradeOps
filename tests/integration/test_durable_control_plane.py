from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from trading_control_plane.capabilities import CapabilityService
from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import (
    CommandChannel,
    CommandEnvelope,
    CommandStatus,
)
from trading_control_plane.database import Database
from trading_control_plane.inbox import IdempotentInboxProcessor, InboxPayloadConflict
from trading_control_plane.models import (
    AuditEvent,
    CapabilityGate,
    CommandReceipt,
    InboxReceipt,
    OutboxMessage,
)

pytestmark = pytest.mark.integration


def make_disable_envelope(**overrides: object) -> CommandEnvelope:
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
        "auth_context_ref": "auth-session:integration",
        "payload_schema_version": 1,
        "reason": "manual risk tightening",
        "payload": {"reason_code": "RISK_TIGHTENING"},
    }
    values.update(overrides)
    return CommandEnvelope.model_validate(values)


def count_rows(session: Session, model: type[object]) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def test_migration_seeds_all_real_capabilities_disabled(database: Database) -> None:
    with database.session_factory.begin() as session:
        gates = session.execute(
            select(CapabilityGate).order_by(CapabilityGate.capability_key)
        ).scalars()

        assert [(gate.capability_key, gate.status) for gate in gates] == [
            ("AUTO_ADD", "DISABLED"),
            ("CAPITAL_TRANSFER", "DISABLED"),
            ("LIVE_ORDER_SEND", "DISABLED"),
        ]


def test_database_readiness_accepts_migrated_control_plane(database: Database) -> None:
    assert database.is_ready() == (True, None)


def test_state_receipt_audit_and_outbox_commit_together(database: Database) -> None:
    with database.session_factory.begin() as session:
        gate = session.get(CapabilityGate, "LIVE_ORDER_SEND")
        assert gate is not None
        gate.status = "SHADOW"

    executor = IdempotentCommandExecutor(database.session_factory)
    result = executor.execute(make_disable_envelope(), CapabilityService().disable)

    assert result.status is CommandStatus.COMPLETED
    assert result.object_version == 2
    with database.session_factory.begin() as session:
        gate = session.get(CapabilityGate, "LIVE_ORDER_SEND")
        assert gate is not None
        assert gate.status == "DISABLED"
        assert count_rows(session, CommandReceipt) == 1
        assert count_rows(session, AuditEvent) == 1
        assert count_rows(session, OutboxMessage) == 1


def test_semantic_retry_replays_without_duplicate_side_effects(database: Database) -> None:
    executor = IdempotentCommandExecutor(database.session_factory)
    first = make_disable_envelope()
    second = make_disable_envelope(
        command_id=uuid4(),
        correlation_id=uuid4(),
        issued_at=first.issued_at + timedelta(seconds=1),
        expires_at=first.expires_at + timedelta(seconds=1),
    )

    original = executor.execute(first, CapabilityService().disable)
    replay = executor.execute(second, CapabilityService().disable)

    assert original.status is CommandStatus.COMPLETED
    assert replay.status is CommandStatus.ALREADY_PROCESSED
    assert replay.command_id == original.command_id
    assert replay.replayed is True
    with database.session_factory.begin() as session:
        assert count_rows(session, CommandReceipt) == 1
        assert count_rows(session, AuditEvent) == 1
        assert count_rows(session, OutboxMessage) == 1


def test_reused_key_with_changed_semantics_is_audited_and_rejected(
    database: Database,
) -> None:
    executor = IdempotentCommandExecutor(database.session_factory)
    original = make_disable_envelope()
    conflict = make_disable_envelope(reason="different business reason")

    executor.execute(original, CapabilityService().disable)
    result = executor.execute(conflict, CapabilityService().disable)

    assert result.status is CommandStatus.CONFLICT
    assert result.error_code == "IDEMPOTENCY_KEY_REUSED"
    with database.session_factory.begin() as session:
        assert count_rows(session, CommandReceipt) == 1
        assert count_rows(session, AuditEvent) == 2
        assert count_rows(session, OutboxMessage) == 1


def test_unexpected_handler_failure_rolls_back_all_writes(database: Database) -> None:
    def fail_after_mutation(session: Session, _: CommandEnvelope) -> None:
        session.execute(
            update(CapabilityGate)
            .where(CapabilityGate.capability_key == "LIVE_ORDER_SEND")
            .values(status="SHADOW", version=2)
        )
        raise RuntimeError("synthetic handler failure")

    executor = IdempotentCommandExecutor(database.session_factory)
    with pytest.raises(RuntimeError, match="synthetic handler failure"):
        executor.execute(make_disable_envelope(), fail_after_mutation)  # type: ignore[arg-type]

    with database.session_factory.begin() as session:
        gate = session.get(CapabilityGate, "LIVE_ORDER_SEND")
        assert gate is not None
        assert (gate.status, gate.version) == ("DISABLED", 1)
        assert count_rows(session, CommandReceipt) == 0
        assert count_rows(session, AuditEvent) == 0
        assert count_rows(session, OutboxMessage) == 0


def test_audit_rows_cannot_be_updated_or_deleted(database: Database) -> None:
    executor = IdempotentCommandExecutor(database.session_factory)
    executor.execute(make_disable_envelope(), CapabilityService().disable)

    with pytest.raises(DBAPIError, match="audit_events is append-only"):
        with database.engine.begin() as connection:
            connection.execute(text("UPDATE audit_events SET event_type = 'Tampered'"))

    with pytest.raises(DBAPIError, match="audit_events is append-only"):
        with database.engine.begin() as connection:
            connection.execute(text("DELETE FROM audit_events"))


def test_inbox_claim_and_handler_commit_exactly_once(database: Database) -> None:
    processor = IdempotentInboxProcessor(database.session_factory)
    message_id = uuid4()

    def handler(session: Session) -> None:
        session.execute(
            update(CapabilityGate)
            .where(CapabilityGate.capability_key == "AUTO_ADD")
            .values(reason="processed once", version=CapabilityGate.version + 1)
        )

    first = processor.process_once(
        consumer_name="projection-worker",
        message_id=message_id,
        payload={"event": "example"},
        handler=handler,
    )
    second = processor.process_once(
        consumer_name="projection-worker",
        message_id=message_id,
        payload={"event": "example"},
        handler=handler,
    )

    assert first is True
    assert second is False
    with database.session_factory.begin() as session:
        gate = session.get(CapabilityGate, "AUTO_ADD")
        assert gate is not None
        assert gate.version == 2
        assert count_rows(session, InboxReceipt) == 1


def test_inbox_rejects_same_message_identity_with_different_payload(
    database: Database,
) -> None:
    processor = IdempotentInboxProcessor(database.session_factory)
    message_id = uuid4()

    assert processor.process_once(
        consumer_name="projection-worker",
        message_id=message_id,
        payload={"version": 1},
        handler=lambda _: None,
    )
    with pytest.raises(InboxPayloadConflict, match="different payload semantics"):
        processor.process_once(
            consumer_name="projection-worker",
            message_id=message_id,
            payload={"version": 2},
            handler=lambda _: None,
        )

    with database.session_factory.begin() as session:
        assert count_rows(session, InboxReceipt) == 1


def test_concurrent_duplicate_commands_have_one_side_effect(database: Database) -> None:
    executor = IdempotentCommandExecutor(database.session_factory)
    service = CapabilityService()
    first = make_disable_envelope()
    second = make_disable_envelope(
        command_id=uuid4(),
        correlation_id=uuid4(),
        issued_at=first.issued_at + timedelta(milliseconds=1),
        expires_at=first.expires_at + timedelta(milliseconds=1),
    )

    def slow_disable(session: Session, envelope: CommandEnvelope):  # type: ignore[no-untyped-def]
        time.sleep(0.15)
        return service.disable(session, envelope)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda item: executor.execute(item, slow_disable), (first, second)))

    assert {result.status for result in results} == {
        CommandStatus.COMPLETED,
        CommandStatus.ALREADY_PROCESSED,
    }
    with database.session_factory.begin() as session:
        assert count_rows(session, CommandReceipt) == 1
        assert count_rows(session, AuditEvent) == 1
        assert count_rows(session, OutboxMessage) == 1
