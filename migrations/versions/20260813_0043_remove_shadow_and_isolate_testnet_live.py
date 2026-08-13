"""remove shadow and isolate testnet live

Revision ID: 20260813_0043
Revises: 20260813_0042
Create Date: 2026-08-13 20:43:10.508186
"""

# The downgrade intentionally recreates the exact legacy schema emitted by PostgreSQL inspection.
# Keeping those generated SQL expressions byte-for-byte stable is more important than reflowing them.
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260813_0043"
down_revision: str | None = "20260813_0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_check(table: str, name: str, expression: str) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, expression)


def _replace_environment_checks(*, include_legacy: bool) -> None:
    environments = "('SHADOW','TESTNET','LIVE')" if include_legacy else "('TESTNET','LIVE')"
    analytics_environments = "('SHADOW','LIVE')" if include_legacy else "('TESTNET','LIVE')"
    for table, name in (
        ("account_equities", "ck_account_equities_environment"),
        ("account_equity_observations", "ck_account_equity_observations_environment"),
        ("campaigns", "ck_campaigns_environment"),
        ("exchange_accounts", "ck_exchange_accounts_environment"),
        ("funding_payments", "ck_funding_payments_environment"),
        ("positions", "ck_positions_environment"),
        ("proposals", "ck_proposals_environment"),
        ("trading_authorizations", "ck_authorizations_environment"),
        ("transfer_authorizations", "ck_transfer_authorizations_environment"),
        ("transfer_proposals", "ck_transfer_proposals_environment"),
        ("venue_fills", "ck_venue_fills_environment"),
        ("venue_orders", "ck_venue_orders_environment"),
    ):
        _replace_check(table, name, f"environment IN {environments}")
    _replace_check(
        "audit_events",
        "ck_audit_events_environment",
        f"environment IS NULL OR environment IN {environments}",
    )
    _replace_check(
        "analytics_equity_snapshots",
        "ck_analytics_equity_snapshots_environment",
        f"environment IN {analytics_environments}",
    )
    _replace_check(
        "analytics_reports",
        "ck_analytics_reports_environment",
        f"environment IN {analytics_environments}",
    )
    generation_expression = (
        "(environment = 'SHADOW' AND generation IS NOT NULL) OR "
        "(environment = 'LIVE' AND generation IS NULL)"
        if include_legacy
        else "generation IS NULL"
    )
    _replace_check(
        "analytics_equity_snapshots",
        "ck_analytics_equity_snapshots_generation",
        generation_expression,
    )
    _replace_check(
        "analytics_reports",
        "ck_analytics_reports_generation",
        generation_expression,
    )
    _replace_check(
        "capital_automation_policies",
        "ck_capital_automation_policies_environment",
        "environment IN ('SHADOW','TESTNET')"
        if include_legacy
        else "environment IN ('TESTNET','LIVE')",
    )
    _replace_check(
        "direct_capital_configurations",
        "ck_direct_capital_configuration_environment",
        "environment IN ('SHADOW','LIVE')" if include_legacy else "environment = 'LIVE'",
    )
    _replace_check(
        "notification_routes",
        "ck_notification_routes_environment",
        "environment IN ('SHADOW','LIVE')"
        if include_legacy
        else "environment IN ('TESTNET','LIVE')",
    )
    _replace_check(
        "teams",
        "ck_teams_execution_mode",
        "execution_mode IN ('SETUP','SHADOW','TESTNET','LIVE')"
        if include_legacy
        else "execution_mode IN ('SETUP','TESTNET','LIVE')",
    )


