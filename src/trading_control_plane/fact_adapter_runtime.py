from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import uvicorn
from fastapi import FastAPI

from trading_control_plane.adapters.facts import (
    CcxtProFactAdapter,
    Environment,
    ExchangeFactSnapshot,
    FactAdapterRegistry,
    FactAdapterScope,
    FactStreamSupervisor,
    Venue,
)
from trading_control_plane.config import Settings, get_settings
from trading_control_plane.database import Database
from trading_control_plane.domain import DomainRejected, ExecutionEnvironment
from trading_control_plane.fact_adapter_api import create_fact_adapter_app
from trading_control_plane.fact_adapter_ingestion import normalize_fact_adapter_snapshot
from trading_control_plane.freqtrade import (
    FreqtradeRpcMessage,
    FreqtradeTrade,
    FreqtradeWorkerClient,
    FreqtradeWorkerSpec,
    freqtrade_pair,
)
from trading_control_plane.logging import configure_logging
from trading_control_plane.service import (
    PreparedFreqtradeWorkerBinding,
    PreparedRuntimeAccountBinding,
    TradingService,
)

logger = logging.getLogger(__name__)

BindingProvider = Callable[[], Sequence[PreparedRuntimeAccountBinding]]
SymbolProvider = Callable[[str], Sequence[str]]
ExchangeFactory = Callable[[FactAdapterScope, Mapping[str, str], Mapping[str, Any]], Any]
SnapshotConsumer = Callable[[PreparedRuntimeAccountBinding, ExchangeFactSnapshot], None]
FreqtradeBindingProvider = Callable[[], Sequence[PreparedFreqtradeWorkerBinding]]
FreqtradeEventConsumer = Callable[
    [PreparedFreqtradeWorkerBinding, FreqtradeRpcMessage, FreqtradeTrade | None],
    None,
]
FreqtradeClientFactory = Callable[[PreparedFreqtradeWorkerBinding], FreqtradeWorkerClient]


@dataclass(slots=True)
class _RunningAdapter:
    version: tuple[int, int]
    supervisor: FactStreamSupervisor
    task: asyncio.Task[None]


