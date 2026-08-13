from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from trading_control_plane.config import get_settings
from trading_control_plane.database import Database
from trading_control_plane.models import Team, User
from trading_control_plane.service import TradingService


def set_test_team_environment(
    database: Database,
    user_id: UUID,
    environment: str,
) -> None:
    """Bind legacy workflow fixtures to the environment they exercise.

    Production bootstrap Teams are intentionally LIVE and locked. Integration
    fixtures that test older SHADOW/TESTNET workflow primitives must opt into
    their environment instead of relying on an unlocked LIVE Team.
    """
    with database.session_factory.begin() as session:
        user = session.get(User, user_id)
        assert user is not None and user.active_team_id is not None
        team = session.get(Team, user.active_team_id, with_for_update=True)
        assert team is not None
        team.execution_mode = environment
        team.execution_mode_locked_at = None


@pytest.fixture()
def database() -> Iterator[Database]:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    parsed = make_url(database_url)
    if parsed.get_backend_name() != "postgresql" or not (parsed.database or "").endswith("_test"):
        raise RuntimeError("integration tests require a disposable PostgreSQL *_test database")

    previous_url = os.environ.get("TRADING_DATABASE_URL")
    os.environ["TRADING_DATABASE_URL"] = database_url
    get_settings.cache_clear()

    engine = create_engine(database_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            connection.exec_driver_sql("CREATE SCHEMA public")
    finally:
        engine.dispose()

    command.upgrade(Config("alembic.ini"), "head")
    instance = Database(database_url)
    try:
        yield instance
    finally:
        instance.dispose()
        if previous_url is None:
            os.environ.pop("TRADING_DATABASE_URL", None)
        else:
            os.environ["TRADING_DATABASE_URL"] = previous_url
        get_settings.cache_clear()


@pytest.fixture()
def service(database: Database) -> TradingService:
    return TradingService(database)
