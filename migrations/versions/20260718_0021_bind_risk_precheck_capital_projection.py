"""Bind proposal risk decisions to exact managed-capital projections.

Revision ID: 20260718_0021
Revises: 20260718_0020
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260718_0021"
down_revision: str | Sequence[str] | None = "20260718_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM risk_decision_snapshots) THEN
                RAISE EXCEPTION
                    'cannot require canonical capital binding while legacy risk decisions remain';
            END IF;
        END;
        $$
        """
    )
    op.add_column(
        "risk_decision_snapshots",
        sa.Column("capital_scope_manifest_id", sa.Uuid(), nullable=False),
    )
    op.add_column(
        "risk_decision_snapshots",
        sa.Column("capital_scope_manifest_version", sa.Integer(), nullable=False),
    )
    op.add_column(
        "risk_decision_snapshots",
        sa.Column("capital_scope_manifest_hash", sa.String(length=64), nullable=False),
    )
    op.add_column(
        "risk_decision_snapshots",
        sa.Column("capital_projection_version", sa.String(length=40), nullable=False),
    )
    op.add_column(
        "risk_decision_snapshots",
        sa.Column("capital_projection_hash", sa.String(length=64), nullable=False),
    )
    op.create_check_constraint(
        "ck_risk_decisions_capital_binding_integrity",
        "risk_decision_snapshots",
        "length(capital_scope_manifest_hash) = 64 "
        "AND length(capital_projection_hash) = 64 "
        "AND capital_projection_version ~ '^portfolio-mtm-v[0-9]+$'",
    )
    op.create_foreign_key(
        "fk_risk_decisions_capital_scope_manifest",
        "risk_decision_snapshots",
        "managed_capital_scope_manifests",
        [
            "capital_scope_manifest_id",
            "organization_id",
            "capital_scope_manifest_version",
        ],
        ["manifest_id", "organization_id", "manifest_version"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM risk_decision_snapshots) THEN
                RAISE EXCEPTION
                    'cannot remove canonical capital binding while risk decisions remain';
            END IF;
        END;
        $$
        """
    )
    op.drop_constraint(
        "fk_risk_decisions_capital_scope_manifest",
        "risk_decision_snapshots",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_risk_decisions_capital_binding_integrity",
        "risk_decision_snapshots",
        type_="check",
    )
    op.drop_column("risk_decision_snapshots", "capital_projection_hash")
    op.drop_column("risk_decision_snapshots", "capital_projection_version")
    op.drop_column("risk_decision_snapshots", "capital_scope_manifest_hash")
    op.drop_column("risk_decision_snapshots", "capital_scope_manifest_version")
    op.drop_column("risk_decision_snapshots", "capital_scope_manifest_id")
