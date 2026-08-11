from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from trading_control_plane.domain import (
    CapitalDirection,
    CapitalTransferStatus,
    CapitalTreasuryProvider,
    DirectCapitalPath,
    Direction,
    RiskTier,
    TargetUrgency,
)

AccessRole = Literal[
    "OBSERVER",
    "PROPOSER",
    "REVIEWER",
    "OPERATOR",
    "TREASURY_ADMIN",
    "SYSTEM_ADMIN",
]
AgentAccessRole = Literal["OBSERVER", "PROPOSER", "REVIEWER"]
VenueScope = Literal["BINANCE", "HYPERLIQUID", "OKX", "BYBIT"]


class MockLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)


class PasswordLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=12, max_length=128)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=12, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)
    expected_auth_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ManagedUserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=12, max_length=128)
    roles: list[AccessRole] = Field(min_length=1, max_length=6)
    account_scope: str | None = Field(default=None, min_length=1, max_length=120)
    venue_scope: VenueScope | None = None

    @field_validator("roles")
    @classmethod
    def unique_roles(cls, value: list[AccessRole]) -> list[AccessRole]:
        if len(value) != len(set(value)):
            raise ValueError("roles must not contain duplicates")
        return value


class ManagedUserAccessRequest(BaseModel):
    roles: list[AccessRole] = Field(min_length=1, max_length=6)
    active: bool = True
    new_password: str | None = Field(default=None, min_length=12, max_length=128)
    account_scope: str | None = Field(default=None, min_length=1, max_length=120)
    venue_scope: VenueScope | None = None

    @field_validator("roles")
    @classmethod
    def unique_roles(cls, value: list[AccessRole]) -> list[AccessRole]:
        if len(value) != len(set(value)):
            raise ValueError("roles must not contain duplicates")
        return value


class AgentCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._-]+$")
    roles: list[AgentAccessRole] = Field(min_length=1, max_length=3)
    account_scope: str = Field(min_length=1, max_length=120)
    venue_scope: VenueScope
    expires_in_days: int = Field(default=90, ge=1, le=365)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @field_validator("roles")
    @classmethod
    def unique_roles(cls, value: list[AgentAccessRole]) -> list[AgentAccessRole]:
        if len(value) != len(set(value)):
            raise ValueError("roles must not contain duplicates")
        return value


class AgentAccessRequest(BaseModel):
    roles: list[AgentAccessRole] = Field(max_length=3)
    active: bool = True
    account_scope: str = Field(min_length=1, max_length=120)
    venue_scope: VenueScope
    expected_auth_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @field_validator("roles")
    @classmethod
    def unique_roles(cls, value: list[AgentAccessRole]) -> list[AgentAccessRole]:
        if len(value) != len(set(value)):
            raise ValueError("roles must not contain duplicates")
        return value

    @model_validator(mode="after")
    def require_active_role(self) -> AgentAccessRequest:
        if self.active and not self.roles:
            raise ValueError("an active agent requires a role")
        return self


class AgentTokenRotationRequest(BaseModel):
    expected_token_version: int = Field(ge=1)
    expires_in_days: int = Field(default=90, ge=1, le=365)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ApiClientCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._-]+$")
    workspace_id: UUID
    team_id: UUID
    account_id: str = Field(min_length=1, max_length=120)
    venue: VenueScope
    expires_in_days: int = Field(default=90, ge=1, le=365)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ApiClientStateRequest(BaseModel):
    active: bool
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ApiClientRevokeRequest(BaseModel):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=160)


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str | None = Field(default=None, min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=1, max_length=160)


class TeamCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str | None = Field(default=None, min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=1, max_length=160)


class TeamShadowActivationRequest(BaseModel):
    confirmation: Literal["SWITCH_TO_SHADOW"]
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=160)


class TeamTradingModeRequest(BaseModel):
    mode: Literal["LIVE", "SHADOW"]
    confirmation: Literal["SWITCH_TO_LIVE", "SWITCH_TO_SHADOW"]
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def confirmation_matches_mode(self) -> TeamTradingModeRequest:
        if self.confirmation != f"SWITCH_TO_{self.mode}":
            raise ValueError("confirmation must match selected trading mode")
        return self


