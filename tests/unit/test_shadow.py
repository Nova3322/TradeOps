from decimal import Decimal

import pytest

from trading_control_plane.domain import DomainRejected
from trading_control_plane.shadow import apply_shadow_fill, quote_shadow_execution


def test_shadow_quote_applies_adverse_tick_rounding_and_explicit_costs() -> None:
    buy = quote_shadow_execution(
        side="BUY",
        quantity=Decimal("2"),
        reference_price=Decimal("100"),
        tick_size=Decimal("0.1"),
        contract_multiplier=Decimal("1"),
        fee_bps=Decimal("4"),
        slippage_bps=Decimal("2"),
    )
    sell = quote_shadow_execution(
        side="SELL",
        quantity=Decimal("2"),
        reference_price=Decimal("100"),
        tick_size=Decimal("0.1"),
        contract_multiplier=Decimal("1"),
        fee_bps=Decimal("4"),
        slippage_bps=Decimal("2"),
    )

    assert buy.fill_price == Decimal("100.1")
    assert buy.fee == Decimal("0.080080000000000000")
    assert buy.slippage_cost == Decimal("0.200000000000000000")
    assert sell.fill_price == Decimal("99.9")
    assert sell.fee == Decimal("0.079920000000000000")
    assert sell.slippage_cost == Decimal("0.200000000000000000")


@pytest.mark.parametrize(
    "overrides",
    [
        {"side": "HOLD"},
        {"quantity": Decimal("0")},
        {"reference_price": Decimal("NaN")},
        {"tick_size": Decimal("-1")},
        {"fee_bps": Decimal("101")},
        {"slippage_bps": Decimal("501")},
    ],
)
def test_shadow_quote_rejects_invalid_identity_or_cost_model(
    overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "side": "BUY",
        "quantity": Decimal("1"),
        "reference_price": Decimal("100"),
        "tick_size": Decimal("0.1"),
        "contract_multiplier": Decimal("1"),
        "fee_bps": Decimal("4"),
        "slippage_bps": Decimal("2"),
    }
    values.update(overrides)

    with pytest.raises(DomainRejected):
        quote_shadow_execution(**values)  # type: ignore[arg-type]


def test_shadow_position_tracks_increase_reduce_flat_and_reversal() -> None:
    opened = apply_shadow_fill(
        current_quantity=Decimal("0"),
        current_average_entry_price=Decimal("0"),
        side="BUY",
        fill_quantity=Decimal("2"),
        fill_price=Decimal("100"),
        reduce_only=False,
    )
    increased = apply_shadow_fill(
        current_quantity=opened.quantity,
        current_average_entry_price=opened.average_entry_price,
        side="BUY",
        fill_quantity=Decimal("1"),
        fill_price=Decimal("110"),
        reduce_only=False,
    )
    reduced = apply_shadow_fill(
        current_quantity=increased.quantity,
        current_average_entry_price=increased.average_entry_price,
        side="SELL",
        fill_quantity=Decimal("1"),
        fill_price=Decimal("105"),
        reduce_only=True,
    )
    flat = apply_shadow_fill(
        current_quantity=reduced.quantity,
        current_average_entry_price=reduced.average_entry_price,
        side="SELL",
        fill_quantity=Decimal("2"),
        fill_price=Decimal("90"),
        reduce_only=True,
    )
    reversed_position = apply_shadow_fill(
        current_quantity=Decimal("1"),
        current_average_entry_price=Decimal("100"),
        side="SELL",
        fill_quantity=Decimal("2"),
        fill_price=Decimal("90"),
        reduce_only=False,
    )

    assert increased.quantity == Decimal("3")
    assert increased.average_entry_price == Decimal("103.333333333333333333")
    assert reduced.quantity == Decimal("2")
    assert reduced.average_entry_price == increased.average_entry_price
    assert flat.quantity == 0
    assert flat.average_entry_price == 0
    assert reversed_position.quantity == Decimal("-1")
    assert reversed_position.average_entry_price == Decimal("90.000000000000000000")


@pytest.mark.parametrize(
    ("side", "quantity"),
    [("BUY", "1"), ("SELL", "3")],
)
def test_shadow_reduce_only_cannot_increase_or_reverse(side: str, quantity: str) -> None:
    with pytest.raises(DomainRejected, match="SHADOW_REDUCE_ONLY_VIOLATION"):
        apply_shadow_fill(
            current_quantity=Decimal("2"),
            current_average_entry_price=Decimal("100"),
            side=side,
            fill_quantity=Decimal(quantity),
            fill_price=Decimal("100"),
            reduce_only=True,
        )
