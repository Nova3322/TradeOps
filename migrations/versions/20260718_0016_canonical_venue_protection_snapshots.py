"""Add canonical private-venue protection-set snapshots.

Revision ID: 20260718_0016
Revises: 20260718_0015
Create Date: 2026-07-18
"""

# ruff: noqa: S608 - SQL fragments are fixed migration constants.

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0016"
down_revision: str | Sequence[str] | None = "20260718_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "venue_protection_snapshots",
        sa.Column("venue_protection_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("first_seen_run_id", sa.Uuid(), nullable=False),
        sa.Column("first_seen_input_id", sa.Uuid(), nullable=False),
        sa.Column("venue_position_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("venue", sa.String(length=80), nullable=False),
        sa.Column("execution_domain", sa.String(length=120), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=False),
        sa.Column("instrument_id", sa.String(length=255), nullable=False),
        sa.Column("venue_update_id", sa.String(length=255), nullable=False),
        sa.Column("position_mode", sa.String(length=80), nullable=False),
        sa.Column("position_side", sa.String(length=20), nullable=False),
        sa.Column("margin_mode", sa.String(length=80), nullable=False),
        sa.Column("collateral_pool_id", sa.String(length=160), nullable=False),
        sa.Column("protection_state", sa.String(length=20), nullable=False),
        sa.Column("protected_direction", sa.String(length=20), nullable=False),
        sa.Column("position_quantity", sa.Numeric(38, 18), nullable=True),
        sa.Column("covered_quantity", sa.Numeric(38, 18), nullable=True),
        sa.Column("uncovered_quantity", sa.Numeric(38, 18), nullable=True),
        sa.Column("active_stop_order_count", sa.Integer(), nullable=True),
        sa.Column("venue_native", sa.Boolean(), nullable=False),
        sa.Column("reduce_only_confirmed", sa.Boolean(), nullable=False),
        sa.Column("replacement_in_progress", sa.Boolean(), nullable=False),
        sa.Column("order_set_hash", sa.String(length=64), nullable=False),
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
            "protection_state IN ('CONFIRMED', 'DEGRADED', 'UNKNOWN')",
            name="ck_venue_protection_snapshots_state",
        ),
        sa.CheckConstraint(
            "position_mode IN ('ONE_WAY', 'HEDGE') "
            "AND position_side IN ('LONG', 'SHORT', 'BOTH') "
            "AND protected_direction IN ('LONG', 'SHORT', 'UNKNOWN')",
            name="ck_venue_protection_snapshots_shape",
        ),
        sa.CheckConstraint(
            "(position_mode = 'ONE_WAY' AND position_side = 'BOTH') OR "
            "(position_mode = 'HEDGE' AND position_side IN ('LONG', 'SHORT'))",
            name="ck_venue_protection_snapshots_mode_side",
        ),
        sa.CheckConstraint(
            "(protection_state = 'CONFIRMED' "
            "AND protected_direction IN ('LONG', 'SHORT') "
            "AND position_quantity > 0 AND covered_quantity = position_quantity "
            "AND uncovered_quantity = 0 AND active_stop_order_count >= 1 "
            "AND venue_native AND reduce_only_confirmed AND NOT replacement_in_progress) OR "
            "(protection_state = 'DEGRADED' "
            "AND protected_direction IN ('LONG', 'SHORT') "
            "AND position_quantity > 0 AND covered_quantity >= 0 "
            "AND uncovered_quantity >= 0 "
            "AND covered_quantity + uncovered_quantity = position_quantity "
            "AND active_stop_order_count >= 0 "
            "AND (uncovered_quantity > 0 OR active_stop_order_count = 0 "
            "OR NOT venue_native OR NOT reduce_only_confirmed OR replacement_in_progress)) OR "
            "(protection_state = 'UNKNOWN' AND protected_direction = 'UNKNOWN' "
            "AND position_quantity IS NULL AND covered_quantity IS NULL "
            "AND uncovered_quantity IS NULL AND active_stop_order_count IS NULL "
            "AND NOT venue_native AND NOT reduce_only_confirmed "
            "AND NOT replacement_in_progress)",
            name="ck_venue_protection_snapshots_coverage",
        ),
        sa.CheckConstraint(
            "event_time <= venue_observed_at AND venue_observed_at <= first_received_at "
            "AND first_received_at <= recorded_at",
            name="ck_venue_protection_snapshots_time_order",
        ),
        sa.CheckConstraint(
            "venue_confirmed AND fact_authority = 'VENUE_PRIVATE' "
            "AND environment = 'SHADOW' AND live_dispatch_eligible = false",
            name="ck_venue_protection_snapshots_authority",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(normalized_payload) = 'object' "
            "AND length(order_set_hash) = 64 AND length(raw_payload_hash) = 64 "
            "AND length(evidence_hash) = 64 AND length(snapshot_hash) = 64",
            name="ck_venue_protection_snapshots_integrity",
        ),
        sa.ForeignKeyConstraint(
            ["first_seen_input_id"],
            ["execution_reconciliation_inputs.input_id"],
            name="fk_venue_protection_snapshots_first_input",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["first_seen_run_id", "organization_id"],
            [
                "execution_reconciliation_runs.run_id",
                "execution_reconciliation_runs.organization_id",
            ],
            name="fk_venue_protection_snapshots_first_run_org",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["venue_position_snapshot_id"],
            ["venue_position_snapshots.venue_position_snapshot_id"],
            name="fk_venue_protection_snapshots_position",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("venue_protection_snapshot_id"),
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
            name="uq_venue_protection_snapshots_external_update",
        ),
    )
    op.create_index(
        "ix_venue_protection_snapshots_scope_time",
        "venue_protection_snapshots",
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
        sa.Column("venue_protection_snapshot_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_venue_fact_input_links_protection_snapshot",
        "venue_fact_input_links",
        "venue_protection_snapshots",
        ["venue_protection_snapshot_id"],
        ["venue_protection_snapshot_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_venue_fact_input_links_protection_fact",
        "venue_fact_input_links",
        ["reconciliation_input_id", "venue_protection_snapshot_id"],
    )
    op.drop_constraint(
        "ck_venue_fact_input_links_exact_fact", "venue_fact_input_links", type_="check"
    )
    op.create_check_constraint(
        "ck_venue_fact_input_links_exact_fact",
        "venue_fact_input_links",
        _link_fact_check(include_protection=True),
    )
    _replace_venue_fact_guards(include_protection=True)
    op.execute(
        """
        CREATE TRIGGER venue_protection_snapshots_immutable
        BEFORE UPDATE OR DELETE ON venue_protection_snapshots
        FOR EACH ROW EXECUTE FUNCTION deny_venue_fact_change()
        """
    )
    op.execute(
        """
        CREATE TRIGGER venue_protection_snapshots_insert_guard
        BEFORE INSERT ON venue_protection_snapshots
        FOR EACH ROW EXECUTE FUNCTION protect_canonical_venue_fact_insert()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER venue_protection_snapshots_first_link_guard
        AFTER INSERT ON venue_protection_snapshots
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION verify_canonical_venue_fact_first_link()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS venue_protection_snapshots_first_link_guard "
        "ON venue_protection_snapshots"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS venue_protection_snapshots_insert_guard "
        "ON venue_protection_snapshots"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS venue_protection_snapshots_immutable ON venue_protection_snapshots"
    )
    _replace_venue_fact_guards(include_protection=False)
    op.drop_constraint(
        "ck_venue_fact_input_links_exact_fact", "venue_fact_input_links", type_="check"
    )
    op.drop_constraint(
        "uq_venue_fact_input_links_protection_fact",
        "venue_fact_input_links",
        type_="unique",
    )
    op.drop_constraint(
        "fk_venue_fact_input_links_protection_snapshot",
        "venue_fact_input_links",
        type_="foreignkey",
    )
    op.drop_column("venue_fact_input_links", "venue_protection_snapshot_id")
    op.create_check_constraint(
        "ck_venue_fact_input_links_exact_fact",
        "venue_fact_input_links",
        _link_fact_check(include_protection=False),
    )
    op.drop_index(
        "ix_venue_protection_snapshots_scope_time",
        table_name="venue_protection_snapshots",
    )
    op.drop_table("venue_protection_snapshots")


def _link_fact_check(*, include_protection: bool) -> str:
    protection_null = "AND venue_protection_snapshot_id IS NULL " if include_protection else ""
    protection_branch = (
        "OR (source_type = 'VENUE_PROTECTION' AND venue_order_observation_id IS NULL "
        "AND venue_fill_id IS NULL AND venue_position_snapshot_id IS NULL "
        "AND venue_protection_snapshot_id IS NOT NULL)"
        if include_protection
        else ""
    )
    return (
        "(source_type = 'VENUE_ORDERS' AND venue_order_observation_id IS NOT NULL "
        f"AND venue_fill_id IS NULL AND venue_position_snapshot_id IS NULL {protection_null}) OR "
        "(source_type = 'VENUE_FILLS' AND venue_order_observation_id IS NULL "
        "AND venue_fill_id IS NOT NULL AND venue_position_snapshot_id IS NULL "
        f"{protection_null}) OR "
        "(source_type = 'VENUE_POSITIONS' AND venue_order_observation_id IS NULL "
        f"AND venue_fill_id IS NULL AND venue_position_snapshot_id IS NOT NULL {protection_null}) "
        f"{protection_branch}"
    )


def _replace_venue_fact_guards(*, include_protection: bool) -> None:
    protection_source = (
        "WHEN 'venue_protection_snapshots' THEN 'VENUE_PROTECTION'" if include_protection else ""
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
        DECLARE position_row venue_position_snapshots%ROWTYPE;
        DECLARE expected_source text;
        DECLARE latest_run_id uuid;
        BEGIN
            expected_source := CASE TG_TABLE_NAME
                WHEN 'venue_order_observations' THEN 'VENUE_ORDERS'
                WHEN 'venue_fills' THEN 'VENUE_FILLS'
                WHEN 'venue_position_snapshots' THEN 'VENUE_POSITIONS'
                {protection_source}
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
            IF TG_TABLE_NAME IN (
                'venue_position_snapshots', 'venue_protection_snapshots'
            ) AND (
                scope_row.position_mode <> to_jsonb(NEW) ->> 'position_mode'
                OR scope_row.margin_mode <> to_jsonb(NEW) ->> 'margin_mode'
                OR scope_row.collateral_pool_id <> to_jsonb(NEW) ->> 'collateral_pool_id'
            ) THEN
                RAISE EXCEPTION 'canonical venue position scope changed';
            END IF;
            IF TG_TABLE_NAME = 'venue_protection_snapshots' THEN
                SELECT * INTO STRICT position_row FROM venue_position_snapshots
                WHERE venue_position_snapshot_id = NEW.venue_position_snapshot_id;
                IF position_row.organization_id <> NEW.organization_id
                    OR position_row.venue <> NEW.venue
                    OR position_row.execution_domain <> NEW.execution_domain
                    OR position_row.account_id <> NEW.account_id
                    OR position_row.instrument_id <> NEW.instrument_id
                    OR position_row.position_mode <> NEW.position_mode
                    OR position_row.position_side <> NEW.position_side
                    OR position_row.margin_mode <> NEW.margin_mode
                    OR position_row.collateral_pool_id <> NEW.collateral_pool_id
                    OR position_row.position_state <> 'OPEN'
                    OR NEW.event_time < position_row.event_time
                    OR (NEW.protection_state <> 'UNKNOWN' AND (
                        NEW.protected_direction <> position_row.direction
                        OR NEW.position_quantity <> position_row.quantity
                    )) THEN
                    RAISE EXCEPTION 'canonical venue protection position binding is invalid';
                END IF;
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
    _replace_input_link_guard(include_protection=include_protection)
    _replace_first_link_guard(include_protection=include_protection)
    normalized_sources = (
        "'VENUE_ORDERS', 'VENUE_FILLS', 'VENUE_POSITIONS', 'VENUE_PROTECTION'"
        if include_protection
        else "'VENUE_ORDERS', 'VENUE_FILLS', 'VENUE_POSITIONS'"
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


def _replace_input_link_guard(*, include_protection: bool) -> None:
    protection_branch = (
        """
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
        """
        if include_protection
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
            {protection_branch}
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


def _replace_first_link_guard(*, include_protection: bool) -> None:
    protection_branch = (
        """
            ELSIF TG_TABLE_NAME = 'venue_protection_snapshots' THEN
                SELECT EXISTS(
                    SELECT 1 FROM venue_fact_input_links
                    WHERE run_id = NEW.first_seen_run_id
                      AND reconciliation_input_id = NEW.first_seen_input_id
                      AND venue_protection_snapshot_id = NEW.venue_protection_snapshot_id
                      AND fact_hash = NEW.snapshot_hash
                ) INTO linked;
        """
        if include_protection
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
            ELSIF TG_TABLE_NAME = 'venue_position_snapshots' THEN
                SELECT EXISTS(
                    SELECT 1 FROM venue_fact_input_links
                    WHERE run_id = NEW.first_seen_run_id
                      AND reconciliation_input_id = NEW.first_seen_input_id
                      AND venue_position_snapshot_id = NEW.venue_position_snapshot_id
                      AND fact_hash = NEW.snapshot_hash
                ) INTO linked;
            {protection_branch}
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
