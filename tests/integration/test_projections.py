from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from tests.reconciliation_fixtures import execute_reconciliation, input_envelope, start_envelope
from tests.sender_fencing_fixtures import acquire_envelope, execute_acquire, make_sender_scope
from tests.venue_fact_fixtures import (
    account_equity_snapshot_request,
    execute_venue_fact,
    position_snapshot_request,
    protection_snapshot_request,
    venue_fact_envelope,
)
from trading_control_plane.commands import CommandStatus
from trading_control_plane.database import Database
from trading_control_plane.projections import (
    CurrentAccountEquityScope,
    CurrentPositionDirection,
    CurrentPositionScope,
    CurrentPositionState,
    CurrentProtectionDirection,
    CurrentProtectionScope,
    CurrentProtectionState,
    ProjectionFreshness,
    ProjectionMaturity,
    ProjectionQueryContext,
    ProjectionState,
    VenueCurrentProjectionService,
)
from trading_control_plane.reconciliation import (
    REQUIRED_RECONCILIATION_SOURCES,
    ReconciliationSourceType,
)
from trading_control_plane.reconciliation_models import ExecutionReconciliationInput
from trading_control_plane.sender_fencing import SenderScopeBinding
from trading_control_plane.venue_fact_models import VenuePositionSnapshot
from trading_control_plane.venue_facts import (
    VenueAccountEquityState,
    VenueFactNormalizationService,
    VenuePositionDirection,
    VenuePositionState,
    VenueProtectionState,
)

pytestmark = pytest.mark.integration


def _prepare_collecting_run(
    database: Database,
    *,
    position_count: int = 0,
    balance_count: int = 0,
    protection_count: int = 0,
    sender_scope: SenderScopeBinding | None = None,
) -> tuple[UUID, int, datetime, dict[ReconciliationSourceType, ExecutionReconciliationInput]]:
    acquired_at = datetime.now(UTC)
    scope = sender_scope or make_sender_scope()
    lease_id = uuid4()
    acquired = execute_acquire(
        database,
        acquire_envelope(
            scope,
            now=acquired_at,
            lease_id=lease_id,
            ttl_seconds=300,
            max_lifetime_seconds=600,
        ),
        now=acquired_at,
    )
    assert acquired.status is CommandStatus.COMPLETED
    run_time = acquired_at + timedelta(seconds=1)
    run_id = uuid4()
    started = execute_reconciliation(
        database,
        start_envelope(run_id, scope, lease_id, 1, now=run_time),
        now=run_time,
    )
    assert started.status is CommandStatus.COMPLETED
    version = 1
    for source_type in REQUIRED_RECONCILIATION_SOURCES:
        item_count = 0
        if source_type is ReconciliationSourceType.VENUE_POSITIONS:
            item_count = position_count
        elif source_type is ReconciliationSourceType.VENUE_BALANCES:
            item_count = balance_count
        elif source_type is ReconciliationSourceType.VENUE_PROTECTION:
            item_count = protection_count
        result = execute_reconciliation(
            database,
            input_envelope(
                run_id,
                source_type,
                now=run_time,
                expected_version=version,
                item_count=item_count,
            ),
            now=run_time,
        )
        assert result.status is CommandStatus.COMPLETED
        version += 1
    with database.session_factory.begin() as session:
        inputs = {
            ReconciliationSourceType(item.source_type): item
            for item in session.scalars(
                select(ExecutionReconciliationInput).where(
                    ExecutionReconciliationInput.run_id == run_id
                )
            )
        }
    return run_id, version, run_time, inputs


def _record_account_equity(
    database: Database,
    run_id: UUID,
    reconciliation_input: ExecutionReconciliationInput,
    *,
    now: datetime,
    **updates: object,
):
    request = account_equity_snapshot_request(reconciliation_input, now=now, **updates)
    result = execute_venue_fact(
        database,
        venue_fact_envelope(
            run_id,
            VenueFactNormalizationService.account_equity_command_type,
            request.model_dump(mode="json"),
            now=now,
        ),
        now=now,
    )
    return request, result


