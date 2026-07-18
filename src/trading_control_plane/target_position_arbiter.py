from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trading_control_plane.commands import CommandRejected, hash_json
from trading_control_plane.metrics import TARGET_POSITION_ARBITRATIONS

DECISION_VERSION = "target-position-decision-v1"


class ReductionSourceType(StrEnum):
    HARD_STOP = "HARD_STOP"
    TREND_EXIT = "TREND_EXIT"
    DYNAMIC_DELEVERAGE = "DYNAMIC_DELEVERAGE"
    SYSTEM_RISK_REDUCTION = "SYSTEM_RISK_REDUCTION"


class ReductionUrgency(StrEnum):
    ORDERLY = "ORDERLY"
    URGENT = "URGENT"
    IMMEDIATE = "IMMEDIATE"


URGENCY_RANK = {
    ReductionUrgency.ORDERLY: 1,
    ReductionUrgency.URGENT: 2,
    ReductionUrgency.IMMEDIATE: 3,
}


class TargetPositionCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_type: ReductionSourceType
    source_ref: str = Field(min_length=1, max_length=255)
    policy_version: str = Field(min_length=1, max_length=160)
    reason_code: str = Field(min_length=1, max_length=160)
    current_position_binding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_quantity: Decimal = Field(ge=0, max_digits=38, decimal_places=18)
    urgency: ReductionUrgency
    facts_as_of: datetime
    valid_until: datetime
    candidate_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def candidate_is_self_consistent(self) -> Self:
        if self.valid_until <= self.facts_as_of:
            raise ValueError("target-position candidate validity is empty")
        material = self.model_dump(mode="json", exclude={"candidate_hash"})
        if self.candidate_hash != hash_json(material):
            raise ValueError("target-position candidate hash mismatch")
        return self


class TargetPositionDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_id: UUID
    current_position_binding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_quantity: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    target_quantity: Decimal = Field(ge=0, max_digits=38, decimal_places=18)
    reduction_quantity: Decimal = Field(ge=0, max_digits=38, decimal_places=18)
    action: str = Field(pattern=r"^(HOLD|REDUCE|EXIT)$")
    requires_order: bool
    reduce_only_required: bool
    urgency: str = Field(pattern=r"^(NONE|ORDERLY|URGENT|IMMEDIATE)$")
    selected_target_source_refs: tuple[str, ...]
    selected_urgency_source_refs: tuple[str, ...]
    target_reason_codes: tuple[str, ...]
    urgency_reason_codes: tuple[str, ...]
    all_reason_codes: tuple[str, ...]
    input_candidate_hashes: tuple[str, ...]
    facts_as_of: datetime
    evaluated_at: datetime
    decision_version: str = Field(pattern=r"^target-position-decision-v[0-9]+$")
    decision_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def decision_is_self_consistent(self) -> Self:
        if self.target_quantity > self.current_quantity:
            raise ValueError("target-position decision would increase the position")
        if self.reduction_quantity != self.current_quantity - self.target_quantity:
            raise ValueError("target-position reduction quantity is inconsistent")
        expected_action = (
            "HOLD"
            if self.target_quantity == self.current_quantity
            else ("EXIT" if self.target_quantity == 0 else "REDUCE")
        )
        if self.action != expected_action:
            raise ValueError("target-position action is inconsistent")
        if self.requires_order != (self.reduction_quantity > 0):
            raise ValueError("target-position order requirement is inconsistent")
        if not self.reduce_only_required:
            raise ValueError("target-position decisions must remain reduce-only")
        if self.action == "HOLD":
            if (
                self.urgency != "NONE"
                or self.selected_target_source_refs
                or self.selected_urgency_source_refs
                or self.target_reason_codes
                or self.urgency_reason_codes
                or self.all_reason_codes
                or self.input_candidate_hashes
            ):
                raise ValueError("empty arbitration cannot contain reduction sources")
        elif (
            self.urgency == "NONE"
            or not self.selected_target_source_refs
            or not self.selected_urgency_source_refs
            or not self.target_reason_codes
            or not self.urgency_reason_codes
            or not self.all_reason_codes
            or not self.input_candidate_hashes
        ):
            raise ValueError("reduction arbitration lacks its source explanation")
        if self.facts_as_of > self.evaluated_at:
            raise ValueError("target-position decision uses future facts")
        material = self.model_dump(mode="json", exclude={"decision_hash"})
        if self.decision_hash != hash_json(material):
            raise ValueError("target-position decision hash mismatch")
        return self


