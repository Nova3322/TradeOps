from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class VenueInstrument:
    symbol: str
    tick_size: Decimal
    lot_size: Decimal
    minimum_notional: Decimal
    quote_currency: str
    collateral_currency: str
    active: bool


@dataclass(frozen=True, slots=True)
class VenueOrder:
    order_id: str
    client_order_id: str
    status: str
    side: str
    order_type: str
    ordered_quantity: Decimal
    filled_quantity: Decimal
    stop_price: Decimal
    reduce_only: bool
    close_position: bool
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class VenueFill:
    fill_id: str
    order_id: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_currency: str
    executed_at: datetime


@dataclass(frozen=True, slots=True)
class VenuePosition:
    quantity: Decimal
    average_entry_price: Decimal
    mark_price: Decimal
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class VenueEquity:
    equity: Decimal
    available_balance: Decimal
    currency: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class VenueFunding:
    payment_id: str
    amount: Decimal
    currency: str
    paid_at: datetime


@dataclass(frozen=True, slots=True)
class VenueProtection:
    order_id: str
    quantity: Decimal
    trigger_price: Decimal
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class VenueReadOnlySnapshot:
    symbol: str
    observed_at: datetime
    instrument: VenueInstrument
    orders: tuple[VenueOrder, ...]
    fills: tuple[VenueFill, ...]
    position: VenuePosition
    equity: VenueEquity
    funding: tuple[VenueFunding, ...]
    protection: VenueProtection | None
    history_error_code: str | None = None