def _record_position(
    database: Database,
    run_id: UUID,
    reconciliation_input: ExecutionReconciliationInput,
    *,
    now: datetime,
    **updates: object,
):
    request = position_snapshot_request(reconciliation_input, now=now, **updates)
    result = execute_venue_fact(
        database,
        venue_fact_envelope(
            run_id,
            VenueFactNormalizationService.position_command_type,
            request.model_dump(mode="json"),
            now=now,
        ),
        now=now,
    )
    return request, result


def _record_protection(
    database: Database,
    run_id: UUID,
    reconciliation_input: ExecutionReconciliationInput,
    position_snapshot_id: UUID,
    *,
    now: datetime,
    **updates: object,
):
    with database.session_factory.begin() as session:
        position = session.get(VenuePositionSnapshot, position_snapshot_id)
        assert position is not None
    request = protection_snapshot_request(
        reconciliation_input,
        position,
        now=now,
        **updates,
    )
    result = execute_venue_fact(
        database,
        venue_fact_envelope(
            run_id,
            VenueFactNormalizationService.protection_command_type,
            request.model_dump(mode="json"),
            now=now,
        ),
        now=now,
    )
    return request, result


def _account_scope(**updates: str) -> CurrentAccountEquityScope:
    values = {
        "organization_id": "org-1",
        "venue": "BINANCE",
        "execution_domain": "BINANCE_USDM",
        "account_id": "account-1",
        "margin_mode": "ISOLATED",
        "collateral_pool_id": "pool-usdt-1",
        "settlement_currency": "USDT",
    }
    values.update(updates)
    return CurrentAccountEquityScope.model_validate(values)


def _position_scope(*, instrument_id: str = "BTCUSDT-PERP") -> CurrentPositionScope:
    return CurrentPositionScope(
        organization_id="org-1",
        venue="BINANCE",
        execution_domain="BINANCE_USDM",
        account_id="account-1",
        instrument_id=instrument_id,
        position_mode="ONE_WAY",
        position_side="BOTH",
        margin_mode="ISOLATED",
        collateral_pool_id="pool-usdt-1",
        settlement_currency="USDT",
    )


def _protection_scope(*, instrument_id: str = "BTCUSDT-PERP") -> CurrentProtectionScope:
    return CurrentProtectionScope(
        organization_id="org-1",
        venue="BINANCE",
        execution_domain="BINANCE_USDM",
        account_id="account-1",
        instrument_id=instrument_id,
        position_mode="ONE_WAY",
        position_side="BOTH",
        margin_mode="ISOLATED",
        collateral_pool_id="pool-usdt-1",
        settlement_currency="USDT",
    )


def _context(*, as_of: datetime, max_age_ms: int = 10_000) -> ProjectionQueryContext:
    return ProjectionQueryContext(as_of=as_of, max_age_ms=max_age_ms)


