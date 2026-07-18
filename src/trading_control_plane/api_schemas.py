from __future__ import annotations

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from trading_control_plane.domain import Direction, IntentKind, RiskTier, TargetUrgency


class MockLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)


class ManualProposalRequest(BaseModel):
    environment: Literal["SHADOW", "TESTNET"] = "SHADOW"
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


class OrderIntentRequest(BaseModel):
    kind: IntentKind = IntentKind.INITIAL
    account_id: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=64)
    instrument_id: UUID
    direction: Direction
    quantity: Decimal = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @field_validator("venue")
    @classmethod
    def normalize_venue(cls, value: str) -> str:
        return value.upper()


class SenderLeaseRequest(BaseModel):
    execution_scope: str = Field(min_length=3, max_length=255)
    owner_id: str = Field(min_length=1, max_length=255)
    lease_seconds: int = Field(default=60, ge=5, le=300)


class ShadowSendRequest(BaseModel):
    execution_scope: str = Field(min_length=3, max_length=255)
    owner_id: str = Field(min_length=1, max_length=255)
    fencing_token: int = Field(ge=1)
    venue_order_id: str = Field(min_length=1, max_length=255)


class ShadowFillRequest(BaseModel):
    venue_fill_id: str = Field(min_length=1, max_length=255)
    side: Literal["BUY", "SELL"]
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    fee: Decimal = Field(default=Decimal(0), ge=0)
    fee_currency: str = Field(min_length=1, max_length=32)
    slippage_cost: Decimal = Field(default=Decimal(0), ge=0)


class IntentUnknownRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=1_000)


class IntentReleaseRequest(BaseModel):
    terminal_status: Literal["CANCELLED", "REJECTED"]
    reason: str = Field(min_length=2, max_length=1_000)


class PositionFactRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=64)
    instrument_id: UUID
    quantity: Decimal
    average_entry_price: Decimal = Field(ge=0)
    mark_price: Decimal = Field(gt=0)
    known: bool = True

    @field_validator("venue")
    @classmethod
    def normalize_venue(cls, value: str) -> str:
        return value.upper()


class AccountEquityFactRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=64)
    equity: Decimal = Field(ge=0)
    available_balance: Decimal = Field(ge=0)
    currency: str = Field(min_length=1, max_length=32)
    known: bool = True

    @field_validator("venue")
    @classmethod
    def normalize_venue(cls, value: str) -> str:
        return value.upper()


class ProtectionFactRequest(BaseModel):
    position_id: UUID
    venue_order_id: str = Field(min_length=1, max_length=255)
    quantity: Decimal = Field(ge=0)
    trigger_price: Decimal = Field(ge=0)
    fully_covered: bool
    known: bool = True


class FundingFactRequest(BaseModel):
    venue: str = Field(min_length=1, max_length=64)
    venue_payment_id: str = Field(min_length=1, max_length=255)
    amount: Decimal
    currency: str = Field(min_length=1, max_length=32)

    @field_validator("venue")
    @classmethod
    def normalize_venue(cls, value: str) -> str:
        return value.upper()


class TargetCandidateRequest(BaseModel):
    target_quantity: Decimal = Field(ge=0)
    urgency: TargetUrgency
    reason: str = Field(min_length=1, max_length=500)


class CampaignTargetRequest(BaseModel):
    candidates: list[TargetCandidateRequest] = Field(min_length=1, max_length=20)


class ReductionIntentRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=160)


class ReconciliationRequest(BaseModel):
    execution_scope: str = Field(min_length=3, max_length=255)


class ReconciliationReasonRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=1_000)


class BinanceReadOnlySyncRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=120)
    symbol: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z0-9_]+$")


class BinanceTestnetActionRequest(BaseModel):
    execution_scope: str = Field(min_length=3, max_length=255)
    owner_id: str = Field(min_length=1, max_length=255)
    fencing_token: int = Field(ge=1)


class BinanceTestnetProtectionRequest(BinanceTestnetActionRequest):
    trigger_price: Decimal = Field(gt=0)
