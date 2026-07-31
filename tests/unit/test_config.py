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
    assert settings.hyperliquid_testnet_order_send_enabled is False
    assert settings.hyperliquid_core_dex == ""
    assert settings.hyperliquid_effective_account_address is None
    assert settings.hyperliquid_account_scope == "MAIN_ACCOUNT"
    assert not hasattr(settings, "hyperliquid_private_key")
    assert not hasattr(settings, "hyperliquid_vault_address")


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
