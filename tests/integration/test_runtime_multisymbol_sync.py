from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select

from trading_control_plane.binance import (
    BinanceEquity,
    BinanceFill,
    BinanceFunding,
    BinanceInstrument,
    BinancePosition,
    BinanceReadOnlySnapshot,
)
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    DomainRejected,
    ExecutionEnvironment,
    ReconciliationStatus,
    Role,
    SystemRiskState,
)
from trading_control_plane.hyperliquid import (
    HyperliquidEquity,
    HyperliquidInstrument,
    HyperliquidPosition,
    HyperliquidReadOnlySnapshot,
)
from trading_control_plane.models import (
    AccountEquity,
    FundingPayment,
    Instrument,
    Position,
    ReconciliationRun,
    VenueFill,
)
from trading_control_plane.runtime import RuntimeSyncReport, RuntimeSyncWorker, SourceSyncResult
from trading_control_plane.service import TradingService

NOW = datetime(2026, 7, 31, 12, tzinfo=UTC)


def snapshot(symbol: str, quantity: Decimal, now: datetime) -> BinanceReadOnlySnapshot:
    return BinanceReadOnlySnapshot(
        symbol=symbol,
        observed_at=now,
        instrument=BinanceInstrument(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            lot_size=Decimal("0.001"),
            minimum_notional=Decimal(5),
            quote_currency="USDT",
            collateral_currency="USDT",
            active=True,
        ),
        orders=(),
        fills=(),
        position=BinancePosition(
            quantity=quantity,
            average_entry_price=Decimal(0) if quantity == 0 else Decimal(100),
            mark_price=Decimal(110),
            observed_at=now,
        ),
        equity=BinanceEquity(
            equity=Decimal(1_000),
            available_balance=Decimal(900),
            currency="USD",
            observed_at=now,
        ),
        funding=(),
        protection=None,
    )


def seed(database: Database, label: str) -> tuple[TradingService, UUID]:
    service = TradingService(database)
    admin = service.bootstrap_admin(f"{label}-admin", now=NOW)
    actor = service.create_service_principal(f"{label}-runtime", admin, now=NOW)
    service.assign_role(actor, Role.OPERATOR, admin, now=NOW)
    service.set_risk_policy(
        actor_id=admin,
        version=f"{label}-risk-v1",
        system_state=SystemRiskState.NORMAL,
        max_total_risk=Decimal(1_000),
        max_account_risk=Decimal(1_000),
        max_single_loss=Decimal(1_000),
        max_consecutive_losses=3,
        loss_cooldown=timedelta(hours=1),
        max_fact_age=timedelta(seconds=30),
        now=NOW,
    )
    return service, actor


def scoped_positions(
    database: Database,
    *,
    account_id: str,
    environment: str,
    venue: str = "BINANCE",
) -> dict[str, Position]:
    with database.session_factory() as session:
        rows = session.execute(
            select(Position, Instrument)
            .join(Instrument, Position.instrument_id == Instrument.instrument_id)
            .where(
                Position.account_id == account_id,
                Position.venue == venue,
                Position.environment == environment,
            )
        ).all()
        return {instrument.symbol: position for position, instrument in rows}


def ingest(
    service: TradingService,
    actor: UUID,
    account_id: str,
    environment: ExecutionEnvironment,
    now: datetime,
    *snapshots: BinanceReadOnlySnapshot,
) -> dict[str, Any]:
    return service.ingest_binance_read_only_account_snapshot(
        account_id,
        actor,
        snapshots,
        environment=environment,
        now=now,
    )


def with_history(
    value: BinanceReadOnlySnapshot,
    *,
    suffix: str,
) -> BinanceReadOnlySnapshot:
    return replace(
        value,
        fills=(
            BinanceFill(
                fill_id=f"fill-{suffix}",
                order_id=f"order-{suffix}",
                side="BUY",
                quantity=Decimal(1),
                price=Decimal(100),
                fee=Decimal(0),
                fee_currency="USDT",
                executed_at=value.observed_at,
            ),
        ),
        funding=(
            BinanceFunding(
                payment_id=f"funding-{suffix}",
                amount=Decimal(0),
                currency="USDT",
                paid_at=value.observed_at,
            ),
        ),
    )


