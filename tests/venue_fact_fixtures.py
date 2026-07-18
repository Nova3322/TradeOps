from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from trading_control_plane.command_executor import IdempotentCommandExecutor
from trading_control_plane.commands import CommandChannel, CommandEnvelope, CommandResult, hash_json
from trading_control_plane.database import Database
from trading_control_plane.reconciliation_models import ExecutionReconciliationInput
from trading_control_plane.venue_fact_models import VenuePositionSnapshot
from trading_control_plane.venue_facts import (
    VENUE_FACT_SERVICE_PRINCIPAL,
    FeeEffect,
    LiquidityRole,
    RecordVenueAccountEquitySnapshotRequest,
    RecordVenueFillRequest,
    RecordVenueOrderObservationRequest,
    RecordVenuePositionSnapshotRequest,
    RecordVenueProtectionSnapshotRequest,
    VenueAccountEquityState,
    VenueFactNormalizationService,
    VenueOrderStatus,
    VenuePositionDirection,
    VenuePositionMode,
    VenuePositionSide,
    VenuePositionState,
    VenueProtectedDirection,
    VenueProtectionState,
    VenueSide,
    _account_equity_snapshot_contract,
    _fill_contract,
    _order_observation_contract,
    _position_snapshot_contract,
    _protection_snapshot_contract,
)


