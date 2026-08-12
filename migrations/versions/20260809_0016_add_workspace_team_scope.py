"""add workspace and team permission scope

Revision ID: 20260809_0016
Revises: 20260809_0015
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "20260809_0016"
down_revision: str | None = "20260809_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_workspaces_version"),
        sa.ForeignKeyConstraint(["created_by"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("workspace_id"),
        sa.UniqueConstraint("slug", name="uq_workspaces_slug"),
    )
    op.create_table(
        "teams",
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("trading_enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_teams_version"),
        sa.ForeignKeyConstraint(["created_by"], ["users.user_id"]),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.workspace_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("team_id"),
        sa.UniqueConstraint("workspace_id", "slug", name="uq_teams_workspace_slug"),
    )
    op.create_index("ix_teams_workspace_active", "teams", ["workspace_id", "active"])

    op.add_column("users", sa.Column("active_workspace_id", sa.Uuid(), nullable=True))
    op.add_column("users", sa.Column("active_team_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_users_active_workspace_id_workspaces",
        "users",
        "workspaces",
        ["active_workspace_id"],
        ["workspace_id"],
    )
    op.create_foreign_key(
        "fk_users_active_team_id_teams",
        "users",
        "teams",
        ["active_team_id"],
        ["team_id"],
    )

    op.create_table(
        "workspace_memberships",
        sa.Column("membership_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("invited_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('MEMBER','ADMIN')", name="ck_workspace_memberships_role"),
        sa.ForeignKeyConstraint(["invited_by"], ["users.user_id"]),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.user_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.workspace_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("membership_id"),
        sa.UniqueConstraint(
            "workspace_id", "user_id", name="uq_workspace_memberships_workspace_user"
        ),
    )
    op.create_index(
        "ix_workspace_memberships_user_active",
        "workspace_memberships",
        ["user_id", "active"],
    )
    op.create_table(
        "team_memberships",
        sa.Column("membership_id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("invited_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["invited_by"], ["users.user_id"]),
        sa.ForeignKeyConstraint(
            ["team_id"], ["teams.team_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.user_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("membership_id"),
        sa.UniqueConstraint("team_id", "user_id", name="uq_team_memberships_team_user"),
    )
    op.create_index(
        "ix_team_memberships_user_active",
        "team_memberships",
        ["user_id", "active"],
    )

    op.add_column("role_assignments", sa.Column("team_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_role_assignments_team_id_teams",
        "role_assignments",
        "teams",
        ["team_id"],
        ["team_id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_role_assignments_team_user",
        "role_assignments",
        ["team_id", "user_id"],
    )

    op.add_column("audit_events", sa.Column("workspace_id", sa.Uuid(), nullable=True))
    op.add_column("audit_events", sa.Column("team_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_audit_events_workspace_id_workspaces",
        "audit_events",
        "workspaces",
        ["workspace_id"],
        ["workspace_id"],
    )
    op.create_foreign_key(
        "fk_audit_events_team_id_teams",
        "audit_events",
        "teams",
        ["team_id"],
        ["team_id"],
    )
    op.create_index(
        "ix_audit_events_workspace_created",
        "audit_events",
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_audit_events_team_created",
        "audit_events",
        ["team_id", "created_at"],
    )

    connection = op.get_bind()
    creator_id = connection.execute(
        sa.text(
            "SELECT u.user_id FROM users u "
            "LEFT JOIN role_assignments r ON r.user_id = u.user_id "
            "ORDER BY (r.role = 'SYSTEM_ADMIN') DESC, u.created_at, u.user_id LIMIT 1"
        )
    ).scalar_one_or_none()
    if creator_id is not None:
        workspace_id = uuid4()
        team_id = uuid4()
        now = datetime.now(UTC)
        connection.execute(
            sa.text(
                "INSERT INTO workspaces "
                "(workspace_id, name, slug, created_by, active, version, created_at, updated_at) "
                "VALUES (:workspace_id, 'Default Workspace', 'default', :creator_id, "
                "true, 1, :now, :now)"
            ),
            {"workspace_id": workspace_id, "creator_id": creator_id, "now": now},
        )
        connection.execute(
            sa.text(
                "INSERT INTO teams "
                "(team_id, workspace_id, name, slug, created_by, active, trading_enabled, "
                "version, created_at, updated_at) "
                "VALUES (:team_id, :workspace_id, 'Default Team', 'default', :creator_id, "
                "true, true, 1, :now, :now)"
            ),
            {
                "team_id": team_id,
                "workspace_id": workspace_id,
                "creator_id": creator_id,
                "now": now,
            },
        )
        users = connection.execute(sa.text("SELECT user_id, active FROM users")).all()
        for user_id, active in users:
            connection.execute(
                sa.text(
                    "INSERT INTO workspace_memberships "
                    "(membership_id, workspace_id, user_id, role, active, invited_by, "
                    "created_at, updated_at) "
                    "VALUES (:membership_id, :workspace_id, :user_id, :role, :active, "
                    ":creator_id, :now, :now)"
                ),
                {
                    "membership_id": uuid4(),
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "role": "ADMIN" if user_id == creator_id else "MEMBER",
                    "active": active,
                    "creator_id": creator_id,
                    "now": now,
                },
            )
            connection.execute(
                sa.text(
                    "INSERT INTO team_memberships "
                    "(membership_id, team_id, user_id, active, invited_by, created_at, updated_at) "
                    "VALUES (:membership_id, :team_id, :user_id, :active, :creator_id, :now, :now)"
                ),
                {
                    "membership_id": uuid4(),
                    "team_id": team_id,
                    "user_id": user_id,
                    "active": active,
                    "creator_id": creator_id,
                    "now": now,
                },
            )
        connection.execute(
            sa.text(
                "UPDATE users SET active_workspace_id = :workspace_id, active_team_id = :team_id"
            ),
            {"workspace_id": workspace_id, "team_id": team_id},
        )
        connection.execute(
            sa.text("UPDATE role_assignments SET team_id = :team_id"),
            {"team_id": team_id},
        )
        connection.execute(
            sa.text(
                "UPDATE audit_events SET workspace_id = :workspace_id, team_id = :team_id"
            ),
            {"workspace_id": workspace_id, "team_id": team_id},
        )

    op.alter_column("role_assignments", "team_id", nullable=False)


def downgrade() -> None:
    op.drop_index("ix_audit_events_team_created", table_name="audit_events")
    op.drop_index("ix_audit_events_workspace_created", table_name="audit_events")
    op.drop_constraint(
        "fk_audit_events_team_id_teams", "audit_events", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_audit_events_workspace_id_workspaces", "audit_events", type_="foreignkey"
    )
    op.drop_column("audit_events", "team_id")
    op.drop_column("audit_events", "workspace_id")

    op.drop_index("ix_role_assignments_team_user", table_name="role_assignments")
    op.drop_constraint(
        "fk_role_assignments_team_id_teams", "role_assignments", type_="foreignkey"
    )
    op.drop_column("role_assignments", "team_id")

    op.drop_index("ix_team_memberships_user_active", table_name="team_memberships")
    op.drop_table("team_memberships")
    op.drop_index(
        "ix_workspace_memberships_user_active", table_name="workspace_memberships"
    )
    op.drop_table("workspace_memberships")

    op.drop_constraint("fk_users_active_team_id_teams", "users", type_="foreignkey")
    op.drop_constraint(
        "fk_users_active_workspace_id_workspaces", "users", type_="foreignkey"
    )
    op.drop_column("users", "active_team_id")
    op.drop_column("users", "active_workspace_id")

    op.drop_index("ix_teams_workspace_active", table_name="teams")
    op.drop_table("teams")
    op.drop_table("workspaces")
