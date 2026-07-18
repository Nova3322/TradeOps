"""Add immutable non-dispatchable Campaign reduction-plan snapshots.

Revision ID: 20260718_0037
Revises: 20260718_0036
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0037"
down_revision: str | Sequence[str] | None = "20260718_0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "campaign_reduction_plan_snapshots",
        sa.Column("campaign_reduction_plan_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.String(length=120), nullable=False),
        sa.Column("campaign_target_position_fact_id", sa.Uuid(), nullable=False),
        sa.Column("target_version", sa.Integer(), nullable=False),
        sa.Column("target_semantic_hash", sa.String(length=64), nullable=False),
        sa.Column("current_position_binding_hash", sa.String(length=64), nullable=False),
        sa.Column("plan_idempotency_ref", sa.String(length=255), nullable=False),
        sa.Column(
            "plan_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("record_version", sa.String(length=80), nullable=False),
        sa.Column("record_hash", sa.String(length=64), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("dispatch_eligible", sa.Boolean(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "target_version > 0",
            name="ck_campaign_reduction_plans_version",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(plan_payload) = 'object'",
            name="ck_campaign_reduction_plans_payload",
        ),
        sa.CheckConstraint(
            "record_version = 'campaign-reduction-plan-snapshot-v1'",
            name="ck_campaign_reduction_plans_record_version",
        ),
        sa.CheckConstraint(
            "environment = 'SHADOW' AND dispatch_eligible = false",
            name="ck_campaign_reduction_plans_shadow_only",
        ),
        sa.CheckConstraint(
            "length(target_semantic_hash) = 64 "
            "AND length(current_position_binding_hash) = 64 "
            "AND length(plan_hash) = 64 "
            "AND length(record_hash) = 64",
            name="ck_campaign_reduction_plans_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.campaign_id"],
            name="fk_campaign_reduction_plans_campaign",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_target_position_fact_id"],
            ["campaign_target_position_facts.campaign_target_position_fact_id"],
            name="fk_campaign_reduction_plans_target_fact",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("campaign_reduction_plan_snapshot_id"),
        sa.UniqueConstraint(
            "campaign_target_position_fact_id",
            name="uq_campaign_reduction_plans_target_fact",
        ),
        sa.UniqueConstraint(
            "campaign_id",
            "plan_idempotency_ref",
            name="uq_campaign_reduction_plans_idempotency",
        ),
    )
    op.create_index(
        "ix_campaign_reduction_plans_campaign_target",
        "campaign_reduction_plan_snapshots",
        ["campaign_id", "target_version"],
    )
    op.execute(
        """
        CREATE FUNCTION protect_campaign_reduction_plan_snapshot_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            campaign_row campaigns%ROWTYPE;
            target_row campaign_target_position_facts%ROWTYPE;
            position_row venue_position_snapshots%ROWTYPE;
            latest_target_version integer;
        BEGIN
            SELECT * INTO STRICT campaign_row
            FROM campaigns
            WHERE campaign_id = NEW.campaign_id;
            SELECT * INTO STRICT target_row
            FROM campaign_target_position_facts
            WHERE campaign_target_position_fact_id = NEW.campaign_target_position_fact_id;
            SELECT * INTO STRICT position_row
            FROM venue_position_snapshots
            WHERE venue_position_snapshot_id = target_row.current_position_snapshot_id;
            SELECT max(target_version) INTO latest_target_version
            FROM campaign_target_position_facts
            WHERE campaign_id = NEW.campaign_id;

            IF target_row.campaign_id <> NEW.campaign_id
               OR target_row.organization_id <> NEW.organization_id
               OR campaign_row.organization_id <> NEW.organization_id
               OR target_row.target_version <> NEW.target_version
               OR target_row.target_version <> latest_target_version
               OR target_row.target_semantic_hash <> NEW.target_semantic_hash
               OR target_row.current_position_binding_hash
                    <> NEW.current_position_binding_hash
               OR target_row.action NOT IN ('REDUCE', 'EXIT')
               OR target_row.requires_order = false
               OR target_row.reduce_only_required = false
               OR position_row.snapshot_hash <> target_row.current_position_snapshot_hash
               OR NEW.plan_payload->>'campaign_id' IS DISTINCT FROM NEW.campaign_id::text
               OR NEW.plan_payload->>'target_fact_id'
                    IS DISTINCT FROM NEW.campaign_target_position_fact_id::text
               OR (NEW.plan_payload->>'target_version')::integer
                    IS DISTINCT FROM NEW.target_version
               OR NEW.plan_payload->>'target_semantic_hash'
                    IS DISTINCT FROM NEW.target_semantic_hash
               OR NEW.plan_payload->>'current_position_binding_hash'
                    IS DISTINCT FROM NEW.current_position_binding_hash
               OR NEW.plan_payload->>'current_position_snapshot_id'
                    IS DISTINCT FROM target_row.current_position_snapshot_id::text
               OR NEW.plan_payload->>'current_position_snapshot_hash'
                    IS DISTINCT FROM target_row.current_position_snapshot_hash
               OR NEW.plan_payload->>'direction' IS DISTINCT FROM campaign_row.direction
               OR NEW.plan_payload->>'position_side' IS DISTINCT FROM position_row.position_side
               OR NEW.plan_payload->>'side' IS DISTINCT FROM
                    (CASE campaign_row.direction WHEN 'LONG' THEN 'SELL' ELSE 'BUY' END)
               OR (NEW.plan_payload->>'current_quantity')::numeric
                    IS DISTINCT FROM target_row.current_quantity
               OR (NEW.plan_payload->>'target_quantity')::numeric
                    IS DISTINCT FROM target_row.target_quantity
               OR (NEW.plan_payload->>'order_quantity')::numeric
                    IS DISTINCT FROM target_row.reduction_quantity
               OR NEW.plan_payload->>'action' IS DISTINCT FROM target_row.action
               OR NEW.plan_payload->>'urgency' IS DISTINCT FROM target_row.urgency
               OR NEW.plan_payload->'reason_codes' IS DISTINCT FROM target_row.all_reason_codes
               OR (NEW.plan_payload->>'reduce_only')::boolean IS DISTINCT FROM true
               OR NEW.plan_payload->>'plan_idempotency_ref'
                    IS DISTINCT FROM NEW.plan_idempotency_ref
               OR NEW.plan_payload->>'order_type_status' IS DISTINCT FROM 'UNAVAILABLE'
               OR NEW.plan_payload->>'venue_execution_terms_status'
                    IS DISTINCT FROM 'UNAVAILABLE'
               OR NEW.plan_payload->>'plan_version'
                    IS DISTINCT FROM 'campaign-reduction-execution-plan-v1'
               OR NEW.plan_payload->>'plan_hash' IS DISTINCT FROM NEW.plan_hash
               OR NEW.plan_payload->>'environment' IS DISTINCT FROM 'SHADOW'
               OR (NEW.plan_payload->>'live_order_eligible')::boolean IS DISTINCT FROM false
               OR (NEW.plan_payload->>'planned_at')::timestamptz > NEW.recorded_at
               OR (NEW.plan_payload->>'valid_until')::timestamptz
                    <= (NEW.plan_payload->>'planned_at')::timestamptz THEN
                RAISE EXCEPTION 'Campaign reduction plan source binding mismatch';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER campaign_reduction_plan_snapshots_insert_guard
        BEFORE INSERT ON campaign_reduction_plan_snapshots
        FOR EACH ROW EXECUTE FUNCTION protect_campaign_reduction_plan_snapshot_insert()
        """
    )
    op.execute(
        """
        CREATE FUNCTION deny_campaign_reduction_plan_snapshot_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'campaign_reduction_plan_snapshots is immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER campaign_reduction_plan_snapshots_immutable
        BEFORE UPDATE OR DELETE ON campaign_reduction_plan_snapshots
        FOR EACH ROW EXECUTE FUNCTION deny_campaign_reduction_plan_snapshot_change()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM campaign_reduction_plan_snapshots) THEN
                RAISE EXCEPTION 'cannot downgrade while Campaign reduction plans exist';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS campaign_reduction_plan_snapshots_immutable "
        "ON campaign_reduction_plan_snapshots"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS campaign_reduction_plan_snapshots_insert_guard "
        "ON campaign_reduction_plan_snapshots"
    )
    op.execute("DROP FUNCTION IF EXISTS deny_campaign_reduction_plan_snapshot_change()")
    op.execute("DROP FUNCTION IF EXISTS protect_campaign_reduction_plan_snapshot_insert()")
    op.drop_index(
        "ix_campaign_reduction_plans_campaign_target",
        table_name="campaign_reduction_plan_snapshots",
    )
    op.drop_table("campaign_reduction_plan_snapshots")
