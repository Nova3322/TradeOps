from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Any

from trading_control_plane.domain import RiskTier, TargetUrgency

ZERO = Decimal(0)
ONE = Decimal(1)


@dataclass(frozen=True, slots=True)
class PositionPolicySettings:
    maximum_position_notional: Decimal
    add_spacing_bps: int
    bollinger_midline_periods: int
    low_maximum_adds: int
    medium_maximum_adds: int
    high_maximum_adds: int
    low_maximum_loss_fraction: Decimal
    medium_maximum_loss_fraction: Decimal
    high_maximum_loss_fraction: Decimal

    def to_mapping(self) -> dict[str, str | int]:
        return {
            "maximum_position_notional": str(self.maximum_position_notional),
            "add_spacing_bps": self.add_spacing_bps,
            "bollinger_midline_periods": self.bollinger_midline_periods,
            "low_maximum_adds": self.low_maximum_adds,
            "medium_maximum_adds": self.medium_maximum_adds,
            "high_maximum_adds": self.high_maximum_adds,
            "low_maximum_loss_fraction": str(self.low_maximum_loss_fraction),
            "medium_maximum_loss_fraction": str(self.medium_maximum_loss_fraction),
            "high_maximum_loss_fraction": str(self.high_maximum_loss_fraction),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PositionPolicySettings:
        try:
            settings = cls(
                maximum_position_notional=Decimal(str(value["maximum_position_notional"])),
                add_spacing_bps=int(value["add_spacing_bps"]),
                bollinger_midline_periods=int(value["bollinger_midline_periods"]),
                low_maximum_adds=int(value["low_maximum_adds"]),
                medium_maximum_adds=int(value["medium_maximum_adds"]),
                high_maximum_adds=int(value["high_maximum_adds"]),
                low_maximum_loss_fraction=Decimal(
                    str(value["low_maximum_loss_fraction"])
                ),
                medium_maximum_loss_fraction=Decimal(
                    str(value["medium_maximum_loss_fraction"])
                ),
                high_maximum_loss_fraction=Decimal(
                    str(value["high_maximum_loss_fraction"])
                ),
            )
        except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("position policy is incomplete or invalid") from exc
        settings.validate()
        return settings

    def validate(self) -> None:
        fractions = (
            self.low_maximum_loss_fraction,
            self.medium_maximum_loss_fraction,
            self.high_maximum_loss_fraction,
        )
        if (
            not self.maximum_position_notional.is_finite()
            or self.maximum_position_notional <= ZERO
            or not 1 <= self.add_spacing_bps <= 10_000
            or not 2 <= self.bollinger_midline_periods <= 1_000
            or not 0 <= self.low_maximum_adds <= 1
            or not 0 <= self.medium_maximum_adds <= 2
            or not 0 <= self.high_maximum_adds <= 3
            or any(not item.is_finite() or item <= ZERO for item in fractions)
            or self.low_maximum_loss_fraction > Decimal("0.005")
            or self.medium_maximum_loss_fraction > Decimal("0.010")
            or self.high_maximum_loss_fraction > Decimal("0.015")
            or not (
                self.low_maximum_loss_fraction
                <= self.medium_maximum_loss_fraction
                <= self.high_maximum_loss_fraction
            )
        ):
            raise ValueError("position policy exceeds the supported safety boundary")


def position_policy_is_complete(value: Mapping[str, Any] | None) -> bool:
    if value is None:
        return False
    try:
        PositionPolicySettings.from_mapping(value)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class ProfitPyramidPolicy:
    target_leverage: Decimal
    maximum_adds: int
    profit_milestones: tuple[Decimal, ...]
    maximum_campaign_loss_fraction: Decimal

    @classmethod
    def for_risk_tier(
        cls,
        risk_tier: RiskTier,
        settings: PositionPolicySettings | None = None,
    ) -> ProfitPyramidPolicy:
        default = {
            RiskTier.LOW: cls(
                target_leverage=Decimal("3"),
                maximum_adds=1,
                profit_milestones=(Decimal("0.30"),),
                maximum_campaign_loss_fraction=Decimal("0.005"),
            ),
            RiskTier.MEDIUM: cls(
                target_leverage=Decimal("5"),
                maximum_adds=2,
                profit_milestones=(Decimal("0.30"), Decimal("0.50")),
                maximum_campaign_loss_fraction=Decimal("0.010"),
            ),
            RiskTier.HIGH: cls(
                target_leverage=Decimal("10"),
                maximum_adds=3,
                profit_milestones=(Decimal("0.30"), Decimal("0.50"), Decimal("1.00")),
                maximum_campaign_loss_fraction=Decimal("0.015"),
            ),
        }[risk_tier]
        if settings is None:
            return default
        maximum_adds = {
            RiskTier.LOW: settings.low_maximum_adds,
            RiskTier.MEDIUM: settings.medium_maximum_adds,
            RiskTier.HIGH: settings.high_maximum_adds,
        }[risk_tier]
        maximum_loss_fraction = {
            RiskTier.LOW: settings.low_maximum_loss_fraction,
            RiskTier.MEDIUM: settings.medium_maximum_loss_fraction,
            RiskTier.HIGH: settings.high_maximum_loss_fraction,
        }[risk_tier]
        return cls(
            target_leverage=default.target_leverage,
            maximum_adds=maximum_adds,
            profit_milestones=default.profit_milestones,
            maximum_campaign_loss_fraction=maximum_loss_fraction,
        )


@dataclass(frozen=True, slots=True)
class ProfitPyramidFacts:
    current_quantity: Decimal
    current_position_equity: Decimal
    executable_price: Decimal
    current_net_profit_ratio: Decimal
    highest_net_profit_ratio: Decimal
    used_adds: int
    authorized_adds: int
    pullback_confirmed: bool
    trend_valid: bool
    opened_with_auto_add_approval: bool
    global_auto_add_enabled: bool
    position_auto_add_enabled: bool
    stop_order_valid: bool
    stop_fully_covers_position: bool
    no_active_order: bool
    account_facts_current: bool
    position_facts_current: bool
    balance_facts_current: bool
    market_facts_current: bool
    system_allows_add: bool
    margin_sufficient: bool
    liquidation_distance_safe: bool
    liquidity_sufficient: bool
    slippage_within_limit: bool


@dataclass(frozen=True, slots=True)
class ProfitPyramidQuantityCaps:
    remaining_authorized_quantity: Decimal
    single_trade_risk_quantity: Decimal
    account_risk_quantity: Decimal
    portfolio_risk_quantity: Decimal
    campaign_loss_budget_quantity: Decimal
    margin_quantity: Decimal
    liquidation_quantity: Decimal
    liquidity_quantity: Decimal
    slippage_quantity: Decimal
    lot_size: Decimal
    minimum_order_quantity: Decimal
    minimum_order_notional: Decimal


class ProfitPyramidState(StrEnum):
    BLOCKED = "BLOCKED"
    WAITING_FOR_MILESTONE = "WAITING_FOR_MILESTONE"
    WAITING_FOR_PULLBACK = "WAITING_FOR_PULLBACK"
    AT_TARGET = "AT_TARGET"
    READY = "READY"


@dataclass(frozen=True, slots=True)
class ProfitPyramidDecision:
    state: ProfitPyramidState
    add_quantity: Decimal
    target_quantity: Decimal
    actual_leverage: Decimal
    milestone: Decimal | None
    reason: str


def _finite_nonnegative(value: Decimal) -> bool:
    return value.is_finite() and value >= ZERO


def _floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    return (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment


def campaign_loss_budget(total_capital: Decimal, risk_tier: RiskTier) -> Decimal:
    policy = ProfitPyramidPolicy.for_risk_tier(risk_tier)
    if not total_capital.is_finite() or total_capital <= ZERO:
        raise ValueError("total_capital must be finite and positive")
    return total_capital * policy.maximum_campaign_loss_fraction


def bollinger_midline(
    confirmed_closes: tuple[Decimal, ...],
    periods: int,
) -> Decimal:
    if periods < 2 or len(confirmed_closes) < periods:
        raise ValueError("insufficient confirmed closes for the configured reference period")
    window = confirmed_closes[-periods:]
    if any(not value.is_finite() or value <= ZERO for value in window):
        raise ValueError("confirmed close prices must be finite and positive")
    return sum(window, ZERO) / Decimal(periods)


def midline_pullback_confirmed(
    *,
    direction: str,
    executable_price: Decimal,
    middle_line: Decimal,
    previous_entry_or_add_price: Decimal,
    spacing_bps: int,
) -> bool:
    if (
        direction not in {"LONG", "SHORT"}
        or any(
            not value.is_finite() or value <= ZERO
            for value in (executable_price, middle_line, previous_entry_or_add_price)
        )
        or not 1 <= spacing_bps <= 10_000
    ):
        return False
    spacing = Decimal(spacing_bps) / Decimal(10_000)
    if direction == "LONG":
        return (
            executable_price <= middle_line
            and executable_price <= previous_entry_or_add_price * (ONE - spacing)
        )
    return (
        executable_price >= middle_line
        and executable_price >= previous_entry_or_add_price * (ONE + spacing)
    )


def calculate_profit_pyramid(
    *,
    risk_tier: RiskTier,
    facts: ProfitPyramidFacts,
    caps: ProfitPyramidQuantityCaps,
) -> ProfitPyramidDecision:
    """Return a bounded add recommendation without creating or sending an order."""

    policy = ProfitPyramidPolicy.for_risk_tier(risk_tier)
    numeric_facts = (
        facts.current_quantity,
        facts.current_position_equity,
        facts.executable_price,
        facts.current_net_profit_ratio,
        facts.highest_net_profit_ratio,
    )
    numeric_caps = (
        caps.remaining_authorized_quantity,
        caps.single_trade_risk_quantity,
        caps.account_risk_quantity,
        caps.portfolio_risk_quantity,
        caps.campaign_loss_budget_quantity,
        caps.margin_quantity,
        caps.liquidation_quantity,
        caps.liquidity_quantity,
        caps.slippage_quantity,
        caps.lot_size,
        caps.minimum_order_quantity,
        caps.minimum_order_notional,
    )
    if (
        any(not value.is_finite() for value in numeric_facts)
        or any(not _finite_nonnegative(value) for value in numeric_caps)
        or facts.current_quantity <= ZERO
        or facts.current_position_equity <= ZERO
        or facts.executable_price <= ZERO
        or caps.lot_size <= ZERO
        or caps.minimum_order_quantity <= ZERO
        or facts.used_adds < 0
        or facts.authorized_adds < 0
        or facts.used_adds > facts.authorized_adds
    ):
        return ProfitPyramidDecision(
            ProfitPyramidState.BLOCKED,
            ZERO,
            ZERO,
            ZERO,
            None,
            "INVALID_OR_INCOMPLETE_INPUT",
        )

    safety_checks = (
        (facts.opened_with_auto_add_approval, "OPENING_APPROVAL_MISSING"),
        (facts.global_auto_add_enabled, "GLOBAL_AUTO_ADD_DISABLED"),
        (facts.position_auto_add_enabled, "POSITION_AUTO_ADD_DISABLED"),
        (facts.system_allows_add, "SYSTEM_DISALLOWS_ADD"),
        (facts.trend_valid, "TREND_INVALID"),
        (facts.stop_order_valid, "STOP_ORDER_INVALID"),
        (facts.stop_fully_covers_position, "STOP_COVERAGE_INSUFFICIENT"),
        (facts.no_active_order, "ACTIVE_ORDER_EXISTS"),
        (facts.account_facts_current, "ACCOUNT_FACTS_NOT_CURRENT"),
        (facts.position_facts_current, "POSITION_FACTS_NOT_CURRENT"),
        (facts.balance_facts_current, "BALANCE_FACTS_NOT_CURRENT"),
        (facts.market_facts_current, "MARKET_FACTS_NOT_CURRENT"),
        (facts.margin_sufficient, "MARGIN_INSUFFICIENT"),
        (facts.liquidation_distance_safe, "LIQUIDATION_DISTANCE_UNSAFE"),
        (facts.liquidity_sufficient, "LIQUIDITY_INSUFFICIENT"),
        (facts.slippage_within_limit, "SLIPPAGE_LIMIT_EXCEEDED"),
    )
    failed = next((reason for passed, reason in safety_checks if not passed), None)
    if failed is not None:
        return ProfitPyramidDecision(
            ProfitPyramidState.BLOCKED,
            ZERO,
            ZERO,
            ZERO,
            None,
            failed,
        )
    if facts.current_net_profit_ratio <= ZERO:
        return ProfitPyramidDecision(
            ProfitPyramidState.BLOCKED,
            ZERO,
            ZERO,
            ZERO,
            None,
            "POSITION_NOT_PROFITABLE",
        )
    if facts.used_adds >= min(facts.authorized_adds, policy.maximum_adds):
        return ProfitPyramidDecision(
            ProfitPyramidState.BLOCKED,
            ZERO,
            ZERO,
            ZERO,
            None,
            "ADD_LIMIT_EXHAUSTED",
        )

    milestone = policy.profit_milestones[facts.used_adds]
    if facts.highest_net_profit_ratio < milestone:
        return ProfitPyramidDecision(
            ProfitPyramidState.WAITING_FOR_MILESTONE,
            ZERO,
            ZERO,
            ZERO,
            milestone,
            "PROFIT_MILESTONE_NOT_REACHED",
        )
    if facts.current_net_profit_ratio < milestone:
        return ProfitPyramidDecision(
            ProfitPyramidState.BLOCKED,
            ZERO,
            ZERO,
            ZERO,
            milestone,
            "PROFIT_FELL_BELOW_ARMED_MILESTONE",
        )
    if not facts.pullback_confirmed:
        return ProfitPyramidDecision(
            ProfitPyramidState.WAITING_FOR_PULLBACK,
            ZERO,
            ZERO,
            ZERO,
            milestone,
            "PULLBACK_NOT_CONFIRMED",
        )

    actual_leverage = (
        facts.current_quantity * facts.executable_price / facts.current_position_equity
    )
    target_quantity = (
        facts.current_position_equity * policy.target_leverage / facts.executable_price
    )
    desired_add = target_quantity - facts.current_quantity
    if desired_add <= ZERO or actual_leverage >= policy.target_leverage:
        return ProfitPyramidDecision(
            ProfitPyramidState.AT_TARGET,
            ZERO,
            target_quantity,
            actual_leverage,
            milestone,
            "TARGET_LEVERAGE_REACHED",
        )

    bounded_add = min(
        desired_add,
        caps.remaining_authorized_quantity,
        caps.single_trade_risk_quantity,
        caps.account_risk_quantity,
        caps.portfolio_risk_quantity,
        caps.campaign_loss_budget_quantity,
        caps.margin_quantity,
        caps.liquidation_quantity,
        caps.liquidity_quantity,
        caps.slippage_quantity,
    )
    bounded_add = _floor_to_increment(bounded_add, caps.lot_size)
    if (
        bounded_add < caps.minimum_order_quantity
        or bounded_add * facts.executable_price < caps.minimum_order_notional
    ):
        return ProfitPyramidDecision(
            ProfitPyramidState.BLOCKED,
            ZERO,
            target_quantity,
            actual_leverage,
            milestone,
            "BOUNDED_QUANTITY_BELOW_EXCHANGE_MINIMUM",
        )
    return ProfitPyramidDecision(
        ProfitPyramidState.READY,
        bounded_add,
        target_quantity,
        actual_leverage,
        milestone,
        "READY_AFTER_PROFIT_MILESTONE_AND_PULLBACK",
    )


class ProfitDrawdownAction(StrEnum):
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    EXIT = "EXIT"


@dataclass(frozen=True, slots=True)
class ProfitDrawdownPolicy:
    reduce_drawdown_fraction: Decimal
    severe_drawdown_fraction: Decimal
    reduce_target_fraction: Decimal
    severe_target_fraction: Decimal


@dataclass(frozen=True, slots=True)
class ProfitDrawdownDecision:
    action: ProfitDrawdownAction
    target_quantity: Decimal
    urgency: TargetUrgency
    drawdown_fraction: Decimal
    reason: str


def calculate_profit_drawdown_target(
    *,
    current_quantity: Decimal,
    highest_net_profit: Decimal,
    current_net_profit: Decimal,
    trend_valid: bool,
    facts_current: bool,
    lot_size: Decimal,
    policy: ProfitDrawdownPolicy,
) -> ProfitDrawdownDecision:
    """Return a target for the existing reduce-only Target arbitration path."""

    numeric = (
        current_quantity,
        highest_net_profit,
        current_net_profit,
        lot_size,
        policy.reduce_drawdown_fraction,
        policy.severe_drawdown_fraction,
        policy.reduce_target_fraction,
        policy.severe_target_fraction,
    )
    policy_valid = (
        all(value.is_finite() for value in numeric)
        and current_quantity >= ZERO
        and highest_net_profit >= ZERO
        and lot_size > ZERO
        and ZERO < policy.reduce_drawdown_fraction < policy.severe_drawdown_fraction <= ONE
        and ZERO <= policy.severe_target_fraction <= policy.reduce_target_fraction <= ONE
    )
    if not policy_valid or not facts_current:
        return ProfitDrawdownDecision(
            ProfitDrawdownAction.HOLD,
            current_quantity,
            TargetUrgency.IMMEDIATE,
            ZERO,
            "DRAWDOWN_FACTS_OR_POLICY_INVALID",
        )
    if not trend_valid:
        return ProfitDrawdownDecision(
            ProfitDrawdownAction.EXIT,
            ZERO,
            TargetUrgency.IMMEDIATE,
            ZERO,
            "TREND_INVALID",
        )
    if current_quantity == ZERO or highest_net_profit == ZERO:
        return ProfitDrawdownDecision(
            ProfitDrawdownAction.HOLD,
            current_quantity,
            TargetUrgency.NORMAL,
            ZERO,
            "NO_PROFIT_HIGH_WATERMARK",
        )

    drawdown = max(ZERO, (highest_net_profit - current_net_profit) / highest_net_profit)
    if drawdown >= policy.severe_drawdown_fraction:
        target = _floor_to_increment(
            current_quantity * policy.severe_target_fraction,
            lot_size,
        )
        action = ProfitDrawdownAction.EXIT if target == ZERO else ProfitDrawdownAction.REDUCE
        return ProfitDrawdownDecision(
            action,
            target,
            TargetUrgency.IMMEDIATE,
            drawdown,
            "SEVERE_PROFIT_DRAWDOWN",
        )
    if drawdown >= policy.reduce_drawdown_fraction:
        target = _floor_to_increment(
            current_quantity * policy.reduce_target_fraction,
            lot_size,
        )
        action = ProfitDrawdownAction.EXIT if target == ZERO else ProfitDrawdownAction.REDUCE
        return ProfitDrawdownDecision(
            action,
            target,
            TargetUrgency.URGENT,
            drawdown,
            "PROFIT_DRAWDOWN",
        )
    return ProfitDrawdownDecision(
        ProfitDrawdownAction.HOLD,
        current_quantity,
        TargetUrgency.NORMAL,
        drawdown,
        "DRAWDOWN_BELOW_THRESHOLD",
    )
