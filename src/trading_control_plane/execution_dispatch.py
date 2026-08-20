from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from trading_control_plane.domain import DomainRejected
from trading_control_plane.freqtrade import FreqtradeWorkerClient
from trading_control_plane.freqtrade_contracts import (
    FreqtradeEntryCommand,
    FreqtradeExitCommand,
    freqtrade_pair,
)
from trading_control_plane.service import PreparedFreqtradeWorkerBinding, TradingService

WorkerResolver = Callable[[PreparedFreqtradeWorkerBinding], FreqtradeWorkerClient]


@dataclass(frozen=True, slots=True)
class ExecuteIntent:
    intent_id: UUID
    campaign_id: UUID
    actor_id: UUID
    execution_scope: str
    owner_id: str
    fencing_token: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ExecuteIntentResult:
    environment: str
    worker_name: str
    trade_id: str | None
    replayed: bool
    venue_order_fact_id: UUID | None = None
    protection_id: UUID | None = None
    pair: str | None = None
    is_open: bool | None = None


@dataclass(frozen=True, slots=True)
class SyncProtection:
    campaign_id: UUID
    actor_id: UUID
    execution_scope: str
    owner_id: str
    fencing_token: int
    symbol: str


@dataclass(frozen=True, slots=True)
class SyncProtectionResult:
    protection_id: UUID
    environment: str
    worker_name: str
    trade_id: str


def unbound_worker_status(
    workers: tuple[FreqtradeWorkerClient, ...],
    *,
    workers_enabled: bool,
) -> list[dict[str, object]]:
    return [
        {
            "name": worker.spec.name,
            "venue": worker.spec.venue,
            "backend": "FREQTRADE",
            "status": "UNBOUND" if workers_enabled else "DISABLED",
            "reason_code": (
                "ACCOUNT_BINDING_REQUIRED" if workers_enabled else "FREQTRADE_WORKERS_DISABLED"
            ),
            "scope_status": "UNBOUND_LEGACY_DEFAULT",
            "hip3_dexes": list(worker.spec.hip3_dexes),
            "order_send": False,
        }
        for worker in workers
        if worker.spec.exchange_account_id is None
    ]


def _execution_mode(
    execution_scope: str,
) -> tuple[str, str, Literal["TESTNET", "LIVE"]]:
    scope_parts = execution_scope.split(":")
    if len(scope_parts) != 3:
        raise DomainRejected(
            "EXECUTION_SCOPE_INVALID",
            "execution requires environment:account:venue",
        )
    environment, _account_id, venue = scope_parts
    if environment not in {"TESTNET", "LIVE"}:
        raise DomainRejected(
            "EXECUTION_SCOPE_INVALID",
            "execution requires an explicit TESTNET or LIVE scope",
        )
    return environment, venue, "LIVE" if environment == "LIVE" else "TESTNET"


