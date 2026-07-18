from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from trading_control_plane.commands import hash_json
from trading_control_plane.strategy_evaluations import (
    RegisterStrategyEvaluationDraft,
    RegisterStrategyEvaluationRequest,
    StrategyEvaluationEnvironment,
    StrategyEvaluationKind,
    StrategyEvaluationOutcome,
    StrategyRuleId,
    StrategyRuleResult,
    StrategyRuleStatus,
    strategy_evaluation_evidence_hash,
    strategy_evaluation_record_hash,
)


def _rules(
    status: StrategyRuleStatus = StrategyRuleStatus.PASS,
) -> tuple[StrategyRuleResult, ...]:
    return tuple(
        StrategyRuleResult(
            rule_id=rule_id,
            status=status,
            reason_code=f"TEST_{rule_id.value}_{status.value}",
            evidence_payload_hash=hash_json({"rule_id": rule_id.value, "status": status.value}),
        )
        for rule_id in sorted(StrategyRuleId, key=lambda item: item.value)
    )


def _draft(**updates: object) -> RegisterStrategyEvaluationDraft:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "strategy_evaluation_id": uuid4(),
        "campaign_id": uuid4(),
        "organization_id": "org-1",
        "strategy_id": "trend-breakout",
        "strategy_version": "1.0.0",
        "strategy_parameter_version": "params-v1",
        "venue": "BINANCE",
        "execution_domain": "BINANCE_USDM",
        "account_id": "account-1",
        "canonical_instrument_id": "BINANCE:BTCUSDT-PERP",
        "position_mode": "ONE_WAY",
        "margin_mode": "ISOLATED",
        "collateral_pool_id": "pool-usdt-1",
        "evaluation_version": "strategy-evaluation-v1",
        "evaluation_kind": StrategyEvaluationKind.ADD_CONTINUATION,
        "rule_results": _rules(),
        "outcome": StrategyEvaluationOutcome.PASS,
        "risk_fact_set_id": uuid4(),
        "risk_fact_set_version": "risk-facts-v1",
        "risk_fact_set_record_hash": "a" * 64,
        "position_snapshot_id": uuid4(),
        "position_snapshot_hash": "b" * 64,
        "protection_snapshot_id": uuid4(),
        "protection_snapshot_hash": "c" * 64,
        "evaluator_version": "strategy-evaluator-v1",
        "environment": StrategyEvaluationEnvironment.SHADOW,
        "real_funds_eligible": False,
        "evaluated_at": now,
        "valid_from": now,
        "valid_until": now + timedelta(minutes=5),
        "evidence_refs": ("evidence:a", "evidence:b"),
        "source_ref": "strategy-evaluation-service:shadow",
    }
    values.update(updates)
    return RegisterStrategyEvaluationDraft.model_validate(values)


def _request(draft: RegisterStrategyEvaluationDraft) -> RegisterStrategyEvaluationRequest:
    return RegisterStrategyEvaluationRequest.model_validate(
        {
            **draft.model_dump(mode="json"),
            "record_hash": strategy_evaluation_record_hash(draft),
            "evidence_hash": strategy_evaluation_evidence_hash(draft),
        }
    )


def test_strategy_evaluation_hashes_exact_rules_and_source_facts() -> None:
    draft = _draft()
    request = _request(draft)
    changed = _draft(
        strategy_evaluation_id=draft.strategy_evaluation_id,
        campaign_id=draft.campaign_id,
        risk_fact_set_id=draft.risk_fact_set_id,
        position_snapshot_id=draft.position_snapshot_id,
        protection_snapshot_id=draft.protection_snapshot_id,
        position_snapshot_hash="d" * 64,
    )

    assert request.outcome is StrategyEvaluationOutcome.PASS
    assert tuple(item.rule_id for item in request.rule_results) == tuple(
        sorted(StrategyRuleId, key=lambda item: item.value)
    )
    assert request.record_hash != strategy_evaluation_record_hash(changed)


@pytest.mark.parametrize(
    "updates",
    (
        {"rule_results": _rules()[:-1]},
        {"rule_results": tuple(reversed(_rules()))},
        {"outcome": StrategyEvaluationOutcome.FAIL},
        {"evidence_refs": ("evidence:b", "evidence:a")},
        {"environment": "PRODUCTION", "real_funds_eligible": True},
    ),
)
def test_strategy_evaluation_rejects_incomplete_or_inconsistent_contract(
    updates: dict[str, object],
) -> None:
    draft = _draft()
    payload = {
        **draft.model_dump(mode="json"),
        **updates,
        "record_hash": "0" * 64,
        "evidence_hash": "0" * 64,
    }

    with pytest.raises(ValidationError):
        RegisterStrategyEvaluationRequest.model_validate(payload)
