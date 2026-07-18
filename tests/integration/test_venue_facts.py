from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text, update
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
    account_equity_snapshot_request,
    execute_venue_fact,
    fill_request,
    funding_payment_request,
    order_observation_request,
    position_snapshot_request,
    protection_snapshot_request,
    venue_fact_envelope,
)
from trading_control_plane.command_executor import IdempotentCommandExecutor
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
    VenueAccountEquitySnapshot,
    VenueFactInputLink,
    VenueFill,
    VenueFundingPayment,
    VenueOrderObservation,
    VenuePositionSnapshot,
    VenueProtectionSnapshot,
)
from trading_control_plane.venue_facts import (
    FeeEffect,
    FundingEffect,
    VenueAccountEquityState,
    VenueFactNormalizationService,
    VenueOrderStatus,
    VenuePositionDirection,
    VenuePositionMode,
    VenuePositionSide,
    VenuePositionState,
    VenueProtectedDirection,
    VenueProtectionState,
)

pytestmark = pytest.mark.integration


def _prepare_collecting_run(
    database: Database,
    *,
    order_count: int = 0,
    fill_count: int = 0,
    funding_count: int = 0,
    position_count: int = 0,
    balance_count: int = 0,
    protection_count: int = 0,
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
        elif source_type is ReconciliationSourceType.VENUE_FUNDING:
            item_count = funding_count
        elif source_type is ReconciliationSourceType.VENUE_POSITIONS:
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


def _record_funding(
    database: Database,
    run_id: UUID,
    reconciliation_input: ExecutionReconciliationInput,
    *,
    now: datetime,
    **updates: object,
):
    request = funding_payment_request(reconciliation_input, now=now, **updates)
    envelope = venue_fact_envelope(
        run_id,
        VenueFactNormalizationService.funding_command_type,
        request.model_dump(mode="json"),
        now=now,
    )
    return request, execute_venue_fact(database, envelope, now=now)


def _record_protection(
    database: Database,
    run_id: UUID,
    reconciliation_input: ExecutionReconciliationInput,
    position_snapshot: VenuePositionSnapshot,
    *,
    now: datetime,
    **updates: object,
):
    request = protection_snapshot_request(
        reconciliation_input,
        position_snapshot,
        now=now,
        **updates,
    )
    envelope = venue_fact_envelope(
        run_id,
        VenueFactNormalizationService.protection_command_type,
        request.model_dump(mode="json"),
        now=now,
    )
    return request, execute_venue_fact(database, envelope, now=now)


def _record_account_equity(
    database: Database,
    run_id: UUID,
    reconciliation_input: ExecutionReconciliationInput,
    *,
    now: datetime,
    **updates: object,
):
    request = account_equity_snapshot_request(reconciliation_input, now=now, **updates)
    envelope = venue_fact_envelope(
        run_id,
        VenueFactNormalizationService.account_equity_command_type,
        request.model_dump(mode="json"),
        now=now,
    )
    return request, execute_venue_fact(database, envelope, now=now)


def _prepare_protection_run(
    database: Database,
    *,
    protection_count: int,
    position_updates: dict[str, object] | None = None,
) -> tuple[
    UUID,
    UUID,
    int,
    datetime,
    ExecutionReconciliationInput,
    VenuePositionSnapshot,
]:
    position_run_id, lease_id, version, run_time, inputs = _prepare_collecting_run(
        database,
        position_count=1,
    )
    normalized_at = run_time + timedelta(seconds=1)
    position_request, position_result = _record_position(
        database,
        position_run_id,
        inputs[ReconciliationSourceType.VENUE_POSITIONS],
        now=normalized_at,
        **(position_updates or {}),
    )
    assert position_result.status is CommandStatus.COMPLETED
    compared = execute_reconciliation(
        database,
        phase_envelope(
            position_run_id,
            ReconciliationPhase.COMPARING,
            now=normalized_at,
            expected_version=version,
        ),
        now=normalized_at,
    )
    assert compared.status is CommandStatus.COMPLETED
    finished = execute_reconciliation(
        database,
        finish_envelope(
            position_run_id,
            ReconciliationStatus.SUCCEEDED,
            now=normalized_at,
            expected_version=compared.object_version or version + 1,
        ),
        now=normalized_at,
    )
    assert finished.status is CommandStatus.COMPLETED
    with database.session_factory.begin() as session:
        position = session.get(VenuePositionSnapshot, position_request.venue_position_snapshot_id)
        assert position is not None

    protection_run_id, _, protection_version, protection_run_time, protection_inputs = (
        _prepare_collecting_run(
            database,
            protection_count=protection_count,
            now=normalized_at + timedelta(seconds=1),
            lease_id=lease_id,
            supersedes_run_id=position_run_id,
        )
    )
    return (
        protection_run_id,
        lease_id,
        protection_version,
        protection_run_time,
        protection_inputs[ReconciliationSourceType.VENUE_PROTECTION],
        position,
    )


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


def test_funding_payments_preserve_native_signed_cost_and_unlock_comparison(
    database: Database,
) -> None:
    run_id, _, version, run_time, inputs = _prepare_collecting_run(
        database,
        funding_count=2,
    )
    normalized_at = run_time + timedelta(seconds=1)
    funding_input = inputs[ReconciliationSourceType.VENUE_FUNDING]
    payment, payment_result = _record_funding(
        database,
        run_id,
        funding_input,
        now=normalized_at,
        venue_payment_id="funding-debit-1",
        funding_amount=Decimal("2.5"),
        funding_currency="USDT",
        funding_effect=FundingEffect.PAYMENT,
    )
    receipt, receipt_result = _record_funding(
        database,
        run_id,
        funding_input,
        now=normalized_at,
        venue_payment_id="funding-credit-1",
        funding_amount=Decimal("-1.25"),
        funding_currency="USDT",
        funding_effect=FundingEffect.RECEIPT,
    )

    assert payment_result.status is CommandStatus.COMPLETED
    assert receipt_result.status is CommandStatus.COMPLETED
    assert payment_result.data["fact_type"] == "FUNDING_PAYMENT"
    invalid_payload = dict(payment.model_dump(mode="json"))
    invalid_payload["funding_effect"] = "RECEIPT"
    invalid = execute_venue_fact(
        database,
        venue_fact_envelope(
            run_id,
            VenueFactNormalizationService.funding_command_type,
            invalid_payload,
            now=normalized_at,
        ),
        now=normalized_at,
    )
    assert invalid.status is CommandStatus.REJECTED
    assert invalid.error_code == "VENUE_FUNDING_PAYMENT_INVALID"

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
        stored_payment = session.get(
            VenueFundingPayment,
            payment.venue_funding_payment_id,
        )
        stored_receipt = session.get(
            VenueFundingPayment,
            receipt.venue_funding_payment_id,
        )
        assert stored_payment is not None and stored_receipt is not None
        assert stored_payment.funding_amount == Decimal("2.5")
        assert stored_payment.funding_effect == "PAYMENT"
        assert stored_receipt.funding_amount == Decimal("-1.25")
        assert stored_receipt.funding_effect == "RECEIPT"
        assert stored_payment.funding_currency == stored_receipt.funding_currency == "USDT"
        links = tuple(
            session.scalars(
                select(VenueFactInputLink).where(VenueFactInputLink.source_type == "VENUE_FUNDING")
            )
        )
        assert {link.venue_funding_payment_id for link in links} == {
            payment.venue_funding_payment_id,
            receipt.venue_funding_payment_id,
        }

    with pytest.raises(DBAPIError, match="venue_funding_payments is immutable"):
        with database.session_factory.begin() as session:
            session.execute(
                update(VenueFundingPayment)
                .where(
                    VenueFundingPayment.venue_funding_payment_id == payment.venue_funding_payment_id
                )
                .values(funding_amount=Decimal("3"))
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


def test_protection_states_are_canonical_immutable_and_unlock_manifest(
    database: Database,
) -> None:
    run_id, _, version, run_time, protection_input, position = _prepare_protection_run(
        database,
        protection_count=3,
    )
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

    confirmed_request, confirmed = _record_protection(
        database,
        run_id,
        protection_input,
        position,
        now=normalized_at,
        venue_update_id="protection-confirmed-1",
    )
    degraded_request, degraded = _record_protection(
        database,
        run_id,
        protection_input,
        position,
        now=normalized_at,
        venue_update_id="protection-degraded-1",
        protection_state=VenueProtectionState.DEGRADED,
    )
    unknown_request, unknown = _record_protection(
        database,
        run_id,
        protection_input,
        position,
        now=normalized_at,
        venue_update_id="protection-unknown-1",
        protection_state=VenueProtectionState.UNKNOWN,
    )
    _, excess = _record_protection(
        database,
        run_id,
        protection_input,
        position,
        now=normalized_at,
        venue_update_id="protection-excess-1",
    )
    assert confirmed.status is CommandStatus.COMPLETED
    assert degraded.status is CommandStatus.COMPLETED
    assert unknown.status is CommandStatus.COMPLETED
    assert excess.status is CommandStatus.REJECTED
    assert excess.error_code == "VENUE_FACT_INPUT_COUNT_EXCEEDED"

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
        confirmed_row = session.get(
            VenueProtectionSnapshot,
            confirmed_request.venue_protection_snapshot_id,
        )
        degraded_row = session.get(
            VenueProtectionSnapshot,
            degraded_request.venue_protection_snapshot_id,
        )
        unknown_row = session.get(
            VenueProtectionSnapshot,
            unknown_request.venue_protection_snapshot_id,
        )
        assert confirmed_row is not None
        assert confirmed_row.protection_state == "CONFIRMED"
        assert confirmed_row.covered_quantity == position.quantity
        assert confirmed_row.uncovered_quantity == 0
        assert (
            confirmed_row.worst_active_trigger_price == confirmed_request.worst_active_trigger_price
        )
        assert confirmed_row.normalized_payload["worst_active_trigger_price"] == str(
            confirmed_request.worst_active_trigger_price
        )
        assert confirmed_row.venue_native is True
        assert confirmed_row.reduce_only_confirmed is True
        assert degraded_row is not None
        assert degraded_row.protection_state == "DEGRADED"
        assert degraded_row.uncovered_quantity == Decimal("0.1")
        assert degraded_row.worst_active_trigger_price is None
        assert unknown_row is not None
        assert unknown_row.protection_state == "UNKNOWN"
        assert unknown_row.position_quantity is None
        assert unknown_row.covered_quantity is None
        assert unknown_row.worst_active_trigger_price is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(VenueFactInputLink)
                .where(VenueFactInputLink.source_type == "VENUE_PROTECTION")
            )
            == 3
        )
    with pytest.raises(DBAPIError, match="venue_protection_snapshots is immutable"):
        with database.session_factory.begin() as session:
            session.execute(
                update(VenueProtectionSnapshot)
                .where(
                    VenueProtectionSnapshot.venue_protection_snapshot_id
                    == confirmed_request.venue_protection_snapshot_id
                )
                .values(worst_active_trigger_price=Decimal("1"))
            )


