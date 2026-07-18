from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from trading_control_plane.config import get_settings
from trading_control_plane.database import Database
from trading_control_plane.service import TradingService


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
