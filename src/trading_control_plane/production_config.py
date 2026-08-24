from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select

from trading_control_plane import domain, models
from trading_control_plane.config import Settings, get_settings
from trading_control_plane.credentials import CredentialCipher
from trading_control_plane.database import Database
from trading_control_plane.safe_spending import SafeSpendingGateway
from trading_control_plane.service import TradingService
from trading_control_plane.telegram import TelegramBotClient


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TeamConfiguration(_StrictModel):
    team_id: UUID
    workspace_id: UUID
    trading_enabled: bool
    mode: Literal["LIVE", "TESTNET"]


class GateConfiguration(_StrictModel):
    status: Literal["ENABLED", "DISABLED"]
    reason: str = Field(min_length=8, max_length=800)


class WorkerConfiguration(_StrictModel):
    mode: Literal["LIVE", "TESTNET"]
    required_status: Literal["VERIFIED"] = "VERIFIED"
    max_age_seconds: int = Field(default=900, ge=30, le=3_600)
    hip3_dexes: list[str] = Field(default_factory=list)


class FactConfiguration(_StrictModel):
    required: bool = True
    max_age_seconds: int = Field(default=900, ge=30, le=3_600)
    reconciliation_required: bool = True


class AccountConfiguration(_StrictModel):
    account_id: str = Field(min_length=1, max_length=120)
    venue: Literal["BINANCE", "HYPERLIQUID"]
    environment: Literal["LIVE", "TESTNET"]
    runtime_sync_enabled: bool = True
    trading_eligible: bool = True
    worker: WorkerConfiguration
    facts: FactConfiguration = Field(default_factory=FactConfiguration)

    @model_validator(mode="after")
    def validate_scope(self) -> AccountConfiguration:
        if self.account_id != self.account_id.strip():
            raise ValueError("account_id must be an exact identifier without surrounding space")
        if self.worker.mode != self.environment:
            raise ValueError("worker mode must match the exact account environment")
        if self.venue != "HYPERLIQUID" and self.worker.hip3_dexes:
            raise ValueError("HIP-3 DEX scope is valid only for Hyperliquid")
        if len(set(self.worker.hip3_dexes)) != len(self.worker.hip3_dexes):
            raise ValueError("HIP-3 DEX names must be unique")
        return self


class RiskConfiguration(_StrictModel):
    version: str = Field(min_length=1, max_length=120)
    system_state: Literal["NORMAL", "NO_PYRAMID", "REDUCE_ONLY", "KILL_SWITCH"]
    max_total_risk: Decimal = Field(gt=0)
    max_account_risk: Decimal = Field(gt=0)
    max_single_loss: Decimal = Field(gt=0)
    max_consecutive_losses: int = Field(gt=0)
    loss_cooldown_seconds: int = Field(gt=0)
    max_fact_age_seconds: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_limits(self) -> RiskConfiguration:
        if self.max_account_risk > self.max_total_risk:
            raise ValueError("max_account_risk cannot exceed max_total_risk")
        if self.max_single_loss > self.max_account_risk:
            raise ValueError("max_single_loss cannot exceed max_account_risk")
        return self


class ProposalConfiguration(_StrictModel):
    default_account_id: str = Field(min_length=1, max_length=120)
    notional: Decimal = Field(gt=0)
    max_risk: Decimal = Field(gt=0)
    risk_tier: Literal["LOW", "MEDIUM", "HIGH"]
    invalidation_bps: int = Field(ge=1, le=5_000)
    expires_in_minutes: int = Field(ge=480, le=1_440)
    rationale: str = Field(min_length=1, max_length=2_000)
    automatic_proposals: bool
    automatic_min_timeframes: Literal[3, 4]


class PerptapeConfiguration(_StrictModel):
    source_id: UUID
    enabled: bool = True
    require_fresh_feed: bool = True
    max_age_seconds: int = Field(default=900, ge=30, le=3_600)


class TelegramRouteConfiguration(_StrictModel):
    route_id: UUID
    name: str = Field(min_length=1, max_length=120)
    enabled: bool
    subscribed_events: list[str] = Field(min_length=1)
    reviewer_username: str | None = Field(default=None, min_length=1, max_length=120)
    reviewer_telegram_username: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_reviewer(self) -> TelegramRouteConfiguration:
        if "PROPOSAL_REVIEW_REQUIRED" in self.subscribed_events and (
            not self.reviewer_username or not self.reviewer_telegram_username
        ):
            raise ValueError(
                "proposal review routes require explicit internal and Telegram reviewer usernames"
            )
        if self.reviewer_telegram_username is not None:
            normalized = self.reviewer_telegram_username.strip().removeprefix("@").casefold()
            if not normalized or normalized != self.reviewer_telegram_username.casefold():
                raise ValueError("reviewer_telegram_username must be exact without @ or spaces")
        if len(set(self.subscribed_events)) != len(self.subscribed_events):
            raise ValueError("subscribed_events must be unique")
        return self


class CapitalRuntimeConfiguration(_StrictModel):
    safe_spending_enabled: bool
    safe_spending_arbitrum_rpc_url: str | None = None
    capital_arbitrum_rpc_url: str | None = None
    binance_capital_withdraw_enabled: bool

    @model_validator(mode="after")
    def validate_runtime(self) -> CapitalRuntimeConfiguration:
        if self.safe_spending_enabled and not self.safe_spending_arbitrum_rpc_url:
            raise ValueError("Safe spending requires an Arbitrum RPC URL")
        if self.binance_capital_withdraw_enabled and not self.capital_arbitrum_rpc_url:
            raise ValueError("Binance capital continuation requires an Arbitrum RPC URL")
        return self


