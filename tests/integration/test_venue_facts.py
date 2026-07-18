from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from tests.reconciliation_fixtures import (
    execute_reconciliation,
    finish_envelope,
    input_envelope,
    phase_envelope,
    start_envelope,
)
from tests.sender_fencing_fixtures import (
    acquire_envelope,
    execute_acquire,
    make_sender_scope,
)
from tests.venue_fact_fixtures import (
    execute_venue_fact,
    fill_request,
    order_observation_request,
    position_snapshot_request,
    venue_fact_envelope,
)
from trading_control_plane.commands import CommandStatus, hash_json
from trading_control_plane.database import Database
from trading_control_plane.reconciliation import (
    REQUIRED_RECONCILIATION_SOURCES,
    ExecutionReconciliationService,
    ReconciliationPhase,
    ReconciliationSourceType,
    ReconciliationStatus,
)
from trading_control_plane.reconciliation_models import (
    ExecutionReconciliationInput,
    ExecutionReconciliationRunState,
)
from trading_control_plane.venue_fact_models import (
    VenueFactInputLink,
    VenueFill,
    VenueOrderObservation,
    VenuePositionSnapshot,
)
from trading_control_plane.venue_facts import (
    FeeEffect,
    VenueFactNormalizationService,
    VenueOrderStatus,
    VenuePositionDirection,
    VenuePositionMode,
    VenuePositionSide,
    VenuePositionState,
)

pytestmark = pytest.mark.integration


