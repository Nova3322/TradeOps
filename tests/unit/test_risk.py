from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from tests.risk_fixtures import (
    SCOPE_IDS,
    make_capital,
    make_evaluation,
    make_policy,
    make_request,
    make_requested,
)
from trading_control_plane.authorization import RiskTier, SystemRiskState
from trading_control_plane.commands import hash_json
from trading_control_plane.risk import (
    FactStatus,
    FactType,
    RiskDecisionResult,
    RiskEvaluator,
    RiskPolicyParameters,
    RiskPrecheckRequest,
    ScopeLimit,
    ScopeType,
    TradeLossComponents,
)


def test_identical_input_and_versions_produce_identical_decision() -> None:
    evaluation = make_evaluation(now=datetime(2026, 7, 18, 12, tzinfo=UTC))
    evaluator = RiskEvaluator()

    first = evaluator.evaluate(evaluation)
    second = evaluator.evaluate(evaluation)

    assert first == second
    assert hash_json(first.model_dump(mode="json")) == hash_json(second.model_dump(mode="json"))
    assert first.result is RiskDecisionResult.ALLOW
    assert first.execution_eligible is False
    assert first.reservation_created is False


@pytest.mark.parametrize(
    ("tier", "multiplier", "leverage_cap"),
    [
        (RiskTier.LOW, Decimal("1"), Decimal("3")),
        (RiskTier.MEDIUM, Decimal("2"), Decimal("5")),
        (RiskTier.HIGH, Decimal("3"), Decimal("10")),
    ],
)
def test_fixed_one_r_tier_loss_and_leverage_contract(
    tier: RiskTier,
    multiplier: Decimal,
    leverage_cap: Decimal,
) -> None:
    result = RiskEvaluator().evaluate(make_evaluation(request=make_request(risk_tier=tier)))

    assert result.one_r_0 == Decimal("500")
    assert result.frozen_trade_loss_cap == Decimal("500") * multiplier
    assert result.leverage_cap == leverage_cap


def test_positive_equity_change_never_expands_frozen_candidate_capacity() -> None:
    high_equity = make_request(
        capital=make_capital(exchange_settled_equity_ex_upnl=Decimal("200000"))
    )
    result = RiskEvaluator().evaluate(make_evaluation(request=high_equity))

    assert result.dynamic_trade_loss_cap == Decimal("1000")
    assert result.frozen_trade_loss_cap == Decimal("500")
    assert result.effective_trade_loss_cap == Decimal("500")


def test_equity_decline_and_latest_upnl_immediately_tighten_dynamic_capacity() -> None:
    request = make_request(
        capital=make_capital(
            exchange_settled_equity_ex_upnl=Decimal("100000"),
            current_unrealized_pnl=Decimal("-20000"),
        ),
        requested=make_requested(requested_reserved_heat=Decimal("395")),
    )
    result = RiskEvaluator().evaluate(make_evaluation(request=request))

    assert result.current_portfolio_mtm_equity == Decimal("80000")
    assert result.current_unrealized_pnl == Decimal("-20000")
    assert result.dynamic_trade_loss_cap == Decimal("400")
    assert result.effective_trade_loss_cap == Decimal("400")
    assert result.result is RiskDecisionResult.DENY
    assert "TRADE_LOSS_LIMIT_EXCEEDED" in result.reason_codes


def test_exchange_risk_equity_cannot_count_positive_unrealized_profit() -> None:
    with pytest.raises(ValidationError, match="positive UPNL"):
        make_capital(
            exchange_settled_equity_ex_upnl=Decimal("100000"),
            current_unrealized_pnl=Decimal("20000"),
            exchange_risk_equity=Decimal("120000"),
        )


def test_stale_or_unknown_fact_fails_closed_with_zero_safe_quantity() -> None:
    now = datetime(2026, 7, 18, 12, tzinfo=UTC)
    stale = RiskEvaluator().evaluate(
        make_evaluation(
            now=now,
            request=make_request(now=now, fact_age=timedelta(seconds=6)),
        )
    )
    unknown = RiskEvaluator().evaluate(
        make_evaluation(
            now=now,
            request=make_request(now=now, fact_status=FactStatus.UNKNOWN),
        )
    )

    assert stale.result is RiskDecisionResult.DENY
    assert stale.primary_reason_code == "FACTS_STALE"
    assert stale.max_safe_quantity == 0
    assert set(stale.stale_fact_types) == set(FactType)
    assert stale.unknown_fact_types == ()
    assert unknown.result is RiskDecisionResult.DENY
    assert unknown.primary_reason_code == "FACTS_UNKNOWN"
    assert unknown.max_safe_quantity == 0


def test_scope_limit_denies_manual_request_and_reports_safe_quantity_without_execution() -> None:
    policy = make_policy(
        scope_limits=tuple(
            ScopeLimit(
                scope_type=scope_type,
                scope_id=scope_id,
                planned_loss_cap=(
                    Decimal("50") if scope_type is ScopeType.UNDERLYING else Decimal("10000")
                ),
                stress_loss_cap=Decimal("15000"),
            )
            for scope_type, scope_id in SCOPE_IDS.items()
        )
    )
    result = RiskEvaluator().evaluate(make_evaluation(policy=policy))

    assert result.result is RiskDecisionResult.DENY
    assert "SCOPE_PLANNED_LIMIT_EXCEEDED" in result.reason_codes
    assert result.max_safe_quantity == Decimal("0.454")
    assert result.final_quantity == 0
    assert result.execution_eligible is False


