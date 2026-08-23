from __future__ import annotations

import base64
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select, text

from trading_control_plane.agent import AGENT_TOKEN_MARKER, IssuedAgentToken
from trading_control_plane.credentials import CredentialCipher
from trading_control_plane.database import REQUIRED_SCHEMA_REVISION, Base, Database
from trading_control_plane.models import (
    ApiClient,
    AuditEvent,
    CapabilityGate,
    RoleAssignment,
    User,
)
from trading_control_plane.service import TradingService


def issue_legacy_agent_token(agent_id: UUID) -> IssuedAgentToken:
    secret = secrets.token_urlsafe(32)
    return IssuedAgentToken(
        token=f"{AGENT_TOKEN_MARKER}.{agent_id}.{secret}",
        hint=f"{AGENT_TOKEN_MARKER}.…{secret[-4:]}",
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


def test_legacy_role_bearing_agent_migrates_to_owner_inherited_api_key(
    database: Database,
) -> None:
    config = Config("alembic.ini")
    now = datetime.now(UTC)
    encryption_key = base64.urlsafe_b64encode(b"legacy-agent-migration-key-32byt"[:32]).decode(
        "ascii"
    )
    service = TradingService(database, credential_encryption_key=encryption_key)
    owner_id = service.bootstrap_admin("legacy-agent-owner", now=now)
    service.create_exchange_account(
        actor_id=owner_id,
        account_id="legacy-agent-account",
        venue="BINANCE",
        label="Legacy Agent Account",
        credentials={"api_key": "legacy-key", "api_secret": "legacy-secret"},
        idempotency_key="legacy-agent-account",
        now=now,
    )
    with database.session_factory() as session:
        owner = session.get(User, owner_id)
        assert owner is not None
        assert owner.active_workspace_id is not None
        assert owner.active_team_id is not None
        workspace_id = owner.active_workspace_id
        team_id = owner.active_team_id

    command.downgrade(config, "20260811_0031")
    legacy_id = uuid4()
    issued = issue_legacy_agent_token(legacy_id)
    digest = CredentialCipher(encryption_key).secret_fingerprint(
        issued.token,
        purpose=f"agent-api-token:{legacy_id}:v1",
    )
    audit_id = uuid4()
    with database.engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO users (
                    user_id, username, auth_version, active_workspace_id, active_team_id,
                    principal_type, service_kind, agent_token_digest, agent_token_hint,
                    agent_token_version, agent_token_created_at, agent_token_expires_at,
                    active, created_at
                ) VALUES (
                    :user_id, :username, 1, :workspace_id, :team_id,
                    'SERVICE', 'AGENT', :digest, :hint, 1, :created_at, :expires_at,
                    true, :created_at
                )
                """
            ),
            {
                "user_id": legacy_id,
                "username": "legacy-role-agent",
                "workspace_id": workspace_id,
                "team_id": team_id,
                "digest": digest,
                "hint": issued.hint,
                "created_at": now,
                "expires_at": now + timedelta(days=30),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO workspace_memberships (
                    membership_id, workspace_id, user_id, role, active, invited_by,
                    created_at, updated_at
                ) VALUES (
                    :membership_id, :workspace_id, :user_id, 'MEMBER', true, :owner_id,
                    :created_at, :created_at
                )
                """
            ),
            {
                "membership_id": uuid4(),
                "workspace_id": workspace_id,
                "user_id": legacy_id,
                "owner_id": owner_id,
                "created_at": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO team_memberships (
                    membership_id, team_id, user_id, active, invited_by, created_at, updated_at
                ) VALUES (
                    :membership_id, :team_id, :user_id, true, :owner_id, :created_at, :created_at
                )
                """
            ),
            {
                "membership_id": uuid4(),
                "team_id": team_id,
                "user_id": legacy_id,
                "owner_id": owner_id,
                "created_at": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO role_assignments (
                    assignment_id, user_id, team_id, role, account_scope, venue_scope, created_at
                ) VALUES (
                    :assignment_id, :user_id, :team_id, 'PROPOSER',
                    'legacy-agent-account', 'BINANCE', :created_at
                )
                """
            ),
            {
                "assignment_id": uuid4(),
                "user_id": legacy_id,
                "team_id": team_id,
                "created_at": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO audit_events (
                    audit_event_id, workspace_id, team_id, account_id, actor_id,
                    event_type, object_type, object_id, reason, correlation_id,
                    idempotency_key, object_version, created_at
                ) VALUES (
                    :audit_id, :workspace_id, :team_id, 'legacy-agent-account', :actor_id,
                    'LEGACY_AGENT_USED', 'User', :object_id, 'legacy attributed event',
                    :correlation_id, NULL, 1, :created_at
                )
                """
            ),
            {
                "audit_id": audit_id,
                "workspace_id": workspace_id,
                "team_id": team_id,
                "actor_id": str(legacy_id),
                "object_id": str(legacy_id),
                "correlation_id": uuid4(),
                "created_at": now,
            },
        )

    command.upgrade(config, "head")
    migrated_service = TradingService(database, credential_encryption_key=encryption_key)
    authenticated = migrated_service.authenticate_api_client_token(
        issued.token, now=now + timedelta(minutes=1)
    )
    with database.session_factory() as session:
        client = session.get(ApiClient, legacy_id)
        legacy_user = session.get(User, legacy_id)
        copied_roles = session.scalars(
            select(RoleAssignment).where(RoleAssignment.user_id == legacy_id)
        ).all()
        migrated_audit = session.get(AuditEvent, audit_id)

        assert client is not None
        assert client.owner_user_id == owner_id
        assert client.account_id is None
        assert client.venue is None
        assert client.state == "ACTIVE"
        assert legacy_user is not None and legacy_user.active is False
        assert copied_roles == []
        assert migrated_audit is not None
        assert migrated_audit.actor_id == str(owner_id)
        assert migrated_audit.api_client_id == legacy_id
    assert authenticated["user_id"] == owner_id
    assert authenticated["api_client_id"] == legacy_id
    assert authenticated["account_id"] is None
    assert authenticated["venue"] is None


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


def test_durable_freqtrade_dispatch_migration_round_trips(database: Database) -> None:
    config = Config("alembic.ini")

    command.downgrade(config, "20260811_0030")
    with database.engine.connect() as connection:
        columns = {item["name"] for item in inspect(connection).get_columns("order_intents")}
        assert "dispatch_backend" not in columns

    command.upgrade(config, "head")
    with database.engine.connect() as connection:
        columns = {item["name"] for item in inspect(connection).get_columns("order_intents")}
        checks = {
            item["name"]: str(item["sqltext"])
            for item in inspect(connection).get_check_constraints("order_intents")
        }
        indexes = {item["name"]: item for item in inspect(connection).get_indexes("order_intents")}
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)

    assert revision == REQUIRED_SCHEMA_REVISION
    assert {
        "dispatch_backend",
        "dispatch_account_version",
        "dispatch_auth_version",
        "dispatch_owner_id",
        "dispatch_fencing_token",
        "dispatch_external_id",
        "dispatch_started_at",
    } <= columns
    assert "DISPATCHING" in checks["ck_order_intents_status"]
    assert "DISPATCHING" in str(indexes["uq_order_intents_one_active_campaign"]["dialect_options"])
    assert differences == []


def test_account_bound_freqtrade_worker_migration_guards_data_and_round_trips(
    database: Database,
) -> None:
    config = Config("alembic.ini")
    now = datetime.now(UTC)
    encryption_key = (
        base64.urlsafe_b64encode(b"schema-worker-fixture-key-32-byte"[:32]).decode().rstrip("=")
    )
    service = TradingService(database, credential_encryption_key=encryption_key)
    admin = service.bootstrap_admin("schema-worker-admin", now=now)
    account_id = service.create_exchange_account(
        actor_id=admin,
        account_id="schema-worker-account",
        venue="BINANCE",
        label="Schema Worker Account",
        credentials={"api_key": "fixture-key", "api_secret": "fixture-secret"},
        idempotency_key="schema-worker-account-create",
        now=now,
    )
    configured = service.configure_exchange_account_freqtrade_worker(
        account_id,
        actor_id=admin,
        mode="LIVE",
        name="schema-worker",
        base_url="http://127.0.0.1:18081",
        username="schema-user",
        password="schema-password",  # noqa: S106
        ws_token="schema-rpc-token-fixture",  # noqa: S106
        hip3_dexes=(),
        expected_version=1,
        idempotency_key="schema-worker-configure",
        now=now,
    )

    with pytest.raises(
        RuntimeError,
        match="clearing every account-bound Freqtrade worker",
    ):
        command.downgrade(config, "20260811_0028")

    service.configure_exchange_account_freqtrade_worker(
        account_id,
        actor_id=admin,
        mode="UNCONFIGURED",
        name=None,
        base_url=None,
        username=None,
        password=None,
        hip3_dexes=(),
        expected_version=int(configured["version"]),
        idempotency_key="schema-worker-clear",
        now=now,
    )
    command.downgrade(config, "20260811_0028")
    with database.engine.connect() as connection:
        columns = {item["name"] for item in inspect(connection).get_columns("exchange_accounts")}
        assert "freqtrade_worker_mode" not in columns
    command.upgrade(config, "head")
    with database.engine.connect() as connection:
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert revision == REQUIRED_SCHEMA_REVISION
    assert differences == []


@pytest.mark.parametrize("venue", ["OKX", "BYBIT"])
def test_okx_bybit_freqtrade_constraint_migration_guards_data_and_round_trips(
    database: Database, venue: str
) -> None:
    config = Config("alembic.ini")
    now = datetime.now(UTC)
    encryption_key = base64.urlsafe_b64encode(b"s" * 32).decode().rstrip("=")
    service = TradingService(database, credential_encryption_key=encryption_key)
    slug = venue.lower()
    admin = service.bootstrap_admin(f"schema-{slug}-worker-admin", now=now)
    account_id = service.create_exchange_account(
        actor_id=admin,
        account_id=f"schema-{slug}-worker-account",
        venue=venue,
        label=f"Schema {venue} Worker Account",
        credentials={
            "api_key": "fixture-key",
            "api_secret": "fixture-secret",
            **({"passphrase": "fixture-passphrase"} if venue == "OKX" else {}),
        },
        idempotency_key=f"schema-{slug}-worker-account-create",
        now=now,
    )
    configured = service.configure_exchange_account_freqtrade_worker(
        account_id,
        actor_id=admin,
        mode="LIVE",
        name=f"schema-{slug}-worker",
        base_url="http://127.0.0.1:18084",
        username="schema-user",
        password="schema-password",  # noqa: S106
        ws_token=f"schema-{slug}-rpc-token",
        hip3_dexes=(),
        expected_version=1,
        idempotency_key=f"schema-{slug}-worker-configure",
        now=now,
    )

    with pytest.raises(
        RuntimeError,
        match="clearing every OKX/Bybit account-bound Freqtrade worker",
    ):
        command.downgrade(config, "20260811_0029")

    service.configure_exchange_account_freqtrade_worker(
        account_id,
        actor_id=admin,
        mode="UNCONFIGURED",
        name=None,
        base_url=None,
        username=None,
        password=None,
        hip3_dexes=(),
        expected_version=int(configured["version"]),
        idempotency_key=f"schema-{slug}-worker-clear",
        now=now,
    )
    command.downgrade(config, "20260811_0029")
    with database.engine.connect() as connection:
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    assert revision == "20260811_0029"

    command.upgrade(config, "head")
    with database.engine.connect() as connection:
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert revision == REQUIRED_SCHEMA_REVISION
    assert differences == []


def test_notification_migration_round_trip(database: Database) -> None:
    config = Config("alembic.ini")

    command.downgrade(config, "20260810_0022")
    with database.engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        assert "notification_routes" not in tables
        assert "notification_deliveries" not in tables

    command.upgrade(config, "head")
    with database.engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()

    assert revision == REQUIRED_SCHEMA_REVISION
    assert {"notification_routes", "notification_deliveries"}.issubset(tables)
    assert differences == []


def test_team_execution_mode_migration_round_trip(database: Database) -> None:
    config = Config("alembic.ini")

    command.downgrade(config, "20260810_0023")
    with database.engine.connect() as connection:
        columns = {item["name"] for item in inspect(connection).get_columns("teams")}
        assert "execution_mode" not in columns

    command.upgrade(config, "head")
    with database.engine.connect() as connection:
        columns = {item["name"] for item in inspect(connection).get_columns("teams")}
        checks = {
            item["name"]: item["sqltext"]
            for item in inspect(connection).get_check_constraints("teams")
        }
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)

    assert revision == REQUIRED_SCHEMA_REVISION
    assert "execution_mode" in columns
    assert "ck_teams_execution_mode" in checks
    assert "TESTNET" in str(checks["ck_teams_execution_mode"])
    assert differences == []


def test_team_execution_environment_lock_migration_round_trip(database: Database) -> None:
    config = Config("alembic.ini")

    command.downgrade(config, "20260812_0034")
    with database.engine.connect() as connection:
        columns = {item["name"] for item in inspect(connection).get_columns("teams")}
        assert "execution_mode_locked_at" not in columns

    command.upgrade(config, "head")
    with database.engine.connect() as connection:
        columns = {item["name"] for item in inspect(connection).get_columns("teams")}
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)

    assert revision == REQUIRED_SCHEMA_REVISION
    assert "execution_mode_locked_at" in columns
    assert differences == []


def test_user_rbac_scope_migration_backfills_team_lock_and_exact_report_scope(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database)
    service.bootstrap_admin("scope-migration-admin", now=now)
    config = Config("alembic.ini")

    command.downgrade(config, "20260812_0039")
    with database.engine.begin() as connection:
        connection.execute(text("UPDATE teams SET execution_mode_locked_at = NULL"))
    command.upgrade(config, "head")

    with database.engine.connect() as connection:
        client_columns = {
            item["name"]: item for item in inspect(connection).get_columns("api_clients")
        }
        client_fks = {item["name"] for item in inspect(connection).get_foreign_keys("api_clients")}
        client_indexes = {item["name"] for item in inspect(connection).get_indexes("api_clients")}
        report_columns = {
            item["name"] for item in inspect(connection).get_columns("analytics_reports")
        }
        unlocked = connection.execute(
            text("SELECT count(*) FROM teams WHERE execution_mode_locked_at IS NULL")
        ).scalar_one()

    assert client_columns["account_id"]["nullable"] is True
    assert client_columns["venue"]["nullable"] is True
    assert "fk_api_clients_exchange_account_scope" not in client_fks
    assert "ix_api_clients_team" in client_indexes
    assert "account_scopes" in report_columns
    assert unlocked == 0


def test_agent_credential_migration_backfills_internal_services_and_round_trips(
    database: Database,
) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "20260810_0024")
    service_user_id = uuid4()
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users "
                "(user_id, username, principal_type, active, auth_version, created_at) "
                "VALUES (:user_id, 'pre-agent-service', 'SERVICE', true, 1, :created_at)"
            ),
            {"user_id": service_user_id, "created_at": datetime.now(UTC)},
        )

    command.upgrade(config, "head")
    with database.engine.connect() as connection:
        columns = {item["name"] for item in inspect(connection).get_columns("users")}
        checks = {item["name"] for item in inspect(connection).get_check_constraints("users")}
        row = connection.execute(
            text(
                "SELECT service_kind, agent_token_version, agent_token_digest "
                "FROM users WHERE user_id = :user_id"
            ),
            {"user_id": service_user_id},
        ).one()
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)

    assert {
        "service_kind",
        "agent_token_digest",
        "agent_token_hint",
        "agent_token_version",
        "agent_token_created_at",
        "agent_token_expires_at",
        "agent_token_last_used_at",
    }.issubset(columns)
    assert {
        "ck_users_service_kind",
        "ck_users_agent_token_version",
        "ck_users_agent_token_shape",
    }.issubset(checks)
    assert row == ("INTERNAL", 0, None)
    assert differences == []


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