def upgrade() -> None:
    # Retire dedicated simulation tables before deleting their shared references.
    op.drop_index("ix_shadow_fills_team_time", table_name="shadow_fills")
    op.drop_table("shadow_fills")
    op.drop_index("ix_shadow_orders_open", table_name="shadow_orders")
    op.drop_table("shadow_orders")
    op.drop_index("ix_shadow_positions_active", table_name="shadow_positions")
    op.drop_table("shadow_positions")
    op.drop_table("shadow_instruments")
    op.drop_index("uq_team_shadow_accounts_active", table_name="team_shadow_accounts")
    op.drop_table("team_shadow_accounts")

    # Remove only retired-environment facts. Historical TESTNET and LIVE facts remain untouched.
    op.execute("DELETE FROM notification_deliveries WHERE environment = 'SHADOW'")
    op.execute(
        "DELETE FROM notification_deliveries WHERE notification_route_id IN (SELECT notification_route_id FROM notification_routes WHERE environment = 'SHADOW')"
    )
    op.execute("DELETE FROM notification_routes WHERE environment = 'SHADOW'")
    op.execute("DELETE FROM venue_fills WHERE environment = 'SHADOW'")
    op.execute("DELETE FROM venue_orders WHERE environment = 'SHADOW'")
    op.execute("DELETE FROM funding_payments WHERE environment = 'SHADOW'")
    op.execute(
        "DELETE FROM reconciliation_runs WHERE campaign_id IN (SELECT campaign_id FROM campaigns WHERE environment = 'SHADOW')"
    )
    op.execute(
        "DELETE FROM order_intents WHERE campaign_id IN (SELECT campaign_id FROM campaigns WHERE environment = 'SHADOW')"
    )
    op.execute(
        "DELETE FROM risk_reservations WHERE campaign_id IN (SELECT campaign_id FROM campaigns WHERE environment = 'SHADOW')"
    )
    op.execute("DELETE FROM campaigns WHERE environment = 'SHADOW'")
    op.execute("DELETE FROM trading_authorizations WHERE environment = 'SHADOW'")
    op.execute(
        "DELETE FROM risk_decisions WHERE proposal_id IN (SELECT proposal_id FROM proposals WHERE environment = 'SHADOW')"
    )
    op.execute(
        "DELETE FROM approvals WHERE proposal_id IN (SELECT proposal_id FROM proposals WHERE environment = 'SHADOW')"
    )
    op.execute("DELETE FROM proposals WHERE environment = 'SHADOW'")
    op.execute(
        "DELETE FROM protection_orders WHERE position_id IN (SELECT position_id FROM positions WHERE environment = 'SHADOW')"
    )
    op.execute("DELETE FROM positions WHERE environment = 'SHADOW'")
    op.execute("DELETE FROM account_equity_observations WHERE environment = 'SHADOW'")
    op.execute("DELETE FROM account_equities WHERE environment = 'SHADOW'")
    op.execute("DELETE FROM analytics_reports WHERE environment = 'SHADOW'")
    op.execute("DELETE FROM analytics_equity_snapshots WHERE environment = 'SHADOW'")
    op.execute("DELETE FROM capital_transfers WHERE environment = 'SHADOW'")
    op.execute("DELETE FROM transfer_authorizations WHERE environment = 'SHADOW'")
    op.execute(
        "DELETE FROM approvals WHERE transfer_proposal_id IN (SELECT transfer_proposal_id FROM transfer_proposals WHERE environment = 'SHADOW')"
    )
    op.execute("DELETE FROM transfer_proposals WHERE environment = 'SHADOW'")
    op.execute("DELETE FROM capital_automation_policies WHERE environment = 'SHADOW'")
    op.execute("DELETE FROM direct_capital_configurations WHERE environment = 'SHADOW'")
    op.execute("DELETE FROM audit_events WHERE environment = 'SHADOW'")
    op.execute(
        "DELETE FROM direct_capital_operations d USING exchange_accounts a WHERE d.team_id = a.team_id AND d.account_id = a.account_id AND d.venue = a.venue AND a.environment = 'SHADOW'"
    )
    op.execute(
        "DELETE FROM runtime_source_health h USING exchange_accounts a WHERE h.team_id = a.team_id AND h.account_id = a.account_id AND h.venue = a.venue AND a.environment = 'SHADOW'"
    )

    child_fks = (
        ("capital_automation_policies", "fk_capital_automation_policies_team_exchange_account"),
        ("capital_transfers", "fk_capital_transfers_team_exchange_account"),
        ("direct_capital_operations", "fk_direct_capital_operations_team_exchange_account"),
        ("funding_payments", "fk_funding_payments_team_exchange_account"),
        ("positions", "fk_positions_team_exchange_account"),
        ("proposals", "fk_proposals_team_exchange_account"),
        ("runtime_source_health", "fk_runtime_source_health_team_exchange_account"),
        ("transfer_proposals", "fk_transfer_proposals_team_exchange_account"),
        ("venue_fills", "fk_venue_fills_team_exchange_account"),
        ("venue_orders", "fk_venue_orders_team_exchange_account"),
    )
    for table, constraint in child_fks:
        op.drop_constraint(constraint, table, type_="foreignkey")

    op.add_column(
        "direct_capital_operations", sa.Column("environment", sa.String(16), nullable=True)
    )
    op.execute(
        "UPDATE direct_capital_operations d SET environment = a.environment FROM exchange_accounts a WHERE d.team_id = a.team_id AND d.account_id = a.account_id AND d.venue = a.venue"
    )
    # Legacy direct-capital rows with a nullable account scope are production-only
    # operations. They have no exchange-account row to derive from, so make that
    # historical invariant explicit before enforcing the new non-null column.
    op.execute(
        "UPDATE direct_capital_operations SET environment = 'LIVE' WHERE environment IS NULL"
    )
    op.alter_column(
        "direct_capital_operations", "environment", nullable=False, server_default="LIVE"
    )
    op.create_check_constraint(
        "ck_direct_capital_operations_live", "direct_capital_operations", "environment = 'LIVE'"
    )

    op.add_column("runtime_source_health", sa.Column("environment", sa.String(16), nullable=True))
    op.execute(
        "UPDATE runtime_source_health h SET environment = a.environment FROM exchange_accounts a WHERE h.team_id = a.team_id AND h.account_id = a.account_id AND h.venue = a.venue"
    )
    op.create_check_constraint(
        "ck_runtime_source_health_environment",
        "runtime_source_health",
        "(account_id IS NULL AND venue IS NULL AND environment IS NULL) OR "
        "(account_id IS NOT NULL AND venue IS NOT NULL AND environment IN ('TESTNET','LIVE'))",
    )

    op.execute("DELETE FROM exchange_accounts WHERE environment = 'SHADOW'")
    op.execute(
        "UPDATE teams SET execution_mode = 'SETUP', trading_enabled = false, execution_mode_locked_at = NULL, version = version + 1, updated_at = CURRENT_TIMESTAMP WHERE execution_mode = 'SHADOW'"
    )
    _replace_environment_checks(include_legacy=False)

    op.add_column(
        "exchange_accounts", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("exchange_accounts", sa.Column("deleted_by", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_exchange_accounts_deleted_by",
        "exchange_accounts",
        "users",
        ["deleted_by"],
        ["user_id"],
        ondelete="RESTRICT",
    )
    op.add_column("teams", sa.Column("execution_mode_updated_by", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_teams_execution_mode_updated_by",
        "teams",
        "users",
        ["execution_mode_updated_by"],
        ["user_id"],
        ondelete="RESTRICT",
    )

    op.drop_constraint(
        "uq_exchange_accounts_team_account_venue", "exchange_accounts", type_="unique"
    )
    op.create_unique_constraint(
        "uq_exchange_accounts_team_environment_account_venue",
        "exchange_accounts",
        ["team_id", "environment", "account_id", "venue"],
    )
    op.drop_constraint("uq_runtime_source_health_scope", "runtime_source_health", type_="unique")
    op.create_unique_constraint(
        "uq_runtime_source_health_scope",
        "runtime_source_health",
        ["team_id", "source_name", "environment", "account_id", "venue"],
        postgresql_nulls_not_distinct=True,
    )

    for table, constraint in child_fks:
        op.create_foreign_key(
            constraint,
            table,
            "exchange_accounts",
            ["team_id", "environment", "account_id", "venue"],
            ["team_id", "environment", "account_id", "venue"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    _replace_environment_checks(include_legacy=True)
    child_fks = (
        ("capital_automation_policies", "fk_capital_automation_policies_team_exchange_account"),
        ("capital_transfers", "fk_capital_transfers_team_exchange_account"),
        ("direct_capital_operations", "fk_direct_capital_operations_team_exchange_account"),
        ("funding_payments", "fk_funding_payments_team_exchange_account"),
        ("positions", "fk_positions_team_exchange_account"),
        ("proposals", "fk_proposals_team_exchange_account"),
        ("runtime_source_health", "fk_runtime_source_health_team_exchange_account"),
        ("transfer_proposals", "fk_transfer_proposals_team_exchange_account"),
        ("venue_fills", "fk_venue_fills_team_exchange_account"),
        ("venue_orders", "fk_venue_orders_team_exchange_account"),
    )
    for table, constraint in child_fks:
        op.drop_constraint(constraint, table, type_="foreignkey")

    op.drop_constraint("uq_runtime_source_health_scope", "runtime_source_health", type_="unique")
    op.drop_constraint(
        "uq_exchange_accounts_team_environment_account_venue", "exchange_accounts", type_="unique"
    )
    op.create_unique_constraint(
        "uq_exchange_accounts_team_account_venue",
        "exchange_accounts",
        ["team_id", "account_id", "venue"],
    )
    op.create_unique_constraint(
        "uq_runtime_source_health_scope",
        "runtime_source_health",
        ["team_id", "source_name", "account_id", "venue"],
        postgresql_nulls_not_distinct=True,
    )
    for table, constraint in child_fks:
        op.create_foreign_key(
            constraint,
            table,
            "exchange_accounts",
            ["team_id", "account_id", "venue"],
            ["team_id", "account_id", "venue"],
            ondelete="RESTRICT",
        )

    op.drop_constraint(
        "ck_runtime_source_health_environment", "runtime_source_health", type_="check"
    )
    op.drop_column("runtime_source_health", "environment")
    op.drop_constraint(
        "ck_direct_capital_operations_live", "direct_capital_operations", type_="check"
    )
    op.drop_column("direct_capital_operations", "environment")
    op.drop_constraint("fk_exchange_accounts_deleted_by", "exchange_accounts", type_="foreignkey")
    op.drop_column("exchange_accounts", "deleted_by")
    op.drop_column("exchange_accounts", "deleted_at")
    op.drop_constraint("fk_teams_execution_mode_updated_by", "teams", type_="foreignkey")
    op.drop_column("teams", "execution_mode_updated_by")

    op.create_table(
        "team_shadow_accounts",
        sa.Column("shadow_account_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("team_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("generation", sa.INTEGER(), autoincrement=False, nullable=False),
        sa.Column(
            "initial_equity",
            sa.NUMERIC(precision=38, scale=18),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column(
            "equity", sa.NUMERIC(precision=38, scale=18), autoincrement=False, nullable=False
        ),
        sa.Column(
            "available_balance",
            sa.NUMERIC(precision=38, scale=18),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column(
            "realized_pnl", sa.NUMERIC(precision=38, scale=18), autoincrement=False, nullable=False
        ),
        sa.Column(
            "unrealized_pnl",
            sa.NUMERIC(precision=38, scale=18),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column(
            "fees_paid", sa.NUMERIC(precision=38, scale=18), autoincrement=False, nullable=False
        ),
        sa.Column("status", sa.VARCHAR(length=16), autoincrement=False, nullable=False),
        sa.Column("version", sa.INTEGER(), autoincrement=False, nullable=False),
        sa.Column(
            "created_at", postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False
        ),
        sa.Column(
            "updated_at", postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False
        ),
        sa.CheckConstraint(
            "status::text = ANY (ARRAY['ACTIVE'::character varying, 'ARCHIVED'::character varying]::text[])",
            name=op.f("ck_team_shadow_accounts_status"),
        ),
        sa.CheckConstraint(
            "fees_paid >= 0::numeric", name=op.f("ck_team_shadow_accounts_fees_nonnegative")
        ),
        sa.CheckConstraint("generation >= 1", name=op.f("ck_team_shadow_accounts_generation")),
        sa.CheckConstraint(
            "initial_equity = 100000::numeric AND equity >= 0::numeric AND available_balance >= 0::numeric",
            name=op.f("ck_team_shadow_accounts_balances"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_team_shadow_accounts_version")),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.team_id"],
            name=op.f("team_shadow_accounts_team_id_fkey"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("shadow_account_id", name=op.f("team_shadow_accounts_pkey")),
        sa.UniqueConstraint(
            "team_id",
            "generation",
            name=op.f("uq_team_shadow_accounts_generation"),
            postgresql_include=[],
            postgresql_nulls_not_distinct=False,
        ),
    )
    op.create_index(
        op.f("uq_team_shadow_accounts_active"),
        "team_shadow_accounts",
        ["team_id"],
        unique=True,
        postgresql_where="((status)::text = 'ACTIVE'::text)",
    )
    op.create_table(
        "shadow_instruments",
        sa.Column("shadow_instrument_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("team_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("catalog_instrument_id", sa.UUID(), autoincrement=False, nullable=True),
        sa.Column("venue", sa.VARCHAR(length=64), autoincrement=False, nullable=False),
        sa.Column("symbol", sa.VARCHAR(length=120), autoincrement=False, nullable=False),
        sa.Column(
            "price_tick", sa.NUMERIC(precision=38, scale=18), autoincrement=False, nullable=True
        ),
        sa.Column(
            "quantity_step", sa.NUMERIC(precision=38, scale=18), autoincrement=False, nullable=True
        ),
        sa.Column(
            "contract_multiplier",
            sa.NUMERIC(precision=38, scale=18),
            autoincrement=False,
            nullable=True,
        ),
        sa.Column("is_derivative", sa.BOOLEAN(), autoincrement=False, nullable=False),
        sa.Column(
            "latest_price", sa.NUMERIC(precision=38, scale=18), autoincrement=False, nullable=True
        ),
        sa.Column(
            "price_observed_at",
            postgresql.TIMESTAMP(timezone=True),
            autoincrement=False,
            nullable=True,
        ),
        sa.Column("version", sa.INTEGER(), autoincrement=False, nullable=False),
        sa.Column(
            "created_at", postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False
        ),
        sa.Column(
            "updated_at", postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False
        ),
        sa.CheckConstraint(
            "venue::text = ANY (ARRAY['BINANCE'::character varying, 'HYPERLIQUID'::character varying, 'OKX'::character varying, 'BYBIT'::character varying]::text[])",
            name=op.f("ck_shadow_instruments_venue"),
        ),
        sa.CheckConstraint(
            "contract_multiplier IS NULL OR contract_multiplier > 0::numeric",
            name=op.f("ck_shadow_instruments_multiplier"),
        ),
        sa.CheckConstraint(
            "latest_price IS NULL OR latest_price > 0::numeric",
            name=op.f("ck_shadow_instruments_latest_price"),
        ),
        sa.CheckConstraint(
            "price_tick IS NULL OR price_tick > 0::numeric",
            name=op.f("ck_shadow_instruments_price_tick"),
        ),
        sa.CheckConstraint(
            "quantity_step IS NULL OR quantity_step > 0::numeric",
            name=op.f("ck_shadow_instruments_quantity_step"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_shadow_instruments_version")),
        sa.ForeignKeyConstraint(
            ["catalog_instrument_id"],
            ["instruments.instrument_id"],
            name=op.f("shadow_instruments_catalog_instrument_id_fkey"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.team_id"],
            name=op.f("shadow_instruments_team_id_fkey"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("shadow_instrument_id", name=op.f("shadow_instruments_pkey")),
        sa.UniqueConstraint(
            "team_id",
            "venue",
            "symbol",
            name=op.f("uq_shadow_instruments_scope"),
            postgresql_include=[],
            postgresql_nulls_not_distinct=False,
        ),
    )
    op.create_table(
        "shadow_positions",
        sa.Column("shadow_position_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("shadow_account_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("team_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("generation", sa.INTEGER(), autoincrement=False, nullable=False),
        sa.Column("shadow_instrument_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("source_account_id", sa.VARCHAR(length=120), autoincrement=False, nullable=False),
        sa.Column("venue", sa.VARCHAR(length=64), autoincrement=False, nullable=False),
        sa.Column(
            "quantity", sa.NUMERIC(precision=38, scale=18), autoincrement=False, nullable=False
        ),
        sa.Column(
            "average_entry_price",
            sa.NUMERIC(precision=38, scale=18),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column(
            "mark_price", sa.NUMERIC(precision=38, scale=18), autoincrement=False, nullable=False
        ),
        sa.Column(
            "realized_pnl", sa.NUMERIC(precision=38, scale=18), autoincrement=False, nullable=False
        ),
        sa.Column(
            "unrealized_pnl",
            sa.NUMERIC(precision=38, scale=18),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("status", sa.VARCHAR(length=16), autoincrement=False, nullable=False),
        sa.Column("version", sa.INTEGER(), autoincrement=False, nullable=False),
        sa.Column(
            "created_at", postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False
        ),
        sa.Column(
            "updated_at", postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False
        ),
        sa.CheckConstraint(
            "status::text = ANY (ARRAY['OPEN'::character varying, 'CLOSED'::character varying, 'ARCHIVED'::character varying]::text[])",
            name=op.f("ck_shadow_positions_status"),
        ),
        sa.CheckConstraint(
            "average_entry_price >= 0::numeric AND mark_price > 0::numeric",
            name=op.f("ck_shadow_positions_prices"),
        ),
        sa.CheckConstraint("generation >= 1", name=op.f("ck_shadow_positions_generation")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_shadow_positions_version")),
        sa.ForeignKeyConstraint(
            ["shadow_account_id"],
            ["team_shadow_accounts.shadow_account_id"],
            name=op.f("shadow_positions_shadow_account_id_fkey"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["shadow_instrument_id"],
            ["shadow_instruments.shadow_instrument_id"],
            name=op.f("shadow_positions_shadow_instrument_id_fkey"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "source_account_id", "venue"],
            [
                "exchange_accounts.team_id",
                "exchange_accounts.account_id",
                "exchange_accounts.venue",
            ],
            name=op.f("fk_shadow_positions_exchange_account"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.team_id"],
            name=op.f("shadow_positions_team_id_fkey"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("shadow_position_id", name=op.f("shadow_positions_pkey")),
        sa.UniqueConstraint(
            "team_id",
            "generation",
            "shadow_instrument_id",
            name=op.f("uq_shadow_positions_generation_instrument"),
            postgresql_include=[],
            postgresql_nulls_not_distinct=False,
        ),
    )
    op.create_index(
        op.f("ix_shadow_positions_active"),
        "shadow_positions",
        ["team_id", "generation", "status"],
        unique=False,
    )
    op.create_table(
        "shadow_orders",
        sa.Column("shadow_order_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("shadow_account_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("team_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("generation", sa.INTEGER(), autoincrement=False, nullable=False),
        sa.Column("shadow_instrument_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("source_account_id", sa.VARCHAR(length=120), autoincrement=False, nullable=False),
        sa.Column("venue", sa.VARCHAR(length=64), autoincrement=False, nullable=False),
        sa.Column("campaign_id", sa.UUID(), autoincrement=False, nullable=True),
        sa.Column("order_intent_id", sa.UUID(), autoincrement=False, nullable=True),
        sa.Column("shadow_position_id", sa.UUID(), autoincrement=False, nullable=True),
        sa.Column("side", sa.VARCHAR(length=8), autoincrement=False, nullable=False),
        sa.Column("order_type", sa.VARCHAR(length=16), autoincrement=False, nullable=False),
        sa.Column(
            "quantity", sa.NUMERIC(precision=38, scale=18), autoincrement=False, nullable=False
        ),
        sa.Column(
            "limit_price", sa.NUMERIC(precision=38, scale=18), autoincrement=False, nullable=True
        ),
        sa.Column(
            "trigger_price", sa.NUMERIC(precision=38, scale=18), autoincrement=False, nullable=True
        ),
        sa.Column("trigger_type", sa.VARCHAR(length=16), autoincrement=False, nullable=True),
        sa.Column("execution_type", sa.VARCHAR(length=16), autoincrement=False, nullable=True),
        sa.Column("reduce_only", sa.BOOLEAN(), autoincrement=False, nullable=False),
        sa.Column("status", sa.VARCHAR(length=16), autoincrement=False, nullable=False),
        sa.Column(
            "filled_quantity",
            sa.NUMERIC(precision=38, scale=18),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column(
            "fill_price", sa.NUMERIC(precision=38, scale=18), autoincrement=False, nullable=True
        ),
        sa.Column("fee", sa.NUMERIC(precision=38, scale=18), autoincrement=False, nullable=False),
        sa.Column(
            "realized_pnl", sa.NUMERIC(precision=38, scale=18), autoincrement=False, nullable=False
        ),
        sa.Column("correlation_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("idempotency_key", sa.VARCHAR(length=160), autoincrement=False, nullable=False),
        sa.Column("version", sa.INTEGER(), autoincrement=False, nullable=False),
        sa.Column(
            "created_at", postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False
        ),
        sa.Column(
            "updated_at", postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False
        ),
        sa.CheckConstraint(
            "order_type::text = 'LIMIT'::text AND limit_price IS NOT NULL AND limit_price > 0::numeric OR order_type::text = 'MARKET'::text AND limit_price IS NULL OR order_type::text = 'PROTECTION'::text AND trigger_price IS NOT NULL AND (trigger_type::text = ANY (ARRAY['STOP_LOSS'::character varying, 'TAKE_PROFIT'::character varying]::text[])) AND (execution_type::text = ANY (ARRAY['MARKET'::character varying, 'LIMIT'::character varying]::text[])) AND (execution_type::text = 'LIMIT'::text AND limit_price IS NOT NULL AND limit_price > 0::numeric OR execution_type::text = 'MARKET'::text AND limit_price IS NULL)",
            name=op.f("ck_shadow_orders_shape"),
        ),
        sa.CheckConstraint(
            "order_type::text = 'PROTECTION'::text AND reduce_only OR order_type::text <> 'PROTECTION'::text",
            name=op.f("ck_shadow_orders_protection_reduce_only"),
        ),
        sa.CheckConstraint(
            "order_type::text = ANY (ARRAY['MARKET'::character varying, 'LIMIT'::character varying, 'PROTECTION'::character varying]::text[])",
            name=op.f("ck_shadow_orders_type"),
        ),
        sa.CheckConstraint(
            "side::text = ANY (ARRAY['BUY'::character varying, 'SELL'::character varying]::text[])",
            name=op.f("ck_shadow_orders_side"),
        ),
        sa.CheckConstraint(
            "status::text = ANY (ARRAY['OPEN'::character varying, 'TRIGGERED'::character varying, 'FILLED'::character varying, 'CANCELLED'::character varying, 'BLOCKED'::character varying]::text[])",
            name=op.f("ck_shadow_orders_status"),
        ),
        sa.CheckConstraint("fee >= 0::numeric", name=op.f("ck_shadow_orders_fee")),
        sa.CheckConstraint("generation >= 1", name=op.f("ck_shadow_orders_generation")),
        sa.CheckConstraint(
            "quantity > 0::numeric AND filled_quantity >= 0::numeric AND filled_quantity <= quantity",
            name=op.f("ck_shadow_orders_quantities"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_shadow_orders_version")),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.campaign_id"],
            name=op.f("shadow_orders_campaign_id_fkey"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["order_intent_id"],
            ["order_intents.intent_id"],
            name=op.f("shadow_orders_order_intent_id_fkey"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["shadow_account_id"],
            ["team_shadow_accounts.shadow_account_id"],
            name=op.f("shadow_orders_shadow_account_id_fkey"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["shadow_instrument_id"],
            ["shadow_instruments.shadow_instrument_id"],
            name=op.f("shadow_orders_shadow_instrument_id_fkey"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["shadow_position_id"],
            ["shadow_positions.shadow_position_id"],
            name=op.f("shadow_orders_shadow_position_id_fkey"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "source_account_id", "venue"],
            [
                "exchange_accounts.team_id",
                "exchange_accounts.account_id",
                "exchange_accounts.venue",
            ],
            name=op.f("fk_shadow_orders_exchange_account"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.team_id"],
            name=op.f("shadow_orders_team_id_fkey"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("shadow_order_id", name=op.f("shadow_orders_pkey")),
        sa.UniqueConstraint(
            "order_intent_id",
            name=op.f("uq_shadow_orders_intent"),
            postgresql_include=[],
            postgresql_nulls_not_distinct=False,
        ),
        sa.UniqueConstraint(
            "team_id",
            "generation",
            "idempotency_key",
            name=op.f("uq_shadow_orders_idempotency"),
            postgresql_include=[],
            postgresql_nulls_not_distinct=False,
        ),
    )
    op.create_index(
        op.f("ix_shadow_orders_open"),
        "shadow_orders",
        ["team_id", "generation", "status"],
        unique=False,
    )
    op.create_table(
        "shadow_fills",
        sa.Column("shadow_fill_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("shadow_order_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("shadow_account_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("team_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("generation", sa.INTEGER(), autoincrement=False, nullable=False),
        sa.Column("shadow_instrument_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("side", sa.VARCHAR(length=8), autoincrement=False, nullable=False),
        sa.Column(
            "quantity", sa.NUMERIC(precision=38, scale=18), autoincrement=False, nullable=False
        ),
        sa.Column("price", sa.NUMERIC(precision=38, scale=18), autoincrement=False, nullable=False),
        sa.Column(
            "notional", sa.NUMERIC(precision=38, scale=18), autoincrement=False, nullable=False
        ),
        sa.Column("fee", sa.NUMERIC(precision=38, scale=18), autoincrement=False, nullable=False),
        sa.Column(
            "realized_pnl", sa.NUMERIC(precision=38, scale=18), autoincrement=False, nullable=False
        ),
        sa.Column(
            "executed_at", postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False
        ),
        sa.CheckConstraint(
            "side::text = ANY (ARRAY['BUY'::character varying, 'SELL'::character varying]::text[])",
            name=op.f("ck_shadow_fills_side"),
        ),
        sa.CheckConstraint("generation >= 1", name=op.f("ck_shadow_fills_generation")),
        sa.CheckConstraint(
            "quantity > 0::numeric AND price > 0::numeric AND notional > 0::numeric AND fee >= 0::numeric",
            name=op.f("ck_shadow_fills_amounts"),
        ),
        sa.ForeignKeyConstraint(
            ["shadow_account_id"],
            ["team_shadow_accounts.shadow_account_id"],
            name=op.f("shadow_fills_shadow_account_id_fkey"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["shadow_instrument_id"],
            ["shadow_instruments.shadow_instrument_id"],
            name=op.f("shadow_fills_shadow_instrument_id_fkey"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["shadow_order_id"],
            ["shadow_orders.shadow_order_id"],
            name=op.f("shadow_fills_shadow_order_id_fkey"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.team_id"],
            name=op.f("shadow_fills_team_id_fkey"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("shadow_fill_id", name=op.f("shadow_fills_pkey")),
        sa.UniqueConstraint(
            "shadow_order_id",
            name=op.f("uq_shadow_fills_order"),
            postgresql_include=[],
            postgresql_nulls_not_distinct=False,
        ),
    )
    op.create_index(
        op.f("ix_shadow_fills_team_time"),
        "shadow_fills",
        ["team_id", "generation", "executed_at"],
        unique=False,
    )
    # Legacy tables are recreated empty; removed records are intentionally not restored.
