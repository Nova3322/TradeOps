from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from tests.risk_fixtures import (
    SCOPE_IDS,
    TEST_SCOPE_STRESS_SCENARIO,
    make_capital,
    make_evaluation,
    make_policy,
    make_request,
    make_requested,
)
from trading_control_plane.authorization import RiskTier, SystemRiskState
from trading_control_plane.commands import hash_json
from trading_control_plane.risk import (
    PositionDirection,
    RiskDecisionResult,
    RiskEvaluator,
    RiskPolicyParameters,
    RiskPrecheckRequest,
    ScopeLimit,
    ScopeRiskDecision,
    ScopeType,
    TradeLossComponents,
)
from trading_control_plane.risk_facts import FactStatus, FactType


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
        current_trade_loss=TradeLossComponents(
            open_heat=Decimal("390"),
            reserved_heat=Decimal("0"),
            unknown_heat=Decimal("0"),
            protected_profit_giveback=Decimal("0"),
            cost_stress_add_on=Decimal("0"),
        ),
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
            request=make_request(now=now),
            fact_age=timedelta(seconds=6),
        )
    )
    unknown = RiskEvaluator().evaluate(
        make_evaluation(
            now=now,
            request=make_request(now=now),
            fact_status=FactStatus.UNKNOWN,
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
                    Decimal("10") if scope_type is ScopeType.UNDERLYING else Decimal("10000")
                ),
                stress_loss_cap=Decimal("15000"),
                stress_scenario=TEST_SCOPE_STRESS_SCENARIO,
            )
            for scope_type, scope_id in SCOPE_IDS.items()
        )
    )
    result = RiskEvaluator().evaluate(make_evaluation(policy=policy))

    assert result.result is RiskDecisionResult.DENY
    assert "SCOPE_PLANNED_LIMIT_EXCEEDED" in result.reason_codes
    assert result.max_safe_quantity == Decimal("0.924")
    assert result.final_quantity == 0
    assert result.execution_eligible is False


def test_known_unknown_heat_is_counted_and_never_released_by_precheck() -> None:
    request = make_request(
        current_trade_loss=TradeLossComponents(
            open_heat=Decimal("0"),
            reserved_heat=Decimal("0"),
            unknown_heat=Decimal("490"),
            protected_profit_giveback=Decimal("0"),
            cost_stress_add_on=Decimal("0"),
        )
    )
    result = RiskEvaluator().evaluate(make_evaluation(request=request))

    assert result.trade_worst_case_loss_before == Decimal("490")
    assert result.trade_worst_case_loss_after == Decimal("500.8216")
    assert result.result is RiskDecisionResult.DENY
    assert result.max_safe_quantity == Decimal("0.924")


def test_requested_base_heat_is_canonical_for_long_short_and_decision_evidence() -> None:
    long_request = make_request()
    short_market = long_request.market.model_copy(
        update={
            "direction": PositionDirection.SHORT,
            "executable_price": Decimal("90"),
            "initial_invalidation_price": Decimal("100"),
            "funding_rate": Decimal("-0.0001"),
        }
    )
    short_request = make_request(
        market=short_market,
        requested=make_requested(requested_quantity=Decimal("2")),
    )

    assert long_request.requested_base_heat == Decimal("10.5")
    assert short_request.requested_base_heat == Decimal("20")

    result = RiskEvaluator().evaluate(make_evaluation(request=long_request))
    short_result = RiskEvaluator().evaluate(make_evaluation(request=short_request))
    assert result.requested_base_heat == Decimal("10.5")
    assert result.current_trade_loss.protected_profit_giveback == 0
    assert result.current_protected_position_risk_calculation_hash is None
    assert result.requested_fee_stress == Decimal("0.1005")
    assert result.requested_stop_penetration_stress == Decimal("0.201")
    assert result.requested_adverse_funding_stress == Decimal("0.0201")
    assert result.requested_cost_stress_add_on == Decimal("0.3216")
    assert result.requested_incremental_worst_case_loss == Decimal("10.8216")
    assert result.cost_stress_model_version == "fee-stop-funding-stress-v1"
    assert all(
        decision.incremental_planned_loss == Decimal("10.8216")
        and decision.gap_stress_add_on == Decimal("0.5025")
        and decision.liquidity_degradation_stress_add_on == Decimal("1.005")
        and decision.unprotected_window_stress_add_on == Decimal("0.25125")
        and decision.incremental_stress_loss == Decimal("12.58035")
        and decision.scope_stress_model_version == "planned-loss-plus-scope-shocks-v1"
        and decision.scope_stress_source_ref == "test-only:scope-stress-research-v1"
        for decision in result.scope_decisions
    )
    assert short_result.requested_fee_stress == Decimal("0.18")
    assert short_result.requested_stop_penetration_stress == Decimal("0.36")
    assert short_result.requested_adverse_funding_stress == Decimal("0.036")
    assert short_result.requested_cost_stress_add_on == Decimal("0.576")
    assert short_result.requested_incremental_worst_case_loss == Decimal("20.576")
    assert all(
        decision.incremental_stress_loss == Decimal("23.726")
        for decision in short_result.scope_decisions
    )


