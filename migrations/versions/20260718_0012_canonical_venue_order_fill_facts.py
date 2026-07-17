"""Add canonical private-venue order and fill facts.

Revision ID: 20260718_0012
Revises: 20260718_0011
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0012"
down_revision: str | Sequence[str] | None = "20260718_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "venue_order_observations",
        sa.Column("venue_order_observation_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("first_seen_run_id", sa.Uuid(), nullable=False),
        sa.Column("first_seen_input_id", sa.Uuid(), nullable=False),
        sa.Column("venue", sa.String(length=80), nullable=False),
        sa.Column("execution_domain", sa.String(length=120), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=False),
        sa.Column("instrument_id", sa.String(length=255), nullable=False),
        sa.Column("observed_client_order_id", sa.String(length=160), nullable=True),
        sa.Column("venue_order_id", sa.String(length=255), nullable=False),
        sa.Column("venue_update_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("position_side", sa.String(length=20), nullable=False),
        sa.Column("reduce_only", sa.Boolean(), nullable=False),
        sa.Column("order_type", sa.String(length=40), nullable=False),
        sa.Column("time_in_force", sa.String(length=40), nullable=False),
        sa.Column("original_quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("cumulative_filled_quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("known_remaining_quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("zero_fill_confirmed", sa.Boolean(), nullable=False),
        sa.Column("terminal", sa.Boolean(), nullable=False),
        sa.Column("fact_authority", sa.String(length=32), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("live_dispatch_eligible", sa.Boolean(), nullable=False),
        sa.Column("source_version", sa.String(length=160), nullable=False),
        sa.Column("normalization_version", sa.String(length=160), nullable=False),
        sa.Column("normalized_payload", postgresql.JSONB(), nullable=False),
        sa.Column("raw_payload_ref", sa.String(length=255), nullable=False),
        sa.Column("raw_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence_ref", sa.String(length=255), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("observation_hash", sa.String(length=64), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("venue_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('OPEN', 'PARTIALLY_FILLED', 'FILLED', 'CANCEL_PENDING', "
            "'CANCELLED', 'REJECTED', 'EXPIRED', 'UNKNOWN')",
            name="ck_venue_order_observations_status",
        ),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name="ck_venue_order_observations_side"),
        sa.CheckConstraint(
            "position_side IN ('LONG', 'SHORT', 'BOTH')",
            name="ck_venue_order_observations_position_side",
        ),
        sa.CheckConstraint(
            "original_quantity > 0 AND cumulative_filled_quantity >= 0 "
            "AND known_remaining_quantity >= 0 "
            "AND cumulative_filled_quantity + known_remaining_quantity <= original_quantity",
            name="ck_venue_order_observations_quantities",
        ),
        sa.CheckConstraint(
            "(status = 'OPEN' AND cumulative_filled_quantity = 0 "
            "AND known_remaining_quantity = original_quantity AND NOT terminal) OR "
            "(status = 'PARTIALLY_FILLED' AND cumulative_filled_quantity > 0 "
            "AND known_remaining_quantity > 0 "
            "AND cumulative_filled_quantity + known_remaining_quantity = original_quantity "
            "AND NOT terminal) OR "
            "(status = 'FILLED' AND cumulative_filled_quantity = original_quantity "
            "AND known_remaining_quantity = 0 AND terminal) OR "
            "(status = 'CANCEL_PENDING' "
            "AND cumulative_filled_quantity + known_remaining_quantity = original_quantity "
            "AND NOT terminal) OR "
            "(status IN ('CANCELLED', 'EXPIRED') AND known_remaining_quantity = 0 "
            "AND terminal AND zero_fill_confirmed = (cumulative_filled_quantity = 0)) OR "
            "(status = 'REJECTED' AND cumulative_filled_quantity = 0 "
            "AND known_remaining_quantity = 0 AND terminal AND zero_fill_confirmed) OR "
            "(status = 'UNKNOWN' AND NOT terminal AND NOT zero_fill_confirmed)",
            name="ck_venue_order_observations_status_semantics",
        ),
        sa.CheckConstraint(
            "event_time <= venue_observed_at AND venue_observed_at <= first_received_at "
            "AND first_received_at <= recorded_at",
            name="ck_venue_order_observations_time_order",
        ),
        sa.CheckConstraint(
            "fact_authority = 'VENUE_PRIVATE' AND environment = 'SHADOW' "
            "AND live_dispatch_eligible = false",
            name="ck_venue_order_observations_authority",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(normalized_payload) = 'object' "
            "AND length(raw_payload_hash) = 64 AND length(evidence_hash) = 64 "
            "AND length(observation_hash) = 64",
            name="ck_venue_order_observations_integrity",
        ),
        sa.ForeignKeyConstraint(
            ["first_seen_input_id"],
            ["execution_reconciliation_inputs.input_id"],
            name="fk_venue_order_observations_first_input",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["first_seen_run_id", "organization_id"],
            [
                "execution_reconciliation_runs.run_id",
                "execution_reconciliation_runs.organization_id",
            ],
            name="fk_venue_order_observations_first_run_org",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("venue_order_observation_id"),
        sa.UniqueConstraint(
            "organization_id",
            "venue",
            "execution_domain",
            "account_id",
            "venue_order_id",
            "venue_update_id",
            name="uq_venue_order_observations_external_update",
        ),
    )
    op.create_index(
        "ix_venue_order_observations_client_identity",
        "venue_order_observations",
        ["venue", "execution_domain", "account_id", "observed_client_order_id"],
    )
    op.create_index(
        "ix_venue_order_observations_order_time",
        "venue_order_observations",
        ["venue", "execution_domain", "account_id", "venue_order_id", "event_time"],
    )

    op.create_table(
        "venue_fills",
        sa.Column("venue_fill_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("first_seen_run_id", sa.Uuid(), nullable=False),
        sa.Column("first_seen_input_id", sa.Uuid(), nullable=False),
        sa.Column("venue", sa.String(length=80), nullable=False),
        sa.Column("execution_domain", sa.String(length=120), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=False),
        sa.Column("instrument_id", sa.String(length=255), nullable=False),
        sa.Column("observed_client_order_id", sa.String(length=160), nullable=True),
        sa.Column("venue_order_id", sa.String(length=255), nullable=False),
        sa.Column("venue_trade_id", sa.String(length=255), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("position_side", sa.String(length=20), nullable=False),
        sa.Column("reduce_only", sa.Boolean(), nullable=False),
        sa.Column("quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("price", sa.Numeric(38, 18), nullable=False),
        sa.Column("contract_multiplier", sa.Numeric(38, 18), nullable=False),
        sa.Column("notional", sa.Numeric(38, 18), nullable=False),
        sa.Column("liquidity_role", sa.String(length=20), nullable=False),
        sa.Column("fee_amount", sa.Numeric(38, 18), nullable=False),
        sa.Column("fee_currency", sa.String(length=80), nullable=False),
        sa.Column("fee_effect", sa.String(length=20), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(38, 18), nullable=True),
        sa.Column("settlement_currency", sa.String(length=80), nullable=False),
        sa.Column("venue_confirmed", sa.Boolean(), nullable=False),
        sa.Column("fact_authority", sa.String(length=32), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("live_dispatch_eligible", sa.Boolean(), nullable=False),
        sa.Column("source_version", sa.String(length=160), nullable=False),
        sa.Column("normalization_version", sa.String(length=160), nullable=False),
        sa.Column("normalized_payload", postgresql.JSONB(), nullable=False),
        sa.Column("raw_payload_ref", sa.String(length=255), nullable=False),
        sa.Column("raw_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence_ref", sa.String(length=255), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("fill_hash", sa.String(length=64), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("venue_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name="ck_venue_fills_side"),
        sa.CheckConstraint(
            "position_side IN ('LONG', 'SHORT', 'BOTH')",
            name="ck_venue_fills_position_side",
        ),
        sa.CheckConstraint(
            "quantity > 0 AND price > 0 AND contract_multiplier > 0 "
            "AND notional = quantity * price * contract_multiplier",
            name="ck_venue_fills_economics",
        ),
        sa.CheckConstraint(
            "liquidity_role IN ('MAKER', 'TAKER', 'UNKNOWN')",
            name="ck_venue_fills_liquidity",
        ),
        sa.CheckConstraint(
            "(fee_effect = 'CHARGE' AND fee_amount > 0) OR "
            "(fee_effect = 'REBATE' AND fee_amount < 0) OR "
            "(fee_effect = 'ZERO' AND fee_amount = 0)",
            name="ck_venue_fills_fee_effect",
        ),
        sa.CheckConstraint(
            "event_time <= venue_observed_at AND venue_observed_at <= first_received_at "
            "AND first_received_at <= recorded_at",
            name="ck_venue_fills_time_order",
        ),
        sa.CheckConstraint(
            "venue_confirmed AND fact_authority = 'VENUE_PRIVATE' "
            "AND environment = 'SHADOW' AND live_dispatch_eligible = false",
            name="ck_venue_fills_authority",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(normalized_payload) = 'object' "
            "AND length(raw_payload_hash) = 64 AND length(evidence_hash) = 64 "
            "AND length(fill_hash) = 64",
            name="ck_venue_fills_integrity",
        ),
        sa.ForeignKeyConstraint(
            ["first_seen_input_id"],
            ["execution_reconciliation_inputs.input_id"],
            name="fk_venue_fills_first_input",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["first_seen_run_id", "organization_id"],
            [
                "execution_reconciliation_runs.run_id",
                "execution_reconciliation_runs.organization_id",
            ],
            name="fk_venue_fills_first_run_org",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("venue_fill_id"),
        sa.UniqueConstraint(
            "organization_id",
            "venue",
            "execution_domain",
            "account_id",
            "venue_trade_id",
            name="uq_venue_fills_external_trade",
        ),
    )
    op.create_index(
        "ix_venue_fills_instrument_time",
        "venue_fills",
        ["venue", "execution_domain", "account_id", "instrument_id", "event_time"],
    )
    op.create_index(
        "ix_venue_fills_order_time",
        "venue_fills",
        ["venue", "execution_domain", "account_id", "venue_order_id", "event_time"],
    )

    op.create_table(
        "venue_fact_input_links",
        sa.Column("venue_fact_input_link_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("reconciliation_input_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("source_type", sa.String(length=40), nullable=False),
        sa.Column("venue_order_observation_id", sa.Uuid(), nullable=True),
        sa.Column("venue_fill_id", sa.Uuid(), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("fact_hash", sa.String(length=64), nullable=False),
        sa.Column("raw_payload_ref", sa.String(length=255), nullable=False),
        sa.Column("raw_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence_ref", sa.String(length=255), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("link_hash", sa.String(length=64), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(source_type = 'VENUE_ORDERS' AND venue_order_observation_id IS NOT NULL "
            "AND venue_fill_id IS NULL) OR "
            "(source_type = 'VENUE_FILLS' AND venue_order_observation_id IS NULL "
            "AND venue_fill_id IS NOT NULL)",
            name="ck_venue_fact_input_links_exact_fact",
        ),
        sa.CheckConstraint(
            "observed_at <= received_at AND received_at <= linked_at",
            name="ck_venue_fact_input_links_time_order",
        ),
        sa.CheckConstraint(
            "length(input_hash) = 64 AND length(fact_hash) = 64 "
            "AND length(raw_payload_hash) = 64 AND length(evidence_hash) = 64 "
            "AND length(link_hash) = 64",
            name="ck_venue_fact_input_links_integrity",
        ),
        sa.ForeignKeyConstraint(
            ["reconciliation_input_id"],
            ["execution_reconciliation_inputs.input_id"],
            name="fk_venue_fact_input_links_input",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "organization_id"],
            [
                "execution_reconciliation_runs.run_id",
                "execution_reconciliation_runs.organization_id",
            ],
            name="fk_venue_fact_input_links_run_org",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["venue_fill_id"],
            ["venue_fills.venue_fill_id"],
            name="fk_venue_fact_input_links_fill",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["venue_order_observation_id"],
            ["venue_order_observations.venue_order_observation_id"],
            name="fk_venue_fact_input_links_order_observation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("venue_fact_input_link_id"),
        sa.UniqueConstraint(
            "reconciliation_input_id",
            "venue_fill_id",
            name="uq_venue_fact_input_links_fill_fact",
        ),
        sa.UniqueConstraint(
            "reconciliation_input_id",
            "venue_order_observation_id",
            name="uq_venue_fact_input_links_order_fact",
        ),
    )
    op.create_index(
        "ix_venue_fact_input_links_run_source",
        "venue_fact_input_links",
        ["run_id", "source_type"],
    )
    _create_venue_fact_guards()


def downgrade() -> None:
    _drop_venue_fact_guards()
    op.drop_index("ix_venue_fact_input_links_run_source", table_name="venue_fact_input_links")
    op.drop_table("venue_fact_input_links")
    op.drop_index("ix_venue_fills_order_time", table_name="venue_fills")
    op.drop_index("ix_venue_fills_instrument_time", table_name="venue_fills")
    op.drop_table("venue_fills")
    op.drop_index("ix_venue_order_observations_order_time", table_name="venue_order_observations")
    op.drop_index(
        "ix_venue_order_observations_client_identity",
        table_name="venue_order_observations",
    )
    op.drop_table("venue_order_observations")


def _create_venue_fact_guards() -> None:
    op.execute(
        """
        CREATE FUNCTION deny_venue_fact_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is immutable', TG_TABLE_NAME;
        END;
        $$
        """
    )
    for table in ("venue_order_observations", "venue_fills", "venue_fact_input_links"):
        op.execute(
            f"""
            CREATE TRIGGER {table}_immutable
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION deny_venue_fact_change()
            """
        )
    op.execute(
        """
        CREATE FUNCTION protect_canonical_venue_fact_insert()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE run_row execution_reconciliation_runs%ROWTYPE;
        DECLARE state_row execution_reconciliation_run_states%ROWTYPE;
        DECLARE input_row execution_reconciliation_inputs%ROWTYPE;
        DECLARE scope_row execution_sender_scopes%ROWTYPE;
        DECLARE sender_state execution_sender_scope_states%ROWTYPE;
        DECLARE expected_source text;
        DECLARE latest_run_id uuid;
        BEGIN
            expected_source := CASE TG_TABLE_NAME
                WHEN 'venue_order_observations' THEN 'VENUE_ORDERS'
                ELSE 'VENUE_FILLS'
            END;
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
                OR input_row.source_type <> expected_source
                OR input_row.collection_status <> 'COMPLETE'
                OR input_row.source_version <> NEW.source_version
                OR input_row.item_count <= 0
                OR NEW.event_time < input_row.observed_from
                OR NEW.event_time > input_row.observed_through
                OR input_row.received_at > NEW.recorded_at
                OR NEW.first_received_at > NEW.recorded_at THEN
                RAISE EXCEPTION 'canonical venue fact input binding is invalid';
            END IF;
            IF state_row.status <> 'RUNNING' OR state_row.phase <> 'COLLECTING'
                OR run_row.environment <> 'SHADOW' OR run_row.live_dispatch_eligible
                OR latest_run_id <> run_row.run_id
                OR NEW.recorded_at >= run_row.deadline_at THEN
                RAISE EXCEPTION 'canonical venue fact collection authority is closed';
            END IF;
            IF scope_row.venue <> NEW.venue
                OR scope_row.execution_domain <> NEW.execution_domain
                OR scope_row.account_id <> NEW.account_id THEN
                RAISE EXCEPTION 'canonical venue fact route changed';
            END IF;
            IF sender_state.status <> 'LEASED'
                OR sender_state.active_lease_id <> run_row.lease_id
                OR sender_state.current_fencing_token <> run_row.fencing_token
                OR sender_state.lease_expires_at IS NULL
                OR NEW.recorded_at >= sender_state.lease_expires_at THEN
                RAISE EXCEPTION 'canonical venue fact sender lease is stale or fenced';
            END IF;
            RETURN NEW;
        EXCEPTION
            WHEN NO_DATA_FOUND THEN
                RAISE EXCEPTION 'canonical venue fact binding reference is unavailable';
        END;
        $$
        """
    )
    for table in ("venue_order_observations", "venue_fills"):
        op.execute(
            f"""
            CREATE TRIGGER {table}_insert_guard
            BEFORE INSERT ON {table}
            FOR EACH ROW EXECUTE FUNCTION protect_canonical_venue_fact_insert()
            """
        )
    op.execute(
        """
        CREATE FUNCTION protect_venue_fact_input_link_insert()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE run_row execution_reconciliation_runs%ROWTYPE;
        DECLARE state_row execution_reconciliation_run_states%ROWTYPE;
        DECLARE input_row execution_reconciliation_inputs%ROWTYPE;
        DECLARE scope_row execution_sender_scopes%ROWTYPE;
        DECLARE sender_state execution_sender_scope_states%ROWTYPE;
        DECLARE fact_org text;
        DECLARE fact_hash text;
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
                INTO STRICT fact_org, fact_hash, fact_venue, fact_domain,
                            fact_account, fact_event_time, fact_first_run_id,
                            fact_first_input_id, fact_raw_payload_hash, fact_evidence_hash,
                            fact_venue_observed_at, fact_first_received_at
                FROM venue_order_observations
                WHERE venue_order_observation_id = NEW.venue_order_observation_id;
            ELSE
                SELECT organization_id, fill_hash, venue, execution_domain,
                       account_id, event_time, first_seen_run_id, first_seen_input_id,
                       raw_payload_hash, evidence_hash, venue_observed_at, first_received_at
                INTO STRICT fact_org, fact_hash, fact_venue, fact_domain,
                            fact_account, fact_event_time, fact_first_run_id,
                            fact_first_input_id, fact_raw_payload_hash, fact_evidence_hash,
                            fact_venue_observed_at, fact_first_received_at
                FROM venue_fills WHERE venue_fill_id = NEW.venue_fill_id;
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
                OR fact_hash <> NEW.fact_hash
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
    op.execute(
        """
        CREATE TRIGGER venue_fact_input_links_insert_guard
        BEFORE INSERT ON venue_fact_input_links
        FOR EACH ROW EXECUTE FUNCTION protect_venue_fact_input_link_insert()
        """
    )
    op.execute(
        """
        CREATE FUNCTION verify_canonical_venue_fact_first_link()
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
            ELSE
                SELECT EXISTS(
                    SELECT 1 FROM venue_fact_input_links
                    WHERE run_id = NEW.first_seen_run_id
                      AND reconciliation_input_id = NEW.first_seen_input_id
                      AND venue_fill_id = NEW.venue_fill_id
                      AND fact_hash = NEW.fill_hash
                ) INTO linked;
            END IF;
            IF NOT linked THEN
                RAISE EXCEPTION 'canonical venue fact requires its first immutable input link';
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    for table in ("venue_order_observations", "venue_fills"):
        op.execute(
            f"""
            CREATE CONSTRAINT TRIGGER {table}_first_link_guard
            AFTER INSERT ON {table}
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION verify_canonical_venue_fact_first_link()
            """
        )
    op.execute(
        """
        CREATE FUNCTION verify_normalized_venue_fact_manifest()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.phase IN ('COMPARING', 'ADJUSTING') OR NEW.status = 'SUCCEEDED' THEN
                IF EXISTS (
                    SELECT 1
                    FROM execution_reconciliation_inputs input_row
                    WHERE input_row.run_id = NEW.run_id
                      AND input_row.source_type IN ('VENUE_ORDERS', 'VENUE_FILLS')
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
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER execution_reconciliation_states_venue_fact_guard
        AFTER INSERT OR UPDATE ON execution_reconciliation_run_states
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION verify_normalized_venue_fact_manifest()
        """
    )


def _drop_venue_fact_guards() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS execution_reconciliation_states_venue_fact_guard "
        "ON execution_reconciliation_run_states"
    )
    op.execute("DROP FUNCTION IF EXISTS verify_normalized_venue_fact_manifest()")
    for table in ("venue_fills", "venue_order_observations"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_first_link_guard ON {table}")
    op.execute("DROP FUNCTION IF EXISTS verify_canonical_venue_fact_first_link()")
    op.execute(
        "DROP TRIGGER IF EXISTS venue_fact_input_links_insert_guard ON venue_fact_input_links"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_venue_fact_input_link_insert()")
    for table in ("venue_fills", "venue_order_observations"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_insert_guard ON {table}")
    op.execute("DROP FUNCTION IF EXISTS protect_canonical_venue_fact_insert()")
    for table in ("venue_fact_input_links", "venue_fills", "venue_order_observations"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS deny_venue_fact_change()")
