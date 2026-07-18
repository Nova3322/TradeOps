from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from trading_control_plane.campaign_position_binding import (
    CampaignCurrentPositionBindingService,
)
from trading_control_plane.metrics import CAMPAIGN_TARGET_POSITION_EVALUATIONS
from trading_control_plane.projections import ProjectionQueryContext
from trading_control_plane.target_position_arbiter import (
    TargetPositionArbiter,
    TargetPositionCandidate,
    TargetPositionDecision,
)


class CampaignTargetPositionEvaluationService:
    """Binds pure reduction arbitration to a fresh canonical Campaign position."""

    @staticmethod
    def evaluate(
        session: Session,
        campaign_id: UUID,
        candidates: tuple[TargetPositionCandidate, ...],
        context: ProjectionQueryContext,
    ) -> TargetPositionDecision:
        position = CampaignCurrentPositionBindingService.resolve(session, campaign_id, context)
        decision = TargetPositionArbiter.arbitrate(
            campaign_id=campaign_id,
            current_position_binding_hash=position.binding_hash,
            current_quantity=position.current_quantity,
            candidates=candidates,
            evaluated_at=context.as_of,
        )
        CAMPAIGN_TARGET_POSITION_EVALUATIONS.labels(decision.action).inc()
        return decision
