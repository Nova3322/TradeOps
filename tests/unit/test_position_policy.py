from decimal import Decimal

import pytest

from trading_control_plane.domain import RiskTier, TargetUrgency
from trading_control_plane.position_policy import (
    PositionPolicySettings,
    ProfitDrawdownAction,
    ProfitDrawdownPolicy,
    ProfitPyramidFacts,
    ProfitPyramidQuantityCaps,
    ProfitPyramidState,
    bollinger_midline,
    calculate_profit_drawdown_target,
    calculate_profit_pyramid,
    campaign_loss_budget,
    midline_pullback_confirmed,
    position_policy_is_complete,
)


def ready_facts(**overrides: object) -> ProfitPyramidFacts:
    values: dict[str, object] = {
        "current_quantity": Decimal("1"),
        "current_position_equity": Decimal("30"),
        "executable_price": Decimal("100"),
        "current_net_profit_ratio": Decimal("0.35"),
        "highest_net_profit_ratio": Decimal("0.40"),
        "used_adds": 0,
        "authorized_adds": 3,
        "pullback_confirmed": True,
        "trend_valid": True,
        "opened_with_auto_add_approval": True,
        "global_auto_add_enabled": True,
        "position_auto_add_enabled": True,
        "stop_order_valid": True,
        "stop_fully_covers_position": True,
        "no_active_order": True,
        "account_facts_current": True,
        "position_facts_current": True,
        "balance_facts_current": True,
        "market_facts_current": True,
        "system_allows_add": True,
        "margin_sufficient": True,
        "liquidation_distance_safe": True,
        "liquidity_sufficient": True,
        "slippage_within_limit": True,
    }
    values.update(overrides)
    return ProfitPyramidFacts(**values)  # type: ignore[arg-type]


def generous_caps(**overrides: Decimal) -> ProfitPyramidQuantityCaps:
    values = {
        "remaining_authorized_quantity": Decimal("100"),
        "single_trade_risk_quantity": Decimal("100"),
        "account_risk_quantity": Decimal("100"),
        "portfolio_risk_quantity": Decimal("100"),
        "campaign_loss_budget_quantity": Decimal("100"),
        "margin_quantity": Decimal("100"),
        "liquidation_quantity": Decimal("100"),
        "liquidity_quantity": Decimal("100"),
        "slippage_quantity": Decimal("100"),
        "lot_size": Decimal("0.01"),
        "minimum_order_quantity": Decimal("0.01"),
        "minimum_order_notional": Decimal("1"),
    }
    values.update(overrides)
    return ProfitPyramidQuantityCaps(**values)


@pytest.mark.parametrize(
    ("risk_tier", "capital", "expected"),
    [
        (RiskTier.LOW, "10000", "50.000"),
        (RiskTier.MEDIUM, "10000", "100.000"),
        (RiskTier.HIGH, "10000", "150.000"),
    ],
)
def test_campaign_loss_budget_is_one_campaign_total(
    risk_tier: RiskTier,
    capital: str,
    expected: str,
) -> None:
    assert campaign_loss_budget(Decimal(capital), risk_tier) == Decimal(expected)


def test_position_policy_settings_are_complete_and_may_only_tighten_tier_caps() -> None:
    settings = PositionPolicySettings(
        maximum_position_notional=Decimal("10000"),
        add_spacing_bps=150,
        bollinger_midline_periods=20,
        low_maximum_adds=1,
        medium_maximum_adds=1,
        high_maximum_adds=2,
        low_maximum_loss_fraction=Decimal("0.004"),
        medium_maximum_loss_fraction=Decimal("0.008"),
        high_maximum_loss_fraction=Decimal("0.012"),
    )

    mapping = settings.to_mapping()
    assert PositionPolicySettings.from_mapping(mapping) == settings
    assert position_policy_is_complete(mapping)
    assert not position_policy_is_complete({})

    with pytest.raises(ValueError, match="safety boundary"):
        PositionPolicySettings.from_mapping(
            {**mapping, "high_maximum_loss_fraction": "0.016"}
        )