class FactAdapterRuntime:
    """Maintain one CCXT Pro connection per exact database-bound account scope."""

    def __init__(
        self,
        *,
        settings: Settings,
        registry: FactAdapterRegistry,
        binding_provider: BindingProvider,
        symbol_provider: SymbolProvider,
        exchange_factory: ExchangeFactory | None = None,
        snapshot_consumer: SnapshotConsumer | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.binding_provider = binding_provider
        self.symbol_provider = symbol_provider
        self.exchange_factory = exchange_factory
        self.snapshot_consumer = snapshot_consumer
        self._running: dict[str, _RunningAdapter] = {}
        self._stop = asyncio.Event()
        self._reconcile_task: asyncio.Task[None] | None = None

    def _scope(self, binding: PreparedRuntimeAccountBinding) -> FactAdapterScope:
        pairs: list[str] = []
        for symbol in self.symbol_provider(binding.venue):
            try:
                pair = freqtrade_pair(
                    binding.venue,
                    symbol,
                    hip3_dexes=binding.hip3_dexes,
                )
            except DomainRejected:
                continue
            if pair not in pairs:
                pairs.append(pair)
        if not pairs:
            raise DomainRejected(
                "FACT_ADAPTER_SCOPE_EMPTY",
                "the exact account has no supported active instrument subscription",
            )
        return FactAdapterScope(
            workspace_id=str(binding.workspace_id),
            team_id=str(binding.team_id),
            account_id=binding.account_id,
            venue=cast(Venue, binding.venue),
            environment=cast(Environment, binding.environment),
            symbols=tuple(pairs),
            account_mode=binding.account_mode,
        )

    async def _start_binding(self, binding: PreparedRuntimeAccountBinding) -> str:
        scope = self._scope(binding)
        kwargs: dict[str, Any] = {
            "credentials": binding.credentials,
        }
        if self.exchange_factory is not None:
            kwargs["exchange_factory"] = self.exchange_factory
        adapter = CcxtProFactAdapter(scope, **kwargs)
        adapter.validate_capabilities()
        snapshot_callback = None
        if self.snapshot_consumer is not None:
            consumer = self.snapshot_consumer

            async def snapshot_callback(snapshot: ExchangeFactSnapshot) -> None:
                await asyncio.to_thread(consumer, binding, snapshot)

        supervisor = FactStreamSupervisor(
            self.registry,
            adapter,
            reconciliation_seconds=self.settings.fact_adapter_reconciliation_seconds,
            fallback_seconds=self.settings.fact_adapter_fallback_seconds,
            snapshot_callback=snapshot_callback,
        )
        await self.registry.register(adapter)
        task = asyncio.create_task(
            supervisor.run(),
            name=f"fact-adapter:{scope.key}",
        )
        await self.registry.attach_task(scope.key, task)
        self._running[scope.key] = _RunningAdapter(
            version=(binding.account_version, binding.credential_version),
            supervisor=supervisor,
            task=task,
        )
        return scope.key

    async def reconcile_once(self) -> None:
        bindings = tuple(await asyncio.to_thread(self.binding_provider))
        desired: dict[str, PreparedRuntimeAccountBinding] = {}
        for binding in bindings:
            scope = self._scope(binding)
            if scope.key in desired:
                raise DomainRejected(
                    "FACT_ADAPTER_SCOPE_CONFLICT",
                    "database bindings contain a duplicate exact account scope",
                )
            desired[scope.key] = binding
        for key in tuple(self._running):
            running = self._running[key]
            current_binding = desired.get(key)
            version = (
                None
                if current_binding is None
                else (current_binding.account_version, current_binding.credential_version)
            )
            if version != running.version or running.task.done():
                running.supervisor.stop()
                await self.registry.unregister(key)
                self._running.pop(key, None)
        for key, binding in desired.items():
            if key not in self._running:
                await self._start_binding(binding)

    async def _reconcile_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Fact adapter binding reconciliation failed",
                    extra={
                        "event": "fact_adapter_binding_reconciliation_failed",
                        "component": "fact-adapter",
                        "error_type": type(exc).__name__,
                    },
                )
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.settings.fact_adapter_binding_refresh_seconds,
                )
            except TimeoutError:
                continue

    async def start(self) -> None:
        if self._reconcile_task is not None:
            return
        await self.reconcile_once()
        self._reconcile_task = asyncio.create_task(
            self._reconcile_forever(), name="fact-adapter:binding-reconciliation"
        )

    async def close(self) -> None:
        self._stop.set()
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            await asyncio.gather(self._reconcile_task, return_exceptions=True)
            self._reconcile_task = None
        for running in self._running.values():
            running.supervisor.stop()
        self._running.clear()
        await self.registry.close()


@dataclass(slots=True)
class _RunningFreqtradeRpc:
    version: tuple[int, int]
    task: asyncio.Task[None]


