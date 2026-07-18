from __future__ import annotations

from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from trading_control_plane.commands import hash_json
from trading_control_plane.execution_models import OrderIntent, OrderIntentState
from trading_control_plane.metrics import CAMPAIGN_ORDER_INTENT_OCCUPANCY_EVALUATIONS

OCCUPANCY_VERSION = "campaign-order-intent-occupancy-v1"
POSITION_STABLE_STATUSES = frozenset(
    {
        "CANCELLED_ZERO_FILL",
        "REJECTED_ZERO_FILL",
        "POSITION_RECONCILED",
        "PROTECTION_CONFIRMED",
        "COMPLETED",
    }
)


class BlockingCampaignOrderIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    order_intent_id: UUID
    intent_kind: str = Field(pattern=r"^(INITIAL|ADD)$")
    candidate_ref: str = Field(min_length=1, max_length=255)
    status: str = Field(min_length=1, max_length=32)
    state_version: int = Field(gt=0)
    intent_quantity: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    cumulative_filled_quantity: Decimal = Field(ge=0, max_digits=38, decimal_places=18)
    known_remaining_quantity: Decimal = Field(ge=0, max_digits=38, decimal_places=18)
    zero_fill_confirmed: bool
    venue_order_terminal: bool
    position_reconciled: bool
    protection_confirmed: bool

    @model_validator(mode="after")
    def intent_is_unresolved(self) -> Self:
        if self.status in POSITION_STABLE_STATUSES:
            raise ValueError("stable OrderIntent cannot occupy reduction planning")
        return self


class CampaignOrderIntentOccupancy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_id: UUID
    observed_intent_count: int = Field(ge=0)
    stable_intent_count: int = Field(ge=0)
    blocking_intents: tuple[BlockingCampaignOrderIntent, ...]
    status: str = Field(pattern=r"^(CLEAR|BLOCKED)$")
    occupancy_version: str = Field(pattern=r"^campaign-order-intent-occupancy-v[0-9]+$")
    occupancy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def occupancy_is_self_consistent(self) -> Self:
        if self.observed_intent_count != self.stable_intent_count + len(self.blocking_intents):
            raise ValueError("Campaign OrderIntent occupancy count is inconsistent")
        if self.status != ("BLOCKED" if self.blocking_intents else "CLEAR"):
            raise ValueError("Campaign OrderIntent occupancy status is inconsistent")
        if (
            tuple(sorted(self.blocking_intents, key=lambda item: str(item.order_intent_id)))
            != self.blocking_intents
        ):
            raise ValueError("Campaign blocking OrderIntents must be deterministically ordered")
        material = self.model_dump(mode="json", exclude={"occupancy_hash"})
        if self.occupancy_hash != hash_json(material):
            raise ValueError("Campaign OrderIntent occupancy hash mismatch")
        return self


class CampaignOrderIntentOccupancyService:
    """Classifies unresolved INITIAL/ADD intents that can invalidate reduction quantity."""

    @staticmethod
    def resolve(
        session: Session,
        campaign_id: UUID,
        *,
        lock: bool = False,
    ) -> CampaignOrderIntentOccupancy:
        statement: Select[tuple[OrderIntent, OrderIntentState]] = (
            select(OrderIntent, OrderIntentState)
            .join(
                OrderIntentState,
                OrderIntentState.order_intent_id == OrderIntent.order_intent_id,
            )
            .where(OrderIntent.campaign_id == campaign_id)
            .order_by(OrderIntent.order_intent_id)
        )
        if lock:
            statement = statement.with_for_update()
        rows = session.execute(statement).all()
        blockers = tuple(
            sorted(
                (
                    BlockingCampaignOrderIntent(
                        order_intent_id=intent.order_intent_id,
                        intent_kind=intent.intent_kind,
                        candidate_ref=intent.candidate_ref,
                        status=state.status,
                        state_version=state.version,
                        intent_quantity=state.intent_quantity,
                        cumulative_filled_quantity=state.cumulative_filled_quantity,
                        known_remaining_quantity=state.known_remaining_quantity,
                        zero_fill_confirmed=state.zero_fill_confirmed,
                        venue_order_terminal=state.venue_order_terminal,
                        position_reconciled=state.position_reconciled,
                        protection_confirmed=state.protection_confirmed,
                    )
                    for intent, state in rows
                    if state.status not in POSITION_STABLE_STATUSES
                ),
                key=lambda item: str(item.order_intent_id),
            )
        )
        draft = CampaignOrderIntentOccupancy.model_construct(
            campaign_id=campaign_id,
            observed_intent_count=len(rows),
            stable_intent_count=len(rows) - len(blockers),
            blocking_intents=blockers,
            status="BLOCKED" if blockers else "CLEAR",
            occupancy_version=OCCUPANCY_VERSION,
            occupancy_hash="0" * 64,
        )
        occupancy = CampaignOrderIntentOccupancy.model_validate(
            {
                **draft.model_dump(mode="python"),
                "occupancy_hash": hash_json(
                    draft.model_dump(mode="json", exclude={"occupancy_hash"})
                ),
            }
        )
        CAMPAIGN_ORDER_INTENT_OCCUPANCY_EVALUATIONS.labels(occupancy.status).inc()
        return occupancy