def test_bollinger_midline_uses_only_configured_confirmed_window() -> None:
    assert bollinger_midline(
        (Decimal("80"), Decimal("90"), Decimal("100"), Decimal("110")),
        3,
    ) == Decimal("100")
    with pytest.raises(ValueError, match="insufficient"):
        bollinger_midline((Decimal("100"),), 20)


def test_pullback_requires_middle_line_and_configured_spacing() -> None:
    assert midline_pullback_confirmed(
        direction="LONG",
        executable_price=Decimal("98"),
        middle_line=Decimal("99"),
        previous_entry_or_add_price=Decimal("100"),
        spacing_bps=150,
    )
    assert not midline_pullback_confirmed(
        direction="LONG",
        executable_price=Decimal("99"),
        middle_line=Decimal("99"),
        previous_entry_or_add_price=Decimal("100"),
        spacing_bps=150,
    )
    assert midline_pullback_confirmed(
        direction="SHORT",
        executable_price=Decimal("102"),
        middle_line=Decimal("101"),
        previous_entry_or_add_price=Decimal("100"),
        spacing_bps=150,
    )


def test_ready_add_targets_risk_tier_leverage_and_only_scales_down() -> None:
    decision = calculate_profit_pyramid(
        risk_tier=RiskTier.MEDIUM,
        facts=ready_facts(),
        caps=generous_caps(liquidity_quantity=Decimal("0.20")),
    )

    assert decision.state is ProfitPyramidState.READY
    assert decision.target_quantity == Decimal("1.5")
    assert decision.add_quantity == Decimal("0.20")
    assert decision.actual_leverage == Decimal("3.333333333333333333333333333")
    assert decision.milestone == Decimal("0.30")


@pytest.mark.parametrize(
    ("overrides", "state", "reason"),
    [
        (
            {"current_net_profit_ratio": Decimal("-0.01")},
            ProfitPyramidState.BLOCKED,
            "POSITION_NOT_PROFITABLE",
        ),
        (
            {"highest_net_profit_ratio": Decimal("0.29")},
            ProfitPyramidState.WAITING_FOR_MILESTONE,
            "PROFIT_MILESTONE_NOT_REACHED",
        ),
        (
            {
                "highest_net_profit_ratio": Decimal("0.60"),
                "current_net_profit_ratio": Decimal("0.29"),
            },
            ProfitPyramidState.BLOCKED,
            "PROFIT_FELL_BELOW_ARMED_MILESTONE",
        ),
        (
            {"pullback_confirmed": False},
            ProfitPyramidState.WAITING_FOR_PULLBACK,
            "PULLBACK_NOT_CONFIRMED",
        ),
        (
            {"stop_fully_covers_position": False},
            ProfitPyramidState.BLOCKED,
            "STOP_COVERAGE_INSUFFICIENT",
        ),
        (
            {"no_active_order": False},
            ProfitPyramidState.BLOCKED,
            "ACTIVE_ORDER_EXISTS",
        ),
        (
            {"market_facts_current": False},
            ProfitPyramidState.BLOCKED,
            "MARKET_FACTS_NOT_CURRENT",
        ),
        (
            {"system_allows_add": False},
            ProfitPyramidState.BLOCKED,
            "SYSTEM_DISALLOWS_ADD",
        ),
    ],
)
def test_any_failed_condition_prevents_add(
    overrides: dict[str, object],
    state: ProfitPyramidState,
    reason: str,
) -> None:
    decision = calculate_profit_pyramid(
        risk_tier=RiskTier.HIGH,
        facts=ready_facts(**overrides),
        caps=generous_caps(),
    )

    assert decision.state is state
    assert decision.add_quantity == 0
    assert decision.reason == reason


def test_each_add_uses_next_profit_milestone() -> None:
    second = calculate_profit_pyramid(
        risk_tier=RiskTier.MEDIUM,
        facts=ready_facts(
            used_adds=1,
            authorized_adds=2,
            current_net_profit_ratio=Decimal("0.51"),
            highest_net_profit_ratio=Decimal("0.60"),
        ),
        caps=generous_caps(),
    )
    exhausted = calculate_profit_pyramid(
        risk_tier=RiskTier.MEDIUM,
        facts=ready_facts(
            used_adds=2,
            authorized_adds=2,
            current_net_profit_ratio=Decimal("1.20"),
            highest_net_profit_ratio=Decimal("1.20"),
        ),
        caps=generous_caps(),
    )

    assert second.milestone == Decimal("0.50")
    assert second.state is ProfitPyramidState.READY
    assert exhausted.state is ProfitPyramidState.BLOCKED
    assert exhausted.reason == "ADD_LIMIT_EXHAUSTED"


