from __future__ import annotations

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select, text

from trading_control_plane.database import REQUIRED_SCHEMA_REVISION, Base, Database
from trading_control_plane.models import CapabilityGate


def test_initial_schema_round_trip_and_metadata_match(database: Database) -> None:
    config = Config("alembic.ini")

    command.downgrade(config, "base")
    command.upgrade(config, "head")

    with database.engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)

    assert revision == REQUIRED_SCHEMA_REVISION
    assert tables == {*Base.metadata.tables, "alembic_version"}
    assert differences == []


def test_initial_schema_seeds_only_disabled_capability_gates(database: Database) -> None:
    with database.session_factory() as session:
        gates = {
            row.capability_key: row.status for row in session.scalars(select(CapabilityGate)).all()
        }

    assert gates == {
        "LIVE_ORDER_SEND": "DISABLED",
        "CAPITAL_TRANSFER": "DISABLED",
        "AUTO_ADD": "DISABLED",
    }


def test_database_readiness_checks_revision_and_valid_gates(database: Database) -> None:
    assert database.is_ready() == (True, None)

    with database.engine.begin() as connection:
        connection.execute(
            text("UPDATE capability_gates SET status = 'ENABLED' WHERE capability_key = 'AUTO_ADD'")
        )
    assert database.is_ready() == (True, None)

    with database.engine.begin() as connection:
        connection.execute(
            text("DELETE FROM capability_gates WHERE capability_key = 'CAPITAL_TRANSFER'")
        )
    assert database.is_ready() == (False, "CONTROL_GATES_INVALID")

    with database.engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = 'stale'"))
    assert database.is_ready() == (False, "SCHEMA_REVISION_MISMATCH")


def test_database_readiness_fails_closed_when_postgresql_is_unavailable() -> None:
    unavailable = Database("postgresql+psycopg://test:test@127.0.0.1:1/missing_test")
    try:
        assert unavailable.is_ready() == (False, "DATABASE_UNAVAILABLE")
    finally:
        unavailable.dispose()