@pytest.mark.parametrize(
    "direction",
    [VenuePositionDirection.LONG, VenuePositionDirection.SHORT],
)
def test_protection_trigger_must_remain_on_protective_side_of_mark(
    database: Database,
    direction: VenuePositionDirection,
) -> None:
    run_id, _, _, run_time, protection_input, position = _prepare_protection_run(
        database,
        protection_count=1,
        position_updates={"direction": direction},
    )
    assert position.mark_price is not None

    _, result = _record_protection(
        database,
        run_id,
        protection_input,
        position,
        now=run_time + timedelta(seconds=1),
        worst_active_trigger_price=position.mark_price,
    )

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "VENUE_PROTECTION_TRIGGER_PRICE_INVALID"
    with database.session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(VenueProtectionSnapshot)) == 0


def test_protection_snapshot_v2_rejects_legacy_command_and_schema(
    database: Database,
) -> None:
    run_id, _, _, run_time, protection_input, position = _prepare_protection_run(
        database,
        protection_count=1,
    )
    normalized_at = run_time + timedelta(seconds=1)
    request = protection_snapshot_request(
        protection_input,
        position,
        now=normalized_at,
    )
    current = venue_fact_envelope(
        run_id,
        VenueFactNormalizationService.protection_command_type,
        request.model_dump(mode="json"),
        now=normalized_at,
        idempotency_key="protection-current-wrong-schema",
        payload_schema_version=1,
    )
    service = VenueFactNormalizationService(clock=lambda: normalized_at)
    schema_result = IdempotentCommandExecutor(database.session_factory).execute(
        current,
        service.record_protection_snapshot,
    )
    legacy = current.model_copy(
        update={
            "command_id": uuid4(),
            "idempotency_key": "protection-legacy-v1-command",
            "command_type": "execution.venue-protection-snapshot.record.v1",
        }
    )
    command_result = IdempotentCommandExecutor(database.session_factory).execute(
        legacy,
        service.record_protection_snapshot,
    )

    assert schema_result.status is CommandStatus.REJECTED
    assert schema_result.error_code == "PAYLOAD_SCHEMA_VERSION_MISMATCH"
    assert command_result.status is CommandStatus.REJECTED
    assert command_result.error_code == "COMMAND_TYPE_MISMATCH"
    with database.session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(VenueProtectionSnapshot)) == 0


