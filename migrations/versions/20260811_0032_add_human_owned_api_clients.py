"""Replace role-bearing Agent users with HUMAN-owned API Clients.

Revision ID: 20260811_0032
Revises: 20260811_0031
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_0032"
down_revision: str | None = "20260811_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_clients",
        sa.Column("api_client_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.String(length=120), nullable=False),
        sa.Column("venue", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("token_hint", sa.String(length=32), nullable=False),
        sa.Column("token_version", sa.Integer(), nullable=False),
        sa.Column("token_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("token_last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('ACTIVE','DISABLED','REVOKED')",
            name="ck_api_clients_state",
        ),
        sa.CheckConstraint("token_version >= 1", name="ck_api_clients_token_version"),
        sa.CheckConstraint("version >= 1", name="ck_api_clients_version"),
        sa.CheckConstraint(
            "(state = 'REVOKED' AND revoked_at IS NOT NULL) OR "
            "(state <> 'REVOKED' AND revoked_at IS NULL)",
            name="ck_api_clients_revocation_shape",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.user_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.workspace_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.team_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "account_id", "venue"],
            [
                "exchange_accounts.team_id",
                "exchange_accounts.account_id",
                "exchange_accounts.venue",
            ],
            name="fk_api_clients_exchange_account_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("api_client_id"),
        sa.UniqueConstraint("owner_user_id", "name", name="uq_api_clients_owner_name"),
    )
    op.create_index(
        "ix_api_clients_owner_state",
        "api_clients",
        ["owner_user_id", "state"],
        unique=False,
    )
    op.create_index(
        "ix_api_clients_team_scope",
        "api_clients",
        ["team_id", "account_id", "venue"],
        unique=False,
    )

    # Existing AGENT service principals retain their UUID so already-issued opaque
    # credentials keep rotating through the same independent client identifier.
    op.execute(
        """
        INSERT INTO api_clients (
            api_client_id, owner_user_id, name, workspace_id, team_id, account_id, venue,
            state, token_digest, token_hint, token_version, token_created_at,
            token_expires_at, token_last_used_at, revoked_at, version, created_at, updated_at
        )
        SELECT principal.user_id, owner.user_id, principal.username,
               principal.active_workspace_id, principal.active_team_id,
               scoped.account_scope, scoped.venue_scope,
               CASE WHEN principal.active AND owner.active AND wm.active AND tm.active
                    THEN 'ACTIVE' ELSE 'DISABLED' END,
               principal.agent_token_digest, principal.agent_token_hint,
               principal.agent_token_version, principal.agent_token_created_at,
               principal.agent_token_expires_at, principal.agent_token_last_used_at,
               NULL, GREATEST(principal.auth_version, 1), principal.created_at, now()
        FROM users principal
        JOIN workspace_memberships wm
          ON wm.user_id = principal.user_id
         AND wm.workspace_id = principal.active_workspace_id
        JOIN team_memberships tm
          ON tm.user_id = principal.user_id
         AND tm.team_id = principal.active_team_id
        JOIN users owner ON owner.user_id = COALESCE(tm.invited_by, wm.invited_by)
                        AND owner.principal_type = 'HUMAN'
        JOIN LATERAL (
            SELECT assignment.account_scope, assignment.venue_scope
            FROM role_assignments assignment
            WHERE assignment.user_id = principal.user_id
              AND assignment.team_id = principal.active_team_id
              AND assignment.account_scope IS NOT NULL
              AND assignment.venue_scope IS NOT NULL
            ORDER BY assignment.created_at, assignment.assignment_id
            LIMIT 1
        ) scoped ON TRUE
        JOIN teams team ON team.team_id = principal.active_team_id
                       AND team.workspace_id = principal.active_workspace_id
        JOIN exchange_accounts account
          ON account.team_id = principal.active_team_id
         AND account.account_id = scoped.account_scope
         AND account.venue = scoped.venue_scope
        WHERE principal.principal_type = 'SERVICE'
          AND principal.service_kind = 'AGENT'
        """
    )

    op.add_column("audit_events", sa.Column("api_client_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_audit_events_api_client_id",
        "audit_events",
        "api_clients",
        ["api_client_id"],
        ["api_client_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_audit_events_api_client_created",
        "audit_events",
        ["api_client_id", "created_at"],
        unique=False,
    )
    op.execute(
        """
        UPDATE audit_events event
        SET api_client_id = client.api_client_id,
            actor_id = client.owner_user_id::text
        FROM api_clients client
        WHERE event.actor_id = client.api_client_id::text
        """
    )

    # The HUMAN and all of their API Clients are one approval subject. Historical
    # Agent-created proposals are attributed to the inferred owner, self-approvals
    # are removed, and duplicate votes collapse to one HUMAN vote.
    op.execute(
        """
        UPDATE proposals proposal
        SET proposer_id = client.owner_user_id
        FROM api_clients client
        WHERE proposal.proposer_id = client.api_client_id
        """
    )
    op.execute(
        """
        DELETE FROM approvals approval
        USING api_clients client, proposals proposal
        WHERE approval.reviewer_id = client.api_client_id
          AND approval.proposal_id = proposal.proposal_id
          AND proposal.proposer_id = client.owner_user_id
        """
    )
    op.execute(
        """
        WITH normalized AS (
            SELECT approval.approval_id,
                   row_number() OVER (
                       PARTITION BY COALESCE(client.owner_user_id, approval.reviewer_id),
                                    approval.proposal_id,
                                    approval.transfer_proposal_id,
                                    approval.risk_control_change_request_id
                       ORDER BY CASE WHEN client.api_client_id IS NULL THEN 0 ELSE 1 END,
                                approval.created_at,
                                approval.approval_id
                   ) AS ordinal
            FROM approvals approval
            LEFT JOIN api_clients client ON client.api_client_id = approval.reviewer_id
        )
        DELETE FROM approvals approval
        USING normalized
        WHERE approval.approval_id = normalized.approval_id
          AND normalized.ordinal > 1
        """
    )
    op.execute(
        """
        UPDATE approvals approval
        SET reviewer_id = client.owner_user_id
        FROM api_clients client
        WHERE approval.reviewer_id = client.api_client_id
        """
    )

    op.execute(
        """
        UPDATE users principal
        SET active = false, auth_version = auth_version + 1
        WHERE EXISTS (
            SELECT 1 FROM api_clients client
            WHERE client.api_client_id = principal.user_id
        )
        """
    )
    op.execute(
        """
        UPDATE workspace_memberships membership
        SET active = false, updated_at = now()
        WHERE EXISTS (
            SELECT 1 FROM api_clients client
            WHERE client.api_client_id = membership.user_id
        )
        """
    )
    op.execute(
        """
        UPDATE team_memberships membership
        SET active = false, updated_at = now()
        WHERE EXISTS (
            SELECT 1 FROM api_clients client
            WHERE client.api_client_id = membership.user_id
        )
        """
    )
    op.execute(
        """
        DELETE FROM role_assignments assignment
        WHERE EXISTS (
            SELECT 1 FROM api_clients client
            WHERE client.api_client_id = assignment.user_id
        )
        """
    )


def downgrade() -> None:
    connection = op.get_bind()
    native_count = connection.scalar(
        sa.text(
            "SELECT count(*) FROM api_clients client "
            "WHERE NOT EXISTS (SELECT 1 FROM users principal "
            "WHERE principal.user_id = client.api_client_id "
            "AND principal.service_kind = 'AGENT')"
        )
    )
    if native_count:
        raise RuntimeError("0032 downgrade requires native API Clients to be revoked and removed")

    op.execute(
        """
        UPDATE audit_events event
        SET actor_id = event.api_client_id::text
        WHERE event.api_client_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE users principal
        SET active = (client.state = 'ACTIVE'),
            auth_version = auth_version + 1,
            agent_token_digest = client.token_digest,
            agent_token_hint = client.token_hint,
            agent_token_version = client.token_version,
            agent_token_created_at = client.token_created_at,
            agent_token_expires_at = client.token_expires_at,
            agent_token_last_used_at = client.token_last_used_at
        FROM api_clients client
        WHERE principal.user_id = client.api_client_id
        """
    )
    op.execute(
        """
        INSERT INTO role_assignments (
            assignment_id, user_id, team_id, role, account_scope, venue_scope, created_at
        )
        SELECT md5(
                   client.api_client_id::text || ':' || owner_role.role || ':' ||
                   client.account_id || ':' || client.venue
               )::uuid,
               client.api_client_id, client.team_id, owner_role.role,
               client.account_id, client.venue, now()
        FROM api_clients client
        JOIN role_assignments owner_role
          ON owner_role.user_id = client.owner_user_id
         AND owner_role.team_id = client.team_id
         AND owner_role.role IN ('OBSERVER', 'PROPOSER', 'REVIEWER')
         AND (owner_role.account_scope IS NULL
              OR owner_role.account_scope = client.account_id)
         AND (owner_role.venue_scope IS NULL
              OR owner_role.venue_scope = client.venue)
        WHERE NOT EXISTS (
            SELECT 1 FROM role_assignments existing
            WHERE existing.user_id = client.api_client_id
              AND existing.team_id = client.team_id
              AND existing.role = owner_role.role
              AND existing.account_scope = client.account_id
              AND existing.venue_scope = client.venue
        )
        """
    )
    op.execute(
        """
        UPDATE workspace_memberships membership
        SET active = (client.state = 'ACTIVE'), updated_at = now()
        FROM api_clients client
        WHERE membership.user_id = client.api_client_id
        """
    )
    op.execute(
        """
        UPDATE team_memberships membership
        SET active = (client.state = 'ACTIVE'), updated_at = now()
        FROM api_clients client
        WHERE membership.user_id = client.api_client_id
        """
    )

    op.drop_index("ix_audit_events_api_client_created", table_name="audit_events")
    op.drop_constraint("fk_audit_events_api_client_id", "audit_events", type_="foreignkey")
    op.drop_column("audit_events", "api_client_id")
    op.drop_index("ix_api_clients_team_scope", table_name="api_clients")
    op.drop_index("ix_api_clients_owner_state", table_name="api_clients")
    op.drop_table("api_clients")
