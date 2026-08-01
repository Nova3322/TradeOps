"""add reviewed risk policy restoration

Revision ID: 20260801_0005
Revises: 20260731_0004
Create Date: 2026-08-01 10:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260801_0005"
down_revision: str | None = "20260731_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "risk_policies",
        sa.Column("reason", sa.Text(), server_default="legacy risk policy", nullable=False),
    )
    op.add_column(
        "risk_policies",
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_check_constraint("ck_risk_policies_revision", "risk_policies", "revision >= 1")
    op.add_column(
        "capability_gates",
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_check_constraint("ck_capability_gates_version", "capability_gates", "version >= 1")
    op.create_table(
        "risk_control_change_requests",
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("requester_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("restore_auto_add", sa.Boolean(), nullable=False),
        sa.Column("require_live_scope", sa.Boolean(), nullable=False),
        sa.Column("source_policy_id", sa.Uuid(), nullable=False),
        sa.Column("source_policy_version", sa.String(length=120), nullable=False),
        sa.Column("source_policy_revision", sa.Integer(), nullable=False),
        sa.Column("source_auto_add_status", sa.String(length=16), nullable=False),
        sa.Column("source_auto_add_version", sa.Integer(), nullable=False),
        sa.Column(
            "required_scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("resulting_policy_id", sa.Uuid(), nullable=True),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("execute_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING_REVIEW','APPROVED','REJECTED','EXPIRED','EXECUTED')",
            name="ck_risk_control_change_requests_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_risk_control_change_requests_version"),
        sa.CheckConstraint(
            "source_policy_revision >= 1 AND source_auto_add_version >= 1",
            name="ck_risk_control_change_requests_source_versions",
        ),
        sa.CheckConstraint(
            "source_auto_add_status IN ('DISABLED','ENABLED')",
            name="ck_risk_control_change_requests_auto_add_status",
        ),
        sa.CheckConstraint(
            "execute_after >= created_at AND expires_at > execute_after",
            name="ck_risk_control_change_requests_window",
        ),
        sa.ForeignKeyConstraint(["requester_id"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["source_policy_id"], ["risk_policies.policy_id"]),
        sa.ForeignKeyConstraint(["resulting_policy_id"], ["risk_policies.policy_id"]),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_index(
        "uq_risk_control_change_requests_pending",
        "risk_control_change_requests",
        [sa.text("(true)")],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING_REVIEW','APPROVED')"),
    )
    op.create_index(
        "ix_risk_control_change_requests_created",
        "risk_control_change_requests",
        ["created_at"],
        unique=False,
    )
    op.add_column(
        "approvals",
        sa.Column("risk_control_change_request_id", sa.Uuid(), nullable=True),
    )
    op.drop_constraint("ck_approvals_one_parent", "approvals", type_="check")
    op.create_foreign_key(
        "fk_approvals_risk_control_change_request",
        "approvals",
        "risk_control_change_requests",
        ["risk_control_change_request_id"],
        ["request_id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_approvals_one_parent",
        "approvals",
        "((proposal_id IS NOT NULL)::integer + "
        "(transfer_proposal_id IS NOT NULL)::integer + "
        "(risk_control_change_request_id IS NOT NULL)::integer) = 1",
    )
    op.create_index(
        "uq_approvals_risk_control_reviewer",
        "approvals",
        ["risk_control_change_request_id", "reviewer_id"],
        unique=True,
        postgresql_where=sa.text("risk_control_change_request_id IS NOT NULL"),
    )
    op.add_column(
        "trading_authorizations",
        sa.Column("add_revoked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM approvals
                WHERE risk_control_change_request_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade while reviewed risk-control audit records exist';
            END IF;
        END $$
        """
    )
    op.drop_column("trading_authorizations", "add_revoked_at")
    op.drop_index("uq_approvals_risk_control_reviewer", table_name="approvals")
    op.drop_constraint("ck_approvals_one_parent", "approvals", type_="check")
    op.drop_constraint("fk_approvals_risk_control_change_request", "approvals", type_="foreignkey")
    op.drop_column("approvals", "risk_control_change_request_id")
    op.create_check_constraint(
        "ck_approvals_one_parent",
        "approvals",
        "(proposal_id IS NOT NULL) <> (transfer_proposal_id IS NOT NULL)",
    )
    op.drop_index(
        "ix_risk_control_change_requests_created", table_name="risk_control_change_requests"
    )
    op.drop_index(
        "uq_risk_control_change_requests_pending", table_name="risk_control_change_requests"
    )
    op.drop_table("risk_control_change_requests")
    op.drop_constraint("ck_capability_gates_version", "capability_gates", type_="check")
    op.drop_column("capability_gates", "version")
    op.drop_constraint("ck_risk_policies_revision", "risk_policies", type_="check")
    op.drop_column("risk_policies", "revision")
    op.drop_column("risk_policies", "reason")
