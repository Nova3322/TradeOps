"""Bind execution facts to claims and active reconciliation inputs.

Revision ID: 20260718_0010
Revises: 20260718_0009
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260718_0010"
down_revision: str | Sequence[str] | None = "20260718_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "execution_facts",
        sa.Column(
            "fact_contract_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.alter_column("execution_facts", "fact_contract_version", server_default=None)
    op.add_column("execution_facts", sa.Column("fact_kind", sa.String(length=32), nullable=True))
    op.add_column(
        "execution_facts", sa.Column("shadow_dispatch_claim_id", sa.Uuid(), nullable=True)
    )
    op.add_column("execution_facts", sa.Column("reconciliation_run_id", sa.Uuid(), nullable=True))
    op.add_column("execution_facts", sa.Column("reconciliation_input_id", sa.Uuid(), nullable=True))
    op.add_column(
        "execution_facts",
        sa.Column("reconciliation_source_type", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "execution_facts",
        sa.Column("reconciliation_run_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "execution_facts",
        sa.Column("reconciliation_input_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "execution_facts",
        sa.Column("dispatch_claim_hash", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_execution_facts_contract_version",
        "execution_facts",
        "fact_contract_version IN (1, 2)",
    )
    op.create_check_constraint(
        "ck_execution_facts_reconciled_binding",
        "execution_facts",
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
        "AND length(dispatch_claim_hash) = 64 "
        "AND reconciliation_run_ref IS NULL)",
    )
    op.create_foreign_key(
        "fk_execution_facts_shadow_claim",
        "execution_facts",
        "shadow_dispatch_claims",
        ["shadow_dispatch_claim_id"],
        ["claim_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_execution_facts_reconciliation_run",
        "execution_facts",
        "execution_reconciliation_runs",
        ["reconciliation_run_id"],
        ["run_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_execution_facts_reconciliation_input",
        "execution_facts",
        "execution_reconciliation_inputs",
        ["reconciliation_input_id"],
        ["input_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_execution_facts_reconciliation_binding",
        "execution_facts",
        ["reconciliation_run_id", "reconciliation_input_id"],
    )
    _create_execution_fact_binding_guard()


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS execution_facts_reconciled_insert_guard ON execution_facts")
    op.execute("DROP FUNCTION IF EXISTS protect_reconciled_execution_fact_insert()")
    op.drop_index("ix_execution_facts_reconciliation_binding", table_name="execution_facts")
    op.drop_constraint(
        "fk_execution_facts_reconciliation_input", "execution_facts", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_execution_facts_reconciliation_run", "execution_facts", type_="foreignkey"
    )
    op.drop_constraint("fk_execution_facts_shadow_claim", "execution_facts", type_="foreignkey")
    op.drop_constraint("ck_execution_facts_reconciled_binding", "execution_facts", type_="check")
    op.drop_constraint("ck_execution_facts_contract_version", "execution_facts", type_="check")
    for column_name in (
        "dispatch_claim_hash",
        "reconciliation_input_hash",
        "reconciliation_run_hash",
        "reconciliation_source_type",
        "reconciliation_input_id",
        "reconciliation_run_id",
        "shadow_dispatch_claim_id",
        "fact_kind",
        "fact_contract_version",
    ):
        op.drop_column("execution_facts", column_name)


def _create_execution_fact_binding_guard() -> None:
    op.execute(
        """
        CREATE FUNCTION protect_reconciled_execution_fact_insert()
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
                OR run_row.lease_id <> claim_row.lease_id
                OR run_row.fencing_token <> claim_row.fencing_token
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
                OR sender_state.active_lease_id <> claim_row.lease_id
                OR sender_state.current_fencing_token <> claim_row.fencing_token
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
    )
    op.execute(
        """
        CREATE TRIGGER execution_facts_reconciled_insert_guard
        BEFORE INSERT ON execution_facts
        FOR EACH ROW EXECUTE FUNCTION protect_reconciled_execution_fact_insert()
        """
    )
