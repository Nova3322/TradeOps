from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from trading_control_plane.domain import DomainRejected
from trading_control_plane.runtime_contracts import ConnectionProbeResult
from trading_control_plane.service import TradingService


class ConnectionVerifier(Protocol):
    def verify(
        self,
        *,
        workspace_id: str,
        team_id: str,
        account_id: str,
        venue: str,
        environment: str,
        account_mode: str,
        credentials: Mapping[str, str],
        now: datetime,
    ) -> ConnectionProbeResult: ...


@dataclass(slots=True)
class _Flight:
    completed: threading.Event
    error: Exception | None = None


class ExchangeConnectionVerification:
    """Single-flight an account probe; the service owns version-bound cooldown reuse."""

    def __init__(self, verifier: ConnectionVerifier) -> None:
        self._verifier = verifier
        self._lock = threading.Lock()
        self._flights: dict[UUID, _Flight] = {}

    def verify(
        self,
        *,
        service: TradingService,
        exchange_account_id: UUID,
        actor_id: UUID,
        expected_version: int,
        idempotency_key: str,
        clock: Callable[[], datetime],
    ) -> dict[str, object]:
        with self._lock:
            flight = self._flights.get(exchange_account_id)
            if flight is None:
                flight = _Flight(threading.Event())
                self._flights[exchange_account_id] = flight
                leader = True
            else:
                leader = False
        if not leader:
            flight.completed.wait()
            if flight.error is not None:
                raise flight.error
            _command, replay = service.prepare_exchange_account_connection_verification(
                exchange_account_id,
                actor_id=actor_id,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                now=clock(),
            )
            assert replay is not None
            return replay
        try:
            command, replay = service.prepare_exchange_account_connection_verification(
                exchange_account_id,
                actor_id=actor_id,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                now=clock(),
            )
            if replay is not None:
                return replay
            assert command is not None
            outcome = self._verifier.verify(
                workspace_id=str(command.workspace_id),
                team_id=str(command.team_id),
                account_id=command.account_id,
                venue=command.venue,
                environment=command.environment,
                account_mode=command.account_mode,
                credentials=command.credentials,
                now=clock(),
            )
            if outcome.error_code == "BINANCE_RATE_LIMITED_COOLDOWN":
                raise DomainRejected(
                    "BINANCE_CONNECTION_RETRY_DEFERRED",
                    "Binance connection verification was not sent while the current "
                    "process cooldown is active",
                    metadata=outcome.diagnostics,
                )
            return service.record_exchange_account_connection_verification(
                command,
                outcome,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                now=clock(),
            )
        except Exception as exc:
            flight.error = exc
            raise
        finally:
            with self._lock:
                self._flights.pop(exchange_account_id, None)
                flight.completed.set()


__all__ = ["ExchangeConnectionVerification"]