def test_account_projection_uses_latest_event_not_arrival_order(database: Database) -> None:
    run_id, _, run_time, inputs = _prepare_collecting_run(database, balance_count=2)
    normalized_at = run_time + timedelta(seconds=1)
    balance_input = inputs[ReconciliationSourceType.VENUE_BALANCES]
    newer_request, newer = _record_account_equity(
        database,
        run_id,
        balance_input,
        now=normalized_at,
        venue_update_id="account-newer-arrived-first",
        event_time=run_time - timedelta(seconds=1),
        exchange_margin_equity=Decimal("12000"),
    )
    _, older = _record_account_equity(
        database,
        run_id,
        balance_input,
        now=normalized_at,
        venue_update_id="account-older-arrived-last",
        event_time=run_time - timedelta(seconds=3),
        exchange_margin_equity=Decimal("9000"),
    )
    assert newer.status is CommandStatus.COMPLETED
    assert older.status is CommandStatus.COMPLETED

    with database.session_factory.begin() as session:
        projection = VenueCurrentProjectionService.current_account_equity(
            session,
            _account_scope(),
            _context(as_of=normalized_at + timedelta(seconds=1)),
        )
        future = VenueCurrentProjectionService.current_account_equity(
            session,
            _account_scope(),
            _context(as_of=newer_request.event_time - timedelta(microseconds=1)),
        )
    assert projection.projection_state is ProjectionState.CONFIRMED
    assert projection.freshness is ProjectionFreshness.FRESH
    assert projection.maturity is ProjectionMaturity.VENUE_CONFIRMED
    assert projection.source_snapshot_id == newer_request.venue_account_equity_snapshot_id
    assert projection.source_version == newer_request.source_version
    assert projection.normalization_version == newer_request.normalization_version
    assert projection.exchange_margin_equity == Decimal("12000")
    assert projection.max_event_candidate_count == 1
    assert future.projection_state is ProjectionState.UNKNOWN
    assert future.freshness is ProjectionFreshness.UNKNOWN
    assert future.reason_code == "SOURCE_FROM_FUTURE"
    assert future.exchange_margin_equity is None


def test_account_projection_same_event_collision_fails_closed(database: Database) -> None:
    run_id, _, run_time, inputs = _prepare_collecting_run(database, balance_count=2)
    normalized_at = run_time + timedelta(seconds=1)
    event_time = run_time - timedelta(seconds=1)
    for update_id, equity in (("collision-a", "10000"), ("collision-b", "11000")):
        _, result = _record_account_equity(
            database,
            run_id,
            inputs[ReconciliationSourceType.VENUE_BALANCES],
            now=normalized_at,
            venue_update_id=update_id,
            event_time=event_time,
            exchange_margin_equity=Decimal(equity),
        )
        assert result.status is CommandStatus.COMPLETED

    with database.session_factory.begin() as session:
        projection = VenueCurrentProjectionService.current_account_equity(
            session,
            _account_scope(),
            _context(as_of=normalized_at + timedelta(seconds=1)),
        )
    assert projection.projection_state is ProjectionState.UNKNOWN
    assert projection.freshness is ProjectionFreshness.UNKNOWN
    assert projection.reason_code == "MAX_EVENT_TIME_COLLISION"
    assert projection.max_event_candidate_count == 2
    assert projection.source_snapshot_id is None
    assert projection.exchange_margin_equity is None
    assert projection.total_unrealized_pnl is None


def test_account_projection_unknown_stale_and_missing_never_expose_zero(
    database: Database,
) -> None:
    run_id, _, run_time, inputs = _prepare_collecting_run(database, balance_count=2)
    normalized_at = run_time + timedelta(seconds=1)
    request, recorded = _record_account_equity(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_BALANCES],
        now=normalized_at,
        equity_state=VenueAccountEquityState.UNKNOWN,
        event_time=run_time - timedelta(seconds=3),
    )
    assert recorded.status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        unknown = VenueCurrentProjectionService.current_account_equity(
            session,
            _account_scope(),
            _context(as_of=normalized_at + timedelta(seconds=1)),
        )
        missing = VenueCurrentProjectionService.current_account_equity(
            session,
            _account_scope(settlement_currency="USDC"),
            _context(as_of=normalized_at + timedelta(seconds=1)),
        )
    assert unknown.projection_state is ProjectionState.UNKNOWN
    assert unknown.reason_code == "SOURCE_UNKNOWN"
    assert unknown.source_snapshot_id == request.venue_account_equity_snapshot_id
    assert unknown.wallet_balance is None
    assert unknown.exchange_margin_equity is None
    assert missing.freshness is ProjectionFreshness.MISSING
    assert missing.reason_code == "SOURCE_MISSING"
    assert missing.wallet_balance is None

    _, fresh_recorded = _record_account_equity(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_BALANCES],
        now=normalized_at,
        venue_update_id="stale-source",
        event_time=run_time - timedelta(seconds=1),
    )
    assert fresh_recorded.status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        stale = VenueCurrentProjectionService.current_account_equity(
            session,
            _account_scope(),
            _context(as_of=normalized_at + timedelta(seconds=10), max_age_ms=1_000),
        )
    assert stale.projection_state is ProjectionState.UNKNOWN
    assert stale.freshness is ProjectionFreshness.STALE
    assert stale.reason_code == "SOURCE_STALE"
    assert stale.exchange_margin_equity is None
    assert stale.total_unrealized_pnl is None


