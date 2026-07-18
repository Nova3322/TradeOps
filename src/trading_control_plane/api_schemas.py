from __future__ import annotations

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from trading_control_plane.domain import Direction, RiskTier


class MockLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)


class ManualProposalRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=64)
    instrument_id: UUID
    direction: Direction
    risk_tier: RiskTier
    quantity: Decimal = Field(gt=0)
    max_risk: Decimal = Field(gt=0)
    expires_in_minutes: int = Field(default=120, ge=5, le=1_440)
    trigger_price: Decimal = Field(gt=0)
    limit_price: Decimal | None = Field(default=None, gt=0)
    invalidation_price: Decimal = Field(gt=0)
    rationale: str = Field(min_length=3, max_length=2_000)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @field_validator("venue")
    @classmethod
    def normalize_venue(cls, value: str) -> str:
        return value.upper()


class SystemProposalRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=120)
    risk_tier: RiskTier
    quantity: Decimal = Field(gt=0)
    max_risk: Decimal = Field(gt=0)
    expires_in_minutes: int = Field(default=120, ge=5, le=1_440)
    invalidation_price: Decimal = Field(gt=0)
    rationale: str = Field(min_length=3, max_length=2_000)


class ReviewRequest(BaseModel):
    decision: Literal["APPROVE", "REJECT"]
    reason: str = Field(min_length=2, max_length=1_000)
    expected_version: int = Field(ge=1)
    action_grant: str | None = None


class MockStepUpRequest(BaseModel):
    action: Literal["proposal.approve"]
    object_id: UUID
    object_version: int = Field(ge=1)


class RiskDecisionRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=160)
    requested_quantity: Decimal | None = Field(default=None, gt=0)


class AuthorizationRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=160)
    expires_in_minutes: int = Field(default=30, ge=1, le=120)
    allowed_adds: int = Field(default=0, ge=0, le=20)
