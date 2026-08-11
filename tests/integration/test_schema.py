from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select, text

from trading_control_plane.agent import issue_agent_token
from trading_control_plane.credentials import CredentialCipher
from trading_control_plane.database import REQUIRED_SCHEMA_REVISION, Base, Database
from trading_control_plane.models import (
    AccountEquity,
    AccountEquityObservation,
    ApiClient,
    AuditEvent,
    CapabilityGate,
    ExchangeAccount,
    FundingPayment,
    Position,
    Proposal,
    ReconciliationRun,
    RiskPolicy,
    RoleAssignment,
    Team,
    TeamMembership,
    TeamSignalSource,
    User,
    VenueFill,
    VenueOrder,
    Workspace,
    WorkspaceMembership,
)
from trading_control_plane.service import TradingService


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


def test_legacy_role_bearing_agent_migrates_to_owner_inherited_api_client(
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
        credentials=None,
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
    issued = issue_agent_token(legacy_id)
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
        assert client.account_id == "legacy-agent-account"
        assert client.venue == "BINANCE"
        assert client.state == "ACTIVE"
        assert legacy_user is not None and legacy_user.active is False
        assert copied_roles == []
        assert migrated_audit is not None
        assert migrated_audit.actor_id == str(owner_id)
        assert migrated_audit.api_client_id == legacy_id
    assert authenticated["user_id"] == owner_id
    assert authenticated["api_client_id"] == legacy_id


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
        checks = {item["name"] for item in inspect(connection).get_check_constraints("teams")}
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)

    assert revision == REQUIRED_SCHEMA_REVISION
    assert "execution_mode" in columns
    assert "ck_teams_execution_mode" in checks
    assert differences == []


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