def test_position_projection_is_scope_exact_and_event_ordered(database: Database) -> None:
    run_id, _, run_time, inputs = _prepare_collecting_run(database, position_count=3)
    normalized_at = run_time + timedelta(seconds=1)
    position_input = inputs[ReconciliationSourceType.VENUE_POSITIONS]
    flat_request, flat = _record_position(
        database,
        run_id,
        position_input,
        now=normalized_at,
        venue_update_id="btc-flat-newer-first",
        position_state=VenuePositionState.FLAT,
        direction=VenuePositionDirection.FLAT,
        event_time=run_time - timedelta(seconds=1),
    )
    _, open_old = _record_position(
        database,
        run_id,
        position_input,
        now=normalized_at,
        venue_update_id="btc-open-older-last",
        event_time=run_time - timedelta(seconds=3),
    )
    _, eth_unknown = _record_position(
        database,
        run_id,
        position_input,
        now=normalized_at,
        venue_update_id="eth-unknown",
        instrument_id="ETHUSDT-PERP",
        position_state=VenuePositionState.UNKNOWN,
        direction=VenuePositionDirection.UNKNOWN,
        event_time=run_time - timedelta(seconds=1),
    )
    assert flat.status is CommandStatus.COMPLETED
    assert open_old.status is CommandStatus.COMPLETED
    assert eth_unknown.status is CommandStatus.COMPLETED

    context = _context(as_of=normalized_at + timedelta(seconds=1))
    with database.session_factory.begin() as session:
        btc = VenueCurrentProjectionService.current_position(
            session,
            _position_scope(),
            context,
        )
        eth = VenueCurrentProjectionService.current_position(
            session,
            _position_scope(instrument_id="ETHUSDT-PERP"),
            context,
        )
    assert btc.projection_state is ProjectionState.CONFIRMED
    assert btc.source_snapshot_id == flat_request.venue_position_snapshot_id
    assert btc.position_state is CurrentPositionState.FLAT
    assert btc.direction is CurrentPositionDirection.FLAT
    assert btc.quantity == 0
    assert btc.notional == 0
    assert eth.projection_state is ProjectionState.UNKNOWN
    assert eth.reason_code == "SOURCE_UNKNOWN"
    assert eth.quantity is None
    assert eth.unrealized_pnl is None


def test_protection_projection_confirms_only_current_position_binding(
    database: Database,
) -> None:
    run_id, _, run_time, inputs = _prepare_collecting_run(
        database,
        position_count=1,
        protection_count=1,
    )
    normalized_at = run_time + timedelta(seconds=1)
    position_request, position_result = _record_position(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_POSITIONS],
        now=normalized_at,
    )
    protection_request, protection_result = _record_protection(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_PROTECTION],
        position_request.venue_position_snapshot_id,
        now=normalized_at,
    )
    assert position_result.status is CommandStatus.COMPLETED
    assert protection_result.status is CommandStatus.COMPLETED

    with database.session_factory.begin() as session:
        projection = VenueCurrentProjectionService.current_protection(
            session,
            _protection_scope(),
            _context(as_of=normalized_at + timedelta(seconds=1)),
        )

    assert projection.projection_state is ProjectionState.CONFIRMED
    assert projection.freshness is ProjectionFreshness.FRESH
    assert projection.maturity is ProjectionMaturity.VENUE_CONFIRMED
    assert projection.source_snapshot_id == protection_request.venue_protection_snapshot_id
    assert projection.source_position_snapshot_id == position_request.venue_position_snapshot_id
    assert projection.protection_state is CurrentProtectionState.CONFIRMED
    assert projection.protected_direction is CurrentProtectionDirection.LONG
    assert projection.position_quantity == Decimal("0.5")
    assert projection.covered_quantity == Decimal("0.5")
    assert projection.uncovered_quantity == 0
    assert projection.worst_active_trigger_price == Decimal("49900")
    assert projection.venue_native is True
    assert projection.reduce_only_confirmed is True
    assert projection.replacement_in_progress is False
    assert projection.order_set_hash == protection_request.order_set_hash


