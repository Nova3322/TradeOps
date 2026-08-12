"""Scope capital workflow roots and sender leases by team.

Revision ID: 20260811_0026
Revises: 20260810_0025
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_0026"
down_revision: str | None = "20260810_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SCOPED_TABLES = (
    "transfer_proposals",
    "transfer_authorizations",
    "capital_transfers",
    "direct_capital_configurations",
    "direct_capital_operations",
    "capital_automation_policies",
    "sender_leases",
)

AUDIT_ROOTS = (
    ("transfer_proposals", "transfer_proposal_id", "TransferProposal"),
    ("direct_capital_configurations", "config_id", "DirectCapitalConfiguration"),
    ("direct_capital_operations", "operation_id", "DirectCapitalOperation"),
    ("capital_automation_policies", "policy_id", "CapitalAutomationPolicy"),
    ("sender_leases", "execution_scope", "SenderLease"),
)


def _default_team_id(connection: sa.Connection) -> object | None:
    return connection.execute(
        sa.text(
            "SELECT t.team_id FROM teams t JOIN workspaces w "
            "ON w.workspace_id = t.workspace_id "
            "WHERE w.slug = 'default' AND t.slug = 'default' "
            "ORDER BY t.created_at, t.team_id LIMIT 1"
        )
    ).scalar_one_or_none()


def _backfill_audited_root(
    connection: sa.Connection,
    *,
    table_name: str,
    id_column: str,
    object_type: str,
) -> None:
    quote = connection.dialect.identifier_preparer.quote
    table = quote(table_name)
    identity = quote(id_column)
    ambiguous = connection.execute(
        sa.text(
            f"SELECT root.{identity} FROM {table} root "  # noqa: S608
            "JOIN audit_events event ON event.object_type = :object_type "
            f"AND event.object_id = root.{identity}::text "
            "WHERE event.team_id IS NOT NULL "
            f"GROUP BY root.{identity} HAVING count(DISTINCT event.team_id) > 1 "
            "LIMIT 1"
        ),
        {"object_type": object_type},
    ).scalar_one_or_none()
    if ambiguous is not None:
        raise RuntimeError(
            f"0026 found cross-team audit history for {object_type} {ambiguous}; "
            "ownership must be resolved before migration"
        )
    connection.execute(
        sa.text(
            f"UPDATE {table} root SET team_id = owner.team_id FROM ("  # noqa: S608
            "SELECT event.object_id, min(event.team_id::text)::uuid AS team_id "
            "FROM audit_events event WHERE event.object_type = :object_type "
            "AND event.team_id IS NOT NULL GROUP BY event.object_id"
            f") owner WHERE owner.object_id = root.{identity}::text "
            "AND root.team_id IS NULL"
        ),
        {"object_type": object_type},
    )


def _backfill_team_ids(connection: sa.Connection) -> None:
    for table_name, id_column, object_type in AUDIT_ROOTS:
        _backfill_audited_root(
            connection,
            table_name=table_name,
            id_column=id_column,
            object_type=object_type,
        )

    connection.execute(
        sa.text(
            "UPDATE transfer_authorizations auth "
            "SET team_id = proposal.team_id FROM transfer_proposals proposal "
            "WHERE auth.transfer_proposal_id = proposal.transfer_proposal_id "
            "AND auth.team_id IS NULL"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE capital_transfers capital SET team_id = auth.team_id "
            "FROM transfer_authorizations auth "
            "WHERE capital.transfer_authorization_id = "
            "auth.transfer_authorization_id AND capital.team_id IS NULL"
        )
    )

    default_team_id = _default_team_id(connection)
    unresolved = {
        table_name: int(
            connection.execute(
                sa.text(f"SELECT count(*) FROM {table_name} WHERE team_id IS NULL")  # noqa: S608
            ).scalar_one()
        )
        for table_name in SCOPED_TABLES
    }
    if any(unresolved.values()):
        if default_team_id is None:
            raise RuntimeError(
                f"0026 cannot backfill team scope without a default team: {unresolved}"
            )
        for table_name, count in unresolved.items():
            if count:
                connection.execute(
                    sa.text(
                        f"UPDATE {table_name} SET team_id = :team_id "  # noqa: S608
                        "WHERE team_id IS NULL"
                    ),
                    {"team_id": default_team_id},
                )

    for table_name, id_column, object_type in (
        ("transfer_authorizations", "transfer_authorization_id", "TransferAuthorization"),
        ("capital_transfers", "capital_transfer_id", "CapitalTransfer"),
    ):
        conflict = connection.execute(
            sa.text(
                f"SELECT root.{id_column} FROM {table_name} root "  # noqa: S608
                "JOIN audit_events event ON event.object_type = :object_type "
                f"AND event.object_id = root.{id_column}::text "
                "WHERE event.team_id IS NOT NULL AND event.team_id <> root.team_id LIMIT 1"
            ),
            {"object_type": object_type},
        ).scalar_one_or_none()
        if conflict is not None:
            raise RuntimeError(
                f"0026 found audit/team lineage conflict for {object_type} {conflict}"
            )


def _validate_account_roots(connection: sa.Connection) -> None:
    for table_name in (
        "transfer_proposals",
        "capital_transfers",
        "direct_capital_operations",
        "capital_automation_policies",
    ):
        missing = connection.execute(
            sa.text(
                f"SELECT root.account_id FROM {table_name} root "  # noqa: S608
                "LEFT JOIN exchange_accounts account ON account.team_id = root.team_id "
                "AND account.account_id = root.account_id AND account.venue = root.venue "
                "WHERE root.account_id IS NOT NULL AND account.exchange_account_id IS NULL LIMIT 1"
            )
        ).scalar_one_or_none()
        if missing is not None:
            raise RuntimeError(
                f"0026 found {table_name} outside its team exchange-account root: {missing}"
            )


def _backfill_audit_scope(connection: sa.Connection) -> None:
    mappings = (
        ("TransferProposal", "transfer_proposals", "transfer_proposal_id", "account_id"),
        (
            "TransferAuthorization",
            "transfer_authorizations",
            "transfer_authorization_id",
            "account_id",
        ),
        ("CapitalTransfer", "capital_transfers", "capital_transfer_id", "account_id"),
        (
            "DirectCapitalConfiguration",
            "direct_capital_configurations",
            "config_id",
            None,
        ),
        ("DirectCapitalOperation", "direct_capital_operations", "operation_id", "account_id"),
        ("CapitalAutomationPolicy", "capital_automation_policies", "policy_id", "account_id"),
    )
    for object_type, table_name, id_column, account_column in mappings:
        account_assignment = (
            "" if account_column is None else f", account_id = root.{account_column}"
        )
        connection.execute(
            sa.text(
                "UPDATE audit_events event SET team_id = root.team_id, "  # noqa: S608
                "workspace_id = team.workspace_id"
                f"{account_assignment} FROM {table_name} root "
                "JOIN teams team ON team.team_id = root.team_id "
                "WHERE event.object_type = :object_type "
                f"AND event.object_id = root.{id_column}::text"
            ),
            {"object_type": object_type},
        )
    connection.execute(
        sa.text(
            "UPDATE audit_events event SET team_id = lease.team_id, "
            "workspace_id = team.workspace_id, account_id = CASE "
            "WHEN array_length(string_to_array(lease.execution_scope, ':'), 1) = 2 "
            "THEN split_part(lease.execution_scope, ':', 1) "
            "ELSE split_part(lease.execution_scope, ':', 2) END "
            "FROM sender_leases lease JOIN teams team ON team.team_id = lease.team_id "
            "WHERE event.object_type = 'SenderLease' "
            "AND event.object_id = lease.execution_scope"
        )
    )


def upgrade() -> None:
    for table_name in SCOPED_TABLES:
        op.add_column(table_name, sa.Column("team_id", sa.Uuid(), nullable=True))

    connection = op.get_bind()
    _backfill_team_ids(connection)
    _validate_account_roots(connection)

    op.drop_constraint(
        "transfer_authorizations_transfer_proposal_id_fkey",
        "transfer_authorizations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "capital_transfers_transfer_authorization_id_fkey",
        "capital_transfers",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_transfer_authorizations_proposal",
        "transfer_authorizations",
        type_="unique",
    )
    op.drop_constraint(
        "uq_capital_transfers_authorization",
        "capital_transfers",
        type_="unique",
    )
    op.drop_constraint(
        "uq_direct_capital_configurations_version",
        "direct_capital_configurations",
        type_="unique",
    )
    op.drop_index(
        "uq_direct_capital_configuration_active",
        table_name="direct_capital_configurations",
    )
    op.drop_constraint(
        "uq_capital_automation_policies_scope",
        "capital_automation_policies",
        type_="unique",
    )
    op.drop_index("ix_transfer_proposals_status_expires", table_name="transfer_proposals")
    op.drop_index("ix_capital_transfers_status_updated", table_name="capital_transfers")
    op.drop_index("ix_direct_capital_operations_updated", table_name="direct_capital_operations")
    op.drop_constraint("sender_leases_pkey", "sender_leases", type_="primary")

    for table_name in SCOPED_TABLES:
        op.alter_column(table_name, "team_id", existing_type=sa.Uuid(), nullable=False)
        op.create_foreign_key(
            f"fk_{table_name}_team",
            table_name,
            "teams",
            ["team_id"],
            ["team_id"],
            ondelete="RESTRICT",
        )

    op.create_unique_constraint(
        "uq_transfer_proposals_team_identity",
        "transfer_proposals",
        ["team_id", "transfer_proposal_id"],
    )
    op.create_foreign_key(
        "fk_transfer_proposals_team_exchange_account",
        "transfer_proposals",
        "exchange_accounts",
        ["team_id", "account_id", "venue"],
        ["team_id", "account_id", "venue"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_transfer_proposals_status_expires",
        "transfer_proposals",
        ["team_id", "status", "expires_at"],
    )

    op.create_unique_constraint(
        "uq_transfer_authorizations_team_identity",
        "transfer_authorizations",
        ["team_id", "transfer_authorization_id"],
    )
    op.create_unique_constraint(
        "uq_transfer_authorizations_proposal",
        "transfer_authorizations",
        ["team_id", "transfer_proposal_id"],
    )
    op.create_foreign_key(
        "fk_transfer_authorizations_team_proposal",
        "transfer_authorizations",
        "transfer_proposals",
        ["team_id", "transfer_proposal_id"],
        ["team_id", "transfer_proposal_id"],
        ondelete="RESTRICT",
    )

    op.create_unique_constraint(
        "uq_capital_transfers_authorization",
        "capital_transfers",
        ["team_id", "transfer_authorization_id"],
    )
    op.create_foreign_key(
        "fk_capital_transfers_team_authorization",
        "capital_transfers",
        "transfer_authorizations",
        ["team_id", "transfer_authorization_id"],
        ["team_id", "transfer_authorization_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_capital_transfers_team_exchange_account",
        "capital_transfers",
        "exchange_accounts",
        ["team_id", "account_id", "venue"],
        ["team_id", "account_id", "venue"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_capital_transfers_status_updated",
        "capital_transfers",
        ["team_id", "status", "updated_at"],
    )

    op.create_unique_constraint(
        "uq_direct_capital_configurations_version",
        "direct_capital_configurations",
        ["team_id", "version"],
    )
    op.create_index(
        "uq_direct_capital_configuration_active",
        "direct_capital_configurations",
        ["team_id", "active"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_foreign_key(
        "fk_direct_capital_operations_team_exchange_account",
        "direct_capital_operations",
        "exchange_accounts",
        ["team_id", "account_id", "venue"],
        ["team_id", "account_id", "venue"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_direct_capital_operations_updated",
        "direct_capital_operations",
        ["team_id", "updated_at"],
    )
    op.create_unique_constraint(
        "uq_capital_automation_policies_scope",
        "capital_automation_policies",
        ["team_id", "environment", "account_id", "venue", "asset"],
    )
    op.create_foreign_key(
        "fk_capital_automation_policies_team_exchange_account",
        "capital_automation_policies",
        "exchange_accounts",
        ["team_id", "account_id", "venue"],
        ["team_id", "account_id", "venue"],
        ondelete="RESTRICT",
    )
    op.create_primary_key(
        "pk_sender_leases",
        "sender_leases",
        ["team_id", "execution_scope"],
    )

    _backfill_audit_scope(connection)


def _guard_downgrade(connection: sa.Connection) -> None:
    duplicate_checks = {
        "sender lease scope": (
            "SELECT execution_scope FROM sender_leases GROUP BY execution_scope "
            "HAVING count(*) > 1 LIMIT 1"
        ),
        "direct capital version": (
            "SELECT version FROM direct_capital_configurations GROUP BY version "
            "HAVING count(*) > 1 LIMIT 1"
        ),
        "active direct capital configuration": (
            "SELECT true FROM direct_capital_configurations WHERE active "
            "GROUP BY active HAVING count(*) > 1 LIMIT 1"
        ),
        "capital automation scope": (
            "SELECT environment, account_id, venue, asset FROM capital_automation_policies "
            "GROUP BY environment, account_id, venue, asset HAVING count(*) > 1 LIMIT 1"
        ),
    }
    conflicts = [
        label
        for label, statement in duplicate_checks.items()
        if connection.execute(sa.text(statement)).first() is not None
    ]
    if conflicts:
        raise RuntimeError(
            "0026 downgrade cannot represent team-scoped duplicates: " + ", ".join(conflicts)
        )


def downgrade() -> None:
    _guard_downgrade(op.get_bind())

    op.drop_constraint("pk_sender_leases", "sender_leases", type_="primary")
    op.drop_constraint(
        "fk_capital_automation_policies_team_exchange_account",
        "capital_automation_policies",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_capital_automation_policies_scope",
        "capital_automation_policies",
        type_="unique",
    )
    op.drop_index("ix_direct_capital_operations_updated", table_name="direct_capital_operations")
    op.drop_constraint(
        "fk_direct_capital_operations_team_exchange_account",
        "direct_capital_operations",
        type_="foreignkey",
    )
    op.drop_index(
        "uq_direct_capital_configuration_active",
        table_name="direct_capital_configurations",
    )
    op.drop_constraint(
        "uq_direct_capital_configurations_version",
        "direct_capital_configurations",
        type_="unique",
    )
    op.drop_index("ix_capital_transfers_status_updated", table_name="capital_transfers")
    op.drop_constraint(
        "fk_capital_transfers_team_exchange_account",
        "capital_transfers",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_capital_transfers_team_authorization",
        "capital_transfers",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_capital_transfers_authorization",
        "capital_transfers",
        type_="unique",
    )
    op.drop_constraint(
        "fk_transfer_authorizations_team_proposal",
        "transfer_authorizations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_transfer_authorizations_proposal",
        "transfer_authorizations",
        type_="unique",
    )
    op.drop_constraint(
        "uq_transfer_authorizations_team_identity",
        "transfer_authorizations",
        type_="unique",
    )
    op.drop_index("ix_transfer_proposals_status_expires", table_name="transfer_proposals")
    op.drop_constraint(
        "fk_transfer_proposals_team_exchange_account",
        "transfer_proposals",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_transfer_proposals_team_identity",
        "transfer_proposals",
        type_="unique",
    )

    for table_name in reversed(SCOPED_TABLES):
        op.drop_constraint(f"fk_{table_name}_team", table_name, type_="foreignkey")
        op.drop_column(table_name, "team_id")

    op.create_foreign_key(
        "transfer_authorizations_transfer_proposal_id_fkey",
        "transfer_authorizations",
        "transfer_proposals",
        ["transfer_proposal_id"],
        ["transfer_proposal_id"],
    )
    op.create_unique_constraint(
        "uq_transfer_authorizations_proposal",
        "transfer_authorizations",
        ["transfer_proposal_id"],
    )
    op.create_foreign_key(
        "capital_transfers_transfer_authorization_id_fkey",
        "capital_transfers",
        "transfer_authorizations",
        ["transfer_authorization_id"],
        ["transfer_authorization_id"],
    )
    op.create_unique_constraint(
        "uq_capital_transfers_authorization",
        "capital_transfers",
        ["transfer_authorization_id"],
    )
    op.create_unique_constraint(
        "uq_direct_capital_configurations_version",
        "direct_capital_configurations",
        ["version"],
    )
    op.create_index(
        "uq_direct_capital_configuration_active",
        "direct_capital_configurations",
        ["active"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_unique_constraint(
        "uq_capital_automation_policies_scope",
        "capital_automation_policies",
        ["environment", "account_id", "venue", "asset"],
    )
    op.create_index(
        "ix_transfer_proposals_status_expires",
        "transfer_proposals",
        ["status", "expires_at"],
    )
    op.create_index(
        "ix_capital_transfers_status_updated",
        "capital_transfers",
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_direct_capital_operations_updated",
        "direct_capital_operations",
        ["updated_at"],
    )
    op.create_primary_key("sender_leases_pkey", "sender_leases", ["execution_scope"])