@pytest.mark.parametrize(
    ("legacy_path", "legacy_value"),
    (
        (("requested", "requested_reserved_heat"), Decimal("1")),
        (("requested", "requested_cost_stress_add_on"), Decimal("1")),
        (("requested", "requested_protected_profit_giveback"), Decimal("1")),
        (("scope_risks", 0, "requested_incremental_planned_loss"), Decimal("1")),
        (("scope_risks", 0, "requested_incremental_stress_loss"), Decimal("1")),
    ),
)
def test_v1_caller_reported_base_or_scope_planned_loss_is_rejected(
    legacy_path: tuple[str | int, ...],
    legacy_value: Decimal,
) -> None:
    payload = make_request().model_dump(mode="python")
    target: object = payload
    for key in legacy_path[:-1]:
        target = target[key]  # type: ignore[index]
    target[legacy_path[-1]] = legacy_value  # type: ignore[index]

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RiskPrecheckRequest.model_validate(payload)


def test_contract_multiplier_and_canonical_loss_model_are_exactly_bound() -> None:
    multiplier_payload = make_request().model_dump(mode="python")
    multiplier_payload["market"]["contract_multiplier"] = Decimal("2")
    with pytest.raises(ValidationError, match="certified binding"):
        RiskPrecheckRequest.model_validate(multiplier_payload)

    model_payload = make_request().model_dump(mode="python")
    model_payload["market"]["loss_model_version"] = "caller-selected-loss-v1"
    with pytest.raises(ValidationError, match="directional-entry-to-invalidation-v1"):
        RiskPrecheckRequest.model_validate(model_payload)


def test_cost_stress_policy_has_no_default_and_favorable_funding_is_not_charged() -> None:
    missing = make_policy().model_dump(mode="python")
    missing.pop("cost_stress")
    with pytest.raises(ValidationError, match="cost_stress"):
        RiskPolicyParameters.model_validate(missing)

    invalid_source = make_policy().model_dump(mode="python")
    invalid_source["cost_stress"]["source_ref"] = ""
    with pytest.raises(ValidationError, match="source_ref"):
        RiskPolicyParameters.model_validate(invalid_source)

    baseline = make_request()
    favorable = make_request(
        market=baseline.market.model_copy(update={"funding_rate": Decimal("-0.0001")})
    )
    result = RiskEvaluator().evaluate(make_evaluation(request=favorable))

    assert result.requested_adverse_funding_stress == 0
    assert result.requested_cost_stress_add_on == Decimal("0.3015")


def test_scope_stress_policy_has_no_default_or_unattributed_source() -> None:
    missing = make_policy().model_dump(mode="python")
    missing["scope_limits"][0].pop("stress_scenario")
    with pytest.raises(ValidationError, match="stress_scenario"):
        RiskPolicyParameters.model_validate(missing)

    invalid_source = make_policy().model_dump(mode="python")
    invalid_source["scope_limits"][0]["stress_scenario"]["source_ref"] = ""
    with pytest.raises(ValidationError, match="source_ref"):
        RiskPolicyParameters.model_validate(invalid_source)


def test_scope_stress_limit_uses_policy_derived_scenario_not_planned_loss() -> None:
    policy = make_policy(
        scope_limits=tuple(
            limit.model_copy(
                update={
                    "stress_loss_cap": (
                        Decimal("12")
                        if limit.scope_type is ScopeType.UNDERLYING
                        else limit.stress_loss_cap
                    )
                }
            )
            for limit in make_policy().scope_limits
        )
    )

    result = RiskEvaluator().evaluate(make_evaluation(policy=policy))

    underlying = next(
        decision
        for decision in result.scope_decisions
        if decision.scope_type is ScopeType.UNDERLYING
    )
    assert result.result is RiskDecisionResult.DENY
    assert "SCOPE_STRESS_LIMIT_EXCEEDED" in result.reason_codes
    assert underlying.incremental_planned_loss == Decimal("10.8216")
    assert underlying.incremental_stress_loss == Decimal("12.58035")
    assert underlying.planned_passed is True
    assert underlying.stress_passed is False


def test_scope_decision_rejects_inconsistent_derived_stress_evidence() -> None:
    decision = RiskEvaluator().evaluate(make_evaluation()).scope_decisions[0]
    payload = decision.model_dump(mode="python")
    payload["incremental_stress_loss"] = decision.incremental_planned_loss

    with pytest.raises(ValidationError, match="planned loss plus scenario add-ons"):
        ScopeRiskDecision.model_validate(payload)