@pytest.mark.parametrize(
    ("source_state", "expected_reason"),
    [
        (VenueProtectionState.DEGRADED, "SOURCE_DEGRADED"),
        (VenueProtectionState.UNKNOWN, "SOURCE_UNKNOWN"),
    ],
)
def test_protection_projection_nonconfirmed_stale_and_missing_hide_semantics(
    database: Database,
    source_state: VenueProtectionState,
    expected_reason: str,
) -> None:
    run_id, _, run_time, inputs = _prepare_collecting_run(
        database,
        position_count=1,
        protection_count=2,
    )
    normalized_at = run_time + timedelta(seconds=1)
    position_request, position_result = _record_position(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_POSITIONS],
        now=normalized_at,
    )
    source_request, source_result = _record_protection(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_PROTECTION],
        position_request.venue_position_snapshot_id,
        now=normalized_at,
        venue_update_id=f"protection-{source_state.value.lower()}-current",
        protection_state=source_state,
    )
    assert position_result.status is CommandStatus.COMPLETED
    assert source_result.status is CommandStatus.COMPLETED

    with database.session_factory.begin() as session:
        source_projection = VenueCurrentProjectionService.current_protection(
            session,
            _protection_scope(),
            _context(as_of=normalized_at + timedelta(seconds=1)),
        )
        missing = VenueCurrentProjectionService.current_protection(
            session,
            _protection_scope(instrument_id="ETHUSDT-PERP"),
            _context(as_of=normalized_at + timedelta(seconds=1)),
        )
    assert source_projection.projection_state is ProjectionState.UNKNOWN
    assert source_projection.reason_code == expected_reason
    assert source_projection.source_snapshot_id == source_request.venue_protection_snapshot_id
    assert source_projection.source_position_snapshot_id is None
    assert source_projection.worst_active_trigger_price is None
    assert source_projection.covered_quantity is None
    assert missing.freshness is ProjectionFreshness.MISSING
    assert missing.reason_code == "SOURCE_MISSING"

    confirmed_request, confirmed_result = _record_protection(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_PROTECTION],
        position_request.venue_position_snapshot_id,
        now=normalized_at,
        venue_update_id="protection-confirmed-newer",
        event_time=run_time,
    )
    assert confirmed_result.status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        stale = VenueCurrentProjectionService.current_protection(
            session,
            _protection_scope(),
            _context(as_of=normalized_at + timedelta(seconds=10), max_age_ms=1_000),
        )
    assert stale.projection_state is ProjectionState.UNKNOWN
    assert stale.freshness is ProjectionFreshness.STALE
    assert stale.reason_code == "SOURCE_STALE"
    assert stale.source_snapshot_id == confirmed_request.venue_protection_snapshot_id
    assert stale.source_position_snapshot_id is None
    assert stale.worst_active_trigger_price is None


