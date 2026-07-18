from datetime import timedelta
from decimal import Decimal

import pytest

from trading_control_plane.domain import (
    DomainRejected,
    EconomicFill,
    IntentKind,
    RiskEvaluationInput,
    RiskPolicyInput,
    RiskResult,
    SystemRiskState,
    TargetCandidate,
    TargetUrgency,
    compute_pnl,
    evaluate_risk,
    select_target_position,
)


def policy(**overrides: object) -> RiskPolicyInput:
    values: dict[str, object] = {
        "version": "risk-v1",
        "system_state": SystemRiskState.NORMAL,
        "max_total_risk": Decimal("100"),
        "max_fact_age": timedelta(seconds=30),
    }
    values.update(overrides)
    return RiskPolicyInput(**values)  # type: ignore[arg-type]


def risk_input(**overrides: object) -> RiskEvaluationInput:
    values: dict[str, object] = {
        "kind": IntentKind.INITIAL,
        "requested_quantity": Decimal("10"),
        "requested_risk": Decimal("40"),
        "current_risk": Decimal("20"),
        "fact_age": timedelta(seconds=1),
        "position_known": True,
        "equity_known": True,
        "protection_known": True,
    }
    values.update(overrides)
    return RiskEvaluationInput(**values)  # type: ignore[arg-type]


def test_risk_engine_is_deterministic_and_scales_to_available_capacity() -> None:
    inputs = risk_input(requested_risk=Decimal("100"), current_risk=Decimal("50"))

    first = evaluate_risk(policy(), inputs)
    second = evaluate_risk(policy(), inputs)

    assert first == second
    assert first.result is RiskResult.SCALE
    assert first.allowed_quantity == Decimal("5.000000000000000000")
    assert first.allowed_risk == Decimal("50.000000000000000000")
    assert first.reasons == ("RISK_CAPACITY_SCALED",)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"fact_age": timedelta(minutes=2)}, "STALE_FACTS"),
        ({"position_known": False}, "POSITION_UNKNOWN"),
        ({"equity_known": False}, "EQUITY_UNKNOWN"),
        ({"protection_known": False}, "PROTECTION_UNKNOWN"),
    ],
)
def test_risk_engine_denies_stale_or_unknown_facts(
    overrides: dict[str, object], reason: str
) -> None:
    result = evaluate_risk(policy(), risk_input(**overrides))

    assert result.result is RiskResult.DENY
    assert reason in result.reasons
    assert result.allowed_quantity == 0


@pytest.mark.parametrize(
    ("state", "kind", "reason"),
    [
        (SystemRiskState.KILL_SWITCH, IntentKind.INITIAL, "KILL_SWITCH"),
        (SystemRiskState.REDUCE_ONLY, IntentKind.INITIAL, "REDUCE_ONLY"),
        (SystemRiskState.NO_PYRAMID, IntentKind.ADD, "PYRAMID_DISABLED"),
    ],
)
def test_system_risk_state_blocks_new_risk(
    state: SystemRiskState, kind: IntentKind, reason: str
) -> None:
    result = evaluate_risk(policy(system_state=state), risk_input(kind=kind))

    assert result.result is RiskResult.DENY
    assert result.reasons == (reason,)


def test_target_position_arbiter_uses_smallest_target_and_highest_urgency() -> None:
    decision = select_target_position(
        (
            TargetCandidate(Decimal("8"), TargetUrgency.NORMAL, "trend"),
            TargetCandidate(Decimal("3"), TargetUrgency.IMMEDIATE, "protection"),
            TargetCandidate(Decimal("5"), TargetUrgency.URGENT, "risk"),
        )
    )

    assert decision.target_quantity == Decimal("3")
    assert decision.urgency is TargetUrgency.IMMEDIATE
    assert decision.reasons == ("protection", "risk", "trend")


