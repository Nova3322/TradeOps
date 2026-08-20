from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select

from trading_control_plane import domain, models
from trading_control_plane.config import Settings, get_settings
from trading_control_plane.database import Database
from trading_control_plane.execution_dispatch import (
    ExecuteIntent,
    ExecuteIntentResult,
    execute_intent,
)
from trading_control_plane.freqtrade import FreqtradeWorkerClient, FreqtradeWorkerSpec
from trading_control_plane.logging import configure_logging
from trading_control_plane.runtime_contracts import PreparedFreqtradeWorkerBinding
from trading_control_plane.service import TradingService
from trading_control_plane.service_domains.proposal_automation import (
    advance_approved_proposal,
    refresh_approved_proposal_risk,
)

logger = logging.getLogger(__name__)
AUTOMATIC_EXECUTION_OWNER = "tradeops-automatic-execution"


@dataclass(frozen=True, slots=True)
class AutomaticIntent:
    intent_id: UUID
    campaign_id: UUID
    actor_id: UUID
    execution_scope: str


@dataclass(frozen=True, slots=True)
class AutomaticExecutionReport:
    started_at: str
    completed_at: str
    risk_decisions_refreshed: int
    proposals_advanced: int
    intents_selected: int
    intents_completed: int
    reconciliations_completed: int
    blocked: dict[str, int]

    @property
    def successful(self) -> bool:
        return not self.blocked

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


WorkerFactory = Callable[[PreparedFreqtradeWorkerBinding], FreqtradeWorkerClient]


