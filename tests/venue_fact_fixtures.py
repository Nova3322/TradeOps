from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import CommandChannel, CommandEnvelope, CommandResult, hash_json
from trading_control_plane.database import Database
from trading_control_plane.reconciliation_models import ExecutionReconciliationInput
from trading_control_plane.venue_facts import (
    VENUE_FACT_SERVICE_PRINCIPAL,
    FeeEffect,
    LiquidityRole,
    RecordVenueFillRequest,
    RecordVenueOrderObservationRequest,
    RecordVenuePositionSnapshotRequest,
    VenueFactNormalizationService,
    VenueOrderStatus,
    VenuePositionDirection,
    VenuePositionMode,
    VenuePositionSide,
    VenuePositionState,
    VenueSide,
    _fill_contract,
    _order_observation_contract,
    _position_snapshot_contract,
)


def venue_fact_envelope(
    run_id: UUID,
    command_type: str,
    payload: dict[str, object],
    *,
    now: datetime,
    idempotency_key: str | None = None,
) -> CommandEnvelope:
    return CommandEnvelope(
        idempotency_key=idempotency_key or f"venue-fact-{uuid4()}",
        command_type=command_type,
        object_type="ExecutionReconciliationRun",
        object_id=str(run_id),
        expected_version=None,
        service_principal=VENUE_FACT_SERVICE_PRINCIPAL,
        channel=CommandChannel.INTERNAL,
        scope={"organization_id": "org-1"},
        correlation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        auth_context_ref="test-only:venue-fact-normalizer",
        payload_schema_version=1,
        reason="normalize immutable private venue fact",
        payload=payload,
    )


def fill_request(
    reconciliation_input: ExecutionReconciliationInput,
    *,
    now: datetime,
    venue_fill_id: UUID | None = None,
    venue_fact_input_link_id: UUID | None = None,
    venue_trade_id: str = "trade-1",
    venue_order_id: str = "order-1",
    observed_client_order_id: str | None = "client-order-1",
    instrument_id: str = "BTCUSDT-PERP",
    side: VenueSide = VenueSide.BUY,
    position_side: VenuePositionSide = VenuePositionSide.BOTH,
    reduce_only: bool = False,
    quantity: Decimal = Decimal("0.2"),
    price: Decimal = Decimal("50000"),
    fee_amount: Decimal = Decimal("1.5"),
    fee_effect: FeeEffect = FeeEffect.CHARGE,
    event_time: datetime | None = None,
    venue_observed_at: datetime | None = None,
    received_at: datetime | None = None,
) -> RecordVenueFillRequest:
    observed_event = event_time or now - timedelta(seconds=2)
    values: dict[str, object] = {
        "venue_fact_input_link_id": venue_fact_input_link_id or uuid4(),
        "reconciliation_input_id": reconciliation_input.input_id,
        "reconciliation_input_hash": reconciliation_input.input_hash,
        "venue": "BINANCE",
        "execution_domain": "BINANCE_USDM",
        "account_id": "account-1",
        "instrument_id": instrument_id,
        "observed_client_order_id": observed_client_order_id,
        "venue_order_id": venue_order_id,
        "source_version": reconciliation_input.source_version,
        "normalization_version": "venue-normalizer-v1",
        "normalized_payload": {
            "trade_id": venue_trade_id,
            "quantity": str(quantity),
            "price": str(price),
        },
        "raw_payload_ref": f"test-only:raw-fill:{venue_trade_id}",
        "raw_payload_hash": hash_json({"raw_trade_id": venue_trade_id}),
        "evidence_ref": f"test-only:fill-evidence:{venue_trade_id}",
        "event_time": observed_event,
        "venue_observed_at": venue_observed_at or now - timedelta(seconds=1),
        "received_at": received_at or now,
        "venue_fill_id": venue_fill_id or uuid4(),
        "venue_trade_id": venue_trade_id,
        "side": side,
        "position_side": position_side,
        "reduce_only": reduce_only,
        "quantity": quantity,
        "price": price,
        "contract_multiplier": Decimal("1"),
        "notional": quantity * price,
        "liquidity_role": LiquidityRole.TAKER,
        "fee_amount": fee_amount,
        "fee_currency": "USDT",
        "fee_effect": fee_effect,
        "realized_pnl": None,
        "settlement_currency": "USDT",
    }
    draft = RecordVenueFillRequest.model_construct(
        **values, fill_hash="0" * 64, evidence_hash="0" * 64
    )
    fill_hash = hash_json(_fill_contract(draft))
    evidence_draft = RecordVenueFillRequest.model_construct(
        **values, fill_hash=fill_hash, evidence_hash="0" * 64
    )
    evidence_hash = hash_json(evidence_draft.model_dump(mode="json", exclude={"evidence_hash"}))
    return RecordVenueFillRequest.model_validate(
        {**values, "fill_hash": fill_hash, "evidence_hash": evidence_hash}
    )


