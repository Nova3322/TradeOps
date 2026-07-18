from datetime import UTC, datetime

import pytest

from trading_control_plane.campaign_protection_exit import _protection_failure_reason
from trading_control_plane.projections import (
    CurrentProtectionDirection,
    CurrentProtectionProjection,
    CurrentProtectionScope,
    CurrentProtectionState,
    ProjectionFreshness,
    ProjectionMaturity,
    ProjectionState,
)


def _unknown_protection(reason_code: str | None) -> CurrentProtectionProjection:
    return CurrentProtectionProjection(
        scope=CurrentProtectionScope(
            organization_id="org-1",
            venue="BINANCE",
            execution_domain="BINANCE_USDM",
            account_id="account-1",
            instrument_id="BTCUSDT-PERP",
            position_mode="ONE_WAY",
            position_side="BOTH",
            margin_mode="ISOLATED",
            collateral_pool_id="pool-usdt-1",
            settlement_currency="USDT",
        ),
        projection_state=ProjectionState.UNKNOWN,
        freshness=ProjectionFreshness.UNKNOWN,
        maturity=ProjectionMaturity.UNKNOWN,
        reason_code=reason_code,
        source_snapshot_id=None,
        source_snapshot_hash=None,
        source_version=None,
        normalization_version=None,
        source_position_snapshot_id=None,
        protection_state=CurrentProtectionState.UNKNOWN,
        protected_direction=CurrentProtectionDirection.UNKNOWN,
        position_quantity=None,
        covered_quantity=None,
        uncovered_quantity=None,
        active_stop_order_count=None,
        worst_active_trigger_price=None,
        venue_native=False,
        reduce_only_confirmed=False,
        replacement_in_progress=False,
        order_set_hash=None,
        facts_as_of=datetime.now(UTC),
        venue_observed_at=None,
        received_at=None,
        age_ms=0,
        max_event_candidate_count=0,
        projection_version="venue-current-v1",
    )


@pytest.mark.parametrize(
    ("source_reason", "failure_reason"),
    [
        ("SOURCE_MISSING", "PROTECTION_MISSING"),
        ("SOURCE_STALE", "PROTECTION_STALE"),
        ("SOURCE_FROM_FUTURE", "PROTECTION_FROM_FUTURE"),
        ("SOURCE_CONFLICT", "PROTECTION_UNKNOWN"),
        (None, "PROTECTION_UNKNOWN"),
    ],
)
def test_protection_failure_reason_is_bounded(
    source_reason: str | None,
    failure_reason: str,
) -> None:
    assert _protection_failure_reason(_unknown_protection(source_reason)) == failure_reason