def execute_intent(
    request: ExecuteIntent,
    *,
    service: TradingService,
    worker_resolver: WorkerResolver,
    require_enabled: Callable[[], None],
    live_leverage: Decimal,
    clock: Callable[[], datetime],
) -> ExecuteIntentResult:
    environment, _venue, expected_mode = _execution_mode(request.execution_scope)
    require_enabled()
    binding = service.freqtrade_worker_binding(
        actor_id=request.actor_id,
        execution_scope=request.execution_scope,
        owner_id=request.owner_id,
        fencing_token=request.fencing_token,
        now=clock(),
        campaign_id=request.campaign_id,
    )
    worker = worker_resolver(binding)
    command = service.prepare_freqtrade_order(
        request.intent_id,
        request.actor_id,
        request.execution_scope,
        request.owner_id,
        request.fencing_token,
        hip3_dexes=binding.hip3_dexes,
        leverage=live_leverage,
        now=clock(),
    )
    probe_result = worker.probe(expected_mode=expected_mode, required_pair=command.pair)
    runtime_error = service.record_freqtrade_runtime_probe(
        binding,
        probe_result=probe_result,
        error_code=None,
        now=clock(),
    )
    if runtime_error is not None:
        raise DomainRejected(
            runtime_error,
            "Freqtrade runtime identity changed and requires a new explicit verification",
        )
    service.validate_freqtrade_worker_binding(binding)
    external_trade_id = None
    if isinstance(command, FreqtradeExitCommand) or command.position_adjustment:
        external_trade_id = service.freqtrade_dispatch_external_id(
            request.intent_id,
            actor_id=request.actor_id,
            execution_scope=request.execution_scope,
        )
        if external_trade_id is None:
            current = worker.find_open_trade(pair=command.pair)
            if current is None:
                raise DomainRejected(
                    "FREQTRADE_POSITION_NOT_FOUND",
                    "Freqtrade has no unique open trade for the controlled exit",
                )
            external_trade_id = current.trade_id
            if isinstance(command, FreqtradeExitCommand):
                if command.close_all and current.amount > command.max_quantity:
                    raise DomainRejected(
                        "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                        "Freqtrade open amount exceeds the frozen full-exit boundary",
                    )
                if not command.close_all and current.amount <= command.max_quantity:
                    raise DomainRejected(
                        "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                        "partial reduction must remain below the exact open trade amount",
                    )
            elif current.side != command.side:
                raise DomainRejected(
                    "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                    "approved Add direction differs from the exact open trade",
                )
    dispatch = service.start_freqtrade_dispatch(
        request.intent_id,
        actor_id=request.actor_id,
        execution_scope=request.execution_scope,
        owner_id=request.owner_id,
        fencing_token=request.fencing_token,
        binding=binding,
        command=command,
        external_trade_id=external_trade_id,
        idempotency_key=request.idempotency_key,
        now=clock(),
    )
    if dispatch.mode == "COMPLETED":
        return ExecuteIntentResult(
            environment=environment,
            worker_name=worker.spec.name,
            trade_id=dispatch.external_trade_id,
            replayed=True,
        )
    try:
        service.validate_freqtrade_worker_binding(binding)
        if isinstance(command, FreqtradeEntryCommand):
            trade = (
                worker.force_enter(
                    command,
                    expected_mode=expected_mode,
                    trade_id=dispatch.external_trade_id,
                    dispatch_started_at=dispatch.started_at,
                )
                if dispatch.mode == "SEND"
                else worker.recover_entry(
                    command,
                    trade_id=dispatch.external_trade_id,
                    dispatch_started_at=dispatch.started_at,
                )
            )
        else:
            assert dispatch.external_trade_id is not None
            trade = (
                worker.force_exit(
                    dispatch.external_trade_id,
                    command,
                    dispatch_started_at=dispatch.started_at,
                )
                if dispatch.mode == "SEND"
                else worker.recover_exit(
                    dispatch.external_trade_id,
                    command,
                    dispatch_started_at=dispatch.started_at,
                )
            )
    except DomainRejected as exc:
        if exc.code in {
            "FREQTRADE_LIVE_OUTCOME_UNKNOWN",
            "FREQTRADE_PROTECTION_UNCONFIRMED",
        }:
            service.record_freqtrade_unknown(
                request.intent_id,
                request.actor_id,
                request.execution_scope,
                request.owner_id,
                request.fencing_token,
                command,
                exc.code,
                now=clock(),
            )
        raise
    fact_id = service.record_freqtrade_order(
        request.intent_id,
        request.actor_id,
        request.execution_scope,
        request.owner_id,
        request.fencing_token,
        command,
        trade,
        dispatch_started_at=dispatch.started_at,
        now=clock(),
    )
    protection_id = None
    if isinstance(command, FreqtradeEntryCommand):
        protection_id = service.record_freqtrade_protection(
            request.campaign_id,
            request.actor_id,
            request.execution_scope,
            request.owner_id,
            request.fencing_token,
            trade,
            now=clock(),
        )
    return ExecuteIntentResult(
        environment=environment,
        worker_name=worker.spec.name,
        trade_id=trade.trade_id,
        replayed=dispatch.mode != "SEND",
        venue_order_fact_id=fact_id,
        protection_id=protection_id,
        pair=trade.pair,
        is_open=trade.is_open,
    )


def sync_protection(
    request: SyncProtection,
    *,
    service: TradingService,
    worker_resolver: WorkerResolver,
    require_enabled: Callable[[], None],
    clock: Callable[[], datetime],
) -> SyncProtectionResult:
    environment, venue, expected_mode = _execution_mode(request.execution_scope)
    require_enabled()
    binding = service.freqtrade_worker_binding(
        actor_id=request.actor_id,
        execution_scope=request.execution_scope,
        owner_id=request.owner_id,
        fencing_token=request.fencing_token,
        now=clock(),
        campaign_id=request.campaign_id,
    )
    worker = worker_resolver(binding)
    pair = freqtrade_pair(venue, request.symbol, hip3_dexes=binding.hip3_dexes)
    worker.probe(expected_mode=expected_mode, required_pair=pair)
    service.validate_freqtrade_worker_binding(binding)
    trade = worker.find_open_trade(pair=pair)
    if trade is None:
        raise DomainRejected(
            "FREQTRADE_POSITION_NOT_FOUND",
            "Freqtrade has no unique open trade to verify protection",
        )
    protection_id = service.record_freqtrade_protection(
        request.campaign_id,
        request.actor_id,
        request.execution_scope,
        request.owner_id,
        request.fencing_token,
        trade,
        now=clock(),
    )
    return SyncProtectionResult(
        protection_id=protection_id,
        environment=environment,
        worker_name=worker.spec.name,
        trade_id=trade.trade_id,
    )


__all__ = [
    "ExecuteIntent",
    "ExecuteIntentResult",
    "SyncProtection",
    "SyncProtectionResult",
    "execute_intent",
    "sync_protection",
    "unbound_worker_status",
]
