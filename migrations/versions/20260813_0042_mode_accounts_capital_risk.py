"""Unify mode-scoped accounts, capital configuration, notifications, and risk changes.

Revision ID: 20260813_0042
Revises: 20260813_0041
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260813_0042"
down_revision: str | None = "20260813_0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "exchange_accounts",
        sa.Column("environment", sa.String(length=16), nullable=False, server_default="LIVE"),
    )
    op.create_check_constraint(
        "ck_exchange_accounts_environment",
        "exchange_accounts",
        "environment IN ('SHADOW','TESTNET','LIVE')",
    )
    op.create_index(
        "ix_exchange_accounts_team_environment_active",
        "exchange_accounts",
        ["team_id", "environment", "active"],
        unique=False,
    )

    op.add_column(
        "direct_capital_configurations",
        sa.Column("environment", sa.String(length=16), nullable=False, server_default="LIVE"),
    )
    op.add_column(
        "direct_capital_configurations",
        sa.Column("vault_withdrawal_key_ciphertext", sa.Text(), nullable=True),
    )
    op.add_column(
        "direct_capital_configurations",
        sa.Column(
            "vault_withdrawal_key_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "direct_capital_configurations",
        sa.Column("vault_withdrawal_key_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "direct_capital_configurations",
        sa.Column("safe_withdrawal_key_ciphertext", sa.Text(), nullable=True),
    )
    op.add_column(
        "direct_capital_configurations",
        sa.Column(
            "safe_withdrawal_key_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "direct_capital_configurations",
        sa.Column("safe_withdrawal_key_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_direct_capital_configuration_environment",
        "direct_capital_configurations",
        "environment IN ('SHADOW','LIVE')",
    )
    op.create_check_constraint(
        "ck_direct_capital_configuration_vault_key",
        "direct_capital_configurations",
        "(vault_withdrawal_key_ciphertext IS NULL AND vault_withdrawal_key_version = 0) OR "
        "(vault_withdrawal_key_ciphertext IS NOT NULL AND vault_withdrawal_key_version >= 1)",
    )
    op.create_check_constraint(
        "ck_direct_capital_configuration_safe_key",
        "direct_capital_configurations",
        "(safe_withdrawal_key_ciphertext IS NULL AND safe_withdrawal_key_version = 0) OR "
        "(safe_withdrawal_key_ciphertext IS NOT NULL AND safe_withdrawal_key_version >= 1)",
    )
    op.drop_constraint(
        "uq_direct_capital_configurations_version",
        "direct_capital_configurations",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_direct_capital_configurations_environment_version",
        "direct_capital_configurations",
        ["team_id", "environment", "version"],
    )
    op.drop_index(
        "uq_direct_capital_configuration_active",
        table_name="direct_capital_configurations",
    )
    op.create_index(
        "uq_direct_capital_configuration_active",
        "direct_capital_configurations",
        ["team_id", "environment", "active"],
        unique=True,
        postgresql_where=sa.text("active"),
    )

    op.add_column(
        "notification_routes",
        sa.Column("environment", sa.String(length=16), nullable=False, server_default="LIVE"),
    )
    op.create_check_constraint(
        "ck_notification_routes_environment",
        "notification_routes",
        "environment IN ('SHADOW','LIVE')",
    )
    op.drop_index(
        "uq_notification_routes_team_active_name",
        table_name="notification_routes",
    )
    op.create_index(
        "uq_notification_routes_team_active_name",
        "notification_routes",
        ["team_id", "environment", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.add_column(
        "risk_control_change_requests",
        sa.Column(
            "change_type",
            sa.String(length=32),
            nullable=False,
            server_default="RESUME_NEW_RISK",
        ),
    )
    op.add_column(
        "risk_control_change_requests",
        sa.Column(
            "requested_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_risk_control_change_requests_change_type",
        "risk_control_change_requests",
        "change_type IN ('POLICY_UPDATE','DISABLE_AUTO_ADD','ENABLE_AUTO_ADD',"
        "'PAUSE_NEW_RISK','RESUME_NEW_RISK')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_risk_control_change_requests_change_type",
        "risk_control_change_requests",
        type_="check",
    )
    op.drop_column("risk_control_change_requests", "requested_policy")
    op.drop_column("risk_control_change_requests", "change_type")

    op.drop_index("uq_notification_routes_team_active_name", table_name="notification_routes")
    op.execute(
        "UPDATE notification_routes SET name = LEFT(name, 100) || ' [shadow]' "
        "WHERE environment = 'SHADOW' AND deleted_at IS NULL"
    )
    op.create_index(
        "uq_notification_routes_team_active_name",
        "notification_routes",
        ["team_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_constraint(
        "ck_notification_routes_environment", "notification_routes", type_="check"
    )
    op.drop_column("notification_routes", "environment")

    op.execute(
        "UPDATE direct_capital_configurations SET active = false "
        "WHERE environment = 'SHADOW' AND active"
    )
    op.drop_index(
        "uq_direct_capital_configuration_active",
        table_name="direct_capital_configurations",
    )
    op.create_index(
        "uq_direct_capital_configuration_active",
        "direct_capital_configurations",
        ["team_id", "active"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.drop_constraint(
        "uq_direct_capital_configurations_environment_version",
        "direct_capital_configurations",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_direct_capital_configurations_version",
        "direct_capital_configurations",
        ["team_id", "version"],
    )
    op.drop_constraint(
        "ck_direct_capital_configuration_safe_key",
        "direct_capital_configurations",
        type_="check",
    )
    op.drop_constraint(
        "ck_direct_capital_configuration_vault_key",
        "direct_capital_configurations",
        type_="check",
    )
    op.drop_constraint(
        "ck_direct_capital_configuration_environment",
        "direct_capital_configurations",
        type_="check",
    )
    op.drop_column("direct_capital_configurations", "safe_withdrawal_key_version")
    op.drop_column("direct_capital_configurations", "safe_withdrawal_key_metadata")
    op.drop_column("direct_capital_configurations", "safe_withdrawal_key_ciphertext")
    op.drop_column("direct_capital_configurations", "vault_withdrawal_key_version")
    op.drop_column("direct_capital_configurations", "vault_withdrawal_key_metadata")
    op.drop_column("direct_capital_configurations", "vault_withdrawal_key_ciphertext")
    op.drop_column("direct_capital_configurations", "environment")

    op.drop_index(
        "ix_exchange_accounts_team_environment_active", table_name="exchange_accounts"
    )
    op.drop_constraint("ck_exchange_accounts_environment", "exchange_accounts", type_="check")
    op.drop_column("exchange_accounts", "environment")