def _prepare_collecting_run(
    database: Database,
    *,
    order_count: int = 0,
    fill_count: int = 0,
    position_count: int = 0,
    now: datetime | None = None,
    lease_id: UUID | None = None,
    fencing_token: int = 1,
    supersedes_run_id: UUID | None = None,
    scope_updates: dict[str, str] | None = None,
) -> tuple[UUID, UUID, int, datetime, dict[ReconciliationSourceType, ExecutionReconciliationInput]]:
    acquired_at = now or datetime.now(UTC)
    scope = make_sender_scope(**(scope_updates or {}))
    active_lease_id = lease_id or uuid4()
    if lease_id is None:
        acquired = execute_acquire(
            database,
            acquire_envelope(
                scope,
                now=acquired_at,
                lease_id=active_lease_id,
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
        start_envelope(
            run_id,
            scope,
            active_lease_id,
            fencing_token,
            now=run_time,
            supersedes_run_id=supersedes_run_id,
        ),
        now=run_time,
    )
    assert started.status is CommandStatus.COMPLETED
    version = 1
    for source_type in REQUIRED_RECONCILIATION_SOURCES:
        item_count = 0
        if source_type is ReconciliationSourceType.VENUE_ORDERS:
            item_count = order_count
        elif source_type is ReconciliationSourceType.VENUE_FILLS:
            item_count = fill_count
        elif source_type is ReconciliationSourceType.VENUE_POSITIONS:
            item_count = position_count
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
    return run_id, active_lease_id, version, run_time, inputs


def _record_fill(
    database: Database,
    run_id: UUID,
    reconciliation_input: ExecutionReconciliationInput,
    *,
    now: datetime,
    **updates: object,
):
    request = fill_request(reconciliation_input, now=now, **updates)
    envelope = venue_fact_envelope(
        run_id,
        VenueFactNormalizationService.fill_command_type,
        request.model_dump(mode="json"),
        now=now,
    )
    return request, execute_venue_fact(database, envelope, now=now)


def _record_position(
    database: Database,
    run_id: UUID,
    reconciliation_input: ExecutionReconciliationInput,
    *,
    now: datetime,
    **updates: object,
):
    request = position_snapshot_request(reconciliation_input, now=now, **updates)
    envelope = venue_fact_envelope(
        run_id,
        VenueFactNormalizationService.position_command_type,
        request.model_dump(mode="json"),
        now=now,
    )
    return request, execute_venue_fact(database, envelope, now=now)


def test_order_and_fill_facts_are_canonical_immutable_and_unlock_comparison(
    database: Database,
) -> None:
    run_id, _, version, run_time, inputs = _prepare_collecting_run(
        database, order_count=2, fill_count=1
    )
    normalized_at = run_time + timedelta(seconds=1)
    open_order = order_observation_request(
        inputs[ReconciliationSourceType.VENUE_ORDERS],
        now=normalized_at,
    )
    open_result = execute_venue_fact(
        database,
        venue_fact_envelope(
            run_id,
            VenueFactNormalizationService.order_command_type,
            open_order.model_dump(mode="json"),
            now=normalized_at,
        ),
        now=normalized_at,
    )
    rejected_order = order_observation_request(
        inputs[ReconciliationSourceType.VENUE_ORDERS],
        now=normalized_at,
        venue_order_id="order-2",
        venue_update_id="order-2-rejected",
        status=VenueOrderStatus.REJECTED,
        known_remaining_quantity=Decimal("0"),
        zero_fill_confirmed=True,
        terminal=True,
    )
    rejected_result = execute_venue_fact(
        database,
        venue_fact_envelope(
            run_id,
            VenueFactNormalizationService.order_command_type,
            rejected_order.model_dump(mode="json"),
            now=normalized_at,
        ),
        now=normalized_at,
    )
    fill, fill_result = _record_fill(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_FILLS],
        now=normalized_at,
    )

    assert open_result.status is CommandStatus.COMPLETED
    assert rejected_result.status is CommandStatus.COMPLETED
    assert rejected_result.data["new_fact"] is True
    assert fill_result.status is CommandStatus.COMPLETED
    assert fill_result.data["new_fact"] is True
    assert fill_result.data["new_input_link"] is True
    compared = execute_reconciliation(
        database,
        phase_envelope(
            run_id,
            ReconciliationPhase.COMPARING,
            now=normalized_at,
            expected_version=version,
        ),
        now=normalized_at,
    )
    assert compared.status is CommandStatus.COMPLETED

    with database.session_factory.begin() as session:
        stored_fill = session.get(VenueFill, fill.venue_fill_id)
        stored_rejected = session.get(
            VenueOrderObservation, rejected_order.venue_order_observation_id
        )
        assert stored_fill is not None and stored_rejected is not None
        assert stored_fill.notional == Decimal("10000")
        assert stored_fill.fee_amount == Decimal("1.5")
        assert stored_fill.venue_confirmed is True
        assert stored_rejected.zero_fill_confirmed is True
        assert stored_rejected.cumulative_filled_quantity == 0
        fill_link = session.get(
            VenueFactInputLink, UUID(str(fill_result.data["venue_fact_input_link_id"]))
        )
        assert fill_link is not None
        assert fill_link.link_hash == hash_json(
            {
                "run_id": str(run_id),
                "reconciliation_input_id": str(
                    inputs[ReconciliationSourceType.VENUE_FILLS].input_id
                ),
                "organization_id": "org-1",
                "source_type": ReconciliationSourceType.VENUE_FILLS.value,
                "venue_order_observation_id": None,
                "venue_fill_id": str(fill.venue_fill_id),
                "input_hash": inputs[ReconciliationSourceType.VENUE_FILLS].input_hash,
                "fact_hash": fill.fill_hash,
                "raw_payload_hash": fill.raw_payload_hash,
                "evidence_hash": fill.evidence_hash,
                "observed_at": fill.venue_observed_at.astimezone(UTC).isoformat(),
                "received_at": fill.received_at.astimezone(UTC).isoformat(),
            }
        )
        assert session.scalar(select(func.count()).select_from(VenueFactInputLink)) == 3
    with pytest.raises(DBAPIError, match="venue_fills is immutable"):
        with database.session_factory.begin() as session:
            session.execute(
                update(VenueFill)
                .where(VenueFill.venue_fill_id == fill.venue_fill_id)
                .values(fee_amount=Decimal("999"))
            )


