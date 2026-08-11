from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

from trading_control_plane.domain import MONEY_QUANTUM, DomainRejected

BPS = Decimal(10_000)


@dataclass(frozen=True, slots=True)
class ShadowExecutionQuote:
    reference_price: Decimal
    fill_price: Decimal
    fee: Decimal
    slippage_cost: Decimal


@dataclass(frozen=True, slots=True)
class ShadowPositionState:
    quantity: Decimal
    average_entry_price: Decimal


@dataclass(frozen=True, slots=True)
class ShadowLedgerPositionState:
    quantity: Decimal
    average_entry_price: Decimal
    realized_pnl: Decimal


def quantize_shadow_step(
    value: Decimal,
    step: Decimal,
    *,
    rounding: str = ROUND_HALF_EVEN,
) -> Decimal:
    if not _finite_positive(value) or not _finite_positive(step):
        raise DomainRejected(
            "SHADOW_PRECISION_INVALID", "value and precision step must be positive"
        )
    return (value / step).to_integral_value(rounding=rounding) * step


def shadow_limit_crossed(*, side: str, latest_price: Decimal, limit_price: Decimal) -> bool:
    if side not in {"BUY", "SELL"}:
        raise DomainRejected("SHADOW_SIDE_INVALID", "shadow side must be BUY or SELL")
    if not _finite_positive(latest_price) or not _finite_positive(limit_price):
        raise DomainRejected("SHADOW_PRICE_INVALID", "latest and limit prices must be positive")
    return latest_price <= limit_price if side == "BUY" else latest_price >= limit_price


def shadow_protection_triggered(
    *,
    position_quantity: Decimal,
    trigger_type: str,
    latest_price: Decimal,
    trigger_price: Decimal,
) -> bool:
    if position_quantity == 0:
        return False
    if trigger_type not in {"STOP_LOSS", "TAKE_PROFIT"}:
        raise DomainRejected("SHADOW_TRIGGER_TYPE_INVALID", "unknown protection trigger type")
    if not _finite_positive(latest_price) or not _finite_positive(trigger_price):
        raise DomainRejected("SHADOW_PRICE_INVALID", "latest and trigger prices must be positive")
    if position_quantity > 0:
        return (
            latest_price <= trigger_price
            if trigger_type == "STOP_LOSS"
            else latest_price >= trigger_price
        )
    return (
        latest_price >= trigger_price
        if trigger_type == "STOP_LOSS"
        else latest_price <= trigger_price
    )


def _finite_positive(value: Decimal) -> bool:
    return value.is_finite() and value > 0


def quote_shadow_execution(
    *,
    side: str,
    quantity: Decimal,
    reference_price: Decimal,
    tick_size: Decimal,
    contract_multiplier: Decimal,
    fee_bps: Decimal,
    slippage_bps: Decimal,
) -> ShadowExecutionQuote:
    if side not in {"BUY", "SELL"}:
        raise DomainRejected("SHADOW_SIDE_INVALID", "shadow side must be BUY or SELL")
    if not all(
        _finite_positive(value)
        for value in (quantity, reference_price, tick_size, contract_multiplier)
    ):
        raise DomainRejected(
            "SHADOW_QUOTE_INVALID",
            "quantity, reference price, tick size and multiplier must be finite and positive",
        )
    if (
        not fee_bps.is_finite()
        or not slippage_bps.is_finite()
        or fee_bps < 0
        or fee_bps > 100
        or slippage_bps < 0
        or slippage_bps > 500
    ):
        raise DomainRejected(
            "SHADOW_COST_MODEL_INVALID",
            "fee must be 0-100 bps and slippage must be 0-500 bps",
        )
    direction = Decimal(1) if side == "BUY" else Decimal(-1)
    raw_price = reference_price * (Decimal(1) + direction * slippage_bps / BPS)
    rounding = ROUND_CEILING if side == "BUY" else ROUND_FLOOR
    fill_price = (raw_price / tick_size).to_integral_value(rounding=rounding) * tick_size
    if not _finite_positive(fill_price):
        raise DomainRejected("SHADOW_QUOTE_INVALID", "simulated fill price is invalid")
    notional = quantity * fill_price * contract_multiplier
    fee = (notional * fee_bps / BPS).quantize(MONEY_QUANTUM)
    slippage_cost = (quantity * abs(fill_price - reference_price) * contract_multiplier).quantize(
        MONEY_QUANTUM
    )
    return ShadowExecutionQuote(
        reference_price=reference_price,
        fill_price=fill_price,
        fee=fee,
        slippage_cost=slippage_cost,
    )


def apply_shadow_fill(
    *,
    current_quantity: Decimal,
    current_average_entry_price: Decimal,
    side: str,
    fill_quantity: Decimal,
    fill_price: Decimal,
    reduce_only: bool,
) -> ShadowPositionState:
    if (
        side not in {"BUY", "SELL"}
        or not _finite_positive(fill_quantity)
        or not _finite_positive(fill_price)
    ):
        raise DomainRejected("SHADOW_FILL_INVALID", "shadow fill identity is invalid")
    signed_fill = fill_quantity if side == "BUY" else -fill_quantity
    next_quantity = current_quantity + signed_fill
    if reduce_only and (
        current_quantity == 0
        or abs(next_quantity) > abs(current_quantity)
        or (next_quantity != 0 and (next_quantity > 0) != (current_quantity > 0))
    ):
        raise DomainRejected(
            "SHADOW_REDUCE_ONLY_VIOLATION",
            "reduce-only simulation cannot increase or reverse the virtual position",
        )
    if next_quantity == 0:
        return ShadowPositionState(Decimal(0), Decimal(0))
    increasing = current_quantity == 0 or (current_quantity > 0) == (signed_fill > 0)
    if increasing:
        previous_notional = abs(current_quantity) * current_average_entry_price
        fill_notional = fill_quantity * fill_price
        average = (previous_notional + fill_notional) / abs(next_quantity)
    elif (next_quantity > 0) == (current_quantity > 0):
        average = current_average_entry_price
    else:
        average = fill_price
    return ShadowPositionState(next_quantity, average.quantize(MONEY_QUANTUM))


def apply_shadow_ledger_fill(
    *,
    current_quantity: Decimal,
    current_average_entry_price: Decimal,
    side: str,
    fill_quantity: Decimal,
    fill_price: Decimal,
    contract_multiplier: Decimal,
    reduce_only: bool,
) -> ShadowLedgerPositionState:
    next_state = apply_shadow_fill(
        current_quantity=current_quantity,
        current_average_entry_price=current_average_entry_price,
        side=side,
        fill_quantity=fill_quantity,
        fill_price=fill_price,
        reduce_only=reduce_only,
    )
    if not _finite_positive(contract_multiplier):
        raise DomainRejected(
            "SHADOW_CONTRACT_MULTIPLIER_INVALID",
            "contract multiplier must be finite and positive",
        )
    signed_fill = fill_quantity if side == "BUY" else -fill_quantity
    opposite = current_quantity != 0 and (current_quantity > 0) != (signed_fill > 0)
    closed_quantity = min(abs(current_quantity), fill_quantity) if opposite else Decimal(0)
    realized = Decimal(0)
    if closed_quantity:
        direction = Decimal(1) if current_quantity > 0 else Decimal(-1)
        realized = (
            (fill_price - current_average_entry_price)
            * closed_quantity
            * contract_multiplier
            * direction
        ).quantize(MONEY_QUANTUM)
    return ShadowLedgerPositionState(
        quantity=next_state.quantity,
        average_entry_price=next_state.average_entry_price,
        realized_pnl=realized,
    )
