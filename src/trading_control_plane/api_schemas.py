from __future__ import annotations

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from trading_control_plane.domain import Direction, RiskTier, TargetUrgency


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
    initial_quantity: Decimal | None = Field(default=None, gt=0)
    max_risk: Decimal = Field(gt=0)
    expires_in_minutes: int = Field(default=120, ge=5, le=1_440)
    trigger_price: Decimal = Field(gt=0)
    limit_price: Decimal | None = Field(default=None, gt=0)
    invalidation_price: Decimal = Field(gt=0)
    allow_auto_add: bool = False
    requested_adds: int = Field(default=0, ge=0, le=3)
    add_trigger_price: Decimal | None = Field(default=None, gt=0)
    rationale: str = Field(min_length=3, max_length=2_000)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @field_validator("venue")
    @classmethod
    def normalize_venue(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def validate_auto_add_contract(self) -> ManualProposalRequest:
        _validate_auto_add_fields(self)
        return self


class SystemProposalRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=120)
    risk_tier: RiskTier
    quantity: Decimal = Field(gt=0)
    initial_quantity: Decimal | None = Field(default=None, gt=0)
    max_risk: Decimal = Field(gt=0)
    expires_in_minutes: int = Field(default=120, ge=5, le=1_440)
    invalidation_price: Decimal = Field(gt=0)
    allow_auto_add: bool = False
    requested_adds: int = Field(default=0, ge=0, le=3)
    add_trigger_price: Decimal | None = Field(default=None, gt=0)
    rationale: str = Field(min_length=3, max_length=2_000)

    @model_validator(mode="after")
    def validate_auto_add_contract(self) -> SystemProposalRequest:
        _validate_auto_add_fields(self)
        return self


def _validate_auto_add_fields(value: ManualProposalRequest | SystemProposalRequest) -> None:
    initial_quantity = value.quantity if value.initial_quantity is None else value.initial_quantity
    if initial_quantity > value.quantity:
        raise ValueError("initial_quantity cannot exceed the frozen total quantity cap")
    tier_limit = {RiskTier.LOW: 1, RiskTier.MEDIUM: 2, RiskTier.HIGH: 3}[value.risk_tier]
    if value.requested_adds > tier_limit:
        raise ValueError("requested_adds exceeds the selected risk tier limit")
    if value.allow_auto_add:
        if value.requested_adds == 0:
            raise ValueError("enabled AUTO_ADD requires at least one requested AddUnit")
        if initial_quantity >= value.quantity:
            raise ValueError("enabled AUTO_ADD requires quantity capacity after the initial order")
        if value.add_trigger_price is None:
            raise ValueError("enabled AUTO_ADD requires a frozen add trigger price")
    elif (
        value.requested_adds != 0
        or value.add_trigger_price is not None
        or initial_quantity != value.quantity
    ):
        raise ValueError("disabled AUTO_ADD cannot reserve AddUnits or Add quantity")


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
    allowed_adds: int = Field(default=0, ge=0, le=3)


class OrderIntentRequest(BaseModel):
    kind: Literal["INITIAL"] = "INITIAL"
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
    limit_price: Decimal | None = Field(default=None, gt=0)


class AutoAddRequest(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=160)
    quantity: Decimal = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ManagedReductionRequest(BaseModel):
    target_quantity: Decimal = Field(ge=0)
    urgency: TargetUrgency = TargetUrgency.URGENT
    reason: str = Field(min_length=2, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=160)
    limit_price: Decimal | None = Field(default=None, gt=0)


class AutomaticExitRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=160)
    limit_price: Decimal | None = Field(default=None, gt=0)


class RiskTightenRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=160)


class TelegramCampaignActionRequest(BaseModel):
    action: Literal[
        "DISABLE_CAMPAIGN_AUTO_ADD",
        "PAUSE_NEW_RISK",
        "EMERGENCY_REDUCE",
        "EXIT",
    ]
    action_reference: str = Field(min_length=1)
    campaign_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=160)
    target_quantity: Decimal | None = Field(default=None, ge=0)
    limit_price: Decimal | None = Field(default=None, gt=0)


class ReconciliationRequest(BaseModel):
    execution_scope: str = Field(min_length=3, max_length=255)


class ReconciliationReasonRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=1_000)


class BinanceReadOnlySyncRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=120)
    symbol: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z0-9_]+$")


class HyperliquidReadOnlySyncRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=120)
    symbol: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z0-9]+$")


class BinanceTestnetActionRequest(BaseModel):
    execution_scope: str = Field(min_length=3, max_length=255)
    owner_id: str = Field(min_length=1, max_length=255)
    fencing_token: int = Field(ge=1)


class BinanceTestnetProtectionRequest(BinanceTestnetActionRequest):
    trigger_price: Decimal = Field(gt=0)


class HyperliquidTestnetProtectionRequest(BinanceTestnetActionRequest):
    trigger_price: Decimal = Field(gt=0)
    limit_price: Decimal = Field(gt=0)
