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
    VenueFactNormalizationService,
    VenueOrderStatus,
    VenuePositionSide,
    VenueSide,
    _fill_contract,
    _order_observation_contract,
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
    quantity: Decimal = Decimal("0.2"),
    price: Decimal = Decimal("50000"),
    fee_amount: Decimal = Decimal("1.5"),
    fee_effect: FeeEffect = FeeEffect.CHARGE,
    event_time: datetime | None = None,
) -> RecordVenueFillRequest:
    observed_event = event_time or now - timedelta(seconds=2)
    values: dict[str, object] = {
        "venue_fact_input_link_id": venue_fact_input_link_id or uuid4(),
        "reconciliation_input_id": reconciliation_input.input_id,
        "reconciliation_input_hash": reconciliation_input.input_hash,
        "venue": "BINANCE",
        "execution_domain": "BINANCE_USDM",
        "account_id": "account-1",
        "instrument_id": "BTCUSDT-PERP",
        "observed_client_order_id": "client-order-1",
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
        "venue_observed_at": now - timedelta(seconds=1),
        "received_at": now,
        "venue_fill_id": venue_fill_id or uuid4(),
        "venue_trade_id": venue_trade_id,
        "side": VenueSide.BUY,
        "position_side": VenuePositionSide.BOTH,
        "reduce_only": False,
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
    status: VenueOrderStatus = VenueOrderStatus.OPEN,
    original_quantity: Decimal = Decimal("0.5"),
    cumulative_filled_quantity: Decimal = Decimal("0"),
    known_remaining_quantity: Decimal = Decimal("0.5"),
    zero_fill_confirmed: bool = False,
    terminal: bool = False,
    event_time: datetime | None = None,
) -> RecordVenueOrderObservationRequest:
    observed_event = event_time or now - timedelta(seconds=2)
    values: dict[str, object] = {
        "venue_fact_input_link_id": venue_fact_input_link_id or uuid4(),
        "reconciliation_input_id": reconciliation_input.input_id,
        "reconciliation_input_hash": reconciliation_input.input_hash,
        "venue": "BINANCE",
        "execution_domain": "BINANCE_USDM",
        "account_id": "account-1",
        "instrument_id": "BTCUSDT-PERP",
        "observed_client_order_id": "client-order-1",
        "venue_order_id": venue_order_id,
        "source_version": reconciliation_input.source_version,
        "normalization_version": "venue-normalizer-v1",
        "normalized_payload": {"order_id": venue_order_id, "status": status.value},
        "raw_payload_ref": f"test-only:raw-order:{venue_update_id}",
        "raw_payload_hash": hash_json({"raw_update_id": venue_update_id}),
        "evidence_ref": f"test-only:order-evidence:{venue_update_id}",
        "event_time": observed_event,
        "venue_observed_at": now - timedelta(seconds=1),
        "received_at": now,
        "venue_order_observation_id": venue_order_observation_id or uuid4(),
        "venue_update_id": venue_update_id,
        "status": status,
        "side": VenueSide.BUY,
        "position_side": VenuePositionSide.BOTH,
        "reduce_only": False,
        "order_type": "LIMIT",
        "time_in_force": "GTC",
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
    }
    return IdempotentCommandExecutor(database.session_factory).execute(
        envelope, handlers[envelope.command_type]
    )