class CapitalConfiguration(_StrictModel):
    environment: Literal["LIVE"] = "LIVE"
    network: Literal["ARBITRUM"] = "ARBITRUM"
    asset: Literal["USDC"] = "USDC"
    treasury_provider: Literal["SAFE_SPENDING_LIMIT", "NOTILT_VAULT"]
    vault_id: str | None = None
    vault_address: str | None = None
    owned_arbitrum_address: str | None = None
    binance_account_id: str
    binance_deposit_address: str
    binance_withdrawal_address: str
    hyperliquid_account_id: str
    hyperliquid_bridge_address: str
    safe_address: str | None = None
    safe_delegate_address: str | None = None
    max_amount: Decimal = Field(gt=0)
    max_fee: Decimal = Field(ge=0)
    enabled_paths: list[
        Literal[
            "VAULT_TO_BINANCE",
            "BINANCE_TO_VAULT",
            "VAULT_TO_HYPERLIQUID",
            "HYPERLIQUID_TO_VAULT",
        ]
    ] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_capital(self) -> CapitalConfiguration:
        if self.max_fee >= self.max_amount:
            raise ValueError("max_fee must be lower than max_amount")
        if len(set(self.enabled_paths)) != len(self.enabled_paths):
            raise ValueError("enabled_paths must be unique")
        if self.treasury_provider == "SAFE_SPENDING_LIMIT":
            if not self.safe_address or not self.safe_delegate_address:
                raise ValueError("Safe provider requires safe_address and safe_delegate_address")
            if self.binance_withdrawal_address.lower() != self.safe_address.lower():
                raise ValueError("Binance withdrawal address must be the selected Safe")
        elif not self.vault_id or not self.vault_address:
            raise ValueError("NoTilt provider requires vault_id and vault_address")
        return self


class CapitalAutomationPolicyConfiguration(_StrictModel):
    account_id: str = Field(min_length=1, max_length=120)
    venue: Literal["BINANCE", "HYPERLIQUID"]
    asset: str = Field(min_length=1, max_length=32)
    operating_low: Decimal = Field(ge=0)
    operating_target: Decimal = Field(ge=0)
    operating_high: Decimal = Field(ge=0)
    vault_minimum_reserve: Decimal = Field(ge=0)
    minimum_transfer: Decimal = Field(gt=0)
    maximum_transfer: Decimal = Field(gt=0)
    max_fee: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def validate_thresholds(self) -> CapitalAutomationPolicyConfiguration:
        if not self.operating_low <= self.operating_target <= self.operating_high:
            raise ValueError("operating thresholds must be low <= target <= high")
        if self.maximum_transfer < self.minimum_transfer:
            raise ValueError("maximum_transfer cannot be below minimum_transfer")
        if self.max_fee >= self.minimum_transfer:
            raise ValueError("max_fee must be below minimum_transfer")
        return self


class ProductionConfiguration(_StrictModel):
    schema_version: Literal[1]
    operator_username: str = Field(min_length=1, max_length=120)
    team: TeamConfiguration
    gates: dict[
        Literal[
            "LIVE_ORDER_SEND",
            "CAPITAL_TRANSFER",
            "AUTO_ADD",
            "AUTO_OPERATING_REFILL",
            "AUTO_PROFIT_SWEEP",
        ],
        GateConfiguration,
    ]
    accounts: list[AccountConfiguration] = Field(min_length=1)
    risk: RiskConfiguration
    proposals: ProposalConfiguration
    perptape: PerptapeConfiguration
    telegram_routes: list[TelegramRouteConfiguration]
    capital_runtime: CapitalRuntimeConfiguration
    capital: CapitalConfiguration
    capital_automation_policies: list[CapitalAutomationPolicyConfiguration]

    @model_validator(mode="after")
    def validate_complete_contract(self) -> ProductionConfiguration:
        required_gates = {
            "LIVE_ORDER_SEND",
            "CAPITAL_TRANSFER",
            "AUTO_ADD",
            "AUTO_OPERATING_REFILL",
            "AUTO_PROFIT_SWEEP",
        }
        if set(self.gates) != required_gates:
            raise ValueError("all five production gates must be explicit")
        scopes = {(item.environment, item.account_id, item.venue) for item in self.accounts}
        if len(scopes) != len(self.accounts):
            raise ValueError("account scopes must be unique")
        if self.proposals.default_account_id not in {item.account_id for item in self.accounts}:
            raise ValueError("proposal default_account_id must be one configured exact account")
        expected_capital_accounts = {
            (self.capital.binance_account_id, "BINANCE"),
            (self.capital.hyperliquid_account_id, "HYPERLIQUID"),
        }
        actual_accounts = {(item.account_id, item.venue) for item in self.accounts}
        if not expected_capital_accounts <= actual_accounts:
            raise ValueError("capital accounts must be configured exact exchange accounts")
        if self.team.mode != "LIVE" and any(
            gate.status == "ENABLED" for gate in self.gates.values()
        ):
            raise ValueError("TESTNET configuration cannot enable production gates")
        all_capital_paths = {
            "VAULT_TO_BINANCE",
            "BINANCE_TO_VAULT",
            "VAULT_TO_HYPERLIQUID",
            "HYPERLIQUID_TO_VAULT",
        }
        if (
            self.gates["CAPITAL_TRANSFER"].status == "ENABLED"
            and set(self.capital.enabled_paths) != all_capital_paths
        ):
            raise ValueError(
                "CAPITAL_TRANSFER requires all four configured Safe/Binance/Hyperliquid paths"
            )
        for key in ("AUTO_OPERATING_REFILL", "AUTO_PROFIT_SWEEP"):
            if self.gates[key].status == "ENABLED":
                raise ValueError(
                    f"{key} cannot be enabled until LIVE automation policies are supported"
                )
        if self.capital_automation_policies:
            raise ValueError(
                "LIVE capital automation policies are not supported; "
                "configure an explicit empty list"
            )
        return self


@dataclass(frozen=True, slots=True)
class ConfigurationCheck:
    path: str
    status: Literal["OK", "DRIFT", "BLOCKED"]
    code: str
    detail: str


def _reject_secret_keys(value: object, path: str = "root") -> None:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = key.lower().replace("-", "_")
            if any(
                marker in normalized
                for marker in ("api_key", "api_secret", "private_key", "password", "token")
            ):
                raise ValueError(f"{path}.{key}: secrets are forbidden in production.yaml")
            _reject_secret_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_keys(item, f"{path}[{index}]")


def load_production_configuration(path: str | Path) -> ProductionConfiguration:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("production.yaml must contain one mapping")
    _reject_secret_keys(raw)
    return ProductionConfiguration.model_validate(raw)


def _same_decimal(left: Decimal | None, right: Decimal) -> bool:
    return left is not None and left == right