def hyperliquid_snapshot(
    symbol: str,
    quantity: Decimal,
    now: datetime,
    *,
    equity: Decimal = Decimal(1_000),
) -> HyperliquidReadOnlySnapshot:
    return HyperliquidReadOnlySnapshot(
        symbol=symbol,
        observed_at=now,
        instrument=HyperliquidInstrument(
            symbol=symbol,
            tick_size=Decimal("0.1"),
            lot_size=Decimal("0.001"),
            minimum_notional=Decimal(10),
            quote_currency="USDC",
            collateral_currency="USDC",
            active=True,
        ),
        orders=(),
        fills=(),
        position=HyperliquidPosition(
            quantity=quantity,
            average_entry_price=Decimal(0) if quantity == 0 else Decimal(100),
            mark_price=Decimal(110),
            observed_at=now,
        ),
        equity=HyperliquidEquity(
            equity=equity,
            available_balance=equity,
            currency="USDC",
            observed_at=now,
        ),
        funding=(),
        protection=None,
    )


def ingest_hyperliquid(
    service: TradingService,
    actor: UUID,
    now: datetime,
    *snapshots: HyperliquidReadOnlySnapshot,
) -> dict[str, Any]:
    return service.ingest_hyperliquid_read_only_account_snapshot(
        "account-a",
        actor,
        snapshots,
        environment=ExecutionEnvironment.LIVE,
        now=now,
    )


def test_hyperliquid_partial_dex_coverage_never_clears_unqueried_hip3_position(
    database: Database,
) -> None:
    service, actor = seed(database, "hyperliquid-dex-coverage")
    ingest_hyperliquid(
        service,
        actor,
        NOW,
        hyperliquid_snapshot("BTC", Decimal(1), NOW),
        hyperliquid_snapshot("xyz:TSLA", Decimal(2), NOW),
    )

    core_only_at = NOW + timedelta(seconds=1)
    result = ingest_hyperliquid(
        service,
        actor,
        core_only_at,
        hyperliquid_snapshot("BTC", Decimal(1), core_only_at),
    )

    positions = scoped_positions(
        database,
        account_id="account-a",
        environment="LIVE",
        venue="HYPERLIQUID",
    )
    assert result["positions_covered"] == 1
    assert positions["BTC"].observed_at == core_only_at
    assert positions["xyz:TSLA"].quantity == Decimal(2)
    assert positions["xyz:TSLA"].observed_at == NOW


def test_hyperliquid_rejects_non_unified_core_and_hip3_equity(
    database: Database,
) -> None:
    service, actor = seed(database, "hyperliquid-equity-scope")

    with pytest.raises(DomainRejected, match="HYPERLIQUID_EQUITY_SCOPE_INCONSISTENT"):
        ingest_hyperliquid(
            service,
            actor,
            NOW,
            hyperliquid_snapshot("BTC", Decimal(0), NOW, equity=Decimal(100)),
            hyperliquid_snapshot("xyz:TSLA", Decimal(0), NOW, equity=Decimal(90)),
        )

    assert (
        scoped_positions(
            database,
            account_id="account-a",
            environment="LIVE",
            venue="HYPERLIQUID",
        )
        == {}
    )


