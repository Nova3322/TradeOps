"""Add the canonical worst active protection trigger price.

Revision ID: 20260718_0026
Revises: 20260718_0025
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260718_0026"
down_revision: str | Sequence[str] | None = "20260718_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_COVERAGE_CHECK = (
    "(protection_state = 'CONFIRMED' "
    "AND protected_direction IN ('LONG', 'SHORT') "
    "AND position_quantity > 0 AND covered_quantity = position_quantity "
    "AND uncovered_quantity = 0 AND active_stop_order_count >= 1 "
    "AND venue_native AND reduce_only_confirmed AND NOT replacement_in_progress) OR "
    "(protection_state = 'DEGRADED' "
    "AND protected_direction IN ('LONG', 'SHORT') "
    "AND position_quantity > 0 AND covered_quantity >= 0 "
    "AND uncovered_quantity >= 0 "
    "AND covered_quantity + uncovered_quantity = position_quantity "
    "AND active_stop_order_count >= 0 "
    "AND (uncovered_quantity > 0 OR active_stop_order_count = 0 "
    "OR NOT venue_native OR NOT reduce_only_confirmed OR replacement_in_progress)) OR "
    "(protection_state = 'UNKNOWN' AND protected_direction = 'UNKNOWN' "
    "AND position_quantity IS NULL AND covered_quantity IS NULL "
    "AND uncovered_quantity IS NULL AND active_stop_order_count IS NULL "
    "AND NOT venue_native AND NOT reduce_only_confirmed "
    "AND NOT replacement_in_progress)"
)

_NEW_COVERAGE_CHECK = (
    "(protection_state = 'CONFIRMED' "
    "AND protected_direction IN ('LONG', 'SHORT') "
    "AND position_quantity > 0 AND covered_quantity = position_quantity "
    "AND uncovered_quantity = 0 AND active_stop_order_count >= 1 "
    "AND worst_active_trigger_price IS NOT NULL "
    "AND worst_active_trigger_price > 0 "
    "AND venue_native AND reduce_only_confirmed AND NOT replacement_in_progress) OR "
    "(protection_state = 'DEGRADED' "
    "AND protected_direction IN ('LONG', 'SHORT') "
    "AND position_quantity > 0 AND covered_quantity >= 0 "
    "AND uncovered_quantity >= 0 "
    "AND covered_quantity + uncovered_quantity = position_quantity "
    "AND active_stop_order_count >= 0 "
    "AND (worst_active_trigger_price IS NULL OR worst_active_trigger_price > 0) "
    "AND (uncovered_quantity > 0 OR active_stop_order_count = 0 "
    "OR NOT venue_native OR NOT reduce_only_confirmed OR replacement_in_progress)) OR "
    "(protection_state = 'UNKNOWN' AND protected_direction = 'UNKNOWN' "
    "AND position_quantity IS NULL AND covered_quantity IS NULL "
    "AND uncovered_quantity IS NULL AND active_stop_order_count IS NULL "
    "AND worst_active_trigger_price IS NULL "
    "AND NOT venue_native AND NOT reduce_only_confirmed "
    "AND NOT replacement_in_progress)"
)


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM venue_protection_snapshots) THEN
                RAISE EXCEPTION
                    'cannot add canonical protection trigger prices '
                    'while legacy protection snapshots remain';
            END IF;
        END;
        $$
        """
    )
    op.add_column(
        "venue_protection_snapshots",
        sa.Column("worst_active_trigger_price", sa.Numeric(38, 18), nullable=True),
    )
    op.drop_constraint(
        "ck_venue_protection_snapshots_coverage",
        "venue_protection_snapshots",
        type_="check",
    )
    op.create_check_constraint(
        "ck_venue_protection_snapshots_coverage",
        "venue_protection_snapshots",
        _NEW_COVERAGE_CHECK,
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_venue_protection_trigger_price()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE position_mark numeric;
        DECLARE position_direction text;
        BEGIN
            SELECT mark_price, direction
            INTO STRICT position_mark, position_direction
            FROM venue_position_snapshots
            WHERE venue_position_snapshot_id = NEW.venue_position_snapshot_id;

            IF NEW.worst_active_trigger_price IS NOT NULL AND (
                position_mark IS NULL
                OR (position_direction = 'LONG'
                    AND NEW.worst_active_trigger_price >= position_mark)
                OR (position_direction = 'SHORT'
                    AND NEW.worst_active_trigger_price <= position_mark)
            ) THEN
                RAISE EXCEPTION
                    'canonical venue protection trigger price is invalid';
            END IF;
            RETURN NEW;
        EXCEPTION
            WHEN NO_DATA_FOUND THEN
                RAISE EXCEPTION
                    'canonical venue protection trigger position is unavailable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER venue_protection_snapshots_trigger_price_guard
        BEFORE INSERT ON venue_protection_snapshots
        FOR EACH ROW EXECUTE FUNCTION validate_venue_protection_trigger_price()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM venue_protection_snapshots) THEN
                RAISE EXCEPTION
                    'cannot remove canonical protection trigger prices '
                    'while protection snapshots remain';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS venue_protection_snapshots_trigger_price_guard "
        "ON venue_protection_snapshots"
    )
    op.execute("DROP FUNCTION IF EXISTS validate_venue_protection_trigger_price()")
    op.drop_constraint(
        "ck_venue_protection_snapshots_coverage",
        "venue_protection_snapshots",
        type_="check",
    )
    op.create_check_constraint(
        "ck_venue_protection_snapshots_coverage",
        "venue_protection_snapshots",
        _OLD_COVERAGE_CHECK,
    )
    op.drop_column("venue_protection_snapshots", "worst_active_trigger_price")
