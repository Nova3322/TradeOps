"""Add a rebuildable current venue protection projection.

Revision ID: 20260718_0027
Revises: 20260718_0026
Create Date: 2026-07-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260718_0027"
down_revision: str | Sequence[str] | None = "20260718_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PROTECTION_PROJECTION_SQL = """
CREATE VIEW venue_protection_current_projection AS
WITH enriched AS (
    SELECT
        protection.*,
        position.settlement_currency
    FROM venue_protection_snapshots AS protection
    JOIN venue_position_snapshots AS position
      ON position.venue_position_snapshot_id = protection.venue_position_snapshot_id
),
ranked AS (
    SELECT
        snapshot.*,
        count(*) OVER (
            PARTITION BY
                organization_id,
                venue,
                execution_domain,
                account_id,
                instrument_id,
                position_mode,
                position_side,
                margin_mode,
                collateral_pool_id,
                settlement_currency,
                event_time
        ) AS max_event_candidate_count,
        max(venue_observed_at) OVER (
            PARTITION BY
                organization_id,
                venue,
                execution_domain,
                account_id,
                instrument_id,
                position_mode,
                position_side,
                margin_mode,
                collateral_pool_id,
                settlement_currency,
                event_time
        ) AS max_venue_observed_at,
        max(first_received_at) OVER (
            PARTITION BY
                organization_id,
                venue,
                execution_domain,
                account_id,
                instrument_id,
                position_mode,
                position_side,
                margin_mode,
                collateral_pool_id,
                settlement_currency,
                event_time
        ) AS max_received_at,
        row_number() OVER (
            PARTITION BY
                organization_id,
                venue,
                execution_domain,
                account_id,
                instrument_id,
                position_mode,
                position_side,
                margin_mode,
                collateral_pool_id,
                settlement_currency
            ORDER BY
                event_time DESC,
                venue_observed_at DESC,
                first_received_at DESC,
                recorded_at DESC,
                venue_protection_snapshot_id DESC
        ) AS scope_rank
    FROM enriched AS snapshot
),
candidate AS (
    SELECT
        ranked.*,
        current_position.projection_state AS current_position_projection_state,
        current_position.source_snapshot_id AS current_position_source_snapshot_id
    FROM ranked
    LEFT JOIN venue_position_current_projection AS current_position
      ON current_position.organization_id = ranked.organization_id
     AND current_position.venue = ranked.venue
     AND current_position.execution_domain = ranked.execution_domain
     AND current_position.account_id = ranked.account_id
     AND current_position.instrument_id = ranked.instrument_id
     AND current_position.position_mode = ranked.position_mode
     AND current_position.position_side = ranked.position_side
     AND current_position.margin_mode = ranked.margin_mode
     AND current_position.collateral_pool_id = ranked.collateral_pool_id
     AND current_position.settlement_currency = ranked.settlement_currency
    WHERE ranked.scope_rank = 1
),
evaluated AS (
    SELECT
        candidate.*,
        (
            max_event_candidate_count = 1
            AND protection_state = 'CONFIRMED'
            AND current_position_projection_state = 'CONFIRMED'
            AND current_position_source_snapshot_id = venue_position_snapshot_id
        ) AS usable
    FROM candidate
)
SELECT
    organization_id,
    venue,
    execution_domain,
    account_id,
    instrument_id,
    position_mode,
    position_side,
    margin_mode,
    collateral_pool_id,
    settlement_currency,
    CASE WHEN usable THEN 'CONFIRMED' ELSE 'UNKNOWN' END::text AS projection_state,
    CASE
        WHEN max_event_candidate_count > 1 THEN 'MAX_EVENT_TIME_COLLISION'
        WHEN protection_state = 'DEGRADED' THEN 'SOURCE_DEGRADED'
        WHEN protection_state = 'UNKNOWN' THEN 'SOURCE_UNKNOWN'
        WHEN current_position_projection_state IS NULL THEN 'POSITION_MISSING'
        WHEN current_position_projection_state <> 'CONFIRMED' THEN 'POSITION_UNKNOWN'
        WHEN current_position_source_snapshot_id <> venue_position_snapshot_id
            THEN 'POSITION_NOT_CURRENT'
        ELSE NULL
    END::text AS reason_code,
    CASE WHEN max_event_candidate_count = 1 THEN venue_protection_snapshot_id ELSE NULL END
        AS source_snapshot_id,
    CASE WHEN max_event_candidate_count = 1 THEN snapshot_hash ELSE NULL END::text
        AS source_snapshot_hash,
    CASE WHEN max_event_candidate_count = 1 THEN source_version ELSE NULL END::text
        AS source_version,
    CASE WHEN max_event_candidate_count = 1 THEN normalization_version ELSE NULL END::text
        AS normalization_version,
    CASE WHEN usable THEN venue_position_snapshot_id ELSE NULL END
        AS source_position_snapshot_id,
    CASE WHEN usable THEN protection_state ELSE 'UNKNOWN' END::text AS protection_state,
    CASE WHEN usable THEN protected_direction ELSE 'UNKNOWN' END::text AS protected_direction,
    CASE WHEN usable THEN position_quantity ELSE NULL END AS position_quantity,
    CASE WHEN usable THEN covered_quantity ELSE NULL END AS covered_quantity,
    CASE WHEN usable THEN uncovered_quantity ELSE NULL END AS uncovered_quantity,
    CASE WHEN usable THEN active_stop_order_count ELSE NULL END AS active_stop_order_count,
    CASE WHEN usable THEN worst_active_trigger_price ELSE NULL END AS worst_active_trigger_price,
    CASE WHEN usable THEN venue_native ELSE false END AS venue_native,
    CASE WHEN usable THEN reduce_only_confirmed ELSE false END AS reduce_only_confirmed,
    CASE WHEN usable THEN replacement_in_progress ELSE false END AS replacement_in_progress,
    CASE WHEN usable THEN order_set_hash ELSE NULL END::text AS order_set_hash,
    event_time AS facts_as_of,
    max_venue_observed_at AS venue_observed_at,
    max_received_at AS received_at,
    max_event_candidate_count,
    CASE WHEN usable THEN 'VENUE_CONFIRMED' ELSE 'UNKNOWN' END::text AS maturity,
    'venue-current-v1'::text AS projection_version
FROM evaluated
"""


def upgrade() -> None:
    op.execute(PROTECTION_PROJECTION_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW venue_protection_current_projection")
