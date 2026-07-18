from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from trading_control_plane.commands import CommandRejected, hash_json
from trading_control_plane.target_position_arbiter import (
    ReductionSourceType,
    ReductionUrgency,
    TargetPositionArbiter,
    TargetPositionCandidate,
    TargetPositionDecision,
)


def _candidate(
    *,
    source_type: ReductionSourceType,
    source_ref: str,
    target_quantity: Decimal,
    urgency: ReductionUrgency,
    reason_code: str,
    binding_hash: str = "a" * 64,
    now: datetime | None = None,
) -> TargetPositionCandidate:
    evaluated_at = now or datetime.now(UTC)
    draft = TargetPositionCandidate.model_construct(
        source_type=source_type,
        source_ref=source_ref,
        policy_version="target-position-policy-v1",
        reason_code=reason_code,
        current_position_binding_hash=binding_hash,
        target_quantity=target_quantity,
        urgency=urgency,
        facts_as_of=evaluated_at - timedelta(seconds=1),
        valid_until=evaluated_at + timedelta(seconds=30),
        candidate_hash="0" * 64,
    )
    return TargetPositionCandidate.model_validate(
        {
            **draft.model_dump(mode="python"),
            "candidate_hash": hash_json(draft.model_dump(mode="json", exclude={"candidate_hash"})),
        }
    )


def test_target_position_arbiter_returns_hold_without_reduction_candidates() -> None:
    now = datetime.now(UTC)
    decision = TargetPositionArbiter.arbitrate(
        campaign_id=uuid4(),
        current_position_binding_hash="a" * 64,
        current_quantity=Decimal("1"),
        candidates=(),
        evaluated_at=now,
    )

    assert decision.action == "HOLD"
    assert decision.target_quantity == Decimal("1")
    assert decision.reduction_quantity == Decimal("0")
    assert not decision.requires_order
    assert decision.reduce_only_required
    assert decision.urgency == "NONE"
    assert decision.input_candidate_hashes == ()
    assert decision.facts_as_of == now


def test_target_position_arbiter_selects_smaller_target_and_higher_urgency_independently() -> None:
    now = datetime.now(UTC)
    smaller = _candidate(
        source_type=ReductionSourceType.DYNAMIC_DELEVERAGE,
        source_ref="deleverage:btc:1",
        target_quantity=Decimal("0.2"),
        urgency=ReductionUrgency.ORDERLY,
        reason_code="LEVERAGE_ABOVE_MAX",
        now=now,
    )
    urgent = _candidate(
        source_type=ReductionSourceType.SYSTEM_RISK_REDUCTION,
        source_ref="risk-state:reduce-only:1",
        target_quantity=Decimal("0.5"),
        urgency=ReductionUrgency.IMMEDIATE,
        reason_code="SYSTEM_REDUCE_ONLY",
        now=now,
    )
    campaign_id = uuid4()

    forward = TargetPositionArbiter.arbitrate(
        campaign_id=campaign_id,
        current_position_binding_hash="a" * 64,
        current_quantity=Decimal("1"),
        candidates=(smaller, urgent),
        evaluated_at=now,
    )
    reversed_order = TargetPositionArbiter.arbitrate(
        campaign_id=campaign_id,
        current_position_binding_hash="a" * 64,
        current_quantity=Decimal("1"),
        candidates=(urgent, smaller),
        evaluated_at=now,
    )

    assert forward.decision_hash == reversed_order.decision_hash
    assert forward.action == "REDUCE"
    assert forward.target_quantity == Decimal("0.2")
    assert forward.reduction_quantity == Decimal("0.8")
    assert forward.requires_order
    assert forward.reduce_only_required
    assert forward.urgency == "IMMEDIATE"
    assert forward.selected_target_source_refs == ("deleverage:btc:1",)
    assert forward.selected_urgency_source_refs == ("risk-state:reduce-only:1",)
    assert forward.target_reason_codes == ("LEVERAGE_ABOVE_MAX",)
    assert forward.urgency_reason_codes == ("SYSTEM_REDUCE_ONLY",)
    assert forward.all_reason_codes == (
        "LEVERAGE_ABOVE_MAX",
        "SYSTEM_REDUCE_ONLY",
    )