def test_venue_fact_migration_round_trip_and_newer_evidence_guard(
    database: Database,
) -> None:
    alembic_config = Config("alembic.ini")
    command.downgrade(alembic_config, "20260718_0025")
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "20260718_0025"
        )
    command.upgrade(alembic_config, "head")
    assert database.is_ready() == (True, None)

    run_id, _, _, run_time, protection_input, position = _prepare_protection_run(
        database,
        protection_count=1,
    )
    _, recorded = _record_protection(
        database,
        run_id,
        protection_input,
        position,
        now=run_time + timedelta(seconds=1),
    )
    assert recorded.status is CommandStatus.COMPLETED

    with pytest.raises(
        DBAPIError,
        match="cannot downgrade while canonical funding or v2 reconciliation evidence exists",
    ):
        command.downgrade(alembic_config, "20260718_0025")
    assert database.is_ready() == (True, None)


def test_protection_snapshot_is_global_relinkable_and_conflicts_on_changed_semantics(
    database: Database,
) -> None:
    run_id, lease_id, version, run_time, protection_input, position = _prepare_protection_run(
        database, protection_count=1
    )
    normalized_at = run_time + timedelta(seconds=1)
    first_request, first = _record_protection(
        database,
        run_id,
        protection_input,
        position,
        now=normalized_at,
        venue_update_id="protection-global-1",
    )
    assert first.status is CommandStatus.COMPLETED
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
        protection_count=1,
        now=normalized_at + timedelta(seconds=1),
        lease_id=lease_id,
        supersedes_run_id=run_id,
    )
    second_request, second = _record_protection(
        database,
        second_run_id,
        second_inputs[ReconciliationSourceType.VENUE_PROTECTION],
        position,
        now=second_run_time + timedelta(seconds=1),
        venue_update_id="protection-global-1",
        event_time=first_request.event_time,
    )
    assert second.status is CommandStatus.COMPLETED
    assert second.data["venue_fact_id"] == str(first_request.venue_protection_snapshot_id)
    assert second.data["venue_fact_id"] != str(second_request.venue_protection_snapshot_id)
    assert second.data["new_fact"] is False
    assert second.data["new_input_link"] is True

    _, conflict = _record_protection(
        database,
        second_run_id,
        second_inputs[ReconciliationSourceType.VENUE_PROTECTION],
        position,
        now=second_run_time + timedelta(seconds=1),
        venue_update_id="protection-global-1",
        event_time=first_request.event_time,
        order_set_hash=hash_json({"different": True}),
    )
    assert conflict.status is CommandStatus.REJECTED
    assert conflict.error_code == "VENUE_PROTECTION_SNAPSHOT_CONFLICT"
    with database.session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(VenueProtectionSnapshot)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(VenueFactInputLink)
                .where(VenueFactInputLink.source_type == "VENUE_PROTECTION")
            )
            == 2
        )


