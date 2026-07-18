from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.integration.test_execution import prepare_open_add_campaign
from trading_control_plane.campaign_protection_exit import (
    CampaignProtectionExitCandidateService,
    ProtectionExitStatus,
)
from trading_control_plane.campaign_target_position import (
    CampaignTargetPositionEvaluationService,
)
from trading_control_plane.commands import CommandRejected
from trading_control_plane.database import Database
from trading_control_plane.projections import ProjectionQueryContext
from trading_control_plane.target_position_arbiter import (
    ReductionSourceType,
    ReductionUrgency,
)


def test_exact_canonical_protection_does_not_create_exit_candidate(database: Database) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(database, unrealized_pnl=Decimal("9.75"))
    context = ProjectionQueryContext(as_of=datetime.now(UTC), max_age_ms=60_000)

    with database.session_factory.begin() as session:
        evaluation = CampaignProtectionExitCandidateService.evaluate(
            session,
            campaign.campaign_id,
            context,
        )

    assert evaluation.status is ProtectionExitStatus.CLEAR
    assert evaluation.protection_projection_state == "CONFIRMED"
    assert evaluation.protection_snapshot_id is not None
    assert evaluation.failure_reason is None
    assert evaluation.candidate is None


def test_missing_canonical_protection_produces_immediate_zero_target(
    database: Database,
) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(
        database,
        unrealized_pnl=Decimal("9.75"),
        protection_confirmed=False,
    )
    context = ProjectionQueryContext(as_of=datetime.now(UTC), max_age_ms=60_000)

    with database.session_factory.begin() as session:
        evaluation = CampaignProtectionExitCandidateService.evaluate(
            session,
            campaign.campaign_id,
            context,
        )
        assert evaluation.candidate is not None
        decision = CampaignTargetPositionEvaluationService.evaluate(
            session,
            campaign.campaign_id,
            (evaluation.candidate,),
            context,
        )

    candidate = evaluation.candidate
    assert evaluation.status is ProtectionExitStatus.EXIT_REQUIRED
    assert evaluation.protection_projection_state == "UNKNOWN"
    assert evaluation.failure_reason == "PROTECTION_MISSING"
    assert candidate.source_type is ReductionSourceType.SYSTEM_RISK_REDUCTION
    assert candidate.target_quantity == 0
    assert candidate.urgency is ReductionUrgency.IMMEDIATE
    assert decision.action == "EXIT"
    assert decision.current_quantity == Decimal("0.5")
    assert decision.target_quantity == 0
    assert decision.reduce_only_required


def test_protection_exit_rejects_stale_canonical_position_before_evaluation(
    database: Database,
) -> None:
    _, campaign, _, _ = prepare_open_add_campaign(database, unrealized_pnl=Decimal("9.75"))
    context = ProjectionQueryContext(
        as_of=datetime.now(UTC) + timedelta(days=1),
        max_age_ms=1_000,
    )

    with database.session_factory.begin() as session:
        with pytest.raises(CommandRejected) as stale:
            CampaignProtectionExitCandidateService.evaluate(
                session,
                campaign.campaign_id,
                context,
            )

    assert stale.value.error_code == "CAMPAIGN_CURRENT_POSITION_UNAVAILABLE"
