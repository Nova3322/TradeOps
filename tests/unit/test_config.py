import pytest
from pydantic import ValidationError

from trading_control_plane.config import Settings, get_settings


def test_database_is_mandatory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADING_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_empty_optional_environment_values_are_treated_as_unconfigured(tmp_path) -> None:
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "TRADING_DATABASE_URL=postgresql+psycopg://user:pass@localhost/trading\n"
        "TRADING_TELEGRAM_BOT_TOKEN=\n"
        "TRADING_NOTILT_AGENT_ADDRESS=\n"
        "TRADING_CAPITAL_DIRECT_MAX_AMOUNT=\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.telegram_bot_token is None
    assert settings.notilt_agent_address is None
    assert settings.capital_direct_max_amount is None


def test_non_postgresql_database_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must use PostgreSQL"):
        Settings(database_url="sqlite:///local.db", _env_file=None)


def test_postgresql_psycopg_url_is_accepted() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://user:pass@localhost/trading",
        _env_file=None,
    )

    assert settings.environment == "local"
    assert settings.hyperliquid_read_only_enabled is False
    assert settings.hyperliquid_fact_environment == "LIVE"
    assert settings.hyperliquid_base_url == "https://api.hyperliquid.xyz"
    assert settings.hyperliquid_live_order_send_enabled is False
    assert settings.hyperliquid_testnet_order_send_enabled is False
    assert settings.hyperliquid_core_dex == ""
    assert settings.hyperliquid_effective_account_address is None
    assert settings.hyperliquid_account_scope == "MAIN_ACCOUNT"
    assert settings.binance_account_mode == "PORTFOLIO_MARGIN"
    assert settings.binance_live_order_send_enabled is False
    assert settings.binance_live_base_url == "https://papi.binance.com"
    assert settings.perptape_base_url == "https://perptape.com"
    assert settings.perptape_timeout_seconds == 15
    assert settings.perptape_websocket_enabled is False
    assert settings.perptape_websocket_url == "wss://perptape.com/ws/v1/alerts"
    assert settings.runtime_sync_enabled is False
    assert settings.runtime_sync_interval_seconds == 60
    assert settings.execution_backend == "FREQTRADE"
    assert settings.freqtrade_workers_enabled is False
    assert settings.hyperliquid_hip3_dexes == ("xyz",)
    assert settings.notilt_enabled is False
    assert settings.notilt_vaults == {}
    assert not hasattr(settings, "hyperliquid_private_key")
    assert not hasattr(settings, "hyperliquid_vault_address")
    assert not hasattr(settings, "notilt_private_key")