class ShadowAccountResetRequest(BaseModel):
    confirmation: Literal["RESET_TO_100000_U"]
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ShadowOrderCreateRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=120)
    venue: VenueScope
    symbol: str | None = Field(default=None, min_length=1, max_length=120)
    catalog_instrument_id: UUID | None = None
    side: Literal["BUY", "SELL"]
    order_type: Literal["MARKET", "LIMIT"]
    quantity: Decimal = Field(gt=0)
    limit_price: Decimal | None = Field(default=None, gt=0)
    latest_price: Decimal | None = None
    observed_at: datetime | None = None
    price_tick: Decimal | None = None
    quantity_step: Decimal | None = None
    contract_multiplier: Decimal | None = None
    is_derivative: bool = True
    fee_bps: Decimal = Field(default=Decimal("4"), ge=0, le=100)
    slippage_bps: Decimal = Field(default=Decimal("2"), ge=0, le=500)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_order_shape(self) -> ShadowOrderCreateRequest:
        if self.symbol is None and self.catalog_instrument_id is None:
            raise ValueError("symbol or catalog_instrument_id is required")
        if self.order_type == "LIMIT" and self.limit_price is None:
            raise ValueError("LIMIT order requires limit_price")
        if self.order_type == "MARKET" and self.limit_price is not None:
            raise ValueError("MARKET order cannot include limit_price")
        return self


class ShadowOrderMatchRequest(BaseModel):
    expected_version: int = Field(ge=1)
    latest_price: Decimal | None = None
    observed_at: datetime | None = None
    price_tick: Decimal | None = None
    quantity_step: Decimal | None = None
    contract_multiplier: Decimal | None = None
    is_derivative: bool = True
    fee_bps: Decimal = Field(default=Decimal("4"), ge=0, le=100)
    slippage_bps: Decimal = Field(default=Decimal("2"), ge=0, le=500)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ShadowProtectionCreateRequest(BaseModel):
    trigger_type: Literal["STOP_LOSS", "TAKE_PROFIT"]
    execution_type: Literal["MARKET", "LIMIT"]
    trigger_price: Decimal = Field(gt=0)
    limit_price: Decimal | None = Field(default=None, gt=0)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_protection_shape(self) -> ShadowProtectionCreateRequest:
        if self.execution_type == "LIMIT" and self.limit_price is None:
            raise ValueError("LIMIT protection requires limit_price")
        if self.execution_type == "MARKET" and self.limit_price is not None:
            raise ValueError("MARKET protection cannot include limit_price")
        return self


class ShadowScopeInitializeRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=120)
    venue: VenueScope
    instrument_id: UUID
    currency: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._-]+$")
    initial_equity: Decimal | None = Field(default=None, gt=0)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class ShadowSimulationRequest(BaseModel):
    expected_version: int = Field(ge=1)
    reference_price: Decimal = Field(gt=0)
    fee_bps: Decimal = Field(default=Decimal("4"), ge=0, le=100)
    slippage_bps: Decimal = Field(default=Decimal("2"), ge=0, le=500)
    idempotency_key: str = Field(min_length=1, max_length=160)


class TeamMemberInviteRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9._-]+$")
    roles: list[AccessRole] = Field(min_length=1, max_length=6)
    account_scope: str | None = Field(default=None, min_length=1, max_length=120)
    venue_scope: VenueScope | None = None
    idempotency_key: str = Field(min_length=1, max_length=160)

    @field_validator("roles")
    @classmethod
    def unique_roles(cls, value: list[AccessRole]) -> list[AccessRole]:
        if len(value) != len(set(value)):
            raise ValueError("roles must not contain duplicates")
        return value


class ScopeSelectRequest(BaseModel):
    workspace_id: UUID
    team_id: UUID | None = None
    idempotency_key: str = Field(min_length=1, max_length=160)


class ExchangeCredentialRequest(BaseModel):
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=512)
    api_secret: SecretStr | None = Field(default=None, min_length=1, max_length=512)
    passphrase: SecretStr | None = Field(default=None, min_length=1, max_length=512)
    account_address: SecretStr | None = Field(default=None, min_length=1, max_length=120)
    api_wallet_address: SecretStr | None = Field(default=None, min_length=1, max_length=120)
    api_wallet_private_key: SecretStr | None = Field(default=None, min_length=1, max_length=512)

    def plaintext(self) -> dict[str, str]:
        return {
            field: value.get_secret_value()
            for field in (
                "api_key",
                "api_secret",
                "passphrase",
                "account_address",
                "api_wallet_address",
                "api_wallet_private_key",
            )
            if (value := getattr(self, field)) is not None
        }


class ExchangeAccountCreateRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=120)
    venue: VenueScope
    label: str | None = Field(default=None, min_length=1, max_length=120)
    credentials: ExchangeCredentialRequest | None = None
    idempotency_key: str = Field(min_length=1, max_length=160)


class ExchangeCredentialRotateRequest(BaseModel):
    credentials: ExchangeCredentialRequest
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ExchangeConnectionVerifyRequest(BaseModel):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ExchangeRuntimeSyncRequest(BaseModel):
    enabled: bool
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ExchangeTradingEligibilityRequest(BaseModel):
    enabled: bool
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=160)


class FreqtradeWorkerConfigureRequest(BaseModel):
    mode: Literal["UNCONFIGURED", "DRY_RUN", "LIVE"]
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$",
    )
    base_url: str | None = Field(default=None, min_length=1, max_length=2_048)
    username: SecretStr | None = Field(default=None, min_length=1, max_length=120)
    password: SecretStr | None = Field(default=None, min_length=1, max_length=2_048)
    hip3_dexes: list[str] = Field(default_factory=list, max_length=32)
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_configuration_shape(self) -> FreqtradeWorkerConfigureRequest:
        configured = self.mode != "UNCONFIGURED"
        provided = all(
            value is not None
            for value in (self.name, self.base_url, self.username, self.password)
        )
        if configured != provided:
            raise ValueError(
                "configured workers require name, base_url, username and password; "
                "unconfigured workers must omit them"
            )
        if not configured and self.hip3_dexes:
            raise ValueError("unconfigured workers must not include HIP-3 DEX scope")
        if len(self.hip3_dexes) != len(set(self.hip3_dexes)):
            raise ValueError("hip3_dexes must not contain duplicates")
        return self

    def plaintext_username(self) -> str | None:
        return None if self.username is None else self.username.get_secret_value()

    def plaintext_password(self) -> str | None:
        return None if self.password is None else self.password.get_secret_value()


class FreqtradeWorkerVerifyRequest(BaseModel):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=160)


class SignalSourceConfigureRequest(BaseModel):
    mode: Literal["PERPTAPE", "WEBHOOK"]
    secret: SecretStr = Field(min_length=8, max_length=512)
    enabled: bool = True
    webhook_max_age_seconds: int = Field(default=300, ge=30, le=900)
    expected_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=160)


NotificationChannel = Literal["TELEGRAM", "SLACK", "LARK", "EMAIL"]
NotificationEventType = Literal[
    "PROPOSAL_REVIEW_REQUIRED",
    "RISK_DECISION_RECORDED",
    "CAMPAIGN_STATUS_CHANGED",
    "CAPITAL_STATUS_CHANGED",
    "SIGNAL_EVENT_RECEIVED",
    "CONNECTION_CHECK_FAILED",
]


class NotificationRouteConfigurationRequest(BaseModel):
    bot_token: SecretStr | None = Field(default=None, min_length=20, max_length=2_048)
    chat_id: SecretStr | None = Field(default=None, min_length=1, max_length=120)
    webhook_url: SecretStr | None = Field(default=None, min_length=1, max_length=2_048)
    signing_secret: SecretStr | None = Field(default=None, min_length=1, max_length=2_048)
    smtp_host: SecretStr | None = Field(default=None, min_length=1, max_length=253)
    smtp_port: int | None = Field(default=None, ge=1, le=65_535)
    username: SecretStr | None = Field(default=None, min_length=1, max_length=2_048)
    password: SecretStr | None = Field(default=None, min_length=1, max_length=2_048)
    from_address: SecretStr | None = Field(default=None, min_length=3, max_length=255)
    to_address: SecretStr | None = Field(default=None, min_length=3, max_length=255)

    def plaintext(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for field_name in (
            "bot_token",
            "chat_id",
            "webhook_url",
            "signing_secret",
            "smtp_host",
            "username",
            "password",
            "from_address",
            "to_address",
        ):
            value = getattr(self, field_name)
            if value is not None:
                values[field_name] = value.get_secret_value()
        if self.smtp_port is not None:
            values["smtp_port"] = str(self.smtp_port)
        return values


class NotificationRouteWriteRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    channel: NotificationChannel
    event_types: list[NotificationEventType] = Field(min_length=1, max_length=6)
    enabled: bool = True
    configuration: NotificationRouteConfigurationRequest | None = None
    expected_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @field_validator("event_types")
    @classmethod
    def unique_notification_event_types(
        cls,
        value: list[NotificationEventType],
    ) -> list[NotificationEventType]:
        if len(value) != len(set(value)):
            raise ValueError("event_types must not contain duplicates")
        return value


class NotificationTestRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=160)


