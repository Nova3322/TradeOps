from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select, text

from trading_control_plane.database import REQUIRED_SCHEMA_REVISION, Base, Database
from trading_control_plane.models import (
    AuditEvent,
    CapabilityGate,
    RoleAssignment,
    Team,
    TeamMembership,
    User,
    Workspace,
    WorkspaceMembership,
)


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
        "AUTO_PROFIT_SWEEP": "DISABLED",
        "AUTO_OPERATING_REFILL": "DISABLED",
    }


def test_workspace_team_migration_backfills_existing_users_roles_and_audit(
    database: Database,
) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "20260809_0015")
    user_id = uuid4()
    assignment_id = uuid4()
    audit_id = uuid4()
    now = datetime.now(UTC)
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users "
                "(user_id, username, principal_type, active, auth_version, created_at) "
                "VALUES (:user_id, :username, 'HUMAN', true, 1, :created_at)"
            ),
            {"user_id": user_id, "username": "legacy-admin", "created_at": now},
        )
        connection.execute(
            text(
                "INSERT INTO role_assignments "
                "(assignment_id, user_id, role, created_at) "
                "VALUES (:assignment_id, :user_id, 'SYSTEM_ADMIN', :created_at)"
            ),
            {"assignment_id": assignment_id, "user_id": user_id, "created_at": now},
        )
        connection.execute(
            text(
                "INSERT INTO audit_events "
                "(audit_event_id, actor_id, event_type, object_type, object_id, reason, "
                "correlation_id, object_version, created_at) VALUES "
                "(:audit_id, :actor_id, 'LEGACY_EVENT', 'User', :object_id, "
                "'legacy event', :correlation_id, 1, :created_at)"
            ),
            {
                "audit_id": audit_id,
                "actor_id": str(user_id),
                "object_id": str(user_id),
                "correlation_id": uuid4(),
                "created_at": now,
            },
        )

    command.upgrade(config, "head")

    with database.session_factory() as session:
        user = session.get(User, user_id)
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        team = session.scalar(select(Team).where(Team.slug == "default"))
        assignment = session.get(RoleAssignment, assignment_id)
        audit = session.get(AuditEvent, audit_id)
        assert user is not None and workspace is not None and team is not None
        assert user.active_workspace_id == workspace.workspace_id
        assert user.active_team_id == team.team_id
        assert team.workspace_id == workspace.workspace_id
        assert team.trading_enabled is True
        assert assignment is not None and assignment.team_id == team.team_id
        assert audit is not None and audit.workspace_id == workspace.workspace_id
        assert audit.team_id == team.team_id
        assert session.scalar(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace.workspace_id,
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.role == "ADMIN",
                WorkspaceMembership.active,
            )
        )
        assert session.scalar(
            select(TeamMembership).where(
                TeamMembership.team_id == team.team_id,
                TeamMembership.user_id == user_id,
                TeamMembership.active,
            )
        )


def test_database_readiness_checks_revision_and_valid_gates(database: Database) -> None:
    try:
        assert database.is_ready() == (True, None)

        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE capability_gates SET status = 'ENABLED' "
                    "WHERE capability_key = 'AUTO_ADD'"
                )
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
    finally:
        with database.engine.begin() as connection:
            connection.execute(
                text("UPDATE alembic_version SET version_num = :revision"),
                {"revision": REQUIRED_SCHEMA_REVISION},
            )
            connection.execute(
                text(
                    "INSERT INTO capability_gates "
                    "(capability_key, status, reason, operator_id, updated_at) "
                    "VALUES ('CAPITAL_TRANSFER', 'DISABLED', 'initial default', "
                    "'migration', now()) ON CONFLICT (capability_key) DO UPDATE "
                    "SET status = 'DISABLED'"
                )
            )
            connection.execute(
                text(
                    "UPDATE capability_gates SET status = 'DISABLED' "
                    "WHERE capability_key = 'AUTO_ADD'"
                )
            )


def test_database_readiness_fails_closed_when_postgresql_is_unavailable() -> None:
    unavailable = Database("postgresql+psycopg://test:test@127.0.0.1:1/missing_test")
    try:
        assert unavailable.is_ready() == (False, "DATABASE_UNAVAILABLE")
    finally:
        unavailable.dispose()