def order_observation_request(
    reconciliation_input: ExecutionReconciliationInput,
    *,
    now: datetime,
    venue_order_observation_id: UUID | None = None,
    venue_fact_input_link_id: UUID | None = None,
    venue_order_id: str = "order-1",
    venue_update_id: str = "order-1-update-1",
    observed_client_order_id: str | None = "client-order-1",
    instrument_id: str = "BTCUSDT-PERP",
    side: VenueSide = VenueSide.BUY,
    position_side: VenuePositionSide = VenuePositionSide.BOTH,
    reduce_only: bool = False,
    order_type: str = "LIMIT",
    time_in_force: str = "GTC",
    status: VenueOrderStatus = VenueOrderStatus.OPEN,
    original_quantity: Decimal = Decimal("0.5"),
    cumulative_filled_quantity: Decimal = Decimal("0"),
    known_remaining_quantity: Decimal = Decimal("0.5"),
    zero_fill_confirmed: bool = False,
    terminal: bool = False,
    event_time: datetime | None = None,
    venue_observed_at: datetime | None = None,
    received_at: datetime | None = None,
) -> RecordVenueOrderObservationRequest:
    observed_event = event_time or now - timedelta(seconds=2)
    values: dict[str, object] = {
        "venue_fact_input_link_id": venue_fact_input_link_id or uuid4(),
        "reconciliation_input_id": reconciliation_input.input_id,
        "reconciliation_input_hash": reconciliation_input.input_hash,
        "venue": "BINANCE",
        "execution_domain": "BINANCE_USDM",
        "account_id": "account-1",
        "instrument_id": instrument_id,
        "observed_client_order_id": observed_client_order_id,
        "venue_order_id": venue_order_id,
        "source_version": reconciliation_input.source_version,
        "normalization_version": "venue-normalizer-v1",
        "normalized_payload": {"order_id": venue_order_id, "status": status.value},
        "raw_payload_ref": f"test-only:raw-order:{venue_update_id}",
        "raw_payload_hash": hash_json({"raw_update_id": venue_update_id}),
        "evidence_ref": f"test-only:order-evidence:{venue_update_id}",
        "event_time": observed_event,
        "venue_observed_at": venue_observed_at or now - timedelta(seconds=1),
        "received_at": received_at or now,
        "venue_order_observation_id": venue_order_observation_id or uuid4(),
        "venue_update_id": venue_update_id,
        "status": status,
        "side": side,
        "position_side": position_side,
        "reduce_only": reduce_only,
        "order_type": order_type,
        "time_in_force": time_in_force,
        "original_quantity": original_quantity,
        "cumulative_filled_quantity": cumulative_filled_quantity,
        "known_remaining_quantity": known_remaining_quantity,
        "zero_fill_confirmed": zero_fill_confirmed,
        "terminal": terminal,
    }
    draft = RecordVenueOrderObservationRequest.model_construct(
        **values, observation_hash="0" * 64, evidence_hash="0" * 64
    )
    observation_hash = hash_json(_order_observation_contract(draft))
    evidence_draft = RecordVenueOrderObservationRequest.model_construct(
        **values, observation_hash=observation_hash, evidence_hash="0" * 64
    )
    evidence_hash = hash_json(evidence_draft.model_dump(mode="json", exclude={"evidence_hash"}))
    return RecordVenueOrderObservationRequest.model_validate(
        {
            **values,
            "observation_hash": observation_hash,
            "evidence_hash": evidence_hash,
        }
    )