class WebhookSignalPayload(BaseModel):
    payload_version: Literal[1] = 1
    provider: Literal["TRADINGVIEW", "MODEL"]
    external_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    strategy_id: str = Field(min_length=1, max_length=120)
    strategy_version: str = Field(min_length=1, max_length=120)
    venue: VenueScope
    symbol: str = Field(min_length=1, max_length=120)
    direction: Direction
    signal_at: datetime
    timeframe: str | None = Field(default=None, min_length=1, max_length=32)
    reference_price: Decimal | None = Field(default=None, gt=0)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("signal_at")
    @classmethod
    def timezone_aware_signal(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("signal_at must include a timezone")
        return value


class SignalProposalRequest(BaseModel):
    environment: Literal["SHADOW", "TESTNET", "LIVE"] = "SHADOW"
    account_id: str = Field(min_length=1, max_length=120)
    instrument_id: UUID
    risk_tier: RiskTier
    quantity: Decimal = Field(gt=0)
    max_risk: Decimal = Field(gt=0)
    expires_in_minutes: int = Field(default=480, ge=480, le=1_440)
    rationale: str = Field(min_length=10, max_length=2_000)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ManualProposalRequest(BaseModel):
    environment: Literal["SHADOW", "TESTNET", "LIVE"] = "SHADOW"
    account_id: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=64)
    instrument_id: UUID
    direction: Direction
    risk_tier: RiskTier
    quantity: Decimal | None = Field(default=None, gt=0)
    max_position_notional: Decimal | None = Field(default=None, gt=0)
    initial_quantity: Decimal | None = Field(default=None, gt=0)
    initial_position_notional: Decimal | None = Field(default=None, gt=0)
    max_risk: Decimal = Field(gt=0)
    expires_in_minutes: int = Field(default=480, ge=480, le=1_440)
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
        if (self.quantity is None) == (self.max_position_notional is None):
            raise ValueError("provide exactly one of quantity or max_position_notional")
        if self.max_position_notional is not None:
            if self.initial_quantity is not None:
                raise ValueError("notional proposals cannot provide initial_quantity")
        elif self.initial_position_notional is not None:
            raise ValueError("quantity proposals cannot provide initial_position_notional")
        _validate_auto_add_fields(self)
        return self


class SystemProposalRequest(BaseModel):
    environment: Literal["SHADOW", "TESTNET", "LIVE"] = "SHADOW"
    account_id: str = Field(min_length=1, max_length=120)
    risk_tier: RiskTier
    quantity: Decimal = Field(gt=0)
    initial_quantity: Decimal | None = Field(default=None, gt=0)
    max_risk: Decimal = Field(gt=0)
    expires_in_minutes: int = Field(default=480, ge=480, le=1_440)
    invalidation_price: Decimal = Field(gt=0)
    allow_auto_add: bool = False
    requested_adds: int = Field(default=0, ge=0, le=3)
    add_trigger_price: Decimal | None = Field(default=None, gt=0)
    rationale: str = Field(min_length=3, max_length=2_000)
    configuration_mode: Literal["DEFAULT", "ADVANCED_OVERRIDE"] = "ADVANCED_OVERRIDE"
    default_config_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_auto_add_contract(self) -> SystemProposalRequest:
        _validate_auto_add_fields(self)
        if self.configuration_mode == "DEFAULT" and self.default_config_version is None:
            raise ValueError("DEFAULT mode requires default_config_version")
        if (
            self.configuration_mode == "ADVANCED_OVERRIDE"
            and self.default_config_version is not None
        ):
            raise ValueError("advanced override cannot claim a default config version")
        return self