def test_duplicate_trade_is_global_and_can_link_to_a_later_reconciliation(
    database: Database,
) -> None:
    run_id, lease_id, version, run_time, inputs = _prepare_collecting_run(database, fill_count=1)
    normalized_at = run_time + timedelta(seconds=1)
    first_request, first_result = _record_fill(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_FILLS],
        now=normalized_at,
        venue_trade_id="trade-global-1",
    )
    assert first_result.status is CommandStatus.COMPLETED
    compared = execute_reconciliation(
        database,
        phase_envelope(
            run_id,
            ReconciliationPhase.COMPARING,
            now=normalized_at,
            expected_version=version,
        ),
        now=normalized_at,
    )
    finished = execute_reconciliation(
        database,
        finish_envelope(
            run_id,
            ReconciliationStatus.SUCCEEDED,
            now=normalized_at,
            expected_version=compared.object_version or version + 1,
        ),
        now=normalized_at,
    )
    assert finished.status is CommandStatus.COMPLETED

    second_start = normalized_at + timedelta(seconds=1)
    second_run_id, _, _, second_run_time, second_inputs = _prepare_collecting_run(
        database,
        fill_count=1,
        now=second_start,
        lease_id=lease_id,
        supersedes_run_id=run_id,
    )
    second_payload = first_request.model_dump(mode="json")
    second_payload.update(
        {
            "venue_fact_input_link_id": str(uuid4()),
            "venue_fill_id": str(uuid4()),
            "reconciliation_input_id": str(
                second_inputs[ReconciliationSourceType.VENUE_FILLS].input_id
            ),
            "reconciliation_input_hash": second_inputs[
                ReconciliationSourceType.VENUE_FILLS
            ].input_hash,
            "normalization_version": "venue-normalizer-v2",
            "normalized_payload": {
                "trade_id": "trade-global-1",
                "quantity": "0.2",
                "price": "50000",
                "adapter_shape": "v2",
            },
            "raw_payload_ref": "test-only:raw-fill:trade-global-1:second-collection",
            "raw_payload_hash": hash_json({"raw_trade_id": "trade-global-1", "collection": 2}),
            "evidence_ref": "test-only:fill-evidence:trade-global-1:second-collection",
        }
    )
    second_payload["evidence_hash"] = hash_json(
        {key: value for key, value in second_payload.items() if key != "evidence_hash"}
    )
    second_result = execute_venue_fact(
        database,
        venue_fact_envelope(
            second_run_id,
            VenueFactNormalizationService.fill_command_type,
            second_payload,
            now=second_run_time + timedelta(seconds=1),
        ),
        now=second_run_time + timedelta(seconds=1),
    )
    assert second_result.status is CommandStatus.COMPLETED
    assert second_result.data["venue_fact_id"] == str(first_request.venue_fill_id)
    assert second_result.data["new_fact"] is False
    assert second_result.data["new_input_link"] is True
    with database.session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(VenueFill)) == 1
        assert session.scalar(select(func.count()).select_from(VenueFactInputLink)) == 2


def test_same_trade_identity_with_different_economics_fails_closed(
    database: Database,
) -> None:
    run_id, _, _, run_time, inputs = _prepare_collecting_run(database, fill_count=1)
    normalized_at = run_time + timedelta(seconds=1)
    _, first = _record_fill(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_FILLS],
        now=normalized_at,
        venue_trade_id="trade-conflict-1",
    )
    _, conflict = _record_fill(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_FILLS],
        now=normalized_at,
        venue_trade_id="trade-conflict-1",
        price=Decimal("51000"),
    )
    assert first.status is CommandStatus.COMPLETED
    assert conflict.status is CommandStatus.REJECTED
    assert conflict.error_code == "VENUE_FILL_CONFLICT"
    with database.session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(VenueFill)) == 1
        assert session.scalar(select(func.count()).select_from(VenueFactInputLink)) == 1


