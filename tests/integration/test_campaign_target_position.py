from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.integration.test_execution import prepare_open_add_campaign
from trading_control_plane.campaign_position_binding import (
    CampaignCurrentPositionBindingService,
)
from trading_control_plane.campaign_target_position import (
    CampaignTargetPositionEvaluationService,
)
from trading_control_plane.commands import CommandRejected, hash_json
from trading_control_plane.database import Database
from trading_control_plane.projections import ProjectionQueryContext
from trading_control_plane.target_position_arbiter import (
    ReductionSourceType,
    ReductionUrgency,
    TargetPositionCandidate,
)


def _candidate(
    *,
    binding_hash: str,
    target_quantity: Decimal,
    now: datetime,
    source_ref: str = "risk:campaign-target:1",
) -> TargetPositionCandidate:
    draft = TargetPositionCandidate.model_construct(
        source_type=ReductionSourceType.SYSTEM_RISK_REDUCTION,
        source_ref=source_ref,
        policy_version="campaign-target-test-v1",
        reason_code="SYSTEM_REDUCE_ONLY",
        current_position_binding_hash=binding_hash,
        target_quantity=target_quantity,
        urgency=ReductionUrgency.IMMEDIATE,
        facts_as_of=now - timedelta(milliseconds=1),
        valid_until=now + timedelta(seconds=30),
        candidate_hash="0" * 64,
    )
    return TargetPositionCandidate.model_validate(
        {
            **draft.model_dump(mode="python"),
            "candidate_hash": hash_json(draft.model_dump(mode="json", exclude={"candidate_hash"})),
        }
    )


def test_campaign_target_position_uses_server_resolved_current_quantity_and_binding(
    database: Database,
) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(database, unrealized_pnl=Decimal("9.75"))
    context = ProjectionQueryContext(as_of=datetime.now(UTC), max_age_ms=60_000)
    with database.session_factory.begin() as session:
        position = CampaignCurrentPositionBindingService.resolve(
            session,
            campaign.campaign_id,
            context,
        )
    candidate = _candidate(
        binding_hash=position.binding_hash,
        target_quantity=Decimal("0.25"),
        now=context.as_of,
    )

    with database.session_factory.begin() as session:
        reduced = CampaignTargetPositionEvaluationService.evaluate(
            session,
            campaign.campaign_id,
            (candidate,),
            context,
        )
        held = CampaignTargetPositionEvaluationService.evaluate(
            session,
            campaign.campaign_id,
            (),
            context,
        )

    assert reduced.current_position_binding_hash == position.binding_hash
    assert reduced.current_quantity == Decimal("0.5")
    assert reduced.target_quantity == Decimal("0.25")
    assert reduced.reduction_quantity == Decimal("0.25")
    assert reduced.action == "REDUCE"
    assert reduced.reduce_only_required
    assert held.current_quantity == Decimal("0.5")
    assert held.target_quantity == Decimal("0.5")
    assert held.action == "HOLD"


def test_campaign_target_position_rejects_caller_binding_or_quantity_substitution(
    database: Database,
) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(database, unrealized_pnl=Decimal("9.75"))
    context = ProjectionQueryContext(as_of=datetime.now(UTC), max_age_ms=60_000)
    with database.session_factory.begin() as session:
        position = CampaignCurrentPositionBindingService.resolve(
            session,
            campaign.campaign_id,
            context,
        )
    mismatched = _candidate(
        binding_hash="b" * 64,
        target_quantity=Decimal("0.25"),
        now=context.as_of,
        source_ref="risk:mismatched-binding",
    )
    expanding = _candidate(
        binding_hash=position.binding_hash,
        target_quantity=Decimal("0.6"),
        now=context.as_of,
        source_ref="risk:caller-current-quantity",
    )

    for candidate in (mismatched, expanding):
        with database.session_factory.begin() as session:
            with pytest.raises(CommandRejected) as conflict:
                CampaignTargetPositionEvaluationService.evaluate(
                    session,
                    campaign.campaign_id,
                    (candidate,),
                    context,
                )
        assert conflict.value.error_code == "TARGET_POSITION_CANDIDATE_CONFLICT"


def test_campaign_target_position_rejects_stale_canonical_position_before_arbitration(
    database: Database,
) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(database, unrealized_pnl=Decimal("9.75"))
    stale_context = ProjectionQueryContext(
        as_of=datetime.now(UTC) + timedelta(days=1),
        max_age_ms=1_000,
    )

    with database.session_factory.begin() as session:
        with pytest.raises(CommandRejected) as stale:
            CampaignTargetPositionEvaluationService.evaluate(
                session,
                campaign.campaign_id,
                (),
                stale_context,
            )
    assert stale.value.error_code == "CAMPAIGN_CURRENT_POSITION_UNAVAILABLE"