@pytest.mark.parametrize(
    ("updates", "expected_error"),
    [
        (
            {
                "position_quantity": Decimal("0.4"),
                "covered_quantity": Decimal("0.4"),
            },
            "VENUE_PROTECTION_COVERAGE_MISMATCH",
        ),
        (
            {"protected_direction": VenueProtectedDirection.SHORT},
            "VENUE_PROTECTION_COVERAGE_MISMATCH",
        ),
    ],
)
def test_protection_quantity_or_direction_must_match_bound_position(
    database: Database,
    updates: dict[str, object],
    expected_error: str,
) -> None:
    run_id, _, _, run_time, protection_input, position = _prepare_protection_run(
        database,
        protection_count=1,
    )
    _, result = _record_protection(
        database,
        run_id,
        protection_input,
        position,
        now=run_time + timedelta(seconds=1),
        **updates,
    )

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == expected_error
    with database.session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(VenueProtectionSnapshot)) == 0


def test_protection_snapshot_cannot_predate_bound_position(database: Database) -> None:
    run_id, _, _, run_time, protection_input, position = _prepare_protection_run(
        database,
        protection_count=1,
    )
    stale_at = position.event_time - timedelta(microseconds=1)
    _, result = _record_protection(
        database,
        run_id,
        protection_input,
        position,
        now=run_time + timedelta(seconds=1),
        event_time=stale_at,
        venue_observed_at=stale_at,
    )

    assert result.status is CommandStatus.REJECTED
    assert result.error_code == "VENUE_PROTECTION_POSITION_MISMATCH"