def test_pnl_includes_fills_fees_funding_slippage_and_unrealized_value() -> None:
    open_result = compute_pnl(
        fills=(EconomicFill("BUY", Decimal("1"), Decimal("100"), Decimal("1"), Decimal("0.5")),),
        mark_price=Decimal("110"),
        funding=Decimal("-0.2"),
    )
    closed_result = compute_pnl(
        fills=(
            EconomicFill("BUY", Decimal("1"), Decimal("100"), Decimal("1"), Decimal("0.5")),
            EconomicFill("SELL", Decimal("1"), Decimal("120"), Decimal("1"), Decimal("0.5")),
        ),
        mark_price=Decimal("120"),
        funding=Decimal("-0.2"),
    )

    assert open_result.realized_pnl == Decimal("-1.700000000000000000")
    assert open_result.unrealized_pnl == Decimal("10.000000000000000000")
    assert open_result.total_pnl == Decimal("8.300000000000000000")
    assert closed_result.realized_pnl == Decimal("16.800000000000000000")
    assert closed_result.unrealized_pnl == 0
    assert closed_result.total_pnl == Decimal("16.800000000000000000")


def test_risk_engine_allows_valid_capacity_and_rejects_invalid_or_exhausted_requests() -> None:
    allowed = evaluate_risk(policy(), risk_input())
    invalid = evaluate_risk(policy(), risk_input(requested_quantity=Decimal("0")))
    exhausted = evaluate_risk(policy(), risk_input(current_risk=Decimal("100")))

    assert allowed.result is RiskResult.ALLOW
    assert allowed.allowed_quantity == Decimal("10.000000000000000000")
    assert invalid.reasons == ("INVALID_INPUT",)
    assert exhausted.reasons == ("RISK_CAPACITY_EXHAUSTED",)


@pytest.mark.parametrize(
    "overrides",
    [
        {"current_risk": Decimal("-1")},
        {"fact_age": timedelta(seconds=-1)},
        {"requested_risk": Decimal("0")},
    ],
)
def test_risk_engine_rejects_negative_or_nonpositive_inputs(
    overrides: dict[str, object],
) -> None:
    result = evaluate_risk(policy(), risk_input(**overrides))

    assert result.result is RiskResult.DENY
    assert result.reasons == ("INVALID_INPUT",)


def test_target_and_fill_validation_reject_invalid_inputs() -> None:
    with pytest.raises(DomainRejected, match="TARGET_CANDIDATES_REQUIRED"):
        select_target_position(())
    with pytest.raises(DomainRejected, match="INVALID_TARGET"):
        select_target_position((TargetCandidate(Decimal("-1"), TargetUrgency.NORMAL, "invalid"),))
    with pytest.raises(DomainRejected, match="INVALID_FILL"):
        compute_pnl(
            fills=(EconomicFill("BUY", Decimal("0"), Decimal("1"), Decimal("0"), Decimal("0")),),
            mark_price=Decimal("1"),
            funding=Decimal("0"),
        )
    with pytest.raises(DomainRejected, match="INVALID_FILL_SIDE"):
        compute_pnl(
            fills=(EconomicFill("HOLD", Decimal("1"), Decimal("1"), Decimal("0"), Decimal("0")),),
            mark_price=Decimal("1"),
            funding=Decimal("0"),
        )


def test_pnl_handles_short_close_and_position_reversal() -> None:
    result = compute_pnl(
        fills=(
            EconomicFill("SELL", Decimal("1"), Decimal("100"), Decimal("0"), Decimal("0")),
            EconomicFill("BUY", Decimal("2"), Decimal("90"), Decimal("0"), Decimal("0")),
        ),
        mark_price=Decimal("95"),
        funding=Decimal("0"),
    )

    assert result.realized_pnl == Decimal("10.000000000000000000")
    assert result.open_quantity == Decimal("1.000000000000000000")
    assert result.average_entry_price == Decimal("90.000000000000000000")
    assert result.unrealized_pnl == Decimal("5.000000000000000000")
