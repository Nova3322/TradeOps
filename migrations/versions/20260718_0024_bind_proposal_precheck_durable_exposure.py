"""Bind proposal prechecks to exact durable risk exposure snapshots.

Revision ID: 20260718_0024
Revises: 20260718_0023
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260718_0024"
down_revision: str | Sequence[str] | None = "20260718_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM risk_decision_snapshots) THEN
                RAISE EXCEPTION
                    'cannot require durable exposure binding '
                    'while legacy proposal risk decisions remain';
            END IF;
        END;
        $$
        """
    )
    op.add_column(
        "risk_decision_snapshots",
        sa.Column("durable_exposure_snapshot_hash", sa.String(length=64), nullable=False),
    )
    op.drop_constraint(
        "ck_risk_decisions_capital_binding_integrity",
        "risk_decision_snapshots",
        type_="check",
    )
    op.create_check_constraint(
        "ck_risk_decisions_capital_binding_integrity",
        "risk_decision_snapshots",
        "length(capital_scope_manifest_hash) = 64 "
        "AND length(capital_projection_hash) = 64 "
        "AND length(durable_exposure_snapshot_hash) = 64 "
        "AND capital_projection_version ~ '^portfolio-mtm-v[0-9]+$'",
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM risk_decision_snapshots) THEN
                RAISE EXCEPTION
                    'cannot remove durable exposure binding while proposal risk decisions remain';
            END IF;
        END;
        $$
        """
    )
    op.drop_constraint(
        "ck_risk_decisions_capital_binding_integrity",
        "risk_decision_snapshots",
        type_="check",
    )
    op.create_check_constraint(
        "ck_risk_decisions_capital_binding_integrity",
        "risk_decision_snapshots",
        "length(capital_scope_manifest_hash) = 64 "
        "AND length(capital_projection_hash) = 64 "
        "AND capital_projection_version ~ '^portfolio-mtm-v[0-9]+$'",
    )
    op.drop_column("risk_decision_snapshots", "durable_exposure_snapshot_hash")
