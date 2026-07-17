"""Require execution-fact v3 to cite canonical venue facts.

Revision ID: 20260718_0013
Revises: 20260718_0012
Create Date: 2026-07-18
"""

# ruff: noqa: S608 - SQL fragments are fixed migration constants.

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260718_0013"
down_revision: str | Sequence[str] | None = "20260718_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "execution_facts",
        sa.Column("venue_order_observation_id", sa.Uuid(), nullable=True),
    )
    op.add_column("execution_facts", sa.Column("venue_fill_id", sa.Uuid(), nullable=True))
    op.add_column(
        "execution_facts",
        sa.Column("venue_fact_input_link_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "execution_facts", sa.Column("venue_fact_hash", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "execution_facts",
        sa.Column("canonical_venue_order_id", sa.String(length=255), nullable=True),
    )
    op.drop_constraint("ck_execution_facts_contract_version", "execution_facts", type_="check")
    op.drop_constraint("ck_execution_facts_reconciled_binding", "execution_facts", type_="check")
    op.create_check_constraint(
        "ck_execution_facts_contract_version",
        "execution_facts",
        "fact_contract_version IN (1, 2, 3)",
    )
    op.create_check_constraint(
        "ck_execution_facts_reconciled_binding",
        "execution_facts",
        _v3_binding_check(),
    )
    op.create_foreign_key(
        "fk_execution_facts_venue_order_observation",
        "execution_facts",
        "venue_order_observations",
        ["venue_order_observation_id"],
        ["venue_order_observation_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_execution_facts_venue_fill",
        "execution_facts",
        "venue_fills",
        ["venue_fill_id"],
        ["venue_fill_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_execution_facts_venue_fact_input_link",
        "execution_facts",
        "venue_fact_input_links",
        ["venue_fact_input_link_id"],
        ["venue_fact_input_link_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_execution_facts_venue_order_observation",
        "execution_facts",
        ["venue_order_observation_id"],
    )
    op.create_unique_constraint(
        "uq_execution_facts_venue_fill", "execution_facts", ["venue_fill_id"]
    )
    op.create_unique_constraint(
        "uq_execution_facts_venue_fact_input_link",
        "execution_facts",
        ["venue_fact_input_link_id"],
    )
    op.execute(_execution_fact_guard_sql(contract_version=3))
    _create_execution_fact_application_guard()


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS execution_facts_application_guard ON execution_facts")
    op.execute("DROP FUNCTION IF EXISTS verify_execution_fact_application()")
    op.execute(_execution_fact_guard_sql(contract_version=2))
    op.drop_constraint(
        "uq_execution_facts_venue_fact_input_link", "execution_facts", type_="unique"
    )
    op.drop_constraint("uq_execution_facts_venue_fill", "execution_facts", type_="unique")
    op.drop_constraint(
        "uq_execution_facts_venue_order_observation", "execution_facts", type_="unique"
    )
    op.drop_constraint(
        "fk_execution_facts_venue_fact_input_link", "execution_facts", type_="foreignkey"
    )
    op.drop_constraint("fk_execution_facts_venue_fill", "execution_facts", type_="foreignkey")
    op.drop_constraint(
        "fk_execution_facts_venue_order_observation", "execution_facts", type_="foreignkey"
    )
    op.drop_constraint("ck_execution_facts_reconciled_binding", "execution_facts", type_="check")
    op.drop_constraint("ck_execution_facts_contract_version", "execution_facts", type_="check")
    op.create_check_constraint(
        "ck_execution_facts_contract_version",
        "execution_facts",
        "fact_contract_version IN (1, 2)",
    )
    op.create_check_constraint(
        "ck_execution_facts_reconciled_binding",
        "execution_facts",
        _v2_binding_check(),
    )
    for column_name in (
        "canonical_venue_order_id",
        "venue_fact_hash",
        "venue_fact_input_link_id",
        "venue_fill_id",
        "venue_order_observation_id",
    ):
        op.drop_column("execution_facts", column_name)


def _v2_binding_check() -> str:
    return (
        "(fact_contract_version = 1 AND fact_kind IS NULL "
        "AND shadow_dispatch_claim_id IS NULL AND reconciliation_run_id IS NULL "
        "AND reconciliation_input_id IS NULL AND reconciliation_source_type IS NULL "
        "AND reconciliation_run_hash IS NULL AND reconciliation_input_hash IS NULL "
        "AND dispatch_claim_hash IS NULL) OR "
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
        "AND length(dispatch_claim_hash) = 64 AND reconciliation_run_ref IS NULL)"
    )


def _v3_binding_check() -> str:
    return (
        "(fact_contract_version = 1 AND fact_kind IS NULL "
        "AND shadow_dispatch_claim_id IS NULL AND reconciliation_run_id IS NULL "
        "AND reconciliation_input_id IS NULL AND reconciliation_source_type IS NULL "
        "AND reconciliation_run_hash IS NULL AND reconciliation_input_hash IS NULL "
        "AND dispatch_claim_hash IS NULL AND venue_order_observation_id IS NULL "
        "AND venue_fill_id IS NULL AND venue_fact_input_link_id IS NULL "
        "AND venue_fact_hash IS NULL AND canonical_venue_order_id IS NULL) OR "
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
        "AND venue_fill_id IS NULL AND venue_fact_input_link_id IS NOT NULL "
        "AND length(venue_fact_hash) = 64 AND canonical_venue_order_id IS NOT NULL) OR "
        "(fact_kind = 'VENUE_FILL' AND venue_order_observation_id IS NULL "
        "AND venue_fill_id IS NOT NULL AND venue_fact_input_link_id IS NOT NULL "
        "AND length(venue_fact_hash) = 64 AND canonical_venue_order_id IS NOT NULL) OR "
        "(fact_kind NOT IN ('VENUE_ORDER', 'VENUE_FILL') "
        "AND venue_order_observation_id IS NULL AND venue_fill_id IS NULL "
        "AND venue_fact_input_link_id IS NULL AND venue_fact_hash IS NULL "
        "AND canonical_venue_order_id IS NULL)))"
    )


def _execution_fact_guard_sql(*, contract_version: int) -> str:
    version_check = (
        "NEW.fact_contract_version <> 3"
        if contract_version == 3
        else "NEW.fact_contract_version <> 2"
    )
    contract_message = f"new execution facts require the reconciled v{contract_version} contract"
    if contract_version == 3:
        canonical_declarations = """
        DECLARE intent_state order_intent_states%ROWTYPE;
        DECLARE scope_row execution_sender_scopes%ROWTYPE;
        DECLARE link_row venue_fact_input_links%ROWTYPE;
        DECLARE order_row venue_order_observations%ROWTYPE;
        DECLARE fill_row venue_fills%ROWTYPE;
        DECLARE expected_position_side text;
        DECLARE expected_target text;
        DECLARE expected_cumulative numeric;
        DECLARE expected_remaining numeric;
        DECLARE expected_payload jsonb;
        """
        canonical_load = """
            SELECT * INTO STRICT intent_state FROM order_intent_states
            WHERE order_intent_id = NEW.order_intent_id;
            SELECT * INTO STRICT scope_row FROM execution_sender_scopes
            WHERE scope_id = claim_row.scope_id;
        """
        canonical_checks = """
            IF NEW.fact_kind IN ('VENUE_ORDER', 'VENUE_FILL') THEN
                SELECT * INTO STRICT link_row FROM venue_fact_input_links
                WHERE venue_fact_input_link_id = NEW.venue_fact_input_link_id;
                IF link_row.run_id <> NEW.reconciliation_run_id
                    OR link_row.reconciliation_input_id <> NEW.reconciliation_input_id
                    OR link_row.organization_id <> claim_row.organization_id
                    OR link_row.source_type <> NEW.reconciliation_source_type
                    OR link_row.input_hash <> NEW.reconciliation_input_hash
                    OR link_row.fact_hash <> NEW.venue_fact_hash THEN
                    RAISE EXCEPTION 'execution fact canonical input link is invalid';
                END IF;
                expected_position_side := CASE
                    WHEN scope_row.position_mode = 'ONE_WAY' THEN 'BOTH'
                    ELSE intent_row.position_side
                END;
            END IF;

            IF NEW.fact_kind = 'VENUE_ORDER' THEN
                SELECT * INTO STRICT order_row FROM venue_order_observations
                WHERE venue_order_observation_id = NEW.venue_order_observation_id;
                IF link_row.venue_order_observation_id <> order_row.venue_order_observation_id
                    OR link_row.venue_fill_id IS NOT NULL
                    OR order_row.observation_hash <> NEW.venue_fact_hash
                    OR order_row.organization_id <> claim_row.organization_id
                    OR order_row.venue <> intent_row.venue
                    OR order_row.execution_domain <> intent_row.execution_domain
                    OR order_row.account_id <> intent_row.account_id
                    OR order_row.instrument_id <> intent_row.instrument_id
                    OR order_row.observed_client_order_id <> claim_row.client_order_id
                    OR order_row.side <> intent_row.side
                    OR order_row.position_side <> expected_position_side
                    OR order_row.reduce_only <> intent_row.reduce_only
                    OR order_row.order_type <> intent_row.order_type
                    OR order_row.time_in_force <> intent_row.time_in_force
                    OR order_row.original_quantity <> intent_state.intent_quantity
                    OR order_row.cumulative_filled_quantity
                        <> intent_state.cumulative_filled_quantity THEN
                    RAISE EXCEPTION 'execution fact canonical order ownership is invalid';
                END IF;
                expected_target := CASE
                    WHEN order_row.status = 'OPEN' THEN 'VENUE_ACKNOWLEDGED'
                    WHEN order_row.status = 'CANCEL_PENDING' THEN 'CANCEL_PENDING'
                    WHEN order_row.status = 'REJECTED' THEN 'REJECTED_ZERO_FILL'
                    WHEN order_row.status IN ('CANCELLED', 'EXPIRED')
                        AND order_row.cumulative_filled_quantity = 0
                        THEN 'CANCELLED_ZERO_FILL'
                    WHEN order_row.status IN ('CANCELLED', 'EXPIRED')
                        THEN 'CANCELLED_PARTIAL'
                    WHEN order_row.status = 'UNKNOWN' THEN 'RESULT_UNKNOWN'
                    ELSE NULL
                END;
                expected_payload := jsonb_build_object(
                    'venue_fact_type', 'VENUE_ORDER_OBSERVATION',
                    'venue_fact_id', order_row.venue_order_observation_id::text,
                    'venue_fact_hash', order_row.observation_hash,
                    'venue_fact_input_link_id', link_row.venue_fact_input_link_id::text,
                    'canonical_venue_order_id', order_row.venue_order_id
                );
                IF expected_target IS NULL OR NEW.target_status <> expected_target
                    OR NEW.external_fact_id <> order_row.venue_order_observation_id::text
                    OR NEW.canonical_venue_order_id <> order_row.venue_order_id
                    OR NEW.cumulative_filled_quantity
                        <> order_row.cumulative_filled_quantity
                    OR NEW.known_remaining_quantity <> order_row.known_remaining_quantity
                    OR NEW.zero_fill_confirmed <> order_row.zero_fill_confirmed
                    OR NEW.venue_order_terminal <> order_row.terminal
                    OR NEW.position_reconciled OR NEW.protection_confirmed
                    OR NEW.event_time <> order_row.event_time
                    OR NEW.received_at <> link_row.received_at
                    OR NEW.source_ref <> link_row.raw_payload_ref
                    OR NEW.evidence_ref <> link_row.evidence_ref
                    OR NEW.payload <> expected_payload THEN
                    RAISE EXCEPTION 'execution fact canonical order semantics are invalid';
                END IF;
            ELSIF NEW.fact_kind = 'VENUE_FILL' THEN
                SELECT * INTO STRICT fill_row FROM venue_fills
                WHERE venue_fill_id = NEW.venue_fill_id;
                IF link_row.venue_fill_id <> fill_row.venue_fill_id
                    OR link_row.venue_order_observation_id IS NOT NULL
                    OR fill_row.fill_hash <> NEW.venue_fact_hash
                    OR fill_row.organization_id <> claim_row.organization_id
                    OR fill_row.venue <> intent_row.venue
                    OR fill_row.execution_domain <> intent_row.execution_domain
                    OR fill_row.account_id <> intent_row.account_id
                    OR fill_row.instrument_id <> intent_row.instrument_id
                    OR fill_row.observed_client_order_id <> claim_row.client_order_id
                    OR fill_row.side <> intent_row.side
                    OR fill_row.position_side <> expected_position_side
                    OR fill_row.reduce_only <> intent_row.reduce_only THEN
                    RAISE EXCEPTION 'execution fact canonical fill ownership is invalid';
                END IF;
                expected_cumulative := intent_state.cumulative_filled_quantity
                    + fill_row.quantity;
                expected_remaining := intent_state.intent_quantity - expected_cumulative;
                expected_target := CASE
                    WHEN expected_remaining > 0 THEN 'PARTIALLY_FILLED'
                    WHEN expected_remaining = 0 THEN 'FILLED'
                    ELSE NULL
                END;
                expected_payload := jsonb_build_object(
                    'venue_fact_type', 'VENUE_FILL',
                    'venue_fact_id', fill_row.venue_fill_id::text,
                    'venue_fact_hash', fill_row.fill_hash,
                    'venue_fact_input_link_id', link_row.venue_fact_input_link_id::text,
                    'canonical_venue_order_id', fill_row.venue_order_id
                );
                IF expected_target IS NULL OR NEW.target_status <> expected_target
                    OR NEW.external_fact_id <> fill_row.venue_fill_id::text
                    OR NEW.canonical_venue_order_id <> fill_row.venue_order_id
                    OR NEW.cumulative_filled_quantity <> expected_cumulative
                    OR NEW.known_remaining_quantity <> expected_remaining
                    OR NEW.zero_fill_confirmed
                    OR NEW.venue_order_terminal <> (expected_remaining = 0)
                    OR NEW.position_reconciled OR NEW.protection_confirmed
                    OR NEW.event_time <> fill_row.event_time
                    OR NEW.received_at <> link_row.received_at
                    OR NEW.source_ref <> link_row.raw_payload_ref
                    OR NEW.evidence_ref <> link_row.evidence_ref
                    OR NEW.payload <> expected_payload THEN
                    RAISE EXCEPTION 'execution fact canonical fill semantics are invalid';
                END IF;
            END IF;
            IF NEW.canonical_venue_order_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM execution_facts prior
                WHERE prior.order_intent_id = NEW.order_intent_id
                  AND prior.fact_contract_version = 3
                  AND prior.canonical_venue_order_id IS NOT NULL
                  AND prior.canonical_venue_order_id <> NEW.canonical_venue_order_id
            ) THEN
                RAISE EXCEPTION 'execution fact canonical venue order identity changed';
            END IF;
        """
        source_matrix = """
                OR (NEW.target_status = 'CANCELLED_PARTIAL'
                    AND NEW.fact_kind = 'VENUE_ORDER'
                    AND NEW.reconciliation_source_type = 'VENUE_ORDERS')
        """
        failed_safe_order = ""
    else:
        canonical_declarations = ""
        canonical_load = ""
        canonical_checks = ""
        source_matrix = """
                OR (NEW.target_status = 'CANCELLED_PARTIAL'
                    AND NEW.fact_kind = 'VENUE_FILL'
                    AND NEW.reconciliation_source_type = 'VENUE_FILLS')
        """
        failed_safe_order = """
                    OR (NEW.fact_kind = 'VENUE_ORDER'
                        AND NEW.reconciliation_source_type = 'VENUE_ORDERS')
        """
    return f"""
        CREATE OR REPLACE FUNCTION protect_reconciled_execution_fact_insert()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE claim_row shadow_dispatch_claims%ROWTYPE;
        DECLARE run_row execution_reconciliation_runs%ROWTYPE;
        DECLARE run_state execution_reconciliation_run_states%ROWTYPE;
        DECLARE input_row execution_reconciliation_inputs%ROWTYPE;
        DECLARE sender_state execution_sender_scope_states%ROWTYPE;
        DECLARE intent_row order_intents%ROWTYPE;
        DECLARE latest_run_id uuid;
        DECLARE lineage_matches boolean;
        {canonical_declarations}
        BEGIN
            IF {version_check}
                OR NEW.fact_kind IS NULL
                OR NEW.shadow_dispatch_claim_id IS NULL
                OR NEW.reconciliation_run_id IS NULL
                OR NEW.reconciliation_input_id IS NULL
                OR NEW.reconciliation_source_type IS NULL THEN
                RAISE EXCEPTION '{contract_message}';
            END IF;

            SELECT * INTO STRICT claim_row FROM shadow_dispatch_claims
            WHERE claim_id = NEW.shadow_dispatch_claim_id;
            SELECT * INTO STRICT run_row FROM execution_reconciliation_runs
            WHERE run_id = NEW.reconciliation_run_id;
            SELECT * INTO STRICT run_state FROM execution_reconciliation_run_states
            WHERE run_id = NEW.reconciliation_run_id;
            SELECT * INTO STRICT input_row FROM execution_reconciliation_inputs
            WHERE input_id = NEW.reconciliation_input_id;
            SELECT * INTO STRICT sender_state FROM execution_sender_scope_states
            WHERE scope_id = claim_row.scope_id;
            SELECT * INTO STRICT intent_row FROM order_intents
            WHERE order_intent_id = NEW.order_intent_id;
            {canonical_load}
            SELECT run_id INTO STRICT latest_run_id FROM execution_reconciliation_runs
            WHERE scope_id = claim_row.scope_id
            ORDER BY started_at DESC, run_id DESC LIMIT 1;

            WITH RECURSIVE lineage(run_id, supersedes_run_id) AS (
                SELECT run_id, supersedes_run_id FROM execution_reconciliation_runs
                WHERE run_id = NEW.reconciliation_run_id
                UNION ALL
                SELECT parent.run_id, parent.supersedes_run_id
                FROM execution_reconciliation_runs parent
                JOIN lineage child ON parent.run_id = child.supersedes_run_id
            )
            SELECT EXISTS(
                SELECT 1 FROM lineage WHERE run_id = claim_row.reconciliation_run_id
            ) INTO lineage_matches;

            IF claim_row.order_intent_id <> NEW.order_intent_id
                OR claim_row.execution_mode <> 'SHADOW'
                OR claim_row.external_send_permitted
                OR claim_row.live_gate_status <> 'DISABLED'
                OR claim_row.claim_hash <> NEW.dispatch_claim_hash THEN
                RAISE EXCEPTION 'execution fact claim binding is invalid';
            END IF;
            IF run_row.organization_id <> claim_row.organization_id
                OR run_row.scope_id <> claim_row.scope_id
                OR run_row.fencing_token < claim_row.fencing_token
                OR (run_row.fencing_token = claim_row.fencing_token
                    AND run_row.lease_id <> claim_row.lease_id)
                OR run_row.environment <> 'SHADOW'
                OR run_row.live_dispatch_eligible
                OR run_row.run_hash <> NEW.reconciliation_run_hash
                OR run_row.started_at <= claim_row.claimed_at
                OR run_state.status <> 'RUNNING'
                OR run_state.phase NOT IN ('COMPARING', 'ADJUSTING')
                OR latest_run_id <> run_row.run_id
                OR NOT lineage_matches THEN
                RAISE EXCEPTION 'execution fact reconciliation run binding is invalid';
            END IF;
            IF NEW.recorded_at >= run_row.deadline_at THEN
                RAISE EXCEPTION 'execution fact reconciliation run deadline elapsed';
            END IF;
            IF input_row.run_id <> run_row.run_id
                OR input_row.organization_id <> claim_row.organization_id
                OR input_row.source_type <> NEW.reconciliation_source_type
                OR input_row.collection_status <> 'COMPLETE'
                OR input_row.source_version <> NEW.source_version
                OR input_row.input_hash <> NEW.reconciliation_input_hash
                OR NEW.event_time < input_row.observed_from
                OR NEW.event_time > input_row.observed_through
                OR NEW.event_time < claim_row.claimed_at
                OR input_row.received_at > NEW.recorded_at THEN
                RAISE EXCEPTION 'execution fact reconciliation input binding is invalid';
            END IF;
            IF sender_state.status <> 'LEASED'
                OR sender_state.active_lease_id <> run_row.lease_id
                OR sender_state.current_fencing_token <> run_row.fencing_token
                OR sender_state.lease_expires_at IS NULL
                OR NEW.recorded_at >= sender_state.lease_expires_at THEN
                RAISE EXCEPTION 'execution fact sender lease is stale or fenced';
            END IF;
            IF intent_row.venue <> NEW.venue
                OR intent_row.execution_domain <> NEW.execution_domain
                OR intent_row.account_id <> NEW.account_id THEN
                RAISE EXCEPTION 'execution fact route changed';
            END IF;

            IF NOT (
                (NEW.target_status = 'DISPATCHING'
                    AND NEW.fact_kind = 'WORKER_RECEIPT'
                    AND NEW.reconciliation_source_type = 'WORKER_LOCAL')
                OR (NEW.target_status IN (
                        'VENUE_ACKNOWLEDGED', 'CANCEL_PENDING',
                        'CANCELLED_ZERO_FILL', 'REJECTED_ZERO_FILL'
                    )
                    AND NEW.fact_kind = 'VENUE_ORDER'
                    AND NEW.reconciliation_source_type = 'VENUE_ORDERS')
                OR (NEW.target_status IN ('PARTIALLY_FILLED', 'FILLED')
                    AND NEW.fact_kind = 'VENUE_FILL'
                    AND NEW.reconciliation_source_type = 'VENUE_FILLS')
                {source_matrix}
                OR (NEW.target_status = 'RESULT_UNKNOWN' AND (
                    (NEW.fact_kind = 'WORKER_RECEIPT'
                        AND NEW.reconciliation_source_type = 'WORKER_LOCAL')
                    OR (NEW.fact_kind = 'VENUE_ORDER'
                        AND NEW.reconciliation_source_type = 'VENUE_ORDERS')
                ))
                OR (NEW.target_status = 'POSITION_RECONCILED'
                    AND NEW.fact_kind = 'VENUE_POSITION'
                    AND NEW.reconciliation_source_type = 'VENUE_POSITIONS')
                OR (NEW.target_status IN ('PROTECTION_CONFIRMED', 'COMPLETED')
                    AND NEW.fact_kind = 'VENUE_PROTECTION'
                    AND NEW.reconciliation_source_type = 'VENUE_PROTECTION')
                OR (NEW.target_status = 'FAILED_SAFE' AND (
                    (NEW.fact_kind = 'WORKER_RECEIPT'
                        AND NEW.reconciliation_source_type = 'WORKER_LOCAL')
                    {failed_safe_order}
                    OR (NEW.fact_kind = 'VENUE_POSITION'
                        AND NEW.reconciliation_source_type = 'VENUE_POSITIONS')
                    OR (NEW.fact_kind = 'VENUE_PROTECTION'
                        AND NEW.reconciliation_source_type = 'VENUE_PROTECTION')
                ))
            ) THEN
                RAISE EXCEPTION 'execution fact source cannot prove target status';
            END IF;
            {canonical_checks}
            RETURN NEW;
        EXCEPTION
            WHEN NO_DATA_FOUND THEN
                RAISE EXCEPTION 'execution fact binding reference is unavailable';
        END;
        $$
    """


def _create_execution_fact_application_guard() -> None:
    op.execute(
        """
        CREATE FUNCTION verify_execution_fact_application()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE state_row order_intent_states%ROWTYPE;
        DECLARE reservation_row risk_reservations%ROWTYPE;
        DECLARE exposure_row risk_exposure_states%ROWTYPE;
        DECLARE expected_reserved numeric;
        DECLARE expected_open numeric;
        DECLARE expected_unknown numeric;
        DECLARE expected_released numeric;
        BEGIN
            IF NEW.fact_contract_version <> 3 THEN
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
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER execution_facts_application_guard
        AFTER INSERT ON execution_facts
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION verify_execution_fact_application()
        """
    )
