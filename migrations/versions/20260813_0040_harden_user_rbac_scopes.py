"""Harden user RBAC, analytics scopes, and execution environments.

Revision ID: 20260813_0040
Revises: 20260812_0039
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260813_0040"
down_revision: str | None = "20260812_0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_teams_execution_mode", "teams", type_="check")
    op.create_check_constraint(
        "ck_teams_execution_mode",
        "teams",
        "execution_mode IN ('SETUP','SHADOW','TESTNET','LIVE')",
    )

    op.drop_constraint(
        "fk_api_clients_exchange_account_scope",
        "api_clients",
        type_="foreignkey",
    )
    op.drop_index("ix_api_clients_team_scope", table_name="api_clients")
    op.alter_column("api_clients", "account_id", existing_type=sa.String(120), nullable=True)
    op.alter_column("api_clients", "venue", existing_type=sa.String(64), nullable=True)
    op.execute("UPDATE api_clients SET account_id = NULL, venue = NULL")
    op.create_index("ix_api_clients_team", "api_clients", ["team_id"])

    op.add_column(
        "analytics_reports",
        sa.Column(
            "account_scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.alter_column("analytics_reports", "account_scopes", server_default=None)

    # The mode is already semantically fixed for every existing Team. Backfill the
    # lock before the application begins enforcing execution_mode on every call.
    op.execute(
        "UPDATE teams SET execution_mode_locked_at = "
        "COALESCE(execution_mode_locked_at, updated_at) "
        "WHERE execution_mode IN ('LIVE', 'SHADOW')"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE teams SET execution_mode = 'LIVE' "
        "WHERE execution_mode = 'TESTNET'"
    )
    op.drop_constraint("ck_teams_execution_mode", "teams", type_="check")
    op.create_check_constraint(
        "ck_teams_execution_mode",
        "teams",
        "execution_mode IN ('SETUP','SHADOW','LIVE')",
    )

    op.drop_column("analytics_reports", "account_scopes")

    # Reconstruct the former fixed scope from the owner's current Team RBAC. Refuse
    # to downgrade if no exact scope can be selected rather than inventing access.
    op.execute(
        """
        UPDATE api_clients AS client
        SET account_id = (
                SELECT account.account_id
                FROM exchange_accounts AS account
                WHERE account.team_id = client.team_id
                  AND EXISTS (
                      SELECT 1
                      FROM role_assignments AS assignment
                      WHERE assignment.user_id = client.owner_user_id
                        AND assignment.team_id = client.team_id
                        AND (assignment.account_scope IS NULL
                             OR assignment.account_scope = account.account_id)
                        AND (assignment.venue_scope IS NULL
                             OR assignment.venue_scope = account.venue)
                  )
                ORDER BY account.account_id, account.venue
                LIMIT 1
            ),
            venue = (
                SELECT account.venue
                FROM exchange_accounts AS account
                WHERE account.team_id = client.team_id
                  AND EXISTS (
                      SELECT 1
                      FROM role_assignments AS assignment
                      WHERE assignment.user_id = client.owner_user_id
                        AND assignment.team_id = client.team_id
                        AND (assignment.account_scope IS NULL
                             OR assignment.account_scope = account.account_id)
                        AND (assignment.venue_scope IS NULL
                             OR assignment.venue_scope = account.venue)
                  )
                ORDER BY account.account_id, account.venue
                LIMIT 1
            )
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM api_clients WHERE account_id IS NULL OR venue IS NULL
            ) THEN
                RAISE EXCEPTION
                    'cannot restore fixed API client scope without an authorized exchange account';
            END IF;
        END $$
        """
    )
    op.drop_index("ix_api_clients_team", table_name="api_clients")
    op.alter_column("api_clients", "venue", existing_type=sa.String(64), nullable=False)
    op.alter_column("api_clients", "account_id", existing_type=sa.String(120), nullable=False)
    op.create_foreign_key(
        "fk_api_clients_exchange_account_scope",
        "api_clients",
        "exchange_accounts",
        ["team_id", "account_id", "venue"],
        ["team_id", "account_id", "venue"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_api_clients_team_scope",
        "api_clients",
        ["team_id", "account_id", "venue", "state"],
    )