class AgentProposalRequest(BaseModel):
    environment: Literal["SHADOW", "TESTNET", "LIVE"] = "SHADOW"
    account_id: str = Field(min_length=1, max_length=120)
    venue: VenueScope
    instrument_id: UUID
    direction: Direction
    risk_tier: RiskTier
    quantity: Decimal = Field(gt=0)
    max_risk: Decimal = Field(gt=0)
    expires_in_minutes: int = Field(default=480, ge=480, le=1_440)
    trigger_price: Decimal = Field(gt=0)
    invalidation_price: Decimal = Field(gt=0)
    limit_price: Decimal | None = Field(default=None, gt=0)
    model_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._:-]+$")
    model_version: str = Field(min_length=1, max_length=80)
    request_id: str = Field(
        min_length=16,
        max_length=80,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    generated_at: datetime
    rationale: str = Field(min_length=10, max_length=2_000)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @field_validator("generated_at")
    @classmethod
    def timezone_aware_generated_at(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("generated_at must include a timezone")
        return value


class ProposalDefaultConfigRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=120)
    risk_tier: RiskTier
    notional: Decimal = Field(gt=0)
    max_risk: Decimal = Field(gt=0)
    invalidation_bps: int = Field(ge=1, le=5_000)
    expires_in_minutes: int = Field(ge=480, le=1_440)
    rationale: str = Field(min_length=3, max_length=2_000)
    auto_proposal_enabled: bool = False
    auto_proposal_min_timeframes: Literal[3, 4] = 3
    idempotency_key: str = Field(min_length=1, max_length=160)


def _validate_auto_add_fields(value: ManualProposalRequest | SystemProposalRequest) -> None:
    if isinstance(value, ManualProposalRequest) and value.max_position_notional is not None:
        total_position = value.max_position_notional
        initial_position = (
            total_position
            if value.initial_position_notional is None
            else value.initial_position_notional
        )
    else:
        assert value.quantity is not None
        total_position = value.quantity
        initial_position = (
            value.quantity if value.initial_quantity is None else value.initial_quantity
        )
    if initial_position > total_position:
        raise ValueError("initial position cannot exceed the frozen total position cap")
    tier_limit = {RiskTier.LOW: 1, RiskTier.MEDIUM: 2, RiskTier.HIGH: 3}[value.risk_tier]
    if value.requested_adds > tier_limit:
        raise ValueError("requested_adds exceeds the selected risk tier limit")
    if value.allow_auto_add:
        if value.requested_adds == 0:
            raise ValueError("enabled AUTO_ADD requires at least one requested AddUnit")
        if initial_position >= total_position:
            raise ValueError("enabled AUTO_ADD requires position capacity after the initial order")
        if value.add_trigger_price is None:
            raise ValueError("enabled AUTO_ADD requires a frozen add trigger price")
    elif (
        value.requested_adds != 0
        or value.add_trigger_price is not None
        or initial_position != total_position
    ):
        raise ValueError("disabled AUTO_ADD cannot reserve AddUnits or Add quantity")


class ReviewRequest(BaseModel):
    decision: Literal["APPROVE", "REJECT"]
    reason: str = Field(min_length=2, max_length=1_000)
    expected_version: int = Field(ge=1)
    action_grant: str | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)


class MockStepUpRequest(BaseModel):
    action: Literal[
        "proposal.approve",
        "capital.approve",
        "risk.restore.review",
        "risk.restore.execute",
        "risk.restore.direct",
    ]
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


