from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class CommandChannel(StrEnum):
    WEB = "WEB"
    PWA = "PWA"
    TELEGRAM = "TELEGRAM"
    SYSTEM = "SYSTEM"
    INTERNAL = "INTERNAL"


class CommandStatus(StrEnum):
    REJECTED = "REJECTED"
    ACCEPTED = "ACCEPTED"
    COMPLETED = "COMPLETED"
    ALREADY_PROCESSED = "ALREADY_PROCESSED"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"


class CommandEnvelope(BaseModel):
    """Versioned command metadata required by the API/Event contract."""

    model_config = ConfigDict(frozen=True)

    command_id: UUID = Field(default_factory=uuid4)
    idempotency_key: str = Field(min_length=8, max_length=160)
    command_type: str = Field(min_length=3, max_length=160)
    object_type: str | None = Field(default=None, max_length=120)
    object_id: str | None = Field(default=None, max_length=255)
    expected_version: int | None = Field(default=None, ge=1)
    actor_id: str | None = Field(default=None, max_length=255)
    service_principal: str | None = Field(default=None, max_length=255)
    channel: CommandChannel
    scope: dict[str, JsonValue]
    correlation_id: UUID
    causation_id: UUID | None = None
    issued_at: datetime
    expires_at: datetime
    auth_context_ref: str = Field(min_length=1, max_length=255)
    payload_schema_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=1000)
    payload: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_contract(self) -> CommandEnvelope:
        if (self.actor_id is None) == (self.service_principal is None):
            raise ValueError("exactly one of actor_id or service_principal is required")
        if (self.object_type is None) != (self.object_id is None):
            raise ValueError("object_type and object_id must be supplied together")
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("issued_at and expires_at must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", self.command_type):
            raise ValueError("command_type must be a stable versioned identifier")
        return self

    @property
    def caller_id(self) -> str:
        if self.actor_id is not None:
            return f"user:{self.actor_id}"
        if self.service_principal is None:  # pragma: no cover - enforced by validation
            raise RuntimeError("missing principal")
        return f"service:{self.service_principal}"

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at <= (now or datetime.now(UTC))

    def semantic_hash(self) -> str:
        """Hash fields that define business semantics, excluding retry transport metadata."""

        semantic = {
            "idempotency_key": self.idempotency_key,
            "command_type": self.command_type,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "expected_version": self.expected_version,
            "caller_id": self.caller_id,
            "channel": self.channel.value,
            "scope": self.scope,
            "auth_context_ref": self.auth_context_ref,
            "payload_schema_version": self.payload_schema_version,
            "reason": self.reason,
            "payload": self.payload,
        }
        return hash_json(semantic)


class DomainEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: UUID = Field(default_factory=uuid4)
    event_type: str = Field(min_length=3, max_length=160)
    aggregate_type: str = Field(min_length=1, max_length=120)
    aggregate_id: str = Field(min_length=1, max_length=255)
    payload_schema_version: int = Field(default=1, ge=1)
    payload: dict[str, JsonValue]


class CommandOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: CommandStatus
    object_type: str | None = None
    object_id: str | None = None
    object_version: int | None = None
    data: dict[str, JsonValue] = Field(default_factory=dict)
    error_code: str | None = None
    events: tuple[DomainEvent, ...] = ()

    @model_validator(mode="after")
    def validate_outcome(self) -> CommandOutcome:
        if self.status not in {
            CommandStatus.ACCEPTED,
            CommandStatus.COMPLETED,
            CommandStatus.REJECTED,
        }:
            raise ValueError("handlers may only return ACCEPTED, COMPLETED, or REJECTED")
        if self.status is CommandStatus.REJECTED and not self.error_code:
            raise ValueError("rejected outcomes require error_code")
        return self


class CommandResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: CommandStatus
    command_id: UUID
    object_type: str | None = None
    object_id: str | None = None
    object_version: int | None = None
    data: dict[str, JsonValue] = Field(default_factory=dict)
    error_code: str | None = None
    replayed: bool = False


class CommandRejected(Exception):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def hash_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
