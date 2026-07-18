from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from trading_control_plane.commands import (
    CommandChannel,
    CommandEnvelope,
    CommandOutcome,
    CommandRejected,
    CommandStatus,
    DomainEvent,
    hash_json,
)
from trading_control_plane.metrics import VENUE_FACT_INPUT_LINKS, VENUE_FACT_NORMALIZATIONS
from trading_control_plane.reconciliation import ReconciliationSourceType
from trading_control_plane.reconciliation_models import (
    ExecutionReconciliationInput,
    ExecutionReconciliationRun,
    ExecutionReconciliationRunState,
)
from trading_control_plane.sender_fencing_models import (
    ExecutionSenderScope,
    ExecutionSenderScopeState,
)
from trading_control_plane.venue_fact_models import (
    VenueAccountEquitySnapshot,
    VenueFactInputLink,
    VenueFill,
    VenueOrderObservation,
    VenuePositionSnapshot,
    VenueProtectionSnapshot,
)

VENUE_FACT_SERVICE_PRINCIPAL = "execution-reconciliation-service"


class VenueOrderStatus(StrEnum):
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class VenueSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class VenuePositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    BOTH = "BOTH"


class VenuePositionMode(StrEnum):
    ONE_WAY = "ONE_WAY"
    HEDGE = "HEDGE"


class VenuePositionState(StrEnum):
    OPEN = "OPEN"
    FLAT = "FLAT"
    UNKNOWN = "UNKNOWN"


