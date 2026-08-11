"""Add Team trading mode shadow account and deterministic ledger.

Revision ID: 20260812_0033
Revises: 20260811_0032
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260812_0033"
down_revision: str | None = "20260811_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


AMOUNT = sa.Numeric(38, 18)


def upgrade() -> None:
    op.add_column("audit_events", sa.Column("environment", sa.String(16), nullable=True))
    op.add_column("audit_events", sa.Column("generation", sa.Integer(), nullable=True))
    op.add_column(
        "audit_events",
        sa.Column("rule_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        "ck_audit_events_environment",
        "audit_events",
        "environment IS NULL OR environment IN ('SHADOW','TESTNET','LIVE')",
    )
    op.create_check_constraint(
        "ck_audit_events_generation",
        "audit_events",
        "generation IS NULL OR generation >= 1",
    )

    op.create_table(
        "team_shadow_accounts",
        sa.Column("shadow_account_id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("initial_equity", AMOUNT, nullable=False),
        sa.Column("equity", AMOUNT, nullable=False),
        sa.Column("available_balance", AMOUNT, nullable=False),
        sa.Column("realized_pnl", AMOUNT, nullable=False),
        sa.Column("unrealized_pnl", AMOUNT, nullable=False),
        sa.Column("fees_paid", AMOUNT, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation >= 1", name="ck_team_shadow_accounts_generation"),
        sa.CheckConstraint(
            "initial_equity = 100000 AND equity >= 0 AND available_balance >= 0",
            name="ck_team_shadow_accounts_balances",
        ),
        sa.CheckConstraint(
            "fees_paid >= 0", name="ck_team_shadow_accounts_fees_nonnegative"
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','ARCHIVED')", name="ck_team_shadow_accounts_status"
        ),
        sa.CheckConstraint("version >= 1", name="ck_team_shadow_accounts_version"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.team_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("shadow_account_id"),
        sa.UniqueConstraint("team_id", "generation", name="uq_team_shadow_accounts_generation"),
    )
    op.create_index(
        "uq_team_shadow_accounts_active",
        "team_shadow_accounts",
        ["team_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    op.create_table(
        "shadow_instruments",
        sa.Column("shadow_instrument_id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_instrument_id", sa.Uuid(), nullable=True),
        sa.Column("venue", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(120), nullable=False),
        sa.Column("price_tick", AMOUNT, nullable=True),
        sa.Column("quantity_step", AMOUNT, nullable=True),
        sa.Column("contract_multiplier", AMOUNT, nullable=True),
        sa.Column("is_derivative", sa.Boolean(), nullable=False),
        sa.Column("latest_price", AMOUNT, nullable=True),
        sa.Column("price_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "venue IN ('BINANCE','HYPERLIQUID','OKX','BYBIT')",
            name="ck_shadow_instruments_venue",
        ),
        sa.CheckConstraint(
            "price_tick IS NULL OR price_tick > 0",
            name="ck_shadow_instruments_price_tick",
        ),
        sa.CheckConstraint(
            "quantity_step IS NULL OR quantity_step > 0",
            name="ck_shadow_instruments_quantity_step",
        ),
        sa.CheckConstraint(
            "contract_multiplier IS NULL OR contract_multiplier > 0",
            name="ck_shadow_instruments_multiplier",
        ),
        sa.CheckConstraint(
            "latest_price IS NULL OR latest_price > 0",
            name="ck_shadow_instruments_latest_price",
        ),
        sa.CheckConstraint("version >= 1", name="ck_shadow_instruments_version"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.team_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["catalog_instrument_id"], ["instruments.instrument_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("shadow_instrument_id"),
        sa.UniqueConstraint("team_id", "venue", "symbol", name="uq_shadow_instruments_scope"),
    )

    op.create_table(
        "shadow_positions",
        sa.Column("shadow_position_id", sa.Uuid(), nullable=False),
        sa.Column("shadow_account_id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("shadow_instrument_id", sa.Uuid(), nullable=False),
        sa.Column("source_account_id", sa.String(120), nullable=False),
        sa.Column("venue", sa.String(64), nullable=False),
        sa.Column("quantity", AMOUNT, nullable=False),
        sa.Column("average_entry_price", AMOUNT, nullable=False),
        sa.Column("mark_price", AMOUNT, nullable=False),
        sa.Column("realized_pnl", AMOUNT, nullable=False),
        sa.Column("unrealized_pnl", AMOUNT, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation >= 1", name="ck_shadow_positions_generation"),
        sa.CheckConstraint(
            "average_entry_price >= 0 AND mark_price > 0",
            name="ck_shadow_positions_prices",
        ),
        sa.CheckConstraint(
            "status IN ('OPEN','CLOSED','ARCHIVED')", name="ck_shadow_positions_status"
        ),
        sa.CheckConstraint("version >= 1", name="ck_shadow_positions_version"),
        sa.ForeignKeyConstraint(
            ["shadow_account_id"], ["team_shadow_accounts.shadow_account_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["team_id"], ["teams.team_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["shadow_instrument_id"],
            ["shadow_instruments.shadow_instrument_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "source_account_id", "venue"],
            [
                "exchange_accounts.team_id",
                "exchange_accounts.account_id",
                "exchange_accounts.venue",
            ],
            name="fk_shadow_positions_exchange_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("shadow_position_id"),
        sa.UniqueConstraint(
            "team_id",
            "generation",
            "shadow_instrument_id",
            name="uq_shadow_positions_generation_instrument",
        ),
    )
    op.create_index(
        "ix_shadow_positions_active",
        "shadow_positions",
        ["team_id", "generation", "status"],
        unique=False,
    )

    op.create_table(
        "shadow_orders",
        sa.Column("shadow_order_id", sa.Uuid(), nullable=False),
        sa.Column("shadow_account_id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("shadow_instrument_id", sa.Uuid(), nullable=False),
        sa.Column("source_account_id", sa.String(120), nullable=False),
        sa.Column("venue", sa.String(64), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=True),
        sa.Column("order_intent_id", sa.Uuid(), nullable=True),
        sa.Column("shadow_position_id", sa.Uuid(), nullable=True),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("order_type", sa.String(16), nullable=False),
        sa.Column("quantity", AMOUNT, nullable=False),
        sa.Column("limit_price", AMOUNT, nullable=True),
        sa.Column("trigger_price", AMOUNT, nullable=True),
        sa.Column("trigger_type", sa.String(16), nullable=True),
        sa.Column("execution_type", sa.String(16), nullable=True),
        sa.Column("reduce_only", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("filled_quantity", AMOUNT, nullable=False),
        sa.Column("fill_price", AMOUNT, nullable=True),
        sa.Column("fee", AMOUNT, nullable=False),
        sa.Column("realized_pnl", AMOUNT, nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation >= 1", name="ck_shadow_orders_generation"),
        sa.CheckConstraint("side IN ('BUY','SELL')", name="ck_shadow_orders_side"),
        sa.CheckConstraint(
            "order_type IN ('MARKET','LIMIT','PROTECTION')", name="ck_shadow_orders_type"
        ),
        sa.CheckConstraint(
            "status IN ('OPEN','TRIGGERED','FILLED','CANCELLED','BLOCKED')",
            name="ck_shadow_orders_status",
        ),
        sa.CheckConstraint(
            "quantity > 0 AND filled_quantity >= 0 AND filled_quantity <= quantity",
            name="ck_shadow_orders_quantities",
        ),
        sa.CheckConstraint("fee >= 0", name="ck_shadow_orders_fee"),
        sa.CheckConstraint(
            "(order_type = 'LIMIT' AND limit_price IS NOT NULL AND limit_price > 0) OR "
            "(order_type = 'MARKET' AND limit_price IS NULL) OR "
            "(order_type = 'PROTECTION' AND trigger_price IS NOT NULL "
            "AND trigger_type IN ('STOP_LOSS','TAKE_PROFIT') "
            "AND execution_type IN ('MARKET','LIMIT') "
            "AND ((execution_type = 'LIMIT' AND limit_price IS NOT NULL AND limit_price > 0) "
            "OR (execution_type = 'MARKET' AND limit_price IS NULL)))",
            name="ck_shadow_orders_shape",
        ),
        sa.CheckConstraint(
            "(order_type = 'PROTECTION' AND reduce_only) OR order_type <> 'PROTECTION'",
            name="ck_shadow_orders_protection_reduce_only",
        ),
        sa.CheckConstraint("version >= 1", name="ck_shadow_orders_version"),
        sa.ForeignKeyConstraint(
            ["shadow_account_id"], ["team_shadow_accounts.shadow_account_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["team_id"], ["teams.team_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["shadow_instrument_id"],
            ["shadow_instruments.shadow_instrument_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.campaign_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["order_intent_id"], ["order_intents.intent_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["shadow_position_id"], ["shadow_positions.shadow_position_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "source_account_id", "venue"],
            [
                "exchange_accounts.team_id",
                "exchange_accounts.account_id",
                "exchange_accounts.venue",
            ],
            name="fk_shadow_orders_exchange_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("shadow_order_id"),
        sa.UniqueConstraint("order_intent_id", name="uq_shadow_orders_intent"),
        sa.UniqueConstraint(
            "team_id", "generation", "idempotency_key", name="uq_shadow_orders_idempotency"
        ),
    )
    op.create_index(
        "ix_shadow_orders_open",
        "shadow_orders",
        ["team_id", "generation", "status"],
        unique=False,
    )

    op.create_table(
        "shadow_fills",
        sa.Column("shadow_fill_id", sa.Uuid(), nullable=False),
        sa.Column("shadow_order_id", sa.Uuid(), nullable=False),
        sa.Column("shadow_account_id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("shadow_instrument_id", sa.Uuid(), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("quantity", AMOUNT, nullable=False),
        sa.Column("price", AMOUNT, nullable=False),
        sa.Column("notional", AMOUNT, nullable=False),
        sa.Column("fee", AMOUNT, nullable=False),
        sa.Column("realized_pnl", AMOUNT, nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation >= 1", name="ck_shadow_fills_generation"),
        sa.CheckConstraint("side IN ('BUY','SELL')", name="ck_shadow_fills_side"),
        sa.CheckConstraint(
            "quantity > 0 AND price > 0 AND notional > 0 AND fee >= 0",
            name="ck_shadow_fills_amounts",
        ),
        sa.ForeignKeyConstraint(
            ["shadow_order_id"], ["shadow_orders.shadow_order_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["shadow_account_id"], ["team_shadow_accounts.shadow_account_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["team_id"], ["teams.team_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["shadow_instrument_id"],
            ["shadow_instruments.shadow_instrument_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("shadow_fill_id"),
        sa.UniqueConstraint("shadow_order_id", name="uq_shadow_fills_order"),
    )
    op.create_index(
        "ix_shadow_fills_team_time",
        "shadow_fills",
        ["team_id", "generation", "executed_at"],
        unique=False,
    )

    op.execute(
        """
        INSERT INTO team_shadow_accounts (
            shadow_account_id, team_id, generation, initial_equity, equity,
            available_balance, realized_pnl, unrealized_pnl, fees_paid,
            status, version, created_at, updated_at
        )
        SELECT gen_random_uuid(), team_id, 1, 100000, 100000,
               100000, 0, 0, 0, 'ACTIVE', 1, now(), now()
        FROM teams
        WHERE execution_mode = 'SHADOW'
        """
    )


def downgrade() -> None:
    op.drop_index("ix_shadow_fills_team_time", table_name="shadow_fills")
    op.drop_table("shadow_fills")
    op.drop_index("ix_shadow_orders_open", table_name="shadow_orders")
    op.drop_table("shadow_orders")
    op.drop_index("ix_shadow_positions_active", table_name="shadow_positions")
    op.drop_table("shadow_positions")
    op.drop_table("shadow_instruments")
    op.drop_index("uq_team_shadow_accounts_active", table_name="team_shadow_accounts")
    op.drop_table("team_shadow_accounts")
    op.drop_constraint("ck_audit_events_generation", "audit_events", type_="check")
    op.drop_constraint("ck_audit_events_environment", "audit_events", type_="check")
    op.drop_column("audit_events", "rule_summary")
    op.drop_column("audit_events", "generation")
    op.drop_column("audit_events", "environment")
