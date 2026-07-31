from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Literal
from uuid import UUID

from trading_control_plane.binance import (
    BinancePortfolioMarginReadOnlyClient,
    BinanceReadOnlyClient,
)
from trading_control_plane.config import Settings, get_settings
from trading_control_plane.database import Database
from trading_control_plane.domain import DomainRejected, ExecutionEnvironment, ReconciliationStatus
from trading_control_plane.hyperliquid import (
    HyperliquidReadOnlyClient,
    resolve_hyperliquid_main_account,
)
from trading_control_plane.logging import configure_logging
from trading_control_plane.notilt import NoTiltGateway, NoTiltUsdValuator
from trading_control_plane.perptape import (
    PerptapeClient,
    merge_incomplete_perptape_candidates,
    validate_perptape_datetime,
)
from trading_control_plane.perptape_stream import PerptapeStreamWorker
from trading_control_plane.queries import TradingQueries
from trading_control_plane.service import TradingService

logger = logging.getLogger(__name__)

SourceStatus = Literal["SUCCESS", "FAILED", "SKIPPED"]
BinanceReader = BinanceReadOnlyClient | BinancePortfolioMarginReadOnlyClient


@dataclass(frozen=True, slots=True)
class SourceSyncResult:
    status: SourceStatus
    items_observed: int = 0
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeSyncReport:
    started_at: str
    completed_at: str
    sources: dict[str, SourceSyncResult]
    net_worth: dict[str, Any]

    @property
    def successful(self) -> bool:
        return bool(self.sources) and all(
            item.status == "SUCCESS" for item in self.sources.values()
        )

    @property
    def capital_sources_successful(self) -> bool:
        binance = self.sources.get("BINANCE")
        hyperliquid = self.sources.get("HYPERLIQUID")
        vaults = [result for source, result in self.sources.items() if source.startswith("NOTILT")]
        return (
            binance is not None
            and binance.status == "SUCCESS"
            and hyperliquid is not None
            and hyperliquid.status == "SUCCESS"
            and bool(vaults)
            and all(item.status == "SUCCESS" for item in vaults)
        )

    @property
    def ready_for_new_risk(self) -> bool:
        return self.capital_sources_successful and self.net_worth.get("complete") is True

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "source_sync_successful": self.successful,
            "capital_sources_successful": self.capital_sources_successful,
            "ready_for_new_risk": self.ready_for_new_risk,
            "sources": {key: asdict(value) for key, value in self.sources.items()},
            "net_worth": self.net_worth,
        }