def test_scope_migrations_backfill_existing_users_roles_proposals_and_audit(
    database: Database,
) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "20260809_0015")
    user_id = uuid4()
    assignment_id = uuid4()
    audit_id = uuid4()
    proposal_audit_id = uuid4()
    instrument_id = uuid4()
    proposal_id = uuid4()
    position_id = uuid4()
    equity_id = uuid4()
    observation_id = uuid4()
    venue_order_id = uuid4()
    venue_fill_id = uuid4()
    funding_payment_id = uuid4()
    reconciliation_id = uuid4()
    risk_policy_id = uuid4()
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
                "INSERT INTO risk_policies "
                "(policy_id, version, revision, system_state, max_total_risk, "
                "max_fact_age_seconds, reason, active, updated_by, updated_at) "
                "VALUES (:policy_id, 'legacy-risk-v1', 1, 'NORMAL', 100, 30, "
                "'legacy risk policy', true, :updated_by, :updated_at)"
            ),
            {
                "policy_id": risk_policy_id,
                "updated_by": str(user_id),
                "updated_at": now,
            },
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
        connection.execute(
            text(
                "INSERT INTO instruments "
                "(instrument_id, venue, symbol, tick_size, lot_size, minimum_notional, "
                "contract_multiplier, quote_currency, collateral_currency, active, "
                "protection_supported, updated_at) VALUES "
                "(:instrument_id, 'BINANCE', 'BTCUSDT', 0.1, 0.001, 5, 1, "
                "'USDT', 'USDT', true, true, :updated_at)"
            ),
            {"instrument_id": instrument_id, "updated_at": now},
        )
        connection.execute(
            text(
                "INSERT INTO positions "
                "(position_id, account_id, venue, environment, instrument_id, quantity, "
                "average_entry_price, mark_price, fact_status, observed_at, updated_at) "
                "VALUES (:position_id, 'legacy-account', 'BINANCE', 'SHADOW', "
                ":instrument_id, 0, 0, 100, 'KNOWN', :observed_at, :updated_at)"
            ),
            {
                "position_id": position_id,
                "instrument_id": instrument_id,
                "observed_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO account_equities "
                "(account_equity_id, account_id, venue, environment, equity, "
                "available_balance, currency, fact_status, observed_at, updated_at, "
                "valuation_currency, valuation_price, valuation_equity, "
                "valuation_observed_at) VALUES "
                "(:equity_id, 'legacy-account', 'BINANCE', 'SHADOW', 1000, 900, "
                "'USDT', 'KNOWN', :observed_at, :updated_at, 'USD', 1, 1000, "
                ":observed_at)"
            ),
            {"equity_id": equity_id, "observed_at": now, "updated_at": now},
        )
        connection.execute(
            text(
                "INSERT INTO account_equity_observations "
                "(observation_id, account_equity_id, environment, location_type, "
                "account_id, venue, currency, equity, available_balance, usd_equity, "
                "observed_at, recorded_at) VALUES "
                "(:observation_id, :equity_id, 'SHADOW', 'VENUE', 'legacy-account', "
                "'BINANCE', 'USDT', 1000, 900, 1000, :observed_at, :recorded_at)"
            ),
            {
                "observation_id": observation_id,
                "equity_id": equity_id,
                "observed_at": now,
                "recorded_at": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO venue_orders "
                "(venue_order_fact_id, order_intent_id, account_id, venue, environment, "
                "instrument_id, venue_order_id, client_order_id, side, order_type, "
                "reduce_only, status, ordered_quantity, filled_quantity, observed_at, "
                "updated_at) VALUES "
                "(:fact_id, NULL, 'legacy-account', 'BINANCE', 'SHADOW', :instrument_id, "
                "'legacy-order', 'legacy-client-order', 'BUY', 'LIMIT', false, 'FILLED', "
                "1, 1, :observed_at, :updated_at)"
            ),
            {
                "fact_id": venue_order_id,
                "instrument_id": instrument_id,
                "observed_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO venue_fills "
                "(venue_fill_fact_id, venue, venue_fill_id, order_intent_id, campaign_id, "
                "account_id, environment, instrument_id, side, quantity, price, fee, "
                "fee_currency, slippage_cost, executed_at) VALUES "
                "(:fact_id, 'BINANCE', 'legacy-fill', NULL, NULL, 'legacy-account', "
                "'SHADOW', :instrument_id, 'BUY', 1, 100, 0, 'USDT', 0, :executed_at)"
            ),
            {
                "fact_id": venue_fill_id,
                "instrument_id": instrument_id,
                "executed_at": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO funding_payments "
                "(funding_payment_id, campaign_id, account_id, venue, environment, "
                "instrument_id, venue_payment_id, amount, currency, paid_at) VALUES "
                "(:payment_id, NULL, 'legacy-account', 'BINANCE', 'SHADOW', "
                ":instrument_id, 'legacy-funding', 0, 'USDT', :paid_at)"
            ),
            {
                "payment_id": funding_payment_id,
                "instrument_id": instrument_id,
                "paid_at": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO reconciliation_runs "
                "(reconciliation_id, execution_scope, campaign_id, status, is_computed, "
                "differences, resolution_reason, actor_id, correlation_id, started_at, "
                "completed_at) VALUES "
                "(:reconciliation_id, 'SHADOW:legacy-account:BINANCE', NULL, 'MATCH', "
                "true, CAST(:differences AS jsonb), NULL, :actor_id, :correlation_id, "
                ":started_at, :completed_at)"
            ),
            {
                "reconciliation_id": reconciliation_id,
                "differences": "[]",
                "actor_id": str(user_id),
                "correlation_id": uuid4(),
                "started_at": now,
                "completed_at": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO proposals "
                "(proposal_id, source, environment, proposer_id, status, version, "
                "risk_tier, account_id, venue, instrument_id, direction, quantity, "
                "max_risk, frozen_payload, semantic_hash, expires_at, correlation_id, "
                "created_at, updated_at) VALUES "
                "(:proposal_id, 'MANUAL', 'SHADOW', :proposer_id, 'DRAFT', 1, "
                "'LOW', 'legacy-account', 'BINANCE', :instrument_id, 'LONG', 0.01, "
                "10, CAST(:frozen_payload AS jsonb), :semantic_hash, :expires_at, "
                ":correlation_id, :created_at, :updated_at)"
            ),
            {
                "proposal_id": proposal_id,
                "proposer_id": user_id,
                "instrument_id": instrument_id,
                "frozen_payload": '{"legacy": true}',
                "semantic_hash": "0" * 64,
                "expires_at": now + timedelta(hours=1),
                "correlation_id": uuid4(),
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO audit_events "
                "(audit_event_id, actor_id, event_type, object_type, object_id, reason, "
                "correlation_id, object_version, created_at) VALUES "
                "(:audit_id, :actor_id, 'PROPOSAL_CREATED', 'Proposal', :object_id, "
                "'legacy proposal', :correlation_id, 1, :created_at)"
            ),
            {
                "audit_id": proposal_audit_id,
                "actor_id": str(user_id),
                "object_id": str(proposal_id),
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
        proposal = session.get(Proposal, proposal_id)
        proposal_audit = session.get(AuditEvent, proposal_audit_id)
        legacy_facts = (
            session.get(Position, position_id),
            session.get(AccountEquity, equity_id),
            session.get(AccountEquityObservation, observation_id),
            session.get(VenueOrder, venue_order_id),
            session.get(VenueFill, venue_fill_id),
            session.get(FundingPayment, funding_payment_id),
            session.get(ReconciliationRun, reconciliation_id),
        )
        exchange_account = session.scalar(
            select(ExchangeAccount).where(
                ExchangeAccount.team_id == team.team_id,
                ExchangeAccount.account_id == "legacy-account",
                ExchangeAccount.venue == "BINANCE",
            )
        )
        signal_source = session.scalar(
            select(TeamSignalSource).where(TeamSignalSource.team_id == team.team_id)
        )
        assert user is not None and workspace is not None and team is not None
        assert user.active_workspace_id == workspace.workspace_id
        assert user.active_team_id == team.team_id
        assert team.workspace_id == workspace.workspace_id
        assert team.trading_enabled is True
        assert assignment is not None and assignment.team_id == team.team_id
        assert audit is not None and audit.workspace_id == workspace.workspace_id
        assert audit.team_id == team.team_id
        assert audit.account_id is None
        assert proposal is not None and proposal.team_id == team.team_id
        assert all(fact is not None and fact.team_id == team.team_id for fact in legacy_facts)
        assert exchange_account is not None
        assert exchange_account.registration_source == "MIGRATION"
        assert exchange_account.connection_status == "UNCONFIGURED"
        assert exchange_account.trading_status == "DISABLED"
        assert signal_source is not None
        assert signal_source.mode == "PERPTAPE"
        assert signal_source.enabled is True
        assert signal_source.credential_ciphertext is None
        assert signal_source.credential_metadata["credential_source"] == "RUNTIME_FALLBACK"
        assert proposal_audit is not None
        assert proposal_audit.workspace_id == workspace.workspace_id
        assert proposal_audit.team_id == team.team_id
        assert proposal_audit.account_id == "legacy-account"
        assert session.scalar(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace.workspace_id,
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.role == "ADMIN",
                WorkspaceMembership.active,
            )
        )
        migrated_policy = session.get(RiskPolicy, risk_policy_id)
        assert migrated_policy is not None
        assert migrated_policy.team_id == team.team_id
        assert migrated_policy.max_total_risk == 100
        assert migrated_policy.max_account_risk is None
        assert migrated_policy.max_single_loss is None
        assert migrated_policy.max_consecutive_losses is None
        assert migrated_policy.loss_cooldown_seconds is None
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