class FreqtradeRpcRuntime:
    """Supervise one RPC lifecycle stream for each exact account-bound worker."""

    _TRANSACTION_EVENTS = frozenset(
        {
            "entry",
            "entry_fill",
            "entry_cancel",
            "exit",
            "exit_fill",
            "exit_cancel",
            "protection_trigger",
            "protection_trigger_global",
        }
    )

    def __init__(
        self,
        *,
        binding_provider: FreqtradeBindingProvider,
        event_consumer: FreqtradeEventConsumer,
        client_factory: FreqtradeClientFactory,
        refresh_seconds: float,
    ) -> None:
        self.binding_provider = binding_provider
        self.event_consumer = event_consumer
        self.client_factory = client_factory
        self.refresh_seconds = refresh_seconds
        self._running: dict[str, _RunningFreqtradeRpc] = {}
        self._stop = asyncio.Event()
        self._reconcile_task: asyncio.Task[None] | None = None

    @staticmethod
    def _payload_trade_id(message: FreqtradeRpcMessage) -> str | None:
        payload: Mapping[str, Any] = message.payload
        nested = payload.get("trade")
        candidates: list[Any] = [
            payload.get("trade_id"),
            payload.get("tradeid"),
            payload.get("id"),
        ]
        if isinstance(nested, dict):
            candidates.extend((nested.get("trade_id"), nested.get("tradeid"), nested.get("id")))
        for value in candidates:
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    async def _record_recovery_trades(
        self,
        binding: PreparedFreqtradeWorkerBinding,
        client: FreqtradeWorkerClient,
    ) -> None:
        trades = await asyncio.to_thread(client.open_trades)
        for trade in trades:
            message = FreqtradeRpcMessage(
                event_type="reconcile",
                payload={"trade_id": trade.trade_id, "is_open": trade.is_open},
                observed_at=datetime.now(UTC),
            )
            await asyncio.to_thread(self.event_consumer, binding, message, trade)

    async def _consume_binding(self, binding: PreparedFreqtradeWorkerBinding) -> None:
        delay = 1.0
        expected_mode = cast(Any, binding.worker_mode)
        while not self._stop.is_set():
            client = self.client_factory(binding)
            try:
                await asyncio.to_thread(client.probe, expected_mode=expected_mode)
                await self._record_recovery_trades(binding, client)
                delay = 1.0
                async for message in client.rpc_messages():
                    trade: FreqtradeTrade | None = None
                    if message.event_type in self._TRANSACTION_EVENTS:
                        trade_id = self._payload_trade_id(message)
                        if trade_id is not None:
                            try:
                                trade = await asyncio.to_thread(client.trade, trade_id)
                            except DomainRejected:
                                trade = None
                    await asyncio.to_thread(self.event_consumer, binding, message, trade)
                    if self._stop.is_set():
                        return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Freqtrade RPC stream disconnected; query-only recovery will retry",
                    extra={
                        "event": "freqtrade_rpc_disconnected",
                        "component": "fact-adapter",
                        "exchange_account_id": str(binding.exchange_account_id),
                        "error_type": type(exc).__name__,
                    },
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    delay = min(delay * 2, 30.0)

    async def reconcile_once(self) -> None:
        bindings = tuple(await asyncio.to_thread(self.binding_provider))
        desired: dict[str, PreparedFreqtradeWorkerBinding] = {}
        for binding in bindings:
            key = str(binding.exchange_account_id)
            if key in desired:
                raise DomainRejected(
                    "FREQTRADE_RPC_SCOPE_CONFLICT",
                    "database bindings contain duplicate workers for one exchange account",
                )
            desired[key] = binding
        for key in tuple(self._running):
            current = desired.get(key)
            running = self._running[key]
            version = None if current is None else (current.account_version, current.auth_version)
            if version != running.version or running.task.done():
                running.task.cancel()
                await asyncio.gather(running.task, return_exceptions=True)
                self._running.pop(key, None)
        for key, binding in desired.items():
            if key not in self._running:
                task = asyncio.create_task(
                    self._consume_binding(binding),
                    name=f"freqtrade-rpc:{key}",
                )
                self._running[key] = _RunningFreqtradeRpc(
                    version=(binding.account_version, binding.auth_version),
                    task=task,
                )

    async def _reconcile_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Freqtrade RPC binding reconciliation failed",
                    extra={
                        "event": "freqtrade_rpc_binding_reconciliation_failed",
                        "component": "fact-adapter",
                        "error_type": type(exc).__name__,
                    },
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.refresh_seconds)
            except TimeoutError:
                continue

    async def start(self) -> None:
        if self._reconcile_task is not None:
            return
        await self.reconcile_once()
        self._reconcile_task = asyncio.create_task(
            self._reconcile_forever(),
            name="freqtrade-rpc:binding-reconciliation",
        )

    async def close(self) -> None:
        self._stop.set()
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            await asyncio.gather(self._reconcile_task, return_exceptions=True)
            self._reconcile_task = None
        for running in self._running.values():
            running.task.cancel()
        await asyncio.gather(
            *(running.task for running in self._running.values()),
            return_exceptions=True,
        )
        self._running.clear()


def _bootstrap_symbol_provider(settings: Settings) -> SymbolProvider:
    bootstrap = {
        "BINANCE": settings.runtime_binance_symbol,
        "HYPERLIQUID": settings.runtime_hyperliquid_symbol,
        "OKX": settings.runtime_okx_symbol,
        "BYBIT": settings.runtime_bybit_symbol,
    }

    def provider(venue: str) -> tuple[str, ...]:
        # Account-wide position and order reads add every externally discovered
        # live symbol.  Starting from the full persisted market catalog would
        # turn a small account fact stream into hundreds of WebSocket ticker
        # subscriptions and can make the venue close the connection.
        return (bootstrap[venue],)

    return provider