def test_complete_account_snapshot_covers_multisymbol_close_empty_idempotency_and_scope(
    database: Database,
) -> None:
    service, actor = seed(database, "coverage")
    first = ingest(
        service,
        actor,
        "account-a",
        ExecutionEnvironment.LIVE,
        NOW,
        snapshot("BTCUSDT", Decimal(1), NOW),
        snapshot("ETHUSDT", Decimal(2), NOW),
    )
    ingest(
        service,
        actor,
        "account-b",
        ExecutionEnvironment.LIVE,
        NOW,
        snapshot("ETHUSDT", Decimal(3), NOW),
    )
    ingest(
        service,
        actor,
        "account-a-testnet",
        ExecutionEnvironment.TESTNET,
        NOW,
        snapshot("ETHUSDT", Decimal(4), NOW),
    )

    assert first["positions_covered"] == 2
    assert first["positions_authoritatively_closed"] == 0
    assert {
        symbol: row.quantity
        for symbol, row in scoped_positions(
            database, account_id="account-a", environment="LIVE"
        ).items()
    } == {"BTCUSDT": Decimal(1), "ETHUSDT": Decimal(2)}

    closed_at = NOW + timedelta(seconds=1)
    closed = ingest(
        service,
        actor,
        "account-a",
        ExecutionEnvironment.LIVE,
        closed_at,
        snapshot("BTCUSDT", Decimal(1), closed_at),
    )
    live = scoped_positions(database, account_id="account-a", environment="LIVE")
    assert closed["positions_covered"] == 2
    assert closed["positions_authoritatively_closed"] == 1
    assert live["ETHUSDT"].quantity == 0
    assert live["ETHUSDT"].average_entry_price == 0
    assert live["ETHUSDT"].observed_at == closed_at
    assert scoped_positions(database, account_id="account-b", environment="LIVE")[
        "ETHUSDT"
    ].quantity == Decimal(3)
    assert scoped_positions(database, account_id="account-a-testnet", environment="TESTNET")[
        "ETHUSDT"
    ].quantity == Decimal(4)

    empty_at = NOW + timedelta(seconds=2)
    empty = ingest(
        service,
        actor,
        "account-a",
        ExecutionEnvironment.LIVE,
        empty_at,
        snapshot("BTCUSDT", Decimal(0), empty_at),
    )
    duplicate = ingest(
        service,
        actor,
        "account-a",
        ExecutionEnvironment.LIVE,
        empty_at,
        snapshot("BTCUSDT", Decimal(0), empty_at),
    )
    live = scoped_positions(database, account_id="account-a", environment="LIVE")
    assert empty["positions_authoritatively_closed"] == 1
    assert duplicate["positions_authoritatively_closed"] == 0
    assert all(row.quantity == 0 and row.observed_at == empty_at for row in live.values())
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Position)) == 4
        assert session.scalar(select(func.count()).select_from(AccountEquity)) == 3


@pytest.mark.parametrize(
    "error_code",
    [
        "BINANCE_RESPONSE_INCOMPLETE",
        "BINANCE_READ_ONLY_UNAVAILABLE",
        "BINANCE_POSITION_UNKNOWN",
    ],
)
def test_partial_failed_or_unknown_account_response_never_clears_stale_positions(
    database: Database,
    error_code: str,
) -> None:
    service, actor = seed(database, "partial")
    ingest(
        service,
        actor,
        "account-a",
        ExecutionEnvironment.LIVE,
        NOW,
        snapshot("BTCUSDT", Decimal(1), NOW),
        snapshot("ETHUSDT", Decimal(2), NOW),
    )

    class PartialReader:
        @staticmethod
        def read_active_instruments() -> tuple[BinanceInstrument, ...]:
            return (snapshot("BTCUSDT", Decimal(0), NOW).instrument,)

        def read_account_snapshots(
            self, _symbols: tuple[str, ...], *, now: datetime
        ) -> tuple[BinanceReadOnlySnapshot, ...]:
            del now
            raise DomainRejected(error_code, "account response was not complete and authoritative")

    worker: Any = object.__new__(RuntimeSyncWorker)
    worker.settings = SimpleNamespace(
        runtime_binance_account_id="account-a",
        runtime_binance_symbol="BTCUSDT",
        binance_fact_environment="LIVE",
    )
    worker.binance = PartialReader()
    worker.service = service
    results: dict[str, SourceSyncResult] = {}

    RuntimeSyncWorker._attempt(
        "BINANCE",
        lambda: worker._record_binance(actor, NOW + timedelta(seconds=1)),
        results,
    )

    assert results == {"BINANCE": SourceSyncResult("FAILED", error_code=error_code)}
    positions = scoped_positions(database, account_id="account-a", environment="LIVE")
    assert positions["BTCUSDT"].quantity == Decimal(1)
    assert positions["ETHUSDT"].quantity == Decimal(2)
    assert all(row.observed_at == NOW for row in positions.values())