def test_target_position_arbiter_returns_exit_for_zero_target_and_rejects_tampering() -> None:
    now = datetime.now(UTC)
    stop = _candidate(
        source_type=ReductionSourceType.HARD_STOP,
        source_ref="native-stop:btc:1",
        target_quantity=Decimal("0"),
        urgency=ReductionUrgency.IMMEDIATE,
        reason_code="HARD_STOP_TRIGGERED",
        now=now,
    )
    decision = TargetPositionArbiter.arbitrate(
        campaign_id=uuid4(),
        current_position_binding_hash="a" * 64,
        current_quantity=Decimal("0.5"),
        candidates=(stop,),
        evaluated_at=now,
    )

    assert decision.action == "EXIT"
    assert decision.target_quantity == 0
    assert decision.reduction_quantity == Decimal("0.5")
    with pytest.raises(ValueError, match="would increase the position"):
        TargetPositionDecision.model_validate(
            {**decision.model_dump(mode="python"), "target_quantity": Decimal("0.6")}
        )
    with pytest.raises(ValueError, match="must remain reduce-only"):
        TargetPositionDecision.model_validate(
            {**decision.model_dump(mode="python"), "reduce_only_required": False}
        )


def test_target_position_arbiter_rejects_conflicting_stale_or_expanding_candidates() -> None:
    now = datetime.now(UTC)
    current = _candidate(
        source_type=ReductionSourceType.TREND_EXIT,
        source_ref="trend:btc:1",
        target_quantity=Decimal("0.5"),
        urgency=ReductionUrgency.URGENT,
        reason_code="TREND_INVALIDATED",
        now=now,
    )
    duplicate = _candidate(
        source_type=ReductionSourceType.TREND_EXIT,
        source_ref="trend:btc:1",
        target_quantity=Decimal("0.4"),
        urgency=ReductionUrgency.IMMEDIATE,
        reason_code="TREND_INVALIDATED_NEWER",
        now=now,
    )
    mismatched = _candidate(
        source_type=ReductionSourceType.SYSTEM_RISK_REDUCTION,
        source_ref="risk:btc:1",
        target_quantity=Decimal("0.5"),
        urgency=ReductionUrgency.IMMEDIATE,
        reason_code="SYSTEM_REDUCE_ONLY",
        binding_hash="b" * 64,
        now=now,
    )
    expanding = _candidate(
        source_type=ReductionSourceType.DYNAMIC_DELEVERAGE,
        source_ref="deleveraging:btc:1",
        target_quantity=Decimal("1.1"),
        urgency=ReductionUrgency.ORDERLY,
        reason_code="BAD_TARGET",
        now=now,
    )
    no_op = _candidate(
        source_type=ReductionSourceType.DYNAMIC_DELEVERAGE,
        source_ref="deleveraging:btc:no-op",
        target_quantity=Decimal("1"),
        urgency=ReductionUrgency.ORDERLY,
        reason_code="NO_REDUCTION",
        now=now,
    )

    with pytest.raises(CommandRejected) as duplicate_error:
        TargetPositionArbiter.arbitrate(
            campaign_id=uuid4(),
            current_position_binding_hash="a" * 64,
            current_quantity=Decimal("1"),
            candidates=(current, duplicate),
            evaluated_at=now,
        )
    assert duplicate_error.value.error_code == "TARGET_POSITION_CANDIDATE_CONFLICT"

    for candidate in (mismatched, expanding, no_op):
        with pytest.raises(CommandRejected) as conflict:
            TargetPositionArbiter.arbitrate(
                campaign_id=uuid4(),
                current_position_binding_hash="a" * 64,
                current_quantity=Decimal("1"),
                candidates=(candidate,),
                evaluated_at=now,
            )
        assert conflict.value.error_code == "TARGET_POSITION_CANDIDATE_CONFLICT"

    with pytest.raises(CommandRejected) as stale:
        TargetPositionArbiter.arbitrate(
            campaign_id=uuid4(),
            current_position_binding_hash="a" * 64,
            current_quantity=Decimal("1"),
            candidates=(current,),
            evaluated_at=now + timedelta(minutes=1),
        )
    assert stale.value.error_code == "TARGET_POSITION_CANDIDATE_STALE"

    with pytest.raises(CommandRejected) as invalid_position:
        TargetPositionArbiter.arbitrate(
            campaign_id=uuid4(),
            current_position_binding_hash="a" * 64,
            current_quantity=Decimal("0"),
            candidates=(),
            evaluated_at=now,
        )
    assert invalid_position.value.error_code == "TARGET_POSITION_CURRENT_POSITION_INVALID"
