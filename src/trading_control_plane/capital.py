from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from trading_control_plane.domain import CapitalDirection, DomainRejected, ExecutionEnvironment


@dataclass(frozen=True)
class CapitalTransferCommand:
    capital_transfer_id: UUID
    environment: ExecutionEnvironment
    direction: CapitalDirection
    source_id: str
    destination_id: str
    asset: str
    network: str
    destination_reference: str
    gross_amount: Decimal
    max_fee: Decimal
    min_received: Decimal


@dataclass(frozen=True)
class CapitalTransferSubmission:
    external_transfer_id: str
    status: str
    observed_at: datetime


class MockCapitalTransferAdapter:
    """Deterministic test adapter. It has no network, signer, wallet, or venue credentials."""

    def submit(
        self, command: CapitalTransferCommand, *, now: datetime
    ) -> CapitalTransferSubmission:
        if command.environment is ExecutionEnvironment.LIVE:
            raise DomainRejected(
                "CAPITAL_TRANSFER_LIVE_DISABLED",
                "the mock adapter can never submit a LIVE capital transfer",
            )
        identity = hashlib.sha256(
            f"{command.capital_transfer_id}:{command.source_id}:{command.destination_id}:"
            f"{command.asset}:{command.network}:{command.gross_amount}".encode()
        ).hexdigest()[:24]
        return CapitalTransferSubmission(
            external_transfer_id=f"mock-capital-{identity}",
            status="SUBMITTED",
            observed_at=now,
        )