def test_direct_protection_snapshot_without_first_link_is_rolled_back(
    database: Database,
) -> None:
    run_id, _, _, run_time, protection_input, position = _prepare_protection_run(
        database,
        protection_count=1,
    )
    normalized_at = run_time + timedelta(seconds=1)
    request = protection_snapshot_request(
        protection_input,
        position,
        now=normalized_at,
    )
    direct_snapshot = VenueProtectionSnapshot(
        venue_protection_snapshot_id=request.venue_protection_snapshot_id,
        organization_id="org-1",
        first_seen_run_id=run_id,
        first_seen_input_id=protection_input.input_id,
        venue_position_snapshot_id=request.venue_position_snapshot_id,
        venue=request.venue,
        execution_domain=request.execution_domain,
        account_id=request.account_id,
        instrument_id=request.instrument_id,
        venue_update_id=request.venue_update_id,
        position_mode=request.position_mode.value,
        position_side=request.position_side.value,
        margin_mode=request.margin_mode,
        collateral_pool_id=request.collateral_pool_id,
        protection_state=request.protection_state.value,
        protected_direction=request.protected_direction.value,
        position_quantity=request.position_quantity,
        covered_quantity=request.covered_quantity,
        uncovered_quantity=request.uncovered_quantity,
        active_stop_order_count=request.active_stop_order_count,
        worst_active_trigger_price=request.worst_active_trigger_price,
        venue_native=request.venue_native,
        reduce_only_confirmed=request.reduce_only_confirmed,
        replacement_in_progress=request.replacement_in_progress,
        order_set_hash=request.order_set_hash,
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
    assert position.mark_price is not None
    direct_snapshot.worst_active_trigger_price = position.mark_price
    with pytest.raises(
        DBAPIError,
        match="canonical venue protection trigger price is invalid",
    ):
        with database.session_factory.begin() as session:
            session.add(direct_snapshot)
            session.flush()
    direct_snapshot.worst_active_trigger_price = request.worst_active_trigger_price
    with pytest.raises(
        DBAPIError, match="canonical venue fact requires its first immutable input link"
    ):
        with database.session_factory.begin() as session:
            session.add(direct_snapshot)
    with database.session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(VenueProtectionSnapshot)) == 0


