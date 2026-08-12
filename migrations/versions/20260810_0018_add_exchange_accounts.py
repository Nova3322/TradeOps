"""Add team-scoped exchange account custody boundaries.

Revision ID: 20260810_0018
Revises: 20260810_0017
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260810_0018"
down_revision: str | None = "20260810_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SUPPORTED_VENUES = ("BINANCE", "HYPERLIQUID", "OKX", "BYBIT")


def _existing_account_scopes(connection: sa.Connection) -> set[tuple[object, str, str]]:
    columns_by_table: dict[str, set[str]] = {}
    for table_name, column_name in connection.execute(
        sa.text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema()"
        )
    ):
        columns_by_table.setdefault(str(table_name), set()).add(str(column_name))
    default_team_id = connection.execute(
        sa.text(
            "SELECT t.team_id FROM teams t JOIN workspaces w "
            "ON w.workspace_id = t.workspace_id "
            "WHERE w.slug = 'default' AND t.slug = 'default' "
            "ORDER BY t.created_at, t.team_id LIMIT 1"
        )
    ).scalar_one_or_none()
    quote = connection.dialect.identifier_preparer.quote
    values: set[tuple[object, str, str]] = set()
    for table_name, columns in columns_by_table.items():
        if table_name == "exchange_accounts" or not {"account_id", "venue"} <= columns:
            continue
        team_expression = quote("team_id") if "team_id" in columns else ":default_team_id"
        rows = connection.execute(
            sa.text(
                f"SELECT {team_expression}, account_id, upper(venue) "  # noqa: S608
                f"FROM {quote(table_name)} WHERE account_id IS NOT NULL "
                "AND btrim(account_id) <> '' AND venue IS NOT NULL"
            ),
            {"default_team_id": default_team_id},
        ).all()
        if default_team_id is None and "team_id" not in columns and rows:
            raise RuntimeError(
                f"0018 cannot assign unscoped {table_name} account facts without a default team"
            )
        for team_id, account_id, venue in rows:
            if team_id is None:
                continue
            normalized_venue = str(venue)
            if table_name == "proposals" and normalized_venue not in SUPPORTED_VENUES:
                raise RuntimeError(
                    f"0018 found unsupported proposal exchange venue {normalized_venue!r}"
                )
            if normalized_venue in SUPPORTED_VENUES:
                values.add((team_id, str(account_id), normalized_venue))
    return values


def upgrade() -> None:
    op.create_table(
        "exchange_accounts",
        sa.Column("exchange_account_id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.String(120), nullable=False),
        sa.Column("venue", sa.String(64), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("registration_source", sa.String(32), nullable=False),
        sa.Column("connection_status", sa.String(32), nullable=False),
        sa.Column("trading_status", sa.String(16), nullable=False),
        sa.Column("credentials_ciphertext", sa.Text(), nullable=True),
        sa.Column(
            "credential_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("connection_error_code", sa.String(120), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "venue IN ('BINANCE','HYPERLIQUID','OKX','BYBIT')",
            name="ck_exchange_accounts_venue",
        ),
        sa.CheckConstraint(
            "connection_status IN ('UNCONFIGURED','NOT_VERIFIED','VERIFIED','FAILED','STALE')",
            name="ck_exchange_accounts_connection_status",
        ),
        sa.CheckConstraint(
            "trading_status IN ('DISABLED','BLOCKED','ELIGIBLE')",
            name="ck_exchange_accounts_trading_status",
        ),
        sa.CheckConstraint(
            "registration_source IN ('MIGRATION','MANUAL','WORKFLOW_REFERENCE')",
            name="ck_exchange_accounts_registration_source",
        ),
        sa.CheckConstraint("version >= 1", name="ck_exchange_accounts_version"),
        sa.CheckConstraint(
            "credential_version >= 0", name="ck_exchange_accounts_credential_version"
        ),
        sa.CheckConstraint(
            "(credentials_ciphertext IS NULL AND credential_version = 0) OR "
            "(credentials_ciphertext IS NOT NULL AND credential_version >= 1)",
            name="ck_exchange_accounts_credential_envelope",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["team_id"], ["teams.team_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("exchange_account_id"),
        sa.UniqueConstraint(
            "team_id",
            "account_id",
            "venue",
            name="uq_exchange_accounts_team_account_venue",
        ),
    )
    op.create_index("ix_exchange_accounts_team_active", "exchange_accounts", ["team_id", "active"])

    connection = op.get_bind()
    creators = {
        team_id: created_by
        for team_id, created_by in connection.execute(
            sa.text("SELECT team_id, created_by FROM teams")
        )
    }
    now = datetime.now(UTC)
    for team_id, account_id, venue in sorted(
        _existing_account_scopes(connection), key=lambda item: (str(item[0]), item[2], item[1])
    ):
        creator = creators.get(team_id)
        if creator is None:
            raise RuntimeError("0018 found an account scope without an owning team")
        connection.execute(
            sa.text(
                "INSERT INTO exchange_accounts "
                "(exchange_account_id, team_id, account_id, venue, label, "
                "registration_source, connection_status, trading_status, "
                "credentials_ciphertext, credential_metadata, credential_version, "
                "connection_error_code, last_verified_at, active, version, "
                "created_by, updated_by, created_at, updated_at) VALUES "
                "(:exchange_account_id, :team_id, :account_id, :venue, :label, "
                "'MIGRATION', 'UNCONFIGURED', 'DISABLED', NULL, "
                "CAST(:credential_metadata AS jsonb), 0, NULL, NULL, true, 1, "
                ":creator, :creator, :created_at, :created_at)"
            ),
            {
                "exchange_account_id": uuid4(),
                "team_id": team_id,
                "account_id": account_id,
                "venue": venue,
                "label": account_id,
                "credential_metadata": json.dumps({}),
                "creator": creator,
                "created_at": now,
            },
        )

    op.create_foreign_key(
        "fk_proposals_team_exchange_account",
        "proposals",
        "exchange_accounts",
        ["team_id", "account_id", "venue"],
        ["team_id", "account_id", "venue"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_proposals_team_exchange_account", "proposals", type_="foreignkey")
    op.drop_index("ix_exchange_accounts_team_active", table_name="exchange_accounts")
    op.drop_table("exchange_accounts")