def test_explicit_config_directory_loads_both_secret_and_production_layers(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env.local").write_text(
        "TRADING_DATABASE_URL=postgresql+psycopg://user:pass@localhost/trading\n"
        "TRADING_BINANCE_API_KEY=fixture-key\n"
        "TRADING_BINANCE_API_SECRET=fixture-secret\n"
    )
    (tmp_path / ".env.production.local").write_text(
        "TRADING_BINANCE_READ_ONLY_ENABLED=true\nTRADING_RUNTIME_BINANCE_ACCOUNT_ID=binance-main\n"
    )
    monkeypatch.setenv("TRADING_CONFIG_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.binance_read_only_enabled is True
        assert settings.runtime_binance_account_id == "binance-main"
        assert settings.binance_api_key == "fixture-key"
        assert "fixture-secret" not in repr(settings)
    finally:
        get_settings.cache_clear()


def test_hyperliquid_defaults_to_main_account_and_allows_explicit_subaccount() -> None:
    main_account = "0x1111111111111111111111111111111111111111"
    subaccount = "0x2222222222222222222222222222222222222222"
    main = Settings(
        database_url="postgresql+psycopg://user:pass@localhost/trading",
        hyperliquid_account_address=main_account,
        _env_file=None,
    )
    selected_subaccount = Settings(
        database_url="postgresql+psycopg://user:pass@localhost/trading",
        hyperliquid_account_address=main_account,
        hyperliquid_subaccount_address=subaccount,
        _env_file=None,
    )

    assert main.hyperliquid_effective_account_address == main_account
    assert main.hyperliquid_account_scope == "MAIN_ACCOUNT"
    assert selected_subaccount.hyperliquid_effective_account_address == subaccount
    assert selected_subaccount.hyperliquid_account_scope == "SUBACCOUNT"
    selected_subaccount.validate_runtime_security()

    missing_main = Settings(
        database_url="postgresql+psycopg://user:pass@localhost/trading",
        hyperliquid_subaccount_address=subaccount,
        _env_file=None,
    )
    with pytest.raises(ValueError, match="requires the main account"):
        missing_main.validate_runtime_security()


def test_production_rejects_mock_identity_and_default_signing_secret() -> None:
    mock = Settings(
        environment="production",
        database_url="postgresql+psycopg://user:pass@localhost/trading",
        allow_mock_identity=True,
        session_signing_secret="a" * 32,
        _env_file=None,
    )
    with pytest.raises(ValueError, match="mock identity"):
        mock.validate_runtime_security()

    default_secret = Settings(
        environment="production",
        database_url="postgresql+psycopg://user:pass@localhost/trading",
        _env_file=None,
    )
    with pytest.raises(ValueError, match="signing secret"):
        default_secret.validate_runtime_security()


def test_real_telegram_is_default_off_and_requires_binding_configuration() -> None:
    token = "123456789:abcdefghijklmnopqrstuvwxyz"  # noqa: S105
    default = Settings(
        database_url="postgresql+psycopg://user:pass@localhost/trading",
        _env_file=None,
    )
    assert default.telegram_enabled is False
    assert default.telegram_bot_token is None

    missing_binding = Settings(
        database_url="postgresql+psycopg://user:pass@localhost/trading",
        telegram_enabled=True,
        telegram_bot_token=token,
        _env_file=None,
    )
    with pytest.raises(ValueError, match="allowed private-chat username"):
        missing_binding.validate_runtime_security()

    configured = Settings(
        database_url="postgresql+psycopg://user:pass@localhost/trading",
        telegram_enabled=True,
        telegram_bot_token=token,
        telegram_allowed_username="telegram-owner",
        telegram_internal_username="telegram-owner",
        _env_file=None,
    )
    configured.validate_runtime_security()


def test_live_senders_remain_default_off_and_require_explicit_credentials() -> None:
    database_url = "postgresql+psycopg://user:pass@localhost/trading"
    missing_binance = Settings(
        database_url=database_url,
        execution_backend="DIRECT_LEGACY",
        binance_live_order_send_enabled=True,
        _env_file=None,
    )
    with pytest.raises(ValueError, match="Binance LIVE send"):
        missing_binance.validate_runtime_security()

    missing_hyperliquid = Settings(
        database_url=database_url,
        execution_backend="DIRECT_LEGACY",
        hyperliquid_live_order_send_enabled=True,
        _env_file=None,
    )
    with pytest.raises(ValueError, match="Hyperliquid LIVE send"):
        missing_hyperliquid.validate_runtime_security()

    explicit = Settings(
        database_url=database_url,
        execution_backend="DIRECT_LEGACY",
        binance_live_order_send_enabled=True,
        binance_api_key="fixture-key",
        binance_api_secret="fixture-secret",  # noqa: S106
        hyperliquid_live_order_send_enabled=True,
        hyperliquid_api_wallet_address="0x1111111111111111111111111111111111111111",
        hyperliquid_api_wallet_private_key="fixture-private-key",
        _env_file=None,
    )
    explicit.validate_runtime_security()


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_direct_legacy_execution_is_never_a_deployable_backend(environment: str) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://user:pass@localhost/trading",
        environment=environment,
        execution_backend="DIRECT_LEGACY",
        session_signing_secret="deployment-session-secret-0123456789",  # noqa: S106
        _env_file=None,
    )

    with pytest.raises(ValueError, match="production execution require FREQTRADE"):
        settings.validate_runtime_security()


def test_notilt_uses_public_agent_and_three_fixed_mainnet_vault_slots() -> None:
    database_url = "postgresql+psycopg://user:pass@localhost/trading"
    agent = "0x1111111111111111111111111111111111111111"
    ethereum = "0x2222222222222222222222222222222222222222"
    bsc = "0x3333333333333333333333333333333333333333"
    arbitrum = "0x4444444444444444444444444444444444444444"
    configured = Settings(
        database_url=database_url,
        notilt_enabled=True,
        notilt_agent_address=agent,
        notilt_ethereum_vault_address=ethereum,
        notilt_bsc_vault_address=bsc,
        notilt_arbitrum_vault_address=arbitrum,
        _env_file=None,
    )

    configured.validate_runtime_security()
    assert configured.notilt_vaults == {1: ethereum, 56: bsc, 42161: arbitrum}

    missing_agent = Settings(
        database_url=database_url,
        notilt_enabled=True,
        _env_file=None,
    )
    with pytest.raises(ValueError, match="public whitelist agent"):
        missing_agent.validate_runtime_security()

    with pytest.raises(ValidationError, match="20-byte EVM"):
        Settings(
            database_url=database_url,
            notilt_agent_address="invalid",
            _env_file=None,
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"binance_read_only_enabled": True, "binance_api_key": "fixture-key"},
            "read-only key and secret",
        ),
        ({"binance_testnet_api_key": "fixture-key"}, "testnet key and secret"),
        (
            {
                "execution_backend": "DIRECT_LEGACY",
                "binance_testnet_order_send_enabled": True,
            },
            "testnet send requires explicit",
        ),
        (
            {
                "execution_backend": "DIRECT_LEGACY",
                "hyperliquid_testnet_order_send_enabled": True,
            },
            "testnet send requires the main account",
        ),
        (
            {
                "runtime_sync_enabled": True,
                "binance_read_only_enabled": True,
                "binance_api_key": "fixture-key",
                "binance_api_secret": "fixture-secret",
            },
            "runtime Binance sync requires an internal account ID",
        ),
        (
            {
                "runtime_sync_enabled": True,
                "hyperliquid_read_only_enabled": True,
            },
            "runtime Hyperliquid sync requires an internal account ID",
        ),
        (
            {
                "telegram_enabled": True,
                "telegram_allowed_username": "allowed",
                "telegram_internal_username": "internal",
            },
            "requires a Bot API token",
        ),
        (
            {
                "telegram_enabled": True,
                "telegram_bot_token": "123456789:abcdefghijklmnopqrstuvwxyz",
                "telegram_allowed_username": "allowed",
            },
            "existing internal username",
        ),
    ],
)
def test_runtime_configuration_rejects_partial_external_credentials(
    overrides: dict[str, object], message: str
) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://user:pass@localhost/trading",
        _env_file=None,
        **overrides,
    )
    with pytest.raises(ValueError, match=message):
        settings.validate_runtime_security()


