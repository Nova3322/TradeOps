"""Bind INITIAL order precheck to a canonical flat position snapshot.

Revision ID: 20260718_0032
Revises: 20260718_0031
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260718_0032"
down_revision: str | Sequence[str] | None = "20260718_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "execution_risk_decisions",
        sa.Column("initial_flat_position_snapshot_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "execution_risk_decisions",
        sa.Column("initial_flat_position_snapshot_hash", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_exec_risk_initial_flat_position_binding",
        "execution_risk_decisions",
        "(initial_flat_position_snapshot_id IS NULL "
        "AND initial_flat_position_snapshot_hash IS NULL) OR "
        "(intent_kind = 'INITIAL' AND initial_flat_position_snapshot_id IS NOT NULL "
        "AND length(initial_flat_position_snapshot_hash) = 64)",
    )
    op.create_foreign_key(
        "fk_exec_risk_initial_flat_position_snapshot",
        "execution_risk_decisions",
        "venue_position_snapshots",
        ["initial_flat_position_snapshot_id"],
        ["venue_position_snapshot_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM execution_risk_decisions
                WHERE initial_flat_position_snapshot_id IS NOT NULL
                   OR initial_flat_position_snapshot_hash IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade initial flat-position binding while evidence remains';
            END IF;
        END;
        $$
        """
    )
    op.drop_constraint(
        "fk_exec_risk_initial_flat_position_snapshot",
        "execution_risk_decisions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_exec_risk_initial_flat_position_binding",
        "execution_risk_decisions",
        type_="check",
    )
    op.drop_column("execution_risk_decisions", "initial_flat_position_snapshot_hash")
    op.drop_column("execution_risk_decisions", "initial_flat_position_snapshot_id")
