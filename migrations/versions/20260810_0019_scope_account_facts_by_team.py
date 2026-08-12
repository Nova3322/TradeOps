"""Scope trading account facts by team.

Revision ID: 20260810_0019
Revises: 20260810_0018
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260810_0019"
down_revision: str | None = "20260810_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FACT_TABLES = (
    "venue_orders",
    "venue_fills",
    "positions",
    "account_equities",
    "account_equity_observations",
    "funding_payments",
    "reconciliation_runs",
)


def _default_team_id(connection: sa.Connection) -> object:
    team_id = connection.execute(
        sa.text(
            "SELECT t.team_id FROM teams t JOIN workspaces w "
            "ON w.workspace_id = t.workspace_id "
            "WHERE w.slug = 'default' AND t.slug = 'default' "
            "ORDER BY t.created_at, t.team_id LIMIT 1"
        )
    ).scalar_one_or_none()
    if team_id is None:
        raise RuntimeError("0019 requires the migrated default team")
    return team_id


def _backfill_team_ids(connection: sa.Connection) -> None:
    # Facts already bound to a workflow inherit its authoritative team. Facts
    # written before workflow/team support belong to the migrated default team.
    connection.execute(
        sa.text(
            "UPDATE venue_orders fact SET team_id = campaign.team_id "
            "FROM order_intents intent JOIN campaigns campaign "
            "ON campaign.campaign_id = intent.campaign_id "
            "WHERE fact.order_intent_id = intent.intent_id"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE venue_fills fact SET team_id = campaign.team_id "
            "FROM campaigns campaign WHERE fact.campaign_id = campaign.campaign_id"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE venue_fills fact SET team_id = campaign.team_id "
            "FROM order_intents intent JOIN campaigns campaign "
            "ON campaign.campaign_id = intent.campaign_id "
            "WHERE fact.team_id IS NULL AND fact.order_intent_id = intent.intent_id"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE funding_payments fact SET team_id = campaign.team_id "
            "FROM campaigns campaign WHERE fact.campaign_id = campaign.campaign_id"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE reconciliation_runs fact SET team_id = campaign.team_id "
            "FROM campaigns campaign WHERE fact.campaign_id = campaign.campaign_id"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE account_equity_observations observation "
            "SET team_id = equity.team_id FROM account_equities equity "
            "WHERE observation.account_equity_id = equity.account_equity_id"
        )
    )
    unresolved = sum(
        int(
            connection.execute(
                sa.text(f"SELECT count(*) FROM {table_name} WHERE team_id IS NULL")  # noqa: S608
            ).scalar_one()
        )
        for table_name in FACT_TABLES
    )
    if unresolved == 0:
        return
    default_team_id = _default_team_id(connection)
    for table_name in FACT_TABLES:
        connection.execute(
            sa.text(f"UPDATE {table_name} SET team_id = :team_id WHERE team_id IS NULL"),  # noqa: S608
            {"team_id": default_team_id},
        )


def _add_account_root_fks() -> None:
    for table_name in (
        "venue_orders",
        "venue_fills",
        "positions",
        "funding_payments",
    ):
        op.create_foreign_key(
            f"fk_{table_name}_team_exchange_account",
            table_name,
            "exchange_accounts",
            ["team_id", "account_id", "venue"],
            ["team_id", "account_id", "venue"],
            ondelete="RESTRICT",
        )


def upgrade() -> None:
    for table_name in FACT_TABLES:
        op.add_column(table_name, sa.Column("team_id", sa.Uuid(), nullable=True))

    _backfill_team_ids(op.get_bind())

    for table_name in FACT_TABLES:
        op.alter_column(table_name, "team_id", existing_type=sa.Uuid(), nullable=False)
        op.create_foreign_key(
            f"fk_{table_name}_team",
            table_name,
            "teams",
            ["team_id"],
            ["team_id"],
            ondelete="RESTRICT",
        )

    op.drop_constraint("uq_venue_orders_external", "venue_orders", type_="unique")
    op.drop_constraint("uq_venue_orders_client_identity", "venue_orders", type_="unique")
    op.drop_index("ix_venue_orders_scope", table_name="venue_orders")
    op.create_unique_constraint(
        "uq_venue_orders_external",
        "venue_orders",
        ["team_id", "environment", "account_id", "venue", "venue_order_id"],
    )
    op.create_unique_constraint(
        "uq_venue_orders_client_identity",
        "venue_orders",
        ["team_id", "environment", "account_id", "venue", "client_order_id"],
    )
    op.create_index(
        "ix_venue_orders_scope",
        "venue_orders",
        ["team_id", "environment", "account_id", "venue", "instrument_id"],
    )

    op.drop_constraint("uq_venue_fills_external", "venue_fills", type_="unique")
    op.drop_index("ix_venue_fills_scope", table_name="venue_fills")
    op.create_unique_constraint(
        "uq_venue_fills_external",
        "venue_fills",
        ["team_id", "environment", "account_id", "venue", "venue_fill_id"],
    )
    op.create_index(
        "ix_venue_fills_scope",
        "venue_fills",
        ["team_id", "environment", "account_id", "venue", "instrument_id"],
    )

    op.drop_constraint("uq_positions_scope", "positions", type_="unique")
    op.create_unique_constraint(
        "uq_positions_scope",
        "positions",
        ["team_id", "environment", "account_id", "venue", "instrument_id"],
    )

    op.drop_constraint("uq_account_equities_scope", "account_equities", type_="unique")
    op.create_unique_constraint(
        "uq_account_equities_scope",
        "account_equities",
        ["team_id", "environment", "account_id", "venue", "currency"],
    )
    op.create_unique_constraint(
        "uq_account_equities_team_identity",
        "account_equities",
        ["team_id", "account_equity_id", "account_id", "venue"],
    )

    op.drop_constraint(
        "account_equity_observations_account_equity_id_fkey",
        "account_equity_observations",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_account_equity_observations_scope_time",
        table_name="account_equity_observations",
    )
    op.create_foreign_key(
        "fk_account_equity_observations_team_equity",
        "account_equity_observations",
        "account_equities",
        ["team_id", "account_equity_id", "account_id", "venue"],
        ["team_id", "account_equity_id", "account_id", "venue"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_account_equity_observations_scope_time",
        "account_equity_observations",
        ["team_id", "environment", "location_type", "venue", "account_id", "observed_at"],
    )

    op.drop_constraint("uq_funding_payments_external", "funding_payments", type_="unique")
    op.drop_index("ix_funding_payments_scope", table_name="funding_payments")
    op.create_unique_constraint(
        "uq_funding_payments_external",
        "funding_payments",
        ["team_id", "environment", "account_id", "venue", "venue_payment_id"],
    )
    op.create_index(
        "ix_funding_payments_scope",
        "funding_payments",
        ["team_id", "environment", "account_id", "venue", "instrument_id"],
    )

    op.drop_index("ix_reconciliation_scope_completed", table_name="reconciliation_runs")
    op.create_index(
        "ix_reconciliation_scope_completed",
        "reconciliation_runs",
        ["team_id", "execution_scope", "completed_at"],
    )

    _add_account_root_fks()


def downgrade() -> None:
    for table_name in (
        "funding_payments",
        "positions",
        "venue_fills",
        "venue_orders",
    ):
        op.drop_constraint(
            f"fk_{table_name}_team_exchange_account",
            table_name,
            type_="foreignkey",
        )

    op.drop_index("ix_reconciliation_scope_completed", table_name="reconciliation_runs")
    op.create_index(
        "ix_reconciliation_scope_completed",
        "reconciliation_runs",
        ["execution_scope", "completed_at"],
    )

    op.drop_constraint("uq_funding_payments_external", "funding_payments", type_="unique")
    op.drop_index("ix_funding_payments_scope", table_name="funding_payments")
    op.create_unique_constraint(
        "uq_funding_payments_external",
        "funding_payments",
        ["environment", "account_id", "venue", "venue_payment_id"],
    )
    op.create_index(
        "ix_funding_payments_scope",
        "funding_payments",
        ["environment", "account_id", "venue", "instrument_id"],
    )

    op.drop_constraint(
        "fk_account_equity_observations_team_equity",
        "account_equity_observations",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_account_equity_observations_scope_time",
        table_name="account_equity_observations",
    )
    op.create_foreign_key(
        "account_equity_observations_account_equity_id_fkey",
        "account_equity_observations",
        "account_equities",
        ["account_equity_id"],
        ["account_equity_id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_account_equity_observations_scope_time",
        "account_equity_observations",
        ["environment", "location_type", "venue", "account_id", "observed_at"],
    )
    op.drop_constraint("uq_account_equities_team_identity", "account_equities", type_="unique")
    op.drop_constraint("uq_account_equities_scope", "account_equities", type_="unique")
    op.create_unique_constraint(
        "uq_account_equities_scope",
        "account_equities",
        ["environment", "account_id", "venue", "currency"],
    )

    op.drop_constraint("uq_positions_scope", "positions", type_="unique")
    op.create_unique_constraint(
        "uq_positions_scope",
        "positions",
        ["environment", "account_id", "venue", "instrument_id"],
    )

    op.drop_constraint("uq_venue_fills_external", "venue_fills", type_="unique")
    op.drop_index("ix_venue_fills_scope", table_name="venue_fills")
    op.create_unique_constraint(
        "uq_venue_fills_external",
        "venue_fills",
        ["environment", "account_id", "venue", "venue_fill_id"],
    )
    op.create_index(
        "ix_venue_fills_scope",
        "venue_fills",
        ["environment", "account_id", "venue", "instrument_id"],
    )

    op.drop_constraint("uq_venue_orders_external", "venue_orders", type_="unique")
    op.drop_constraint("uq_venue_orders_client_identity", "venue_orders", type_="unique")
    op.drop_index("ix_venue_orders_scope", table_name="venue_orders")
    op.create_unique_constraint(
        "uq_venue_orders_external",
        "venue_orders",
        ["environment", "account_id", "venue", "venue_order_id"],
    )
    op.create_unique_constraint(
        "uq_venue_orders_client_identity",
        "venue_orders",
        ["environment", "account_id", "venue", "client_order_id"],
    )
    op.create_index(
        "ix_venue_orders_scope",
        "venue_orders",
        ["environment", "account_id", "venue", "instrument_id"],
    )

    for table_name in reversed(FACT_TABLES):
        op.drop_constraint(f"fk_{table_name}_team", table_name, type_="foreignkey")
        op.drop_column(table_name, "team_id")