def test_incomplete_history_refreshes_current_domain_closes_absent_and_fails_readiness(
    database: Database,
) -> None:
    service, actor = seed(database, "history-incomplete")
    ingest(
        service,
        actor,
        "account-a",
        ExecutionEnvironment.LIVE,
        NOW,
        snapshot("BTCUSDT", Decimal(1), NOW),
        snapshot("ETHUSDT", Decimal(2), NOW),
        snapshot("SOLUSDT", Decimal(3), NOW),
    )
    refreshed_at = NOW + timedelta(seconds=1)
    incomplete = tuple(
        replace(
            snapshot(symbol, quantity, refreshed_at),
            history_error_code="BINANCE_READ_ONLY_UNAVAILABLE",
        )
        for symbol, quantity in reversed((("BTCUSDT", Decimal(5)), ("ETHUSDT", Decimal(6))))
    )

    class HistoryIncompleteReader:
        @staticmethod
        def read_active_instruments() -> tuple[BinanceInstrument, ...]:
            return tuple(item.instrument for item in incomplete)

        def read_account_snapshots(
            self, _symbols: tuple[str, ...], *, now: datetime
        ) -> tuple[BinanceReadOnlySnapshot, ...]:
            assert now == refreshed_at
            return incomplete

    worker: Any = object.__new__(RuntimeSyncWorker)
    worker.settings = SimpleNamespace(
        runtime_binance_account_id="account-a",
        runtime_binance_symbol="BTCUSDT",
        binance_fact_environment="LIVE",
    )
    worker.binance = HistoryIncompleteReader()
    worker.service = service
    results: dict[str, SourceSyncResult] = {}

    RuntimeSyncWorker._attempt(
        "BINANCE",
        lambda: worker._record_binance(actor, refreshed_at),
        results,
    )
    RuntimeSyncWorker._attempt(
        "BINANCE",
        lambda: worker._record_binance(actor, refreshed_at),
        results,
    )

    assert results == {
        "BINANCE": SourceSyncResult(
            "FAILED",
            error_code="BINANCE_HISTORY_INCOMPLETE:BINANCE_READ_ONLY_UNAVAILABLE",
        )
    }
    positions = scoped_positions(database, account_id="account-a", environment="LIVE")
    assert {symbol: (row.quantity, row.observed_at) for symbol, row in positions.items()} == {
        "BTCUSDT": (Decimal(5), refreshed_at),
        "ETHUSDT": (Decimal(6), refreshed_at),
        "SOLUSDT": (Decimal(0), refreshed_at),
    }
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(VenueFill)) == 0
        assert session.scalar(select(func.count()).select_from(FundingPayment)) == 0

    report = RuntimeSyncReport(
        started_at=refreshed_at.isoformat(),
        completed_at=refreshed_at.isoformat(),
        sources={
            **results,
            "HYPERLIQUID": SourceSyncResult("SUCCESS", items_observed=1),
            "NOTILT:42161": SourceSyncResult("SUCCESS", items_observed=1),
        },
        net_worth={"complete": True},
    )
    assert report.capital_sources_successful is False
    assert report.ready_for_new_risk is False


def test_complete_history_is_written_atomically_and_remains_idempotent(
    database: Database,
) -> None:
    service, actor = seed(database, "history-complete")
    snapshots = (
        with_history(snapshot("ETHUSDT", Decimal(2), NOW), suffix="eth"),
        with_history(snapshot("BTCUSDT", Decimal(1), NOW), suffix="btc"),
    )

    first = ingest(
        service,
        actor,
        "account-a",
        ExecutionEnvironment.LIVE,
        NOW,
        *snapshots,
    )
    duplicate = ingest(
        service,
        actor,
        "account-a",
        ExecutionEnvironment.LIVE,
        NOW,
        *tuple(reversed(snapshots)),
    )

    assert first["history_error_code"] is None
    assert duplicate["history_error_code"] is None
    assert sum(item["fills"] for item in first["symbols"].values()) == 2
    assert sum(item["funding"] for item in first["symbols"].values()) == 2
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(VenueFill)) == 2
        assert session.scalar(select(func.count()).select_from(FundingPayment)) == 2


def test_newer_account_snapshot_revision_wins_concurrent_late_arrival(
    database: Database,
) -> None:
    service, actor = seed(database, "revision")
    ingest(
        service,
        actor,
        "account-a",
        ExecutionEnvironment.LIVE,
        NOW,
        snapshot("BTCUSDT", Decimal(1), NOW),
    )
    newer_at = NOW + timedelta(seconds=2)
    older_at = NOW + timedelta(seconds=1)
    newer_done = threading.Event()
    errors: list[str] = []

    def newer() -> None:
        ingest(
            TradingService(database),
            actor,
            "account-a",
            ExecutionEnvironment.LIVE,
            newer_at,
            snapshot("BTCUSDT", Decimal(5), newer_at),
        )
        newer_done.set()

    def older() -> None:
        assert newer_done.wait(timeout=3)
        try:
            ingest(
                TradingService(database),
                actor,
                "account-a",
                ExecutionEnvironment.LIVE,
                older_at,
                snapshot("BTCUSDT", Decimal(2), older_at),
            )
        except DomainRejected as exc:
            errors.append(exc.code)

    threads = [threading.Thread(target=newer), threading.Thread(target=older)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=4)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == ["BINANCE_SNAPSHOT_SUPERSEDED"]
    position = scoped_positions(database, account_id="account-a", environment="LIVE")["BTCUSDT"]
    assert position.quantity == Decimal(5)
    assert position.observed_at == newer_at