def test_known_unknown_heat_is_counted_and_never_released_by_precheck() -> None:
    request = make_request(
        current_trade_loss=TradeLossComponents(
            open_heat=Decimal("0"),
            reserved_heat=Decimal("0"),
            unknown_heat=Decimal("450"),
            protected_profit_giveback=Decimal("0"),
            cost_stress_add_on=Decimal("0"),
        )
    )
    result = RiskEvaluator().evaluate(make_evaluation(request=request))

    assert result.trade_worst_case_loss_before == Decimal("450")
    assert result.trade_worst_case_loss_after == Decimal("560")
    assert result.result is RiskDecisionResult.DENY
    assert result.max_safe_quantity == Decimal("0.454")


def test_initial_precheck_obeys_formal_risk_state_action_matrix() -> None:
    no_pyramid = RiskEvaluator().evaluate(
        make_evaluation(system_risk_state=SystemRiskState.NO_PYRAMID)
    )
    no_new = RiskEvaluator().evaluate(
        make_evaluation(system_risk_state=SystemRiskState.NO_NEW_POSITION)
    )

    assert no_pyramid.result is RiskDecisionResult.ALLOW
    assert no_new.result is RiskDecisionResult.DENY
    assert no_new.primary_reason_code == "SYSTEM_RISK_STATE_DENY"
    assert no_new.max_safe_quantity == 0


def test_invalid_protection_boundary_or_trading_rule_fails_closed() -> None:
    baseline_market = make_request().market
    invalidation_request = make_request(
        market=baseline_market.model_copy(update={"initial_invalidation_price": Decimal("101")})
    )
    minimum_request = make_request(
        market=baseline_market.model_copy(update={"minimum_quantity": Decimal("2")})
    )

    invalidation = RiskEvaluator().evaluate(make_evaluation(request=invalidation_request))
    minimum = RiskEvaluator().evaluate(make_evaluation(request=minimum_request))

    assert invalidation.primary_reason_code == "INVALIDATION_PRICE_INVALID"
    assert invalidation.max_safe_quantity == 0
    assert minimum.primary_reason_code == "TRADING_RULE_VIOLATION"
    assert minimum.max_safe_quantity == 0


def test_proposal_declared_risk_cap_above_tier_is_rejected_not_silently_capped() -> None:
    request = make_request(requested=make_requested(proposal_requested_loss_cap=Decimal("501")))

    result = RiskEvaluator().evaluate(make_evaluation(request=request))

    assert result.result is RiskDecisionResult.DENY
    assert result.primary_reason_code == "PROPOSAL_RISK_CAP_INVALID"
    assert result.max_safe_quantity == 0
    assert result.effective_trade_loss_cap == Decimal("500")


def test_policy_cannot_change_fixed_one_r_or_tier_contract() -> None:
    payload = make_policy().model_dump()
    payload["one_r_fraction"] = Decimal("0.01")

    with pytest.raises(ValidationError, match="one_r_fraction"):
        RiskPolicyParameters.model_validate(payload)


def test_precheck_requires_every_fact_and_scope_type() -> None:
    payload = make_request().model_dump()
    payload["facts"] = payload["facts"][:-1]

    with pytest.raises(ValidationError, match="every required fact type"):
        RiskPrecheckRequest.model_validate(payload)


def test_precheck_has_no_legacy_unbound_capital_fallback() -> None:
    payload = make_request().model_dump()
    payload.pop("capital_projection_binding")

    with pytest.raises(ValidationError, match="capital_projection_binding"):
        RiskPrecheckRequest.model_validate(payload)


def test_caller_supplied_certificate_boolean_is_rejected_and_derived_invalidity_denies() -> None:
    payload = make_request().model_dump(mode="python")
    payload["capability_certificate_valid"] = True

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RiskPrecheckRequest.model_validate(payload)

    result = RiskEvaluator().evaluate(make_evaluation(capability_valid=False))
    assert result.result is RiskDecisionResult.DENY
    assert result.primary_reason_code == "CAPABILITY_CERTIFICATE_INVALID"
    assert result.max_safe_quantity == 0


@given(
    frozen_capital=st.decimals(
        min_value=Decimal("1000"),
        max_value=Decimal("1000000"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
    current_equity=st.decimals(
        min_value=Decimal("1"),
        max_value=Decimal("2000000"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
    tier=st.sampled_from(list(RiskTier)),
)
def test_effective_trade_cap_is_always_the_stricter_frozen_dynamic_boundary(
    frozen_capital: Decimal,
    current_equity: Decimal,
    tier: RiskTier,
) -> None:
    multiplier = {
        RiskTier.LOW: Decimal("1"),
        RiskTier.MEDIUM: Decimal("2"),
        RiskTier.HIGH: Decimal("3"),
    }[tier]
    fixed_cap = frozen_capital * Decimal("0.005") * multiplier
    request = make_request(
        risk_tier=tier,
        capital=make_capital(
            exchange_settled_equity_ex_upnl=current_equity,
            total_capital_snapshot_0=frozen_capital,
        ),
        requested=make_requested(proposal_requested_loss_cap=fixed_cap),
    )

    result = RiskEvaluator().evaluate(make_evaluation(request=request))

    assert result.one_r_0 == frozen_capital * Decimal("0.005")
    assert result.effective_trade_loss_cap <= result.frozen_trade_loss_cap
    assert result.effective_trade_loss_cap <= result.dynamic_trade_loss_cap
    assert result.max_safe_quantity <= result.requested_quantity