def test_account_equity_confirmed_unknown_and_exact_manifest_are_canonical(
    database: Database,
) -> None:
    run_id, _, version, run_time, inputs = _prepare_collecting_run(database, balance_count=2)
    normalized_at = run_time + timedelta(seconds=1)
    balance_input = inputs[ReconciliationSourceType.VENUE_BALANCES]

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

    confirmed_request = account_equity_snapshot_request(
        balance_input,
        now=normalized_at,
        venue_update_id="account-equity-confirmed-1",
    )
    confirmed_envelope = venue_fact_envelope(
        run_id,
        VenueFactNormalizationService.account_equity_command_type,
        confirmed_request.model_dump(mode="json"),
        now=normalized_at,
        idempotency_key="account-equity-confirmed-idempotent",
    )
    first = execute_venue_fact(database, confirmed_envelope, now=normalized_at)
    replay = execute_venue_fact(database, confirmed_envelope, now=normalized_at)
    unknown_request, unknown = _record_account_equity(
        database,
        run_id,
        balance_input,
        now=normalized_at,
        venue_update_id="account-equity-unknown-1",
        equity_state=VenueAccountEquityState.UNKNOWN,
    )
    _, excess = _record_account_equity(
        database,
        run_id,
        balance_input,
        now=normalized_at,
        venue_update_id="account-equity-excess-1",
    )

    assert first.status is CommandStatus.COMPLETED
    assert replay.status is CommandStatus.ALREADY_PROCESSED
    assert replay.replayed is True
    assert replay.data["original_status"] == CommandStatus.COMPLETED.value
    assert replay.data["original_data"] == first.data
    assert unknown.status is CommandStatus.COMPLETED
    assert excess.status is CommandStatus.REJECTED
    assert excess.error_code == "VENUE_FACT_INPUT_COUNT_EXCEEDED"

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
        confirmed = session.get(
            VenueAccountEquitySnapshot,
            confirmed_request.venue_account_equity_snapshot_id,
        )
        unknown_row = session.get(
            VenueAccountEquitySnapshot,
            unknown_request.venue_account_equity_snapshot_id,
        )
        assert confirmed is not None
        assert confirmed.equity_state == "CONFIRMED"
        assert confirmed.wallet_balance == Decimal("10000")
        assert confirmed.exchange_margin_equity == Decimal("10500")
        assert confirmed.total_unrealized_pnl == Decimal("500")
        assert confirmed.includes_unrealized_pnl is True
        assert unknown_row is not None
        assert unknown_row.equity_state == "UNKNOWN"
        assert unknown_row.wallet_balance is None
        assert unknown_row.exchange_margin_equity is None
        assert unknown_row.total_unrealized_pnl is None
        assert unknown_row.includes_unrealized_pnl is False
        assert (
            session.scalar(
                select(func.count())
                .select_from(VenueFactInputLink)
                .where(VenueFactInputLink.source_type == "VENUE_BALANCES")
            )
            == 2
        )
    with pytest.raises(DBAPIError, match="venue_account_equity_snapshots is immutable"):
        with database.session_factory.begin() as session:
            session.execute(
                update(VenueAccountEquitySnapshot)
                .where(
                    VenueAccountEquitySnapshot.venue_account_equity_snapshot_id
                    == confirmed_request.venue_account_equity_snapshot_id
                )
                .values(exchange_margin_equity=Decimal("99999"))
            )


def test_account_equity_is_global_relinkable_and_conflicts_on_changed_semantics(
    database: Database,
) -> None:
    run_id, lease_id, version, run_time, inputs = _prepare_collecting_run(database, balance_count=1)
    normalized_at = run_time + timedelta(seconds=1)
    first_request, first = _record_account_equity(
        database,
        run_id,
        inputs[ReconciliationSourceType.VENUE_BALANCES],
        now=normalized_at,
        venue_update_id="account-equity-global-1",
    )
    assert first.status is CommandStatus.COMPLETED
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
        balance_count=1,
        now=normalized_at + timedelta(seconds=1),
        lease_id=lease_id,
        supersedes_run_id=run_id,
    )
    second_request, second = _record_account_equity(
        database,
        second_run_id,
        second_inputs[ReconciliationSourceType.VENUE_BALANCES],
        now=second_run_time + timedelta(seconds=1),
        venue_update_id="account-equity-global-1",
        event_time=first_request.event_time,
    )
    assert second.status is CommandStatus.COMPLETED
    assert second.data["venue_fact_id"] == str(first_request.venue_account_equity_snapshot_id)
    assert second.data["venue_fact_id"] != str(second_request.venue_account_equity_snapshot_id)
    assert second.data["new_fact"] is False
    assert second.data["new_input_link"] is True

    _, conflict = _record_account_equity(
        database,
        second_run_id,
        second_inputs[ReconciliationSourceType.VENUE_BALANCES],
        now=second_run_time + timedelta(seconds=1),
        venue_update_id="account-equity-global-1",
        event_time=first_request.event_time,
        wallet_balance=Decimal("10001"),
    )
    assert conflict.status is CommandStatus.REJECTED
    assert conflict.error_code == "VENUE_ACCOUNT_EQUITY_SNAPSHOT_CONFLICT"
    with database.session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(VenueAccountEquitySnapshot)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(VenueFactInputLink)
                .where(VenueFactInputLink.source_type == "VENUE_BALANCES")
            )
            == 2
        )


