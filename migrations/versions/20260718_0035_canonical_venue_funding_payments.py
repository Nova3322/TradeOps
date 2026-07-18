"""Add canonical settled venue funding-payment facts.

Revision ID: 20260718_0035
Revises: 20260718_0034
Create Date: 2026-07-18
"""

# ruff: noqa: S608 -- all interpolated SQL fragments are fixed migration constants.

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0035"
down_revision: str | Sequence[str] | None = "20260718_0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OLD_SOURCES = (
    "'TRADING_LEDGER', 'VENUE_ORDERS', 'VENUE_FILLS', "
    "'VENUE_POSITIONS', 'VENUE_BALANCES', 'VENUE_PROTECTION', 'WORKER_LOCAL'"
)
NEW_SOURCES = (
    "'TRADING_LEDGER', 'VENUE_ORDERS', 'VENUE_FILLS', 'VENUE_FUNDING', "
    "'VENUE_POSITIONS', 'VENUE_BALANCES', 'VENUE_PROTECTION', 'WORKER_LOCAL'"
)


def upgrade() -> None:
    _replace_reconciliation_constraints(include_funding=True)
    op.create_table(
        "venue_funding_payments",
        sa.Column("venue_funding_payment_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("first_seen_run_id", sa.Uuid(), nullable=False),
        sa.Column("first_seen_input_id", sa.Uuid(), nullable=False),
        sa.Column("venue", sa.String(length=80), nullable=False),
        sa.Column("execution_domain", sa.String(length=120), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=False),
        sa.Column("instrument_id", sa.String(length=255), nullable=False),
        sa.Column("venue_payment_id", sa.String(length=255), nullable=False),
        sa.Column("position_side", sa.String(length=20), nullable=False),
        sa.Column("margin_mode", sa.String(length=80), nullable=False),
        sa.Column("collateral_pool_id", sa.String(length=160), nullable=False),
        sa.Column("funding_amount", sa.Numeric(38, 18), nullable=False),
        sa.Column("funding_currency", sa.String(length=80), nullable=False),
        sa.Column("funding_effect", sa.String(length=20), nullable=False),
        sa.Column("venue_confirmed", sa.Boolean(), nullable=False),
        sa.Column("fact_authority", sa.String(length=32), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("live_dispatch_eligible", sa.Boolean(), nullable=False),
        sa.Column("source_version", sa.String(length=160), nullable=False),
        sa.Column("normalization_version", sa.String(length=160), nullable=False),
        sa.Column(
            "normalized_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("raw_payload_ref", sa.String(length=255), nullable=False),
        sa.Column("raw_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence_ref", sa.String(length=255), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("funding_hash", sa.String(length=64), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("venue_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "position_side IN ('LONG', 'SHORT', 'BOTH')",
            name="ck_venue_funding_payments_position_side",
        ),
        sa.CheckConstraint(
            "(funding_effect = 'PAYMENT' AND funding_amount > 0) OR "
            "(funding_effect = 'RECEIPT' AND funding_amount < 0) OR "
            "(funding_effect = 'ZERO' AND funding_amount = 0)",
            name="ck_venue_funding_payments_effect",
        ),
        sa.CheckConstraint(
            "event_time <= venue_observed_at AND venue_observed_at <= first_received_at "
            "AND first_received_at <= recorded_at",
            name="ck_venue_funding_payments_time_order",
        ),
        sa.CheckConstraint(
            "venue_confirmed AND fact_authority = 'VENUE_PRIVATE' "
            "AND environment = 'SHADOW' AND live_dispatch_eligible = false",
            name="ck_venue_funding_payments_authority",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(normalized_payload) = 'object' "
            "AND length(raw_payload_hash) = 64 AND length(evidence_hash) = 64 "
            "AND length(funding_hash) = 64",
            name="ck_venue_funding_payments_integrity",
        ),
        sa.ForeignKeyConstraint(
            ["first_seen_input_id"],
            ["execution_reconciliation_inputs.input_id"],
            name="fk_venue_funding_payments_first_input",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["first_seen_run_id", "organization_id"],
            [
                "execution_reconciliation_runs.run_id",
                "execution_reconciliation_runs.organization_id",
            ],
            name="fk_venue_funding_payments_first_run_org",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("venue_funding_payment_id"),
        sa.UniqueConstraint(
            "organization_id",
            "venue",
            "execution_domain",
            "account_id",
            "venue_payment_id",
            name="uq_venue_funding_payments_external_payment",
        ),
    )
    op.create_index(
        "ix_venue_funding_payments_scope_time",
        "venue_funding_payments",
        ["venue", "execution_domain", "account_id", "instrument_id", "event_time"],
    )
    op.add_column(
        "venue_fact_input_links",
        sa.Column("venue_funding_payment_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_venue_fact_input_links_funding_payment",
        "venue_fact_input_links",
        "venue_funding_payments",
        ["venue_funding_payment_id"],
        ["venue_funding_payment_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_venue_fact_input_links_funding_fact",
        "venue_fact_input_links",
        ["reconciliation_input_id", "venue_funding_payment_id"],
    )
    _replace_link_constraint(include_funding=True)
    _replace_run_insert_guard(include_funding=True)
    _create_funding_insert_guard()
    _replace_input_link_guard(include_funding=True)
    _replace_first_link_guard(include_funding=True)
    _replace_manifest_guard(include_funding=True)
    op.execute(
        """
        CREATE TRIGGER venue_funding_payments_immutable
        BEFORE UPDATE OR DELETE ON venue_funding_payments
        FOR EACH ROW EXECUTE FUNCTION deny_venue_fact_change()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER venue_funding_payments_first_link_guard
        AFTER INSERT ON venue_funding_payments
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION verify_canonical_venue_fact_first_link()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM venue_funding_payments)
               OR EXISTS (
                    SELECT 1 FROM execution_reconciliation_runs WHERE schema_version = 2
               ) THEN
                RAISE EXCEPTION
                    'cannot downgrade while canonical funding or v2 reconciliation evidence exists';
            END IF;
        END;
        $$
        """
    )
    _replace_run_insert_guard(include_funding=False)
    _replace_input_link_guard(include_funding=False)
    _replace_first_link_guard(include_funding=False)
    _replace_manifest_guard(include_funding=False)
    op.drop_constraint(
        "ck_venue_fact_input_links_exact_fact",
        "venue_fact_input_links",
        type_="check",
    )
    op.drop_constraint(
        "uq_venue_fact_input_links_funding_fact",
        "venue_fact_input_links",
        type_="unique",
    )
    op.drop_constraint(
        "fk_venue_fact_input_links_funding_payment",
        "venue_fact_input_links",
        type_="foreignkey",
    )
    op.drop_column("venue_fact_input_links", "venue_funding_payment_id")
    _replace_link_constraint(include_funding=False, drop_existing=False)
    op.drop_index(
        "ix_venue_funding_payments_scope_time",
        table_name="venue_funding_payments",
    )
    op.drop_table("venue_funding_payments")
    op.execute("DROP FUNCTION IF EXISTS protect_venue_funding_payment_insert()")
    _replace_reconciliation_constraints(include_funding=False)


def _replace_reconciliation_constraints(*, include_funding: bool) -> None:
    op.drop_constraint(
        "ck_execution_reconciliation_runs_schema",
        "execution_reconciliation_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_execution_reconciliation_runs_sources",
        "execution_reconciliation_runs",
        type_="check",
    )
    if include_funding:
        op.create_check_constraint(
            "ck_execution_reconciliation_runs_schema",
            "execution_reconciliation_runs",
            "(schema_version = 1 AND jsonb_array_length(required_source_types) = 7) OR "
            "(schema_version = 2 AND jsonb_array_length(required_source_types) = 8)",
        )
        op.create_check_constraint(
            "ck_execution_reconciliation_runs_sources",
            "execution_reconciliation_runs",
            "jsonb_typeof(required_source_types) = 'array'",
        )
    else:
        op.create_check_constraint(
            "ck_execution_reconciliation_runs_schema",
            "execution_reconciliation_runs",
            "schema_version = 1",
        )
        op.create_check_constraint(
            "ck_execution_reconciliation_runs_sources",
            "execution_reconciliation_runs",
            "jsonb_typeof(required_source_types) = 'array' "
            "AND jsonb_array_length(required_source_types) = 7",
        )
    op.drop_constraint(
        "ck_execution_reconciliation_inputs_source",
        "execution_reconciliation_inputs",
        type_="check",
    )
    sources = NEW_SOURCES if include_funding else OLD_SOURCES
    op.create_check_constraint(
        "ck_execution_reconciliation_inputs_source",
        "execution_reconciliation_inputs",
        f"source_type IN ({sources})",
    )


def _link_contract(*, include_funding: bool) -> str:
    funding_null = "AND venue_funding_payment_id IS NULL" if include_funding else ""
    funding_branch = (
        " OR (source_type = 'VENUE_FUNDING' AND venue_order_observation_id IS NULL "
        "AND venue_fill_id IS NULL AND venue_position_snapshot_id IS NULL "
        "AND venue_protection_snapshot_id IS NULL "
        "AND venue_account_equity_snapshot_id IS NULL "
        "AND venue_funding_payment_id IS NOT NULL)"
        if include_funding
        else ""
    )
    return (
        "(source_type = 'VENUE_ORDERS' AND venue_order_observation_id IS NOT NULL "
        "AND venue_fill_id IS NULL AND venue_position_snapshot_id IS NULL "
        "AND venue_protection_snapshot_id IS NULL "
        f"AND venue_account_equity_snapshot_id IS NULL {funding_null}) OR "
        "(source_type = 'VENUE_FILLS' AND venue_order_observation_id IS NULL "
        "AND venue_fill_id IS NOT NULL AND venue_position_snapshot_id IS NULL "
        "AND venue_protection_snapshot_id IS NULL "
        f"AND venue_account_equity_snapshot_id IS NULL {funding_null}) OR "
        "(source_type = 'VENUE_POSITIONS' AND venue_order_observation_id IS NULL "
        "AND venue_fill_id IS NULL AND venue_position_snapshot_id IS NOT NULL "
        "AND venue_protection_snapshot_id IS NULL "
        f"AND venue_account_equity_snapshot_id IS NULL {funding_null}) OR "
        "(source_type = 'VENUE_PROTECTION' AND venue_order_observation_id IS NULL "
        "AND venue_fill_id IS NULL AND venue_position_snapshot_id IS NULL "
        "AND venue_protection_snapshot_id IS NOT NULL "
        f"AND venue_account_equity_snapshot_id IS NULL {funding_null}) OR "
        "(source_type = 'VENUE_BALANCES' AND venue_order_observation_id IS NULL "
        "AND venue_fill_id IS NULL AND venue_position_snapshot_id IS NULL "
        "AND venue_protection_snapshot_id IS NULL "
        f"AND venue_account_equity_snapshot_id IS NOT NULL {funding_null})"
        f"{funding_branch}"
    )


def _replace_link_constraint(*, include_funding: bool, drop_existing: bool = True) -> None:
    if drop_existing:
        op.drop_constraint(
            "ck_venue_fact_input_links_exact_fact",
            "venue_fact_input_links",
            type_="check",
        )
    op.create_check_constraint(
        "ck_venue_fact_input_links_exact_fact",
        "venue_fact_input_links",
        _link_contract(include_funding=include_funding),
    )


def _replace_run_insert_guard(*, include_funding: bool) -> None:
    manifest = (
        "jsonb_build_array('TRADING_LEDGER', 'VENUE_ORDERS', 'VENUE_FILLS', "
        "'VENUE_FUNDING', 'VENUE_POSITIONS', 'VENUE_BALANCES', 'VENUE_PROTECTION', "
        "'WORKER_LOCAL')"
        if include_funding
        else "jsonb_build_array('TRADING_LEDGER', 'VENUE_ORDERS', 'VENUE_FILLS', "
        "'VENUE_POSITIONS', 'VENUE_BALANCES', 'VENUE_PROTECTION', 'WORKER_LOCAL')"
    )
    schema = "NEW.schema_version <> 2 OR " if include_funding else "NEW.schema_version <> 1 OR "
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION protect_execution_reconciliation_run_insert()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE sender_state execution_sender_scope_states%ROWTYPE;
        DECLARE lease_row execution_sender_leases%ROWTYPE;
        DECLARE latest_run_id uuid;
        BEGIN
            SELECT * INTO STRICT sender_state FROM execution_sender_scope_states
            WHERE scope_id = NEW.scope_id;
            SELECT * INTO STRICT lease_row FROM execution_sender_leases
            WHERE lease_id = NEW.lease_id;
            IF sender_state.status <> 'LEASED'
                OR sender_state.active_lease_id <> NEW.lease_id
                OR sender_state.current_fencing_token <> NEW.fencing_token
                OR sender_state.lease_expires_at IS NULL
                OR NEW.started_at >= sender_state.lease_expires_at
                OR lease_row.scope_id <> NEW.scope_id
                OR lease_row.organization_id <> NEW.organization_id
                OR lease_row.fencing_token <> NEW.fencing_token
                OR NEW.started_at < lease_row.issued_at
                OR lease_row.environment <> 'SHADOW'
                OR lease_row.live_dispatch_eligible THEN
                RAISE EXCEPTION 'reconciliation run requires the exact current shadow lease';
            END IF;
            IF {schema}NEW.required_source_types <> {manifest} THEN
                RAISE EXCEPTION 'reconciliation source manifest must equal the frozen set';
            END IF;
            SELECT run_id INTO latest_run_id FROM execution_reconciliation_runs
            WHERE scope_id = NEW.scope_id
            ORDER BY started_at DESC, run_id DESC LIMIT 1;
            IF latest_run_id IS NULL AND NEW.supersedes_run_id IS NOT NULL THEN
                RAISE EXCEPTION 'first reconciliation cannot supersede another run';
            ELSIF latest_run_id IS NOT NULL
                AND NEW.supersedes_run_id IS DISTINCT FROM latest_run_id THEN
                RAISE EXCEPTION 'reconciliation must supersede the latest scope run';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )


def _create_funding_insert_guard() -> None:
    op.execute(
        """
        CREATE FUNCTION protect_venue_funding_payment_insert()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE run_row execution_reconciliation_runs%ROWTYPE;
        DECLARE state_row execution_reconciliation_run_states%ROWTYPE;
        DECLARE input_row execution_reconciliation_inputs%ROWTYPE;
        DECLARE scope_row execution_sender_scopes%ROWTYPE;
        DECLARE sender_state execution_sender_scope_states%ROWTYPE;
        DECLARE latest_run_id uuid;
        BEGIN
            SELECT * INTO STRICT run_row FROM execution_reconciliation_runs
            WHERE run_id = NEW.first_seen_run_id;
            SELECT * INTO STRICT state_row FROM execution_reconciliation_run_states
            WHERE run_id = NEW.first_seen_run_id;
            SELECT * INTO STRICT input_row FROM execution_reconciliation_inputs
            WHERE input_id = NEW.first_seen_input_id;
            SELECT * INTO STRICT scope_row FROM execution_sender_scopes
            WHERE scope_id = run_row.scope_id;
            SELECT * INTO STRICT sender_state FROM execution_sender_scope_states
            WHERE scope_id = run_row.scope_id;
            SELECT run_id INTO STRICT latest_run_id FROM execution_reconciliation_runs
            WHERE scope_id = run_row.scope_id
            ORDER BY started_at DESC, run_id DESC LIMIT 1;

            IF NEW.organization_id <> run_row.organization_id
                OR input_row.run_id <> run_row.run_id
                OR input_row.organization_id <> run_row.organization_id
                OR input_row.source_type <> 'VENUE_FUNDING'
                OR input_row.collection_status <> 'COMPLETE'
                OR input_row.source_version <> NEW.source_version
                OR input_row.item_count <= 0
                OR NEW.event_time < input_row.observed_from
                OR NEW.event_time > input_row.observed_through
                OR input_row.received_at > NEW.recorded_at
                OR NEW.first_received_at > NEW.recorded_at THEN
                RAISE EXCEPTION 'canonical venue funding input binding is invalid';
            END IF;
            IF state_row.status <> 'RUNNING' OR state_row.phase <> 'COLLECTING'
                OR run_row.environment <> 'SHADOW' OR run_row.live_dispatch_eligible
                OR latest_run_id <> run_row.run_id
                OR NEW.recorded_at >= run_row.deadline_at THEN
                RAISE EXCEPTION 'canonical venue funding collection authority is closed';
            END IF;
            IF scope_row.venue <> NEW.venue
                OR scope_row.execution_domain <> NEW.execution_domain
                OR scope_row.account_id <> NEW.account_id
                OR scope_row.margin_mode <> NEW.margin_mode
                OR scope_row.collateral_pool_id <> NEW.collateral_pool_id THEN
                RAISE EXCEPTION 'canonical venue funding scope changed';
            END IF;
            IF sender_state.status <> 'LEASED'
                OR sender_state.active_lease_id <> run_row.lease_id
                OR sender_state.current_fencing_token <> run_row.fencing_token
                OR sender_state.lease_expires_at IS NULL
                OR NEW.recorded_at >= sender_state.lease_expires_at THEN
                RAISE EXCEPTION 'canonical venue funding sender lease is stale or fenced';
            END IF;
            RETURN NEW;
        EXCEPTION
            WHEN NO_DATA_FOUND THEN
                RAISE EXCEPTION 'canonical venue funding binding reference is unavailable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER venue_funding_payments_insert_guard
        BEFORE INSERT ON venue_funding_payments
        FOR EACH ROW EXECUTE FUNCTION protect_venue_funding_payment_insert()
        """
    )


def _replace_input_link_guard(*, include_funding: bool) -> None:
    funding_branch = (
        """
            ELSIF NEW.source_type = 'VENUE_FUNDING' THEN
                SELECT organization_id, funding_hash, venue, execution_domain,
                       account_id, event_time, first_seen_run_id, first_seen_input_id,
                       raw_payload_hash, evidence_hash, venue_observed_at, first_received_at
                INTO STRICT fact_org, canonical_fact_hash, fact_venue, fact_domain,
                            fact_account, fact_event_time, fact_first_run_id,
                            fact_first_input_id, fact_raw_payload_hash, fact_evidence_hash,
                            fact_venue_observed_at, fact_first_received_at
                FROM venue_funding_payments
                WHERE venue_funding_payment_id = NEW.venue_funding_payment_id;
        """
        if include_funding
        else ""
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION protect_venue_fact_input_link_insert()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE run_row execution_reconciliation_runs%ROWTYPE;
        DECLARE state_row execution_reconciliation_run_states%ROWTYPE;
        DECLARE input_row execution_reconciliation_inputs%ROWTYPE;
        DECLARE scope_row execution_sender_scopes%ROWTYPE;
        DECLARE sender_state execution_sender_scope_states%ROWTYPE;
        DECLARE fact_org text;
        DECLARE canonical_fact_hash text;
        DECLARE fact_venue text;
        DECLARE fact_domain text;
        DECLARE fact_account text;
        DECLARE fact_event_time timestamptz;
        DECLARE fact_first_run_id uuid;
        DECLARE fact_first_input_id uuid;
        DECLARE fact_raw_payload_hash text;
        DECLARE fact_evidence_hash text;
        DECLARE fact_venue_observed_at timestamptz;
        DECLARE fact_first_received_at timestamptz;
        DECLARE linked_count bigint;
        DECLARE latest_run_id uuid;
        BEGIN
            SELECT * INTO STRICT run_row FROM execution_reconciliation_runs
            WHERE run_id = NEW.run_id;
            SELECT * INTO STRICT state_row FROM execution_reconciliation_run_states
            WHERE run_id = NEW.run_id FOR UPDATE;
            SELECT * INTO STRICT input_row FROM execution_reconciliation_inputs
            WHERE input_id = NEW.reconciliation_input_id FOR UPDATE;
            SELECT * INTO STRICT scope_row FROM execution_sender_scopes
            WHERE scope_id = run_row.scope_id;
            SELECT * INTO STRICT sender_state FROM execution_sender_scope_states
            WHERE scope_id = run_row.scope_id;
            SELECT run_id INTO STRICT latest_run_id FROM execution_reconciliation_runs
            WHERE scope_id = run_row.scope_id
            ORDER BY started_at DESC, run_id DESC LIMIT 1;

            IF NEW.source_type = 'VENUE_ORDERS' THEN
                SELECT organization_id, observation_hash, venue, execution_domain,
                       account_id, event_time, first_seen_run_id, first_seen_input_id,
                       raw_payload_hash, evidence_hash, venue_observed_at, first_received_at
                INTO STRICT fact_org, canonical_fact_hash, fact_venue, fact_domain,
                            fact_account, fact_event_time, fact_first_run_id,
                            fact_first_input_id, fact_raw_payload_hash, fact_evidence_hash,
                            fact_venue_observed_at, fact_first_received_at
                FROM venue_order_observations
                WHERE venue_order_observation_id = NEW.venue_order_observation_id;
            ELSIF NEW.source_type = 'VENUE_FILLS' THEN
                SELECT organization_id, fill_hash, venue, execution_domain,
                       account_id, event_time, first_seen_run_id, first_seen_input_id,
                       raw_payload_hash, evidence_hash, venue_observed_at, first_received_at
                INTO STRICT fact_org, canonical_fact_hash, fact_venue, fact_domain,
                            fact_account, fact_event_time, fact_first_run_id,
                            fact_first_input_id, fact_raw_payload_hash, fact_evidence_hash,
                            fact_venue_observed_at, fact_first_received_at
                FROM venue_fills WHERE venue_fill_id = NEW.venue_fill_id;
            {funding_branch}
            ELSIF NEW.source_type = 'VENUE_POSITIONS' THEN
                SELECT organization_id, snapshot_hash, venue, execution_domain,
                       account_id, event_time, first_seen_run_id, first_seen_input_id,
                       raw_payload_hash, evidence_hash, venue_observed_at, first_received_at
                INTO STRICT fact_org, canonical_fact_hash, fact_venue, fact_domain,
                            fact_account, fact_event_time, fact_first_run_id,
                            fact_first_input_id, fact_raw_payload_hash, fact_evidence_hash,
                            fact_venue_observed_at, fact_first_received_at
                FROM venue_position_snapshots
                WHERE venue_position_snapshot_id = NEW.venue_position_snapshot_id;
            ELSIF NEW.source_type = 'VENUE_PROTECTION' THEN
                SELECT organization_id, snapshot_hash, venue, execution_domain,
                       account_id, event_time, first_seen_run_id, first_seen_input_id,
                       raw_payload_hash, evidence_hash, venue_observed_at, first_received_at
                INTO STRICT fact_org, canonical_fact_hash, fact_venue, fact_domain,
                            fact_account, fact_event_time, fact_first_run_id,
                            fact_first_input_id, fact_raw_payload_hash, fact_evidence_hash,
                            fact_venue_observed_at, fact_first_received_at
                FROM venue_protection_snapshots
                WHERE venue_protection_snapshot_id = NEW.venue_protection_snapshot_id;
            ELSIF NEW.source_type = 'VENUE_BALANCES' THEN
                SELECT organization_id, snapshot_hash, venue, execution_domain,
                       account_id, event_time, first_seen_run_id, first_seen_input_id,
                       raw_payload_hash, evidence_hash, venue_observed_at, first_received_at
                INTO STRICT fact_org, canonical_fact_hash, fact_venue, fact_domain,
                            fact_account, fact_event_time, fact_first_run_id,
                            fact_first_input_id, fact_raw_payload_hash, fact_evidence_hash,
                            fact_venue_observed_at, fact_first_received_at
                FROM venue_account_equity_snapshots
                WHERE venue_account_equity_snapshot_id
                    = NEW.venue_account_equity_snapshot_id;
            ELSE
                RAISE EXCEPTION 'venue fact input membership source is unsupported';
            END IF;
            SELECT count(*) INTO linked_count FROM venue_fact_input_links
            WHERE reconciliation_input_id = NEW.reconciliation_input_id;

            IF NEW.organization_id <> run_row.organization_id
                OR input_row.run_id <> run_row.run_id
                OR input_row.organization_id <> run_row.organization_id
                OR input_row.source_type <> NEW.source_type
                OR input_row.collection_status <> 'COMPLETE'
                OR input_row.input_hash <> NEW.input_hash
                OR linked_count >= input_row.item_count
                OR fact_org <> run_row.organization_id
                OR canonical_fact_hash <> NEW.fact_hash
                OR fact_event_time < input_row.observed_from
                OR fact_event_time > input_row.observed_through
                OR input_row.received_at > NEW.linked_at
                OR NEW.received_at > NEW.linked_at
                OR (
                    fact_first_run_id = NEW.run_id
                    AND fact_first_input_id = NEW.reconciliation_input_id
                    AND (
                        fact_raw_payload_hash <> NEW.raw_payload_hash
                        OR fact_evidence_hash <> NEW.evidence_hash
                        OR fact_venue_observed_at <> NEW.observed_at
                        OR fact_first_received_at <> NEW.received_at
                    )
                ) THEN
                RAISE EXCEPTION 'venue fact input membership is invalid';
            END IF;
            IF state_row.status <> 'RUNNING' OR state_row.phase <> 'COLLECTING'
                OR run_row.environment <> 'SHADOW' OR run_row.live_dispatch_eligible
                OR latest_run_id <> run_row.run_id
                OR NEW.linked_at >= run_row.deadline_at THEN
                RAISE EXCEPTION 'venue fact input membership authority is closed';
            END IF;
            IF scope_row.venue <> fact_venue
                OR scope_row.execution_domain <> fact_domain
                OR scope_row.account_id <> fact_account THEN
                RAISE EXCEPTION 'venue fact input membership route changed';
            END IF;
            IF sender_state.status <> 'LEASED'
                OR sender_state.active_lease_id <> run_row.lease_id
                OR sender_state.current_fencing_token <> run_row.fencing_token
                OR sender_state.lease_expires_at IS NULL
                OR NEW.linked_at >= sender_state.lease_expires_at THEN
                RAISE EXCEPTION 'venue fact input membership lease is stale or fenced';
            END IF;
            RETURN NEW;
        EXCEPTION
            WHEN NO_DATA_FOUND THEN
                RAISE EXCEPTION 'venue fact input membership reference is unavailable';
        END;
        $$
        """
    )


def _replace_first_link_guard(*, include_funding: bool) -> None:
    funding_branch = (
        """
            ELSIF TG_TABLE_NAME = 'venue_funding_payments' THEN
                SELECT EXISTS(
                    SELECT 1 FROM venue_fact_input_links
                    WHERE run_id = NEW.first_seen_run_id
                      AND reconciliation_input_id = NEW.first_seen_input_id
                      AND venue_funding_payment_id = NEW.venue_funding_payment_id
                      AND fact_hash = NEW.funding_hash
                ) INTO linked;
        """
        if include_funding
        else ""
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION verify_canonical_venue_fact_first_link()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE linked boolean;
        BEGIN
            IF TG_TABLE_NAME = 'venue_order_observations' THEN
                SELECT EXISTS(
                    SELECT 1 FROM venue_fact_input_links
                    WHERE run_id = NEW.first_seen_run_id
                      AND reconciliation_input_id = NEW.first_seen_input_id
                      AND venue_order_observation_id = NEW.venue_order_observation_id
                      AND fact_hash = NEW.observation_hash
                ) INTO linked;
            ELSIF TG_TABLE_NAME = 'venue_fills' THEN
                SELECT EXISTS(
                    SELECT 1 FROM venue_fact_input_links
                    WHERE run_id = NEW.first_seen_run_id
                      AND reconciliation_input_id = NEW.first_seen_input_id
                      AND venue_fill_id = NEW.venue_fill_id
                      AND fact_hash = NEW.fill_hash
                ) INTO linked;
            {funding_branch}
            ELSIF TG_TABLE_NAME = 'venue_position_snapshots' THEN
                SELECT EXISTS(
                    SELECT 1 FROM venue_fact_input_links
                    WHERE run_id = NEW.first_seen_run_id
                      AND reconciliation_input_id = NEW.first_seen_input_id
                      AND venue_position_snapshot_id = NEW.venue_position_snapshot_id
                      AND fact_hash = NEW.snapshot_hash
                ) INTO linked;
            ELSIF TG_TABLE_NAME = 'venue_protection_snapshots' THEN
                SELECT EXISTS(
                    SELECT 1 FROM venue_fact_input_links
                    WHERE run_id = NEW.first_seen_run_id
                      AND reconciliation_input_id = NEW.first_seen_input_id
                      AND venue_protection_snapshot_id = NEW.venue_protection_snapshot_id
                      AND fact_hash = NEW.snapshot_hash
                ) INTO linked;
            ELSIF TG_TABLE_NAME = 'venue_account_equity_snapshots' THEN
                SELECT EXISTS(
                    SELECT 1 FROM venue_fact_input_links
                    WHERE run_id = NEW.first_seen_run_id
                      AND reconciliation_input_id = NEW.first_seen_input_id
                      AND venue_account_equity_snapshot_id
                          = NEW.venue_account_equity_snapshot_id
                      AND fact_hash = NEW.snapshot_hash
                ) INTO linked;
            ELSE
                linked := false;
            END IF;
            IF NOT linked THEN
                RAISE EXCEPTION 'canonical venue fact requires its first immutable input link';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )


def _replace_manifest_guard(*, include_funding: bool) -> None:
    normalized = (
        "'VENUE_ORDERS', 'VENUE_FILLS', 'VENUE_FUNDING', 'VENUE_POSITIONS', "
        "'VENUE_BALANCES', 'VENUE_PROTECTION'"
        if include_funding
        else "'VENUE_ORDERS', 'VENUE_FILLS', 'VENUE_POSITIONS', "
        "'VENUE_BALANCES', 'VENUE_PROTECTION'"
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION verify_normalized_venue_fact_manifest()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.phase IN ('COMPARING', 'ADJUSTING') OR NEW.status = 'SUCCEEDED' THEN
                IF EXISTS (
                    SELECT 1
                    FROM execution_reconciliation_inputs input_row
                    WHERE input_row.run_id = NEW.run_id
                      AND input_row.source_type IN ({normalized})
                      AND (
                        input_row.collection_status <> 'COMPLETE'
                        OR input_row.item_count <> (
                            SELECT count(*) FROM venue_fact_input_links link_row
                            WHERE link_row.reconciliation_input_id = input_row.input_id
                              AND link_row.source_type = input_row.source_type
                        )
                      )
                ) THEN
                    RAISE EXCEPTION 'advanced reconciliation venue fact count mismatch';
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
