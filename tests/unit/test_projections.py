from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from trading_control_plane.projections import (
    CurrentPositionDirection,
    CurrentPositionProjection,
    CurrentPositionScope,
    CurrentPositionState,
    CurrentProtectedPositionRiskProjection,
    CurrentProtectionDirection,
    CurrentProtectionProjection,
    CurrentProtectionScope,
    CurrentProtectionState,
    ProjectionFreshness,
    ProjectionMaturity,
    ProjectionState,
    derive_protected_position_risk,
)

POSITION_SNAPSHOT_ID = UUID("00000000-0000-0000-0000-000000000101")
PROTECTION_SNAPSHOT_ID = UUID("00000000-0000-0000-0000-000000000201")
FACT_TIME = datetime(2026, 7, 18, 6, 0, tzinfo=UTC)


def _scope_values() -> dict[str, str]:
    return {
        "organization_id": "org-1",
        "venue": "BINANCE",
        "execution_domain": "BINANCE_USDM",
        "account_id": "account-1",
        "instrument_id": "BTCUSDT-PERP",
        "position_mode": "ONE_WAY",
        "position_side": "BOTH",
        "margin_mode": "ISOLATED",
        "collateral_pool_id": "pool-usdt-1",
        "settlement_currency": "USDT",
    }


def _position(
    *,
    direction: CurrentPositionDirection,
    entry_price: str,
    mark_price: str,
    quantity: str = "2",
    contract_multiplier: str = "1",
    unrealized_pnl: str = "0",
) -> CurrentPositionProjection:
    return CurrentPositionProjection(
        scope=CurrentPositionScope(**_scope_values()),
        projection_state=ProjectionState.CONFIRMED,
        freshness=ProjectionFreshness.FRESH,
        maturity=ProjectionMaturity.VENUE_CONFIRMED,
        reason_code=None,
        source_snapshot_id=POSITION_SNAPSHOT_ID,
        source_snapshot_hash="position-hash",
        source_version="binance-position-v1",
        normalization_version="venue-position-v1",
        position_state=CurrentPositionState.OPEN,
        direction=direction,
        quantity=Decimal(quantity),
        entry_price=Decimal(entry_price),
        mark_price=Decimal(mark_price),
        contract_multiplier=Decimal(contract_multiplier),
        notional=Decimal("200"),
        unrealized_pnl=Decimal(unrealized_pnl),
        liquidation_price=None,
        leverage=Decimal("3"),
        initial_margin=Decimal("100"),
        maintenance_margin=Decimal("10"),
        facts_as_of=FACT_TIME,
        venue_observed_at=FACT_TIME,
        received_at=FACT_TIME,
        age_ms=0,
        max_event_candidate_count=1,
        projection_version="venue-current-v1",
    )


def _protection(
    *,
    direction: CurrentProtectionDirection,
    trigger_price: str,
    quantity: str = "2",
    position_snapshot_id: UUID = POSITION_SNAPSHOT_ID,
    settlement_currency: str = "USDT",
) -> CurrentProtectionProjection:
    scope_values = _scope_values()
    scope_values["settlement_currency"] = settlement_currency
    return CurrentProtectionProjection(
        scope=CurrentProtectionScope(**scope_values),
        projection_state=ProjectionState.CONFIRMED,
        freshness=ProjectionFreshness.FRESH,
        maturity=ProjectionMaturity.VENUE_CONFIRMED,
        reason_code=None,
        source_snapshot_id=PROTECTION_SNAPSHOT_ID,
        source_snapshot_hash="protection-hash",
        source_version="binance-protection-v2",
        normalization_version="venue-protection-v2",
        source_position_snapshot_id=position_snapshot_id,
        protection_state=CurrentProtectionState.CONFIRMED,
        protected_direction=direction,
        position_quantity=Decimal(quantity),
        covered_quantity=Decimal(quantity),
        uncovered_quantity=Decimal("0"),
        active_stop_order_count=1,
        worst_active_trigger_price=Decimal(trigger_price),
        venue_native=True,
        reduce_only_confirmed=True,
        replacement_in_progress=False,
        order_set_hash="order-set-hash",
        facts_as_of=FACT_TIME + timedelta(milliseconds=1),
        venue_observed_at=FACT_TIME,
        received_at=FACT_TIME,
        age_ms=0,
        max_event_candidate_count=1,
        projection_version="venue-current-v1",
    )


@pytest.mark.parametrize(
    (
        "position_direction",
        "protection_direction",
        "entry_price",
        "mark_price",
        "trigger_price",
        "expected_total",
        "expected_open_heat",
        "expected_giveback",
    ),
    [
        ("LONG", "LONG", "100", "120", "90", "60", "20", "40"),
        ("LONG", "LONG", "100", "120", "110", "20", "0", "20"),
        ("LONG", "LONG", "100", "95", "90", "10", "10", "0"),
        ("SHORT", "SHORT", "100", "80", "110", "60", "20", "40"),
        ("SHORT", "SHORT", "100", "80", "90", "20", "0", "20"),
        ("SHORT", "SHORT", "100", "105", "110", "10", "10", "0"),
    ],
)
def test_protected_position_risk_is_mutually_exclusive(
    position_direction: str,
    protection_direction: str,
    entry_price: str,
    mark_price: str,
    trigger_price: str,
    expected_total: str,
    expected_open_heat: str,
    expected_giveback: str,
) -> None:
    position = _position(
        direction=CurrentPositionDirection(position_direction),
        entry_price=entry_price,
        mark_price=mark_price,
    )
    protection = _protection(
        direction=CurrentProtectionDirection(protection_direction),
        trigger_price=trigger_price,
    )

    result = derive_protected_position_risk(position, protection)

    assert result.projection_state is ProjectionState.CONFIRMED
    assert result.scope.settlement_currency == "USDT"
    assert result.current_to_protection_loss == Decimal(expected_total)
    assert result.open_heat == Decimal(expected_open_heat)
    assert result.protected_profit_giveback == Decimal(expected_giveback)
    assert result.unrealized_pnl == position.unrealized_pnl
    assert result.current_to_protection_loss == (
        result.open_heat + result.protected_profit_giveback
    )
    assert result.facts_as_of == position.facts_as_of
    assert result.calculation_hash is not None
    assert result.calculation_version == "protected-position-risk-v2"