def _scope_key(environment: str, account_id: str, venue: str) -> str:
    return f"{environment}:{account_id}:{venue}"


class ProductionConfigurator:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        configuration: ProductionConfiguration,
        *,
        now: datetime | None = None,
        safe_gateway: SafeSpendingGateway | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.configuration = configuration
        self.now = now or datetime.now(UTC)
        self.service = TradingService(
            database,
            credential_encryption_key=settings.credential_encryption_key,
        )
        self.safe_gateway = safe_gateway or SafeSpendingGateway(
            timeout_seconds=settings.safe_spending_gateway_timeout_seconds
        )
        self.credential_cipher = CredentialCipher(settings.credential_encryption_key)

    def _telegram_configuration(self, route: models.NotificationRoute) -> dict[str, str]:
        raw = self.credential_cipher.decrypt_secret(
            route.configuration_ciphertext,
            team_id=route.team_id,
            object_id=route.notification_route_id,
            purpose=(
                "notification-route:telegram"
                if route.environment == "LIVE"
                else "notification-route:testnet:telegram"
            ),
            credential_version=route.credential_version,
        )
        decoded = json.loads(raw)
        if not isinstance(decoded, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in decoded.items()
        ):
            raise domain.DomainRejected(
                "NOTIFICATION_CONFIGURATION_UNAVAILABLE",
                "Telegram route configuration is invalid",
            )
        return decoded

    @staticmethod
    def _telegram_private_identity(
        configuration: dict[str, str], expected_username: str
    ) -> tuple[str, str]:
        response = TelegramBotClient(configuration["bot_token"]).call(
            "getChat", {"chat_id": configuration["chat_id"]}
        )
        chat = response.get("result")
        if not isinstance(chat, dict):
            raise domain.DomainRejected(
                "TELEGRAM_IDENTITY_UNAVAILABLE", "Telegram getChat returned no identity"
            )
        chat_type = str(chat.get("type", ""))
        username = str(chat.get("username", "")).strip().removeprefix("@").casefold()
        if chat_type != "private" or username != expected_username.casefold():
            raise domain.DomainRejected(
                "TELEGRAM_REVIEWER_IDENTITY_MISMATCH",
                "Telegram route must target the configured reviewer private chat",
            )
        return str(chat.get("id", configuration["chat_id"])), username

    def _identity(self, session: Any) -> tuple[models.User | None, models.Team | None]:
        operator = session.scalar(
            select(models.User).where(
                models.User.username == self.configuration.operator_username,
                models.User.active,
                models.User.principal_type == "HUMAN",
            )
        )
        team = session.get(models.Team, self.configuration.team.team_id)
        return operator, team

    @staticmethod
    def _check(
        checks: list[ConfigurationCheck],
        path: str,
        condition: bool,
        code: str,
        detail: str,
        *,
        mutable: bool = False,
    ) -> None:
        checks.append(
            ConfigurationCheck(
                path=path,
                status="OK" if condition else "DRIFT" if mutable else "BLOCKED",
                code="MATCH" if condition else code,
                detail="configured" if condition else detail,
            )
        )

    def checks(self) -> list[ConfigurationCheck]:
        config = self.configuration
        checks: list[ConfigurationCheck] = []
        with self.database.session_factory() as session:
            operator, team = self._identity(session)
            self._check(
                checks,
                "operator_username",
                operator is not None,
                "OPERATOR_NOT_FOUND",
                "configured operator must be one active HUMAN user",
            )
            if operator is None:
                return checks
            role = session.scalar(
                select(models.RoleAssignment.assignment_id).where(
                    models.RoleAssignment.user_id == operator.user_id,
                    models.RoleAssignment.team_id == config.team.team_id,
                    models.RoleAssignment.role == "SYSTEM_ADMIN",
                )
            )
            self._check(
                checks,
                "operator_username",
                role is not None,
                "SYSTEM_ADMIN_REQUIRED",
                "configuration apply requires SYSTEM_ADMIN in the exact team",
            )
            self._check(
                checks,
                "team.team_id",
                team is not None and team.workspace_id == config.team.workspace_id and team.active,
                "TEAM_SCOPE_MISMATCH",
                "team/workspace scope is missing, inactive, or mismatched",
            )
            if team is None:
                return checks
            self._check(
                checks,
                "team.mode",
                team.execution_mode == config.team.mode,
                "TEAM_MODE_DRIFT",
                "team execution mode differs",
                mutable=True,
            )
            self._check(
                checks,
                "team.trading_enabled",
                team.trading_enabled == config.team.trading_enabled,
                "TEAM_TRADING_DRIFT",
                "team trading_enabled differs",
                mutable=(config.team.trading_enabled and team.execution_mode != config.team.mode),
            )

            accounts: dict[tuple[str, str], models.ExchangeAccount] = {}
            for expected_account in config.accounts:
                account = session.scalar(
                    select(models.ExchangeAccount).where(
                        models.ExchangeAccount.team_id == team.team_id,
                        models.ExchangeAccount.environment == expected_account.environment,
                        models.ExchangeAccount.account_id == expected_account.account_id,
                        models.ExchangeAccount.venue == expected_account.venue,
                        models.ExchangeAccount.deleted_at.is_(None),
                    )
                )
                path = f"accounts.{expected_account.venue}.{expected_account.account_id}"
                self._check(
                    checks,
                    path,
                    account is not None,
                    "EXACT_ACCOUNT_MISSING",
                    "exact account scope is missing",
                )
                if account is None:
                    continue
                accounts[(expected_account.account_id, expected_account.venue)] = account
                self._check(
                    checks,
                    f"{path}.connection",
                    account.active
                    and account.connection_status == "VERIFIED"
                    and account.credentials_ciphertext is not None
                    and account.credential_version >= 1,
                    "ACCOUNT_CONNECTION_NOT_VERIFIED",
                    "active verified encrypted exchange credentials are required",
                )
                self._check(
                    checks,
                    f"{path}.runtime_sync_enabled",
                    account.runtime_sync_enabled == expected_account.runtime_sync_enabled,
                    "RUNTIME_SYNC_DRIFT",
                    "continuous fact sync state differs",
                    mutable=True,
                )
                self._check(
                    checks,
                    f"{path}.trading_eligible",
                    (account.trading_status == "ELIGIBLE") == expected_account.trading_eligible,
                    "TRADING_ELIGIBILITY_DRIFT",
                    "exact account trading eligibility differs",
                    mutable=True,
                )
                worker_fresh = (
                    account.freqtrade_last_verified_at is not None
                    and self.now - account.freqtrade_last_verified_at
                    <= timedelta(seconds=expected_account.worker.max_age_seconds)
                )
                self._check(
                    checks,
                    f"{path}.worker",
                    account.freqtrade_worker_mode == expected_account.worker.mode
                    and account.freqtrade_worker_status == expected_account.worker.required_status
                    and bool(account.freqtrade_runtime_fingerprint)
                    and worker_fresh,
                    "FREQTRADE_WORKER_NOT_READY",
                    "verified fresh LIVE Worker and runtime fingerprint are required",
                )
                self._check(
                    checks,
                    f"{path}.worker.hip3_dexes",
                    sorted(account.freqtrade_hip3_dexes or [])
                    == sorted(expected_account.worker.hip3_dexes),
                    "FREQTRADE_HIP3_SCOPE_DRIFT",
                    "Hyperliquid HIP-3 DEX scope differs",
                )
                if expected_account.facts.required:
                    runtime_source = session.scalar(
                        select(models.RuntimeSourceHealth).where(
                            models.RuntimeSourceHealth.team_id == team.team_id,
                            models.RuntimeSourceHealth.environment == expected_account.environment,
                            models.RuntimeSourceHealth.account_id == expected_account.account_id,
                            models.RuntimeSourceHealth.venue == expected_account.venue,
                            models.RuntimeSourceHealth.source_name == expected_account.venue,
                        )
                    )
                    fresh = (
                        runtime_source is not None
                        and runtime_source.status == "SUCCESS"
                        and runtime_source.last_success_at is not None
                        and self.now - runtime_source.last_success_at
                        <= timedelta(seconds=expected_account.facts.max_age_seconds)
                    )
                    self._check(
                        checks,
                        f"{path}.facts",
                        fresh,
                        "ACCOUNT_FACTS_NOT_CURRENT",
                        "balance, position, order and catalog facts must be current",
                    )
                if expected_account.facts.reconciliation_required:
                    scope = _scope_key(
                        expected_account.environment,
                        expected_account.account_id,
                        expected_account.venue,
                    )
                    reconciliation = session.scalar(
                        select(models.ReconciliationRun)
                        .where(
                            models.ReconciliationRun.team_id == team.team_id,
                            models.ReconciliationRun.execution_scope == scope,
                            models.ReconciliationRun.campaign_id.is_(None),
                            models.ReconciliationRun.is_computed,
                        )
                        .order_by(models.ReconciliationRun.completed_at.desc())
                    )
                    reconciled = (
                        reconciliation is not None
                        and reconciliation.status == "MATCH"
                        and self.now - reconciliation.completed_at
                        <= timedelta(seconds=expected_account.facts.max_age_seconds)
                    )
                    self._check(
                        checks,
                        f"{path}.reconciliation",
                        reconciled,
                        "ACCOUNT_RECONCILIATION_NOT_CURRENT",
                        "fresh computed MATCH reconciliation is required",
                    )

            policy = session.scalar(
                select(models.RiskPolicy).where(
                    models.RiskPolicy.team_id == team.team_id,
                    models.RiskPolicy.active,
                )
            )
            expected_risk = config.risk
            risk_matches = (
                policy is not None
                and policy.version == expected_risk.version
                and policy.system_state == expected_risk.system_state
                and _same_decimal(policy.max_total_risk, expected_risk.max_total_risk)
                and _same_decimal(policy.max_account_risk, expected_risk.max_account_risk)
                and _same_decimal(policy.max_single_loss, expected_risk.max_single_loss)
                and policy.max_consecutive_losses == expected_risk.max_consecutive_losses
                and policy.loss_cooldown_seconds == expected_risk.loss_cooldown_seconds
                and policy.max_fact_age_seconds == expected_risk.max_fact_age_seconds
            )
            self._check(
                checks,
                "risk",
                risk_matches,
                "RISK_POLICY_DRIFT",
                "active risk policy differs; bump version for a deliberate policy change",
                mutable=policy is None or policy.version != expected_risk.version,
            )

            proposal = session.scalar(
                select(models.ProposalDefaultConfig).where(
                    models.ProposalDefaultConfig.team_id == team.team_id,
                    models.ProposalDefaultConfig.active,
                )
            )
            expected_proposal = config.proposals
            proposal_matches = (
                proposal is not None
                and proposal.account_id == expected_proposal.default_account_id
                and proposal.risk_tier == expected_proposal.risk_tier
                and _same_decimal(proposal.notional, expected_proposal.notional)
                and _same_decimal(proposal.max_risk, expected_proposal.max_risk)
                and proposal.invalidation_bps == expected_proposal.invalidation_bps
                and proposal.expires_in_minutes == expected_proposal.expires_in_minutes
                and proposal.rationale == expected_proposal.rationale
                and proposal.auto_proposal_enabled == expected_proposal.automatic_proposals
                and proposal.auto_proposal_min_timeframes
                == expected_proposal.automatic_min_timeframes
            )
            self._check(
                checks,
                "proposals",
                proposal_matches,
                "PROPOSAL_DEFAULTS_DRIFT",
                "proposal defaults differ",
                mutable=True,
            )

            signal_source = session.scalar(
                select(models.TeamSignalSource).where(
                    models.TeamSignalSource.signal_source_id == config.perptape.source_id,
                    models.TeamSignalSource.team_id == team.team_id,
                    models.TeamSignalSource.mode == "PERPTAPE",
                    models.TeamSignalSource.deleted_at.is_(None),
                )
            )
            self._check(
                checks,
                "perptape.source_id",
                signal_source is not None and signal_source.credential_ciphertext is not None,
                "PERPTAPE_SOURCE_NOT_CONFIGURED",
                "exact encrypted Perptape source is missing",
            )
            if signal_source is not None:
                self._check(
                    checks,
                    "perptape.enabled",
                    signal_source.enabled == config.perptape.enabled,
                    "PERPTAPE_STATE_DRIFT",
                    "Perptape enabled state differs",
                    mutable=True,
                )
            if config.perptape.require_fresh_feed:
                latest_feed = session.scalar(
                    select(models.PerptapeFeed)
                    .where(models.PerptapeFeed.team_id == team.team_id)
                    .order_by(models.PerptapeFeed.fetched_at.desc())
                )
                self._check(
                    checks,
                    "perptape.feed",
                    latest_feed is not None
                    and self.now - latest_feed.fetched_at
                    <= timedelta(seconds=config.perptape.max_age_seconds),
                    "PERPTAPE_FEED_NOT_CURRENT",
                    "fresh Perptape feed is required",
                )

            for expected_route in config.telegram_routes:
                route = session.scalar(
                    select(models.NotificationRoute).where(
                        models.NotificationRoute.notification_route_id == expected_route.route_id,
                        models.NotificationRoute.team_id == team.team_id,
                        models.NotificationRoute.deleted_at.is_(None),
                    )
                )
                path = f"telegram_routes.{expected_route.route_id}"
                self._check(
                    checks,
                    path,
                    route is not None
                    and route.channel == "TELEGRAM"
                    and bool(route.configuration_ciphertext),
                    "TELEGRAM_ROUTE_NOT_CONFIGURED",
                    "exact encrypted Telegram route is missing",
                )
                if route is None:
                    continue
                self._check(
                    checks,
                    f"{path}.settings",
                    route.name == expected_route.name
                    and route.enabled == expected_route.enabled
                    and sorted(route.event_types) == sorted(expected_route.subscribed_events),
                    "TELEGRAM_ROUTE_DRIFT",
                    "route name, state, or subscribed events differ",
                    mutable=True,
                )
                reviewer = (
                    None
                    if expected_route.reviewer_username is None
                    else session.scalar(
                        select(models.User).where(
                            models.User.username == expected_route.reviewer_username,
                            models.User.active,
                            models.User.principal_type == "HUMAN",
                        )
                    )
                )
                if expected_route.reviewer_username is not None:
                    self._check(
                        checks,
                        f"{path}.reviewer",
                        reviewer is not None
                        and reviewer.telegram_chat_id is not None
                        and route.recipient_user_id == reviewer.user_id,
                        "TELEGRAM_REVIEWER_BINDING_MISSING",
                        "configured private Telegram identity is not bound to this reviewer",
                        mutable=reviewer is not None,
                    )

            active_capital_policies = session.scalars(
                select(models.CapitalAutomationPolicy).where(
                    models.CapitalAutomationPolicy.team_id == team.team_id,
                    models.CapitalAutomationPolicy.environment == "LIVE",
                    models.CapitalAutomationPolicy.active,
                )
            ).all()
            self._check(
                checks,
                "capital_automation_policies",
                not active_capital_policies,
                "UNEXPECTED_LIVE_CAPITAL_AUTOMATION_POLICY",
                "LIVE capital automation remains unsupported; remove active policies before apply",
            )

            capital = session.scalar(
                select(models.DirectCapitalConfiguration).where(
                    models.DirectCapitalConfiguration.team_id == team.team_id,
                    models.DirectCapitalConfiguration.environment == "LIVE",
                    models.DirectCapitalConfiguration.active,
                )
            )
            expected_capital = config.capital
            capital_matches = (
                capital is not None
                and capital.network == expected_capital.network
                and capital.asset == expected_capital.asset
                and capital.treasury_provider == expected_capital.treasury_provider
                and capital.vault_id == expected_capital.vault_id
                and capital.vault_address == expected_capital.vault_address
                and capital.owned_arbitrum_address == expected_capital.owned_arbitrum_address
                and capital.binance_account_id == expected_capital.binance_account_id
                and capital.binance_deposit_address == expected_capital.binance_deposit_address
                and capital.binance_withdrawal_address
                == expected_capital.binance_withdrawal_address
                and capital.hyperliquid_account_id == expected_capital.hyperliquid_account_id
                and capital.hyperliquid_bridge_address
                == expected_capital.hyperliquid_bridge_address
                and capital.safe_address == expected_capital.safe_address
                and capital.safe_delegate_address == expected_capital.safe_delegate_address
                and _same_decimal(capital.max_amount, expected_capital.max_amount)
                and _same_decimal(capital.max_fee, expected_capital.max_fee)
            )
            self._check(
                checks,
                "capital",
                capital_matches,
                "CAPITAL_CONFIGURATION_DRIFT",
                "versioned direct capital configuration differs",
                mutable=True,
            )
            runtime = config.capital_runtime
            runtime_matches = (
                self.settings.safe_spending_enabled == runtime.safe_spending_enabled
                and self.settings.safe_spending_arbitrum_rpc_url
                == runtime.safe_spending_arbitrum_rpc_url
                and self.settings.capital_arbitrum_rpc_url == runtime.capital_arbitrum_rpc_url
                and self.settings.binance_capital_withdraw_enabled
                == runtime.binance_capital_withdraw_enabled
            )
            self._check(
                checks,
                "capital_runtime",
                runtime_matches,
                "CAPITAL_RUNTIME_DRIFT",
                "server runtime flags/RPC configuration differ",
            )
            if (
                expected_capital.treasury_provider == "SAFE_SPENDING_LIMIT"
                and runtime.safe_spending_enabled
                and runtime.safe_spending_arbitrum_rpc_url
                and expected_capital.safe_address
                and expected_capital.safe_delegate_address
            ):
                try:
                    safe = self.safe_gateway.read_limit(
                        rpc_url=runtime.safe_spending_arbitrum_rpc_url,
                        safe=expected_capital.safe_address,
                        delegate=expected_capital.safe_delegate_address,
                    )
                    safe_ready = (
                        bool(safe.get("moduleEnabled"))
                        and Decimal(str(safe.get("available", "0"))) > 0
                    )
                except domain.DomainRejected as exc:
                    self._check(
                        checks,
                        "capital.safe",
                        False,
                        exc.code,
                        "Safe Allowance Module read-only preflight failed",
                    )
                else:
                    self._check(
                        checks,
                        "capital.safe",
                        safe_ready,
                        "SAFE_ALLOWANCE_NOT_READY",
                        "Safe module must be enabled with positive available allowance",
                    )

            for key, expected_gate in config.gates.items():
                gate = session.get(models.CapabilityGate, key)
                self._check(
                    checks,
                    f"gates.{key}",
                    gate is not None
                    and gate.status == expected_gate.status
                    and gate.reason == expected_gate.reason,
                    "CAPABILITY_GATE_DRIFT",
                    "gate status or audited reason differs",
                    mutable=True,
                )
                if expected_gate.status == "ENABLED" and key == "LIVE_ORDER_SEND":
                    ready_accounts = all(
                        account is not None
                        and account.connection_status == "VERIFIED"
                        and account.runtime_sync_enabled
                        and account.trading_status == "ELIGIBLE"
                        and account.freqtrade_worker_mode == "LIVE"
                        and account.freqtrade_worker_status == "VERIFIED"
                        and bool(account.freqtrade_runtime_fingerprint)
                        for account in accounts.values()
                    ) and len(accounts) == len(config.accounts)
                    self._check(
                        checks,
                        "gates.LIVE_ORDER_SEND.preconditions",
                        team.execution_mode == "LIVE"
                        and team.trading_enabled
                        and ready_accounts
                        and policy is not None
                        and policy.system_state == "NORMAL",
                        "LIVE_ORDER_SEND_PRECONDITIONS_FAILED",
                        "LIVE team, NORMAL risk and every exact account/Worker are required",
                    )
                if expected_gate.status == "ENABLED" and key == "CAPITAL_TRANSFER":
                    capital_ready = (
                        capital_matches
                        and runtime_matches
                        and expected_capital.max_amount > expected_capital.max_fee
                        and (expected_capital.binance_account_id, "BINANCE") in accounts
                        and (expected_capital.hyperliquid_account_id, "HYPERLIQUID") in accounts
                    )
                    self._check(
                        checks,
                        "gates.CAPITAL_TRANSFER.preconditions",
                        capital_ready,
                        "CAPITAL_TRANSFER_PRECONDITIONS_FAILED",
                        (
                            "exact capital accounts, runtime switches, Safe and bounded limits "
                            "are required"
                        ),
                    )
                if expected_gate.status == "ENABLED" and key == "AUTO_ADD":
                    self._check(
                        checks,
                        "gates.AUTO_ADD.preconditions",
                        gate is not None and gate.status == "ENABLED",
                        "REVIEWED_RESTORE_REQUIRED",
                        "AUTO_ADD can only be enabled through the reviewed restore workflow",
                    )
        return checks

    def _hash(self) -> str:
        raw = self.configuration.model_dump(mode="json")
        encoded = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()[:20]

    def _key(self, step: str, version: int) -> str:
        return f"production-config:{self._hash()}:{step}:v{version}"

    def apply(self) -> list[str]:
        config = self.configuration
        applied: list[str] = []
        with self.database.session_factory() as session:
            operator, team = self._identity(session)
            if operator is None or team is None:
                raise domain.DomainRejected(
                    "PRODUCTION_CONFIG_SCOPE_INVALID",
                    "operator and exact team/workspace must exist before apply",
                )
            actor_id = operator.user_id
            if team.workspace_id != config.team.workspace_id:
                raise domain.DomainRejected(
                    "PRODUCTION_CONFIG_SCOPE_INVALID", "workspace/team scope mismatch"
                )

        if team.execution_mode != config.team.mode:
            result = self.service.set_team_execution_mode(
                actor_id=actor_id,
                team_id=team.team_id,
                mode=config.team.mode,
                confirmation=(
                    "I_CONFIRM_LIVE_PRODUCTION_MONEY"
                    if config.team.mode == "LIVE"
                    else "SWITCH_TO_TESTNET"
                ),
                expected_version=team.version,
                idempotency_key=self._key("team-mode", team.version),
                now=self.now,
            )
            applied.append("team.mode")
            team.version = int(result["version"])
            team.execution_mode = config.team.mode
            team.trading_enabled = True
        if team.trading_enabled != config.team.trading_enabled:
            raise domain.DomainRejected(
                "TEAM_TRADING_STATE_REQUIRES_MODE_CHANGE",
                "team trading state is changed only by the existing execution-mode workflow",
            )

        for expected_account in config.accounts:
            with self.database.session_factory() as session:
                account = session.scalar(
                    select(models.ExchangeAccount).where(
                        models.ExchangeAccount.team_id == team.team_id,
                        models.ExchangeAccount.environment == expected_account.environment,
                        models.ExchangeAccount.account_id == expected_account.account_id,
                        models.ExchangeAccount.venue == expected_account.venue,
                        models.ExchangeAccount.deleted_at.is_(None),
                    )
                )
            if account is None:
                raise domain.DomainRejected("EXACT_ACCOUNT_MISSING", expected_account.account_id)
            if account.runtime_sync_enabled != expected_account.runtime_sync_enabled:
                result = self.service.configure_exchange_account_runtime_sync(
                    account.exchange_account_id,
                    actor_id=actor_id,
                    enabled=expected_account.runtime_sync_enabled,
                    expected_version=account.version,
                    idempotency_key=self._key(
                        f"account-runtime-{account.exchange_account_id}", account.version
                    ),
                    now=self.now,
                )
                account.version = int(result["version"])
                account.runtime_sync_enabled = expected_account.runtime_sync_enabled
                applied.append(
                    f"accounts.{expected_account.venue}.{expected_account.account_id}.runtime_sync"
                )
            desired_eligible = "ELIGIBLE" if expected_account.trading_eligible else "DISABLED"
            if account.trading_status != desired_eligible:
                self.service.configure_exchange_account_trading(
                    account.exchange_account_id,
                    actor_id=actor_id,
                    enabled=expected_account.trading_eligible,
                    expected_version=account.version,
                    idempotency_key=self._key(
                        f"account-trading-{account.exchange_account_id}", account.version
                    ),
                    now=self.now,
                )
                applied.append(
                    f"accounts.{expected_account.venue}.{expected_account.account_id}.trading"
                )

        with self.database.session_factory() as session:
            policy = session.scalar(
                select(models.RiskPolicy).where(
                    models.RiskPolicy.team_id == team.team_id,
                    models.RiskPolicy.active,
                )
            )
        expected_risk = config.risk
        risk_matches = (
            policy is not None
            and policy.version == expected_risk.version
            and policy.system_state == expected_risk.system_state
            and _same_decimal(policy.max_total_risk, expected_risk.max_total_risk)
            and _same_decimal(policy.max_account_risk, expected_risk.max_account_risk)
            and _same_decimal(policy.max_single_loss, expected_risk.max_single_loss)
            and policy.max_consecutive_losses == expected_risk.max_consecutive_losses
            and policy.loss_cooldown_seconds == expected_risk.loss_cooldown_seconds
            and policy.max_fact_age_seconds == expected_risk.max_fact_age_seconds
        )
        if not risk_matches:
            if policy is not None and policy.version == expected_risk.version:
                raise domain.DomainRejected(
                    "RISK_POLICY_VERSION_CONFLICT",
                    "bump risk.version for a deliberate policy change",
                )
            self.service.configure_risk_policy(
                actor_id=actor_id,
                version=expected_risk.version,
                max_total_risk=expected_risk.max_total_risk,
                max_account_risk=expected_risk.max_account_risk,
                max_single_loss=expected_risk.max_single_loss,
                max_consecutive_losses=expected_risk.max_consecutive_losses,
                loss_cooldown=timedelta(seconds=expected_risk.loss_cooldown_seconds),
                max_fact_age=timedelta(seconds=expected_risk.max_fact_age_seconds),
                expected_revision=0 if policy is None else policy.revision,
                reason="production.yaml",
                idempotency_key=self._key("risk", 0 if policy is None else policy.revision),
                now=self.now,
            )
            applied.append("risk")

        with self.database.session_factory() as session:
            proposal = session.scalar(
                select(models.ProposalDefaultConfig).where(
                    models.ProposalDefaultConfig.team_id == team.team_id,
                    models.ProposalDefaultConfig.active,
                )
            )
        expected_proposal = config.proposals
        proposal_matches = (
            proposal is not None
            and proposal.account_id == expected_proposal.default_account_id
            and proposal.risk_tier == expected_proposal.risk_tier
            and _same_decimal(proposal.notional, expected_proposal.notional)
            and _same_decimal(proposal.max_risk, expected_proposal.max_risk)
            and proposal.invalidation_bps == expected_proposal.invalidation_bps
            and proposal.expires_in_minutes == expected_proposal.expires_in_minutes
            and proposal.rationale == expected_proposal.rationale
            and proposal.auto_proposal_enabled == expected_proposal.automatic_proposals
            and proposal.auto_proposal_min_timeframes == expected_proposal.automatic_min_timeframes
        )
        if not proposal_matches:
            self.service.set_proposal_default_config(
                actor_id,
                self._key("proposal-defaults", 0 if proposal is None else proposal.version),
                account_id=expected_proposal.default_account_id,
                risk_tier=domain.RiskTier(expected_proposal.risk_tier),
                notional=expected_proposal.notional,
                max_risk=expected_proposal.max_risk,
                invalidation_bps=expected_proposal.invalidation_bps,
                expires_in_minutes=expected_proposal.expires_in_minutes,
                rationale=expected_proposal.rationale,
                auto_proposal_enabled=expected_proposal.automatic_proposals,
                auto_proposal_min_timeframes=expected_proposal.automatic_min_timeframes,
                now=self.now,
            )
            applied.append("proposals")

        with self.database.session_factory() as session:
            signal_source = session.get(models.TeamSignalSource, config.perptape.source_id)
        if (
            signal_source is None
            or signal_source.team_id != team.team_id
            or signal_source.deleted_at is not None
        ):
            raise domain.DomainRejected(
                "PERPTAPE_SOURCE_NOT_CONFIGURED", "exact Perptape source is missing"
            )
        if signal_source.enabled != config.perptape.enabled:
            self.service.set_signal_source_enabled(
                signal_source.signal_source_id,
                actor_id=actor_id,
                enabled=config.perptape.enabled,
                expected_version=signal_source.version,
                idempotency_key=self._key("perptape", signal_source.version),
                now=self.now,
            )
            applied.append("perptape.enabled")

        for expected_route in config.telegram_routes:
            with self.database.session_factory() as session:
                route = session.get(models.NotificationRoute, expected_route.route_id)
            if route is None or route.team_id != team.team_id or route.deleted_at is not None:
                raise domain.DomainRejected(
                    "TELEGRAM_ROUTE_NOT_CONFIGURED", str(expected_route.route_id)
                )
            reviewer = None
            route_configuration: dict[str, str] | None = None
            if expected_route.reviewer_username:
                with self.database.session_factory() as session:
                    reviewer = session.scalar(
                        select(models.User).where(
                            models.User.username == expected_route.reviewer_username,
                            models.User.active,
                            models.User.principal_type == "HUMAN",
                        )
                    )
                if reviewer is None:
                    raise domain.DomainRejected(
                        "TELEGRAM_REVIEWER_NOT_FOUND", expected_route.reviewer_username
                    )
                route_configuration = self._telegram_configuration(route)
                assert expected_route.reviewer_telegram_username is not None
                telegram_chat_id, normalized_username = self._telegram_private_identity(
                    route_configuration, expected_route.reviewer_telegram_username
                )
                if reviewer.telegram_chat_id is None:
                    self.service.bind_telegram_private_chat(
                        internal_username=expected_route.reviewer_username,
                        telegram_username=normalized_username,
                        telegram_chat_id=telegram_chat_id,
                        now=self.now,
                    )
                    reviewer.telegram_chat_id = telegram_chat_id
                    applied.append(
                        f"telegram_routes.{route.notification_route_id}.reviewer_identity"
                    )
                elif reviewer.telegram_chat_id != telegram_chat_id:
                    raise domain.DomainRejected(
                        "TELEGRAM_BINDING_CONFLICT",
                        "configured reviewer is already bound to another private chat",
                    )
            settings_match = (
                route.name == expected_route.name
                and route.enabled == expected_route.enabled
                and sorted(route.event_types) == sorted(expected_route.subscribed_events)
            )
            recipient_matches = reviewer is None or route.recipient_user_id == reviewer.user_id
            if not settings_match or not recipient_matches:
                result = self.service.configure_notification_route(
                    actor_id=actor_id,
                    notification_route_id=route.notification_route_id,
                    environment="LIVE",
                    name=expected_route.name,
                    channel="TELEGRAM",
                    event_types=expected_route.subscribed_events,
                    enabled=expected_route.enabled,
                    configuration=(None if recipient_matches else route_configuration),
                    expected_version=route.version,
                    idempotency_key=self._key(
                        f"notification-{route.notification_route_id}", route.version
                    ),
                    now=self.now,
                )
                route.version = int(result["version"])
                if reviewer is not None:
                    route.recipient_user_id = reviewer.user_id
                applied.append(f"telegram_routes.{route.notification_route_id}.settings")

        with self.database.session_factory() as session:
            capital = session.scalar(
                select(models.DirectCapitalConfiguration).where(
                    models.DirectCapitalConfiguration.team_id == team.team_id,
                    models.DirectCapitalConfiguration.environment == "LIVE",
                    models.DirectCapitalConfiguration.active,
                )
            )
        expected_capital = config.capital
        capital_matches = (
            capital is not None
            and capital.network == expected_capital.network
            and capital.asset == expected_capital.asset
            and capital.treasury_provider == expected_capital.treasury_provider
            and capital.vault_id == expected_capital.vault_id
            and capital.vault_address == expected_capital.vault_address
            and capital.owned_arbitrum_address == expected_capital.owned_arbitrum_address
            and capital.binance_account_id == expected_capital.binance_account_id
            and capital.binance_deposit_address == expected_capital.binance_deposit_address
            and capital.binance_withdrawal_address == expected_capital.binance_withdrawal_address
            and capital.hyperliquid_account_id == expected_capital.hyperliquid_account_id
            and capital.hyperliquid_bridge_address == expected_capital.hyperliquid_bridge_address
            and capital.safe_address == expected_capital.safe_address
            and capital.safe_delegate_address == expected_capital.safe_delegate_address
            and _same_decimal(capital.max_amount, expected_capital.max_amount)
            and _same_decimal(capital.max_fee, expected_capital.max_fee)
        )
        if not capital_matches:
            self.service.set_direct_capital_configuration(
                actor_id,
                self._key("capital", 0 if capital is None else capital.version),
                environment="LIVE",
                network=expected_capital.network,
                asset=expected_capital.asset,
                treasury_provider=expected_capital.treasury_provider,
                vault_id=expected_capital.vault_id,
                vault_address=expected_capital.vault_address,
                owned_arbitrum_address=expected_capital.owned_arbitrum_address,
                binance_account_id=expected_capital.binance_account_id,
                binance_deposit_address=expected_capital.binance_deposit_address,
                binance_withdrawal_address=expected_capital.binance_withdrawal_address,
                hyperliquid_account_id=expected_capital.hyperliquid_account_id,
                hyperliquid_bridge_address=expected_capital.hyperliquid_bridge_address,
                safe_address=expected_capital.safe_address,
                safe_delegate_address=expected_capital.safe_delegate_address,
                vault_withdrawal_private_key=None,
                safe_withdrawal_private_key=None,
                max_amount=expected_capital.max_amount,
                max_fee=expected_capital.max_fee,
                now=self.now,
            )
            applied.append("capital")

        blockers = [item for item in self.checks() if item.status == "BLOCKED"]
        if blockers:
            raise domain.DomainRejected(
                "PRODUCTION_CONFIG_PRECONDITIONS_FAILED",
                "; ".join(f"{item.path}={item.code}" for item in blockers),
            )

        for key in (
            "AUTO_PROFIT_SWEEP",
            "AUTO_OPERATING_REFILL",
            "AUTO_ADD",
            "CAPITAL_TRANSFER",
            "LIVE_ORDER_SEND",
        ):
            expected_gate = config.gates[key]
            with self.database.session_factory() as session:
                gate = session.get(models.CapabilityGate, key)
            if gate is None:
                raise domain.DomainRejected("CAPABILITY_GATE_NOT_FOUND", key)
            if gate.status != expected_gate.status or gate.reason != expected_gate.reason:
                self.service.set_capability_gate(
                    key,
                    domain.CapabilityStatus(expected_gate.status),
                    expected_gate.reason,
                    actor_id,
                    now=self.now,
                )
                applied.append(f"gates.{key}")
        return applied