def venue_fact_envelope(
    run_id: UUID,
    command_type: str,
    payload: dict[str, object],
    *,
    now: datetime,
    idempotency_key: str | None = None,
    payload_schema_version: int | None = None,
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
        payload_schema_version=(
            payload_schema_version
            if payload_schema_version is not None
            else (2 if command_type == VenueFactNormalizationService.protection_command_type else 1)
        ),
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


def protection_snapshot_request(
    reconciliation_input: ExecutionReconciliationInput,
    position_snapshot: VenuePositionSnapshot,
    *,
    now: datetime,
    venue_protection_snapshot_id: UUID | None = None,
    venue_fact_input_link_id: UUID | None = None,
    venue_update_id: str = "protection-update-1",
    protection_state: VenueProtectionState = VenueProtectionState.CONFIRMED,
    protected_direction: VenueProtectedDirection | None = None,
    position_quantity: Decimal | None = None,
    covered_quantity: Decimal | None = None,
    uncovered_quantity: Decimal | None = None,
    active_stop_order_count: int | None = None,
    worst_active_trigger_price: Decimal | None = None,
    venue_native: bool | None = None,
    reduce_only_confirmed: bool | None = None,
    replacement_in_progress: bool | None = None,
    order_set_hash: str | None = None,
    event_time: datetime | None = None,
    venue_observed_at: datetime | None = None,
    received_at: datetime | None = None,
) -> RecordVenueProtectionSnapshotRequest:
    observed_event = event_time or now - timedelta(seconds=2)
    effective_trigger_price: Decimal | None
    if protection_state is VenueProtectionState.CONFIRMED:
        effective_direction = protected_direction or VenueProtectedDirection(
            position_snapshot.direction
        )
        effective_position_quantity = (
            position_quantity if position_quantity is not None else position_snapshot.quantity
        )
        effective_covered = (
            covered_quantity if covered_quantity is not None else effective_position_quantity
        )
        effective_uncovered = uncovered_quantity if uncovered_quantity is not None else Decimal("0")
        effective_count = active_stop_order_count if active_stop_order_count is not None else 1
        assert position_snapshot.mark_price is not None
        effective_trigger_price = (
            worst_active_trigger_price
            if worst_active_trigger_price is not None
            else (
                position_snapshot.mark_price - Decimal("100")
                if effective_direction is VenueProtectedDirection.LONG
                else position_snapshot.mark_price + Decimal("100")
            )
        )
        effective_native = venue_native if venue_native is not None else True
        effective_reduce_only = reduce_only_confirmed if reduce_only_confirmed is not None else True
        effective_replacement = (
            replacement_in_progress if replacement_in_progress is not None else False
        )
    elif protection_state is VenueProtectionState.DEGRADED:
        effective_direction = protected_direction or VenueProtectedDirection(
            position_snapshot.direction
        )
        effective_position_quantity = (
            position_quantity if position_quantity is not None else position_snapshot.quantity
        )
        assert effective_position_quantity is not None
        effective_uncovered = (
            uncovered_quantity if uncovered_quantity is not None else Decimal("0.1")
        )
        effective_covered = (
            covered_quantity
            if covered_quantity is not None
            else effective_position_quantity - effective_uncovered
        )
        effective_count = active_stop_order_count if active_stop_order_count is not None else 1
        effective_trigger_price = worst_active_trigger_price
        effective_native = venue_native if venue_native is not None else True
        effective_reduce_only = reduce_only_confirmed if reduce_only_confirmed is not None else True
        effective_replacement = (
            replacement_in_progress if replacement_in_progress is not None else False
        )
    else:
        effective_direction = VenueProtectedDirection.UNKNOWN
        effective_position_quantity = None
        effective_covered = None
        effective_uncovered = None
        effective_count = None
        effective_trigger_price = None
        effective_native = False
        effective_reduce_only = False
        effective_replacement = False
    effective_order_set_hash = order_set_hash or hash_json(
        {
            "venue_update_id": venue_update_id,
            "active_stop_order_count": effective_count,
            "protection_state": protection_state.value,
        }
    )
    values: dict[str, object] = {
        "venue_fact_input_link_id": venue_fact_input_link_id or uuid4(),
        "reconciliation_input_id": reconciliation_input.input_id,
        "reconciliation_input_hash": reconciliation_input.input_hash,
        "venue": position_snapshot.venue,
        "execution_domain": position_snapshot.execution_domain,
        "account_id": position_snapshot.account_id,
        "instrument_id": position_snapshot.instrument_id,
        "source_version": reconciliation_input.source_version,
        "normalization_version": "venue-protection-normalizer-v1",
        "normalized_payload": {
            "venue_update_id": venue_update_id,
            "position_snapshot_id": str(position_snapshot.venue_position_snapshot_id),
            "protection_state": protection_state.value,
            "worst_active_trigger_price": (
                str(effective_trigger_price) if effective_trigger_price is not None else None
            ),
            "order_set_hash": effective_order_set_hash,
        },
        "raw_payload_ref": f"test-only:raw-protection:{venue_update_id}",
        "raw_payload_hash": hash_json({"raw_protection_update_id": venue_update_id}),
        "evidence_ref": f"test-only:protection-evidence:{venue_update_id}",
        "event_time": observed_event,
        "venue_observed_at": venue_observed_at or now - timedelta(seconds=1),
        "received_at": received_at or now,
        "venue_protection_snapshot_id": venue_protection_snapshot_id or uuid4(),
        "venue_position_snapshot_id": position_snapshot.venue_position_snapshot_id,
        "venue_update_id": venue_update_id,
        "position_mode": VenuePositionMode(position_snapshot.position_mode),
        "position_side": VenuePositionSide(position_snapshot.position_side),
        "margin_mode": position_snapshot.margin_mode,
        "collateral_pool_id": position_snapshot.collateral_pool_id,
        "protection_state": protection_state,
        "protected_direction": effective_direction,
        "position_quantity": effective_position_quantity,
        "covered_quantity": effective_covered,
        "uncovered_quantity": effective_uncovered,
        "active_stop_order_count": effective_count,
        "worst_active_trigger_price": effective_trigger_price,
        "venue_native": effective_native,
        "reduce_only_confirmed": effective_reduce_only,
        "replacement_in_progress": effective_replacement,
        "order_set_hash": effective_order_set_hash,
    }
    draft = RecordVenueProtectionSnapshotRequest.model_construct(
        **values, snapshot_hash="0" * 64, evidence_hash="0" * 64
    )
    snapshot_hash = hash_json(_protection_snapshot_contract(draft))
    evidence_draft = RecordVenueProtectionSnapshotRequest.model_construct(
        **values, snapshot_hash=snapshot_hash, evidence_hash="0" * 64
    )
    evidence_hash = hash_json(evidence_draft.model_dump(mode="json", exclude={"evidence_hash"}))
    return RecordVenueProtectionSnapshotRequest.model_validate(
        {**values, "snapshot_hash": snapshot_hash, "evidence_hash": evidence_hash}
    )


def account_equity_snapshot_request(
    reconciliation_input: ExecutionReconciliationInput,
    *,
    now: datetime,
    venue_account_equity_snapshot_id: UUID | None = None,
    venue_fact_input_link_id: UUID | None = None,
    venue_update_id: str = "account-equity-update-1",
    margin_mode: str = "ISOLATED",
    collateral_pool_id: str = "pool-usdt-1",
    settlement_currency: str = "USDT",
    equity_state: VenueAccountEquityState = VenueAccountEquityState.CONFIRMED,
    wallet_balance: Decimal | None = None,
    exchange_margin_equity: Decimal | None = None,
    available_margin: Decimal | None = None,
    total_unrealized_pnl: Decimal | None = None,
    total_initial_margin: Decimal | None = None,
    total_maintenance_margin: Decimal | None = None,
    total_liability: Decimal | None = None,
    unsettled_fee: Decimal | None = None,
    unsettled_funding: Decimal | None = None,
    includes_unrealized_pnl: bool | None = None,
    event_time: datetime | None = None,
    venue_observed_at: datetime | None = None,
    received_at: datetime | None = None,
) -> RecordVenueAccountEquitySnapshotRequest:
    observed_event = event_time or now - timedelta(seconds=2)
    if equity_state is VenueAccountEquityState.CONFIRMED:
        effective_wallet = wallet_balance if wallet_balance is not None else Decimal("10000")
        effective_equity = (
            exchange_margin_equity if exchange_margin_equity is not None else Decimal("10500")
        )
        effective_available = available_margin if available_margin is not None else Decimal("9000")
        effective_upnl = (
            total_unrealized_pnl if total_unrealized_pnl is not None else Decimal("500")
        )
        effective_initial = (
            total_initial_margin if total_initial_margin is not None else Decimal("1000")
        )
        effective_maintenance = (
            total_maintenance_margin if total_maintenance_margin is not None else Decimal("250")
        )
        effective_liability = total_liability if total_liability is not None else Decimal("0")
        effective_fee = unsettled_fee if unsettled_fee is not None else Decimal("0")
        effective_funding = unsettled_funding if unsettled_funding is not None else Decimal("0")
        effective_includes_upnl = (
            includes_unrealized_pnl if includes_unrealized_pnl is not None else True
        )
    else:
        effective_wallet = None
        effective_equity = None
        effective_available = None
        effective_upnl = None
        effective_initial = None
        effective_maintenance = None
        effective_liability = None
        effective_fee = None
        effective_funding = None
        effective_includes_upnl = False
    values: dict[str, object] = {
        "venue_fact_input_link_id": venue_fact_input_link_id or uuid4(),
        "reconciliation_input_id": reconciliation_input.input_id,
        "reconciliation_input_hash": reconciliation_input.input_hash,
        "venue": "BINANCE",
        "execution_domain": "BINANCE_USDM",
        "account_id": "account-1",
        "instrument_id": None,
        "source_version": reconciliation_input.source_version,
        "normalization_version": "venue-account-equity-normalizer-v1",
        "normalized_payload": {
            "venue_update_id": venue_update_id,
            "collateral_pool_id": collateral_pool_id,
            "settlement_currency": settlement_currency,
            "equity_state": equity_state.value,
        },
        "raw_payload_ref": f"test-only:raw-account-equity:{venue_update_id}",
        "raw_payload_hash": hash_json({"raw_account_equity_update_id": venue_update_id}),
        "evidence_ref": f"test-only:account-equity-evidence:{venue_update_id}",
        "event_time": observed_event,
        "venue_observed_at": venue_observed_at or now - timedelta(seconds=1),
        "received_at": received_at or now,
        "venue_account_equity_snapshot_id": venue_account_equity_snapshot_id or uuid4(),
        "venue_update_id": venue_update_id,
        "margin_mode": margin_mode,
        "collateral_pool_id": collateral_pool_id,
        "settlement_currency": settlement_currency,
        "equity_state": equity_state,
        "wallet_balance": effective_wallet,
        "exchange_margin_equity": effective_equity,
        "available_margin": effective_available,
        "total_unrealized_pnl": effective_upnl,
        "total_initial_margin": effective_initial,
        "total_maintenance_margin": effective_maintenance,
        "total_liability": effective_liability,
        "unsettled_fee": effective_fee,
        "unsettled_funding": effective_funding,
        "includes_unrealized_pnl": effective_includes_upnl,
    }
    draft = RecordVenueAccountEquitySnapshotRequest.model_construct(
        **values, snapshot_hash="0" * 64, evidence_hash="0" * 64
    )
    snapshot_hash = hash_json(_account_equity_snapshot_contract(draft))
    evidence_draft = RecordVenueAccountEquitySnapshotRequest.model_construct(
        **values, snapshot_hash=snapshot_hash, evidence_hash="0" * 64
    )
    evidence_hash = hash_json(evidence_draft.model_dump(mode="json", exclude={"evidence_hash"}))
    return RecordVenueAccountEquitySnapshotRequest.model_validate(
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
        service.protection_command_type: service.record_protection_snapshot,
        service.account_equity_command_type: service.record_account_equity_snapshot,
    }
    return IdempotentCommandExecutor(database.session_factory).execute(
        envelope, handlers[envelope.command_type]
    )
