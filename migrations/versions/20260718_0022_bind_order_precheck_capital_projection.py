"""Bind final order prechecks to exact managed-capital projections.

Revision ID: 20260718_0022
Revises: 20260718_0021
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260718_0022"
down_revision: str | Sequence[str] | None = "20260718_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM execution_risk_decisions) THEN
                RAISE EXCEPTION
                    'cannot require canonical capital binding '
                    'while legacy execution decisions remain';
            END IF;
        END;
        $$
        """
    )
    op.add_column(
        "execution_risk_decisions",
        sa.Column("capital_scope_manifest_id", sa.Uuid(), nullable=False),
    )
    op.add_column(
        "execution_risk_decisions",
        sa.Column("capital_scope_manifest_version", sa.Integer(), nullable=False),
    )
    op.add_column(
        "execution_risk_decisions",
        sa.Column("capital_scope_manifest_hash", sa.String(length=64), nullable=False),
    )
    op.add_column(
        "execution_risk_decisions",
        sa.Column("capital_projection_version", sa.String(length=40), nullable=False),
    )
    op.add_column(
        "execution_risk_decisions",
        sa.Column("capital_projection_hash", sa.String(length=64), nullable=False),
    )
    op.create_check_constraint(
        "ck_exec_risk_capital_binding_integrity",
        "execution_risk_decisions",
        "length(capital_scope_manifest_hash) = 64 "
        "AND length(capital_projection_hash) = 64 "
        "AND capital_projection_version ~ '^portfolio-mtm-v[0-9]+$'",
    )
    op.create_foreign_key(
        "fk_exec_risk_capital_scope_manifest",
        "execution_risk_decisions",
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
            IF EXISTS (SELECT 1 FROM execution_risk_decisions) THEN
                RAISE EXCEPTION
                    'cannot remove canonical capital binding while execution decisions remain';
            END IF;
        END;
        $$
        """
    )
    op.drop_constraint(
        "fk_exec_risk_capital_scope_manifest",
        "execution_risk_decisions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_exec_risk_capital_binding_integrity",
        "execution_risk_decisions",
        type_="check",
    )
    op.drop_column("execution_risk_decisions", "capital_projection_hash")
    op.drop_column("execution_risk_decisions", "capital_projection_version")
    op.drop_column("execution_risk_decisions", "capital_scope_manifest_hash")
    op.drop_column("execution_risk_decisions", "capital_scope_manifest_version")
    op.drop_column("execution_risk_decisions", "capital_scope_manifest_id")