def test_exact_count_gate_and_overcount_rollback(database: Database) -> None:
    run_id, _, version, run_time, inputs = _prepare_collecting_run(database, fill_count=1)
    normalized_at = run_time + timedelta(seconds=1)
    premature = execute_reconciliation(
        database,
        phase_envelope(
            run_id,
            ReconciliationPhase.COMPARING,
            now=normalized_at,
            expected_version=version,
        ),
        now=normalized_at,
    )
    assert premature.status is CommandStatus.REJECTED
    assert premature.error_code == "RECONCILIATION_NORMALIZED_FACT_COUNT_MISMATCH"
    _, first = _record_fill(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_FILLS],
        now=normalized_at,
        venue_trade_id="trade-count-1",
    )
    _, excess = _record_fill(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_FILLS],
        now=normalized_at,
        venue_trade_id="trade-count-2",
    )
    assert first.status is CommandStatus.COMPLETED
    assert excess.status is CommandStatus.REJECTED
    assert excess.error_code == "VENUE_FACT_INPUT_COUNT_EXCEEDED"
    with database.session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(VenueFill)) == 1
        assert session.scalar(select(func.count()).select_from(VenueFactInputLink)) == 1
    compared = execute_reconciliation(
        database,
        phase_envelope(
            run_id,
            ReconciliationPhase.COMPARING,
            now=normalized_at,
            expected_version=version,
        ),
        now=normalized_at,
    )
    assert compared.status is CommandStatus.COMPLETED


def test_out_of_order_fills_and_signed_fees_remain_distinct_facts(
    database: Database,
) -> None:
    run_id, _, version, run_time, inputs = _prepare_collecting_run(database, fill_count=3)
    normalized_at = run_time + timedelta(seconds=1)
    input_row = inputs[ReconciliationSourceType.VENUE_FILLS]
    cases = (
        ("trade-late", run_time - timedelta(seconds=1), Decimal("1"), FeeEffect.CHARGE),
        ("trade-early", run_time - timedelta(seconds=4), Decimal("-0.2"), FeeEffect.REBATE),
        ("trade-middle", run_time - timedelta(seconds=2), Decimal("0"), FeeEffect.ZERO),
    )
    for trade_id, event_time, fee_amount, fee_effect in cases:
        _, result = _record_fill(
            database,
            run_id,
            input_row,
            now=normalized_at,
            venue_trade_id=trade_id,
            quantity=Decimal("0.1"),
            fee_amount=fee_amount,
            fee_effect=fee_effect,
            event_time=event_time,
        )
        assert result.status is CommandStatus.COMPLETED
    compared = execute_reconciliation(
        database,
        phase_envelope(
            run_id,
            ReconciliationPhase.COMPARING,
            now=normalized_at,
            expected_version=version,
        ),
        now=normalized_at,
    )
    assert compared.status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        fills = list(session.scalars(select(VenueFill).order_by(VenueFill.event_time)))
        assert [fill.venue_trade_id for fill in fills] == [
            "trade-early",
            "trade-middle",
            "trade-late",
        ]
        assert [(fill.fee_effect, fill.fee_amount) for fill in fills] == [
            ("REBATE", Decimal("-0.2")),
            ("ZERO", Decimal("0")),
            ("CHARGE", Decimal("1")),
        ]


def test_wrong_input_binding_and_direct_state_bypass_are_rejected(
    database: Database,
) -> None:
    run_id, _, version, run_time, inputs = _prepare_collecting_run(database, fill_count=1)
    normalized_at = run_time + timedelta(seconds=1)
    request = fill_request(inputs[ReconciliationSourceType.VENUE_FILLS], now=normalized_at)
    payload = request.model_dump(mode="json")
    payload["reconciliation_input_id"] = str(inputs[ReconciliationSourceType.VENUE_ORDERS].input_id)
    payload["reconciliation_input_hash"] = inputs[ReconciliationSourceType.VENUE_ORDERS].input_hash
    payload["evidence_hash"] = hash_json(
        {key: value for key, value in payload.items() if key != "evidence_hash"}
    )
    wrong_input = execute_venue_fact(
        database,
        venue_fact_envelope(
            run_id,
            VenueFactNormalizationService.fill_command_type,
            payload,
            now=normalized_at,
        ),
        now=normalized_at,
    )
    assert wrong_input.status is CommandStatus.REJECTED
    assert wrong_input.error_code == "VENUE_FACT_INPUT_MISMATCH"

    with pytest.raises(DBAPIError, match="advanced reconciliation venue fact count mismatch"):
        with database.session_factory.begin() as session:
            session.execute(
                update(ExecutionReconciliationRunState)
                .where(ExecutionReconciliationRunState.run_id == run_id)
                .values(
                    phase="COMPARING",
                    version=version + 1,
                    reason_code="DIRECT_DB_BYPASS",
                    source_ref="test-only:direct-db-bypass",
                    updated_at=normalized_at,
                )
            )
    premature = execute_reconciliation(
        database,
        phase_envelope(
            run_id,
            ReconciliationPhase.COMPARING,
            now=normalized_at,
            expected_version=version,
        ),
        now=normalized_at,
    )
    assert premature.error_code == "RECONCILIATION_NORMALIZED_FACT_COUNT_MISMATCH"