def test_target_reached_never_adds() -> None:
    decision = calculate_profit_pyramid(
        risk_tier=RiskTier.LOW,
        facts=ready_facts(current_position_equity=Decimal("20")),
        caps=generous_caps(),
    )

    assert decision.actual_leverage == Decimal("5")
    assert decision.state is ProfitPyramidState.AT_TARGET
    assert decision.add_quantity == 0


def test_exchange_minimum_blocks_too_small_bounded_quantity() -> None:
    decision = calculate_profit_pyramid(
        risk_tier=RiskTier.HIGH,
        facts=ready_facts(),
        caps=generous_caps(
            remaining_authorized_quantity=Decimal("0.004"),
            lot_size=Decimal("0.001"),
            minimum_order_quantity=Decimal("0.001"),
            minimum_order_notional=Decimal("1"),
        ),
    )

    assert decision.state is ProfitPyramidState.BLOCKED
    assert decision.reason == "BOUNDED_QUANTITY_BELOW_EXCHANGE_MINIMUM"


def test_drawdown_uses_existing_target_shape_without_execution() -> None:
    policy = ProfitDrawdownPolicy(
        reduce_drawdown_fraction=Decimal("0.20"),
        severe_drawdown_fraction=Decimal("0.50"),
        reduce_target_fraction=Decimal("0.70"),
        severe_target_fraction=Decimal("0.30"),
    )

    ordinary = calculate_profit_drawdown_target(
        current_quantity=Decimal("10"),
        highest_net_profit=Decimal("100"),
        current_net_profit=Decimal("75"),
        trend_valid=True,
        facts_current=True,
        lot_size=Decimal("0.1"),
        policy=policy,
    )
    severe = calculate_profit_drawdown_target(
        current_quantity=Decimal("10"),
        highest_net_profit=Decimal("100"),
        current_net_profit=Decimal("40"),
        trend_valid=True,
        facts_current=True,
        lot_size=Decimal("0.1"),
        policy=policy,
    )
    invalidated = calculate_profit_drawdown_target(
        current_quantity=Decimal("10"),
        highest_net_profit=Decimal("100"),
        current_net_profit=Decimal("90"),
        trend_valid=False,
        facts_current=True,
        lot_size=Decimal("0.1"),
        policy=policy,
    )

    assert ordinary == ordinary.__class__(
        ProfitDrawdownAction.REDUCE,
        Decimal("7.0"),
        TargetUrgency.URGENT,
        Decimal("0.25"),
        "PROFIT_DRAWDOWN",
    )
    assert severe.action is ProfitDrawdownAction.REDUCE
    assert severe.target_quantity == Decimal("3.0")
    assert severe.urgency is TargetUrgency.IMMEDIATE
    assert invalidated.action is ProfitDrawdownAction.EXIT
    assert invalidated.target_quantity == 0
    assert invalidated.urgency is TargetUrgency.IMMEDIATE


def test_drawdown_fails_closed_without_current_facts() -> None:
    decision = calculate_profit_drawdown_target(
        current_quantity=Decimal("10"),
        highest_net_profit=Decimal("100"),
        current_net_profit=Decimal("50"),
        trend_valid=True,
        facts_current=False,
        lot_size=Decimal("0.1"),
        policy=ProfitDrawdownPolicy(
            reduce_drawdown_fraction=Decimal("0.20"),
            severe_drawdown_fraction=Decimal("0.50"),
            reduce_target_fraction=Decimal("0.70"),
            severe_target_fraction=Decimal("0.30"),
        ),
    )

    assert decision.action is ProfitDrawdownAction.HOLD
    assert decision.target_quantity == Decimal("10")
    assert decision.reason == "DRAWDOWN_FACTS_OR_POLICY_INVALID"