def test_base_and_cost_amounts_round_up_to_database_precision() -> None:
    baseline = make_request()
    precise = make_request(
        market=baseline.market.model_copy(
            update={"executable_price": Decimal("100.5000000000000000001")}
        ),
    )

    result = RiskEvaluator().evaluate(make_evaluation(request=precise))

    assert precise.requested_base_heat == Decimal("10.500000000000000001")
    assert result.requested_fee_stress == Decimal("0.100500000000000001")
    assert result.requested_stop_penetration_stress == Decimal("0.201000000000000001")
    assert result.requested_adverse_funding_stress == Decimal("0.020100000000000001")
    assert all(
        decision.gap_stress_add_on == Decimal("0.502500000000000001")
        and decision.liquidity_degradation_stress_add_on == Decimal("1.005000000000000001")
        and decision.unprotected_window_stress_add_on == Decimal("0.251250000000000001")
        for decision in result.scope_decisions
    )


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


def test_precheck_rejects_caller_facts_and_requires_every_scope_type() -> None:
    payload = make_request().model_dump()
    payload["facts"] = []

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RiskPrecheckRequest.model_validate(payload)

    payload = make_request().model_dump()
    payload["scope_risks"] = payload["scope_risks"][:-1]

    with pytest.raises(ValidationError, match="every required scope type"):
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


def test_caller_instrument_boolean_is_rejected_and_catalog_invalidity_denies() -> None:
    payload = make_request().model_dump(mode="python")
    payload["instrument_classified"] = True

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RiskPrecheckRequest.model_validate(payload)

    result = RiskEvaluator().evaluate(make_evaluation(classification_valid=False))
    assert result.result is RiskDecisionResult.DENY
    assert result.primary_reason_code == "INSTRUMENT_UNCLASSIFIED"
    assert result.catalog_record_id is None
    assert result.catalog_validation_reason_codes == ("INSTRUMENT_CATALOG_RECORD_NOT_FOUND",)


def test_caller_protection_boolean_is_rejected_and_durable_invalidity_denies() -> None:
    payload = make_request().model_dump(mode="python")
    payload["protection_available"] = True

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RiskPrecheckRequest.model_validate(payload)

    result = RiskEvaluator().evaluate(make_evaluation(protection_valid=False))
    assert result.result is RiskDecisionResult.DENY
    assert result.primary_reason_code == "PROTECTION_UNAVAILABLE"
    assert result.protection_capability_record_id is None
    assert result.protection_capability_reason_codes == ("PROTECTION_CAPABILITY_RECORD_NOT_FOUND",)


def test_missing_durable_risk_fact_set_denies_without_evaluating_caller_facts() -> None:
    result = RiskEvaluator().evaluate(make_evaluation(fact_set_valid=False))

    assert result.result is RiskDecisionResult.DENY
    assert result.primary_reason_code == "RISK_FACT_SET_UNAVAILABLE"
    assert result.risk_fact_set_id is None
    assert result.risk_fact_set_reason_codes == ("RISK_FACT_SET_RECORD_NOT_FOUND",)
    assert result.max_safe_quantity == 0


def test_allow_validity_is_bounded_by_exact_catalog_record() -> None:
    now = datetime(2026, 7, 18, 12, tzinfo=UTC)
    evaluation = make_evaluation(now=now)
    catalog_valid_until = now + timedelta(seconds=1)
    bounded = evaluation.model_copy(
        update={
            "instrument_classification": evaluation.instrument_classification.model_copy(
                update={"valid_until": catalog_valid_until}
            )
        }
    )

    result = RiskEvaluator().evaluate(bounded)

    assert result.result is RiskDecisionResult.ALLOW
    assert result.valid_until == catalog_valid_until


def test_allow_validity_is_bounded_by_exact_protection_capability() -> None:
    now = datetime(2026, 7, 18, 12, tzinfo=UTC)
    evaluation = make_evaluation(now=now)
    protection_valid_until = now + timedelta(seconds=1)
    bounded = evaluation.model_copy(
        update={
            "protection_capability": evaluation.protection_capability.model_copy(
                update={"valid_until": protection_valid_until}
            )
        }
    )

    result = RiskEvaluator().evaluate(bounded)

    assert result.result is RiskDecisionResult.ALLOW
    assert result.valid_until == protection_valid_until


def test_allow_validity_is_bounded_by_exact_risk_fact_set() -> None:
    now = datetime(2026, 7, 18, 12, tzinfo=UTC)
    evaluation = make_evaluation(now=now)
    fact_set_valid_until = now + timedelta(seconds=1)
    bounded = evaluation.model_copy(
        update={
            "risk_fact_set": evaluation.risk_fact_set.model_copy(
                update={"valid_until": fact_set_valid_until}
            )
        }
    )

    result = RiskEvaluator().evaluate(bounded)

    assert result.result is RiskDecisionResult.ALLOW
    assert result.valid_until == fact_set_valid_until


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