def test_only_internal_reconciliation_service_can_normalize(database: Database) -> None:
    run_id, _, _, run_time, inputs = _prepare_collecting_run(database, fill_count=1)
    normalized_at = run_time + timedelta(seconds=1)
    request = fill_request(inputs[ReconciliationSourceType.VENUE_FILLS], now=normalized_at)
    envelope = venue_fact_envelope(
        run_id,
        VenueFactNormalizationService.fill_command_type,
        request.model_dump(mode="json"),
        now=normalized_at,
    ).model_copy(update={"service_principal": "untrusted-worker"})
    result = execute_venue_fact(database, envelope, now=normalized_at)
    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "VENUE_FACT_SERVICE_REQUIRED"
    assert ExecutionReconciliationService.input_command_type != envelope.command_type


def test_direct_canonical_fact_without_first_link_is_rolled_back(
    database: Database,
) -> None:
    run_id, _, _, run_time, inputs = _prepare_collecting_run(database, fill_count=1)
    normalized_at = run_time + timedelta(seconds=1)
    reconciliation_input = inputs[ReconciliationSourceType.VENUE_FILLS]
    request = fill_request(reconciliation_input, now=normalized_at)
    direct_fill = VenueFill(
        venue_fill_id=request.venue_fill_id,
        organization_id="org-1",
        first_seen_run_id=run_id,
        first_seen_input_id=reconciliation_input.input_id,
        venue=request.venue,
        execution_domain=request.execution_domain,
        account_id=request.account_id,
        instrument_id=request.instrument_id,
        observed_client_order_id=request.observed_client_order_id,
        venue_order_id=request.venue_order_id,
        venue_trade_id=request.venue_trade_id,
        side=request.side.value,
        position_side=request.position_side.value,
        reduce_only=request.reduce_only,
        quantity=request.quantity,
        price=request.price,
        contract_multiplier=request.contract_multiplier,
        notional=request.notional,
        liquidity_role=request.liquidity_role.value,
        fee_amount=request.fee_amount,
        fee_currency=request.fee_currency,
        fee_effect=request.fee_effect.value,
        realized_pnl=request.realized_pnl,
        settlement_currency=request.settlement_currency,
        venue_confirmed=True,
        fact_authority="VENUE_PRIVATE",
        environment="SHADOW",
        live_dispatch_eligible=False,
        source_version=request.source_version,
        normalization_version=request.normalization_version,
        normalized_payload=request.normalized_payload,
        raw_payload_ref=request.raw_payload_ref,
        raw_payload_hash=request.raw_payload_hash,
        evidence_ref=request.evidence_ref,
        evidence_hash=request.evidence_hash,
        fill_hash=request.fill_hash,
        event_time=request.event_time,
        venue_observed_at=request.venue_observed_at,
        first_received_at=request.received_at,
        recorded_at=normalized_at,
    )
    with pytest.raises(
        DBAPIError, match="canonical venue fact requires its first immutable input link"
    ):
        with database.session_factory.begin() as session:
            session.add(direct_fill)
    with database.session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(VenueFill)) == 0


