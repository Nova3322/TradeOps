from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from trading_control_plane.config import get_settings
from trading_control_plane.database import Database


@pytest.fixture(scope="session")
def database() -> Iterator[Database]:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")

    previous_url = os.environ.get("TRADING_DATABASE_URL")
    os.environ["TRADING_DATABASE_URL"] = database_url
    get_settings.cache_clear()

    alembic_config = Config("alembic.ini")
    command.upgrade(alembic_config, "head")
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


@pytest.fixture(autouse=True)
def reset_database(database: Database) -> None:
    with database.engine.begin() as connection:
        connection.execute(
            text(
                """
                TRUNCATE TABLE
                    shadow_dispatch_claims,
                    execution_sender_scope_state_history,
                    execution_sender_scope_states,
                    execution_sender_leases,
                    execution_sender_scopes,
                    capability_certificate_state_history,
                    capability_certificate_states,
                    capability_certificates,
                    capability_evidence_bundles,
                    risk_exposure_state_history,
                    order_intent_state_history,
                    risk_ledger_entries,
                    risk_exposure_states,
                    risk_reservations,
                    execution_facts,
                    order_intent_states,
                    order_intents,
                    execution_risk_decisions,
                    authorization_state_transitions,
                    add_unit_states,
                    add_units,
                    add_authorization_package_states,
                    add_authorization_packages,
                    initial_authorization_states,
                    initial_order_authorizations,
                    campaign_states,
                    campaigns,
                    trading_authorizations,
                    risk_decision_snapshots,
                    risk_policies,
                    system_risk_state_transitions,
                    reviewer_votes,
                    approval_decisions,
                    system_risk_states,
                    proposal_version_states,
                    proposal_versions,
                    authorization_decisions,
                    action_assurances,
                    explicit_denies,
                    permission_scopes,
                    role_assignments,
                    identity_principals,
                    command_receipts,
                    audit_events,
                    outbox_messages,
                    inbox_receipts
                RESTART IDENTITY CASCADE
                """
            )
        )
        connection.execute(
            text(
                """
                UPDATE capability_gates
                SET status = 'DISABLED',
                    version = 1,
                    reason = 'integration test reset',
                    policy_version = 'WP-0001',
                    certificate_ref = NULL,
                    updated_at = now()
                """
            )
        )
