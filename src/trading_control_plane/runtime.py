from __future__ import annotations

import argparse
import hashlib
import json
import logging
import signal
import threading
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from functools import partial
from typing import Any, Literal
from uuid import UUID

from trading_control_plane.binance import (
    BinancePortfolioMarginReadOnlyClient,
    BinanceReadOnlyClient,
)
from trading_control_plane.binance_state import DatabaseBinanceRequestState
from trading_control_plane.bybit import BybitReadOnlyClient
from trading_control_plane.config import Settings, get_settings
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    Direction,
    DomainRejected,
    ExecutionEnvironment,
    ProposalSource,
    ProposalStatus,
    ReconciliationStatus,
    RiskTier,
)
from trading_control_plane.hyperliquid import HyperliquidReadOnlyClient
from trading_control_plane.logging import configure_logging
from trading_control_plane.notilt import NoTiltGateway, NoTiltUsdValuator
from trading_control_plane.okx import OkxReadOnlyClient
from trading_control_plane.perptape import (
    PERPTAPE_OPERATIONAL_TIME_HEADROOM,
    PerptapeCandidate,
    PerptapeClient,
    PerptapeFeedSnapshot,
    merge_incomplete_perptape_candidates,
    normalize_perptape_operational_datetime,
)
from trading_control_plane.perptape_stream import PerptapeStreamWorker
from trading_control_plane.queries import TradingQueries
from trading_control_plane.service import (
    PreparedPerptapeRuntimeBinding,
    PreparedRuntimeAccountBinding,
    TradingService,
)

logger = logging.getLogger(__name__)

SourceStatus = Literal["SUCCESS", "FAILED", "SKIPPED"]
BinanceReader = BinanceReadOnlyClient | BinancePortfolioMarginReadOnlyClient
TIMEFRAME_ORDER = {"1h": 0, "4h": 1, "1d": 2, "1w": 3}
AMOUNT_QUANTUM = Decimal("0.000000000000000001")


