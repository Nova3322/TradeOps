from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from trading_control_plane.domain import DomainRejected, ReconciliationStatus
from trading_control_plane.execution_dispatch import ExecuteIntent, ExecuteIntentResult
from trading_control_plane.execution_runtime import (
    AUTOMATIC_EXECUTION_OWNER,
    AutomaticExecutionWorker,
    AutomaticIntent,
)


class _Service:
    def __init__(self) -> None:
        self.acquired: list[tuple[str, str]] = []
        self.reconciled: list[str] = []

    def acquire_sender(
        self,
        execution_scope: str,
        owner_id: str,
        actor_id: object,
        now: datetime,
    ) -> int:
        del actor_id, now
        self.acquired.append((execution_scope, owner_id))
        return 7

    def reconcile_scope(
        self,
        execution_scope: str,
        actor_id: object,
        *,
        now: datetime,
    ) -> object:
        del actor_id, now
        self.reconciled.append(execution_scope)
        return uuid4()


class _SenderTakeoverService(_Service):
    def __init__(self, status: str) -> None:
        super().__init__()
        self.status = status
        self.acquire_attempts = 0

    def acquire_sender(
        self,
        execution_scope: str,
        owner_id: str,
        actor_id: object,
        now: datetime,
    ) -> int:
        del actor_id, now
        self.acquire_attempts += 1
        self.acquired.append((execution_scope, owner_id))
        if self.acquire_attempts == 1 or self.status != "MATCH":
            raise DomainRejected("RECONCILIATION_REQUIRED", "fresh MATCH required")
        return 8

    def reconciliation_status(self, _reconciliation_id: object) -> ReconciliationStatus:
        return ReconciliationStatus(self.status)