class CapitalBalanceFactRequest(BaseModel):
    environment: Literal["SHADOW", "TESTNET"] = "TESTNET"
    location_type: Literal["VAULT", "VENUE"]
    location_id: str = Field(min_length=1, max_length=160)
    venue: str = Field(min_length=1, max_length=64)
    equity: Decimal = Field(ge=0)
    available_balance: Decimal = Field(ge=0)
    withdrawable_balance: Decimal = Field(ge=0)
    asset: str = Field(min_length=1, max_length=32)
    control_status: Literal["CONTROLLED", "READ_ONLY", "UNKNOWN"]
    deposit_status: Literal["READY", "PENDING", "UNKNOWN"]
    network: str | None = Field(default=None, max_length=64)
    address_reference: str | None = Field(default=None, max_length=255)
    known: bool = True

    @field_validator("venue", "asset")
    @classmethod
    def normalize_capital_identifier(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def validate_balances(self) -> CapitalBalanceFactRequest:
        if self.withdrawable_balance > self.available_balance:
            raise ValueError("withdrawable_balance cannot exceed available_balance")
        if self.available_balance > self.equity:
            raise ValueError("available_balance cannot exceed equity")
        return self


class TransferProposalRequest(BaseModel):
    environment: Literal["SHADOW", "TESTNET", "LIVE"] = "TESTNET"
    direction: CapitalDirection
    account_id: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=64)
    vault_id: str = Field(min_length=1, max_length=160)
    asset: str = Field(min_length=1, max_length=32)
    network: str = Field(min_length=1, max_length=64)
    destination_reference: str = Field(min_length=1, max_length=255)
    amount: Decimal = Field(gt=0)
    max_fee: Decimal = Field(ge=0)
    min_received: Decimal = Field(gt=0)
    reason: str = Field(min_length=3, max_length=1_000)
    expires_in_minutes: int = Field(default=120, ge=5, le=1_440)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @field_validator("venue", "asset")
    @classmethod
    def normalize_transfer_identifier(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def validate_amounts(self) -> TransferProposalRequest:
        if self.min_received > self.amount:
            raise ValueError("min_received cannot exceed amount")
        if self.min_received + self.max_fee > self.amount:
            raise ValueError("minimum receipt and maximum fee cannot exceed gross amount")
        return self


class TransferReviewRequest(BaseModel):
    decision: Literal["APPROVE", "REJECT"]
    reason: str = Field(min_length=2, max_length=1_000)
    expected_version: int = Field(ge=1)
    action_grant: str | None = None


class TransferAuthorizationRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=160)
    expires_in_minutes: int = Field(default=30, ge=1, le=120)


class CapitalTransferCreateRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=160)


class DirectCapitalOperationRequest(BaseModel):
    path: DirectCapitalPath
    treasury_provider: CapitalTreasuryProvider | None = None
    amount: Decimal = Field(gt=0)
    final_confirmed: Literal[True]
    idempotency_key: str = Field(min_length=1, max_length=160)


class DirectCapitalUnsignedPlanRequest(BaseModel):
    expected_version: int = Field(ge=1)
    final_confirmed: Literal[True]
    idempotency_key: str = Field(min_length=1, max_length=160)


class DirectCapitalWalletSubmissionRequest(BaseModel):
    expected_version: int = Field(ge=1)
    stage: Literal[
        "HYPERLIQUID_DEPOSIT",
        "HYPERLIQUID_WITHDRAWAL",
        "HYPERLIQUID_CLASS_TRANSFER",
        "TREASURY_DEPOSIT",
    ]
    outcome: Literal["SUBMITTED", "CANCELLED"]
    transaction_hash: str | None = Field(default=None, pattern=r"^0x[0-9a-fA-F]{64}$")
    action_hash: str | None = Field(default=None, pattern=r"^0x[0-9a-fA-F]{64}$")
    nonce: int | None = Field(default=None, ge=1)
    final_confirmed: Literal[True]
    idempotency_key: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_wallet_result(self) -> DirectCapitalWalletSubmissionRequest:
        if self.outcome == "CANCELLED":
            if self.transaction_hash is not None or self.action_hash is not None:
                raise ValueError("cancelled wallet requests cannot include transaction evidence")
            return self
        if self.stage in {"HYPERLIQUID_DEPOSIT", "TREASURY_DEPOSIT"} and (
            self.transaction_hash is None
        ):
            raise ValueError("onchain wallet submission requires an Arbitrum transaction hash")
        if self.stage in {"HYPERLIQUID_WITHDRAWAL", "HYPERLIQUID_CLASS_TRANSFER"} and (
            self.action_hash is None or self.nonce is None
        ):
            raise ValueError("Hyperliquid withdrawal submission requires action hash and nonce")
        return self


