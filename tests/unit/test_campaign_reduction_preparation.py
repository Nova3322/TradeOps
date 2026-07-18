from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from trading_control_plane.campaign_reduction_plan import (
    PLAN_VERSION,
    CampaignReductionExecutionPlan,
)
from trading_control_plane.campaign_reduction_preparation import (
    CampaignReductionPlanPreparationService,
)
from trading_control_plane.commands import hash_json


def _plan() -> CampaignReductionExecutionPlan:
    now = datetime.now(UTC)
    draft = CampaignReductionExecutionPlan.model_construct(
        campaign_id=uuid4(),
        target_fact_id=uuid4(),
        target_version=1,
        target_semantic_hash="a" * 64,
        current_position_binding_hash="b" * 64,
        current_position_snapshot_id=uuid4(),
        current_position_snapshot_hash="c" * 64,
        direction="LONG",
        side="SELL",
        position_side="LONG",
        current_quantity=Decimal("0.5"),
        target_quantity=Decimal("0"),
        order_quantity=Decimal("0.5"),
        action="EXIT",
        urgency="IMMEDIATE",
        reason_codes=("PROTECTION_MISSING",),
        reduce_only=True,
        plan_idempotency_ref="campaign-reduction:test",
        order_type_status="UNAVAILABLE",
        venue_execution_terms_status="UNAVAILABLE",
        planned_at=now,
        valid_until=now + timedelta(minutes=1),
        plan_version=PLAN_VERSION,
        plan_hash="0" * 64,
        environment="SHADOW",
        live_order_eligible=False,
    )
    return CampaignReductionExecutionPlan.model_validate(
        {
            **draft.model_dump(mode="python"),
            "plan_hash": hash_json(draft.model_dump(mode="json", exclude={"plan_hash"})),
        }
    )


def test_reduction_plan_snapshot_rejects_payload_tampering() -> None:
    plan = _plan()
    snapshot = CampaignReductionPlanPreparationService._snapshot(
        snapshot_id=uuid4(),
        organization_id="org-test",
        plan=plan,
        recorded_at=plan.planned_at,
    )
    tampered = snapshot.model_dump(mode="python")
    tampered["plan_payload"]["order_quantity"] = "0.4"

    with pytest.raises(ValidationError, match="quantity is inconsistent"):
        snapshot.model_validate(tampered)