def test_position_open_flat_unknown_are_canonical_and_gate_comparison(
    database: Database,
) -> None:
    run_id, _, version, run_time, inputs = _prepare_collecting_run(database, position_count=3)
    normalized_at = run_time + timedelta(seconds=1)
    with pytest.raises(DBAPIError, match="advanced reconciliation venue fact count mismatch"):
        with database.session_factory.begin() as session:
            session.execute(
                update(ExecutionReconciliationRunState)
                .where(ExecutionReconciliationRunState.run_id == run_id)
                .values(
                    phase="COMPARING",
                    version=version + 1,
                    reason_code="DIRECT_POSITION_MANIFEST_BYPASS",
                    source_ref="test-only:direct-position-manifest-bypass",
                    updated_at=normalized_at,
                )
            )
    premature = execute_reconciliation(
        database,
        phase_envelope(
            run_id,
            ReconciliationPhase.COMPARING,
            now=normalized_at,
            expected_version=version,
        ),
        now=normalized_at,
    )
    assert premature.status is CommandStatus.REJECTED
    assert premature.error_code == "RECONCILIATION_NORMALIZED_FACT_COUNT_MISMATCH"

    position_input = inputs[ReconciliationSourceType.VENUE_POSITIONS]
    open_request, open_result = _record_position(
        database,
        run_id,
        position_input,
        now=normalized_at,
        venue_update_id="position-open-1",
        instrument_id="BTCUSDT-PERP",
    )
    flat_request, flat_result = _record_position(
        database,
        run_id,
        position_input,
        now=normalized_at,
        venue_update_id="position-flat-1",
        instrument_id="ETHUSDT-PERP",
        position_state=VenuePositionState.FLAT,
        direction=VenuePositionDirection.FLAT,
        mark_price=Decimal("3000"),
    )
    unknown_request, unknown_result = _record_position(
        database,
        run_id,
        position_input,
        now=normalized_at,
        venue_update_id="position-unknown-1",
        instrument_id="SOLUSDT-PERP",
        position_state=VenuePositionState.UNKNOWN,
        direction=VenuePositionDirection.UNKNOWN,
    )
    _, excess_result = _record_position(
        database,
        run_id,
        position_input,
        now=normalized_at,
        venue_update_id="position-excess-1",
        instrument_id="XRPUSDT-PERP",
    )
    assert open_result.status is CommandStatus.COMPLETED
    assert flat_result.status is CommandStatus.COMPLETED
    assert unknown_result.status is CommandStatus.COMPLETED
    assert excess_result.status is CommandStatus.REJECTED
    assert excess_result.error_code == "VENUE_FACT_INPUT_COUNT_EXCEEDED"

    compared = execute_reconciliation(
        database,
        phase_envelope(
            run_id,
            ReconciliationPhase.COMPARING,
            now=normalized_at,
            expected_version=version,
        ),
        now=normalized_at,
    )
    assert compared.status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        open_snapshot = session.get(VenuePositionSnapshot, open_request.venue_position_snapshot_id)
        flat_snapshot = session.get(VenuePositionSnapshot, flat_request.venue_position_snapshot_id)
        unknown_snapshot = session.get(
            VenuePositionSnapshot, unknown_request.venue_position_snapshot_id
        )
        assert open_snapshot is not None
        assert open_snapshot.position_state == "OPEN"
        assert open_snapshot.notional == Decimal("25000")
        assert open_snapshot.unrealized_pnl == Decimal("500")
        assert flat_snapshot is not None
        assert flat_snapshot.position_state == "FLAT"
        assert flat_snapshot.quantity == 0
        assert flat_snapshot.notional == 0
        assert unknown_snapshot is not None
        assert unknown_snapshot.position_state == "UNKNOWN"
        assert unknown_snapshot.quantity is None
        assert session.scalar(select(func.count()).select_from(VenueFactInputLink)) == 3
    with pytest.raises(DBAPIError, match="venue_position_snapshots is immutable"):
        with database.session_factory.begin() as session:
            session.execute(
                update(VenuePositionSnapshot)
                .where(
                    VenuePositionSnapshot.venue_position_snapshot_id
                    == open_request.venue_position_snapshot_id
                )
                .values(quantity=Decimal("99"))
            )


