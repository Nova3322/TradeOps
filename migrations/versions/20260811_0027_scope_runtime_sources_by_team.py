"""Scope runtime feeds, source health, and account bindings by team.

Revision ID: 20260811_0027
Revises: 20260811_0026
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_0027"
down_revision: str | None = "20260811_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _default_team_id(connection: sa.Connection) -> object | None:
    return connection.execute(
        sa.text(
            "SELECT t.team_id FROM teams t JOIN workspaces w "
            "ON w.workspace_id = t.workspace_id "
            "WHERE w.slug = 'default' AND t.slug = 'default' "
            "ORDER BY t.created_at, t.team_id LIMIT 1"
        )
    ).scalar_one_or_none()


def _backfill_perptape_team(connection: sa.Connection) -> None:
    conflict = connection.execute(
        sa.text(
            "SELECT feed.feed_key FROM perptape_feeds feed "
            "JOIN audit_events event ON event.object_type = 'PerptapeFeed' "
            "AND event.object_id = feed.feed_key WHERE event.team_id IS NOT NULL "
            "GROUP BY feed.feed_key HAVING count(DISTINCT event.team_id) > 1 LIMIT 1"
        )
    ).scalar_one_or_none()
    if conflict is not None:
        raise RuntimeError(
            f"0027 found cross-team Perptape feed history for {conflict}; "
            "ownership must be resolved before migration"
        )
    connection.execute(
        sa.text(
            "UPDATE perptape_feeds feed SET team_id = owner.team_id FROM ("
            "SELECT event.object_id, min(event.team_id::text)::uuid AS team_id "
            "FROM audit_events event WHERE event.object_type = 'PerptapeFeed' "
            "AND event.team_id IS NOT NULL GROUP BY event.object_id"
            ") owner WHERE owner.object_id = feed.feed_key AND feed.team_id IS NULL"
        )
    )


def _backfill_runtime_scope(connection: sa.Connection) -> None:
    connection.execute(
        sa.text(
            "UPDATE runtime_source_health health SET team_id = principal.active_team_id "
            "FROM users principal WHERE principal.user_id = health.updated_by "
            "AND principal.active_team_id IS NOT NULL AND health.team_id IS NULL"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE runtime_source_health health SET account_id = candidate.account_id, "
            "venue = health.source_name FROM ("
            "SELECT team_id, venue, min(account_id) AS account_id FROM exchange_accounts "
            "WHERE active GROUP BY team_id, venue HAVING count(*) = 1"
            ") candidate WHERE candidate.team_id = health.team_id "
            "AND candidate.venue = health.source_name "
            "AND health.source_name IN ('BINANCE','HYPERLIQUID','OKX','BYBIT')"
        )
    )
    rows = connection.execute(
        sa.text(
            "SELECT source_name FROM runtime_source_health "
            "WHERE runtime_source_health_id IS NULL"
        )
    ).scalars()
    for source_name in rows:
        connection.execute(
            sa.text(
                "UPDATE runtime_source_health SET runtime_source_health_id = :identity "
                "WHERE source_name = :source_name AND runtime_source_health_id IS NULL"
            ),
            {"identity": uuid4(), "source_name": source_name},
        )


def _fill_missing_team(connection: sa.Connection) -> None:
    default_team_id = _default_team_id(connection)
    missing = {
        table: int(
            connection.execute(
                sa.text(f"SELECT count(*) FROM {table} WHERE team_id IS NULL")  # noqa: S608
            ).scalar_one()
        )
        for table in ("perptape_feeds", "runtime_source_health")
    }
    if not any(missing.values()):
        return
    if default_team_id is None:
        raise RuntimeError(f"0027 cannot backfill runtime team scope: {missing}")
    for table, count in missing.items():
        if count:
            connection.execute(
                sa.text(
                    f"UPDATE {table} SET team_id = :team_id "  # noqa: S608
                    "WHERE team_id IS NULL"
                ),
                {"team_id": default_team_id},
            )


def upgrade() -> None:
    op.add_column(
        "exchange_accounts",
        sa.Column("runtime_sync_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "exchange_accounts",
        sa.Column("runtime_service_principal_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_exchange_accounts_runtime_service_principal",
        "exchange_accounts",
        "users",
        ["runtime_service_principal_id"],
        ["user_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_exchange_accounts_runtime_sync_ready",
        "exchange_accounts",
        "NOT runtime_sync_enabled OR (active AND connection_status = 'VERIFIED' "
        "AND credential_version >= 1 AND venue IN ('BINANCE','HYPERLIQUID') "
        "AND runtime_service_principal_id IS NOT NULL)",
    )
    op.create_index(
        "ix_exchange_accounts_runtime_sync",
        "exchange_accounts",
        ["team_id", "runtime_sync_enabled", "venue"],
    )
    op.alter_column(
        "exchange_accounts",
        "runtime_sync_enabled",
        existing_type=sa.Boolean(),
        server_default=None,
    )

    op.add_column("perptape_feeds", sa.Column("team_id", sa.Uuid(), nullable=True))
    op.add_column(
        "runtime_source_health",
        sa.Column("runtime_source_health_id", sa.Uuid(), nullable=True),
    )
    op.add_column("runtime_source_health", sa.Column("team_id", sa.Uuid(), nullable=True))
    op.add_column(
        "runtime_source_health", sa.Column("account_id", sa.String(length=120), nullable=True)
    )
    op.add_column(
        "runtime_source_health", sa.Column("venue", sa.String(length=64), nullable=True)
    )

    connection = op.get_bind()
    _backfill_perptape_team(connection)
    _backfill_runtime_scope(connection)
    _fill_missing_team(connection)

    op.drop_constraint("perptape_feeds_pkey", "perptape_feeds", type_="primary")
    op.create_primary_key("pk_perptape_feeds", "perptape_feeds", ["team_id", "feed_key"])
    op.alter_column("perptape_feeds", "team_id", existing_type=sa.Uuid(), nullable=False)
    op.create_foreign_key(
        "fk_perptape_feeds_team",
        "perptape_feeds",
        "teams",
        ["team_id"],
        ["team_id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("runtime_source_health_pkey", "runtime_source_health", type_="primary")
    op.alter_column(
        "runtime_source_health",
        "runtime_source_health_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
    op.alter_column(
        "runtime_source_health", "team_id", existing_type=sa.Uuid(), nullable=False
    )
    op.create_primary_key(
        "pk_runtime_source_health",
        "runtime_source_health",
        ["runtime_source_health_id"],
    )
    op.create_foreign_key(
        "fk_runtime_source_health_team",
        "runtime_source_health",
        "teams",
        ["team_id"],
        ["team_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_runtime_source_health_team_exchange_account",
        "runtime_source_health",
        "exchange_accounts",
        ["team_id", "account_id", "venue"],
        ["team_id", "account_id", "venue"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_runtime_source_health_scope",
        "runtime_source_health",
        ["team_id", "source_name", "account_id", "venue"],
        postgresql_nulls_not_distinct=True,
    )
    op.create_check_constraint(
        "ck_runtime_source_health_account_scope",
        "runtime_source_health",
        "(account_id IS NULL AND venue IS NULL) OR "
        "(account_id IS NOT NULL AND venue IN "
        "('BINANCE','HYPERLIQUID','OKX','BYBIT'))",
    )
    op.create_index(
        "ix_runtime_source_health_team_checked",
        "runtime_source_health",
        ["team_id", "checked_at"],
    )

    connection.execute(
        sa.text(
            "UPDATE audit_events event SET team_id = feed.team_id, "
            "workspace_id = team.workspace_id FROM perptape_feeds feed "
            "JOIN teams team ON team.team_id = feed.team_id "
            "WHERE event.object_type = 'PerptapeFeed' AND event.object_id = feed.feed_key"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE users principal SET active = false, auth_version = auth_version + 1 "
            "WHERE principal.principal_type = 'SERVICE' "
            "AND principal.service_kind = 'INTERNAL' AND principal.active "
            "AND principal.username LIKE 'signal-%' AND NOT EXISTS ("
            "SELECT 1 FROM team_signal_sources source "
            "WHERE source.service_principal_id = principal.user_id "
            "AND source.enabled AND source.mode = 'PERPTAPE'"
            ")"
        )
    )


def _guard_downgrade(connection: sa.Connection) -> None:
    if connection.execute(
        sa.text("SELECT 1 FROM exchange_accounts WHERE runtime_sync_enabled LIMIT 1")
    ).first() is not None:
        raise RuntimeError("0027 downgrade requires all database runtime bindings disabled")
    conflicts = {
        "Perptape feed": (
            "SELECT feed_key FROM perptape_feeds GROUP BY feed_key HAVING count(*) > 1 LIMIT 1"
        ),
        "runtime source": (
            "SELECT source_name FROM runtime_source_health GROUP BY source_name "
            "HAVING count(*) > 1 LIMIT 1"
        ),
    }
    blocked = [
        label
        for label, statement in conflicts.items()
        if connection.execute(sa.text(statement)).first() is not None
    ]
    if blocked:
        raise RuntimeError(
            "0027 downgrade cannot represent team-scoped duplicates: " + ", ".join(blocked)
        )


def downgrade() -> None:
    connection = op.get_bind()
    _guard_downgrade(connection)

    op.drop_index(
        "ix_runtime_source_health_team_checked", table_name="runtime_source_health"
    )
    op.drop_constraint(
        "ck_runtime_source_health_account_scope",
        "runtime_source_health",
        type_="check",
    )
    op.drop_constraint(
        "uq_runtime_source_health_scope",
        "runtime_source_health",
        type_="unique",
    )
    op.drop_constraint(
        "fk_runtime_source_health_team_exchange_account",
        "runtime_source_health",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_runtime_source_health_team", "runtime_source_health", type_="foreignkey"
    )
    op.drop_constraint("pk_runtime_source_health", "runtime_source_health", type_="primary")
    op.drop_column("runtime_source_health", "venue")
    op.drop_column("runtime_source_health", "account_id")
    op.drop_column("runtime_source_health", "team_id")
    op.drop_column("runtime_source_health", "runtime_source_health_id")
    op.create_primary_key(
        "runtime_source_health_pkey", "runtime_source_health", ["source_name"]
    )

    op.drop_constraint("fk_perptape_feeds_team", "perptape_feeds", type_="foreignkey")
    op.drop_constraint("pk_perptape_feeds", "perptape_feeds", type_="primary")
    op.drop_column("perptape_feeds", "team_id")
    op.create_primary_key("perptape_feeds_pkey", "perptape_feeds", ["feed_key"])

    op.drop_index("ix_exchange_accounts_runtime_sync", table_name="exchange_accounts")
    op.drop_constraint(
        "ck_exchange_accounts_runtime_sync_ready", "exchange_accounts", type_="check"
    )
    op.drop_constraint(
        "fk_exchange_accounts_runtime_service_principal",
        "exchange_accounts",
        type_="foreignkey",
    )
    op.drop_column("exchange_accounts", "runtime_service_principal_id")
    op.drop_column("exchange_accounts", "runtime_sync_enabled")
