from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select

from trading_control_plane import domain, models
from trading_control_plane import execution_scope as scope_rules
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
from trading_control_plane.safe_spending import SafeSpendingGateway
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
    proposal_id: UUID | None = None
    query_only: bool = False
    reduce_only: bool = False


@dataclass(frozen=True, slots=True)
class AutomaticExecutionReport:
    started_at: str
    completed_at: str
    capital_facts_refreshed: int
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
        safe_spending_gateway: SafeSpendingGateway | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self.database = database
        self.service = TradingService(
            database,
            credential_encryption_key=settings.credential_encryption_key,
        )
        self.worker_factory = worker_factory or self._worker_client
        self.safe_spending_gateway = safe_spending_gateway or SafeSpendingGateway(
            timeout_seconds=settings.safe_spending_gateway_timeout_seconds
        )
        self._safe_refresh_next_at: dict[UUID, datetime] = {}
        self._sender_reconciliation_next_at: dict[str, datetime] = {}
        self._worker_health_next_at: datetime | None = None
        self.clock = clock

    def _refresh_safe_capital_fact(
        self,
        *,
        proposal_id: UUID,
        now: datetime,
        require_retryable_denial: bool = True,
    ) -> UUID | None:
        """Refresh a stale configured Safe fact before approved new-risk work."""

        with self.database.session_factory() as session:
            proposal = session.get(models.Proposal, proposal_id)
            if (
                proposal is None
                or proposal.environment != domain.ExecutionEnvironment.LIVE.value
            ):
                return None
            if require_retryable_denial:
                decision = session.scalar(
                    select(models.RiskDecision)
                    .where(models.RiskDecision.proposal_id == proposal_id)
                    .order_by(models.RiskDecision.created_at.desc())
                    .limit(1)
                )
                if (
                    decision is None
                    or decision.result != domain.RiskResult.DENY.value
                    or not frozenset(decision.reasons)
                    & {"STALE_FACTS", "EQUITY_UNKNOWN"}
                ):
                    return None
            next_at = self._safe_refresh_next_at.get(proposal.team_id)
            if next_at is not None and now < next_at:
                return None
            config = session.scalar(
                select(models.DirectCapitalConfiguration).where(
                    models.DirectCapitalConfiguration.team_id == proposal.team_id,
                    models.DirectCapitalConfiguration.environment
                    == domain.ExecutionEnvironment.LIVE.value,
                    models.DirectCapitalConfiguration.active,
                    models.DirectCapitalConfiguration.treasury_provider
                    == "SAFE_SPENDING_LIMIT",
                )
            )
            if (
                config is None
                or not self.settings.safe_spending_enabled
                or self.settings.safe_spending_arbitrum_rpc_url is None
                or config.safe_address is None
                or config.safe_delegate_address is None
            ):
                return None
            policy = session.scalar(
                select(models.RiskPolicy).where(
                    models.RiskPolicy.team_id == proposal.team_id,
                    models.RiskPolicy.active,
                )
            )
            if policy is None:
                return None
            fact = session.scalar(
                select(models.AccountEquity).where(
                    models.AccountEquity.team_id == proposal.team_id,
                    models.AccountEquity.environment
                    == domain.ExecutionEnvironment.LIVE.value,
                    models.AccountEquity.account_id == config.safe_address,
                    models.AccountEquity.venue == "VAULT",
                    models.AccountEquity.currency == "USDC",
                )
            )
            fact_time = None
            if fact is not None:
                fact_time = min(
                    value
                    for value in (fact.observed_at, fact.valuation_observed_at)
                    if value is not None
                )
            fact_current = (
                fact is not None
                and fact.fact_status == domain.FactStatus.KNOWN.value
                and fact.control_status == "CONTROLLED"
                and fact_time is not None
                and not scope_rules.fact_is_stale(
                    fact_time,
                    now,
                    timedelta(seconds=policy.max_fact_age_seconds),
                )
            )
            if fact_current:
                return None
            actor = session.scalar(
                select(models.User)
                .join(
                    models.RoleAssignment,
                    models.RoleAssignment.user_id == models.User.user_id,
                )
                .where(
                    models.User.username == self.settings.runtime_sync_service_username,
                    models.User.principal_type == domain.PrincipalType.SERVICE.value,
                    models.User.active,
                    models.RoleAssignment.team_id == proposal.team_id,
                    models.RoleAssignment.role.in_(
                        (
                            domain.Role.TREASURY_ADMIN.value,
                            domain.Role.SYSTEM_ADMIN.value,
                        )
                    ),
                )
            )
            if actor is None:
                raise domain.DomainRejected(
                    "CAPITAL_FACT_ACTOR_UNAVAILABLE",
                    "the configured capital fact service principal is unavailable",
                )
            team_id = proposal.team_id
            actor_id = actor.user_id
            safe_address = config.safe_address
            safe_delegate = config.safe_delegate_address
            rpc_url = self.settings.safe_spending_arbitrum_rpc_url
        self._safe_refresh_next_at[team_id] = now + timedelta(
            seconds=max(60, self.settings.runtime_sync_interval_seconds)
        )
        safe_fact = self.safe_spending_gateway.read_limit(
            rpc_url=rpc_url,
            safe=safe_address,
            delegate=safe_delegate,
        )
        try:
            scale = Decimal(10) ** 6
            observed_at = datetime.fromtimestamp(
                int(str(safe_fact["blockTimestamp"])),
                UTC,
            )
            balance = Decimal(str(safe_fact["balance"])) / scale
            available_limit = Decimal(str(safe_fact["available"])) / scale
            module_enabled = bool(safe_fact["moduleEnabled"])
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise domain.DomainRejected(
                "SAFE_RESPONSE_INVALID",
                "Safe gateway returned invalid read-only fact data",
            ) from exc
        self.service.record_safe_spending_snapshot(
            actor_id=actor_id,
            safe_address=safe_address,
            asset="USDC",
            balance=balance,
            available_limit=available_limit,
            module_enabled=module_enabled,
            observed_at=observed_at,
            now=now,
        )
        return team_id

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
        now = self.clock()
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
                    models.Campaign.proposal_id,
                    models.ExchangeAccount.runtime_service_principal_id,
                    models.Campaign.environment,
                    models.Campaign.account_id,
                    models.Campaign.venue,
                    models.OrderIntent.status,
                    models.OrderIntent.reduce_only,
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
                    or_(
                        models.OrderIntent.status == domain.OrderIntentStatus.READY.value,
                        and_(
                            models.OrderIntent.status.in_(
                                (
                                    domain.OrderIntentStatus.DISPATCHING.value,
                                    domain.OrderIntentStatus.SENT.value,
                                    domain.OrderIntentStatus.PARTIALLY_FILLED.value,
                                    domain.OrderIntentStatus.UNKNOWN.value,
                                )
                            ),
                            models.OrderIntent.dispatch_backend == "FREQTRADE",
                            models.OrderIntent.dispatch_started_at.is_not(None),
                        ),
                    ),
                    models.ExchangeAccount.active,
                    models.ExchangeAccount.deleted_at.is_(None),
                    models.ExchangeAccount.runtime_service_principal_id.is_not(None),
                    or_(
                        models.OrderIntent.execution_retry_at.is_(None),
                        models.OrderIntent.execution_retry_at <= now,
                    ),
                    (
                        (models.Campaign.environment != domain.ExecutionEnvironment.LIVE.value)
                        | live_enabled
                    ),
                )
                .order_by(models.OrderIntent.created_at, models.OrderIntent.intent_id)
                .limit(self.settings.execution_worker_batch_size)
            ).all()
        results: list[AutomaticIntent] = []
        for (
            intent_id,
            campaign_id,
            proposal_id,
            actor_id,
            environment,
            account_id,
            venue,
            intent_status,
            reduce_only,
        ) in rows:
            assert actor_id is not None
            results.append(
                AutomaticIntent(
                    intent_id=intent_id,
                    campaign_id=campaign_id,
                    actor_id=actor_id,
                    execution_scope=f"{environment}:{account_id}:{venue}",
                    proposal_id=proposal_id,
                    query_only=intent_status != domain.OrderIntentStatus.READY.value,
                    reduce_only=bool(reduce_only),
                )
            )
        return tuple(results)

    def _refresh_worker_health(self, *, now: datetime) -> dict[str, int]:
        """Probe configured workers read-only and publish process/account heartbeats."""

        if not hasattr(self.service, "runtime_freqtrade_worker_bindings"):
            return {}
        next_at = getattr(self, "_worker_health_next_at", None)
        if next_at is not None and now < next_at:
            return {}
        interval = max(30, int(getattr(self.settings, "runtime_sync_interval_seconds", 60)))
        self._worker_health_next_at = now + timedelta(seconds=interval)
        blocked: dict[str, int] = {}
        try:
            bindings = self.service.runtime_freqtrade_worker_bindings(verified_only=False)
        except domain.DomainRejected as exc:
            return {exc.code: 1}
        for binding in bindings:
            if binding.service_principal_id is None:
                blocked["FREQTRADE_RUNTIME_BINDING_INVALID"] = (
                    blocked.get("FREQTRADE_RUNTIME_BINDING_INVALID", 0) + 1
                )
                continue
            probe_result: dict[str, Any] | None = None
            error_code: str | None = None
            try:
                probe_result = self.worker_factory(binding).probe(
                    expected_mode=binding.worker_mode,  # type: ignore[arg-type]
                )
            except domain.DomainRejected as exc:
                error_code = exc.code
            try:
                error_code = self.service.record_freqtrade_runtime_probe(
                    binding,
                    probe_result=probe_result,
                    error_code=error_code,
                    now=now,
                )
            except domain.DomainRejected as exc:
                error_code = exc.code
            status = "SUCCESS" if error_code is None else "FAILED"
            try:
                self.service.record_runtime_source_health(
                    binding.service_principal_id,
                    {
                        "EXECUTION_WORKER": {
                            "status": "SUCCESS",
                            "items_observed": 1,
                            "error_code": None,
                        },
                        "FREQTRADE_WORKER": {
                            "status": status,
                            "items_observed": 1 if error_code is None else 0,
                            "error_code": error_code,
                        },
                    },
                    scopes={
                        "EXECUTION_WORKER": (binding.account_id, binding.venue),
                        "FREQTRADE_WORKER": (binding.account_id, binding.venue),
                    },
                    now=now,
                )
            except domain.DomainRejected as exc:
                error_code = error_code or exc.code
            if error_code is not None:
                blocked[error_code] = blocked.get(error_code, 0) + 1
        return blocked

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
        blocked = self._refresh_worker_health(now=started_at)
        capital_facts_refreshed = 0
        risk_decisions_refreshed = 0
        proposals_advanced = 0
        for proposal_id in self._approved_proposal_ids(now=started_at):
            try:
                if (
                    self._refresh_safe_capital_fact(
                        proposal_id=proposal_id,
                        now=self.clock(),
                    )
                    is not None
                ):
                    capital_facts_refreshed += 1
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
                if (
                    intent.proposal_id is not None
                    and not intent.query_only
                    and not intent.reduce_only
                    and self._refresh_safe_capital_fact(
                        proposal_id=intent.proposal_id,
                        now=self.clock(),
                        require_retryable_denial=False,
                    )
                    is not None
                ):
                    capital_facts_refreshed += 1
                fencing_token = self._acquire_sender(intent)
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
                    clock=self.clock,
                )
            except domain.DomainRejected as exc:
                blocked[exc.code] = blocked.get(exc.code, 0) + 1
                try:
                    if hasattr(self.service, "record_execution_blocker"):
                        self.service.record_execution_blocker(
                            intent.intent_id,
                            actor_id=intent.actor_id,
                            error_code=exc.code,
                            now=self.clock(),
                            retry_after_seconds=max(
                                30,
                                int(
                                    getattr(
                                        self.settings,
                                        "runtime_sync_interval_seconds",
                                        60,
                                    )
                                ),
                            ),
                        )
                    if exc.code == "AUTHORIZATION_EXPIRED" and hasattr(
                        self.service, "release_unfilled_intent"
                    ):
                        self.service.release_unfilled_intent(
                            intent.intent_id,
                            intent.actor_id,
                            domain.OrderIntentStatus.CANCELLED,
                            "authorization expired before any external send attempt",
                            now=self.clock(),
                        )
                except domain.DomainRejected as persistence_error:
                    blocked[persistence_error.code] = (
                        blocked.get(persistence_error.code, 0) + 1
                    )
                continue
            if (
                execution_result.venue_order_fact_id is not None
                or execution_result.replayed
            ):
                intents_completed += 1

        reconciliations_completed = 0
        for intent in self._reconciliation_intents():
            try:
                reconciliation_id = self.service.reconcile_scope(
                    intent.execution_scope,
                    intent.actor_id,
                    now=self.clock(),
                )
            except domain.DomainRejected as exc:
                blocked[exc.code] = blocked.get(exc.code, 0) + 1
                continue
            reconciliations_completed += 1
            if (
                self.service.reconciliation_status(reconciliation_id)
                is domain.ReconciliationStatus.MATCH
            ):
                try:
                    self.service.close_campaign(
                        intent.campaign_id,
                        intent.actor_id,
                        now=self.clock(),
                    )
                except domain.DomainRejected as exc:
                    if exc.code not in {
                        "CAMPAIGN_POSITION_NOT_CLOSED",
                        "CAMPAIGN_EXIT_NOT_TERMINAL",
                        "RECONCILIATION_REQUIRED",
                        "RISK_RESERVATION_UNRESOLVED",
                    }:
                        blocked[exc.code] = blocked.get(exc.code, 0) + 1

        completed_at = self.clock()
        return AutomaticExecutionReport(
            started_at=started_at.isoformat(),
            completed_at=completed_at.isoformat(),
            capital_facts_refreshed=capital_facts_refreshed,
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

    def _acquire_sender(self, intent: AutomaticIntent) -> int:
        now = self.clock()
        if intent.query_only:
            return self.service.acquire_freqtrade_recovery_sender(
                intent.intent_id,
                intent.execution_scope,
                AUTOMATIC_EXECUTION_OWNER,
                intent.actor_id,
                now,
            )
        if intent.reduce_only:
            return self.service.acquire_reduce_only_sender(
                intent.intent_id,
                intent.execution_scope,
                AUTOMATIC_EXECUTION_OWNER,
                intent.actor_id,
                now,
            )
        try:
            return self.service.acquire_sender(
                intent.execution_scope,
                AUTOMATIC_EXECUTION_OWNER,
                intent.actor_id,
                now,
            )
        except domain.DomainRejected as exc:
            if exc.code != "RECONCILIATION_REQUIRED":
                raise
            next_at = self._sender_reconciliation_next_at.get(intent.execution_scope)
            if next_at is not None and now < next_at:
                raise
            self._sender_reconciliation_next_at[intent.execution_scope] = now + timedelta(
                seconds=max(60, self.settings.runtime_sync_interval_seconds)
            )
            reconciliation_id = self.service.reconcile_scope(
                intent.execution_scope,
                intent.actor_id,
                now=now,
            )
            if (
                self.service.reconciliation_status(reconciliation_id)
                is not domain.ReconciliationStatus.MATCH
            ):
                raise domain.DomainRejected(
                    "RECONCILIATION_REQUIRED",
                    "automatic sender takeover requires a computed MATCH reconciliation",
                ) from exc
            fencing_token = self.service.acquire_sender(
                intent.execution_scope,
                AUTOMATIC_EXECUTION_OWNER,
                intent.actor_id,
                self.clock(),
            )
            self._sender_reconciliation_next_at.pop(intent.execution_scope, None)
            return fencing_token

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            report = self.run_once()
            logger.info(
                "Automatic execution cycle completed",
                extra={
                    "event": "automatic_execution_cycle_completed",
                    "component": "execution-worker",
                    "result": "READY" if report.successful else "DEGRADED",
                    "capital_facts_refreshed": report.capital_facts_refreshed,
                    "risk_decisions_refreshed": report.risk_decisions_refreshed,
                    "proposals_advanced": report.proposals_advanced,
                    "intents_selected": report.intents_selected,
                    "intents_completed": report.intents_completed,
                    "reconciliations_completed": report.reconciliations_completed,
                    "blocked_count": sum(report.blocked.values()),
                    "blocked_codes": ",".join(sorted(report.blocked)) or "none",
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
