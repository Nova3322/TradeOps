from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_control_plane.domain import DomainRejected, RiskTier
from trading_control_plane.models import Instrument
from trading_control_plane.service_domains.proposals import resolve_proposal_terms


def instrument(
    *,
    lot_size: str,
    tick_size: str = "0.1",
    minimum_notional: str = "5",
    contract_multiplier: str = "1",
) -> Instrument:
    return Instrument(
        venue="BINANCE",
        symbol="MIRAUSDT",
        tick_size=Decimal(tick_size),
        lot_size=Decimal(lot_size),
        minimum_notional=Decimal(minimum_notional),
        contract_multiplier=Decimal(contract_multiplier),
        quote_currency="USDT",
        collateral_currency="USDT",
        active=True,
        protection_supported=True,
        updated_at=datetime.now(UTC),
    )


@pytest.mark.parametrize(
    ("lot_size", "raw_quantity", "expected_quantity"),
    (
        ("1", "2.9", Decimal("2")),
        ("0.001", "0.0019", Decimal("0.001")),
    ),
)
def test_catalog_resolution_floors_integer_and_decimal_lot_sizes(
    lot_size: str,
    raw_quantity: str,
    expected_quantity: Decimal,
) -> None:
    quantity, leverage, risk, details = resolve_proposal_terms(
        instrument=instrument(lot_size=lot_size),
        risk_tier=RiskTier.HIGH,
        raw_quantity=Decimal(raw_quantity),
        max_risk=Decimal("10"),
        details={"trigger_price": "10000.09", "invalidation_price": "9999.09"},
    )

    assert quantity == expected_quantity
    assert leverage == Decimal(10)
    assert Decimal(details["trigger_price"]) == Decimal("10000.0")
    assert Decimal(details["invalidation_price"]) == Decimal("9999.0")
    assert Decimal(details["resolved_notional"]) == quantity * Decimal("10000")
    assert risk == quantity
    assert Decimal(details["resolved_risk"]) == risk


def test_catalog_resolution_uses_contract_multiplier_and_low_tier_leverage() -> None:
    quantity, leverage, risk, details = resolve_proposal_terms(
        instrument=instrument(lot_size="1", contract_multiplier="0.01"),
        risk_tier=RiskTier.LOW,
        raw_quantity=Decimal("12.9"),
        max_risk=Decimal("2"),
        details={"trigger_price": "100.09", "invalidation_price": "99.09"},
    )

    assert quantity == Decimal(12)
    assert leverage == Decimal(3)
    assert Decimal(details["resolved_notional"]) == Decimal(12)
    assert risk == Decimal("0.12")


@pytest.mark.parametrize(
    ("raw_quantity", "minimum_notional", "error_code"),
    (
        ("0.9", "5", "INSTRUMENT_QUANTITY_ROUNDED_TO_ZERO"),
        ("1.9", "200", "MINIMUM_NOTIONAL"),
    ),
)
def test_catalog_resolution_rejects_zero_or_below_minimum_notional(
    raw_quantity: str,
    minimum_notional: str,
    error_code: str,
) -> None:
    with pytest.raises(DomainRejected, match=error_code):
        resolve_proposal_terms(
            instrument=instrument(lot_size="1", minimum_notional=minimum_notional),
            risk_tier=RiskTier.MEDIUM,
            raw_quantity=Decimal(raw_quantity),
            max_risk=Decimal("10"),
            details={"trigger_price": "100"},
        )