class AutomaticExecutionWorker:
    """Consume durable approved-workflow and Freqtrade intent state without UI actions."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        worker_factory: WorkerFactory | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self.database = database
        self.service = TradingService(
            database,
            credential_encryption_key=settings.credential_encryption_key,
        )
        self.worker_factory = worker_factory or self._worker_client
        self.clock = clock

    def _worker_client(
        self,
        binding: PreparedFreqtradeWorkerBinding,
    ) -> FreqtradeWorkerClient:
        return FreqtradeWorkerClient(
            FreqtradeWorkerSpec(
                name=binding.worker_name,
                venue=binding.venue,  # type: ignore[arg-type]
                base_url=binding.worker_url,
                username=binding.username,
                password=binding.password,
                ws_token=binding.ws_token,
                hip3_dexes=binding.hip3_dexes,
                exchange_account_id=str(binding.exchange_account_id),
                team_id=str(binding.team_id),
                account_id=binding.account_id,
            ),
            timeout_seconds=self.settings.freqtrade_timeout_seconds,
            confirmation_timeout_seconds=(
                self.settings.freqtrade_confirmation_timeout_seconds
            ),
        )

    def _approved_proposal_ids(self, *, now: datetime) -> tuple[UUID, ...]:
        with self.database.session_factory() as session:
            ids = session.scalars(
                select(models.Proposal.proposal_id)
                .where(
                    models.Proposal.status == domain.ProposalStatus.APPROVED.value,
                    models.Proposal.expires_at > now,
                    ~select(models.OrderIntent.intent_id)
                    .join(
                        models.Campaign,
                        models.Campaign.campaign_id == models.OrderIntent.campaign_id,
                    )
                    .where(
                        models.Campaign.proposal_id == models.Proposal.proposal_id,
                        models.OrderIntent.kind == domain.IntentKind.INITIAL.value,
                    )
                    .exists(),
                )
                .order_by(models.Proposal.updated_at, models.Proposal.proposal_id)
                .limit(self.settings.execution_worker_batch_size)
            ).all()
        return tuple(ids)

    def _automatic_intents(self) -> tuple[AutomaticIntent, ...]:
        with self.database.session_factory() as session:
            live_gate = session.get(models.CapabilityGate, "LIVE_ORDER_SEND")
            live_enabled = (
                live_gate is not None
                and live_gate.status == domain.CapabilityStatus.ENABLED.value
            )
            rows = session.execute(
                select(
                    models.OrderIntent.intent_id,
                    models.Campaign.campaign_id,
                    models.ExchangeAccount.runtime_service_principal_id,
                    models.Campaign.environment,
                    models.Campaign.account_id,
                    models.Campaign.venue,
                )
                .join(
                    models.Campaign,
                    models.Campaign.campaign_id == models.OrderIntent.campaign_id,
                )
                .join(
                    models.ExchangeAccount,
                    (models.ExchangeAccount.team_id == models.Campaign.team_id)
                    & (models.ExchangeAccount.environment == models.Campaign.environment)
                    & (models.ExchangeAccount.account_id == models.Campaign.account_id)
                    & (models.ExchangeAccount.venue == models.Campaign.venue),
                )
                .where(
                    models.OrderIntent.status.in_(
                        (
                            domain.OrderIntentStatus.READY.value,
                            domain.OrderIntentStatus.DISPATCHING.value,
                        )
                    ),
                    models.ExchangeAccount.active,
                    models.ExchangeAccount.deleted_at.is_(None),
                    models.ExchangeAccount.runtime_service_principal_id.is_not(None),
                    (
                        (models.Campaign.environment != domain.ExecutionEnvironment.LIVE.value)
                        | live_enabled
                    ),
                )
                .order_by(models.OrderIntent.created_at, models.OrderIntent.intent_id)
                .limit(self.settings.execution_worker_batch_size)
            ).all()
        results: list[AutomaticIntent] = []
        for intent_id, campaign_id, actor_id, environment, account_id, venue in rows:
            assert actor_id is not None
            results.append(
                AutomaticIntent(
                    intent_id=intent_id,
                    campaign_id=campaign_id,
                    actor_id=actor_id,
                    execution_scope=f"{environment}:{account_id}:{venue}",
                )
            )
        return tuple(results)

    def _reconciliation_intents(self) -> tuple[AutomaticIntent, ...]:
        with self.database.session_factory() as session:
            rows = session.execute(
                select(
                    models.OrderIntent.intent_id,
                    models.Campaign.campaign_id,
                    models.ExchangeAccount.runtime_service_principal_id,
                    models.Campaign.team_id,
                    models.Campaign.environment,
                    models.Campaign.account_id,
                    models.Campaign.venue,
                    models.OrderIntent.updated_at,
                )
                .join(
                    models.Campaign,
                    models.Campaign.campaign_id == models.OrderIntent.campaign_id,
                )
                .join(
                    models.ExchangeAccount,
                    (models.ExchangeAccount.team_id == models.Campaign.team_id)
                    & (models.ExchangeAccount.environment == models.Campaign.environment)
                    & (models.ExchangeAccount.account_id == models.Campaign.account_id)
                    & (models.ExchangeAccount.venue == models.Campaign.venue),
                )
                .where(
                    models.Campaign.status != domain.CampaignStatus.CLOSED.value,
                    models.OrderIntent.status.in_(
                        (
                            domain.OrderIntentStatus.DISPATCHING.value,
                            domain.OrderIntentStatus.SENT.value,
                            domain.OrderIntentStatus.PARTIALLY_FILLED.value,
                            domain.OrderIntentStatus.FILLED.value,
                            domain.OrderIntentStatus.UNKNOWN.value,
                        )
                    ),
                    models.ExchangeAccount.active,
                    models.ExchangeAccount.deleted_at.is_(None),
                    models.ExchangeAccount.runtime_service_principal_id.is_not(None),
                )
                .order_by(models.OrderIntent.updated_at, models.OrderIntent.intent_id)
            ).all()
            selected: dict[str, AutomaticIntent] = {}
            for (
                intent_id,
                campaign_id,
                actor_id,
                team_id,
                environment,
                account_id,
                venue,
                intent_updated_at,
            ) in rows:
                assert actor_id is not None
                scope = f"{environment}:{account_id}:{venue}"
                if scope in selected:
                    continue
                latest = session.scalar(
                    select(models.ReconciliationRun)
                    .where(
                        models.ReconciliationRun.team_id == team_id,
                        models.ReconciliationRun.execution_scope == scope,
                    )
                    .order_by(models.ReconciliationRun.completed_at.desc())
                    .limit(1)
                )
                if latest is not None and latest.completed_at >= intent_updated_at:
                    if latest.status == domain.ReconciliationStatus.MATCH.value:
                        continue
                    latest_fact_at = max(
                        (
                            value
                            for value in (
                                session.scalar(
                                    select(func.max(models.Position.observed_at)).where(
                                        models.Position.team_id == team_id,
                                        models.Position.environment == environment,
                                        models.Position.account_id == account_id,
                                        models.Position.venue == venue,
                                    )
                                ),
                                session.scalar(
                                    select(func.max(models.AccountEquity.observed_at)).where(
                                        models.AccountEquity.team_id == team_id,
                                        models.AccountEquity.environment == environment,
                                        models.AccountEquity.account_id == account_id,
                                        models.AccountEquity.venue == venue,
                                    )
                                ),
                                session.scalar(
                                    select(func.max(models.VenueOrder.observed_at)).where(
                                        models.VenueOrder.team_id == team_id,
                                        models.VenueOrder.environment == environment,
                                        models.VenueOrder.account_id == account_id,
                                        models.VenueOrder.venue == venue,
                                    )
                                ),
                                session.scalar(
                                    select(func.max(models.VenueFill.executed_at)).where(
                                        models.VenueFill.team_id == team_id,
                                        models.VenueFill.environment == environment,
                                        models.VenueFill.account_id == account_id,
                                        models.VenueFill.venue == venue,
                                    )
                                ),
                                session.scalar(
                                    select(func.max(models.ProtectionOrder.observed_at))
                                    .join(
                                        models.Position,
                                        models.Position.position_id
                                        == models.ProtectionOrder.position_id,
                                    )
                                    .where(
                                        models.Position.team_id == team_id,
                                        models.Position.environment == environment,
                                        models.Position.account_id == account_id,
                                        models.Position.venue == venue,
                                    )
                                ),
                            )
                            if value is not None
                        ),
                        default=None,
                    )
                    if latest_fact_at is None or latest_fact_at <= latest.completed_at:
                        continue
                selected[scope] = AutomaticIntent(
                    intent_id=intent_id,
                    campaign_id=campaign_id,
                    actor_id=actor_id,
                    execution_scope=scope,
                )
                if len(selected) >= self.settings.execution_worker_batch_size:
                    break
        return tuple(selected.values())

    def run_once(self) -> AutomaticExecutionReport:
        started_at = self.clock()
        blocked: dict[str, int] = {}
        risk_decisions_refreshed = 0
        proposals_advanced = 0
        for proposal_id in self._approved_proposal_ids(now=started_at):
            try:
                if refresh_approved_proposal_risk(
                    self.service,
                    proposal_id=proposal_id,
                    fallback_service_username=self.settings.runtime_sync_service_username,
                    now=self.clock(),
                ):
                    risk_decisions_refreshed += 1
                automation_result = advance_approved_proposal(
                    self.service,
                    proposal_id=proposal_id,
                    fallback_service_username=self.settings.runtime_sync_service_username,
                    now=self.clock(),
                )
            except domain.DomainRejected as exc:
                blocked[exc.code] = blocked.get(exc.code, 0) + 1
                continue
            if automation_result["status"] == "READY":
                proposals_advanced += 1

        intents = self._automatic_intents()
        intents_completed = 0
        for intent in intents:
            try:
                fencing_token = self.service.acquire_sender(
                    intent.execution_scope,
                    AUTOMATIC_EXECUTION_OWNER,
                    intent.actor_id,
                    self.clock(),
                )
                execution_result: ExecuteIntentResult = execute_intent(
                    ExecuteIntent(
                        intent_id=intent.intent_id,
                        campaign_id=intent.campaign_id,
                        actor_id=intent.actor_id,
                        execution_scope=intent.execution_scope,
                        owner_id=AUTOMATIC_EXECUTION_OWNER,
                        fencing_token=fencing_token,
                        idempotency_key=f"automatic-freqtrade:{intent.intent_id}",
                    ),
                    service=self.service,
                    worker_resolver=self.worker_factory,
                    require_enabled=self._require_enabled,
                    live_leverage=self.settings.freqtrade_live_leverage,
                    clock=self.clock,
                )
            except domain.DomainRejected as exc:
                blocked[exc.code] = blocked.get(exc.code, 0) + 1
                continue
            if (
                execution_result.venue_order_fact_id is not None
                or execution_result.replayed
            ):
                intents_completed += 1

        reconciliations_completed = 0
        for intent in self._reconciliation_intents():
            try:
                self.service.reconcile_scope(
                    intent.execution_scope,
                    intent.actor_id,
                    now=self.clock(),
                )
            except domain.DomainRejected as exc:
                blocked[exc.code] = blocked.get(exc.code, 0) + 1
                continue
            reconciliations_completed += 1

        completed_at = self.clock()
        return AutomaticExecutionReport(
            started_at=started_at.isoformat(),
            completed_at=completed_at.isoformat(),
            risk_decisions_refreshed=risk_decisions_refreshed,
            proposals_advanced=proposals_advanced,
            intents_selected=len(intents),
            intents_completed=intents_completed,
            reconciliations_completed=reconciliations_completed,
            blocked=blocked,
        )

    def _require_enabled(self) -> None:
        if not self.settings.execution_worker_enabled:
            raise domain.DomainRejected(
                "AUTOMATIC_EXECUTION_DISABLED",
                "automatic execution worker is explicitly disabled",
            )
        if not self.settings.freqtrade_workers_enabled:
            raise domain.DomainRejected(
                "FREQTRADE_EXECUTION_DISABLED",
                "Freqtrade execution is explicitly disabled",
            )

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            report = self.run_once()
            logger.info(
                "Automatic execution cycle completed",
                extra={
                    "event": "automatic_execution_cycle_completed",
                    "component": "execution-worker",
                    "result": "READY" if report.successful else "DEGRADED",
                    "risk_decisions_refreshed": report.risk_decisions_refreshed,
                    "proposals_advanced": report.proposals_advanced,
                    "intents_selected": report.intents_selected,
                    "intents_completed": report.intents_completed,
                    "reconciliations_completed": report.reconciliations_completed,
                    "blocked": report.blocked,
                },
            )
            if stop_event.wait(self.settings.execution_worker_interval_seconds):
                break


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run automatic approved-trade execution")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run one execution cycle")
    mode.add_argument("--healthcheck", action="store_true", help="validate worker readiness")
    args = parser.parse_args(argv)
    settings = get_settings()
    settings.validate_runtime_security()
    configure_logging(settings.log_level)
    if not settings.execution_worker_enabled:
        raise SystemExit("TRADING_EXECUTION_WORKER_ENABLED must be true")
    if not settings.freqtrade_workers_enabled:
        raise SystemExit("TRADING_FREQTRADE_WORKERS_ENABLED must be true")
    database = Database(settings.database_url)
    try:
        ready, reason = database.is_ready()
        if not ready:
            raise SystemExit(f"database is not ready: {reason}")
        if args.healthcheck:
            print(
                json.dumps(
                    {
                        "component": "execution-worker",
                        "database": "READY",
                        "execution_worker_enabled": True,
                        "freqtrade_workers_enabled": True,
                        "status": "READY",
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        worker = AutomaticExecutionWorker(settings=settings, database=database)
        if args.once:
            report = worker.run_once()
            print(json.dumps(report.to_dict(), separators=(",", ":"), sort_keys=True))
            return 0 if report.successful else 1
        stop_event = threading.Event()
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signal_number, lambda _signal, _frame: stop_event.set())
        worker.run_forever(stop_event)
        return 0
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AUTOMATIC_EXECUTION_OWNER",
    "AutomaticExecutionReport",
    "AutomaticExecutionWorker",
    "AutomaticIntent",
    "main",
]