class TargetPositionArbiter:
    """Pure reduction arbitration: smaller target and higher urgency win independently."""

    @staticmethod
    def arbitrate(
        *,
        campaign_id: UUID,
        current_position_binding_hash: str,
        current_quantity: Decimal,
        candidates: tuple[TargetPositionCandidate, ...],
        evaluated_at: datetime,
    ) -> TargetPositionDecision:
        if current_quantity <= 0:
            TARGET_POSITION_ARBITRATIONS.labels("INVALID_CURRENT_POSITION").inc()
            raise CommandRejected(
                "TARGET_POSITION_CURRENT_POSITION_INVALID",
                "target-position arbitration requires a positive current position",
            )
        ordered = tuple(
            sorted(
                candidates,
                key=lambda item: (
                    item.source_type.value,
                    item.source_ref,
                    item.candidate_hash,
                ),
            )
        )
        source_refs = tuple(item.source_ref for item in ordered)
        if len(set(source_refs)) != len(source_refs):
            TARGET_POSITION_ARBITRATIONS.labels("SOURCE_CONFLICT").inc()
            raise CommandRejected(
                "TARGET_POSITION_CANDIDATE_CONFLICT",
                "one reduction source supplied multiple current candidates",
            )
        if any(
            item.current_position_binding_hash != current_position_binding_hash
            or item.target_quantity >= current_quantity
            for item in ordered
        ):
            TARGET_POSITION_ARBITRATIONS.labels("SOURCE_CONFLICT").inc()
            raise CommandRejected(
                "TARGET_POSITION_CANDIDATE_CONFLICT",
                "reduction candidates do not share the exact current position",
            )
        if any(
            item.facts_as_of > evaluated_at or evaluated_at >= item.valid_until for item in ordered
        ):
            TARGET_POSITION_ARBITRATIONS.labels("STALE_CANDIDATE").inc()
            raise CommandRejected(
                "TARGET_POSITION_CANDIDATE_STALE",
                "reduction candidate is future-dated or no longer valid",
            )

        if not ordered:
            target_quantity = current_quantity
            urgency = "NONE"
            selected_target_sources: tuple[str, ...] = ()
            selected_urgency_sources: tuple[str, ...] = ()
            target_reasons: tuple[str, ...] = ()
            urgency_reasons: tuple[str, ...] = ()
            all_reasons: tuple[str, ...] = ()
            facts_as_of = evaluated_at
        else:
            target_quantity = min(item.target_quantity for item in ordered)
            urgency_value = max(URGENCY_RANK[item.urgency] for item in ordered)
            urgency = next(
                item.value for item, rank in URGENCY_RANK.items() if rank == urgency_value
            )
            target_winners = tuple(
                item for item in ordered if item.target_quantity == target_quantity
            )
            urgency_winners = tuple(
                item for item in ordered if URGENCY_RANK[item.urgency] == urgency_value
            )
            selected_target_sources = tuple(item.source_ref for item in target_winners)
            selected_urgency_sources = tuple(item.source_ref for item in urgency_winners)
            target_reasons = tuple(sorted({item.reason_code for item in target_winners}))
            urgency_reasons = tuple(sorted({item.reason_code for item in urgency_winners}))
            all_reasons = tuple(sorted({item.reason_code for item in ordered}))
            facts_as_of = max(item.facts_as_of for item in ordered)

        reduction_quantity = current_quantity - target_quantity
        action = (
            "HOLD" if reduction_quantity == 0 else ("EXIT" if target_quantity == 0 else "REDUCE")
        )
        draft = TargetPositionDecision.model_construct(
            campaign_id=campaign_id,
            current_position_binding_hash=current_position_binding_hash,
            current_quantity=current_quantity,
            target_quantity=target_quantity,
            reduction_quantity=reduction_quantity,
            action=action,
            requires_order=reduction_quantity > 0,
            reduce_only_required=True,
            urgency=urgency,
            selected_target_source_refs=selected_target_sources,
            selected_urgency_source_refs=selected_urgency_sources,
            target_reason_codes=target_reasons,
            urgency_reason_codes=urgency_reasons,
            all_reason_codes=all_reasons,
            input_candidate_hashes=tuple(item.candidate_hash for item in ordered),
            facts_as_of=facts_as_of,
            evaluated_at=evaluated_at,
            decision_version=DECISION_VERSION,
            decision_hash="0" * 64,
        )
        decision = TargetPositionDecision.model_validate(
            {
                **draft.model_dump(mode="python"),
                "decision_hash": hash_json(
                    draft.model_dump(mode="json", exclude={"decision_hash"})
                ),
            }
        )
        TARGET_POSITION_ARBITRATIONS.labels(action).inc()
        return decision