def test_position_snapshot_is_global_relinkable_and_conflicts_on_changed_semantics(
    database: Database,
) -> None:
    run_id, lease_id, version, run_time, inputs = _prepare_collecting_run(
        database, position_count=1
    )
    normalized_at = run_time + timedelta(seconds=1)
    first_request, first_result = _record_position(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_POSITIONS],
        now=normalized_at,
        venue_update_id="position-global-1",
    )
    assert first_result.status is CommandStatus.COMPLETED
    compared = execute_reconciliation(
        database,
        phase_envelope(
            run_id,
            ReconciliationPhase.COMPARING,
            now=normalized_at,
            expected_version=version,
        ),
        now=normalized_at,
    )
    finished = execute_reconciliation(
        database,
        finish_envelope(
            run_id,
            ReconciliationStatus.SUCCEEDED,
            now=normalized_at,
            expected_version=compared.object_version or version + 1,
        ),
        now=normalized_at,
    )
    assert finished.status is CommandStatus.COMPLETED

    second_run_id, _, _, second_run_time, second_inputs = _prepare_collecting_run(
        database,
        position_count=1,
        now=normalized_at + timedelta(seconds=1),
        lease_id=lease_id,
        supersedes_run_id=run_id,
    )
    second_request, second_result = _record_position(
        database,
        second_run_id,
        second_inputs[ReconciliationSourceType.VENUE_POSITIONS],
        now=second_run_time + timedelta(seconds=1),
        venue_update_id="position-global-1",
        event_time=first_request.event_time,
    )
    assert second_result.status is CommandStatus.COMPLETED
    assert second_result.data["venue_fact_id"] == str(first_request.venue_position_snapshot_id)
    assert second_result.data["venue_fact_id"] != str(second_request.venue_position_snapshot_id)
    assert second_result.data["new_fact"] is False
    assert second_result.data["new_input_link"] is True
    _, conflict = _record_position(
        database,
        second_run_id,
        second_inputs[ReconciliationSourceType.VENUE_POSITIONS],
        now=second_run_time + timedelta(seconds=1),
        venue_update_id="position-global-1",
        event_time=first_request.event_time,
        mark_price=Decimal("51000"),
    )
    assert conflict.status is CommandStatus.REJECTED
    assert conflict.error_code == "VENUE_POSITION_SNAPSHOT_CONFLICT"
    with database.session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(VenuePositionSnapshot)) == 1
        assert session.scalar(select(func.count()).select_from(VenueFactInputLink)) == 2


def test_position_scope_and_tampered_economics_fail_closed(database: Database) -> None:
    run_id, _, _, run_time, inputs = _prepare_collecting_run(database, position_count=1)
    normalized_at = run_time + timedelta(seconds=1)
    position_input = inputs[ReconciliationSourceType.VENUE_POSITIONS]
    _, wrong_scope = _record_position(
        database,
        run_id,
        position_input,
        now=normalized_at,
        margin_mode="CROSS",
    )
    assert wrong_scope.status is CommandStatus.REJECTED
    assert wrong_scope.error_code == "VENUE_POSITION_SCOPE_MISMATCH"

    request = position_snapshot_request(position_input, now=normalized_at)
    tampered_payload = request.model_dump(mode="json")
    tampered_payload["quantity"] = "0.6"
    tampered = execute_venue_fact(
        database,
        venue_fact_envelope(
            run_id,
            VenueFactNormalizationService.position_command_type,
            tampered_payload,
            now=normalized_at,
        ),
        now=normalized_at,
    )
    assert tampered.status is CommandStatus.REJECTED
    assert tampered.error_code == "VENUE_POSITION_SNAPSHOT_INVALID"
    with database.session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(VenuePositionSnapshot)) == 0
        assert session.scalar(select(func.count()).select_from(VenueFactInputLink)) == 0