def test_protection_projection_same_event_collision_fails_closed(
    database: Database,
) -> None:
    run_id, _, run_time, inputs = _prepare_collecting_run(
        database,
        position_count=1,
        protection_count=2,
    )
    normalized_at = run_time + timedelta(seconds=1)
    position_request, position_result = _record_position(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_POSITIONS],
        now=normalized_at,
    )
    event_time = run_time
    assert position_result.status is CommandStatus.COMPLETED
    for venue_update_id in ("protection-collision-a", "protection-collision-b"):
        _, result = _record_protection(
            database,
            run_id,
            inputs[ReconciliationSourceType.VENUE_PROTECTION],
            position_request.venue_position_snapshot_id,
            now=normalized_at,
            venue_update_id=venue_update_id,
            event_time=event_time,
        )
        assert result.status is CommandStatus.COMPLETED

    with database.session_factory.begin() as session:
        projection = VenueCurrentProjectionService.current_protection(
            session,
            _protection_scope(),
            _context(as_of=normalized_at + timedelta(seconds=1)),
        )
    assert projection.projection_state is ProjectionState.UNKNOWN
    assert projection.reason_code == "MAX_EVENT_TIME_COLLISION"
    assert projection.max_event_candidate_count == 2
    assert projection.source_snapshot_id is None
    assert projection.source_position_snapshot_id is None
    assert projection.worst_active_trigger_price is None


def test_protection_projection_rejects_snapshot_for_superseded_position(
    database: Database,
) -> None:
    run_id, _, run_time, inputs = _prepare_collecting_run(
        database,
        position_count=2,
        protection_count=1,
    )
    normalized_at = run_time + timedelta(seconds=1)
    old_position, old_result = _record_position(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_POSITIONS],
        now=normalized_at,
        venue_update_id="position-old-protected",
        event_time=run_time - timedelta(seconds=3),
    )
    protection_request, protection_result = _record_protection(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_PROTECTION],
        old_position.venue_position_snapshot_id,
        now=normalized_at,
        event_time=run_time - timedelta(seconds=2),
    )
    new_position, new_result = _record_position(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_POSITIONS],
        now=normalized_at,
        venue_update_id="position-new-unprotected",
        event_time=run_time - timedelta(seconds=1),
    )
    assert old_result.status is CommandStatus.COMPLETED
    assert protection_result.status is CommandStatus.COMPLETED
    assert new_result.status is CommandStatus.COMPLETED

    with database.session_factory.begin() as session:
        current_position = VenueCurrentProjectionService.current_position(
            session,
            _position_scope(),
            _context(as_of=normalized_at + timedelta(seconds=1)),
        )
        protection = VenueCurrentProjectionService.current_protection(
            session,
            _protection_scope(),
            _context(as_of=normalized_at + timedelta(seconds=1)),
        )
    assert current_position.source_snapshot_id == new_position.venue_position_snapshot_id
    assert protection.projection_state is ProjectionState.UNKNOWN
    assert protection.reason_code == "POSITION_NOT_CURRENT"
    assert protection.source_snapshot_id == protection_request.venue_protection_snapshot_id
    assert protection.source_position_snapshot_id is None
    assert protection.worst_active_trigger_price is None


def test_projection_views_are_rebuildable_read_only_queries(database: Database) -> None:
    run_id, _, run_time, inputs = _prepare_collecting_run(database, balance_count=1)
    normalized_at = run_time + timedelta(seconds=1)
    _, recorded = _record_account_equity(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_BALANCES],
        now=normalized_at,
    )
    assert recorded.status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        relkind = session.execute(
            text(
                """
                SELECT relkind
                FROM pg_class
                WHERE relname = 'venue_account_equity_current_projection'
                """
            )
        ).scalar_one()
        assert relkind == "v"
        protection_relkind = session.execute(
            text(
                """
                SELECT relkind
                FROM pg_class
                WHERE relname = 'venue_protection_current_projection'
                """
            )
        ).scalar_one()
        assert protection_relkind == "v"
    with pytest.raises(DBAPIError, match="cannot update view"):
        with database.session_factory.begin() as session:
            session.execute(
                text(
                    """
                    UPDATE venue_account_equity_current_projection
                    SET exchange_margin_equity = 999999
                    WHERE organization_id = 'org-1'
                    """
                )
            )
    with pytest.raises(DBAPIError, match="cannot update view"):
        with database.session_factory.begin() as session:
            session.execute(
                text(
                    """
                    UPDATE venue_protection_current_projection
                    SET worst_active_trigger_price = 1
                    WHERE organization_id = 'org-1'
                    """
                )
            )
