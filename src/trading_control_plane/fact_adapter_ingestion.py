from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_control_plane.adapters.facts import ExchangeFactSnapshot
from trading_control_plane.domain import DomainRejected
from trading_control_plane.venue_read_only import (
    VenueEquity,
    VenueFill,
    VenueFunding,
    VenueInstrument,
    VenueOrder,
    VenuePosition,
    VenueProtection,
    VenueReadOnlySnapshot,
)


def _decimal(value: object, field: str, *, allow_none: bool = False) -> Decimal | None:
    if value is None and allow_none:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DomainRejected(
            "FACT_ADAPTER_SNAPSHOT_INVALID",
            f"normalized fact field {field} is not numeric",
        ) from exc
    if not result.is_finite():
        raise DomainRejected(
            "FACT_ADAPTER_SNAPSHOT_INVALID",
            f"normalized fact field {field} is not finite",
        )
    return result


def _time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise DomainRejected(
            "FACT_ADAPTER_SNAPSHOT_INVALID",
            f"normalized fact field {field} has no timestamp",
        )
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DomainRejected(
            "FACT_ADAPTER_SNAPSHOT_INVALID",
            f"normalized fact field {field} has an invalid timestamp",
        ) from exc
    if result.utcoffset() is None:
        raise DomainRejected(
            "FACT_ADAPTER_SNAPSHOT_INVALID",
            f"normalized fact field {field} has no timezone",
        )
    return result.astimezone(UTC)


def _native_symbol(value: Mapping[str, Any]) -> str:
    symbol = value.get("native_symbol")
    if not isinstance(symbol, str) or not symbol:
        raise DomainRejected(
            "FACT_ADAPTER_SNAPSHOT_INVALID",
            "normalized fact has no native instrument identity",
        )
    return symbol


def _status(value: object) -> str:
    normalized = str(value or "").lower()
    mapping = {
        "open": "SENT",
        "new": "SENT",
        "partially_filled": "PARTIALLY_FILLED",
        "partiallyfilled": "PARTIALLY_FILLED",
        "closed": "FILLED",
        "filled": "FILLED",
        "canceled": "CANCELLED",
        "cancelled": "CANCELLED",
        "rejected": "REJECTED",
        "expired": "CANCELLED",
    }
    result = mapping.get(normalized)
    if result is None:
        raise DomainRejected(
            "FACT_ADAPTER_SNAPSHOT_INVALID",
            "normalized fact order status is unknown",
        )
    return result


