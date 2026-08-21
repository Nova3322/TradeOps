from __future__ import annotations

import base64
import binascii
import os
import re
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from trading_control_plane.perptape import (
    validate_perptape_http_url,
    validate_perptape_websocket_url,
)

DEFAULT_SESSION_SECRET = "local-development-session-secret-change-me"  # noqa: S105
EVM_ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")
PUBLIC_DNS_HOST_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}$"
)


class Settings(BaseSettings):
    """Server-side configuration.

    There is intentionally no database fallback: a missing durable store must fail closed.
    """

    model_config = SettingsConfigDict(
        env_prefix="TRADING_",
        env_file=(".env.local", ".env.production.local"),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        populate_by_name=True,
    )

    environment: Literal["local", "test", "staging", "production"] = "local"
    database_url: str = Field(min_length=1)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    public_base_url: str = "http://127.0.0.1:8000"
    session_signing_secret: str = Field(
        default=DEFAULT_SESSION_SECRET,
        min_length=32,
        repr=False,
    )
    credential_encryption_key: str | None = Field(default=None, repr=False)
    allow_mock_identity: bool = False
    session_ttl_seconds: int = Field(default=28_800, ge=300, le=86_400)
    action_token_ttl_seconds: int = Field(default=300, ge=30, le=900)
    telegram_enabled: bool = False
    telegram_bot_token: str | None = Field(
        default=None,
        min_length=20,
        repr=False,
        validation_alias=AliasChoices(
            "TRADING_TELEGRAM_BOT_TOKEN",
            "TELEGRAM_BOT_TOKEN",
        ),
    )
    telegram_bot_username: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "TRADING_TELEGRAM_BOT_USERNAME",
            "TELEGRAM_BOT_USERNAME",
        ),
    )
    telegram_allowed_username: str | None = None
    telegram_internal_username: str | None = None
    telegram_poll_timeout_seconds: int = Field(default=20, ge=1, le=30)
    perptape_base_url: str = "https://perptape.com"
    perptape_api_key: str | None = Field(default=None, repr=False)
    perptape_service_username: str = "perptape"
    perptape_contract_version: str = "breakouts-v1"
    perptape_cache_seconds: int = Field(default=60, ge=1, le=300)
    perptape_timeout_seconds: float = Field(default=15, ge=5, le=30)
    perptape_websocket_enabled: bool = False
    perptape_websocket_url: str = "wss://perptape.com/ws/v1/alerts"
    perptape_websocket_heartbeat_timeout_seconds: int = Field(default=45, ge=20, le=120)
    perptape_websocket_reconciliation_seconds: int = Field(default=300, ge=60, le=3_600)
    perptape_websocket_reconnect_initial_seconds: float = Field(default=1, ge=0.1, le=30)
    perptape_websocket_reconnect_max_seconds: float = Field(default=30, ge=1, le=300)
    perptape_websocket_max_reconnect_attempts: int = Field(default=8, ge=1, le=20)
    perptape_auto_proposal_enabled: bool = False
    perptape_auto_proposal_account_id: str | None = None
    perptape_auto_proposal_environment: Literal["TESTNET", "LIVE"] = "LIVE"
    perptape_auto_proposal_risk_tier: Literal["LOW", "MEDIUM", "HIGH"] = "MEDIUM"
    perptape_auto_proposal_min_timeframes: int = Field(default=3, ge=3, le=4)
    perptape_auto_proposal_notional: Decimal = Field(default=Decimal(100), gt=0)
    perptape_auto_proposal_max_risk: Decimal = Field(default=Decimal(1), gt=0)
    perptape_auto_proposal_invalidation_bps: int = Field(default=200, ge=1, le=5_000)
    perptape_auto_proposal_expires_minutes: int = Field(default=480, ge=480, le=1_440)
    runtime_sync_enabled: bool = False
    runtime_sync_interval_seconds: int = Field(default=60, ge=30, le=3_600)
    runtime_sync_service_username: str = "runtime-sync"
    fact_adapter_enabled: bool = False
    fact_adapter_host: str = "127.0.0.1"
    fact_adapter_port: int = Field(default=8010, ge=1, le=65_535)
    fact_adapter_bearer_token: str | None = Field(default=None, min_length=32, repr=False)
    fact_adapter_binding_refresh_seconds: int = Field(default=60, ge=30, le=3_600)
    fact_adapter_stale_after_seconds: int = Field(default=360, ge=15, le=900)
    fact_adapter_reconciliation_seconds: int = Field(default=300, ge=30, le=3_600)
    fact_adapter_fallback_seconds: int = Field(default=60, ge=30, le=900)
    notification_worker_enabled: bool = False
    notification_worker_interval_seconds: int = Field(default=15, ge=5, le=300)
    notification_worker_batch_size: int = Field(default=50, ge=1, le=200)
    notification_email_smtp_allowed_hosts: str = ""
    runtime_binance_symbol: str = "BTCUSDT"
    runtime_hyperliquid_symbol: str = "BTC"
    runtime_okx_symbol: str = "BTC-USDT-SWAP"
    runtime_bybit_symbol: str = "BTCUSDT"
    freqtrade_workers_enabled: bool = False
    freqtrade_timeout_seconds: float = Field(default=5, ge=1, le=15)
    freqtrade_confirmation_timeout_seconds: float = Field(default=90, ge=10, le=120)
    freqtrade_live_leverage: Decimal = Field(default=Decimal(1), ge=1, le=20)
    execution_worker_enabled: bool = False
    execution_worker_interval_seconds: int = Field(default=5, ge=1, le=60)
    execution_worker_batch_size: int = Field(default=20, ge=1, le=200)
    capital_continuation_worker_enabled: bool = False
    capital_continuation_worker_scan_seconds: int = Field(default=30, ge=5, le=60)
    capital_continuation_worker_batch_size: int = Field(default=20, ge=1, le=100)
    binance_recv_window_ms: int = Field(default=10_000, ge=1_000, le=60_000)
    binance_capital_base_url: str = "https://api.binance.com"
    binance_capital_api_key: str | None = Field(default=None, repr=False)
    binance_capital_api_secret: str | None = Field(default=None, repr=False)
    binance_capital_account_id: str | None = None
    binance_capital_timeout_seconds: float = Field(default=8, ge=1, le=15)
    binance_capital_withdraw_enabled: bool = False
    hyperliquid_base_url: str = "https://api.hyperliquid.xyz"
    hyperliquid_account_address: str | None = None
    hyperliquid_api_wallet_address: str | None = None
    hyperliquid_subaccount_address: str | None = None
    notilt_enabled: bool = False
    notilt_agent_address: str | None = None
    notilt_ethereum_vault_address: str | None = None
    notilt_bsc_vault_address: str | None = None
    notilt_arbitrum_vault_address: str | None = None
    notilt_gateway_timeout_seconds: int = Field(default=30, ge=5, le=120)
    notilt_ethereum_min_confirmations: int = Field(default=12, ge=1, le=128)
    notilt_bsc_min_confirmations: int = Field(default=15, ge=1, le=128)
    notilt_arbitrum_min_confirmations: int = Field(default=20, ge=1, le=128)
    capital_direct_vault_id: str | None = None
    capital_direct_vault_address: str | None = None
    capital_direct_owned_arbitrum_address: str | None = None
    capital_direct_binance_account_id: str | None = None
    capital_direct_binance_deposit_address: str | None = None
    capital_direct_binance_withdrawal_address: str | None = None
    capital_direct_hyperliquid_account_id: str | None = None
    capital_direct_hyperliquid_bridge_address: str | None = None
    capital_direct_treasury_provider: Literal["NOTILT_VAULT", "SAFE_SPENDING_LIMIT"] = (
        "NOTILT_VAULT"
    )
    capital_direct_asset: str = "USDC"
    capital_direct_network: Literal["ARBITRUM"] = "ARBITRUM"
    capital_direct_max_amount: Decimal | None = Field(default=None, gt=0)
    capital_direct_max_fee: Decimal | None = Field(default=None, ge=0)
    capital_arbitrum_rpc_url: str | None = None
    safe_spending_enabled: bool = False
    safe_spending_arbitrum_rpc_url: str | None = None
    capital_direct_safe_address: str | None = None
    capital_direct_safe_delegate_address: str | None = None
    safe_spending_gateway_timeout_seconds: int = Field(default=20, ge=5, le=60)

    @property
    def hyperliquid_effective_account_address(self) -> str | None:
        return self.hyperliquid_subaccount_address or self.hyperliquid_account_address

    @property
    def hyperliquid_account_scope(self) -> Literal["MAIN_ACCOUNT", "SUBACCOUNT"]:
        return "SUBACCOUNT" if self.hyperliquid_subaccount_address else "MAIN_ACCOUNT"

    @property
    def notification_email_smtp_allowlist(self) -> tuple[str, ...]:
        return tuple(item for item in self.notification_email_smtp_allowed_hosts.split(",") if item)

    @property
    def notilt_vaults(self) -> dict[int, str]:
        return {
            chain_id: address
            for chain_id, address in (
                (1, self.notilt_ethereum_vault_address),
                (56, self.notilt_bsc_vault_address),
                (42161, self.notilt_arbitrum_vault_address),
            )
            if address is not None
        }

    @property
    def notilt_min_confirmations(self) -> dict[int, int]:
        return {
            1: self.notilt_ethereum_min_confirmations,
            56: self.notilt_bsc_min_confirmations,
            42161: self.notilt_arbitrum_min_confirmations,
        }

    @field_validator("database_url")
    @classmethod
    def require_postgresql(cls, value: str) -> str:
        if not value.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError("database_url must use PostgreSQL with the psycopg driver")
        return value

    @field_validator("credential_encryption_key")
    @classmethod
    def require_aes_256_credential_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except (ValueError, binascii.Error) as exc:
            raise ValueError("credential encryption key must be URL-safe base64") from exc
        if len(decoded) != 32:
            raise ValueError("credential encryption key must decode to exactly 32 bytes")
        return value

    @field_validator("perptape_base_url")
    @classmethod
    def require_official_perptape_http_url(cls, value: str) -> str:
        return validate_perptape_http_url(value)

    @field_validator("perptape_websocket_url")
    @classmethod
    def require_official_perptape_websocket_url(cls, value: str) -> str:
        return validate_perptape_websocket_url(value)

    @field_validator("notification_email_smtp_allowed_hosts")
    @classmethod
    def require_valid_notification_smtp_allowlist(cls, value: str) -> str:
        hosts = tuple(item.strip().lower() for item in value.split(",") if item.strip())
        if len(hosts) != len(set(hosts)) or any(
            PUBLIC_DNS_HOST_PATTERN.fullmatch(host) is None for host in hosts
        ):
            raise ValueError("notification email SMTP hosts must be unique public DNS hostnames")
        return ",".join(hosts)

    @field_validator("binance_capital_base_url")
    @classmethod
    def require_official_binance_capital_url(cls, value: str) -> str:
        if value.rstrip("/") != "https://api.binance.com":
            raise ValueError("Binance capital API must use https://api.binance.com")
        return value.rstrip("/")

    @field_validator(
        "notilt_agent_address",
        "notilt_ethereum_vault_address",
        "notilt_bsc_vault_address",
        "notilt_arbitrum_vault_address",
        "capital_direct_vault_address",
        "capital_direct_owned_arbitrum_address",
        "capital_direct_binance_deposit_address",
        "capital_direct_binance_withdrawal_address",
        "capital_direct_hyperliquid_bridge_address",
        "capital_direct_safe_address",
        "capital_direct_safe_delegate_address",
    )
    @classmethod
    def require_evm_address(cls, value: str | None) -> str | None:
        if value is not None and not EVM_ADDRESS_PATTERN.fullmatch(value):
            raise ValueError("configured EVM addresses must be 20-byte EVM addresses")
        return value

    def validate_runtime_security(self) -> None:
        if self.environment == "production" and self.allow_mock_identity:
            raise ValueError("mock identity must be disabled in production")
        if (
            self.environment == "production"
            and self.session_signing_secret == DEFAULT_SESSION_SECRET
        ):
            raise ValueError("production requires an explicit session signing secret")
        if bool(self.binance_capital_api_key) != bool(self.binance_capital_api_secret):
            raise ValueError("Binance capital key and secret must be configured together")
        if bool(self.binance_capital_api_key) != bool(self.binance_capital_account_id):
            raise ValueError(
                "Binance capital credentials require an exact dedicated capital account ID"
            )
        if self.fact_adapter_enabled and (
            not self.runtime_sync_enabled
            or not self.credential_encryption_key
            or not self.fact_adapter_bearer_token
        ):
            raise ValueError(
                "enabled fact adapter requires runtime sync, credential encryption and "
                "an internal bearer token"
            )
        if self.freqtrade_workers_enabled and not self.credential_encryption_key:
            raise ValueError(
                "enabled Freqtrade workers require database credential encryption for "
                "account-bound worker credentials"
            )
        if self.execution_worker_enabled and not self.freqtrade_workers_enabled:
            raise ValueError("automatic execution requires enabled Freqtrade workers")
        if self.hyperliquid_subaccount_address and not self.hyperliquid_account_address:
            raise ValueError("Hyperliquid subaccount requires the main account address")
        if self.notilt_enabled and not self.notilt_agent_address:
            raise ValueError("enabled NoTilt requires the public whitelist agent address")
        if self.notilt_vaults and not self.notilt_agent_address:
            raise ValueError("configured NoTilt Vaults require the public whitelist agent address")
        if self.telegram_enabled and not self.telegram_bot_token:
            raise ValueError("enabled Telegram requires a Bot API token")
        if self.telegram_enabled and not self.telegram_allowed_username:
            raise ValueError("enabled Telegram requires an allowed private-chat username")
        if self.telegram_enabled and not self.telegram_internal_username:
            raise ValueError("enabled Telegram requires an existing internal username")
        if self.perptape_websocket_enabled and not self.runtime_sync_enabled:
            raise ValueError("enabled Perptape WebSocket requires the runtime sync worker")
        if self.perptape_websocket_enabled and not (
            self.perptape_api_key or self.credential_encryption_key
        ):
            raise ValueError(
                "enabled Perptape WebSocket requires a platform API key or database "
                "credential encryption key"
            )
        if self.perptape_auto_proposal_enabled and not self.runtime_sync_enabled:
            raise ValueError("automatic Perptape proposals require the runtime sync worker")
        if self.perptape_auto_proposal_enabled and not self.perptape_api_key:
            raise ValueError("automatic Perptape proposals require the platform API key")
        if self.perptape_auto_proposal_enabled and not self.perptape_auto_proposal_account_id:
            raise ValueError("automatic Perptape proposals require an internal account ID")
        if self.notification_worker_enabled and not self.credential_encryption_key:
            raise ValueError("enabled notification worker requires the credential encryption key")
        if self.capital_continuation_worker_enabled and not self.credential_encryption_key:
            raise ValueError(
                "enabled capital continuation worker requires the credential encryption key"
            )
        if (
            self.perptape_websocket_reconnect_initial_seconds
            > self.perptape_websocket_reconnect_max_seconds
        ):
            raise ValueError("Perptape WebSocket reconnect initial delay exceeds its maximum")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    config_dir = os.environ.get("TRADING_CONFIG_DIR")
    if config_dir is None:
        return Settings()  # type: ignore[call-arg]
    root = Path(config_dir)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("TRADING_CONFIG_DIR must be an existing absolute directory")
    env_files = tuple(
        path for path in (root / ".env.local", root / ".env.production.local") if path.is_file()
    )
    if not env_files:
        raise ValueError("TRADING_CONFIG_DIR contains no supported environment files")
    return Settings(_env_file=env_files)  # type: ignore[call-arg]