def create_runtime_app(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    exchange_factory: ExchangeFactory | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    resolved_settings.validate_runtime_security()
    if not resolved_settings.fact_adapter_enabled:
        raise ValueError("TRADING_FACT_ADAPTER_ENABLED must be true")
    assert resolved_settings.fact_adapter_bearer_token is not None
    resolved_database = database or Database(resolved_settings.database_url)
    service = TradingService(
        resolved_database,
        credential_encryption_key=resolved_settings.credential_encryption_key,
    )
    registry = FactAdapterRegistry()

    def persist_snapshot(
        binding: PreparedRuntimeAccountBinding,
        snapshot: ExchangeFactSnapshot,
    ) -> None:
        service.ingest_normalized_read_only_account_snapshot(
            binding.account_id,
            binding.service_principal_id,
            normalize_fact_adapter_snapshot(snapshot),
            venue=binding.venue,
            environment=ExecutionEnvironment(binding.environment),
            runtime_binding=binding,
            now=datetime.now(UTC),
        )

    runtime = FactAdapterRuntime(
        settings=resolved_settings,
        registry=registry,
        binding_provider=service.runtime_account_bindings,
        symbol_provider=_bootstrap_symbol_provider(resolved_settings),
        exchange_factory=exchange_factory,
        snapshot_consumer=persist_snapshot,
    )
    rpc_runtime: FreqtradeRpcRuntime | None = None
    if resolved_settings.freqtrade_workers_enabled:

        def freqtrade_client(binding: PreparedFreqtradeWorkerBinding) -> FreqtradeWorkerClient:
            return FreqtradeWorkerClient(
                FreqtradeWorkerSpec(
                    name=binding.worker_name,
                    venue=cast(Any, binding.venue),
                    base_url=binding.worker_url,
                    username=binding.username,
                    password=binding.password,
                    ws_token=binding.ws_token,
                    hip3_dexes=binding.hip3_dexes,
                    exchange_account_id=str(binding.exchange_account_id),
                    team_id=str(binding.team_id),
                    account_id=binding.account_id,
                ),
                timeout_seconds=resolved_settings.freqtrade_timeout_seconds,
                confirmation_timeout_seconds=(
                    resolved_settings.freqtrade_confirmation_timeout_seconds
                ),
            )

        def consume_freqtrade_event(
            binding: PreparedFreqtradeWorkerBinding,
            message: FreqtradeRpcMessage,
            trade: FreqtradeTrade | None,
        ) -> None:
            service.record_freqtrade_rpc_event(
                binding,
                message,
                trade,
                now=datetime.now(UTC),
            )

        rpc_runtime = FreqtradeRpcRuntime(
            binding_provider=service.runtime_freqtrade_worker_bindings,
            event_consumer=consume_freqtrade_event,
            client_factory=freqtrade_client,
            refresh_seconds=resolved_settings.fact_adapter_binding_refresh_seconds,
        )
    app = create_fact_adapter_app(
        registry=registry,
        bearer_token=resolved_settings.fact_adapter_bearer_token,
        stale_after_seconds=resolved_settings.fact_adapter_stale_after_seconds,
    )

    @app.on_event("startup")
    async def start_runtime() -> None:
        await runtime.start()
        if rpc_runtime is not None:
            await rpc_runtime.start()

    @app.on_event("shutdown")
    async def close_runtime() -> None:
        if rpc_runtime is not None:
            await rpc_runtime.close()
        await runtime.close()
        if database is None:
            resolved_database.engine.dispose()

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live", "component": "fact-adapter"}

    app.state.fact_adapter_registry = registry
    app.state.fact_adapter_runtime = runtime
    app.state.freqtrade_rpc_runtime = rpc_runtime
    return app


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the isolated CCXT Pro fact adapter")
    parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    app = create_runtime_app(settings=settings)
    uvicorn.run(
        app,
        host=settings.fact_adapter_host,
        port=settings.fact_adapter_port,
        log_config=None,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