def _by_symbol(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(_native_symbol(row), []).append(row)
    return grouped


def normalize_fact_adapter_snapshot(
    snapshot: ExchangeFactSnapshot,
) -> tuple[VenueReadOnlySnapshot, ...]:
    """Convert a confirmed adapter snapshot into the existing stable persistence contract."""

    if snapshot.data_status != "CURRENT":
        raise DomainRejected(
            "FACT_ADAPTER_SNAPSHOT_NOT_CURRENT",
            "stale or unknown adapter facts cannot overwrite current persisted facts",
        )
    instruments = {
        _native_symbol(row): row for row in snapshot.instruments if isinstance(row, Mapping)
    }
    if len(instruments) != len(snapshot.instruments) or not instruments:
        raise DomainRejected(
            "FACT_ADAPTER_SNAPSHOT_INVALID",
            "normalized fact instrument coverage is incomplete",
        )
    positions = _by_symbol(snapshot.positions)
    orders = _by_symbol(snapshot.orders)
    fills = _by_symbol(snapshot.fills)
    marks = _by_symbol(snapshot.marks)
    funding = _by_symbol(tuple(row for row in snapshot.funding if row.get("kind") == "PAYMENT"))
    balances = {
        str(row.get("currency")): row
        for row in snapshot.balances
        if isinstance(row.get("currency"), str)
    }
    results: list[VenueReadOnlySnapshot] = []
    for native, instrument in sorted(instruments.items()):
        instrument_positions = [
            row
            for row in positions.get(native, [])
            if _decimal(row.get("quantity"), "position.quantity") != 0
        ]
        if len(instrument_positions) > 1:
            raise DomainRejected(
                "FACT_ADAPTER_HEDGE_MODE_UNREPRESENTABLE",
                "simultaneous long and short legs cannot be collapsed into one position",
            )
        position = instrument_positions[0] if instrument_positions else None
        instrument_marks = marks.get(native, [])
        if len(instrument_marks) != 1:
            raise DomainRejected(
                "FACT_ADAPTER_SNAPSHOT_INCOMPLETE",
                "a confirmed mark price is required for every covered instrument",
            )
        mark_price = _decimal(instrument_marks[0].get("mark_price"), "mark.mark_price")
        assert mark_price is not None
        settle = instrument.get("settle") or instrument.get("quote")
        if not isinstance(settle, str) or settle not in balances:
            raise DomainRejected(
                "FACT_ADAPTER_SNAPSHOT_INCOMPLETE",
                "a confirmed settlement balance is required for every covered instrument",
            )
        balance = balances[settle]
        equity = _decimal(balance.get("total"), "balance.total")
        available = _decimal(balance.get("free"), "balance.free")
        assert equity is not None and available is not None
        tick_size = _decimal(instrument.get("price_precision"), "instrument.price_precision")
        lot_size = _decimal(instrument.get("amount_precision"), "instrument.amount_precision")
        minimum_notional = _decimal(
            instrument.get("minimum_notional"),
            "instrument.minimum_notional",
        )
        if (
            tick_size is None
            or lot_size is None
            or minimum_notional is None
            or tick_size <= 0
            or lot_size <= 0
            or minimum_notional < 0
        ):
            raise DomainRejected(
                "FACT_ADAPTER_SNAPSHOT_INVALID",
                "normalized instrument limits are invalid",
            )
        normalized_orders = tuple(
            VenueOrder(
                order_id=str(row["order_id"]),
                client_order_id=str(row.get("client_order_id") or ""),
                status=_status(row.get("status")),
                side=str(row.get("side") or "").upper(),
                order_type=str(row.get("type") or "").upper(),
                ordered_quantity=Decimal(str(row["quantity"])),
                filled_quantity=Decimal(str(row["filled_quantity"])),
                stop_price=Decimal(str(row.get("trigger_price") or 0)),
                reduce_only=bool(row.get("reduce_only")),
                close_position=False,
                observed_at=_time(row.get("observed_at"), "order.observed_at"),
            )
            for row in orders.get(native, [])
        )
        protection_orders = tuple(
            row
            for row in normalized_orders
            if row.reduce_only and row.stop_price > 0 and row.status in {"SENT", "PARTIALLY_FILLED"}
        )
        if len(protection_orders) > 1:
            raise DomainRejected(
                "FACT_ADAPTER_PROTECTION_AMBIGUOUS",
                "multiple active protection orders require explicit lifecycle reconciliation",
            )
        normalized_fills = tuple(
            VenueFill(
                fill_id=str(row["fill_id"]),
                order_id=str(row.get("order_id") or ""),
                side=str(row.get("side") or "").upper(),
                quantity=Decimal(str(row["quantity"])),
                price=Decimal(str(row["price"])),
                fee=Decimal(str(row.get("fee") or 0)),
                fee_currency=str(row.get("fee_currency") or settle),
                executed_at=_time(row.get("executed_at"), "fill.executed_at"),
            )
            for row in fills.get(native, [])
        )
        normalized_funding = tuple(
            VenueFunding(
                payment_id=str(row["payment_id"]),
                amount=Decimal(str(row["amount"])),
                currency=str(row.get("currency") or settle),
                paid_at=_time(row.get("observed_at"), "funding.observed_at"),
            )
            for row in funding.get(native, [])
        )
        protection = protection_orders[0] if protection_orders else None
        results.append(
            VenueReadOnlySnapshot(
                symbol=native,
                observed_at=snapshot.observed_at,
                instrument=VenueInstrument(
                    symbol=native,
                    tick_size=tick_size,
                    lot_size=lot_size,
                    minimum_notional=minimum_notional,
                    quote_currency=str(instrument.get("quote") or settle),
                    collateral_currency=settle,
                    active=bool(instrument.get("active")),
                ),
                orders=normalized_orders,
                fills=normalized_fills,
                position=VenuePosition(
                    quantity=(
                        Decimal(0) if position is None else Decimal(str(position["quantity"]))
                    ),
                    average_entry_price=(
                        Decimal(0)
                        if position is None
                        else Decimal(str(position.get("entry_price") or 0))
                    ),
                    mark_price=mark_price,
                    observed_at=snapshot.observed_at,
                ),
                equity=VenueEquity(
                    equity=equity,
                    available_balance=available,
                    currency=settle,
                    observed_at=snapshot.observed_at,
                ),
                funding=normalized_funding,
                protection=(
                    None
                    if protection is None
                    else VenueProtection(
                        order_id=protection.order_id,
                        quantity=protection.ordered_quantity,
                        trigger_price=protection.stop_price,
                        observed_at=protection.observed_at,
                    )
                ),
                history_error_code=(
                    None
                    if not {"fetchMyTrades", "fetchFundingHistory"}.intersection(
                        snapshot.unknown_fields
                    )
                    else "FACT_ADAPTER_HISTORY_INCOMPLETE"
                ),
            )
        )
    return tuple(results)


__all__ = ["normalize_fact_adapter_snapshot"]