def _report(command: str, checks: list[ConfigurationCheck], applied: list[str]) -> dict[str, Any]:
    blocked = sum(item.status == "BLOCKED" for item in checks)
    drift = sum(item.status == "DRIFT" for item in checks)
    return {
        "command": command,
        "status": "BLOCKED" if blocked else "DRIFT" if drift else "MATCH",
        "summary": {"blocked": blocked, "drift": drift, "ok": len(checks) - blocked - drift},
        "checks": [asdict(item) for item in checks],
        "applied": applied,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tradeops-config")
    parser.add_argument("command", choices=("validate", "apply", "status"))
    parser.add_argument("configuration")
    args = parser.parse_args(argv)
    try:
        configuration = load_production_configuration(args.configuration)
        settings = get_settings()
        settings.validate_runtime_security()
        database = Database(settings.database_url)
        ready, error = database.is_ready()
        if not ready:
            raise RuntimeError(error or "DATABASE_NOT_READY")
        configurator = ProductionConfigurator(database, settings, configuration)
        applied: list[str] = []
        if args.command == "apply":
            applied = configurator.apply()
        checks = configurator.checks()
        report = _report(args.command, checks, applied)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        if args.command == "validate":
            return 2 if report["summary"]["blocked"] else 0
        return 0 if report["status"] == "MATCH" else 3
    except (ValidationError, ValueError, OSError) as exc:
        print(json.dumps({"command": args.command, "status": "INVALID", "error": str(exc)}))
        return 2
    except domain.DomainRejected as exc:
        print(
            json.dumps(
                {
                    "command": args.command,
                    "status": "BLOCKED",
                    "code": exc.code,
                    "error": exc.detail,
                },
                ensure_ascii=False,
            )
        )
        return 2
    except RuntimeError as exc:
        print(json.dumps({"command": args.command, "status": "BLOCKED", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
