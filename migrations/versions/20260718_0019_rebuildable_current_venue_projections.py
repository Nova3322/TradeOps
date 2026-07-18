"""Add rebuildable current venue position and account-equity projections.

Revision ID: 20260718_0019
Revises: 20260718_0018
Create Date: 2026-07-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260718_0019"
down_revision: str | Sequence[str] | None = "20260718_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


POSITION_PROJECTION_SQL = """
CREATE VIEW venue_position_current_projection AS
WITH ranked AS (
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
                venue_position_snapshot_id DESC
        ) AS scope_rank
    FROM venue_position_snapshots AS snapshot
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
    CASE
        WHEN max_event_candidate_count = 1 AND position_state <> 'UNKNOWN'
            THEN 'CONFIRMED'
        ELSE 'UNKNOWN'
    END::text AS projection_state,
    CASE
        WHEN max_event_candidate_count > 1 THEN 'MAX_EVENT_TIME_COLLISION'
        WHEN position_state = 'UNKNOWN' THEN 'SOURCE_UNKNOWN'
        ELSE NULL
    END::text AS reason_code,
    CASE
        WHEN max_event_candidate_count = 1 THEN venue_position_snapshot_id
        ELSE NULL
    END AS source_snapshot_id,
    CASE
        WHEN max_event_candidate_count = 1 THEN snapshot_hash
        ELSE NULL
    END::text AS source_snapshot_hash,
    CASE
        WHEN max_event_candidate_count = 1 THEN source_version
        ELSE NULL
    END::text AS source_version,
    CASE
        WHEN max_event_candidate_count = 1 THEN normalization_version
        ELSE NULL
    END::text AS normalization_version,
    CASE
        WHEN max_event_candidate_count = 1 THEN position_state
        ELSE 'UNKNOWN'
    END::text AS position_state,
    CASE
        WHEN max_event_candidate_count = 1 AND position_state <> 'UNKNOWN' THEN direction
        ELSE 'UNKNOWN'
    END::text AS direction,
    CASE
        WHEN max_event_candidate_count = 1 AND position_state <> 'UNKNOWN' THEN quantity
        ELSE NULL
    END AS quantity,
    CASE
        WHEN max_event_candidate_count = 1 AND position_state <> 'UNKNOWN' THEN entry_price
        ELSE NULL
    END AS entry_price,
    CASE
        WHEN max_event_candidate_count = 1 AND position_state <> 'UNKNOWN' THEN mark_price
        ELSE NULL
    END AS mark_price,
    CASE
        WHEN max_event_candidate_count = 1 AND position_state <> 'UNKNOWN'
            THEN contract_multiplier
        ELSE NULL
    END AS contract_multiplier,
    CASE
        WHEN max_event_candidate_count = 1 AND position_state <> 'UNKNOWN' THEN notional
        ELSE NULL
    END AS notional,
    CASE
        WHEN max_event_candidate_count = 1 AND position_state <> 'UNKNOWN'
            THEN unrealized_pnl
        ELSE NULL
    END AS unrealized_pnl,
    CASE
        WHEN max_event_candidate_count = 1 AND position_state <> 'UNKNOWN'
            THEN liquidation_price
        ELSE NULL
    END AS liquidation_price,
    CASE
        WHEN max_event_candidate_count = 1 AND position_state <> 'UNKNOWN' THEN leverage
        ELSE NULL
    END AS leverage,
    CASE
        WHEN max_event_candidate_count = 1 AND position_state <> 'UNKNOWN'
            THEN initial_margin
        ELSE NULL
    END AS initial_margin,
    CASE
        WHEN max_event_candidate_count = 1 AND position_state <> 'UNKNOWN'
            THEN maintenance_margin
        ELSE NULL
    END AS maintenance_margin,
    event_time AS facts_as_of,
    max_venue_observed_at AS venue_observed_at,
    max_received_at AS received_at,
    max_event_candidate_count,
    CASE
        WHEN max_event_candidate_count = 1 AND position_state <> 'UNKNOWN'
            THEN 'VENUE_CONFIRMED'
        ELSE 'UNKNOWN'
    END::text AS maturity,
    'venue-current-v1'::text AS projection_version