def position_snapshot_request(
    reconciliation_input: ExecutionReconciliationInput,
    *,
    now: datetime,
    venue_position_snapshot_id: UUID | None = None,
    venue_fact_input_link_id: UUID | None = None,
    venue_update_id: str = "position-update-1",
    instrument_id: str = "BTCUSDT-PERP",
    position_mode: VenuePositionMode = VenuePositionMode.ONE_WAY,
    position_side: VenuePositionSide = VenuePositionSide.BOTH,
    margin_mode: str = "ISOLATED",
    collateral_pool_id: str = "pool-usdt-1",
    position_state: VenuePositionState = VenuePositionState.OPEN,
    direction: VenuePositionDirection = VenuePositionDirection.LONG,
    quantity: Decimal | None = None,
    entry_price: Decimal | None = None,
    mark_price: Decimal | None = None,
    contract_multiplier: Decimal = Decimal("1"),
    notional: Decimal | None = None,
    unrealized_pnl: Decimal | None = None,
    liquidation_price: Decimal | None = None,
    leverage: Decimal | None = None,
    initial_margin: Decimal | None = None,
    maintenance_margin: Decimal | None = None,
    event_time: datetime | None = None,
    venue_observed_at: datetime | None = None,
    received_at: datetime | None = None,
) -> RecordVenuePositionSnapshotRequest:
    observed_event = event_time or now - timedelta(seconds=2)
    if position_state is VenuePositionState.OPEN:
        effective_quantity = quantity if quantity is not None else Decimal("0.5")
        effective_entry_price = entry_price if entry_price is not None else Decimal("49000")
        effective_mark_price = mark_price if mark_price is not None else Decimal("50000")
        effective_notional = (
            notional
            if notional is not None
            else effective_quantity * effective_mark_price * contract_multiplier
        )
        effective_unrealized_pnl = unrealized_pnl if unrealized_pnl is not None else Decimal("500")
        effective_liquidation_price = (
            liquidation_price if liquidation_price is not None else Decimal("40000")
        )
        effective_leverage = leverage if leverage is not None else Decimal("3")
        effective_initial_margin = (
            initial_margin if initial_margin is not None else Decimal("8333.333333333333333333")
        )
        effective_maintenance_margin = (
            maintenance_margin if maintenance_margin is not None else Decimal("125")
        )
    elif position_state is VenuePositionState.FLAT:
        effective_quantity = Decimal("0")
        effective_entry_price = None
        effective_mark_price = mark_price
        effective_notional = Decimal("0")
        effective_unrealized_pnl = Decimal("0")
        effective_liquidation_price = None
        effective_leverage = None
        effective_initial_margin = Decimal("0")
        effective_maintenance_margin = Decimal("0")
    else:
        effective_quantity = None
        effective_entry_price = None
        effective_mark_price = None
        effective_notional = None
        effective_unrealized_pnl = None
        effective_liquidation_price = None
        effective_leverage = None
        effective_initial_margin = None
        effective_maintenance_margin = None
    values: dict[str, object] = {
        "venue_fact_input_link_id": venue_fact_input_link_id or uuid4(),
        "reconciliation_input_id": reconciliation_input.input_id,
        "reconciliation_input_hash": reconciliation_input.input_hash,
        "venue": "BINANCE",
        "execution_domain": "BINANCE_USDM",
        "account_id": "account-1",
        "instrument_id": instrument_id,
        "source_version": reconciliation_input.source_version,
        "normalization_version": "venue-position-normalizer-v1",
        "normalized_payload": {
            "venue_update_id": venue_update_id,
            "instrument_id": instrument_id,
            "position_state": position_state.value,
        },
        "raw_payload_ref": f"test-only:raw-position:{venue_update_id}",
        "raw_payload_hash": hash_json({"raw_position_update_id": venue_update_id}),
        "evidence_ref": f"test-only:position-evidence:{venue_update_id}",
        "event_time": observed_event,
        "venue_observed_at": venue_observed_at or now - timedelta(seconds=1),
        "received_at": received_at or now,
        "venue_position_snapshot_id": venue_position_snapshot_id or uuid4(),
        "venue_update_id": venue_update_id,
        "position_mode": position_mode,
        "position_side": position_side,
        "margin_mode": margin_mode,
        "collateral_pool_id": collateral_pool_id,
        "position_state": position_state,
        "direction": direction,
        "quantity": effective_quantity,
        "entry_price": effective_entry_price,
        "mark_price": effective_mark_price,
        "contract_multiplier": contract_multiplier,
        "notional": effective_notional,
        "unrealized_pnl": effective_unrealized_pnl,
        "liquidation_price": effective_liquidation_price,
        "leverage": effective_leverage,
        "initial_margin": effective_initial_margin,
        "maintenance_margin": effective_maintenance_margin,
        "settlement_currency": "USDT",
    }
    draft = RecordVenuePositionSnapshotRequest.model_construct(
        **values, snapshot_hash="0" * 64, evidence_hash="0" * 64
    )
    snapshot_hash = hash_json(_position_snapshot_contract(draft))
    evidence_draft = RecordVenuePositionSnapshotRequest.model_construct(
        **values, snapshot_hash=snapshot_hash, evidence_hash="0" * 64
    )
    evidence_hash = hash_json(evidence_draft.model_dump(mode="json", exclude={"evidence_hash"}))
    return RecordVenuePositionSnapshotRequest.model_validate(
        {**values, "snapshot_hash": snapshot_hash, "evidence_hash": evidence_hash}
    )


def execute_venue_fact(
    database: Database,
    envelope: CommandEnvelope,
    *,
    now: datetime,
) -> CommandResult:
    service = VenueFactNormalizationService(clock=lambda: now)
    handlers = {
        service.order_command_type: service.record_order_observation,
        service.fill_command_type: service.record_fill,
        service.position_command_type: service.record_position_snapshot,
    }
    return IdempotentCommandExecutor(database.session_factory).execute(
        envelope, handlers[envelope.command_type]
    )