def test_account_batch_rolls_back_all_symbols_when_later_revision_fails(
    database: Database,
) -> None:
    service, actor = seed(database, "atomic")
    ingest(
        service,
        actor,
        "account-a",
        ExecutionEnvironment.LIVE,
        NOW,
        snapshot("BTCUSDT", Decimal(1), NOW),
        snapshot("ETHUSDT", Decimal(2), NOW),
    )
    newer_at = NOW + timedelta(seconds=2)
    service.ingest_binance_read_only_snapshot(
        "account-a",
        actor,
        snapshot("ETHUSDT", Decimal(5), newer_at),
        environment=ExecutionEnvironment.LIVE,
        now=newer_at,
    )

    older_at = NOW + timedelta(seconds=1)
    with pytest.raises(DomainRejected, match="BINANCE_SNAPSHOT_SUPERSEDED"):
        ingest(
            service,
            actor,
            "account-a",
            ExecutionEnvironment.LIVE,
            older_at,
            snapshot("BTCUSDT", Decimal(9), older_at),
            snapshot("ETHUSDT", Decimal(8), older_at),
        )

    positions = scoped_positions(database, account_id="account-a", environment="LIVE")
    assert positions["BTCUSDT"].quantity == Decimal(1)
    assert positions["BTCUSDT"].observed_at == NOW
    assert positions["ETHUSDT"].quantity == Decimal(5)
    assert positions["ETHUSDT"].observed_at == newer_at


def test_reconciliation_match_difference_unknown_and_stale_use_full_scope_coverage(
    database: Database,
) -> None:
    service, actor = seed(database, "reconciliation")
    flat_at = NOW + timedelta(seconds=1)
    ingest(
        service,
        actor,
        "account-a",
        ExecutionEnvironment.LIVE,
        flat_at,
        snapshot("BTCUSDT", Decimal(0), flat_at),
    )
    scope = "LIVE:account-a:BINANCE"
    match = service.reconcile_scope(scope, actor, now=flat_at)
    assert service.reconciliation_status(match) is ReconciliationStatus.MATCH

    difference_at = NOW + timedelta(seconds=2)
    ingest(
        service,
        actor,
        "account-a",
        ExecutionEnvironment.LIVE,
        difference_at,
        snapshot("BTCUSDT", Decimal(1), difference_at),
    )
    difference = service.reconcile_scope(scope, actor, now=difference_at)
    assert service.reconciliation_status(difference) is ReconciliationStatus.DIFFERENCE

    position = scoped_positions(database, account_id="account-a", environment="LIVE")["BTCUSDT"]
    unknown_at = NOW + timedelta(seconds=3)
    service.record_position(
        "account-a",
        "BINANCE",
        position.instrument_id,
        Decimal(0),
        Decimal(0),
        Decimal(110),
        False,
        actor,
        environment=ExecutionEnvironment.LIVE,
        now=unknown_at,
    )
    unknown = service.reconcile_scope(scope, actor, now=unknown_at)
    assert service.reconciliation_status(unknown) is ReconciliationStatus.UNKNOWN

    fresh_at = NOW + timedelta(seconds=4)
    ingest(
        service,
        actor,
        "account-a",
        ExecutionEnvironment.LIVE,
        fresh_at,
        snapshot("BTCUSDT", Decimal(0), fresh_at),
    )
    stale = service.reconcile_scope(scope, actor, now=fresh_at + timedelta(seconds=31))
    assert service.reconciliation_status(stale) is ReconciliationStatus.UNKNOWN
    with database.session_factory() as session:
        run = session.get(ReconciliationRun, stale)
        assert run is not None
        assert any(item.startswith("POSITION_STALE:") for item in run.differences)
