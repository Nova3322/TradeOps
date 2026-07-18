"""Freeze independently derived loss components on risk reservations.

Revision ID: 20260718_0025
Revises: 20260718_0024
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260718_0025"
down_revision: str | Sequence[str] | None = "20260718_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM risk_reservations) THEN
                RAISE EXCEPTION
                    'cannot freeze independent loss components while legacy reservations remain';
            END IF;
        END;
        $$
        """
    )
    op.add_column(
        "risk_reservations",
        sa.Column("base_heat_reserved", sa.Numeric(38, 18), nullable=False),
    )
    op.add_column(
        "risk_reservations",
        sa.Column(
            "protected_profit_giveback_reserved",
            sa.Numeric(38, 18),
            nullable=False,
        ),
    )
    op.add_column(
        "risk_reservations",
        sa.Column("cost_stress_add_on_reserved", sa.Numeric(38, 18), nullable=False),
    )
    op.drop_constraint(
        "ck_risk_reservations_amounts",
        "risk_reservations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_risk_reservations_amounts",
        "risk_reservations",
        "reserved_quantity > 0 AND reserved_heat > 0 "
        "AND base_heat_reserved > 0 "
        "AND protected_profit_giveback_reserved >= 0 "
        "AND cost_stress_add_on_reserved >= 0 "
        "AND reserved_heat = base_heat_reserved "
        "+ protected_profit_giveback_reserved + cost_stress_add_on_reserved "
        "AND funding_reserved >= 0 AND margin_reserved >= 0",
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM risk_reservations) THEN
                RAISE EXCEPTION
                    'cannot remove independent loss components while reservations remain';
            END IF;
        END;
        $$
        """
    )
    op.drop_constraint(
        "ck_risk_reservations_amounts",
        "risk_reservations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_risk_reservations_amounts",
        "risk_reservations",
        "reserved_quantity > 0 AND reserved_heat > 0 "
        "AND funding_reserved >= 0 AND margin_reserved >= 0",
    )
    op.drop_column("risk_reservations", "cost_stress_add_on_reserved")
    op.drop_column("risk_reservations", "protected_profit_giveback_reserved")
    op.drop_column("risk_reservations", "base_heat_reserved")
