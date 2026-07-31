import pytest
from pydantic import ValidationError

from trading_control_plane.config import Settings


def test_database_is_mandatory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADING_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None)  # type: ignore[call-arg]


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
    assert settings.notilt_enabled is False
    assert settings.notilt_vaults == {}
    assert not hasattr(settings, "hyperliquid_private_key")
    assert not hasattr(settings, "hyperliquid_vault_address")
    assert not hasattr(settings, "notilt_private_key")


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
        telegram_allowed_username="kelly_oooo",
        telegram_internal_username="kelly_oooo",
        _env_file=None,
    )
    configured.validate_runtime_security()


def test_live_senders_remain_default_off_and_require_explicit_credentials() -> None:
    database_url = "postgresql+psycopg://user:pass@localhost/trading"
    missing_binance = Settings(
        database_url=database_url,
        binance_live_order_send_enabled=True,
        _env_file=None,
    )
    with pytest.raises(ValueError, match="Binance LIVE send"):
        missing_binance.validate_runtime_security()

    missing_hyperliquid = Settings(
        database_url=database_url,
        hyperliquid_live_order_send_enabled=True,
        _env_file=None,
    )
    with pytest.raises(ValueError, match="Hyperliquid LIVE send"):
        missing_hyperliquid.validate_runtime_security()

    explicit = Settings(
        database_url=database_url,
        binance_live_order_send_enabled=True,
        binance_api_key="fixture-key",
        binance_api_secret="fixture-secret",  # noqa: S106
        hyperliquid_live_order_send_enabled=True,
        hyperliquid_api_wallet_address="0x1111111111111111111111111111111111111111",
        hyperliquid_api_wallet_private_key="fixture-private-key",
        _env_file=None,
    )
    explicit.validate_runtime_security()


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
            {"binance_testnet_order_send_enabled": True},
            "testnet send requires explicit",
        ),
        (
            {"hyperliquid_testnet_order_send_enabled": True},
            "testnet send requires the main account",
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