def test_perptape_websocket_requires_explicit_worker_and_a_credential_source() -> None:
    database_url = "postgresql+psycopg://user:pass@localhost/trading"
    missing_worker = Settings(
        database_url=database_url,
        perptape_websocket_enabled=True,
        perptape_api_key="fixture-key",
        _env_file=None,
    )
    with pytest.raises(ValueError, match="runtime sync worker"):
        missing_worker.validate_runtime_security()

    missing_credential_source = Settings(
        database_url=database_url,
        runtime_sync_enabled=True,
        perptape_websocket_enabled=True,
        _env_file=None,
    )
    with pytest.raises(ValueError, match="database credential encryption key"):
        missing_credential_source.validate_runtime_security()

    enabled = Settings(
        database_url=database_url,
        runtime_sync_enabled=True,
        perptape_websocket_enabled=True,
        perptape_api_key="fixture-key",
        _env_file=None,
    )
    enabled.validate_runtime_security()

    database_bound = Settings(
        database_url=database_url,
        runtime_sync_enabled=True,
        perptape_websocket_enabled=True,
        credential_encryption_key=("eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg"),
        _env_file=None,
    )
    database_bound.validate_runtime_security()


def test_notification_worker_is_off_by_default_and_requires_the_encryption_key() -> None:
    database_url = "postgresql+psycopg://user:pass@localhost/trading"
    defaults = Settings(database_url=database_url, _env_file=None)
    assert defaults.notification_worker_enabled is False
    assert defaults.notification_worker_batch_size == 50
    assert defaults.notification_worker_interval_seconds == 15
    assert defaults.notification_email_smtp_allowlist == ()

    missing_key = Settings(
        database_url=database_url,
        notification_worker_enabled=True,
        _env_file=None,
    )
    with pytest.raises(ValueError, match="credential encryption key"):
        missing_key.validate_runtime_security()

    configured = Settings(
        database_url=database_url,
        notification_worker_enabled=True,
        credential_encryption_key="G4dAqHdhSHI_KptQdXKVIgF_eVXWYFW3viBTPWLSBEs",
        _env_file=None,
    )
    configured.validate_runtime_security()

    allowlisted = Settings(
        database_url=database_url,
        notification_email_smtp_allowed_hosts=" SMTP.EXAMPLE.COM,mail.example.org ",
        _env_file=None,
    )
    assert allowlisted.notification_email_smtp_allowlist == (
        "smtp.example.com",
        "mail.example.org",
    )

    with pytest.raises(ValidationError, match="public DNS hostnames"):
        Settings(
            database_url=database_url,
            notification_email_smtp_allowed_hosts="localhost,127.0.0.1",
            _env_file=None,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("perptape_base_url", "http://perptape.com"),
        ("perptape_base_url", "https://perptape.example"),
        ("perptape_websocket_url", "ws://perptape.com/ws/v1/alerts"),
        ("perptape_websocket_url", "wss://perptape.com/ws/markets"),
        ("perptape_websocket_url", "wss://perptape.com/ws/v1/alerts?apiKey=secret"),
    ],
)
def test_perptape_urls_are_pinned_to_official_tls_endpoints(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match="official"):
        Settings(
            database_url="postgresql+psycopg://user:pass@localhost/trading",
            _env_file=None,
            **{field: value},
        )
