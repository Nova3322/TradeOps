from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from trading_control_plane.commands import (
    CommandEnvelope,
    CommandOutcome,
    CommandRejected,
    CommandResult,
    CommandStatus,
    DomainEvent,
    hash_json,
)
from trading_control_plane.metrics import COMMAND_DURATION, COMMAND_RESULTS
from trading_control_plane.models import AuditEvent, CommandReceipt, OutboxMessage

logger = logging.getLogger(__name__)

CommandHandler = Callable[[Session, CommandEnvelope], CommandOutcome]


class IdempotentCommandExecutor:
    """Executes one handler in the receipt/audit/outbox database transaction."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def execute(self, envelope: CommandEnvelope, handler: CommandHandler) -> CommandResult:
        started = time.monotonic()
        metric_result = "ERROR"
        try:
            with self._session_factory.begin() as session:
                receipt = self._claim_receipt(session, envelope)
                if receipt is not None:
                    if receipt.request_hash != envelope.semantic_hash():
                        result = self._record_conflict(session, envelope)
                    else:
                        result = self._replay(receipt)
                    metric_result = result.status.value
                    return result

                outcome = self._execute_handler(session, envelope, handler)
                result = self._persist_outcome(session, envelope, outcome)
                metric_result = result.status.value
                return result
        except Exception:
            logger.exception(
                "durable command transaction failed",
                extra={
                    "event": "command_transaction_failed",
                    "command_type": envelope.command_type,
                    "result": "ERROR",
                    "component": "command_executor",
                },
            )
            raise
        finally:
            COMMAND_RESULTS.labels(envelope.command_type, metric_result).inc()
            COMMAND_DURATION.labels(envelope.command_type).observe(time.monotonic() - started)

    def _claim_receipt(self, session: Session, envelope: CommandEnvelope) -> CommandReceipt | None:
        receipt_id = uuid4()
        claim = (
            insert(CommandReceipt)
            .values(
                receipt_id=receipt_id,
                command_id=envelope.command_id,
                caller_id=envelope.caller_id,
                command_type=envelope.command_type,
                idempotency_key=envelope.idempotency_key,
                request_hash=envelope.semantic_hash(),
                # A final state is written before commit; an exception rolls back the row.
                state="COMPLETED",
                response={},
                completed_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=("caller_id", "command_type", "idempotency_key"))
            .returning(CommandReceipt.receipt_id)
        )
        owned_receipt_id = session.execute(claim).scalar_one_or_none()
        if owned_receipt_id is not None:
            return None
        return session.execute(
            select(CommandReceipt).where(
                CommandReceipt.caller_id == envelope.caller_id,
                CommandReceipt.command_type == envelope.command_type,
                CommandReceipt.idempotency_key == envelope.idempotency_key,
            )
        ).scalar_one()

    def _execute_handler(
        self,
        session: Session,
        envelope: CommandEnvelope,
        handler: CommandHandler,
    ) -> CommandOutcome:
        if envelope.is_expired():
            return CommandOutcome(
                status=CommandStatus.REJECTED,
                object_type=envelope.object_type,
                object_id=envelope.object_id,
                error_code="EXPIRED",
                data={"message": "command expired before durable handling"},
                events=(
                    DomainEvent(
                        event_type="CommandExpired",
                        aggregate_type=envelope.object_type or "Command",
                        aggregate_id=envelope.object_id or str(envelope.command_id),
                        payload={"error_code": "EXPIRED"},
                    ),
                ),
            )
        try:
            return handler(session, envelope)
        except CommandRejected as exc:
            return CommandOutcome(
                status=CommandStatus.REJECTED,
                object_type=envelope.object_type,
                object_id=envelope.object_id,
                error_code=exc.error_code,
                data={"message": exc.message},
                events=(
                    DomainEvent(
                        event_type="CommandRejected",
                        aggregate_type=envelope.object_type or "Command",
                        aggregate_id=envelope.object_id or str(envelope.command_id),
                        payload={"error_code": exc.error_code},
                    ),
                ),
            )

    def _persist_outcome(
        self,
        session: Session,
        envelope: CommandEnvelope,
        outcome: CommandOutcome,
    ) -> CommandResult:
        result = CommandResult(
            status=outcome.status,
            command_id=envelope.command_id,
            object_type=outcome.object_type,
            object_id=outcome.object_id,
            object_version=outcome.object_version,
            data=outcome.data,
            error_code=outcome.error_code,
        )
        for event in outcome.events:
            self._append_event(session, envelope, event)

        receipt = session.execute(
            select(CommandReceipt).where(CommandReceipt.command_id == envelope.command_id)
        ).scalar_one()
        receipt.state = "REJECTED" if outcome.status is CommandStatus.REJECTED else "COMPLETED"
        receipt.response = result.model_dump(mode="json")
        receipt.completed_at = datetime.now(UTC)
        return result

    def _append_event(
        self,
        session: Session,
        envelope: CommandEnvelope,
        event: DomainEvent,
    ) -> None:
        occurred_at = datetime.now(UTC)
        payload = event.model_dump(mode="json")["payload"]
        audit_payload: dict[str, Any] = {
            "event": payload,
            "scope": envelope.scope,
            "auth_context_ref": envelope.auth_context_ref,
        }
        session.add(
            AuditEvent(
                event_id=event.event_id,
                event_type=event.event_type,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                command_id=envelope.command_id,
                correlation_id=envelope.correlation_id,
                causation_id=envelope.causation_id,
                caller_id=envelope.caller_id,
                channel=envelope.channel.value,
                payload_schema_version=event.payload_schema_version,
                payload=audit_payload,
                payload_hash=hash_json(audit_payload),
                occurred_at=occurred_at,
            )
        )
        session.add(
            OutboxMessage(
                message_id=event.event_id,
                topic="trading.domain-events.v1",
                message_key=f"{event.aggregate_type}:{event.aggregate_id}",
                event_type=event.event_type,
                payload_schema_version=event.payload_schema_version,
                payload=payload,
                headers={
                    "command_id": str(envelope.command_id),
                    "correlation_id": str(envelope.correlation_id),
                    "causation_id": (str(envelope.causation_id) if envelope.causation_id else None),
                },
                occurred_at=occurred_at,
            )
        )

    def _record_conflict(self, session: Session, envelope: CommandEnvelope) -> CommandResult:
        event = DomainEvent(
            event_type="CommandIdempotencyConflictDetected",
            aggregate_type=envelope.object_type or "Command",
            aggregate_id=envelope.object_id or str(envelope.command_id),
            payload={"error_code": "IDEMPOTENCY_KEY_REUSED"},
        )
        # A conflict is audit-only. It must not overwrite or republish the original command.
        occurred_at = datetime.now(UTC)
        payload: dict[str, Any] = {
            "event": event.payload,
            "scope": envelope.scope,
            "auth_context_ref": envelope.auth_context_ref,
        }
        session.add(
            AuditEvent(
                event_id=event.event_id,
                event_type=event.event_type,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                command_id=envelope.command_id,
                correlation_id=envelope.correlation_id,
                causation_id=envelope.causation_id,
                caller_id=envelope.caller_id,
                channel=envelope.channel.value,
                payload_schema_version=event.payload_schema_version,
                payload=payload,
                payload_hash=hash_json(payload),
                occurred_at=occurred_at,
            )
        )
        return CommandResult(
            status=CommandStatus.CONFLICT,
            command_id=envelope.command_id,
            object_type=envelope.object_type,
            object_id=envelope.object_id,
            error_code="IDEMPOTENCY_KEY_REUSED",
            data={"message": "idempotency key already belongs to different semantics"},
        )

    @staticmethod
    def _replay(receipt: CommandReceipt) -> CommandResult:
        original = CommandResult.model_validate(receipt.response)
        return CommandResult(
            status=CommandStatus.ALREADY_PROCESSED,
            command_id=original.command_id,
            object_type=original.object_type,
            object_id=original.object_id,
            object_version=original.object_version,
            error_code=original.error_code,
            data={"original_status": original.status.value, "original_data": original.data},
            replayed=True,
        )
