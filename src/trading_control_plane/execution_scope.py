from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from trading_control_plane.domain import (
    CapitalTransferStatus,
    ExecutionEnvironment,
    OrderIntentStatus,
    ReservationStatus,
)
from trading_control_plane.rejections import reject

DEFAULT_SENDER_LEASE_DURATION = timedelta(minutes=1)

ACTIVE_INTENT_STATUSES = {
    OrderIntentStatus.PENDING.value,
    OrderIntentStatus.RESERVED.value,
    OrderIntentStatus.READY.value,
    OrderIntentStatus.DISPATCHING.value,
    OrderIntentStatus.SENT.value,
    OrderIntentStatus.PARTIALLY_FILLED.value,
    OrderIntentStatus.UNKNOWN.value,
}

OCCUPIED_RESERVATION_STATUSES = {
    ReservationStatus.RESERVED.value,
    ReservationStatus.OPEN.value,
    ReservationStatus.UNKNOWN.value,
}

MAX_FACT_CLOCK_SKEW = timedelta(seconds=30)

RELEASABLE_INTENT_STATUSES = {
    OrderIntentStatus.PENDING.value,
    OrderIntentStatus.RESERVED.value,
    OrderIntentStatus.READY.value,
    OrderIntentStatus.SENT.value,
    OrderIntentStatus.PARTIALLY_FILLED.value,
}


def advisory_lock_key(caller_id: str, operation: str, key: str) -> int:
    digest = hashlib.sha256(f"{caller_id}:{operation}:{key}".encode()).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


RISK_CAPACITY_LOCK_KEY = advisory_lock_key("trading", "risk-capacity", "global")

OCCUPIED_CAPITAL_STATUSES = {
    CapitalTransferStatus.SOURCE_RESERVED.value,
    CapitalTransferStatus.SUBMITTED.value,
    CapitalTransferStatus.IN_FLIGHT.value,
    CapitalTransferStatus.DESTINATION_CONFIRMED.value,
    CapitalTransferStatus.UNKNOWN.value,
    CapitalTransferStatus.MANUAL_REQUIRED.value,
}


def scope_key(environment: str, account_id: str, venue: str) -> str:
    return f"{environment}:{account_id}:{venue}"


def scope_parts(execution_scope: str) -> tuple[ExecutionEnvironment, str, str]:
    parts = execution_scope.split(":")
    if len(parts) == 3:
        try:
            environment = ExecutionEnvironment(parts[0])
        except ValueError:
            reject("EXECUTION_SCOPE_INVALID", "execution scope environment is invalid")
        account_id, venue = parts[1:]
    else:
        reject(
            "EXECUTION_SCOPE_INVALID",
            "execution scope must be environment:account:venue",
        )
    if not account_id or not venue or account_id.strip() != account_id or venue.strip() != venue:
        reject("EXECUTION_SCOPE_INVALID", "execution scope must contain non-empty exact parts")
    return environment, account_id, venue


def fact_is_stale(observed_at: datetime, now: datetime, max_age: timedelta) -> bool:
    """Apply the single bounded-clock freshness rule used by writes and projections."""

    return observed_at > now + MAX_FACT_CLOCK_SKEW or now - observed_at > max_age
