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
