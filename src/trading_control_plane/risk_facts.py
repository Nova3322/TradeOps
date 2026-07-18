from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FactType(StrEnum):
    MARKET = "MARKET"
    ACCOUNT = "ACCOUNT"
    VAULT = "VAULT"
    POSITIONS = "POSITIONS"
    ORDERS = "ORDERS"
    LEDGER = "LEDGER"
    CATALOG = "CATALOG"
    VENUE_CAPABILITY = "VENUE_CAPABILITY"
    PROTECTION = "PROTECTION"


class FactStatus(StrEnum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"


class FactObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_type: FactType
    status: FactStatus
    source_ref: str = Field(min_length=1, max_length=255)
    source_version: str = Field(min_length=1, max_length=120)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_time: datetime
    received_at: datetime

    @model_validator(mode="after")
    def timestamps_are_aware_and_monotonic(self) -> Self:
        if (
            self.event_time.tzinfo is None
            or self.event_time.utcoffset() is None
            or self.received_at.tzinfo is None
            or self.received_at.utcoffset() is None
        ):
            raise ValueError("fact timestamps must be timezone-aware")
        if self.received_at < self.event_time:
            raise ValueError("fact receive time cannot precede event time")
        return self


REQUIRED_FACT_TYPES = frozenset(FactType)