class VenuePositionDirection(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"
    UNKNOWN = "UNKNOWN"


class VenueProtectionState(StrEnum):
    CONFIRMED = "CONFIRMED"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"


class VenueAccountEquityState(StrEnum):
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"


class VenueProtectedDirection(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    UNKNOWN = "UNKNOWN"


class LiquidityRole(StrEnum):
    MAKER = "MAKER"
    TAKER = "TAKER"
    UNKNOWN = "UNKNOWN"


class FeeEffect(StrEnum):
    CHARGE = "CHARGE"
    REBATE = "REBATE"
    ZERO = "ZERO"


class VenueFactCollectionBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    venue_fact_input_link_id: UUID
    reconciliation_input_id: UUID
    reconciliation_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    venue: str = Field(min_length=1, max_length=80)
    execution_domain: str = Field(min_length=1, max_length=120)
    account_id: str = Field(min_length=1, max_length=160)
    instrument_id: str | None = Field(default=None, min_length=1, max_length=255)
    source_version: str = Field(min_length=1, max_length=160)
    normalization_version: str = Field(min_length=1, max_length=160)
    normalized_payload: dict[str, Any]
    raw_payload_ref: str = Field(min_length=1, max_length=255)
    raw_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ref: str = Field(min_length=1, max_length=255)
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_time: datetime
    venue_observed_at: datetime
    received_at: datetime

    @model_validator(mode="after")
    def collection_evidence_is_ordered(self) -> Self:
        times = (self.event_time, self.venue_observed_at, self.received_at)
        if any(value.tzinfo is None for value in times):
            raise ValueError("venue fact timestamps must be timezone-aware")
        if not self.event_time <= self.venue_observed_at <= self.received_at:
            raise ValueError("venue fact timestamps are not monotonic")
        return self


class RecordVenueOrderObservationRequest(VenueFactCollectionBinding):
    instrument_id: str = Field(min_length=1, max_length=255)
    venue_order_observation_id: UUID
    observed_client_order_id: str | None = Field(default=None, max_length=160)
    venue_order_id: str = Field(min_length=1, max_length=255)
    venue_update_id: str = Field(min_length=1, max_length=255)
    status: VenueOrderStatus
    side: VenueSide
    position_side: VenuePositionSide
    reduce_only: bool
    order_type: str = Field(min_length=1, max_length=40)
    time_in_force: str = Field(min_length=1, max_length=40)
    original_quantity: Decimal = Field(gt=0)
    cumulative_filled_quantity: Decimal = Field(ge=0)
    known_remaining_quantity: Decimal = Field(ge=0)
    zero_fill_confirmed: bool
    terminal: bool
    observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def observation_is_self_consistent(self) -> Self:
        filled = self.cumulative_filled_quantity
        remaining = self.known_remaining_quantity
        original = self.original_quantity
        if filled + remaining > original:
            raise ValueError("venue order quantities exceed original quantity")
        if self.status is VenueOrderStatus.OPEN:
            valid = filled == 0 and remaining == original and not self.terminal
        elif self.status is VenueOrderStatus.PARTIALLY_FILLED:
            valid = filled > 0 and remaining > 0 and filled + remaining == original
            valid = valid and not self.terminal
        elif self.status is VenueOrderStatus.FILLED:
            valid = filled == original and remaining == 0 and self.terminal
        elif self.status is VenueOrderStatus.CANCEL_PENDING:
            valid = filled + remaining == original and not self.terminal
        elif self.status in {VenueOrderStatus.CANCELLED, VenueOrderStatus.EXPIRED}:
            valid = remaining == 0 and self.terminal and self.zero_fill_confirmed == (filled == 0)
        elif self.status is VenueOrderStatus.REJECTED:
            valid = filled == 0 and remaining == 0 and self.terminal and self.zero_fill_confirmed
        else:
            valid = not self.terminal and not self.zero_fill_confirmed
        if not valid:
            raise ValueError("venue order status semantics are inconsistent")
        if self.observation_hash != hash_json(_order_observation_contract(self)):
            raise ValueError("venue order observation hash mismatch")
        if self.evidence_hash != hash_json(self.model_dump(mode="json", exclude={"evidence_hash"})):
            raise ValueError("venue order evidence hash mismatch")
        return self


class RecordVenueFillRequest(VenueFactCollectionBinding):
    instrument_id: str = Field(min_length=1, max_length=255)
    venue_fill_id: UUID
    observed_client_order_id: str | None = Field(default=None, max_length=160)
    venue_order_id: str = Field(min_length=1, max_length=255)
    venue_trade_id: str = Field(min_length=1, max_length=255)
    side: VenueSide
    position_side: VenuePositionSide
    reduce_only: bool
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    contract_multiplier: Decimal = Field(gt=0)
    notional: Decimal = Field(gt=0)
    liquidity_role: LiquidityRole
    fee_amount: Decimal
    fee_currency: str = Field(min_length=1, max_length=80)
    fee_effect: FeeEffect
    realized_pnl: Decimal | None = None
    settlement_currency: str = Field(min_length=1, max_length=80)
    fill_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def fill_is_self_consistent(self) -> Self:
        if self.notional != self.quantity * self.price * self.contract_multiplier:
            raise ValueError("venue fill notional mismatch")
        fee_valid = (
            (self.fee_effect is FeeEffect.CHARGE and self.fee_amount > 0)
            or (self.fee_effect is FeeEffect.REBATE and self.fee_amount < 0)
            or (self.fee_effect is FeeEffect.ZERO and self.fee_amount == 0)
        )
        if not fee_valid:
            raise ValueError("venue fill fee sign mismatch")
        if self.fill_hash != hash_json(_fill_contract(self)):
            raise ValueError("venue fill hash mismatch")
        if self.evidence_hash != hash_json(self.model_dump(mode="json", exclude={"evidence_hash"})):
            raise ValueError("venue fill evidence hash mismatch")
        return self


class RecordVenuePositionSnapshotRequest(VenueFactCollectionBinding):
    instrument_id: str = Field(min_length=1, max_length=255)
    venue_position_snapshot_id: UUID
    venue_update_id: str = Field(min_length=1, max_length=255)
    position_mode: VenuePositionMode
    position_side: VenuePositionSide
    margin_mode: str = Field(min_length=1, max_length=80)
    collateral_pool_id: str = Field(min_length=1, max_length=160)
    position_state: VenuePositionState
    direction: VenuePositionDirection
    quantity: Decimal | None = None
    entry_price: Decimal | None = None
    mark_price: Decimal | None = None
    contract_multiplier: Decimal = Field(gt=0)
    notional: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    liquidation_price: Decimal | None = None
    leverage: Decimal | None = None
    initial_margin: Decimal | None = None
    maintenance_margin: Decimal | None = None
    settlement_currency: str = Field(min_length=1, max_length=80)
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def position_is_self_consistent(self) -> Self:
        if self.position_mode is VenuePositionMode.ONE_WAY:
            if self.position_side is not VenuePositionSide.BOTH:
                raise ValueError("one-way position must use BOTH position side")
        elif self.position_side is VenuePositionSide.BOTH:
            raise ValueError("hedge position must use LONG or SHORT position side")

        if self.position_state is VenuePositionState.OPEN:
            valid = (
                self.direction in {VenuePositionDirection.LONG, VenuePositionDirection.SHORT}
                and self.quantity is not None
                and self.quantity > 0
                and self.entry_price is not None
                and self.entry_price > 0
                and self.mark_price is not None
                and self.mark_price > 0
                and self.notional == self.quantity * self.mark_price * self.contract_multiplier
                and self.unrealized_pnl is not None
                and (self.liquidation_price is None or self.liquidation_price > 0)
                and (self.leverage is None or self.leverage > 0)
                and (self.initial_margin is None or self.initial_margin >= 0)
                and (self.maintenance_margin is None or self.maintenance_margin >= 0)
            )
            if (
                self.position_mode is VenuePositionMode.HEDGE
                and self.direction.value != self.position_side.value
            ):
                valid = False
        elif self.position_state is VenuePositionState.FLAT:
            valid = (
                self.direction is VenuePositionDirection.FLAT
                and self.quantity == 0
                and self.entry_price is None
                and (self.mark_price is None or self.mark_price > 0)
                and self.notional == 0
                and self.unrealized_pnl == 0
                and self.liquidation_price is None
                and self.leverage is None
                and self.initial_margin in {None, Decimal("0")}
                and self.maintenance_margin in {None, Decimal("0")}
            )
        else:
            valid = (
                self.direction is VenuePositionDirection.UNKNOWN
                and self.quantity is None
                and self.entry_price is None
                and self.mark_price is None
                and self.notional is None
                and self.unrealized_pnl is None
                and self.liquidation_price is None
                and self.leverage is None
                and self.initial_margin is None
                and self.maintenance_margin is None
            )
        if not valid:
            raise ValueError("venue position state semantics are inconsistent")
        if self.snapshot_hash != hash_json(_position_snapshot_contract(self)):
            raise ValueError("venue position snapshot hash mismatch")
        if self.evidence_hash != hash_json(self.model_dump(mode="json", exclude={"evidence_hash"})):
            raise ValueError("venue position evidence hash mismatch")
        return self


class RecordVenueProtectionSnapshotRequest(VenueFactCollectionBinding):
    instrument_id: str = Field(min_length=1, max_length=255)
    venue_protection_snapshot_id: UUID
    venue_position_snapshot_id: UUID
    venue_update_id: str = Field(min_length=1, max_length=255)
    position_mode: VenuePositionMode
    position_side: VenuePositionSide
    margin_mode: str = Field(min_length=1, max_length=80)
    collateral_pool_id: str = Field(min_length=1, max_length=160)
    protection_state: VenueProtectionState
    protected_direction: VenueProtectedDirection
    position_quantity: Decimal | None = None
    covered_quantity: Decimal | None = None
    uncovered_quantity: Decimal | None = None
    active_stop_order_count: int | None = Field(default=None, ge=0)
    venue_native: bool
    reduce_only_confirmed: bool
    replacement_in_progress: bool
    order_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def protection_is_self_consistent(self) -> Self:
        if self.position_mode is VenuePositionMode.ONE_WAY:
            if self.position_side is not VenuePositionSide.BOTH:
                raise ValueError("one-way protection must use BOTH position side")
        elif self.position_side is VenuePositionSide.BOTH:
            raise ValueError("hedge protection must use LONG or SHORT position side")

        if self.protection_state is VenueProtectionState.CONFIRMED:
            valid = (
                self.protected_direction
                in {VenueProtectedDirection.LONG, VenueProtectedDirection.SHORT}
                and self.position_quantity is not None
                and self.position_quantity > 0
                and self.covered_quantity == self.position_quantity
                and self.uncovered_quantity == 0
                and self.active_stop_order_count is not None
                and self.active_stop_order_count >= 1
                and self.venue_native
                and self.reduce_only_confirmed
                and not self.replacement_in_progress
            )
        elif self.protection_state is VenueProtectionState.DEGRADED:
            known_quantities = (
                self.position_quantity is not None
                and self.position_quantity > 0
                and self.covered_quantity is not None
                and self.covered_quantity >= 0
                and self.uncovered_quantity is not None
                and self.uncovered_quantity >= 0
                and self.covered_quantity + self.uncovered_quantity == self.position_quantity
                and self.active_stop_order_count is not None
            )
            deficient = (
                self.uncovered_quantity is not None
                and self.active_stop_order_count is not None
                and (
                    self.uncovered_quantity > 0
                    or self.active_stop_order_count == 0
                    or not self.venue_native
                    or not self.reduce_only_confirmed
                    or self.replacement_in_progress
                )
            )
            valid = (
                self.protected_direction
                in {VenueProtectedDirection.LONG, VenueProtectedDirection.SHORT}
                and known_quantities
                and deficient
            )
        else:
            valid = (
                self.protected_direction is VenueProtectedDirection.UNKNOWN
                and self.position_quantity is None
                and self.covered_quantity is None
                and self.uncovered_quantity is None
                and self.active_stop_order_count is None
                and not self.venue_native
                and not self.reduce_only_confirmed
                and not self.replacement_in_progress
            )
        if not valid:
            raise ValueError("venue protection state semantics are inconsistent")
        if self.snapshot_hash != hash_json(_protection_snapshot_contract(self)):
            raise ValueError("venue protection snapshot hash mismatch")
        if self.evidence_hash != hash_json(self.model_dump(mode="json", exclude={"evidence_hash"})):
            raise ValueError("venue protection evidence hash mismatch")
        return self


class RecordVenueAccountEquitySnapshotRequest(VenueFactCollectionBinding):
    venue_account_equity_snapshot_id: UUID
    venue_update_id: str = Field(min_length=1, max_length=255)
    margin_mode: str = Field(min_length=1, max_length=80)
    collateral_pool_id: str = Field(min_length=1, max_length=160)
    settlement_currency: str = Field(min_length=1, max_length=80)
    equity_state: VenueAccountEquityState
    wallet_balance: Decimal | None = None
    exchange_margin_equity: Decimal | None = None
    available_margin: Decimal | None = None
    total_unrealized_pnl: Decimal | None = None
    total_initial_margin: Decimal | None = Field(default=None, ge=0)
    total_maintenance_margin: Decimal | None = Field(default=None, ge=0)
    total_liability: Decimal | None = Field(default=None, ge=0)
    unsettled_fee: Decimal | None = None
    unsettled_funding: Decimal | None = None
    includes_unrealized_pnl: bool
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def account_equity_is_self_consistent(self) -> Self:
        economics = (
            self.wallet_balance,
            self.exchange_margin_equity,
            self.available_margin,
            self.total_unrealized_pnl,
            self.total_initial_margin,
            self.total_maintenance_margin,
            self.total_liability,
            self.unsettled_fee,
            self.unsettled_funding,
        )
        if self.instrument_id is not None:
            raise ValueError("account equity snapshot cannot claim instrument scope")
        if self.equity_state is VenueAccountEquityState.CONFIRMED:
            valid = all(value is not None for value in economics)
            valid = valid and self.includes_unrealized_pnl
        else:
            valid = all(value is None for value in economics)
            valid = valid and not self.includes_unrealized_pnl
        if not valid:
            raise ValueError("venue account equity state semantics are inconsistent")
        if self.snapshot_hash != hash_json(_account_equity_snapshot_contract(self)):
            raise ValueError("venue account equity snapshot hash mismatch")
        if self.evidence_hash != hash_json(self.model_dump(mode="json", exclude={"evidence_hash"})):
            raise ValueError("venue account equity evidence hash mismatch")
        return self


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _decimal(value: Decimal) -> str:
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return "0" if normalized in {"", "-0"} else normalized


def _optional_decimal(value: Decimal | None) -> str | None:
    return None if value is None else _decimal(value)


def _order_observation_contract(request: RecordVenueOrderObservationRequest) -> dict[str, Any]:
    return {
        "venue": request.venue,
        "execution_domain": request.execution_domain,
        "account_id": request.account_id,
        "instrument_id": request.instrument_id,
        "observed_client_order_id": request.observed_client_order_id,
        "venue_order_id": request.venue_order_id,
        "venue_update_id": request.venue_update_id,
        "status": request.status.value,
        "side": request.side.value,
        "position_side": request.position_side.value,
        "reduce_only": request.reduce_only,
        "order_type": request.order_type,
        "time_in_force": request.time_in_force,
        "original_quantity": _decimal(request.original_quantity),
        "cumulative_filled_quantity": _decimal(request.cumulative_filled_quantity),
        "known_remaining_quantity": _decimal(request.known_remaining_quantity),
        "zero_fill_confirmed": request.zero_fill_confirmed,
        "terminal": request.terminal,
        "event_time": _iso(request.event_time),
    }


def _fill_contract(request: RecordVenueFillRequest) -> dict[str, Any]:
    return {
        "venue": request.venue,
        "execution_domain": request.execution_domain,
        "account_id": request.account_id,
        "instrument_id": request.instrument_id,
        "observed_client_order_id": request.observed_client_order_id,
        "venue_order_id": request.venue_order_id,
        "venue_trade_id": request.venue_trade_id,
        "side": request.side.value,
        "position_side": request.position_side.value,
        "reduce_only": request.reduce_only,
        "quantity": _decimal(request.quantity),
        "price": _decimal(request.price),
        "contract_multiplier": _decimal(request.contract_multiplier),
        "notional": _decimal(request.notional),
        "liquidity_role": request.liquidity_role.value,
        "fee_amount": _decimal(request.fee_amount),
        "fee_currency": request.fee_currency,
        "fee_effect": request.fee_effect.value,
        "realized_pnl": (None if request.realized_pnl is None else _decimal(request.realized_pnl)),
        "settlement_currency": request.settlement_currency,
        "event_time": _iso(request.event_time),
    }


def _position_snapshot_contract(
    request: RecordVenuePositionSnapshotRequest,
) -> dict[str, Any]:
    return {
        "venue": request.venue,
        "execution_domain": request.execution_domain,
        "account_id": request.account_id,
        "instrument_id": request.instrument_id,
        "venue_update_id": request.venue_update_id,
        "position_mode": request.position_mode.value,
        "position_side": request.position_side.value,
        "margin_mode": request.margin_mode,
        "collateral_pool_id": request.collateral_pool_id,
        "position_state": request.position_state.value,
        "direction": request.direction.value,
        "quantity": _optional_decimal(request.quantity),
        "entry_price": _optional_decimal(request.entry_price),
        "mark_price": _optional_decimal(request.mark_price),
        "contract_multiplier": _decimal(request.contract_multiplier),
        "notional": _optional_decimal(request.notional),
        "unrealized_pnl": _optional_decimal(request.unrealized_pnl),
        "liquidation_price": _optional_decimal(request.liquidation_price),
        "leverage": _optional_decimal(request.leverage),
        "initial_margin": _optional_decimal(request.initial_margin),
        "maintenance_margin": _optional_decimal(request.maintenance_margin),
        "settlement_currency": request.settlement_currency,
        "event_time": _iso(request.event_time),
    }


def _protection_snapshot_contract(
    request: RecordVenueProtectionSnapshotRequest,
) -> dict[str, Any]:
    return {
        "venue": request.venue,
        "execution_domain": request.execution_domain,
        "account_id": request.account_id,
        "instrument_id": request.instrument_id,
        "venue_position_snapshot_id": str(request.venue_position_snapshot_id),
        "venue_update_id": request.venue_update_id,
        "position_mode": request.position_mode.value,
        "position_side": request.position_side.value,
        "margin_mode": request.margin_mode,
        "collateral_pool_id": request.collateral_pool_id,
        "protection_state": request.protection_state.value,
        "protected_direction": request.protected_direction.value,
        "position_quantity": _optional_decimal(request.position_quantity),
        "covered_quantity": _optional_decimal(request.covered_quantity),
        "uncovered_quantity": _optional_decimal(request.uncovered_quantity),
        "active_stop_order_count": request.active_stop_order_count,
        "venue_native": request.venue_native,
        "reduce_only_confirmed": request.reduce_only_confirmed,
        "replacement_in_progress": request.replacement_in_progress,
        "order_set_hash": request.order_set_hash,
        "event_time": _iso(request.event_time),
    }


def _account_equity_snapshot_contract(
    request: RecordVenueAccountEquitySnapshotRequest,
) -> dict[str, Any]:
    return {
        "venue": request.venue,
        "execution_domain": request.execution_domain,
        "account_id": request.account_id,
        "venue_update_id": request.venue_update_id,
        "margin_mode": request.margin_mode,
        "collateral_pool_id": request.collateral_pool_id,
        "settlement_currency": request.settlement_currency,
        "equity_state": request.equity_state.value,
        "wallet_balance": _optional_decimal(request.wallet_balance),
        "exchange_margin_equity": _optional_decimal(request.exchange_margin_equity),
        "available_margin": _optional_decimal(request.available_margin),
        "total_unrealized_pnl": _optional_decimal(request.total_unrealized_pnl),
        "total_initial_margin": _optional_decimal(request.total_initial_margin),
        "total_maintenance_margin": _optional_decimal(request.total_maintenance_margin),
        "total_liability": _optional_decimal(request.total_liability),
        "unsettled_fee": _optional_decimal(request.unsettled_fee),
        "unsettled_funding": _optional_decimal(request.unsettled_funding),
        "includes_unrealized_pnl": request.includes_unrealized_pnl,
        "event_time": _iso(request.event_time),
    }


class VenueFactNormalizationService:
    order_command_type = "execution.venue-order-observation.record.v1"
    fill_command_type = "execution.venue-fill.record.v1"
    position_command_type = "execution.venue-position-snapshot.record.v1"
    protection_command_type = "execution.venue-protection-snapshot.record.v1"
    account_equity_command_type = "execution.venue-account-equity-snapshot.record.v1"

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def record_order_observation(
        self, session: Session, envelope: CommandEnvelope
    ) -> CommandOutcome:
        run_id = self._run_id(envelope, self.order_command_type)
        try:
            request = RecordVenueOrderObservationRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("VENUE_ORDER_OBSERVATION_INVALID", str(exc)) from exc
        now = self._clock()
        self._lock_external_identity(
            session,
            f"venue-order:{envelope.scope.get('organization_id')}:{request.venue}:"
            f"{request.execution_domain}:"
            f"{request.account_id}:{request.venue_order_id}:{request.venue_update_id}",
        )
        run, reconciliation_input = self._validate_collection_context(
            session,
            envelope,
            run_id,
            request,
            ReconciliationSourceType.VENUE_ORDERS,
            now,
        )
        existing = session.execute(
            select(VenueOrderObservation).where(
                VenueOrderObservation.organization_id == run.organization_id,
                VenueOrderObservation.venue == request.venue,
                VenueOrderObservation.execution_domain == request.execution_domain,
                VenueOrderObservation.account_id == request.account_id,
                VenueOrderObservation.venue_order_id == request.venue_order_id,
                VenueOrderObservation.venue_update_id == request.venue_update_id,
            )
        ).scalar_one_or_none()
        identity_owner = session.get(VenueOrderObservation, request.venue_order_observation_id)
        if identity_owner is not None and (
            existing is None
            or identity_owner.venue_order_observation_id != existing.venue_order_observation_id
        ):
            raise CommandRejected(
                "VENUE_ORDER_OBSERVATION_ID_CONFLICT",
                "venue order observation identity already exists",
            )
        new_fact = existing is None
        if existing is None:
            self._require_new_fact_link_possible(session, reconciliation_input, request)
            existing = VenueOrderObservation(
                venue_order_observation_id=request.venue_order_observation_id,
                organization_id=run.organization_id,
                first_seen_run_id=run.run_id,
                first_seen_input_id=reconciliation_input.input_id,
                venue=request.venue,
                execution_domain=request.execution_domain,
                account_id=request.account_id,
                instrument_id=request.instrument_id,
                observed_client_order_id=request.observed_client_order_id,
                venue_order_id=request.venue_order_id,
                venue_update_id=request.venue_update_id,
                status=request.status.value,
                side=request.side.value,
                position_side=request.position_side.value,
                reduce_only=request.reduce_only,
                order_type=request.order_type,
                time_in_force=request.time_in_force,
                original_quantity=request.original_quantity,
                cumulative_filled_quantity=request.cumulative_filled_quantity,
                known_remaining_quantity=request.known_remaining_quantity,
                zero_fill_confirmed=request.zero_fill_confirmed,
                terminal=request.terminal,
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
                observation_hash=request.observation_hash,
                event_time=request.event_time,
                venue_observed_at=request.venue_observed_at,
                first_received_at=request.received_at,
                recorded_at=now,
            )
            session.add(existing)
            session.flush()
        elif (
            existing.organization_id != run.organization_id
            or existing.observation_hash != request.observation_hash
        ):
            raise CommandRejected(
                "VENUE_ORDER_OBSERVATION_CONFLICT",
                "venue order update identity has different immutable semantics",
            )
        return self._link_and_outcome(
            session,
            run,
            reconciliation_input,
            request,
            ReconciliationSourceType.VENUE_ORDERS,
            "ORDER_OBSERVATION",
            existing.venue_order_observation_id,
            None,
            None,
            None,
            None,
            existing.observation_hash,
            new_fact,
            now,
        )

    def record_fill(self, session: Session, envelope: CommandEnvelope) -> CommandOutcome:
        run_id = self._run_id(envelope, self.fill_command_type)
        try:
            request = RecordVenueFillRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("VENUE_FILL_INVALID", str(exc)) from exc
        now = self._clock()
        self._lock_external_identity(
            session,
            f"venue-fill:{envelope.scope.get('organization_id')}:{request.venue}:"
            f"{request.execution_domain}:"
            f"{request.account_id}:{request.venue_trade_id}",
        )
        run, reconciliation_input = self._validate_collection_context(
            session,
            envelope,
            run_id,
            request,
            ReconciliationSourceType.VENUE_FILLS,
            now,
        )
        existing = session.execute(
            select(VenueFill).where(
                VenueFill.organization_id == run.organization_id,
                VenueFill.venue == request.venue,
                VenueFill.execution_domain == request.execution_domain,
                VenueFill.account_id == request.account_id,
                VenueFill.venue_trade_id == request.venue_trade_id,
            )
        ).scalar_one_or_none()
        identity_owner = session.get(VenueFill, request.venue_fill_id)
        if identity_owner is not None and (
            existing is None or identity_owner.venue_fill_id != existing.venue_fill_id
        ):
            raise CommandRejected("VENUE_FILL_ID_CONFLICT", "venue fill identity already exists")
        new_fact = existing is None
        if existing is None:
            self._require_new_fact_link_possible(session, reconciliation_input, request)
            existing = VenueFill(
                venue_fill_id=request.venue_fill_id,
                organization_id=run.organization_id,
                first_seen_run_id=run.run_id,
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
                recorded_at=now,
            )
            session.add(existing)
            session.flush()
        elif (
            existing.organization_id != run.organization_id
            or existing.fill_hash != request.fill_hash
        ):
            raise CommandRejected(
                "VENUE_FILL_CONFLICT",
                "venue trade identity has different immutable semantics",
            )
        return self._link_and_outcome(
            session,
            run,
            reconciliation_input,
            request,
            ReconciliationSourceType.VENUE_FILLS,
            "FILL",
            None,
            existing.venue_fill_id,
            None,
            None,
            None,
            existing.fill_hash,
            new_fact,
            now,
        )

    def record_position_snapshot(
        self, session: Session, envelope: CommandEnvelope
    ) -> CommandOutcome:
        run_id = self._run_id(envelope, self.position_command_type)
        try:
            request = RecordVenuePositionSnapshotRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("VENUE_POSITION_SNAPSHOT_INVALID", str(exc)) from exc
        now = self._clock()
        self._lock_external_identity(
            session,
            f"venue-position:{envelope.scope.get('organization_id')}:{request.venue}:"
            f"{request.execution_domain}:{request.account_id}:{request.instrument_id}:"
            f"{request.position_mode.value}:{request.position_side.value}:"
            f"{request.margin_mode}:{request.collateral_pool_id}:{request.venue_update_id}",
        )
        run, reconciliation_input = self._validate_collection_context(
            session,
            envelope,
            run_id,
            request,
            ReconciliationSourceType.VENUE_POSITIONS,
            now,
        )
        scope = session.get(ExecutionSenderScope, run.scope_id)
        assert scope is not None
        if (
            request.position_mode.value != scope.position_mode
            or request.margin_mode != scope.margin_mode
            or request.collateral_pool_id != scope.collateral_pool_id
        ):
            raise CommandRejected(
                "VENUE_POSITION_SCOPE_MISMATCH",
                "position mode, margin mode, or collateral pool changed",
            )
        existing = session.execute(
            select(VenuePositionSnapshot).where(
                VenuePositionSnapshot.organization_id == run.organization_id,
                VenuePositionSnapshot.venue == request.venue,
                VenuePositionSnapshot.execution_domain == request.execution_domain,
                VenuePositionSnapshot.account_id == request.account_id,
                VenuePositionSnapshot.instrument_id == request.instrument_id,
                VenuePositionSnapshot.position_mode == request.position_mode.value,
                VenuePositionSnapshot.position_side == request.position_side.value,
                VenuePositionSnapshot.margin_mode == request.margin_mode,
                VenuePositionSnapshot.collateral_pool_id == request.collateral_pool_id,
                VenuePositionSnapshot.venue_update_id == request.venue_update_id,
            )
        ).scalar_one_or_none()
        identity_owner = session.get(VenuePositionSnapshot, request.venue_position_snapshot_id)
        if identity_owner is not None and (
            existing is None
            or identity_owner.venue_position_snapshot_id != existing.venue_position_snapshot_id
        ):
            raise CommandRejected(
                "VENUE_POSITION_SNAPSHOT_ID_CONFLICT",
                "venue position snapshot identity already exists",
            )
        new_fact = existing is None
        if existing is None:
            self._require_new_fact_link_possible(session, reconciliation_input, request)
            existing = VenuePositionSnapshot(
                venue_position_snapshot_id=request.venue_position_snapshot_id,
                organization_id=run.organization_id,
                first_seen_run_id=run.run_id,
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
                recorded_at=now,
            )
            session.add(existing)
            session.flush()
        elif (
            existing.organization_id != run.organization_id
            or existing.snapshot_hash != request.snapshot_hash
        ):
            raise CommandRejected(
                "VENUE_POSITION_SNAPSHOT_CONFLICT",
                "venue position update identity has different immutable semantics",
            )
        return self._link_and_outcome(
            session,
            run,
            reconciliation_input,
            request,
            ReconciliationSourceType.VENUE_POSITIONS,
            "POSITION_SNAPSHOT",
            None,
            None,
            existing.venue_position_snapshot_id,
            None,
            None,
            existing.snapshot_hash,
            new_fact,
            now,
        )

    def record_protection_snapshot(
        self, session: Session, envelope: CommandEnvelope
    ) -> CommandOutcome:
        run_id = self._run_id(envelope, self.protection_command_type)
        try:
            request = RecordVenueProtectionSnapshotRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("VENUE_PROTECTION_SNAPSHOT_INVALID", str(exc)) from exc
        now = self._clock()
        self._lock_external_identity(
            session,
            f"venue-protection:{envelope.scope.get('organization_id')}:{request.venue}:"
            f"{request.execution_domain}:{request.account_id}:{request.instrument_id}:"
            f"{request.position_mode.value}:{request.position_side.value}:"
            f"{request.margin_mode}:{request.collateral_pool_id}:{request.venue_update_id}",
        )
        run, reconciliation_input = self._validate_collection_context(
            session,
            envelope,
            run_id,
            request,
            ReconciliationSourceType.VENUE_PROTECTION,
            now,
        )
        scope = session.get(ExecutionSenderScope, run.scope_id)
        position = session.get(VenuePositionSnapshot, request.venue_position_snapshot_id)
        assert scope is not None
        if (
            request.position_mode.value != scope.position_mode
            or request.margin_mode != scope.margin_mode
            or request.collateral_pool_id != scope.collateral_pool_id
        ):
            raise CommandRejected(
                "VENUE_PROTECTION_SCOPE_MISMATCH",
                "position mode, margin mode, or collateral pool changed",
            )
        if position is None:
            raise CommandRejected(
                "VENUE_PROTECTION_POSITION_NOT_FOUND",
                "canonical venue position snapshot is unavailable",
            )
        expected_position_side = position.position_side
        expected_direction = position.direction
        if (
            position.organization_id != run.organization_id
            or position.venue != request.venue
            or position.execution_domain != request.execution_domain
            or position.account_id != request.account_id
            or position.instrument_id != request.instrument_id
            or position.position_mode != request.position_mode.value
            or expected_position_side != request.position_side.value
            or position.margin_mode != request.margin_mode
            or position.collateral_pool_id != request.collateral_pool_id
            or position.position_state != "OPEN"
            or request.event_time < position.event_time
        ):
            raise CommandRejected(
                "VENUE_PROTECTION_POSITION_MISMATCH",
                "protection snapshot does not bind the exact open venue position",
            )
        if request.protection_state is not VenueProtectionState.UNKNOWN and (
            request.protected_direction.value != expected_direction
            or request.position_quantity != position.quantity
        ):
            raise CommandRejected(
                "VENUE_PROTECTION_COVERAGE_MISMATCH",
                "protection direction or quantity differs from the bound position",
            )
        existing = session.execute(
            select(VenueProtectionSnapshot).where(
                VenueProtectionSnapshot.organization_id == run.organization_id,
                VenueProtectionSnapshot.venue == request.venue,
                VenueProtectionSnapshot.execution_domain == request.execution_domain,
                VenueProtectionSnapshot.account_id == request.account_id,
                VenueProtectionSnapshot.instrument_id == request.instrument_id,
                VenueProtectionSnapshot.position_mode == request.position_mode.value,
                VenueProtectionSnapshot.position_side == request.position_side.value,
                VenueProtectionSnapshot.margin_mode == request.margin_mode,
                VenueProtectionSnapshot.collateral_pool_id == request.collateral_pool_id,
                VenueProtectionSnapshot.venue_update_id == request.venue_update_id,
            )
        ).scalar_one_or_none()
        identity_owner = session.get(VenueProtectionSnapshot, request.venue_protection_snapshot_id)
        if identity_owner is not None and (
            existing is None
            or identity_owner.venue_protection_snapshot_id != existing.venue_protection_snapshot_id
        ):
            raise CommandRejected(
                "VENUE_PROTECTION_SNAPSHOT_ID_CONFLICT",
                "venue protection snapshot identity already exists",
            )
        new_fact = existing is None
        if existing is None:
            self._require_new_fact_link_possible(session, reconciliation_input, request)
            existing = VenueProtectionSnapshot(
                venue_protection_snapshot_id=request.venue_protection_snapshot_id,
                organization_id=run.organization_id,
                first_seen_run_id=run.run_id,
                first_seen_input_id=reconciliation_input.input_id,
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
                recorded_at=now,
            )
            session.add(existing)
            session.flush()
        elif (
            existing.organization_id != run.organization_id
            or existing.snapshot_hash != request.snapshot_hash
        ):
            raise CommandRejected(
                "VENUE_PROTECTION_SNAPSHOT_CONFLICT",
                "venue protection update identity has different immutable semantics",
            )
        return self._link_and_outcome(
            session,
            run,
            reconciliation_input,
            request,
            ReconciliationSourceType.VENUE_PROTECTION,
            "PROTECTION_SNAPSHOT",
            None,
            None,
            None,
            existing.venue_protection_snapshot_id,
            None,
            existing.snapshot_hash,
            new_fact,
            now,
        )

    def record_account_equity_snapshot(
        self, session: Session, envelope: CommandEnvelope
    ) -> CommandOutcome:
        run_id = self._run_id(envelope, self.account_equity_command_type)
        try:
            request = RecordVenueAccountEquitySnapshotRequest.model_validate(envelope.payload)
        except ValidationError as exc:
            raise CommandRejected("VENUE_ACCOUNT_EQUITY_SNAPSHOT_INVALID", str(exc)) from exc
        now = self._clock()
        self._lock_external_identity(
            session,
            f"venue-account-equity:{envelope.scope.get('organization_id')}:"
            f"{request.venue}:{request.execution_domain}:{request.account_id}:"
            f"{request.margin_mode}:{request.collateral_pool_id}:"
            f"{request.settlement_currency}:{request.venue_update_id}",
        )
        run, reconciliation_input = self._validate_collection_context(
            session,
            envelope,
            run_id,
            request,
            ReconciliationSourceType.VENUE_BALANCES,
            now,
        )
        scope = session.get(ExecutionSenderScope, run.scope_id)
        assert scope is not None
        if (
            request.margin_mode != scope.margin_mode
            or request.collateral_pool_id != scope.collateral_pool_id
        ):
            raise CommandRejected(
                "VENUE_ACCOUNT_EQUITY_SCOPE_MISMATCH",
                "margin mode or collateral pool changed",
            )
        existing = session.execute(
            select(VenueAccountEquitySnapshot).where(
                VenueAccountEquitySnapshot.organization_id == run.organization_id,
                VenueAccountEquitySnapshot.venue == request.venue,
                VenueAccountEquitySnapshot.execution_domain == request.execution_domain,
                VenueAccountEquitySnapshot.account_id == request.account_id,
                VenueAccountEquitySnapshot.margin_mode == request.margin_mode,
                VenueAccountEquitySnapshot.collateral_pool_id == request.collateral_pool_id,
                VenueAccountEquitySnapshot.settlement_currency == request.settlement_currency,
                VenueAccountEquitySnapshot.venue_update_id == request.venue_update_id,
            )
        ).scalar_one_or_none()
        identity_owner = session.get(
            VenueAccountEquitySnapshot, request.venue_account_equity_snapshot_id
        )
        if identity_owner is not None and (
            existing is None
            or identity_owner.venue_account_equity_snapshot_id
            != existing.venue_account_equity_snapshot_id
        ):
            raise CommandRejected(
                "VENUE_ACCOUNT_EQUITY_SNAPSHOT_ID_CONFLICT",
                "venue account equity snapshot identity already exists",
            )
        new_fact = existing is None
        if existing is None:
            self._require_new_fact_link_possible(session, reconciliation_input, request)
            existing = VenueAccountEquitySnapshot(
                venue_account_equity_snapshot_id=request.venue_account_equity_snapshot_id,
                organization_id=run.organization_id,
                first_seen_run_id=run.run_id,
                first_seen_input_id=reconciliation_input.input_id,
                venue=request.venue,
                execution_domain=request.execution_domain,
                account_id=request.account_id,
                venue_update_id=request.venue_update_id,
                margin_mode=request.margin_mode,
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
                recorded_at=now,
            )
            session.add(existing)
            session.flush()
        elif (
            existing.organization_id != run.organization_id
            or existing.snapshot_hash != request.snapshot_hash
        ):
            raise CommandRejected(
                "VENUE_ACCOUNT_EQUITY_SNAPSHOT_CONFLICT",
                "venue account equity update identity has different immutable semantics",
            )
        return self._link_and_outcome(
            session,
            run,
            reconciliation_input,
            request,
            ReconciliationSourceType.VENUE_BALANCES,
            "ACCOUNT_EQUITY_SNAPSHOT",
            None,
            None,
            None,
            None,
            existing.venue_account_equity_snapshot_id,
            existing.snapshot_hash,
            new_fact,
            now,
        )

    @staticmethod
    def _run_id(envelope: CommandEnvelope, expected_command_type: str) -> UUID:
        if envelope.command_type != expected_command_type:
            raise CommandRejected("COMMAND_TYPE_MISMATCH", "unexpected command type")
        if (
            envelope.channel is not CommandChannel.INTERNAL
            or envelope.service_principal != VENUE_FACT_SERVICE_PRINCIPAL
        ):
            raise CommandRejected(
                "VENUE_FACT_SERVICE_REQUIRED",
                "only the reconciliation service may normalize private venue facts",
            )
        if envelope.object_type != "ExecutionReconciliationRun" or envelope.object_id is None:
            raise CommandRejected(
                "OBJECT_BINDING_MISMATCH", "ExecutionReconciliationRun binding is required"
            )
        try:
            return UUID(envelope.object_id)
        except ValueError as exc:
            raise CommandRejected("OBJECT_BINDING_MISMATCH", "run identity is invalid") from exc

    @staticmethod
    def _lock_external_identity(session: Session, lock_key: str) -> None:
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )

    @staticmethod
    def _validate_collection_context(
        session: Session,
        envelope: CommandEnvelope,
        run_id: UUID,
        request: VenueFactCollectionBinding,
        expected_source: ReconciliationSourceType,
        now: datetime,
    ) -> tuple[ExecutionReconciliationRun, ExecutionReconciliationInput]:
        run = session.get(ExecutionReconciliationRun, run_id)
        state = session.execute(
            select(ExecutionReconciliationRunState)
            .where(ExecutionReconciliationRunState.run_id == run_id)
            .with_for_update()
        ).scalar_one_or_none()
        if run is None or state is None:
            raise CommandRejected("RECONCILIATION_RUN_NOT_FOUND", "run is unavailable")
        if envelope.scope.get("organization_id") != run.organization_id:
            raise CommandRejected("SCOPE_MISMATCH", "organization scope changed")
        if (
            state.status != "RUNNING"
            or state.phase != "COLLECTING"
            or run.environment != "SHADOW"
            or run.live_dispatch_eligible
        ):
            raise CommandRejected(
                "VENUE_FACT_COLLECTION_CLOSED",
                "venue facts require an active SHADOW collection phase",
            )
        if now >= run.deadline_at:
            raise CommandRejected(
                "VENUE_FACT_COLLECTION_EXPIRED", "run deadline elapsed before normalization"
            )
        latest_run_id = session.execute(
            select(ExecutionReconciliationRun.run_id)
            .where(ExecutionReconciliationRun.scope_id == run.scope_id)
            .order_by(
                ExecutionReconciliationRun.started_at.desc(),
                ExecutionReconciliationRun.run_id.desc(),
            )
            .limit(1)
        ).scalar_one()
        if latest_run_id != run.run_id:
            raise CommandRejected(
                "VENUE_FACT_RUN_NOT_LATEST", "only the latest scope run may normalize facts"
            )
        reconciliation_input = session.get(
            ExecutionReconciliationInput, request.reconciliation_input_id
        )
        if (
            reconciliation_input is None
            or reconciliation_input.run_id != run.run_id
            or reconciliation_input.organization_id != run.organization_id
            or reconciliation_input.source_type != expected_source.value
            or reconciliation_input.collection_status != "COMPLETE"
            or reconciliation_input.source_version != request.source_version
            or reconciliation_input.input_hash != request.reconciliation_input_hash
        ):
            raise CommandRejected(
                "VENUE_FACT_INPUT_MISMATCH",
                "venue fact is not bound to the exact complete source input",
            )
        if not (
            reconciliation_input.observed_from
            <= request.event_time
            <= reconciliation_input.observed_through
        ):
            raise CommandRejected(
                "VENUE_FACT_OUTSIDE_INPUT_WINDOW", "venue fact is outside the input watermark"
            )
        if request.received_at > now or reconciliation_input.received_at > now:
            raise CommandRejected("VENUE_FACT_FROM_FUTURE", "venue fact is from the future")
        scope = session.get(ExecutionSenderScope, run.scope_id)
        sender_state = session.get(ExecutionSenderScopeState, run.scope_id)
        if scope is None or sender_state is None:
            raise CommandRejected("VENUE_FACT_SCOPE_NOT_FOUND", "sender scope is unavailable")
        if (
            request.venue != scope.venue
            or request.execution_domain != scope.execution_domain
            or request.account_id != scope.account_id
        ):
            raise CommandRejected("VENUE_FACT_ROUTE_MISMATCH", "venue fact route changed")
        if (
            sender_state.status != "LEASED"
            or sender_state.active_lease_id != run.lease_id
            or sender_state.current_fencing_token != run.fencing_token
            or sender_state.lease_expires_at is None
            or now >= sender_state.lease_expires_at
        ):
            raise CommandRejected(
                "VENUE_FACT_SENDER_LEASE_STALE", "normalization authority is fenced or expired"
            )
        return run, reconciliation_input

    @staticmethod
    def _link_and_outcome(
        session: Session,
        run: ExecutionReconciliationRun,
        reconciliation_input: ExecutionReconciliationInput,
        request: VenueFactCollectionBinding,
        source_type: ReconciliationSourceType,
        fact_type: str,
        order_observation_id: UUID | None,
        fill_id: UUID | None,
        position_snapshot_id: UUID | None,
        protection_snapshot_id: UUID | None,
        account_equity_snapshot_id: UUID | None,
        fact_hash: str,
        new_fact: bool,
        now: datetime,
    ) -> CommandOutcome:
        if order_observation_id is not None:
            fact_identity = VenueFactInputLink.venue_order_observation_id == order_observation_id
        elif fill_id is not None:
            fact_identity = VenueFactInputLink.venue_fill_id == fill_id
        elif position_snapshot_id is not None:
            fact_identity = VenueFactInputLink.venue_position_snapshot_id == position_snapshot_id
        elif protection_snapshot_id is not None:
            fact_identity = (
                VenueFactInputLink.venue_protection_snapshot_id == protection_snapshot_id
            )
        else:
            assert account_equity_snapshot_id is not None
            fact_identity = (
                VenueFactInputLink.venue_account_equity_snapshot_id == account_equity_snapshot_id
            )
        existing_link = session.execute(
            select(VenueFactInputLink).where(
                VenueFactInputLink.reconciliation_input_id == reconciliation_input.input_id,
                fact_identity,
            )
        ).scalar_one_or_none()
        link_values = {
            "run_id": str(run.run_id),
            "reconciliation_input_id": str(reconciliation_input.input_id),
            "organization_id": run.organization_id,
            "source_type": source_type.value,
            "venue_order_observation_id": None
            if order_observation_id is None
            else str(order_observation_id),
            "venue_fill_id": None if fill_id is None else str(fill_id),
            "input_hash": reconciliation_input.input_hash,
            "fact_hash": fact_hash,
            "raw_payload_hash": request.raw_payload_hash,
            "evidence_hash": request.evidence_hash,
            "observed_at": _iso(request.venue_observed_at),
            "received_at": _iso(request.received_at),
        }
        if position_snapshot_id is not None:
            link_values["venue_position_snapshot_id"] = str(position_snapshot_id)
        if protection_snapshot_id is not None:
            link_values["venue_protection_snapshot_id"] = str(protection_snapshot_id)
        if account_equity_snapshot_id is not None:
            link_values["venue_account_equity_snapshot_id"] = str(account_equity_snapshot_id)
        link_hash = hash_json(link_values)
        if existing_link is not None:
            if existing_link.link_hash != link_hash:
                raise CommandRejected(
                    "VENUE_FACT_INPUT_LINK_CONFLICT",
                    "input already links different evidence for this venue fact",
                )
            VENUE_FACT_NORMALIZATIONS.labels(fact_type, "EXISTING_FACT").inc()
            VENUE_FACT_INPUT_LINKS.labels(source_type.value, "ALREADY_LINKED").inc()
            return VenueFactNormalizationService._outcome(
                run,
                fact_type,
                order_observation_id,
                fill_id,
                position_snapshot_id,
                protection_snapshot_id,
                account_equity_snapshot_id,
                existing_link.venue_fact_input_link_id,
                fact_hash,
                new_fact=False,
                new_link=False,
            )
        if session.get(VenueFactInputLink, request.venue_fact_input_link_id) is not None:
            raise CommandRejected(
                "VENUE_FACT_INPUT_LINK_ID_CONFLICT", "input link identity already exists"
            )
        linked_count = session.execute(
            select(func.count())
            .select_from(VenueFactInputLink)
            .where(VenueFactInputLink.reconciliation_input_id == reconciliation_input.input_id)
        ).scalar_one()
        if linked_count >= reconciliation_input.item_count:
            raise CommandRejected(
                "VENUE_FACT_INPUT_COUNT_EXCEEDED",
                "normalized venue facts exceed the frozen input item count",
            )
        link = VenueFactInputLink(
            venue_fact_input_link_id=request.venue_fact_input_link_id,
            run_id=run.run_id,
            reconciliation_input_id=reconciliation_input.input_id,
            organization_id=run.organization_id,
            source_type=source_type.value,
            venue_order_observation_id=order_observation_id,
            venue_fill_id=fill_id,
            venue_position_snapshot_id=position_snapshot_id,
            venue_protection_snapshot_id=protection_snapshot_id,
            venue_account_equity_snapshot_id=account_equity_snapshot_id,
            input_hash=reconciliation_input.input_hash,
            fact_hash=fact_hash,
            raw_payload_ref=request.raw_payload_ref,
            raw_payload_hash=request.raw_payload_hash,
            evidence_ref=request.evidence_ref,
            evidence_hash=request.evidence_hash,
            observed_at=request.venue_observed_at,
            received_at=request.received_at,
            link_hash=link_hash,
            linked_at=now,
        )
        session.add(link)
        session.flush()
        VENUE_FACT_NORMALIZATIONS.labels(
            fact_type, "NEW_FACT" if new_fact else "EXISTING_FACT"
        ).inc()
        VENUE_FACT_INPUT_LINKS.labels(source_type.value, "LINKED").inc()
        return VenueFactNormalizationService._outcome(
            run,
            fact_type,
            order_observation_id,
            fill_id,
            position_snapshot_id,
            protection_snapshot_id,
            account_equity_snapshot_id,
            link.venue_fact_input_link_id,
            fact_hash,
            new_fact=new_fact,
            new_link=True,
        )

    @staticmethod
    def _require_new_fact_link_possible(
        session: Session,
        reconciliation_input: ExecutionReconciliationInput,
        request: VenueFactCollectionBinding,
    ) -> None:
        if session.get(VenueFactInputLink, request.venue_fact_input_link_id) is not None:
            raise CommandRejected(
                "VENUE_FACT_INPUT_LINK_ID_CONFLICT", "input link identity already exists"
            )
        linked_count = session.execute(
            select(func.count())
            .select_from(VenueFactInputLink)
            .where(VenueFactInputLink.reconciliation_input_id == reconciliation_input.input_id)
        ).scalar_one()
        if linked_count >= reconciliation_input.item_count:
            raise CommandRejected(
                "VENUE_FACT_INPUT_COUNT_EXCEEDED",
                "normalized venue facts exceed the frozen input item count",
            )

    @staticmethod
    def _outcome(
        run: ExecutionReconciliationRun,
        fact_type: str,
        order_observation_id: UUID | None,
        fill_id: UUID | None,
        position_snapshot_id: UUID | None,
        protection_snapshot_id: UUID | None,
        account_equity_snapshot_id: UUID | None,
        link_id: UUID,
        fact_hash: str,
        *,
        new_fact: bool,
        new_link: bool,
    ) -> CommandOutcome:
        fact_id = (
            order_observation_id
            or fill_id
            or position_snapshot_id
            or protection_snapshot_id
            or account_equity_snapshot_id
        )
        assert fact_id is not None
        if order_observation_id is not None:
            event_type = "VenueOrderObserved"
            object_type = "VenueOrderObservation"
        elif fill_id is not None:
            event_type = "VenueFillObserved"
            object_type = "VenueFill"
        elif position_snapshot_id is not None:
            event_type = "VenuePositionSnapshotObserved"
            object_type = "VenuePositionSnapshot"
        elif protection_snapshot_id is not None:
            event_type = "VenueProtectionSnapshotObserved"
            object_type = "VenueProtectionSnapshot"
        else:
            event_type = "VenueAccountEquitySnapshotObserved"
            object_type = "VenueAccountEquitySnapshot"
        return CommandOutcome(
            status=CommandStatus.COMPLETED,
            object_type=object_type,
            object_id=str(fact_id),
            object_version=1,
            data={
                "venue_fact_id": str(fact_id),
                "venue_fact_input_link_id": str(link_id),
                "fact_type": fact_type,
                "fact_hash": fact_hash,
                "new_fact": new_fact,
                "new_input_link": new_link,
                "execution_reconciliation_run_id": str(run.run_id),
                "dispatch_eligible": False,
            },
            events=(
                DomainEvent(
                    event_type=event_type,
                    aggregate_type="ExecutionReconciliationRun",
                    aggregate_id=str(run.run_id),
                    payload={
                        "venue_fact_id": str(fact_id),
                        "venue_fact_input_link_id": str(link_id),
                        "fact_type": fact_type,
                        "fact_hash": fact_hash,
                        "new_fact": new_fact,
                        "new_input_link": new_link,
                    },
                ),
            ),
        )