class DirectCapitalHyperliquidReceiptRequest(BaseModel):
    expected_version: int = Field(ge=1)
    stage: Literal[
        "HYPERLIQUID_DEPOSIT_ARBITRUM",
        "HYPERLIQUID_DEPOSIT_LEDGER",
        "HYPERLIQUID_WITHDRAWAL_LEDGER",
        "HYPERLIQUID_WITHDRAWAL_ARBITRUM",
        "HYPERLIQUID_CLASS_TRANSFER_LEDGER",
    ]
    transaction_hash: str | None = Field(default=None, pattern=r"^0x[0-9a-fA-F]{64}$")
    action_hash: str | None = Field(default=None, pattern=r"^0x[0-9a-fA-F]{64}$")
    nonce: int | None = Field(default=None, ge=1)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_receipt_reference(self) -> DirectCapitalHyperliquidReceiptRequest:
        if self.stage.endswith("ARBITRUM") and self.transaction_hash is None:
            raise ValueError("Arbitrum receipt verification requires a transaction hash")
        if self.stage.endswith("LEDGER") and self.action_hash is None:
            raise ValueError("Hyperliquid ledger verification requires an action hash")
        if (
            self.stage
            in {
                "HYPERLIQUID_WITHDRAWAL_LEDGER",
                "HYPERLIQUID_CLASS_TRANSFER_LEDGER",
            }
            and self.nonce is None
        ):
            raise ValueError("withdrawal ledger verification requires the signed action nonce")
        return self