FROM ranked
WHERE scope_rank = 1
"""


ACCOUNT_EQUITY_PROJECTION_SQL = """
CREATE VIEW venue_account_equity_current_projection AS
WITH ranked AS (
    SELECT
        snapshot.*,
        count(*) OVER (
            PARTITION BY
                organization_id,
                venue,
                execution_domain,
                account_id,
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
                margin_mode,
                collateral_pool_id,
                settlement_currency
            ORDER BY
                event_time DESC,
                venue_observed_at DESC,
                first_received_at DESC,
                recorded_at DESC,
                venue_account_equity_snapshot_id DESC
        ) AS scope_rank
    FROM venue_account_equity_snapshots AS snapshot
)
SELECT
    organization_id,
    venue,
    execution_domain,
    account_id,
    margin_mode,
    collateral_pool_id,
    settlement_currency,
    CASE
        WHEN max_event_candidate_count = 1 AND equity_state = 'CONFIRMED'
            THEN 'CONFIRMED'
        ELSE 'UNKNOWN'
    END::text AS projection_state,
    CASE
        WHEN max_event_candidate_count > 1 THEN 'MAX_EVENT_TIME_COLLISION'
        WHEN equity_state = 'UNKNOWN' THEN 'SOURCE_UNKNOWN'
        ELSE NULL
    END::text AS reason_code,
    CASE
        WHEN max_event_candidate_count = 1 THEN venue_account_equity_snapshot_id
        ELSE NULL
    END AS source_snapshot_id,
    CASE
        WHEN max_event_candidate_count = 1 THEN snapshot_hash
        ELSE NULL
    END::text AS source_snapshot_hash,
    CASE
        WHEN max_event_candidate_count = 1 THEN source_version
        ELSE NULL
    END::text AS source_version,
    CASE
        WHEN max_event_candidate_count = 1 THEN normalization_version
        ELSE NULL
    END::text AS normalization_version,
    CASE
        WHEN max_event_candidate_count = 1 AND equity_state = 'CONFIRMED'
            THEN wallet_balance
        ELSE NULL
    END AS wallet_balance,
    CASE
        WHEN max_event_candidate_count = 1 AND equity_state = 'CONFIRMED'
            THEN exchange_margin_equity
        ELSE NULL
    END AS exchange_margin_equity,
    CASE
        WHEN max_event_candidate_count = 1 AND equity_state = 'CONFIRMED'
            THEN available_margin
        ELSE NULL
    END AS available_margin,
    CASE
        WHEN max_event_candidate_count = 1 AND equity_state = 'CONFIRMED'
            THEN total_unrealized_pnl
        ELSE NULL
    END AS total_unrealized_pnl,
    CASE
        WHEN max_event_candidate_count = 1 AND equity_state = 'CONFIRMED'
            THEN total_initial_margin
        ELSE NULL
    END AS total_initial_margin,
    CASE
        WHEN max_event_candidate_count = 1 AND equity_state = 'CONFIRMED'
            THEN total_maintenance_margin
        ELSE NULL
    END AS total_maintenance_margin,
    CASE
        WHEN max_event_candidate_count = 1 AND equity_state = 'CONFIRMED'
            THEN total_liability
        ELSE NULL
    END AS total_liability,
    CASE
        WHEN max_event_candidate_count = 1 AND equity_state = 'CONFIRMED'
            THEN unsettled_fee
        ELSE NULL
    END AS unsettled_fee,
    CASE
        WHEN max_event_candidate_count = 1 AND equity_state = 'CONFIRMED'
            THEN unsettled_funding
        ELSE NULL
    END AS unsettled_funding,
    CASE
        WHEN max_event_candidate_count = 1 AND equity_state = 'CONFIRMED'
            THEN includes_unrealized_pnl
        ELSE false
    END AS includes_unrealized_pnl,
    event_time AS facts_as_of,
    max_venue_observed_at AS venue_observed_at,
    max_received_at AS received_at,
    max_event_candidate_count,
    CASE
        WHEN max_event_candidate_count = 1 AND equity_state = 'CONFIRMED'
            THEN 'VENUE_CONFIRMED'
        ELSE 'UNKNOWN'
    END::text AS maturity,
    'venue-current-v1'::text AS projection_version
FROM ranked
WHERE scope_rank = 1
"""


def upgrade() -> None:
    op.execute(POSITION_PROJECTION_SQL)
    op.execute(ACCOUNT_EQUITY_PROJECTION_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW venue_account_equity_current_projection")
    op.execute("DROP VIEW venue_position_current_projection")