def test_protected_position_risk_hash_binds_canonical_unrealized_pnl() -> None:
    protection = _protection(
        direction=CurrentProtectionDirection.LONG,
        trigger_price="110",
    )
    positive = derive_protected_position_risk(
        _position(
            direction=CurrentPositionDirection.LONG,
            entry_price="100",
            mark_price="120",
            unrealized_pnl="40",
        ),
        protection,
    )
    negative = derive_protected_position_risk(
        _position(
            direction=CurrentPositionDirection.LONG,
            entry_price="100",
            mark_price="120",
            unrealized_pnl="-1",
        ),
        protection,
    )

    assert positive.unrealized_pnl == Decimal("40")
    assert negative.unrealized_pnl == Decimal("-1")
    assert positive.calculation_hash != negative.calculation_hash


def test_protected_position_risk_rounds_total_up_and_uses_exact_residual() -> None:
    position = _position(
        direction=CurrentPositionDirection.LONG,
        entry_price="100",
        mark_price="100.1",
        quantity="0.000000000000000001",
    )
    protection = _protection(
        direction=CurrentProtectionDirection.LONG,
        trigger_price="100",
        quantity="0.000000000000000001",
    )

    result = derive_protected_position_risk(position, protection)

    assert result.current_to_protection_loss == Decimal("0.000000000000000001")
    assert result.open_heat == 0
    assert result.protected_profit_giveback == Decimal("0.000000000000000001")
    assert result.current_to_protection_loss == (
        result.open_heat + result.protected_profit_giveback
    )


@pytest.mark.parametrize(
    ("protection", "expected_reason"),
    [
        (
            _protection(
                direction=CurrentProtectionDirection.LONG,
                trigger_price="90",
                position_snapshot_id=UUID("00000000-0000-0000-0000-000000000999"),
            ),
            "SOURCE_BINDING_MISMATCH",
        ),
        (
            _protection(
                direction=CurrentProtectionDirection.LONG,
                trigger_price="90",
                settlement_currency="USDC",
            ),
            "SCOPE_MISMATCH",
        ),
    ],
)
def test_protected_position_risk_mismatch_hides_all_economics(
    protection: CurrentProtectionProjection,
    expected_reason: str,
) -> None:
    result = derive_protected_position_risk(
        _position(
            direction=CurrentPositionDirection.LONG,
            entry_price="100",
            mark_price="120",
        ),
        protection,
    )

    assert result.projection_state is ProjectionState.UNKNOWN
    assert result.reason_code == expected_reason
    assert result.direction is CurrentProtectionDirection.UNKNOWN
    assert result.position_snapshot_id is None
    assert result.protection_snapshot_id is None
    assert result.current_to_protection_loss is None
    assert result.open_heat is None
    assert result.protected_profit_giveback is None
    assert result.calculation_hash is None


def test_protected_position_risk_rejects_invalid_position_economics() -> None:
    result = derive_protected_position_risk(
        _position(
            direction=CurrentPositionDirection.LONG,
            entry_price="100",
            mark_price="0",
        ),
        _protection(
            direction=CurrentProtectionDirection.LONG,
            trigger_price="90",
        ),
    )

    assert result.projection_state is ProjectionState.UNKNOWN
    assert result.reason_code == "ECONOMICS_INVALID"
    assert result.current_to_protection_loss is None


@pytest.mark.parametrize(
    ("protection", "expected_reason"),
    [
        (
            _protection(
                direction=CurrentProtectionDirection.SHORT,
                trigger_price="130",
            ),
            "DIRECTION_MISMATCH",
        ),
        (
            _protection(
                direction=CurrentProtectionDirection.LONG,
                trigger_price="90",
                quantity="3",
            ),
            "QUANTITY_MISMATCH",
        ),
        (
            _protection(
                direction=CurrentProtectionDirection.LONG,
                trigger_price="130",
            ),
            "TRIGGER_SIDE_INVALID",
        ),
    ],
)
def test_protected_position_risk_rejects_conflicting_confirmed_inputs(
    protection: CurrentProtectionProjection,
    expected_reason: str,
) -> None:
    result = derive_protected_position_risk(
        _position(
            direction=CurrentPositionDirection.LONG,
            entry_price="100",
            mark_price="120",
        ),
        protection,
    )

    assert result.projection_state is ProjectionState.UNKNOWN
    assert result.reason_code == expected_reason
    assert result.current_to_protection_loss is None


def test_protected_position_risk_rejects_tampered_calculation_hash() -> None:
    result = derive_protected_position_risk(
        _position(
            direction=CurrentPositionDirection.LONG,
            entry_price="100",
            mark_price="120",
        ),
        _protection(
            direction=CurrentProtectionDirection.LONG,
            trigger_price="90",
        ),
    )
    payload = result.model_dump(mode="python")
    payload["calculation_hash"] = "0" * 64

    with pytest.raises(ValidationError, match="confirmed protected-position risk is inconsistent"):
        CurrentProtectedPositionRiskProjection.model_validate(payload)