def _resonance_decimal_identity(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == 0:
        return "0"
    return format(normalized, "f")


def perptape_resonance_signal_identity(
    candidates: Sequence[PerptapeCandidate],
) -> str | None:
    """Identify one continuing breakout without using refresh timestamps or candidate IDs."""

    if not candidates or any(item.threshold is None for item in candidates):
        return None
    primary = candidates[0]
    return "|".join(
        [
            primary.source_contract_version,
            primary.venue,
            primary.source_exchange,
            primary.symbol,
            primary.canonical_symbol,
            primary.direction.value,
            *(
                f"{item.timeframe}:{_resonance_decimal_identity(item.threshold)}"
                for item in candidates
                if item.threshold is not None
            ),
        ]
    )


def perptape_resonance_groups(
    candidates: Sequence[PerptapeCandidate],
    *,
    now: datetime,
    max_age: timedelta,
    minimum_timeframes: int = 3,
) -> tuple[tuple[PerptapeCandidate, ...], ...]:
    """Return exact-instrument, same-direction fresh groups without guessing conflicts."""

    if minimum_timeframes < 3 or minimum_timeframes > len(TIMEFRAME_ORDER):
        raise ValueError("minimum_timeframes must be between 3 and 4")
    grouped: dict[tuple[str, str, Direction], dict[str, PerptapeCandidate]] = {}
    conflicted: set[tuple[str, str, Direction]] = set()
    cutoff = now - max_age
    future_limit = now + timedelta(seconds=30)
    for candidate in candidates:
        if (
            candidate.readiness != "READY"
            or candidate.data_health != "CURRENT"
            or candidate.observed_at < cutoff
            or candidate.observed_at > future_limit
        ):
            continue
        key = (candidate.venue, candidate.symbol, candidate.direction)
        existing = grouped.setdefault(key, {}).get(candidate.timeframe)
        if existing is not None and existing.candidate_id != candidate.candidate_id:
            conflicted.add(key)
            continue
        grouped[key][candidate.timeframe] = candidate
    results: list[tuple[PerptapeCandidate, ...]] = []
    for key, by_timeframe in grouped.items():
        values = tuple(
            sorted(by_timeframe.values(), key=lambda item: TIMEFRAME_ORDER[item.timeframe])
        )
        if key in conflicted or len(values) < minimum_timeframes:
            continue
        if (
            len({item.canonical_symbol for item in values}) != 1
            or len({item.source_exchange for item in values}) != 1
            or len({item.source_contract_version for item in values}) != 1
        ):
            continue
        results.append(values)
    return tuple(
        sorted(
            results,
            key=lambda items: (items[0].venue, items[0].symbol, items[0].direction.value),
        )
    )


@dataclass(frozen=True, slots=True)
class SourceSyncResult:
    status: SourceStatus
    items_observed: int = 0
    error_code: str | None = None
    retry_at: str | None = None


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


@dataclass(frozen=True, slots=True)
class RuntimeBindingSyncReport:
    started_at: str
    completed_at: str
    sources: dict[str, SourceSyncResult]

    @property
    def successful(self) -> bool:
        return bool(self.sources) and all(
            item.status == "SUCCESS" for item in self.sources.values()
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "binding_source": "DATABASE_ENVELOPE",
            "source_sync_successful": self.successful,
            "sources": {key: asdict(value) for key, value in self.sources.items()},
        }


@dataclass(slots=True)
class _BoundPerptapeStream:
    binding: PreparedPerptapeRuntimeBinding
    worker: RuntimeSyncWorker
    stream: PerptapeStreamWorker
    stop_event: threading.Event
    thread: threading.Thread

    @property
    def version(self) -> tuple[UUID, UUID, int, int]:
        return (
            self.binding.team_id,
            self.binding.service_principal_id,
            self.binding.source_version,
            self.binding.credential_version,
        )


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
        self.service = TradingService(
            database,
            credential_encryption_key=settings.credential_encryption_key,
        )
        self.queries = TradingQueries(database)
        self._perptape_stream_thread: threading.Thread | None = None
        self._database_account_reader: OkxReadOnlyClient | BybitReadOnlyClient | None = None
        self._database_account_scope: tuple[str, str] | None = None

    def install_database_account_reader(
        self,
        binding: PreparedRuntimeAccountBinding,
    ) -> None:
        """Install one short-lived DB-envelope reader without copying secrets to Settings."""

        if binding.venue == "OKX":
            self._database_account_reader = OkxReadOnlyClient(
                api_key=binding.credentials["api_key"],
                api_secret=binding.credentials["api_secret"],
                passphrase=binding.credentials["passphrase"],
            )
        elif binding.venue == "BYBIT":
            self._database_account_reader = BybitReadOnlyClient(
                api_key=binding.credentials["api_key"],
                api_secret=binding.credentials["api_secret"],
            )
        else:
            raise DomainRejected(
                "RUNTIME_BINDING_WORKER_SCOPE_MISMATCH",
                "the normalized account reader is restricted to OKX and Bybit",
            )
        self._database_account_scope = (binding.venue, binding.account_id)

    @property
    def dependencies_in_use(self) -> bool:
        thread = getattr(self, "_perptape_stream_thread", None)
        return thread is not None and thread.is_alive()

    def _perptape_stop_timeout_seconds(self) -> float:
        transport_timeout = self.settings.perptape_timeout_seconds
        close_timeout = min(transport_timeout, 5)
        return transport_timeout + close_timeout + 2

    def _runtime_time_headroom(self, *, continuous: bool) -> timedelta:
        headroom = PERPTAPE_OPERATIONAL_TIME_HEADROOM
        if getattr(self.settings, "perptape_api_key", None) is not None:
            headroom = max(
                headroom,
                timedelta(
                    seconds=getattr(
                        self.settings,
                        "perptape_cache_seconds",
                        int(PERPTAPE_OPERATIONAL_TIME_HEADROOM.total_seconds()),
                    )
                ),
            )
        if continuous:
            headroom = max(
                headroom,
                timedelta(
                    seconds=getattr(
                        self.settings,
                        "runtime_sync_interval_seconds",
                        int(PERPTAPE_OPERATIONAL_TIME_HEADROOM.total_seconds()),
                    )
                ),
            )
        return headroom

    def _normalize_runtime_time(self, value: datetime, *, continuous: bool) -> datetime:
        return normalize_perptape_operational_datetime(
            value,
            required_headroom=self._runtime_time_headroom(continuous=continuous),
        )

    def _runtime_clock(self, *, continuous: bool) -> datetime:
        return self._normalize_runtime_time(self.clock(), continuous=continuous)

    def _require_scope_match(
        self,
        scope: str,
        actor_id: UUID,
        now: datetime,
        *,
        source_error_code: str | None = None,
    ) -> None:
        reconciliation_id = self.service.reconcile_scope(scope, actor_id, now=now)
        if source_error_code is not None:
            raise DomainRejected(
                source_error_code,
                "runtime current facts were refreshed but history supplementation is incomplete",
            )
        if self.service.reconciliation_status(reconciliation_id) is not ReconciliationStatus.MATCH:
            raise DomainRejected(
                "RUNTIME_RECONCILIATION_NOT_MATCH",
                "runtime venue synchronization requires a computed reconciliation MATCH",
            )

    def _record_binance(
        self,
        actor_id: UUID,
        now: datetime,
        *,
        runtime_binding: PreparedRuntimeAccountBinding | None = None,
    ) -> int:
        account_id = self.settings.runtime_binance_account_id
        if account_id is None:
            raise DomainRejected(
                "RUNTIME_BINANCE_TARGET_MISSING",
                "runtime Binance sync requires an internal account ID",
            )
        venue_instruments = self.binance.read_active_instruments()
        worker_instruments = tuple(
            instrument
            for instrument in venue_instruments
            if instrument.quote_currency == "USDT" and instrument.collateral_currency == "USDT"
        )
        if not worker_instruments:
            raise DomainRejected(
                "FREQTRADE_BINANCE_SCOPE_EMPTY",
                "Binance returned no active USDT-margined perpetual supported by the bound "
                "Freqtrade worker",
            )
        excluded = len(venue_instruments) - len(worker_instruments)
        if excluded:
            logger.info(
                "Binance instruments outside the bound Freqtrade collateral scope were skipped",
                extra={
                    "event": "freqtrade_catalog_scope_filtered",
                    "component": "BINANCE",
                    "excluded_count": excluded,
                    "included_count": len(worker_instruments),
                },
            )
        self.service.synchronize_active_venue_instruments(
            actor_id=actor_id,
            account_id=account_id,
            venue="BINANCE",
            instruments=worker_instruments,
            runtime_binding=runtime_binding,
            now=now,
        )
        cursor_installer = getattr(self.binance, "set_history_cursors", None)
        if cursor_installer is not None:
            cursor_installer(
                self.service.binance_history_cursors(
                    account_id,
                    actor_id,
                    environment=ExecutionEnvironment(self.settings.binance_fact_environment),
                )
            )
        snapshots = self.binance.read_account_snapshots(
            (self.settings.runtime_binance_symbol,), now=now
        )
        persisted = self.service.ingest_binance_read_only_account_snapshot(
            account_id,
            actor_id,
            snapshots,
            environment=ExecutionEnvironment(self.settings.binance_fact_environment),
            runtime_binding=runtime_binding,
            now=now,
        )
        scope = f"{self.settings.binance_fact_environment}:{account_id}:BINANCE"
        self._require_scope_match(
            scope,
            actor_id,
            now,
            source_error_code=(
                None
                if persisted["history_error_code"] is None
                else f"BINANCE_HISTORY_INCOMPLETE:{persisted['history_error_code']}"
            ),
        )
        return int(persisted["positions_covered"])

    def _record_perptape(
        self,
        actor_id: UUID,
        now: datetime,
        *,
        runtime_binding: PreparedPerptapeRuntimeBinding | None = None,
    ) -> int:
        now = self._normalize_runtime_time(now, continuous=False)
        base = self.queries.perptape_feed(actor_id)
        if (
            base is not None
            and base.contract_version == self.settings.perptape_contract_version
            and now < base.next_allowed_at
        ):
            self._create_resonance_proposals(
                actor_id,
                base,
                now=now,
                runtime_binding=runtime_binding,
            )
            return len(base.candidates)
        feed = self.perptape.refresh(now=now)
        current = self.queries.perptape_feed(actor_id)
        if current is not None and current.contract_version == feed.contract_version:
            feed = merge_incomplete_perptape_candidates(
                feed,
                (
                    candidate
                    for candidate in current.candidates
                    if candidate.readiness == "INCOMPLETE"
                ),
                pending_max_age=timedelta(
                    seconds=self.settings.perptape_websocket_reconciliation_seconds
                ),
            )
        self._record_perptape_snapshot(
            actor_id,
            feed,
            now=now,
            base_snapshot=base,
            runtime_binding=runtime_binding,
        )
        return len(feed.candidates)

    def _record_perptape_snapshot(
        self,
        actor_id: UUID,
        feed: PerptapeFeedSnapshot,
        *,
        now: datetime,
        base_snapshot: PerptapeFeedSnapshot | None,
        runtime_binding: PreparedPerptapeRuntimeBinding | None = None,
    ) -> None:
        self.service.record_perptape_feed(
            actor_id,
            feed,
            now=now,
            base_snapshot=base_snapshot,
            runtime_binding=runtime_binding,
        )
        self._create_resonance_proposals(
            actor_id,
            feed,
            now=now,
            runtime_binding=runtime_binding,
        )

    def _create_resonance_proposals(
        self,
        actor_id: UUID,
        feed: PerptapeFeedSnapshot,
        *,
        now: datetime,
        runtime_binding: PreparedPerptapeRuntimeBinding | None = None,
    ) -> int:
        if runtime_binding is not None:
            self.service.validate_perptape_runtime_binding(runtime_binding)
        config = self.service.proposal_automation_config(actor_id)
        if config is None or not config["auto_proposal_enabled"]:
            return 0
        expired_duplicates = self.service.expire_duplicate_active_system_proposals(
            actor_id=actor_id,
            strategy_id="perptape-resonance",
            now=now,
        )
        if expired_duplicates:
            logger.warning(
                "expired duplicate active Perptape resonance proposals",
                extra={
                    "event": "perptape_resonance_duplicates_expired",
                    "component": "perptape",
                    "expired_count": expired_duplicates,
                },
            )
        account_id = str(config["account_id"])
        minimum_timeframes = int(config["auto_proposal_min_timeframes"])
        notional = Decimal(str(config["notional"]))
        max_risk = Decimal(str(config["max_risk"]))
        invalidation_bps = int(config["invalidation_bps"])
        expires_in_minutes = int(config["expires_in_minutes"])
        max_age = timedelta(
            seconds=(
                self.settings.runtime_sync_interval_seconds
                + int(self.settings.perptape_timeout_seconds)
                + 30
            )
        )
        groups = perptape_resonance_groups(
            feed.candidates,
            now=now,
            max_age=max_age,
            minimum_timeframes=minimum_timeframes,
        )
        created = 0
        for candidates in groups:
            primary = max(
                candidates,
                key=lambda item: (item.observed_at, item.triggered_at or item.observed_at),
            )
            try:
                instrument_id = self.queries.instrument_id_by_venue_symbol(
                    primary.venue, primary.symbol
                )
            except DomainRejected as exc:
                if exc.code != "INSTRUMENT_UNAVAILABLE":
                    raise
                logger.info(
                    "Perptape resonance skipped outside the exact Instrument Catalog",
                    extra={
                        "event": "perptape_resonance_proposal_skipped",
                        "component": "perptape",
                        "error_code": exc.code,
                    },
                )
                continue
            signal_identity = perptape_resonance_signal_identity(candidates)
            if signal_identity is None:
                logger.info(
                    "Perptape resonance skipped without stable breakout thresholds",
                    extra={
                        "event": "perptape_resonance_proposal_skipped",
                        "component": "perptape",
                        "error_code": "PERPTAPE_SIGNAL_IDENTITY_INCOMPLETE",
                    },
                )
                continue
            source_candidate_id = "ptr_" + hashlib.sha256(signal_identity.encode()).hexdigest()[:33]
            quantity = (notional / primary.reference_price).quantize(
                AMOUNT_QUANTUM, rounding=ROUND_DOWN
            )
            if quantity <= 0:
                continue
            invalidation_factor = Decimal(invalidation_bps) / Decimal(10_000)
            invalidation_price = (
                primary.reference_price
                * (
                    Decimal(1) - invalidation_factor
                    if primary.direction is Direction.LONG
                    else Decimal(1) + invalidation_factor
                )
            ).quantize(AMOUNT_QUANTUM, rounding=ROUND_DOWN)
            timeframes = [item.timeframe for item in candidates]
            proposal_id = self.service.create_proposal(
                actor_id=actor_id,
                source=ProposalSource.SYSTEM,
                risk_tier=RiskTier(str(config["risk_tier"])),
                account_id=account_id,
                venue=primary.venue,
                instrument_id=instrument_id,
                direction=primary.direction,
                quantity=quantity,
                max_risk=max_risk,
                expires_at=now + timedelta(minutes=expires_in_minutes),
                idempotency_key=f"perptape-resonance:{source_candidate_id}",
                strategy_id="perptape-resonance",
                strategy_version=(
                    f"{feed.contract_version}:policy-v{config['version']}:min-{minimum_timeframes}"
                ),
                environment=ExecutionEnvironment.LIVE,
                source_candidate_id=source_candidate_id,
                source_link=primary.detail_url,
                source_observed_at=max(item.observed_at for item in candidates),
                source_readiness="READY",
                details={
                    "candidate": primary.to_dict(),
                    "resonance_candidates": [item.to_dict() for item in candidates],
                    "resonance_timeframes": timeframes,
                    "resonance_threshold": minimum_timeframes,
                    "default_config_id": config["config_id"],
                    "default_config_version": config["version"],
                    "configuration_mode": "AUTO_POLICY",
                    "signal_source_version": (
                        None if runtime_binding is None else runtime_binding.source_version
                    ),
                    "trigger_price": str(primary.reference_price),
                    "invalidation_price": str(invalidation_price),
                    "initial_quantity": str(quantity),
                    "allow_auto_add": False,
                    "requested_adds": 0,
                    "add_trigger_price": None,
                    "rationale": (
                        "创建提案时，Perptape 中同一精确合约、同一方向在 "  # noqa: RUF001
                        f"{'、'.join(timeframes)} 同时突破。{config['rationale']} "
                        "来源与参数已冻结，仅创建待审核提案；不会自动授权或下单。"  # noqa: RUF001
                    ),
                },
                idempotency_payload={
                    "source_candidate_id": source_candidate_id,
                    "signal_identity": signal_identity,
                },
                deduplicate_active_system_scope=True,
                perptape_runtime_binding=runtime_binding,
                now=now,
            )
            detail = self.queries.proposal_detail(actor_id, proposal_id, now=now)
            if detail["status"] == ProposalStatus.DRAFT.value:
                self.service.submit_proposal(
                    proposal_id,
                    actor_id,
                    perptape_runtime_binding=runtime_binding,
                    now=now,
                )
                created += 1
        return created

    def _record_hyperliquid(
        self,
        actor_id: UUID,
        now: datetime,
        *,
        runtime_binding: PreparedRuntimeAccountBinding | None = None,
    ) -> int:
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
        self.service.synchronize_active_venue_instruments(
            actor_id=actor_id,
            account_id=account_id,
            venue="HYPERLIQUID",
            instruments=self.hyperliquid.read_active_instruments(),
            hip3_dexes=self.settings.hyperliquid_hip3_dexes,
            runtime_binding=runtime_binding,
            now=now,
        )
        snapshots = self.hyperliquid.read_account_snapshots(
            (self.settings.runtime_hyperliquid_symbol,),
            now=now,
        )
        environment = ExecutionEnvironment(self.settings.hyperliquid_fact_environment)
        persisted = self.service.ingest_hyperliquid_read_only_account_snapshot(
            account_id,
            actor_id,
            snapshots,
            environment=environment,
            runtime_binding=runtime_binding,
            now=now,
        )
        scope = f"{environment.value}:{account_id}:HYPERLIQUID"
        self._require_scope_match(
            scope,
            actor_id,
            now,
            source_error_code=(
                None
                if persisted["history_error_code"] is None
                else f"HYPERLIQUID_HISTORY_INCOMPLETE:{persisted['history_error_code']}"
            ),
        )
        return int(persisted["positions_covered"])

    def _record_database_normalized_venue(
        self,
        binding: PreparedRuntimeAccountBinding,
        now: datetime,
    ) -> int:
        reader = self._database_account_reader
        if reader is None or self._database_account_scope != (
            binding.venue,
            binding.account_id,
        ):
            raise DomainRejected(
                "RUNTIME_BINDING_WORKER_SCOPE_MISMATCH",
                "the runtime worker lacks the frozen OKX or Bybit account reader",
            )
        instruments = reader.read_active_instruments()
        self.service.synchronize_active_venue_instruments(
            actor_id=binding.service_principal_id,
            account_id=binding.account_id,
            venue=binding.venue,
            instruments=instruments,
            runtime_binding=binding,
            now=now,
        )
        snapshots = reader.read_account_snapshots((), now=now)
        if binding.venue == "OKX":
            persisted = self.service.ingest_okx_read_only_account_snapshot(
                binding.account_id,
                binding.service_principal_id,
                snapshots,
                environment=ExecutionEnvironment(binding.environment),
                runtime_binding=binding,
                now=now,
            )
        elif binding.venue == "BYBIT":
            persisted = self.service.ingest_bybit_read_only_account_snapshot(
                binding.account_id,
                binding.service_principal_id,
                snapshots,
                environment=ExecutionEnvironment(binding.environment),
                runtime_binding=binding,
                now=now,
            )
        else:
            raise DomainRejected(
                "RUNTIME_BINDING_WORKER_SCOPE_MISMATCH",
                "normalized runtime facts are restricted to OKX and Bybit",
            )
        self._require_scope_match(
            f"LIVE:{binding.account_id}:{binding.venue}",
            binding.service_principal_id,
            now,
            source_error_code=(
                None
                if persisted["history_error_code"] is None
                else f"{binding.venue}_HISTORY_INCOMPLETE:{persisted['history_error_code']}"
            ),
        )
        return int(persisted["positions_covered"])

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
            retry_at = None
            if exc.metadata is not None and exc.metadata.get("next_retry_at") is not None:
                retry_at = str(exc.metadata["next_retry_at"])
            results[name] = SourceSyncResult("FAILED", error_code=exc.code, retry_at=retry_at)
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

    def _rate_limit_cooldown(
        self,
        source_name: str,
        *,
        actor_id: UUID,
        account_id: str | None = None,
        venue: str | None = None,
        now: datetime,
    ) -> SourceSyncResult | None:
        health = self.queries.runtime_source_health(
            actor_id,
            source_name,
            account_id=account_id,
            venue=venue,
        )
        if health is None or "RATE_LIMITED" not in str(health.get("error_code") or ""):
            return None
        retry_value = health.get("retry_at")
        if not isinstance(retry_value, str):
            return None
        try:
            retry_at = datetime.fromisoformat(retry_value)
        except ValueError:
            return None
        if retry_at <= now:
            return None
        return SourceSyncResult(
            "SKIPPED",
            error_code=f"{source_name}_RATE_LIMITED_COOLDOWN",
        )

    def run_once(
        self,
        *,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> RuntimeSyncReport:
        started_at = self._normalize_runtime_time(
            self.clock() if started_at is None else started_at,
            continuous=False,
        )
        completed_at = self._normalize_runtime_time(
            self.clock() if completed_at is None else completed_at,
            continuous=False,
        )
        actor = self.queries.service_principal_by_username(
            self.settings.runtime_sync_service_username
        )
        results: dict[str, SourceSyncResult] = {}

        if self.settings.binance_read_only_enabled:
            cooldown = self._rate_limit_cooldown(
                "BINANCE",
                actor_id=actor.user_id,
                account_id=self.settings.runtime_binance_account_id,
                venue="BINANCE",
                now=started_at,
            )
            if cooldown is None:
                self._attempt(
                    "BINANCE",
                    lambda: self._record_binance(actor.user_id, started_at),
                    results,
                )
            else:
                results["BINANCE"] = cooldown
        else:
            results["BINANCE"] = SourceSyncResult("SKIPPED")

        if self.settings.hyperliquid_read_only_enabled:
            cooldown = self._rate_limit_cooldown(
                "HYPERLIQUID",
                actor_id=actor.user_id,
                account_id=self.settings.runtime_hyperliquid_account_id,
                venue="HYPERLIQUID",
                now=started_at,
            )
            if cooldown is None:
                self._attempt(
                    "HYPERLIQUID",
                    lambda: self._record_hyperliquid(actor.user_id, started_at),
                    results,
                )
            else:
                results["HYPERLIQUID"] = cooldown
        else:
            results["HYPERLIQUID"] = SourceSyncResult("SKIPPED")

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

        authoritative_live_accounts = {
            venue: account_id
            for venue, account_id in (
                ("BINANCE", self.settings.runtime_binance_account_id),
                ("HYPERLIQUID", self.settings.runtime_hyperliquid_account_id),
            )
            if account_id
        }
        net_worth = self.queries.capital_center(
            actor.user_id,
            authoritative_live_accounts=authoritative_live_accounts,
        )["net_worth"]
        self.service.record_runtime_source_health(
            actor.user_id,
            {source: asdict(result) for source, result in results.items()},
            scopes={
                "BINANCE": (
                    None
                    if self.settings.runtime_binance_account_id is None
                    else (self.settings.runtime_binance_account_id, "BINANCE")
                ),
                "HYPERLIQUID": (
                    None
                    if self.settings.runtime_hyperliquid_account_id is None
                    else (self.settings.runtime_hyperliquid_account_id, "HYPERLIQUID")
                ),
            },
            require_exact_account_scope=False,
            now=completed_at,
        )
        return RuntimeSyncReport(
            started_at=started_at.isoformat(),
            completed_at=completed_at.isoformat(),
            sources=results,
            net_worth=net_worth,
        )

    def run_bound_account_once(
        self,
        binding: PreparedRuntimeAccountBinding,
        *,
        now: datetime,
    ) -> SourceSyncResult:
        if (
            binding.service_principal_username != self.settings.runtime_sync_service_username
            or (
                binding.venue in {"BINANCE", "HYPERLIQUID"}
                and binding.account_id
                not in {
                    self.settings.runtime_binance_account_id,
                    self.settings.runtime_hyperliquid_account_id,
                }
            )
            or (
                binding.venue in {"OKX", "BYBIT"}
                and self._database_account_scope != (binding.venue, binding.account_id)
            )
        ):
            raise DomainRejected(
                "RUNTIME_BINDING_WORKER_SCOPE_MISMATCH",
                "the runtime worker does not match the frozen account binding",
            )
        result: SourceSyncResult
        if binding.venue == "BINANCE":
            cooldown = self._rate_limit_cooldown(
                "BINANCE",
                actor_id=binding.service_principal_id,
                account_id=binding.account_id,
                venue=binding.venue,
                now=now,
            )
            if cooldown is not None:
                result = cooldown
            else:
                results: dict[str, SourceSyncResult] = {}
                self._attempt(
                    "BINANCE",
                    lambda: self._record_binance(
                        binding.service_principal_id,
                        now,
                        runtime_binding=binding,
                    ),
                    results,
                )
                result = results["BINANCE"]
        elif binding.venue == "HYPERLIQUID":
            cooldown = self._rate_limit_cooldown(
                "HYPERLIQUID",
                actor_id=binding.service_principal_id,
                account_id=binding.account_id,
                venue=binding.venue,
                now=now,
            )
            if cooldown is not None:
                result = cooldown
            else:
                results = {}
                self._attempt(
                    "HYPERLIQUID",
                    lambda: self._record_hyperliquid(
                        binding.service_principal_id,
                        now,
                        runtime_binding=binding,
                    ),
                    results,
                )
                result = results["HYPERLIQUID"]
        elif binding.venue in {"OKX", "BYBIT"}:
            cooldown = self._rate_limit_cooldown(
                binding.venue,
                actor_id=binding.service_principal_id,
                account_id=binding.account_id,
                venue=binding.venue,
                now=now,
            )
            if cooldown is not None:
                result = cooldown
            else:
                results = {}
                self._attempt(
                    binding.venue,
                    lambda: self._record_database_normalized_venue(binding, now),
                    results,
                )
                result = results[binding.venue]
        else:
            raise DomainRejected("RUNTIME_BINDING_INVALID", "runtime venue is unsupported")
        self.service.record_runtime_source_health(
            binding.service_principal_id,
            {binding.venue: asdict(result)},
            scopes={binding.venue: (binding.account_id, binding.venue)},
            runtime_account_binding=binding,
            now=now,
        )
        return result

    def run_bound_perptape_once(
        self,
        binding: PreparedPerptapeRuntimeBinding,
        *,
        now: datetime,
    ) -> SourceSyncResult:
        if binding.service_principal_username != self.settings.perptape_service_username:
            raise DomainRejected(
                "SIGNAL_RUNTIME_WORKER_SCOPE_MISMATCH",
                "the runtime worker does not match the frozen Perptape binding",
            )
        results: dict[str, SourceSyncResult] = {}
        self._attempt(
            "PERPTAPE",
            lambda: self._record_perptape(
                binding.service_principal_id,
                now,
                runtime_binding=binding,
            ),
            results,
        )
        result = results["PERPTAPE"]
        self.service.record_runtime_source_health(
            binding.service_principal_id,
            {"PERPTAPE": asdict(result)},
            perptape_runtime_binding=binding,
            now=now,
        )
        return result

    def build_bound_perptape_stream(
        self,
        binding: PreparedPerptapeRuntimeBinding,
    ) -> PerptapeStreamWorker:
        """Build one stream whose reads and writes remain frozen to one Team source."""

        if (
            binding.service_principal_username != self.settings.perptape_service_username
            or binding.api_key != self.settings.perptape_api_key
        ):
            raise DomainRejected(
                "SIGNAL_RUNTIME_WORKER_SCOPE_MISMATCH",
                "the runtime worker does not match the frozen Perptape binding",
            )
        self.service.validate_perptape_runtime_binding(binding)
        return PerptapeStreamWorker(
            client=self.perptape,
            websocket_url=self.settings.perptape_websocket_url,
            api_key=binding.api_key,
            contract_version=self.settings.perptape_contract_version,
            load_snapshot=lambda: self.queries.perptape_feed(binding.service_principal_id),
            record_snapshot=lambda feed, now, base_snapshot: self._record_perptape_snapshot(
                binding.service_principal_id,
                feed,
                now=now,
                base_snapshot=base_snapshot,
                runtime_binding=binding,
            ),
            timeout_seconds=self.settings.perptape_timeout_seconds,
            heartbeat_timeout_seconds=(self.settings.perptape_websocket_heartbeat_timeout_seconds),
            reconciliation_interval_seconds=(
                self.settings.perptape_websocket_reconciliation_seconds
            ),
            reconnect_initial_seconds=(self.settings.perptape_websocket_reconnect_initial_seconds),
            reconnect_max_seconds=self.settings.perptape_websocket_reconnect_max_seconds,
            max_reconnect_attempts=(self.settings.perptape_websocket_max_reconnect_attempts),
            clock=self.clock,
        )

    def run_forever(self, stop_event: threading.Event) -> None:
        stream: PerptapeStreamWorker | None = None
        stream_thread: threading.Thread | None = None
        try:
            if stop_event.is_set():
                return
            cycle_started_at = self._runtime_clock(continuous=True)
            cycle_completed_at = self._runtime_clock(continuous=True)
            if self.settings.perptape_websocket_enabled:
                perptape_actor = self.queries.service_principal_by_username(
                    self.settings.perptape_service_username
                )
                stream = PerptapeStreamWorker(
                    client=self.perptape,
                    websocket_url=self.settings.perptape_websocket_url,
                    api_key=self.settings.perptape_api_key,
                    contract_version=self.settings.perptape_contract_version,
                    load_snapshot=lambda: self.queries.perptape_feed(perptape_actor.user_id),
                    record_snapshot=lambda feed, now, base_snapshot: self._record_perptape_snapshot(
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
                    max_reconnect_attempts=(
                        self.settings.perptape_websocket_max_reconnect_attempts
                    ),
                    clock=self.clock,
                )
                stream_thread = threading.Thread(
                    target=stream.run_forever,
                    args=(stop_event,),
                    name="perptape-stream",
                )
                self._perptape_stream_thread = stream_thread
                stream_thread.start()
            while not stop_event.is_set():
                report = self.run_once(
                    started_at=cycle_started_at,
                    completed_at=cycle_completed_at,
                )
                logger.info(
                    "Runtime read-only synchronization cycle completed",
                    extra={
                        "event": "runtime_sync_cycle_completed",
                        "result": "READY" if report.ready_for_new_risk else "DEGRADED",
                        "component": "runtime-sync",
                    },
                )
                if stop_event.is_set():
                    break
                self._runtime_clock(continuous=True)
                if stop_event.wait(self.settings.runtime_sync_interval_seconds):
                    break
                cycle_started_at = self._runtime_clock(continuous=True)
                cycle_completed_at = self._runtime_clock(continuous=True)
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
    binance_request_state = DatabaseBinanceRequestState(database)
    if settings.binance_account_mode == "PORTFOLIO_MARGIN":
        binance = BinancePortfolioMarginReadOnlyClient(
            base_url=settings.binance_live_base_url,
            api_key=settings.binance_api_key,
            api_secret=settings.binance_api_secret,
            recv_window_ms=settings.binance_recv_window_ms,
            request_state=binance_request_state,
        )
    else:
        binance = BinanceReadOnlyClient(
            base_url=settings.binance_futures_base_url,
            api_key=settings.binance_api_key,
            api_secret=settings.binance_api_secret,
            recv_window_ms=settings.binance_recv_window_ms,
            request_state=binance_request_state,
        )
    hyperliquid = HyperliquidReadOnlyClient(
        base_url=settings.hyperliquid_base_url,
        account_address=(
            settings.hyperliquid_subaccount_address or settings.hyperliquid_account_address
        ),
        api_wallet_address=(
            settings.hyperliquid_api_wallet_address
            if settings.hyperliquid_read_only_enabled
            else None
        ),
        dex=settings.hyperliquid_core_dex,
        hip3_dexes=settings.hyperliquid_hip3_dexes,
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


class RuntimeBindingSupervisor:
    """Refresh every explicitly enabled database binding in its frozen team scope."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        worker_factory: Callable[[Settings, Database], RuntimeSyncWorker] = build_runtime_worker,
    ) -> None:
        self.settings = settings
        self.database = database
        self.clock = clock
        self.worker_factory = worker_factory
        self.service = TradingService(
            database,
            credential_encryption_key=settings.credential_encryption_key,
        )
        self._perptape_streams: dict[UUID, _BoundPerptapeStream] = {}
        self._failed_perptape_stream_versions: dict[UUID, tuple[UUID, UUID, int, int]] = {}

    @property
    def dependencies_in_use(self) -> bool:
        return any(handle.thread.is_alive() for handle in self._perptape_streams.values())

    def _account_worker(self, binding: PreparedRuntimeAccountBinding) -> RuntimeSyncWorker:
        updates: dict[str, Any] = {
            "runtime_sync_service_username": binding.service_principal_username,
            "runtime_binance_account_id": (
                binding.account_id if binding.venue == "BINANCE" else None
            ),
            "runtime_hyperliquid_account_id": (
                binding.account_id if binding.venue == "HYPERLIQUID" else None
            ),
            "binance_read_only_enabled": binding.venue == "BINANCE",
            "hyperliquid_read_only_enabled": binding.venue == "HYPERLIQUID",
            "binance_fact_environment": binding.environment,
            "hyperliquid_fact_environment": binding.environment,
            "binance_account_mode": (
                "STANDARD"
                if binding.environment == "TESTNET"
                else self.settings.binance_account_mode
            ),
            "binance_futures_base_url": (
                self.settings.binance_testnet_base_url
                if binding.environment == "TESTNET"
                else self.settings.binance_futures_base_url
            ),
            "hyperliquid_base_url": (
                self.settings.hyperliquid_testnet_base_url
                if binding.environment == "TESTNET"
                else self.settings.hyperliquid_live_base_url
            ),
            "perptape_api_key": None,
            "perptape_websocket_enabled": False,
            "notilt_enabled": False,
        }
        if binding.venue == "BINANCE":
            updates.update(
                binance_api_key=binding.credentials.get("api_key"),
                binance_api_secret=binding.credentials.get("api_secret"),
            )
        elif binding.venue == "HYPERLIQUID":
            updates.update(
                hyperliquid_account_address=binding.credentials.get("account_address"),
                hyperliquid_api_wallet_address=binding.credentials.get("api_wallet_address"),
                hyperliquid_api_wallet_private_key=None,
            )
        worker = self.worker_factory(self.settings.model_copy(update=updates), self.database)
        if binding.venue in {"OKX", "BYBIT"}:
            installer = getattr(worker, "install_database_account_reader", None)
            if installer is None:
                raise DomainRejected(
                    "RUNTIME_BINDING_WORKER_SCOPE_MISMATCH",
                    "runtime worker cannot install the database-bound venue reader",
                )
            installer(binding)
        return worker

    def _perptape_worker(self, binding: PreparedPerptapeRuntimeBinding) -> RuntimeSyncWorker:
        scoped = self.settings.model_copy(
            update={
                "perptape_api_key": binding.api_key,
                "perptape_service_username": binding.service_principal_username,
                "perptape_websocket_enabled": False,
                "binance_read_only_enabled": False,
                "hyperliquid_read_only_enabled": False,
                "notilt_enabled": False,
            }
        )
        return self.worker_factory(scoped, self.database)

    @staticmethod
    def _perptape_binding_version(
        binding: PreparedPerptapeRuntimeBinding,
    ) -> tuple[UUID, UUID, int, int]:
        return (
            binding.team_id,
            binding.service_principal_id,
            binding.source_version,
            binding.credential_version,
        )

    def _record_perptape_stream_health(
        self,
        binding: PreparedPerptapeRuntimeBinding,
        result: SourceSyncResult,
        *,
        now: datetime,
    ) -> None:
        try:
            self.service.record_runtime_source_health(
                binding.service_principal_id,
                {"PERPTAPE_WEBSOCKET": asdict(result)},
                perptape_runtime_binding=binding,
                now=now,
            )
        except DomainRejected as exc:
            logger.info(
                "Stale Team Perptape stream health was not recorded",
                extra={
                    "event": "perptape_team_stream_health_rejected",
                    "component": "perptape",
                    "error_code": exc.code,
                    "team_id": str(binding.team_id),
                },
            )

    def _stop_perptape_stream(self, handle: _BoundPerptapeStream) -> None:
        handle.stop_event.set()
        timeout_seconds = (
            self.settings.perptape_timeout_seconds
            + min(self.settings.perptape_timeout_seconds, 5)
            + 2
        )
        handle.thread.join(timeout=timeout_seconds)
        if handle.thread.is_alive():
            raise DomainRejected(
                "PERPTAPE_STREAM_STOP_TIMEOUT",
                "a Team Perptape WebSocket did not stop within its bounded timeout",
            )

    def _start_perptape_stream(
        self,
        binding: PreparedPerptapeRuntimeBinding,
        *,
        now: datetime,
    ) -> None:
        version = self._perptape_binding_version(binding)
        try:
            worker = self._perptape_worker(binding)
            stream = worker.build_bound_perptape_stream(binding)
        except DomainRejected as exc:
            self._failed_perptape_stream_versions[binding.signal_source_id] = version
            self._record_perptape_stream_health(
                binding,
                SourceSyncResult("FAILED", error_code=exc.code),
                now=now,
            )
            return
        stop_event = threading.Event()
        thread = threading.Thread(
            target=stream.run_forever,
            args=(stop_event,),
            name=f"perptape-team-stream-{str(binding.team_id)[:8]}",
        )
        handle = _BoundPerptapeStream(
            binding=binding,
            worker=worker,
            stream=stream,
            stop_event=stop_event,
            thread=thread,
        )
        self._perptape_streams[binding.signal_source_id] = handle
        self._failed_perptape_stream_versions.pop(binding.signal_source_id, None)
        thread.start()
        self._record_perptape_stream_health(
            binding,
            SourceSyncResult("SKIPPED", error_code="PERPTAPE_STREAM_STARTING"),
            now=now,
        )

    def _reconcile_perptape_streams(
        self,
        bindings: Sequence[PreparedPerptapeRuntimeBinding],
        *,
        now: datetime,
    ) -> None:
        current = {binding.signal_source_id: binding for binding in bindings}
        for signal_source_id, handle in tuple(self._perptape_streams.items()):
            binding = current.get(signal_source_id)
            current_version = None if binding is None else self._perptape_binding_version(binding)
            if not handle.thread.is_alive():
                self._perptape_streams.pop(signal_source_id, None)
                if current_version == handle.version:
                    error_code = handle.stream.fatal_error_code or "PERPTAPE_STREAM_STOPPED"
                    self._failed_perptape_stream_versions[signal_source_id] = handle.version
                    self._record_perptape_stream_health(
                        handle.binding,
                        SourceSyncResult("FAILED", error_code=error_code),
                        now=now,
                    )
                continue
            if current_version != handle.version:
                self._stop_perptape_stream(handle)
                self._perptape_streams.pop(signal_source_id, None)

        active_ids = set(current)
        for signal_source_id in tuple(self._failed_perptape_stream_versions):
            if signal_source_id not in active_ids:
                self._failed_perptape_stream_versions.pop(signal_source_id, None)

        for signal_source_id, binding in current.items():
            if signal_source_id in self._perptape_streams:
                continue
            version = self._perptape_binding_version(binding)
            if self._failed_perptape_stream_versions.get(signal_source_id) == version:
                continue
            self._start_perptape_stream(binding, now=now)

        for handle in tuple(self._perptape_streams.values()):
            if not handle.thread.is_alive():
                continue
            result = (
                SourceSyncResult(
                    "SUCCESS",
                    items_observed=handle.stream.stats.messages_received,
                )
                if handle.stream.connection_healthy
                else SourceSyncResult(
                    "SKIPPED",
                    error_code="PERPTAPE_STREAM_STARTING",
                )
            )
            self._record_perptape_stream_health(handle.binding, result, now=now)

    def _shutdown_perptape_streams(self) -> None:
        handles = tuple(self._perptape_streams.values())
        for handle in handles:
            handle.stop_event.set()
        timeout_seconds = (
            self.settings.perptape_timeout_seconds
            + min(self.settings.perptape_timeout_seconds, 5)
            + 2
        )
        for handle in handles:
            handle.thread.join(timeout=timeout_seconds)
        if any(handle.thread.is_alive() for handle in handles):
            raise DomainRejected(
                "PERPTAPE_STREAM_STOP_TIMEOUT",
                "one or more Team Perptape WebSockets did not stop within their bounded timeout",
            )
        self._perptape_streams.clear()

    def run_once(
        self,
        *,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        account_bindings: Sequence[PreparedRuntimeAccountBinding] | None = None,
        signal_bindings: Sequence[PreparedPerptapeRuntimeBinding] | None = None,
    ) -> RuntimeBindingSyncReport:
        headroom = timedelta(seconds=self.settings.runtime_sync_interval_seconds + 30)
        started_at = normalize_perptape_operational_datetime(
            self.clock() if started_at is None else started_at,
            required_headroom=headroom,
        )
        completed_at = normalize_perptape_operational_datetime(
            self.clock() if completed_at is None else completed_at,
            required_headroom=headroom,
        )
        results: dict[str, SourceSyncResult] = {}
        if account_bindings is None:
            account_bindings = (
                ()
                if self.settings.fact_adapter_enabled
                else self.service.runtime_account_bindings()
            )
        if signal_bindings is None:
            signal_bindings = self.service.perptape_runtime_bindings()
        for account_binding in account_bindings:
            key = f"{account_binding.team_id}:{account_binding.venue}:{account_binding.account_id}"
            results[key] = self._account_worker(account_binding).run_bound_account_once(
                account_binding,
                now=started_at,
            )
        for signal_binding in signal_bindings:
            key = f"{signal_binding.team_id}:PERPTAPE"
            results[key] = self._perptape_worker(signal_binding).run_bound_perptape_once(
                signal_binding,
                now=started_at,
            )
        return RuntimeBindingSyncReport(
            started_at=started_at.isoformat(),
            completed_at=completed_at.isoformat(),
            sources=results,
        )

    def run_forever(self, stop_event: threading.Event) -> None:
        try:
            while not stop_event.is_set():
                account_bindings = (
                    ()
                    if self.settings.fact_adapter_enabled
                    else self.service.runtime_account_bindings()
                )
                signal_bindings = self.service.perptape_runtime_bindings()
                started_at = normalize_perptape_operational_datetime(self.clock())
                if self.settings.perptape_websocket_enabled:
                    self._reconcile_perptape_streams(signal_bindings, now=started_at)
                report = self.run_once(
                    started_at=started_at,
                    account_bindings=account_bindings,
                    signal_bindings=signal_bindings,
                )
                if self.settings.perptape_websocket_enabled:
                    self._reconcile_perptape_streams(
                        signal_bindings,
                        now=normalize_perptape_operational_datetime(self.clock()),
                    )
                logger.info(
                    "Database-bound read-only synchronization cycle completed",
                    extra={
                        "event": "runtime_binding_sync_cycle_completed",
                        "result": "READY" if report.successful else "DEGRADED",
                        "component": "runtime-sync",
                    },
                )
                if stop_event.wait(self.settings.runtime_sync_interval_seconds):
                    break
        finally:
            self._shutdown_perptape_streams()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run read-only trading fact synchronization")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run one synchronization cycle")
    mode.add_argument(
        "--healthcheck",
        action="store_true",
        help="validate the supervised runtime worker without external probes",
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    settings.validate_runtime_security()
    configure_logging(settings.log_level)
    if (args.healthcheck or not args.once) and not settings.runtime_sync_enabled:
        raise SystemExit("TRADING_RUNTIME_SYNC_ENABLED must be true for continuous mode")
    database = Database(settings.database_url)
    worker: RuntimeSyncWorker | RuntimeBindingSupervisor | None = None
    try:
        ready, reason = database.is_ready()
        if not ready:
            raise SystemExit(f"database is not ready: {reason}")
        if args.healthcheck:
            print(
                json.dumps(
                    {
                        "component": "runtime-sync",
                        "database": "READY",
                        "runtime_sync_enabled": True,
                        "status": "READY",
                        "team_perptape_websocket_requested": (settings.perptape_websocket_enabled),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        # Continuous synchronization must keep discovering database bindings.
        # Choosing the legacy environment-backed worker only once at process
        # startup leaves newly configured Team accounts and Perptape keys
        # invisible until a manual restart and can keep using stale deployment
        # credentials.  The supervisor refreshes the binding set every cycle.
        worker = (
            RuntimeBindingSupervisor(
                settings=settings,
                database=database,
                worker_factory=build_runtime_worker,
            )
            if settings.runtime_sync_enabled
            else build_runtime_worker(settings, database)
        )
        if args.once:
            report = worker.run_once()
            print(json.dumps(report.to_dict(), separators=(",", ":"), sort_keys=True))
            accepted = (
                report.ready_for_new_risk
                if isinstance(report, RuntimeSyncReport)
                else report.successful
            )
            return 0 if accepted else 1
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
