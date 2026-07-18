"""Add canonical private-venue position snapshots.

Revision ID: 20260718_0014
Revises: 20260718_0013
Create Date: 2026-07-18
"""

# ruff: noqa: S608 - SQL fragments are fixed migration constants.

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0014"
down_revision: str | Sequence[str] | None = "20260718_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "venue_position_snapshots",
        sa.Column("venue_position_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("first_seen_run_id", sa.Uuid(), nullable=False),
        sa.Column("first_seen_input_id", sa.Uuid(), nullable=False),
        sa.Column("venue", sa.String(length=80), nullable=False),
        sa.Column("execution_domain", sa.String(length=120), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=False),
        sa.Column("instrument_id", sa.String(length=255), nullable=False),
        sa.Column("venue_update_id", sa.String(length=255), nullable=False),
        sa.Column("position_mode", sa.String(length=80), nullable=False),
        sa.Column("position_side", sa.String(length=20), nullable=False),
        sa.Column("margin_mode", sa.String(length=80), nullable=False),
        sa.Column("collateral_pool_id", sa.String(length=160), nullable=False),
        sa.Column("position_state", sa.String(length=20), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("quantity", sa.Numeric(38, 18), nullable=True),
        sa.Column("entry_price", sa.Numeric(38, 18), nullable=True),
        sa.Column("mark_price", sa.Numeric(38, 18), nullable=True),
        sa.Column("contract_multiplier", sa.Numeric(38, 18), nullable=False),
        sa.Column("notional", sa.Numeric(38, 18), nullable=True),
        sa.Column("unrealized_pnl", sa.Numeric(38, 18), nullable=True),
        sa.Column("liquidation_price", sa.Numeric(38, 18), nullable=True),
        sa.Column("leverage", sa.Numeric(38, 18), nullable=True),
        sa.Column("initial_margin", sa.Numeric(38, 18), nullable=True),
        sa.Column("maintenance_margin", sa.Numeric(38, 18), nullable=True),
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
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("venue_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "position_state IN ('OPEN', 'FLAT', 'UNKNOWN')",
            name="ck_venue_position_snapshots_state",
        ),
        sa.CheckConstraint(
            "position_mode IN ('ONE_WAY', 'HEDGE') "
            "AND position_side IN ('LONG', 'SHORT', 'BOTH') "
            "AND direction IN ('LONG', 'SHORT', 'FLAT', 'UNKNOWN')",
            name="ck_venue_position_snapshots_shape",
        ),
        sa.CheckConstraint(
            "(position_mode = 'ONE_WAY' AND position_side = 'BOTH') OR "
            "(position_mode = 'HEDGE' AND position_side IN ('LONG', 'SHORT'))",
            name="ck_venue_position_snapshots_mode_side",
        ),
        sa.CheckConstraint(
            "contract_multiplier > 0 AND ("
            "(position_state = 'OPEN' AND direction IN ('LONG', 'SHORT') "
            "AND quantity > 0 AND entry_price > 0 AND mark_price > 0 "
            "AND notional = quantity * mark_price * contract_multiplier "
            "AND unrealized_pnl IS NOT NULL "
            "AND (liquidation_price IS NULL OR liquidation_price > 0) "
            "AND (leverage IS NULL OR leverage > 0) "
            "AND (initial_margin IS NULL OR initial_margin >= 0) "
            "AND (maintenance_margin IS NULL OR maintenance_margin >= 0)) OR "
            "(position_state = 'FLAT' AND direction = 'FLAT' AND quantity = 0 "
            "AND entry_price IS NULL AND (mark_price IS NULL OR mark_price > 0) "
            "AND notional = 0 AND unrealized_pnl = 0 AND liquidation_price IS NULL "
            "AND leverage IS NULL AND (initial_margin IS NULL OR initial_margin = 0) "
            "AND (maintenance_margin IS NULL OR maintenance_margin = 0)) OR "
            "(position_state = 'UNKNOWN' AND direction = 'UNKNOWN' "
            "AND quantity IS NULL AND entry_price IS NULL AND mark_price IS NULL "
            "AND notional IS NULL AND unrealized_pnl IS NULL "
            "AND liquidation_price IS NULL AND leverage IS NULL "
            "AND initial_margin IS NULL AND maintenance_margin IS NULL))",
            name="ck_venue_position_snapshots_economics",
        ),
        sa.CheckConstraint(
            "(position_mode = 'ONE_WAY') OR position_state <> 'OPEN' OR direction = position_side",
            name="ck_venue_position_snapshots_hedge_direction",
        ),
        sa.CheckConstraint(
            "event_time <= venue_observed_at AND venue_observed_at <= first_received_at "
            "AND first_received_at <= recorded_at",
            name="ck_venue_position_snapshots_time_order",
        ),
        sa.CheckConstraint(
            "venue_confirmed AND fact_authority = 'VENUE_PRIVATE' "
            "AND environment = 'SHADOW' AND live_dispatch_eligible = false",
            name="ck_venue_position_snapshots_authority",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(normalized_payload) = 'object' "
            "AND length(raw_payload_hash) = 64 AND length(evidence_hash) = 64 "
            "AND length(snapshot_hash) = 64",
            name="ck_venue_position_snapshots_integrity",
        ),
        sa.ForeignKeyConstraint(
            ["first_seen_input_id"],
            ["execution_reconciliation_inputs.input_id"],
            name="fk_venue_position_snapshots_first_input",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["first_seen_run_id", "organization_id"],
            [
                "execution_reconciliation_runs.run_id",
                "execution_reconciliation_runs.organization_id",
            ],
            name="fk_venue_position_snapshots_first_run_org",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("venue_position_snapshot_id"),
        sa.UniqueConstraint(
            "organization_id",
            "venue",
            "execution_domain",
            "account_id",
            "instrument_id",
            "position_mode",
            "position_side",
            "margin_mode",
            "collateral_pool_id",
            "venue_update_id",
            name="uq_venue_position_snapshots_external_update",
        ),
    )
    op.create_index(
        "ix_venue_position_snapshots_scope_time",
        "venue_position_snapshots",
        [
            "venue",
            "execution_domain",
            "account_id",
            "instrument_id",
            "position_side",
            "event_time",
        ],
    )
    op.add_column(
        "venue_fact_input_links",
        sa.Column("venue_position_snapshot_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_venue_fact_input_links_position_snapshot",
        "venue_fact_input_links",
        "venue_position_snapshots",
        ["venue_position_snapshot_id"],
        ["venue_position_snapshot_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_venue_fact_input_links_position_fact",
        "venue_fact_input_links",
        ["reconciliation_input_id", "venue_position_snapshot_id"],
    )
    op.drop_constraint(
        "ck_venue_fact_input_links_exact_fact", "venue_fact_input_links", type_="check"
    )
    op.create_check_constraint(
        "ck_venue_fact_input_links_exact_fact",
        "venue_fact_input_links",
        _link_fact_check(include_position=True),
    )
    _replace_venue_fact_guards(include_position=True)
    op.execute(
        """
        CREATE TRIGGER venue_position_snapshots_immutable
        BEFORE UPDATE OR DELETE ON venue_position_snapshots
        FOR EACH ROW EXECUTE FUNCTION deny_venue_fact_change()
        """
    )
    op.execute(
        """
        CREATE TRIGGER venue_position_snapshots_insert_guard
        BEFORE INSERT ON venue_position_snapshots
        FOR EACH ROW EXECUTE FUNCTION protect_canonical_venue_fact_insert()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER venue_position_snapshots_first_link_guard
        AFTER INSERT ON venue_position_snapshots
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION verify_canonical_venue_fact_first_link()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS venue_position_snapshots_first_link_guard "
        "ON venue_position_snapshots"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS venue_position_snapshots_insert_guard ON venue_position_snapshots"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS venue_position_snapshots_immutable ON venue_position_snapshots"
    )
    _replace_venue_fact_guards(include_position=False)
    op.drop_constraint(
        "ck_venue_fact_input_links_exact_fact", "venue_fact_input_links", type_="check"
    )
    op.drop_constraint(
        "uq_venue_fact_input_links_position_fact", "venue_fact_input_links", type_="unique"
    )
    op.drop_constraint(
        "fk_venue_fact_input_links_position_snapshot",
        "venue_fact_input_links",
        type_="foreignkey",
    )
    op.drop_column("venue_fact_input_links", "venue_position_snapshot_id")
    op.create_check_constraint(
        "ck_venue_fact_input_links_exact_fact",
        "venue_fact_input_links",
        _link_fact_check(include_position=False),
    )
    op.drop_index(
        "ix_venue_position_snapshots_scope_time",
        table_name="venue_position_snapshots",
    )
    op.drop_table("venue_position_snapshots")


def _link_fact_check(*, include_position: bool) -> str:
    if include_position:
        return (
            "(source_type = 'VENUE_ORDERS' AND venue_order_observation_id IS NOT NULL "
            "AND venue_fill_id IS NULL AND venue_position_snapshot_id IS NULL) OR "
            "(source_type = 'VENUE_FILLS' AND venue_order_observation_id IS NULL "
            "AND venue_fill_id IS NOT NULL AND venue_position_snapshot_id IS NULL) OR "
            "(source_type = 'VENUE_POSITIONS' AND venue_order_observation_id IS NULL "
            "AND venue_fill_id IS NULL AND venue_position_snapshot_id IS NOT NULL)"
        )
    return (
        "(source_type = 'VENUE_ORDERS' AND venue_order_observation_id IS NOT NULL "
        "AND venue_fill_id IS NULL) OR "
        "(source_type = 'VENUE_FILLS' AND venue_order_observation_id IS NULL "
        "AND venue_fill_id IS NOT NULL)"
    )


def _replace_venue_fact_guards(*, include_position: bool) -> None:
    expected_source_case = (
        "WHEN 'venue_position_snapshots' THEN 'VENUE_POSITIONS'" if include_position else ""
    )
    position_scope_check = (
        """
            IF TG_TABLE_NAME = 'venue_position_snapshots' AND (
                scope_row.position_mode <> to_jsonb(NEW) ->> 'position_mode'
                OR scope_row.margin_mode <> to_jsonb(NEW) ->> 'margin_mode'
                OR scope_row.collateral_pool_id <> to_jsonb(NEW) ->> 'collateral_pool_id'
            ) THEN
                RAISE EXCEPTION 'canonical venue position scope changed';
            END IF;
        """
        if include_position
        else ""
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION protect_canonical_venue_fact_insert()
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
                WHEN 'venue_fills' THEN 'VENUE_FILLS'
                {expected_source_case}
                ELSE NULL
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

            IF expected_source IS NULL
                OR NEW.organization_id <> run_row.organization_id
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
            {position_scope_check}
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
    _replace_input_link_guard(include_position=include_position)
    _replace_first_link_guard(include_position=include_position)
    normalized_sources = (
        "'VENUE_ORDERS', 'VENUE_FILLS', 'VENUE_POSITIONS'"
        if include_position
        else "'VENUE_ORDERS', 'VENUE_FILLS'"
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
                      AND input_row.source_type IN ({normalized_sources})
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


def _replace_input_link_guard(*, include_position: bool) -> None:
    position_branch = (
        """
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
        """
        if include_position
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
            {position_branch}
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


def _replace_first_link_guard(*, include_position: bool) -> None:
    position_branch = (
        """
            ELSIF TG_TABLE_NAME = 'venue_position_snapshots' THEN
                SELECT EXISTS(
                    SELECT 1 FROM venue_fact_input_links
                    WHERE run_id = NEW.first_seen_run_id
                      AND reconciliation_input_id = NEW.first_seen_input_id
                      AND venue_position_snapshot_id = NEW.venue_position_snapshot_id
                      AND fact_hash = NEW.snapshot_hash
                ) INTO linked;
        """
        if include_position
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
            {position_branch}
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