class DirectCapitalTreasuryReceiptRequest(BaseModel):
    expected_version: int = Field(ge=1)
    transaction_hash: str = Field(pattern=r"^0x[0-9a-fA-F]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=160)


class DirectCapitalBinanceSubmissionRequest(BaseModel):
    expected_version: int = Field(ge=1)
    final_confirmed: Literal[True]
    confirmation_phrase: Literal["CONFIRM_BINANCE_WITHDRAWAL"]
    idempotency_key: str = Field(min_length=1, max_length=160)


class DirectCapitalBinanceReceiptRequest(BaseModel):
    expected_version: int = Field(ge=1)
    stage: Literal["BINANCE_DEPOSIT", "BINANCE_WITHDRAWAL"]
    transaction_hash: str | None = Field(default=None, pattern=r"^0x[0-9a-fA-F]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_binance_receipt(self) -> DirectCapitalBinanceReceiptRequest:
        if self.stage == "BINANCE_DEPOSIT" and self.transaction_hash is None:
            raise ValueError("Binance deposit verification requires the Arbitrum transaction hash")
        return self


class DirectCapitalConfigurationRequest(BaseModel):
    network: Literal["ARBITRUM"] = "ARBITRUM"
    asset: Literal["USDC"] = "USDC"
    treasury_provider: CapitalTreasuryProvider | None = None
    vault_id: str | None = Field(default=None, min_length=1, max_length=160)
    vault_address: str | None = None
    owned_arbitrum_address: str | None = None
    binance_account_id: str | None = Field(default=None, min_length=1, max_length=120)
    binance_deposit_address: str | None = None
    binance_withdrawal_address: str | None = None
    hyperliquid_account_id: str | None = Field(default=None, min_length=1, max_length=120)
    hyperliquid_bridge_address: str | None = None
    safe_address: str | None = None
    safe_delegate_address: str | None = None
    max_amount: Decimal | None = Field(default=None, gt=0)
    max_fee: Decimal | None = Field(default=None, ge=0)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @field_validator(
        "vault_address",
        "owned_arbitrum_address",
        "binance_deposit_address",
        "binance_withdrawal_address",
        "hyperliquid_bridge_address",
        "safe_address",
        "safe_delegate_address",
    )
    @classmethod
    def validate_evm_address(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 42
            or not value.startswith("0x")
            or any(character not in "0123456789abcdefABCDEF" for character in value[2:])
        ):
            raise ValueError("capital addresses must be 20-byte EVM addresses")
        return None if value is None else value.lower()


class NoTiltReceiptRequest(BaseModel):
    transaction_hash: str = Field(pattern=r"^0x[0-9a-fA-F]{64}$")

    @field_validator("transaction_hash")
    @classmethod
    def normalize_transaction_hash(cls, value: str) -> str:
        return value.lower()


class CapitalTransferObservationRequest(BaseModel):
    status: Literal[
        "IN_FLIGHT",
        "DESTINATION_CONFIRMED",
        "UNKNOWN",
        "FAILED_SOURCE_RESTORED",
        "MANUAL_REQUIRED",
    ]
    transaction_reference: str | None = Field(default=None, max_length=255)
    fee_amount: Decimal | None = Field(default=None, ge=0)
    net_received: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_destination_evidence(self) -> CapitalTransferObservationRequest:
        if self.status == CapitalTransferStatus.DESTINATION_CONFIRMED.value and (
            self.fee_amount is None or self.net_received is None
        ):
            raise ValueError("destination confirmation requires fee_amount and net_received")
        return self


class CapitalScopeReconciliationRequest(BaseModel):
    environment: Literal["SHADOW", "TESTNET"] = "TESTNET"
    account_id: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=64)

    @field_validator("venue")
    @classmethod
    def normalize_capital_venue(cls, value: str) -> str:
        return value.upper()


class CapitalAutomationPolicyRequest(BaseModel):
    environment: Literal["SHADOW", "TESTNET"] = "TESTNET"
    account_id: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=64)
    vault_id: str = Field(min_length=1, max_length=160)
    asset: str = Field(min_length=1, max_length=32)
    network: str = Field(min_length=1, max_length=64)
    vault_destination_reference: str = Field(min_length=1, max_length=255)
    venue_destination_reference: str = Field(min_length=1, max_length=255)
    operating_low: Decimal = Field(ge=0)
    operating_target: Decimal = Field(ge=0)
    operating_high: Decimal = Field(ge=0)
    vault_minimum_reserve: Decimal = Field(ge=0)
    minimum_transfer: Decimal = Field(gt=0)
    maximum_transfer: Decimal = Field(gt=0)
    max_fee: Decimal = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=160)

    @field_validator("venue", "asset")
    @classmethod
    def normalize_automation_identifier(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def validate_thresholds(self) -> CapitalAutomationPolicyRequest:
        if not self.operating_low <= self.operating_target <= self.operating_high:
            raise ValueError("operating thresholds must be low <= target <= high")
        if self.maximum_transfer < self.minimum_transfer:
            raise ValueError("maximum_transfer cannot be below minimum_transfer")
        if self.max_fee >= self.minimum_transfer:
            raise ValueError("max_fee must be below minimum_transfer")
        return self


class CapitalAutomationEvaluateRequest(BaseModel):
    purpose: Literal["AUTO_PROFIT_SWEEP", "AUTO_OPERATING_REFILL"]
    idempotency_key: str = Field(min_length=1, max_length=160)


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


class RiskPolicyConfigureRequest(BaseModel):
    version: str = Field(min_length=1, max_length=120)
    max_total_risk: Decimal = Field(gt=0)
    max_account_risk: Decimal = Field(gt=0)
    max_single_loss: Decimal = Field(gt=0)
    max_consecutive_losses: int = Field(gt=0)
    loss_cooldown_seconds: int = Field(gt=0)
    max_fact_age_seconds: int = Field(gt=0)
    expected_revision: int = Field(ge=0)
    reason: str = Field(min_length=10, max_length=2_000)
    idempotency_key: str = Field(min_length=1, max_length=160)


class RiskControlChangeCreateRequest(BaseModel):
    reason: str = Field(min_length=10, max_length=2_000)
    restore_auto_add: bool = False
    idempotency_key: str = Field(min_length=1, max_length=160)


class RiskControlChangeReviewRequest(BaseModel):
    decision: Literal["APPROVE", "REJECT"]
    reason: str = Field(min_length=10, max_length=2_000)
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=160)
    action_grant: str | None = None


class RiskControlChangeExecuteRequest(BaseModel):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=160)
    action_grant: str


class RiskControlDirectRestoreRequest(BaseModel):
    reason: str = Field(min_length=10, max_length=2_000)
    idempotency_key: str = Field(min_length=1, max_length=160)
    action_grant: str


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


class FreqtradeLiveActionRequest(BinanceTestnetActionRequest):
    idempotency_key: str = Field(min_length=1, max_length=160)


class BinanceTestnetProtectionRequest(BinanceTestnetActionRequest):
    trigger_price: Decimal = Field(gt=0)


class HyperliquidTestnetProtectionRequest(BinanceTestnetActionRequest):
    trigger_price: Decimal = Field(gt=0)
    limit_price: Decimal = Field(gt=0)