class RuntimeSyncWorker:
    """Continuously refresh read-only external facts without any venue write capability."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        perptape: PerptapeClient,
        binance: BinanceReader,
        hyperliquid: HyperliquidReadOnlyClient,
        notilt: NoTiltGateway,
        notilt_valuator: NoTiltUsdValuator,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self.database = database
        self.perptape = perptape
        self.binance = binance
        self.hyperliquid = hyperliquid
        self.notilt = notilt
        self.notilt_valuator = notilt_valuator
        self.clock = clock
        self.service = TradingService(database)
        self.queries = TradingQueries(database)
        self._perptape_stream_thread: threading.Thread | None = None

    @property
    def dependencies_in_use(self) -> bool:
        thread = getattr(self, "_perptape_stream_thread", None)
        return thread is not None and thread.is_alive()

    def _perptape_stop_timeout_seconds(self) -> float:
        transport_timeout = self.settings.perptape_timeout_seconds
        close_timeout = min(transport_timeout, 5)
        return transport_timeout + close_timeout + 2

    def _require_scope_match(self, scope: str, actor_id: UUID, now: datetime) -> None:
        reconciliation_id = self.service.reconcile_scope(scope, actor_id, now=now)
        if self.service.reconciliation_status(reconciliation_id) is not ReconciliationStatus.MATCH:
            raise DomainRejected(
                "RUNTIME_RECONCILIATION_NOT_MATCH",
                "runtime venue synchronization requires a computed reconciliation MATCH",
            )

    def _record_binance(self, actor_id: UUID, now: datetime) -> int:
        account_id = self.settings.runtime_binance_account_id
        if account_id is None:
            raise DomainRejected(
                "RUNTIME_BINANCE_TARGET_MISSING",
                "runtime Binance sync requires an internal account ID",
            )
        snapshot = self.binance.read_snapshot(self.settings.runtime_binance_symbol, now=now)
        persisted = self.service.ingest_binance_read_only_snapshot(
            account_id,
            actor_id,
            snapshot,
            environment=ExecutionEnvironment(self.settings.binance_fact_environment),
            now=now,
        )
        scope = f"{self.settings.binance_fact_environment}:{account_id}:BINANCE"
        self._require_scope_match(scope, actor_id, now)
        return len(persisted)

    def _record_perptape(self, actor_id: UUID, now: datetime) -> int:
        validate_perptape_datetime(now)
        base = self.queries.perptape_feed()
        if (
            base is not None
            and base.contract_version == self.settings.perptape_contract_version
            and now < base.next_allowed_at
        ):
            return len(base.candidates)
        feed = self.perptape.refresh(now=now)
        current = self.queries.perptape_feed()
        if current is not None and current.contract_version == feed.contract_version:
            feed = merge_incomplete_perptape_candidates(
                feed,
                (
                    candidate
                    for candidate in current.candidates
                    if candidate.readiness == "INCOMPLETE"
                ),
            )
        self.service.record_perptape_feed(
            actor_id,
            feed,
            now=now,
            base_snapshot=base,
        )
        return len(feed.candidates)

    def _record_hyperliquid(self, actor_id: UUID, now: datetime) -> int:
        account_id = self.settings.runtime_hyperliquid_account_id
        if account_id is None:
            raise DomainRejected(
                "RUNTIME_HYPERLIQUID_TARGET_MISSING",
                "runtime Hyperliquid sync requires an internal account ID",
            )
        if self.hyperliquid.fact_environment != self.settings.hyperliquid_fact_environment:
            raise DomainRejected(
                "HYPERLIQUID_ENVIRONMENT_MISMATCH",
                "Hyperliquid API host does not match the configured fact environment",
            )
        snapshot = self.hyperliquid.read_snapshot(
            self.settings.runtime_hyperliquid_symbol,
            now=now,
        )
        environment = ExecutionEnvironment(self.settings.hyperliquid_fact_environment)
        persisted = self.service.ingest_hyperliquid_read_only_snapshot(
            account_id,
            actor_id,
            snapshot,
            environment=environment,
            now=now,
        )
        scope = f"{environment.value}:{account_id}:HYPERLIQUID"
        self._require_scope_match(scope, actor_id, now)
        return len(persisted)

    def _record_notilt(self, actor_id: UUID, chain_id: int, now: datetime) -> int:
        agent = self.settings.notilt_agent_address
        vault = self.settings.notilt_vaults.get(chain_id)
        if agent is None or vault is None:
            raise DomainRejected(
                "NOTILT_NOT_CONFIGURED",
                "NoTilt public agent and configured Vault are required",
            )
        snapshot = self.notilt.read_vault(chain_id, vault, agent)
        valuations = {
            budget.asset: self.notilt_valuator.value(
                budget.asset,
                budget.balance,
                now=now,
            )
            for budget in snapshot.budgets
        }
        return len(
            self.service.record_notilt_vault_snapshot(
                actor_id=actor_id,
                snapshot=snapshot,
                valuations=valuations,
                now=now,
            )
        )

    @staticmethod
    def _attempt(
        name: str,
        action: Callable[[], int],
        results: dict[str, SourceSyncResult],
    ) -> None:
        try:
            items_observed = action()
        except DomainRejected as exc:
            results[name] = SourceSyncResult("FAILED", error_code=exc.code)
            logger.warning(
                "Runtime read-only source synchronization failed",
                extra={
                    "event": "runtime_sync_source_failed",
                    "component": name,
                    "error_code": exc.code,
                },
            )
        else:
            results[name] = SourceSyncResult("SUCCESS", items_observed=items_observed)

    def run_once(self) -> RuntimeSyncReport:
        started_at = self.clock()
        validate_perptape_datetime(started_at)
        actor = self.queries.service_principal_by_username(
            self.settings.runtime_sync_service_username
        )
        results: dict[str, SourceSyncResult] = {}

        if self.settings.perptape_api_key:
            perptape_actor = self.queries.service_principal_by_username(
                self.settings.perptape_service_username
            )
            self._attempt(
                "PERPTAPE",
                lambda: self._record_perptape(perptape_actor.user_id, started_at),
                results,
            )
        else:
            results["PERPTAPE"] = SourceSyncResult("SKIPPED")

        if self.settings.binance_read_only_enabled:
            self._attempt(
                "BINANCE",
                lambda: self._record_binance(actor.user_id, started_at),
                results,
            )
        else:
            results["BINANCE"] = SourceSyncResult("SKIPPED")

        if self.settings.hyperliquid_read_only_enabled:
            self._attempt(
                "HYPERLIQUID",
                lambda: self._record_hyperliquid(actor.user_id, started_at),
                results,
            )
        else:
            results["HYPERLIQUID"] = SourceSyncResult("SKIPPED")

        if self.settings.notilt_enabled:
            for chain_id in sorted(self.settings.notilt_vaults):
                self._attempt(
                    f"NOTILT:{chain_id}",
                    partial(
                        self._record_notilt,
                        actor.user_id,
                        chain_id,
                        started_at,
                    ),
                    results,
                )
        if not self.settings.notilt_enabled or not self.settings.notilt_vaults:
            results["NOTILT"] = SourceSyncResult("SKIPPED")

        completed_at = self.clock()
        validate_perptape_datetime(completed_at)
        net_worth = self.queries.capital_center(actor.user_id)["net_worth"]
        return RuntimeSyncReport(
            started_at=started_at.isoformat(),
            completed_at=completed_at.isoformat(),
            sources=results,
            net_worth=net_worth,
        )

    def run_forever(self, stop_event: threading.Event) -> None:
        stream: PerptapeStreamWorker | None = None
        stream_thread: threading.Thread | None = None
        if self.settings.perptape_websocket_enabled:
            perptape_actor = self.queries.service_principal_by_username(
                self.settings.perptape_service_username
            )
            stream = PerptapeStreamWorker(
                client=self.perptape,
                websocket_url=self.settings.perptape_websocket_url,
                api_key=self.settings.perptape_api_key,
                contract_version=self.settings.perptape_contract_version,
                load_snapshot=self.queries.perptape_feed,
                record_snapshot=lambda feed, now, base_snapshot: self.service.record_perptape_feed(
                    perptape_actor.user_id,
                    feed,
                    now=now,
                    base_snapshot=base_snapshot,
                ),
                timeout_seconds=self.settings.perptape_timeout_seconds,
                heartbeat_timeout_seconds=(
                    self.settings.perptape_websocket_heartbeat_timeout_seconds
                ),
                reconciliation_interval_seconds=(
                    self.settings.perptape_websocket_reconciliation_seconds
                ),
                reconnect_initial_seconds=(
                    self.settings.perptape_websocket_reconnect_initial_seconds
                ),
                reconnect_max_seconds=self.settings.perptape_websocket_reconnect_max_seconds,
                max_reconnect_attempts=(self.settings.perptape_websocket_max_reconnect_attempts),
                clock=self.clock,
            )
            stream_thread = threading.Thread(
                target=stream.run_forever,
                args=(stop_event,),
                name="perptape-stream",
            )
            self._perptape_stream_thread = stream_thread
            stream_thread.start()
        try:
            while not stop_event.is_set():
                report = self.run_once()
                logger.info(
                    "Runtime read-only synchronization cycle completed",
                    extra={
                        "event": "runtime_sync_cycle_completed",
                        "result": "READY" if report.ready_for_new_risk else "DEGRADED",
                        "component": "runtime-sync",
                    },
                )
                stop_event.wait(self.settings.runtime_sync_interval_seconds)
        finally:
            stop_event.set()
            if stream_thread is not None:
                stream_thread.join(timeout=self._perptape_stop_timeout_seconds())
                if stream_thread.is_alive():
                    raise DomainRejected(
                        "PERPTAPE_STREAM_STOP_TIMEOUT",
                        "Perptape WebSocket did not stop within its bounded timeout",
                    )
                self._perptape_stream_thread = None
        if stream is not None and stream.fatal_error_code is not None:
            raise DomainRejected(
                stream.fatal_error_code,
                "Perptape WebSocket stopped after bounded failures",
            )


def build_runtime_worker(settings: Settings, database: Database) -> RuntimeSyncWorker:
    perptape = PerptapeClient(
        base_url=settings.perptape_base_url,
        api_key=settings.perptape_api_key,
        contract_version=settings.perptape_contract_version,
        cache_ttl=timedelta(seconds=settings.perptape_cache_seconds),
        timeout_seconds=settings.perptape_timeout_seconds,
    )
    binance: BinanceReader
    if settings.binance_account_mode == "PORTFOLIO_MARGIN":
        binance = BinancePortfolioMarginReadOnlyClient(
            base_url=settings.binance_live_base_url,
            api_key=settings.binance_api_key,
            api_secret=settings.binance_api_secret,
            recv_window_ms=settings.binance_recv_window_ms,
        )
    else:
        binance = BinanceReadOnlyClient(
            base_url=settings.binance_futures_base_url,
            api_key=settings.binance_api_key,
            api_secret=settings.binance_api_secret,
            recv_window_ms=settings.binance_recv_window_ms,
        )
    hyperliquid_account = resolve_hyperliquid_main_account(
        base_url=settings.hyperliquid_base_url,
        account_address=settings.hyperliquid_account_address,
        api_wallet_address=(
            settings.hyperliquid_api_wallet_address
            if settings.hyperliquid_read_only_enabled
            else None
        ),
    )
    hyperliquid = HyperliquidReadOnlyClient(
        base_url=settings.hyperliquid_base_url,
        account_address=settings.hyperliquid_subaccount_address or hyperliquid_account,
        dex=settings.hyperliquid_core_dex,
    )
    return RuntimeSyncWorker(
        settings=settings,
        database=database,
        perptape=perptape,
        binance=binance,
        hyperliquid=hyperliquid,
        notilt=NoTiltGateway(timeout_seconds=settings.notilt_gateway_timeout_seconds),
        notilt_valuator=NoTiltUsdValuator(),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run read-only trading fact synchronization")
    parser.add_argument("--once", action="store_true", help="run one synchronization cycle")
    args = parser.parse_args(argv)
    settings = get_settings()
    settings.validate_runtime_security()
    configure_logging(settings.log_level)
    if not args.once and not settings.runtime_sync_enabled:
        raise SystemExit("TRADING_RUNTIME_SYNC_ENABLED must be true for continuous mode")
    database = Database(settings.database_url)
    worker: RuntimeSyncWorker | None = None
    try:
        ready, reason = database.is_ready()
        if not ready:
            raise SystemExit(f"database is not ready: {reason}")
        worker = build_runtime_worker(settings, database)
        if args.once:
            report = worker.run_once()
            print(json.dumps(report.to_dict(), separators=(",", ":"), sort_keys=True))
            return 0 if report.ready_for_new_risk else 1
        stop_event = threading.Event()
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signal_number, lambda _signal, _frame: stop_event.set())
        try:
            worker.run_forever(stop_event)
        except DomainRejected as exc:
            logger.error(
                "Runtime read-only synchronization stopped",
                extra={
                    "event": "runtime_sync_stopped",
                    "component": "runtime-sync",
                    "error_code": exc.code,
                },
            )
            return 1
        return 0
    finally:
        if worker is None or not bool(getattr(worker, "dependencies_in_use", False)):
            database.dispose()
        else:
            logger.critical(
                "Database disposal skipped while a background worker is still active",
                extra={
                    "event": "runtime_dependency_release_deferred",
                    "component": "runtime-sync",
                    "error_code": "PERPTAPE_STREAM_STOP_TIMEOUT",
                },
            )


if __name__ == "__main__":
    raise SystemExit(main())
