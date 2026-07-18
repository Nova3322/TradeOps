"""Add immutable Campaign target-position facts.

Revision ID: 20260718_0036
Revises: 20260718_0035
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0036"
down_revision: str | Sequence[str] | None = "20260718_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "campaign_target_position_facts",
        sa.Column("campaign_target_position_fact_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("target_version", sa.Integer(), nullable=False),
        sa.Column("current_position_binding_hash", sa.String(length=64), nullable=False),
        sa.Column("current_position_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("current_position_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("current_quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("target_quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("reduction_quantity", sa.Numeric(38, 18), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("requires_order", sa.Boolean(), nullable=False),
        sa.Column("reduce_only_required", sa.Boolean(), nullable=False),
        sa.Column("urgency", sa.String(length=20), nullable=False),
        sa.Column(
            "selected_target_source_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "selected_urgency_source_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "target_reason_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "urgency_reason_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "all_reason_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "input_candidate_hashes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "decision_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("decision_facts_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_version", sa.String(length=80), nullable=False),
        sa.Column("decision_hash", sa.String(length=64), nullable=False),
        sa.Column("target_semantic_hash", sa.String(length=64), nullable=False),
        sa.Column("record_version", sa.String(length=80), nullable=False),
        sa.Column("record_hash", sa.String(length=64), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("live_order_eligible", sa.Boolean(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "target_version > 0",
            name="ck_campaign_target_facts_version",
        ),
        sa.CheckConstraint(
            "current_quantity > 0 AND target_quantity >= 0 "
            "AND target_quantity <= current_quantity "
            "AND reduction_quantity = current_quantity - target_quantity",
            name="ck_campaign_target_facts_quantities",
        ),
        sa.CheckConstraint(
            "(action = 'HOLD' AND target_quantity = current_quantity "
            "AND reduction_quantity = 0 AND requires_order = false AND urgency = 'NONE') OR "
            "(action = 'REDUCE' AND target_quantity > 0 "
            "AND target_quantity < current_quantity AND reduction_quantity > 0 "
            "AND requires_order = true AND urgency IN ('ORDERLY', 'URGENT', 'IMMEDIATE')) OR "
            "(action = 'EXIT' AND target_quantity = 0 AND reduction_quantity > 0 "
            "AND requires_order = true AND urgency IN ('ORDERLY', 'URGENT', 'IMMEDIATE'))",
            name="ck_campaign_target_facts_action",
        ),
        sa.CheckConstraint(
            "reduce_only_required = true",
            name="ck_campaign_target_facts_reduce_only",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(selected_target_source_refs) = 'array' "
            "AND jsonb_typeof(selected_urgency_source_refs) = 'array' "
            "AND jsonb_typeof(target_reason_codes) = 'array' "
            "AND jsonb_typeof(urgency_reason_codes) = 'array' "
            "AND jsonb_typeof(all_reason_codes) = 'array' "
            "AND jsonb_typeof(input_candidate_hashes) = 'array' "
            "AND jsonb_typeof(decision_payload) = 'object'",
            name="ck_campaign_target_facts_arrays",
        ),
        sa.CheckConstraint(
            "decision_facts_as_of <= decision_evaluated_at "
            "AND decision_evaluated_at <= recorded_at",
            name="ck_campaign_target_facts_time_order",
        ),
        sa.CheckConstraint(
            "decision_version = 'target-position-decision-v1' "
            "AND record_version = 'campaign-target-position-fact-v1'",
            name="ck_campaign_target_facts_contract_versions",
        ),
        sa.CheckConstraint(
            "environment = 'SHADOW' AND live_order_eligible = false",
            name="ck_campaign_target_facts_shadow_only",
        ),
        sa.CheckConstraint(
            "length(current_position_binding_hash) = 64 "
            "AND length(current_position_snapshot_hash) = 64 "
            "AND length(decision_hash) = 64 "
            "AND length(target_semantic_hash) = 64 "
            "AND length(record_hash) = 64",
            name="ck_campaign_target_facts_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.campaign_id"],
            name="fk_campaign_target_facts_campaign",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["current_position_snapshot_id"],
            ["venue_position_snapshots.venue_position_snapshot_id"],
            name="fk_campaign_target_facts_position_snapshot",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("campaign_target_position_fact_id"),
        sa.UniqueConstraint(
            "campaign_id",
            "target_version",
            name="uq_campaign_target_facts_campaign_version",
        ),
        sa.UniqueConstraint(
            "campaign_id",
            "target_semantic_hash",
            name="uq_campaign_target_facts_campaign_semantic",
        ),
    )
    op.create_index(
        "ix_campaign_target_facts_latest",
        "campaign_target_position_facts",
        ["campaign_id", "target_version"],
    )
    op.execute(
        """
        CREATE FUNCTION protect_campaign_target_position_fact_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            campaign_row campaigns%ROWTYPE;
            position_row venue_position_snapshots%ROWTYPE;
            previous_version integer;
            previous_target numeric;
        BEGIN
            SELECT * INTO STRICT campaign_row
            FROM campaigns
            WHERE campaign_id = NEW.campaign_id;
            SELECT * INTO STRICT position_row
            FROM venue_position_snapshots
            WHERE venue_position_snapshot_id = NEW.current_position_snapshot_id;
            SELECT target_version, target_quantity
            INTO previous_version, previous_target
            FROM campaign_target_position_facts
            WHERE campaign_id = NEW.campaign_id
            ORDER BY target_version DESC
            LIMIT 1;

            IF NEW.target_version <> COALESCE(previous_version, 0) + 1 THEN
                RAISE EXCEPTION 'campaign target version is not contiguous';
            END IF;
            IF previous_target IS NOT NULL AND NEW.target_quantity > previous_target THEN
                RAISE EXCEPTION 'campaign target position cannot be relaxed';
            END IF;
            IF NEW.organization_id <> campaign_row.organization_id
               OR position_row.organization_id <> campaign_row.organization_id
               OR position_row.venue <> campaign_row.venue
               OR position_row.execution_domain <> campaign_row.execution_domain
               OR position_row.account_id <> campaign_row.account_id
               OR position_row.instrument_id <> campaign_row.instrument_id
               OR position_row.direction <> campaign_row.direction
               OR position_row.position_state <> 'OPEN'
               OR position_row.quantity <> NEW.current_quantity
               OR position_row.snapshot_hash <> NEW.current_position_snapshot_hash
               OR NEW.decision_facts_as_of < position_row.event_time
               OR NEW.decision_payload->>'campaign_id' IS DISTINCT FROM NEW.campaign_id::text
               OR NEW.decision_payload->>'current_position_binding_hash'
                    IS DISTINCT FROM NEW.current_position_binding_hash
               OR (NEW.decision_payload->>'current_quantity')::numeric
                    IS DISTINCT FROM NEW.current_quantity
               OR (NEW.decision_payload->>'target_quantity')::numeric
                    IS DISTINCT FROM NEW.target_quantity
               OR (NEW.decision_payload->>'reduction_quantity')::numeric
                    IS DISTINCT FROM NEW.reduction_quantity
               OR NEW.decision_payload->>'action' IS DISTINCT FROM NEW.action
               OR (NEW.decision_payload->>'requires_order')::boolean
                    IS DISTINCT FROM NEW.requires_order
               OR (NEW.decision_payload->>'reduce_only_required')::boolean
                    IS DISTINCT FROM NEW.reduce_only_required
               OR NEW.decision_payload->>'urgency' IS DISTINCT FROM NEW.urgency
               OR NEW.decision_payload->'selected_target_source_refs'
                    IS DISTINCT FROM NEW.selected_target_source_refs
               OR NEW.decision_payload->'selected_urgency_source_refs'
                    IS DISTINCT FROM NEW.selected_urgency_source_refs
               OR NEW.decision_payload->'target_reason_codes'
                    IS DISTINCT FROM NEW.target_reason_codes
               OR NEW.decision_payload->'urgency_reason_codes'
                    IS DISTINCT FROM NEW.urgency_reason_codes
               OR NEW.decision_payload->'all_reason_codes'
                    IS DISTINCT FROM NEW.all_reason_codes
               OR NEW.decision_payload->'input_candidate_hashes'
                    IS DISTINCT FROM NEW.input_candidate_hashes
               OR (NEW.decision_payload->>'facts_as_of')::timestamptz
                    IS DISTINCT FROM NEW.decision_facts_as_of
               OR (NEW.decision_payload->>'evaluated_at')::timestamptz
                    IS DISTINCT FROM NEW.decision_evaluated_at
               OR NEW.decision_payload->>'decision_version'
                    IS DISTINCT FROM NEW.decision_version
               OR NEW.decision_payload->>'decision_hash' IS DISTINCT FROM NEW.decision_hash THEN
                RAISE EXCEPTION 'campaign target position source binding mismatch';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER campaign_target_position_facts_insert_guard
        BEFORE INSERT ON campaign_target_position_facts
        FOR EACH ROW EXECUTE FUNCTION protect_campaign_target_position_fact_insert()
        """
    )
    op.execute(
        """
        CREATE FUNCTION deny_campaign_target_position_fact_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'campaign_target_position_facts is immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER campaign_target_position_facts_immutable
        BEFORE UPDATE OR DELETE ON campaign_target_position_facts
        FOR EACH ROW EXECUTE FUNCTION deny_campaign_target_position_fact_change()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM campaign_target_position_facts) THEN
                RAISE EXCEPTION 'cannot downgrade while Campaign target facts exist';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS campaign_target_position_facts_immutable "
        "ON campaign_target_position_facts"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS campaign_target_position_facts_insert_guard "
        "ON campaign_target_position_facts"
    )
    op.execute("DROP FUNCTION IF EXISTS deny_campaign_target_position_fact_change()")
    op.execute("DROP FUNCTION IF EXISTS protect_campaign_target_position_fact_insert()")
    op.drop_index(
        "ix_campaign_target_facts_latest",
        table_name="campaign_target_position_facts",
    )
    op.drop_table("campaign_target_position_facts")
