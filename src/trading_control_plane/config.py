from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SESSION_SECRET = "local-development-session-secret-change-me"  # noqa: S105


class Settings(BaseSettings):
    """Server-side configuration.

    There is intentionally no database fallback: a missing durable store must fail closed.
    """

    model_config = SettingsConfigDict(
        env_prefix="TRADING_",
        env_file=".env.local",
        env_file_encoding="utf-8",
        extra="ignore",
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
    perptape_base_url: str = "http://127.0.0.1:8787"
    perptape_api_key: str | None = Field(default=None, repr=False)
    perptape_service_username: str = "perptape"
    perptape_contract_version: str = "breakouts-v1"
    perptape_cache_seconds: int = Field(default=60, ge=1, le=300)
    binance_read_only_enabled: bool = False
    binance_fact_environment: Literal["TESTNET", "LIVE"] = "LIVE"
    binance_futures_base_url: str = "https://fapi.binance.com"
    binance_api_key: str | None = Field(default=None, repr=False)
    binance_api_secret: str | None = Field(default=None, repr=False)
    binance_recv_window_ms: int = Field(default=5_000, ge=1_000, le=10_000)
    binance_testnet_order_send_enabled: bool = False
    binance_testnet_base_url: str = "https://testnet.binancefuture.com"
    binance_testnet_api_key: str | None = Field(default=None, repr=False)
    binance_testnet_api_secret: str | None = Field(default=None, repr=False)

    @field_validator("database_url")
    @classmethod
    def require_postgresql(cls, value: str) -> str:
        if not value.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError("database_url must use PostgreSQL with the psycopg driver")
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
        if bool(self.binance_testnet_api_key) != bool(self.binance_testnet_api_secret):
            raise ValueError("Binance testnet key and secret must be configured together")
        if self.binance_testnet_order_send_enabled and not (
            self.binance_testnet_api_key and self.binance_testnet_api_secret
        ):
            raise ValueError("enabled Binance testnet send requires explicit testnet credentials")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