def test_account_equity_scope_and_unknown_zero_fail_closed(database: Database) -> None:
    run_id, _, _, run_time, inputs = _prepare_collecting_run(database, balance_count=1)
    normalized_at = run_time + timedelta(seconds=1)
    balance_input = inputs[ReconciliationSourceType.VENUE_BALANCES]
    _, wrong_scope = _record_account_equity(
        database,
        run_id,
        balance_input,
        now=normalized_at,
        margin_mode="CROSS",
    )
    assert wrong_scope.status is CommandStatus.REJECTED
    assert wrong_scope.error_code == "VENUE_ACCOUNT_EQUITY_SCOPE_MISMATCH"

    unknown_request = account_equity_snapshot_request(
        balance_input,
        now=normalized_at,
        equity_state=VenueAccountEquityState.UNKNOWN,
    )
    payload = unknown_request.model_dump(mode="json")
    payload["wallet_balance"] = "0"
    payload["evidence_hash"] = hash_json(
        {key: value for key, value in payload.items() if key != "evidence_hash"}
    )
    unknown_as_zero = execute_venue_fact(
        database,
        venue_fact_envelope(
            run_id,
            VenueFactNormalizationService.account_equity_command_type,
            payload,
            now=normalized_at,
        ),
        now=normalized_at,
    )
    assert unknown_as_zero.status is CommandStatus.REJECTED
    assert unknown_as_zero.error_code == "VENUE_ACCOUNT_EQUITY_SNAPSHOT_INVALID"
    with database.session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(VenueAccountEquitySnapshot)) == 0


def test_direct_account_equity_scope_or_missing_first_link_is_rolled_back(
    database: Database,
) -> None:
    run_id, _, _, run_time, inputs = _prepare_collecting_run(database, balance_count=1)
    normalized_at = run_time + timedelta(seconds=1)
    balance_input = inputs[ReconciliationSourceType.VENUE_BALANCES]
    request = account_equity_snapshot_request(balance_input, now=normalized_at)

    def direct_snapshot(*, margin_mode: str) -> VenueAccountEquitySnapshot:
        return VenueAccountEquitySnapshot(
            venue_account_equity_snapshot_id=request.venue_account_equity_snapshot_id,
            organization_id="org-1",
            first_seen_run_id=run_id,
            first_seen_input_id=balance_input.input_id,
            venue=request.venue,
            execution_domain=request.execution_domain,
            account_id=request.account_id,
            venue_update_id=request.venue_update_id,
            margin_mode=margin_mode,
            collateral_pool_id=request.collateral_pool_id,
            settlement_currency=request.settlement_currency,
            equity_state=request.equity_state.value,
            wallet_balance=request.wallet_balance,
            exchange_margin_equity=request.exchange_margin_equity,
            available_margin=request.available_margin,
            total_unrealized_pnl=request.total_unrealized_pnl,
            total_initial_margin=request.total_initial_margin,
            total_maintenance_margin=request.total_maintenance_margin,
            total_liability=request.total_liability,
            unsettled_fee=request.unsettled_fee,
            unsettled_funding=request.unsettled_funding,
            includes_unrealized_pnl=request.includes_unrealized_pnl,
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

    with pytest.raises(DBAPIError, match="canonical venue account equity scope changed"):
        with database.session_factory.begin() as session:
            session.add(direct_snapshot(margin_mode="CROSS"))
    with pytest.raises(
        DBAPIError, match="canonical venue fact requires its first immutable input link"
    ):
        with database.session_factory.begin() as session:
            session.add(direct_snapshot(margin_mode="ISOLATED"))
    with database.session_factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(VenueAccountEquitySnapshot)) == 0
