"""Require execution-fact v4 to bind canonical venue positions.

Revision ID: 20260718_0015
Revises: 20260718_0014
Create Date: 2026-07-18
"""

# ruff: noqa: S608 - SQL fragments are fixed migration constants.

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260718_0015"
down_revision: str | Sequence[str] | None = "20260718_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "execution_facts",
        sa.Column("venue_position_snapshot_id", sa.Uuid(), nullable=True),
    )
    op.drop_constraint("ck_execution_facts_contract_version", "execution_facts", type_="check")
    op.drop_constraint("ck_execution_facts_reconciled_binding", "execution_facts", type_="check")
    op.create_check_constraint(
        "ck_execution_facts_contract_version",
        "execution_facts",
        "fact_contract_version IN (1, 2, 3, 4)",
    )
    op.create_check_constraint(
        "ck_execution_facts_reconciled_binding",
        "execution_facts",
        _v4_binding_check(),
    )
    op.create_foreign_key(
        "fk_execution_facts_venue_position_snapshot",
        "execution_facts",
        "venue_position_snapshots",
        ["venue_position_snapshot_id"],
        ["venue_position_snapshot_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_execution_facts_venue_position_snapshot",
        "execution_facts",
        ["venue_position_snapshot_id"],
    )
    _create_v4_prepare_guard()
    _create_v4_restore_guard()
    _replace_execution_fact_application_guard(contract_version=4)


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM execution_facts WHERE fact_contract_version = 4
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade execution-fact v4 while v4 facts remain';
            END IF;
        END;
        $$
        """
    )
    op.execute("DROP TRIGGER IF EXISTS z_execution_facts_v4_restore ON execution_facts")
    op.execute("DROP FUNCTION IF EXISTS restore_execution_fact_v4_contract()")
    op.execute("DROP TRIGGER IF EXISTS a_execution_facts_v4_prepare ON execution_facts")
    op.execute("DROP FUNCTION IF EXISTS prepare_execution_fact_v4_contract()")
    _replace_execution_fact_application_guard(contract_version=3)
    op.drop_constraint("ck_execution_facts_reconciled_binding", "execution_facts", type_="check")
    op.drop_constraint("ck_execution_facts_contract_version", "execution_facts", type_="check")
    op.drop_constraint(
        "uq_execution_facts_venue_position_snapshot", "execution_facts", type_="unique"
    )
    op.drop_constraint(
        "fk_execution_facts_venue_position_snapshot", "execution_facts", type_="foreignkey"
    )
    op.drop_column("execution_facts", "venue_position_snapshot_id")
    op.create_check_constraint(
        "ck_execution_facts_contract_version",
        "execution_facts",
        "fact_contract_version IN (1, 2, 3)",
    )
    op.create_check_constraint(
        "ck_execution_facts_reconciled_binding",
        "execution_facts",
        _legacy_binding_check(include_position_column=False),
    )


def _legacy_binding_check(*, include_position_column: bool) -> str:
    position_null = "AND venue_position_snapshot_id IS NULL " if include_position_column else ""
    return (
        "(fact_contract_version = 1 AND fact_kind IS NULL "
        "AND shadow_dispatch_claim_id IS NULL AND reconciliation_run_id IS NULL "
        "AND reconciliation_input_id IS NULL AND reconciliation_source_type IS NULL "
        "AND reconciliation_run_hash IS NULL AND reconciliation_input_hash IS NULL "
        "AND dispatch_claim_hash IS NULL AND venue_order_observation_id IS NULL "
        f"AND venue_fill_id IS NULL {position_null}"
        "AND venue_fact_input_link_id IS NULL AND venue_fact_hash IS NULL "
        "AND canonical_venue_order_id IS NULL) OR "
        "(fact_contract_version = 2 "
        "AND fact_kind IN ('WORKER_RECEIPT', 'VENUE_ORDER', 'VENUE_FILL', "
        "'VENUE_POSITION', 'VENUE_PROTECTION') "
        "AND shadow_dispatch_claim_id IS NOT NULL AND reconciliation_run_id IS NOT NULL "
        "AND reconciliation_input_id IS NOT NULL "
        "AND reconciliation_source_type IN ('TRADING_LEDGER', 'VENUE_ORDERS', "
        "'VENUE_FILLS', 'VENUE_POSITIONS', 'VENUE_BALANCES', "
        "'VENUE_PROTECTION', 'WORKER_LOCAL') "
        "AND length(reconciliation_run_hash) = 64 "
        "AND length(reconciliation_input_hash) = 64 "
        "AND length(dispatch_claim_hash) = 64 AND reconciliation_run_ref IS NULL "
        "AND venue_order_observation_id IS NULL AND venue_fill_id IS NULL "
        f"{position_null}"
        "AND venue_fact_input_link_id IS NULL AND venue_fact_hash IS NULL "
        "AND canonical_venue_order_id IS NULL) OR "
        "(fact_contract_version = 3 "
        "AND fact_kind IN ('WORKER_RECEIPT', 'VENUE_ORDER', 'VENUE_FILL', "
        "'VENUE_POSITION', 'VENUE_PROTECTION') "
        "AND shadow_dispatch_claim_id IS NOT NULL AND reconciliation_run_id IS NOT NULL "
        "AND reconciliation_input_id IS NOT NULL "
        "AND reconciliation_source_type IN ('TRADING_LEDGER', 'VENUE_ORDERS', "
        "'VENUE_FILLS', 'VENUE_POSITIONS', 'VENUE_BALANCES', "
        "'VENUE_PROTECTION', 'WORKER_LOCAL') "
        "AND length(reconciliation_run_hash) = 64 "
        "AND length(reconciliation_input_hash) = 64 "
        "AND length(dispatch_claim_hash) = 64 AND reconciliation_run_ref IS NULL "
        "AND ((fact_kind = 'VENUE_ORDER' AND venue_order_observation_id IS NOT NULL "
        f"AND venue_fill_id IS NULL {position_null}"
        "AND venue_fact_input_link_id IS NOT NULL AND length(venue_fact_hash) = 64 "
        "AND canonical_venue_order_id IS NOT NULL) OR "
        "(fact_kind = 'VENUE_FILL' AND venue_order_observation_id IS NULL "
        f"AND venue_fill_id IS NOT NULL {position_null}"
        "AND venue_fact_input_link_id IS NOT NULL AND length(venue_fact_hash) = 64 "
        "AND canonical_venue_order_id IS NOT NULL) OR "
        "(fact_kind NOT IN ('VENUE_ORDER', 'VENUE_FILL') "
        "AND venue_order_observation_id IS NULL AND venue_fill_id IS NULL "
        f"{position_null}"
        "AND venue_fact_input_link_id IS NULL AND venue_fact_hash IS NULL "
        "AND canonical_venue_order_id IS NULL)))"
    )


def _v4_binding_check() -> str:
    return (
        _legacy_binding_check(include_position_column=True) + " OR " + "(fact_contract_version = 4 "
        "AND fact_kind IN ('WORKER_RECEIPT', 'VENUE_ORDER', 'VENUE_FILL', "
        "'VENUE_POSITION', 'VENUE_PROTECTION') "
        "AND shadow_dispatch_claim_id IS NOT NULL AND reconciliation_run_id IS NOT NULL "
        "AND reconciliation_input_id IS NOT NULL "
        "AND reconciliation_source_type IN ('TRADING_LEDGER', 'VENUE_ORDERS', "
        "'VENUE_FILLS', 'VENUE_POSITIONS', 'VENUE_BALANCES', "
        "'VENUE_PROTECTION', 'WORKER_LOCAL') "
        "AND length(reconciliation_run_hash) = 64 "
        "AND length(reconciliation_input_hash) = 64 "
        "AND length(dispatch_claim_hash) = 64 AND reconciliation_run_ref IS NULL "
        "AND ((fact_kind = 'VENUE_ORDER' AND venue_order_observation_id IS NOT NULL "
        "AND venue_fill_id IS NULL AND venue_position_snapshot_id IS NULL "
        "AND venue_fact_input_link_id IS NOT NULL AND length(venue_fact_hash) = 64 "
        "AND canonical_venue_order_id IS NOT NULL) OR "
        "(fact_kind = 'VENUE_FILL' AND venue_order_observation_id IS NULL "
        "AND venue_fill_id IS NOT NULL AND venue_position_snapshot_id IS NULL "
        "AND venue_fact_input_link_id IS NOT NULL AND length(venue_fact_hash) = 64 "
        "AND canonical_venue_order_id IS NOT NULL) OR "
        "(fact_kind = 'VENUE_POSITION' AND venue_order_observation_id IS NULL "
        "AND venue_fill_id IS NULL AND venue_position_snapshot_id IS NOT NULL "
        "AND venue_fact_input_link_id IS NOT NULL AND length(venue_fact_hash) = 64 "
        "AND canonical_venue_order_id IS NULL) OR "
        "(fact_kind NOT IN ('VENUE_ORDER', 'VENUE_FILL', 'VENUE_POSITION') "
        "AND venue_order_observation_id IS NULL AND venue_fill_id IS NULL "
        "AND venue_position_snapshot_id IS NULL AND venue_fact_input_link_id IS NULL "
        "AND venue_fact_hash IS NULL AND canonical_venue_order_id IS NULL)))"
    )


def _create_v4_prepare_guard() -> None:
    op.execute(
        """
        CREATE FUNCTION prepare_execution_fact_v4_contract()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE claim_row shadow_dispatch_claims%ROWTYPE;
        DECLARE scope_row execution_sender_scopes%ROWTYPE;
        DECLARE intent_row order_intents%ROWTYPE;
        DECLARE state_row order_intent_states%ROWTYPE;
        DECLARE reservation_row risk_reservations%ROWTYPE;
        DECLARE link_row venue_fact_input_links%ROWTYPE;
        DECLARE snapshot_row venue_position_snapshots%ROWTYPE;
        DECLARE expected_position_side text;
        DECLARE expected_quantity numeric;
        DECLARE expected_payload jsonb;
        DECLARE latest_execution_event timestamptz;
        DECLARE prior_canonical_order_id text;
        BEGIN
            IF NEW.fact_contract_version <> 4 THEN
                RAISE EXCEPTION 'new execution facts require the reconciled v4 contract';
            END IF;

            IF NEW.canonical_venue_order_id IS NOT NULL THEN
                SELECT canonical_venue_order_id INTO prior_canonical_order_id
                FROM execution_facts
                WHERE order_intent_id = NEW.order_intent_id
                  AND fact_contract_version IN (3, 4)
                  AND canonical_venue_order_id IS NOT NULL
                ORDER BY fact_sequence LIMIT 1;
                IF prior_canonical_order_id IS NOT NULL
                    AND prior_canonical_order_id <> NEW.canonical_venue_order_id THEN
                    RAISE EXCEPTION 'execution fact canonical venue order identity changed';
                END IF;
            END IF;

            IF NEW.fact_kind = 'VENUE_POSITION' THEN
                IF NEW.target_status <> 'POSITION_RECONCILED'
                    OR NEW.reconciliation_source_type <> 'VENUE_POSITIONS'
                    OR NEW.venue_order_observation_id IS NOT NULL
                    OR NEW.venue_fill_id IS NOT NULL
                    OR NEW.venue_position_snapshot_id IS NULL
                    OR NEW.venue_fact_input_link_id IS NULL
                    OR NEW.venue_fact_hash IS NULL
                    OR NEW.canonical_venue_order_id IS NOT NULL THEN
                    RAISE EXCEPTION 'execution fact canonical position contract is invalid';
                END IF;
                SELECT * INTO STRICT claim_row FROM shadow_dispatch_claims
                WHERE claim_id = NEW.shadow_dispatch_claim_id;
                SELECT * INTO STRICT scope_row FROM execution_sender_scopes
                WHERE scope_id = claim_row.scope_id;
                SELECT * INTO STRICT intent_row FROM order_intents
                WHERE order_intent_id = NEW.order_intent_id;
                SELECT * INTO STRICT state_row FROM order_intent_states
                WHERE order_intent_id = NEW.order_intent_id;
                SELECT * INTO STRICT reservation_row FROM risk_reservations
                WHERE order_intent_id = NEW.order_intent_id;
                SELECT * INTO STRICT link_row FROM venue_fact_input_links
                WHERE venue_fact_input_link_id = NEW.venue_fact_input_link_id;
                SELECT * INTO STRICT snapshot_row FROM venue_position_snapshots
                WHERE venue_position_snapshot_id = NEW.venue_position_snapshot_id;
                expected_position_side := CASE
                    WHEN scope_row.position_mode = 'ONE_WAY' THEN 'BOTH'
                    ELSE intent_row.position_side
                END;
                expected_quantity := intent_row.current_position_quantity
                    + state_row.cumulative_filled_quantity;
                SELECT max(event_time) INTO latest_execution_event
                FROM execution_facts
                WHERE order_intent_id = NEW.order_intent_id
                  AND fact_kind IN ('VENUE_ORDER', 'VENUE_FILL');
                expected_payload := jsonb_build_object(
                    'venue_fact_type', 'VENUE_POSITION_SNAPSHOT',
                    'venue_fact_id', snapshot_row.venue_position_snapshot_id::text,
                    'venue_fact_hash', snapshot_row.snapshot_hash,
                    'venue_fact_input_link_id', link_row.venue_fact_input_link_id::text
                );

                IF link_row.run_id <> NEW.reconciliation_run_id
                    OR link_row.reconciliation_input_id <> NEW.reconciliation_input_id
                    OR link_row.organization_id <> claim_row.organization_id
                    OR link_row.source_type <> 'VENUE_POSITIONS'
                    OR link_row.input_hash <> NEW.reconciliation_input_hash
                    OR link_row.fact_hash <> NEW.venue_fact_hash
                    OR link_row.venue_position_snapshot_id
                        <> snapshot_row.venue_position_snapshot_id
                    OR link_row.venue_order_observation_id IS NOT NULL
                    OR link_row.venue_fill_id IS NOT NULL THEN
                    RAISE EXCEPTION 'execution fact canonical position input link is invalid';
                END IF;
                IF snapshot_row.snapshot_hash <> NEW.venue_fact_hash
                    OR snapshot_row.organization_id <> claim_row.organization_id
                    OR snapshot_row.venue <> intent_row.venue
                    OR snapshot_row.execution_domain <> intent_row.execution_domain
                    OR snapshot_row.account_id <> intent_row.account_id
                    OR snapshot_row.instrument_id <> intent_row.instrument_id
                    OR snapshot_row.position_mode <> scope_row.position_mode
                    OR snapshot_row.position_side <> expected_position_side
                    OR snapshot_row.margin_mode <> scope_row.margin_mode
                    OR snapshot_row.collateral_pool_id <> scope_row.collateral_pool_id
                    OR snapshot_row.collateral_pool_id <> reservation_row.collateral_pool_id
                    OR (snapshot_row.position_state = 'OPEN'
                        AND snapshot_row.direction <> intent_row.position_side) THEN
                    RAISE EXCEPTION 'execution fact canonical position ownership is invalid';
                END IF;
                IF state_row.status NOT IN ('FILLED', 'CANCELLED_PARTIAL')
                    OR snapshot_row.position_state <> 'OPEN'
                    OR snapshot_row.quantity <> expected_quantity
                    OR latest_execution_event IS NULL
                    OR snapshot_row.event_time < latest_execution_event THEN
                    RAISE EXCEPTION 'execution fact canonical position quantity is invalid';
                END IF;
                IF NEW.external_fact_id <> snapshot_row.venue_position_snapshot_id::text
                    OR NEW.cumulative_filled_quantity
                        <> state_row.cumulative_filled_quantity
                    OR NEW.known_remaining_quantity <> state_row.known_remaining_quantity
                    OR NEW.zero_fill_confirmed
                    OR NOT NEW.venue_order_terminal
                    OR NOT state_row.venue_order_terminal
                    OR NOT NEW.position_reconciled
                    OR NEW.protection_confirmed
                    OR NEW.event_time <> snapshot_row.event_time
                    OR NEW.received_at <> link_row.received_at
                    OR NEW.source_ref <> link_row.raw_payload_ref
                    OR NEW.evidence_ref <> link_row.evidence_ref
                    OR NEW.payload <> expected_payload THEN
                    RAISE EXCEPTION 'execution fact canonical position semantics are invalid';
                END IF;
            ELSIF NEW.venue_position_snapshot_id IS NOT NULL THEN
                RAISE EXCEPTION 'execution fact canonical position reference is misplaced';
            END IF;

            NEW.fact_contract_version := 3;
            RETURN NEW;
        EXCEPTION
            WHEN NO_DATA_FOUND THEN
                RAISE EXCEPTION 'execution fact canonical position reference is unavailable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER a_execution_facts_v4_prepare
        BEFORE INSERT ON execution_facts
        FOR EACH ROW EXECUTE FUNCTION prepare_execution_fact_v4_contract()
        """
    )


def _create_v4_restore_guard() -> None:
    op.execute(
        """
        CREATE FUNCTION restore_execution_fact_v4_contract()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.fact_contract_version <> 3 THEN
                RAISE EXCEPTION 'execution fact v4 guard sequence is invalid';
            END IF;
            NEW.fact_contract_version := 4;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER z_execution_facts_v4_restore
        BEFORE INSERT ON execution_facts
        FOR EACH ROW EXECUTE FUNCTION restore_execution_fact_v4_contract()
        """
    )


def _replace_execution_fact_application_guard(*, contract_version: int) -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION verify_execution_fact_application()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE state_row order_intent_states%ROWTYPE;
        DECLARE reservation_row risk_reservations%ROWTYPE;
        DECLARE exposure_row risk_exposure_states%ROWTYPE;
        DECLARE expected_reserved numeric;
        DECLARE expected_open numeric;
        DECLARE expected_unknown numeric;
        DECLARE expected_released numeric;
        BEGIN
            IF NEW.fact_contract_version <> {contract_version} THEN
                RETURN NULL;
            END IF;
            SELECT * INTO STRICT state_row FROM order_intent_states
            WHERE order_intent_id = NEW.order_intent_id;
            SELECT * INTO STRICT reservation_row FROM risk_reservations
            WHERE order_intent_id = NEW.order_intent_id;
            SELECT * INTO STRICT exposure_row FROM risk_exposure_states
            WHERE risk_reservation_id = reservation_row.risk_reservation_id;

            IF NEW.target_status IN ('CANCELLED_ZERO_FILL', 'REJECTED_ZERO_FILL') THEN
                expected_reserved := 0;
                expected_open := 0;
                expected_unknown := 0;
                expected_released := state_row.intent_quantity;
            ELSIF NEW.target_status = 'RESULT_UNKNOWN' THEN
                expected_reserved := 0;
                expected_open := NEW.cumulative_filled_quantity;
                expected_unknown := state_row.intent_quantity
                    - NEW.cumulative_filled_quantity;
                expected_released := 0;
            ELSE
                expected_open := NEW.cumulative_filled_quantity;
                expected_unknown := 0;
                expected_reserved := CASE
                    WHEN NEW.venue_order_terminal THEN 0
                    ELSE NEW.known_remaining_quantity
                END;
                expected_released := CASE
                    WHEN NEW.venue_order_terminal
                        THEN state_row.intent_quantity - NEW.cumulative_filled_quantity
                    ELSE 0
                END;
            END IF;

            IF state_row.last_fact_sequence <> NEW.fact_sequence
                OR state_row.last_fact_hash <> NEW.evidence_hash
                OR state_row.status <> NEW.target_status
                OR state_row.cumulative_filled_quantity
                    <> NEW.cumulative_filled_quantity
                OR state_row.known_remaining_quantity <> NEW.known_remaining_quantity
                OR state_row.zero_fill_confirmed <> NEW.zero_fill_confirmed
                OR state_row.venue_order_terminal <> NEW.venue_order_terminal
                OR state_row.position_reconciled <> NEW.position_reconciled
                OR state_row.protection_confirmed <> NEW.protection_confirmed
                OR exposure_row.reserved_quantity <> expected_reserved
                OR exposure_row.open_quantity <> expected_open
                OR exposure_row.unknown_quantity <> expected_unknown
                OR exposure_row.released_quantity <> expected_released THEN
                RAISE EXCEPTION
                    'execution fact requires atomic intent-state and exposure application';
            END IF;
            RETURN NULL;
        EXCEPTION
            WHEN NO_DATA_FOUND THEN
                RAISE EXCEPTION 'execution fact application reference is unavailable';
        END;
        $$
        """
    )
