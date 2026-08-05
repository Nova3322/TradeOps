from __future__ import annotations

import os
import re
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from trading_control_plane.freqtrade import parse_hip3_dexes, validate_worker_url
from trading_control_plane.perptape import (
    validate_perptape_http_url,
    validate_perptape_websocket_url,
)

DEFAULT_SESSION_SECRET = "local-development-session-secret-change-me"  # noqa: S105
EVM_ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")


class Settings(BaseSettings):
    """Server-side configuration.

    There is intentionally no database fallback: a missing durable store must fail closed.
    """

    model_config = SettingsConfigDict(
        env_prefix="TRADING_",
        env_file=(".env.local", ".env.production.local"),
        env_file_encoding="utf-8",
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
    perptape_auto_proposal_environment: Literal["SHADOW", "TESTNET", "LIVE"] = "LIVE"
    perptape_auto_proposal_risk_tier: Literal["LOW", "MEDIUM", "HIGH"] = "MEDIUM"
    perptape_auto_proposal_min_timeframes: int = Field(default=3, ge=3, le=4)
    perptape_auto_proposal_notional: Decimal = Field(default=Decimal(100), gt=0)
    perptape_auto_proposal_max_risk: Decimal = Field(default=Decimal(1), gt=0)
    perptape_auto_proposal_invalidation_bps: int = Field(default=200, ge=1, le=5_000)
    perptape_auto_proposal_expires_minutes: int = Field(default=480, ge=480, le=1_440)
    runtime_sync_enabled: bool = False
    runtime_sync_interval_seconds: int = Field(default=60, ge=30, le=3_600)
    runtime_sync_service_username: str = "runtime-sync"
    runtime_binance_account_id: str | None = None
    runtime_binance_symbol: str = "BTCUSDT"
    runtime_hyperliquid_account_id: str | None = None
    runtime_hyperliquid_symbol: str = "BTC"
    execution_backend: Literal["FREQTRADE", "DIRECT_LEGACY"] = "FREQTRADE"
    freqtrade_workers_enabled: bool = False
    freqtrade_binance_worker_url: str = "http://127.0.0.1:8081"
    freqtrade_hyperliquid_worker_url: str = "http://127.0.0.1:8082"
    freqtrade_api_username: str | None = None
    freqtrade_api_password: str | None = Field(default=None, repr=False)
    freqtrade_timeout_seconds: float = Field(default=5, ge=1, le=15)
    freqtrade_confirmation_timeout_seconds: float = Field(default=90, ge=10, le=120)
    freqtrade_hyperliquid_hip3_dexes: str = "xyz"
    freqtrade_live_order_send_enabled: bool = False
    freqtrade_live_leverage: Decimal = Field(default=Decimal(1), ge=1, le=20)
    binance_read_only_enabled: bool = False
    binance_fact_environment: Literal["TESTNET", "LIVE"] = "LIVE"
    binance_futures_base_url: str = "https://fapi.binance.com"
    binance_account_mode: Literal["STANDARD", "PORTFOLIO_MARGIN"] = "PORTFOLIO_MARGIN"
    binance_api_key: str | None = Field(default=None, repr=False)
    binance_api_secret: str | None = Field(default=None, repr=False)
    binance_recv_window_ms: int = Field(default=10_000, ge=1_000, le=60_000)
    binance_live_order_send_enabled: bool = False
    binance_live_base_url: str = "https://papi.binance.com"
    binance_testnet_order_send_enabled: bool = False
    binance_testnet_base_url: str = "https://testnet.binancefuture.com"
    binance_testnet_api_key: str | None = Field(default=None, repr=False)
    binance_testnet_api_secret: str | None = Field(default=None, repr=False)
    hyperliquid_read_only_enabled: bool = False
    hyperliquid_fact_environment: Literal["TESTNET", "LIVE"] = "LIVE"
    hyperliquid_base_url: str = "https://api.hyperliquid.xyz"
    hyperliquid_account_address: str | None = None
    hyperliquid_api_wallet_address: str | None = None
    hyperliquid_api_wallet_private_key: str | None = Field(default=None, repr=False)
    hyperliquid_core_dex: Literal[""] = ""
    hyperliquid_live_order_send_enabled: bool = False
    hyperliquid_live_base_url: str = "https://api.hyperliquid.xyz"
    hyperliquid_testnet_order_send_enabled: bool = False
    hyperliquid_testnet_base_url: str = "https://api.hyperliquid-testnet.xyz"
    hyperliquid_testnet_api_wallet_private_key: str | None = Field(default=None, repr=False)
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
    capital_direct_asset: str = "USDC"
    capital_direct_network: Literal["ARBITRUM"] = "ARBITRUM"
    capital_direct_max_amount: Decimal | None = Field(default=None, gt=0)
    capital_direct_max_fee: Decimal | None = Field(default=None, ge=0)
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
    def hyperliquid_hip3_dexes(self) -> tuple[str, ...]:
        return parse_hip3_dexes(self.freqtrade_hyperliquid_hip3_dexes)

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

    @field_validator("perptape_base_url")
    @classmethod
    def require_official_perptape_http_url(cls, value: str) -> str:
        return validate_perptape_http_url(value)

    @field_validator("perptape_websocket_url")
    @classmethod
    def require_official_perptape_websocket_url(cls, value: str) -> str:
        return validate_perptape_websocket_url(value)

    @field_validator("freqtrade_binance_worker_url", "freqtrade_hyperliquid_worker_url")
    @classmethod
    def require_safe_freqtrade_worker_url(cls, value: str) -> str:
        return validate_worker_url(value)

    @field_validator("freqtrade_hyperliquid_hip3_dexes")
    @classmethod
    def require_valid_hip3_dexes(cls, value: str) -> str:
        parse_hip3_dexes(value)
        return value

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
        if self.binance_read_only_enabled and bool(self.binance_api_key) != bool(
            self.binance_api_secret
        ):
            raise ValueError("Binance read-only key and secret must be configured together")
        direct_send_enabled = any(
            (
                self.binance_live_order_send_enabled,
                self.binance_testnet_order_send_enabled,
                self.hyperliquid_live_order_send_enabled,
                self.hyperliquid_testnet_order_send_enabled,
            )
        )
        if direct_send_enabled and self.execution_backend != "DIRECT_LEGACY":
            raise ValueError(
                "direct venue sending is retired; enabled order sending requires "
                "DIRECT_LEGACY only for isolated compatibility tests"
            )
        if bool(self.freqtrade_api_username) != bool(self.freqtrade_api_password):
            raise ValueError("Freqtrade worker username and password must be configured together")
        if self.freqtrade_workers_enabled and not (
            self.freqtrade_api_username and self.freqtrade_api_password
        ):
            raise ValueError("enabled Freqtrade workers require explicit control credentials")
        if self.freqtrade_live_order_send_enabled and (
            self.execution_backend != "FREQTRADE"
            or not self.freqtrade_workers_enabled
            or not self.freqtrade_api_username
            or not self.freqtrade_api_password
        ):
            raise ValueError(
                "Freqtrade LIVE send requires the FREQTRADE backend, enabled workers and "
                "explicit control credentials"
            )
        if (
            self.runtime_sync_enabled
            and self.binance_read_only_enabled
            and not self.runtime_binance_account_id
        ):
            raise ValueError("runtime Binance sync requires an internal account ID")
        if bool(self.binance_testnet_api_key) != bool(self.binance_testnet_api_secret):
            raise ValueError("Binance testnet key and secret must be configured together")
        if self.binance_testnet_order_send_enabled and not (
            self.binance_testnet_api_key and self.binance_testnet_api_secret
        ):
            raise ValueError("enabled Binance testnet send requires explicit testnet credentials")
        if self.binance_live_order_send_enabled and not (
            self.binance_api_key and self.binance_api_secret
        ):
            raise ValueError("enabled Binance LIVE send requires explicit LIVE credentials")
        if self.hyperliquid_subaccount_address and not self.hyperliquid_account_address:
            raise ValueError("Hyperliquid subaccount requires the main account address")
        if (
            self.runtime_sync_enabled
            and self.hyperliquid_read_only_enabled
            and not self.runtime_hyperliquid_account_id
        ):
            raise ValueError("runtime Hyperliquid sync requires an internal account ID")
        if self.hyperliquid_live_order_send_enabled and not (
            (self.hyperliquid_account_address or self.hyperliquid_api_wallet_address)
            and self.hyperliquid_api_wallet_private_key
        ):
            raise ValueError(
                "enabled Hyperliquid LIVE send requires an account or API wallet address "
                "and the API wallet private key"
            )
        if self.hyperliquid_testnet_order_send_enabled and not (
            self.hyperliquid_account_address and self.hyperliquid_testnet_api_wallet_private_key
        ):
            raise ValueError(
                "enabled Hyperliquid testnet send requires the main account address "
                "and testnet API wallet private key"
            )
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
        if self.perptape_websocket_enabled and not self.perptape_api_key:
            raise ValueError("enabled Perptape WebSocket requires the platform API key")
        if self.perptape_auto_proposal_enabled and not self.runtime_sync_enabled:
            raise ValueError("automatic Perptape proposals require the runtime sync worker")
        if self.perptape_auto_proposal_enabled and not self.perptape_api_key:
            raise ValueError("automatic Perptape proposals require the platform API key")
        if self.perptape_auto_proposal_enabled and not self.perptape_auto_proposal_account_id:
            raise ValueError("automatic Perptape proposals require an internal account ID")
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