def test_hedge_position_lines_preserve_independent_long_and_short_sides(
    database: Database,
) -> None:
    run_id, _, version, run_time, inputs = _prepare_collecting_run(
        database,
        position_count=2,
        scope_updates={"position_mode": "HEDGE"},
    )
    normalized_at = run_time + timedelta(seconds=1)
    position_input = inputs[ReconciliationSourceType.VENUE_POSITIONS]
    long_request, long_result = _record_position(
        database,
        run_id,
        position_input,
        now=normalized_at,
        venue_update_id="hedge-update-1",
        position_mode=VenuePositionMode.HEDGE,
        position_side=VenuePositionSide.LONG,
        direction=VenuePositionDirection.LONG,
    )
    short_request, short_result = _record_position(
        database,
        run_id,
        position_input,
        now=normalized_at,
        venue_update_id="hedge-update-1",
        position_mode=VenuePositionMode.HEDGE,
        position_side=VenuePositionSide.SHORT,
        direction=VenuePositionDirection.SHORT,
        quantity=Decimal("0.2"),
    )
    assert long_result.status is CommandStatus.COMPLETED
    assert short_result.status is CommandStatus.COMPLETED
    compared = execute_reconciliation(
        database,
        phase_envelope(
            run_id,
            ReconciliationPhase.COMPARING,
            now=normalized_at,
            expected_version=version,
        ),
        now=normalized_at,
    )
    assert compared.status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        long_snapshot = session.get(VenuePositionSnapshot, long_request.venue_position_snapshot_id)
        short_snapshot = session.get(
            VenuePositionSnapshot, short_request.venue_position_snapshot_id
        )
        assert long_snapshot is not None and short_snapshot is not None
        assert (long_snapshot.position_side, long_snapshot.direction) == ("LONG", "LONG")
        assert (short_snapshot.position_side, short_snapshot.direction) == (
            "SHORT",
            "SHORT",
        )


def test_direct_position_snapshot_without_first_link_is_rolled_back(
    database: Database,
) -> None:
    run_id, _, _, run_time, inputs = _prepare_collecting_run(database, position_count=1)
    normalized_at = run_time + timedelta(seconds=1)
    reconciliation_input = inputs[ReconciliationSourceType.VENUE_POSITIONS]
    request = position_snapshot_request(reconciliation_input, now=normalized_at)
    direct_snapshot = VenuePositionSnapshot(
        venue_position_snapshot_id=request.venue_position_snapshot_id,
        organization_id="org-1",
        first_seen_run_id=run_id,
        first_seen_input_id=reconciliation_input.input_id,
        venue=request.venue,
        execution_domain=request.execution_domain,
        account_id=request.account_id,
        instrument_id=request.instrument_id,
        venue_update_id=request.venue_update_id,
        position_mode=request.position_mode.value,
        position_side=request.position_side.value,
        margin_mode=request.margin_mode,
        collateral_pool_id=request.collateral_pool_id,
        position_state=request.position_state.value,
        direction=request.direction.value,
        quantity=request.quantity,
        entry_price=request.entry_price,
        mark_price=request.mark_price,
        contract_multiplier=request.contract_multiplier,
        notional=request.notional,
        unrealized_pnl=request.unrealized_pnl,
        liquidation_price=request.liquidation_price,
        leverage=request.leverage,
        initial_margin=request.initial_margin,
        maintenance_margin=request.maintenance_margin,
        settlement_currency=request.settlement_currency,
        venue_confirmed=True,
        fact_authority="VENUE_PRIVATE",
        environment="SHADOW",
        live_dispatch_eligible=False,
        source_version=request.source_version,
        normalization_version=request.normalization_version,
        normalized_payload=request.normalized_payload,
        raw_payload_ref=request.raw_payload_ref,
        raw_payload_hash=request.raw_payload_hash,
        evidence_ref=request.evidence_ref,
        evidence_hash=request.evidence_hash,
        snapshot_hash=request.snapshot_hash,
        event_time=request.event_time,
        venue_observed_at=request.venue_observed_at,
        first_received_at=request.received_at,
        recorded_at=normalized_at,
    )
    with pytest.raises(
        DBAPIError, match="canonical venue fact requires its first immutable input link"
    ):
        with database.session_factory.begin() as session:
            session.add(direct_snapshot)
    with database.session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(VenuePositionSnapshot)) == 0