def test_worker_advances_approved_proposal_and_dispatches_ready_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = object.__new__(AutomaticExecutionWorker)
    service = _Service()
    proposal_id = uuid4()
    intent = AutomaticIntent(
        intent_id=uuid4(),
        campaign_id=uuid4(),
        actor_id=uuid4(),
        execution_scope="LIVE:acct-1:BINANCE",
    )
    start = datetime(2026, 8, 20, tzinfo=UTC)
    tick = 0

    def clock() -> datetime:
        nonlocal tick
        value = start + timedelta(seconds=tick)
        tick += 1
        return value

    worker.settings = SimpleNamespace(
        runtime_sync_service_username="runtime-sync",
        freqtrade_live_leverage=1,
        execution_worker_enabled=True,
        freqtrade_workers_enabled=True,
    )
    worker.service = service
    worker.clock = clock
    worker.worker_factory = lambda _binding: object()
    safe_refreshes: list[object] = []

    def refresh_safe(**kwargs: object) -> object:
        safe_refreshes.append(kwargs)
        return uuid4()

    worker._refresh_safe_capital_fact = refresh_safe  # type: ignore[method-assign]
    worker._approved_proposal_ids = lambda *, now: (proposal_id,)  # type: ignore[method-assign]
    worker._automatic_intents = lambda: (intent,)  # type: ignore[method-assign]
    worker._reconciliation_intents = lambda: (intent,)  # type: ignore[method-assign]

    advances: list[object] = []
    dispatches: list[ExecuteIntent] = []

    def advance(*_args: object, **kwargs: object) -> dict[str, str | None]:
        advances.append(kwargs)
        return {
            "status": "READY",
            "risk_decision_id": str(uuid4()),
            "authorization_id": str(uuid4()),
            "campaign_id": str(intent.campaign_id),
            "reservation_id": str(uuid4()),
            "intent_id": str(intent.intent_id),
        }

    def dispatch(request: ExecuteIntent, **_kwargs: object) -> ExecuteIntentResult:
        dispatches.append(request)
        return ExecuteIntentResult(
            environment="LIVE",
            worker_name="binance-live",
            trade_id="trade-1",
            replayed=False,
            venue_order_fact_id=uuid4(),
        )

    monkeypatch.setattr(
        "trading_control_plane.execution_runtime.advance_approved_proposal",
        advance,
    )
    monkeypatch.setattr(
        "trading_control_plane.execution_runtime.refresh_approved_proposal_risk",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr("trading_control_plane.execution_runtime.execute_intent", dispatch)

    report = worker.run_once()

    assert report.proposals_advanced == 1
    assert report.capital_facts_refreshed == 1
    assert report.risk_decisions_refreshed == 1
    assert report.intents_selected == 1
    assert report.intents_completed == 1
    assert report.reconciliations_completed == 1
    assert report.blocked == {}
    assert len(advances) == 1
    assert len(safe_refreshes) == 1
    assert service.acquired == [
        ("LIVE:acct-1:BINANCE", AUTOMATIC_EXECUTION_OWNER)
    ]
    assert len(dispatches) == 1
    assert service.reconciled == ["LIVE:acct-1:BINANCE"]
    request = dispatches[0]
    assert request.intent_id == intent.intent_id
    assert request.idempotency_key == f"automatic-freqtrade:{intent.intent_id}"


def test_worker_records_block_and_continues_without_blind_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = object.__new__(AutomaticExecutionWorker)
    service = _Service()
    proposal_id = uuid4()
    unknown_intent = AutomaticIntent(
        intent_id=uuid4(),
        campaign_id=uuid4(),
        actor_id=uuid4(),
        execution_scope="LIVE:acct-1:BINANCE",
    )
    worker.settings = SimpleNamespace(
        runtime_sync_service_username="runtime-sync",
        freqtrade_live_leverage=1,
        execution_worker_enabled=True,
        freqtrade_workers_enabled=True,
    )
    worker.service = service
    worker.clock = lambda: datetime(2026, 8, 20, tzinfo=UTC)
    worker.worker_factory = lambda _binding: object()
    worker._refresh_safe_capital_fact = lambda **_kwargs: None  # type: ignore[method-assign]
    worker._approved_proposal_ids = lambda *, now: (proposal_id,)  # type: ignore[method-assign]
    worker._automatic_intents = lambda: (unknown_intent,)  # type: ignore[method-assign]
    worker._reconciliation_intents = lambda: ()  # type: ignore[method-assign]

    def blocked_advance(*_args: object, **_kwargs: object) -> dict[str, str | None]:
        raise DomainRejected("AUTHORIZATION_EXPIRED", "expired")

    def unknown_dispatch(*_args: object, **_kwargs: object) -> ExecuteIntentResult:
        raise DomainRejected("FREQTRADE_LIVE_OUTCOME_UNKNOWN", "unknown")

    monkeypatch.setattr(
        "trading_control_plane.execution_runtime.advance_approved_proposal",
        blocked_advance,
    )
    monkeypatch.setattr(
        "trading_control_plane.execution_runtime.refresh_approved_proposal_risk",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "trading_control_plane.execution_runtime.execute_intent",
        unknown_dispatch,
    )

    report = worker.run_once()

    assert report.proposals_advanced == 0
    assert report.capital_facts_refreshed == 0
    assert report.risk_decisions_refreshed == 0
    assert report.intents_completed == 0
    assert report.reconciliations_completed == 0
    assert report.blocked == {
        "AUTHORIZATION_EXPIRED": 1,
        "FREQTRADE_LIVE_OUTCOME_UNKNOWN": 1,
    }


def test_worker_reconciles_once_before_expired_sender_lease_takeover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = object.__new__(AutomaticExecutionWorker)
    service = _SenderTakeoverService("MATCH")
    intent = AutomaticIntent(
        intent_id=uuid4(),
        campaign_id=uuid4(),
        actor_id=uuid4(),
        execution_scope="LIVE:acct-1:BINANCE",
    )
    worker.settings = SimpleNamespace(
        runtime_sync_service_username="runtime-sync",
        runtime_sync_interval_seconds=60,
        freqtrade_live_leverage=1,
        execution_worker_enabled=True,
        freqtrade_workers_enabled=True,
    )
    worker.service = service
    worker.clock = lambda: datetime(2026, 8, 20, tzinfo=UTC)
    worker.worker_factory = lambda _binding: object()
    worker._safe_refresh_next_at = {}
    worker._sender_reconciliation_next_at = {}
    worker._refresh_safe_capital_fact = lambda **_kwargs: None  # type: ignore[method-assign]
    worker._approved_proposal_ids = lambda *, now: ()  # type: ignore[method-assign]
    worker._automatic_intents = lambda: (intent,)  # type: ignore[method-assign]
    worker._reconciliation_intents = lambda: ()  # type: ignore[method-assign]
    dispatches: list[ExecuteIntent] = []

    def dispatch(request: ExecuteIntent, **_kwargs: object) -> ExecuteIntentResult:
        dispatches.append(request)
        return ExecuteIntentResult(
            environment="LIVE",
            worker_name="binance-live",
            trade_id="trade-1",
            replayed=False,
            venue_order_fact_id=uuid4(),
        )

    monkeypatch.setattr("trading_control_plane.execution_runtime.execute_intent", dispatch)

    report = worker.run_once()

    assert report.blocked == {}
    assert report.intents_selected == 1
    assert report.intents_completed == 1
    assert service.acquire_attempts == 2
    assert service.reconciled == [intent.execution_scope]
    assert len(dispatches) == 1
    assert worker._sender_reconciliation_next_at == {}


def test_worker_keeps_nonmatching_sender_takeover_query_only_on_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = object.__new__(AutomaticExecutionWorker)
    service = _SenderTakeoverService("UNKNOWN")
    intent = AutomaticIntent(
        intent_id=uuid4(),
        campaign_id=uuid4(),
        actor_id=uuid4(),
        execution_scope="LIVE:acct-1:BINANCE",
    )
    now = datetime(2026, 8, 20, tzinfo=UTC)
    worker.settings = SimpleNamespace(
        runtime_sync_service_username="runtime-sync",
        runtime_sync_interval_seconds=60,
        freqtrade_live_leverage=1,
        execution_worker_enabled=True,
        freqtrade_workers_enabled=True,
    )
    worker.service = service
    worker.clock = lambda: now
    worker.worker_factory = lambda _binding: object()
    worker._safe_refresh_next_at = {}
    worker._sender_reconciliation_next_at = {}
    worker._refresh_safe_capital_fact = lambda **_kwargs: None  # type: ignore[method-assign]
    worker._approved_proposal_ids = lambda *, now: ()  # type: ignore[method-assign]
    worker._automatic_intents = lambda: (intent,)  # type: ignore[method-assign]
    worker._reconciliation_intents = lambda: ()  # type: ignore[method-assign]
    dispatches: list[object] = []
    monkeypatch.setattr(
        "trading_control_plane.execution_runtime.execute_intent",
        lambda *_args, **_kwargs: dispatches.append(object()),
    )

    first = worker.run_once()
    second = worker.run_once()

    assert first.blocked == {"RECONCILIATION_REQUIRED": 1}
    assert second.blocked == {"RECONCILIATION_REQUIRED": 1}
    assert service.acquire_attempts == 2
    assert service.reconciled == [intent.execution_scope]
    assert dispatches == []


def test_execution_worker_requires_both_process_switches() -> None:
    worker = object.__new__(AutomaticExecutionWorker)
    worker.settings = SimpleNamespace(
        execution_worker_enabled=False,
        freqtrade_workers_enabled=True,
    )
    with pytest.raises(DomainRejected, match="automatic execution worker") as disabled:
        worker._require_enabled()
    assert disabled.value.code == "AUTOMATIC_EXECUTION_DISABLED"

    worker.settings = SimpleNamespace(
        execution_worker_enabled=True,
        freqtrade_workers_enabled=False,
    )
    with pytest.raises(DomainRejected, match="Freqtrade execution") as freqtrade_disabled:
        worker._require_enabled()
    assert freqtrade_disabled.value.code == "FREQTRADE_EXECUTION_DISABLED"
