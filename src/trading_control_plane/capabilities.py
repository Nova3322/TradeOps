from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_control_plane.commands import (
    CommandEnvelope,
    CommandOutcome,
    CommandRejected,
    CommandStatus,
    DomainEvent,
)
from trading_control_plane.models import CapabilityGate


class CapabilityService:
    """Risk-tightening capability operations.

    WP-0001 intentionally provides no enable operation.
    """

    command_type = "capability.disable.v1"

    def disable(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        if envelope.command_type != self.command_type:
            raise CommandRejected("COMMAND_TYPE_MISMATCH", "unexpected command type")
        if envelope.object_type != "CapabilityGate" or envelope.object_id is None:
            raise CommandRejected("VALIDATION_FAILED", "CapabilityGate target is required")
        if not envelope.reason:
            raise CommandRejected("VALIDATION_FAILED", "a structured disable reason is required")

        gate = session.execute(
            select(CapabilityGate)
            .where(CapabilityGate.capability_key == envelope.object_id)
            .with_for_update()
        ).scalar_one_or_none()
        if gate is None:
            raise CommandRejected("CAPABILITY_UNKNOWN", "capability gate is not registered")
        if envelope.expected_version is not None and envelope.expected_version != gate.version:
            raise CommandRejected("VERSION_CONFLICT", "capability gate version changed")

        changed = gate.status != "DISABLED"
        if changed:
            gate.status = "DISABLED"
            gate.version += 1
            gate.reason = envelope.reason
            gate.certificate_ref = None
            gate.updated_at = datetime.now(UTC)

        event_type = "CapabilityDisabled" if changed else "CapabilityDisableConfirmed"
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type="CapabilityGate",
            object_id=gate.capability_key,
            object_version=gate.version,
            data={"status": gate.status, "changed": changed},
            events=(
                DomainEvent(
                    event_type=event_type,
                    aggregate_type="CapabilityGate",
                    aggregate_id=gate.capability_key,
                    payload={
                        "status": gate.status,
                        "version": gate.version,
                        "changed": changed,
                        "reason_code": "RISK_TIGHTENING",
                    },
                ),
            ),
        )
