"""Allow current successor leases to reconcile historical shadow claims.

Revision ID: 20260718_0011
Revises: 20260718_0010
Create Date: 2026-07-18
"""

# ruff: noqa: S608 - this migration interpolates only fixed SQL guard fragments.

from collections.abc import Sequence

from alembic import op

revision: str = "20260718_0011"
down_revision: str | Sequence[str] | None = "20260718_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(_execution_fact_guard_sql(allow_successor_lease=True))


def downgrade() -> None:
    op.execute(_execution_fact_guard_sql(allow_successor_lease=False))


def _execution_fact_guard_sql(*, allow_successor_lease: bool) -> str:
    if allow_successor_lease:
        run_lease_checks = """
                OR run_row.fencing_token < claim_row.fencing_token
                OR (run_row.fencing_token = claim_row.fencing_token
                    AND run_row.lease_id <> claim_row.lease_id)
        """
        sender_lease_checks = """
                OR sender_state.active_lease_id <> run_row.lease_id
                OR sender_state.current_fencing_token <> run_row.fencing_token
        """
    else:
        run_lease_checks = """
                OR run_row.lease_id <> claim_row.lease_id
                OR run_row.fencing_token <> claim_row.fencing_token
        """
        sender_lease_checks = """
                OR sender_state.active_lease_id <> claim_row.lease_id
                OR sender_state.current_fencing_token <> claim_row.fencing_token
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
        BEGIN
            IF NEW.fact_contract_version <> 2
                OR NEW.fact_kind IS NULL
                OR NEW.shadow_dispatch_claim_id IS NULL
                OR NEW.reconciliation_run_id IS NULL
                OR NEW.reconciliation_input_id IS NULL
                OR NEW.reconciliation_source_type IS NULL THEN
                RAISE EXCEPTION 'new execution facts require the reconciled v2 contract';
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
                {run_lease_checks}
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
                {sender_lease_checks}
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
                OR (NEW.target_status IN ('PARTIALLY_FILLED', 'FILLED', 'CANCELLED_PARTIAL')
                    AND NEW.fact_kind = 'VENUE_FILL'
                    AND NEW.reconciliation_source_type = 'VENUE_FILLS')
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
                    OR (NEW.fact_kind = 'VENUE_ORDER'
                        AND NEW.reconciliation_source_type = 'VENUE_ORDERS')
                    OR (NEW.fact_kind = 'VENUE_POSITION'
                        AND NEW.reconciliation_source_type = 'VENUE_POSITIONS')
                    OR (NEW.fact_kind = 'VENUE_PROTECTION'
                        AND NEW.reconciliation_source_type = 'VENUE_PROTECTION')
                ))
            ) THEN
                RAISE EXCEPTION 'execution fact source cannot prove target status';
            END IF;
            RETURN NEW;
        EXCEPTION
            WHEN NO_DATA_FOUND THEN
                RAISE EXCEPTION 'execution fact binding reference is unavailable';
        END;
        $$
    """
