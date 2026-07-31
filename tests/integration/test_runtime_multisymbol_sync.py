from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select

from trading_control_plane.binance import (
    BinanceEquity,
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
from trading_control_plane.models import AccountEquity, Instrument, Position, ReconciliationRun
from trading_control_plane.runtime import RuntimeSyncWorker, SourceSyncResult
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
        max_fact_age=timedelta(seconds=30),
        now=NOW,
    )
    return service, actor


def scoped_positions(
    database: Database,
    *,
    account_id: str,
    environment: str,
) -> dict[str, Position]:
    with database.session_factory() as session:
        rows = session.execute(
            select(Position, Instrument)
            .join(Instrument, Position.instrument_id == Instrument.instrument_id)
            .where(
                Position.account_id == account_id,
                Position.venue == "BINANCE",
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
        "account-a",
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
    assert scoped_positions(database, account_id="account-a", environment="TESTNET")[
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
