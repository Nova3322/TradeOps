"""Scope versioned risk policy and reservations by team.

Revision ID: 20260810_0020
Revises: 20260810_0019
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "20260810_0020"
down_revision: str | None = "20260810_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _default_team_id(connection: sa.Connection) -> object | None:
    team_id = connection.execute(
        sa.text(
            "SELECT t.team_id FROM teams t JOIN workspaces w "
            "ON w.workspace_id = t.workspace_id "
            "WHERE w.slug = 'default' AND t.slug = 'default' "
            "ORDER BY t.created_at, t.team_id LIMIT 1"
        )
    ).scalar_one_or_none()
    return team_id


def _clone_legacy_policies(connection: sa.Connection, default_team_id: object) -> None:
    policies = connection.execute(
        sa.text(
            "SELECT policy_id, version, revision, system_state, max_total_risk, "
            "max_fact_age_seconds, reason, active, updated_by, updated_at "
            "FROM risk_policies WHERE team_id = :team_id ORDER BY revision"
        ),
        {"team_id": default_team_id},
    ).mappings().all()
    active_policy = next((item for item in policies if item["active"]), None)
    if active_policy is None:
        raise RuntimeError("0020 requires one active legacy risk policy")

    teams = connection.execute(
        sa.text("SELECT team_id FROM teams WHERE team_id <> :team_id ORDER BY team_id"),
        {"team_id": default_team_id},
    ).scalars().all()
    for team_id in teams:
        referenced = set(
            connection.execute(
                sa.text("SELECT DISTINCT policy_id FROM risk_decisions WHERE team_id = :team_id"),
                {"team_id": team_id},
            ).scalars()
        )
        source_rows = [item for item in policies if item["policy_id"] in referenced]
        if active_policy["policy_id"] not in referenced:
            source_rows.append(active_policy)
        mapping: dict[object, object] = {}
        for source in source_rows:
            cloned_id = uuid4()
            mapping[source["policy_id"]] = cloned_id
            connection.execute(
                sa.text(
                    "INSERT INTO risk_policies "
                    "(policy_id, team_id, version, revision, system_state, max_total_risk, "
                    "max_account_risk, max_single_loss, max_consecutive_losses, "
                    "loss_cooldown_seconds, max_fact_age_seconds, reason, active, updated_by, "
                    "updated_at) VALUES (:policy_id, :team_id, :version, :revision, "
                    ":system_state, :max_total_risk, NULL, NULL, NULL, NULL, "
                    ":max_fact_age_seconds, :reason, :active, :updated_by, :updated_at)"
                ),
                {
                    **source,
                    "policy_id": cloned_id,
                    "team_id": team_id,
                },
            )
        for source_id, cloned_id in mapping.items():
            connection.execute(
                sa.text(
                    "UPDATE risk_decisions SET policy_id = :cloned_id "
                    "WHERE team_id = :team_id AND policy_id = :source_id"
                ),
                {"cloned_id": cloned_id, "team_id": team_id, "source_id": source_id},
            )


def upgrade() -> None:
    connection = op.get_bind()
    default_team_id = _default_team_id(connection)

    op.add_column("risk_policies", sa.Column("team_id", sa.Uuid(), nullable=True))
    op.add_column(
        "risk_policies", sa.Column("max_account_risk", sa.Numeric(38, 18), nullable=True)
    )
    op.add_column(
        "risk_policies", sa.Column("max_single_loss", sa.Numeric(38, 18), nullable=True)
    )
    op.add_column("risk_policies", sa.Column("max_consecutive_losses", sa.Integer()))
    op.add_column("risk_policies", sa.Column("loss_cooldown_seconds", sa.Integer()))
    policy_count = int(
        connection.execute(sa.text("SELECT count(*) FROM risk_policies")).scalar_one()
    )
    if policy_count:
        if default_team_id is None:
            raise RuntimeError("0020 requires the migrated default team for legacy policies")
        connection.execute(
            sa.text("UPDATE risk_policies SET team_id = :team_id WHERE team_id IS NULL"),
            {"team_id": default_team_id},
        )
        _clone_legacy_policies(connection, default_team_id)
    op.alter_column("risk_policies", "team_id", existing_type=sa.Uuid(), nullable=False)
    op.create_foreign_key(
        "fk_risk_policies_team",
        "risk_policies",
        "teams",
        ["team_id"],
        ["team_id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint("risk_policies_version_key", "risk_policies", type_="unique")
    op.create_unique_constraint(
        "uq_risk_policies_team_identity", "risk_policies", ["team_id", "policy_id"]
    )
    op.create_unique_constraint(
        "uq_risk_policies_team_version", "risk_policies", ["team_id", "version"]
    )
    op.create_unique_constraint(
        "uq_risk_policies_team_revision", "risk_policies", ["team_id", "revision"]
    )
    op.create_check_constraint(
        "ck_risk_policies_account_risk_positive", "risk_policies", "max_account_risk > 0"
    )
    op.create_check_constraint(
        "ck_risk_policies_single_loss_positive", "risk_policies", "max_single_loss > 0"
    )
    op.create_check_constraint(
        "ck_risk_policies_consecutive_losses_positive",
        "risk_policies",
        "max_consecutive_losses > 0",
    )
    op.create_check_constraint(
        "ck_risk_policies_loss_cooldown_positive",
        "risk_policies",
        "loss_cooldown_seconds > 0",
    )
    op.create_check_constraint(
        "ck_risk_policies_limits_all_or_none",
        "risk_policies",
        "(max_account_risk IS NULL AND max_single_loss IS NULL "
        "AND max_consecutive_losses IS NULL AND loss_cooldown_seconds IS NULL) OR "
        "(max_account_risk IS NOT NULL AND max_single_loss IS NOT NULL "
        "AND max_consecutive_losses IS NOT NULL AND loss_cooldown_seconds IS NOT NULL)",
    )
    op.drop_index("uq_risk_policies_one_active", table_name="risk_policies")
    op.create_index(
        "uq_risk_policies_one_active",
        "risk_policies",
        ["team_id", "active"],
        unique=True,
        postgresql_where=sa.text("active"),
    )

    op.add_column(
        "risk_control_change_requests", sa.Column("team_id", sa.Uuid(), nullable=True)
    )
    connection.execute(
        sa.text(
            "UPDATE risk_control_change_requests request SET team_id = policy.team_id "
            "FROM risk_policies policy WHERE request.source_policy_id = policy.policy_id"
        )
    )
    op.alter_column(
        "risk_control_change_requests", "team_id", existing_type=sa.Uuid(), nullable=False
    )
    op.create_foreign_key(
        "fk_risk_control_change_requests_team",
        "risk_control_change_requests",
        "teams",
        ["team_id"],
        ["team_id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "risk_control_change_requests_source_policy_id_fkey",
        "risk_control_change_requests",
        type_="foreignkey",
    )
    op.drop_constraint(
        "risk_control_change_requests_resulting_policy_id_fkey",
        "risk_control_change_requests",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_risk_control_change_requests_team_source_policy",
        "risk_control_change_requests",
        "risk_policies",
        ["team_id", "source_policy_id"],
        ["team_id", "policy_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_risk_control_change_requests_team_result_policy",
        "risk_control_change_requests",
        "risk_policies",
        ["team_id", "resulting_policy_id"],
        ["team_id", "policy_id"],
        ondelete="RESTRICT",
    )
    op.drop_index(
        "uq_risk_control_change_requests_pending",
        table_name="risk_control_change_requests",
    )
    op.create_index(
        "uq_risk_control_change_requests_pending",
        "risk_control_change_requests",
        ["team_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING_REVIEW','APPROVED')"),
    )

    op.drop_constraint("risk_decisions_policy_id_fkey", "risk_decisions", type_="foreignkey")
    op.create_foreign_key(
        "fk_risk_decisions_team_policy",
        "risk_decisions",
        "risk_policies",
        ["team_id", "policy_id"],
        ["team_id", "policy_id"],
        ondelete="RESTRICT",
    )

    op.create_unique_constraint(
        "uq_campaigns_team_identity", "campaigns", ["team_id", "campaign_id"]
    )
    op.add_column("risk_reservations", sa.Column("team_id", sa.Uuid(), nullable=True))
    connection.execute(
        sa.text(
            "UPDATE risk_reservations reservation SET team_id = campaign.team_id "
            "FROM campaigns campaign WHERE reservation.campaign_id = campaign.campaign_id"
        )
    )
    op.alter_column("risk_reservations", "team_id", existing_type=sa.Uuid(), nullable=False)
    op.create_foreign_key(
        "fk_risk_reservations_team",
        "risk_reservations",
        "teams",
        ["team_id"],
        ["team_id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "risk_reservations_campaign_id_fkey", "risk_reservations", type_="foreignkey"
    )
    op.drop_constraint(
        "risk_reservations_authorization_id_fkey", "risk_reservations", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_risk_reservations_team_campaign",
        "risk_reservations",
        "campaigns",
        ["team_id", "campaign_id"],
        ["team_id", "campaign_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_risk_reservations_team_authorization",
        "risk_reservations",
        "trading_authorizations",
        ["team_id", "authorization_id"],
        ["team_id", "authorization_id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_risk_reservations_team_status", "risk_reservations", ["team_id", "status"]
    )


def downgrade() -> None:
    connection = op.get_bind()
    default_team_id = _default_team_id(connection)

    op.drop_index("ix_risk_reservations_team_status", table_name="risk_reservations")
    op.drop_constraint(
        "fk_risk_reservations_team_authorization", "risk_reservations", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_risk_reservations_team_campaign", "risk_reservations", type_="foreignkey"
    )
    op.create_foreign_key(
        "risk_reservations_authorization_id_fkey",
        "risk_reservations",
        "trading_authorizations",
        ["authorization_id"],
        ["authorization_id"],
    )
    op.create_foreign_key(
        "risk_reservations_campaign_id_fkey",
        "risk_reservations",
        "campaigns",
        ["campaign_id"],
        ["campaign_id"],
    )
    op.drop_constraint("fk_risk_reservations_team", "risk_reservations", type_="foreignkey")
    op.drop_column("risk_reservations", "team_id")
    op.drop_constraint("uq_campaigns_team_identity", "campaigns", type_="unique")

    op.drop_constraint("fk_risk_decisions_team_policy", "risk_decisions", type_="foreignkey")
    if default_team_id is not None:
        connection.execute(
            sa.text(
                "UPDATE risk_decisions decision SET policy_id = legacy.policy_id "
                "FROM risk_policies current_policy JOIN risk_policies legacy "
                "ON legacy.team_id = :default_team_id "
                "AND legacy.version = current_policy.version "
                "AND legacy.revision = current_policy.revision "
                "WHERE decision.policy_id = current_policy.policy_id "
                "AND current_policy.team_id <> :default_team_id"
            ),
            {"default_team_id": default_team_id},
        )
    op.create_foreign_key(
        "risk_decisions_policy_id_fkey",
        "risk_decisions",
        "risk_policies",
        ["policy_id"],
        ["policy_id"],
    )

    op.drop_index(
        "uq_risk_control_change_requests_pending",
        table_name="risk_control_change_requests",
    )
    op.create_index(
        "uq_risk_control_change_requests_pending",
        "risk_control_change_requests",
        [sa.text("(true)")],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING_REVIEW','APPROVED')"),
    )
    op.drop_constraint(
        "fk_risk_control_change_requests_team_result_policy",
        "risk_control_change_requests",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_risk_control_change_requests_team_source_policy",
        "risk_control_change_requests",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "risk_control_change_requests_resulting_policy_id_fkey",
        "risk_control_change_requests",
        "risk_policies",
        ["resulting_policy_id"],
        ["policy_id"],
    )
    op.create_foreign_key(
        "risk_control_change_requests_source_policy_id_fkey",
        "risk_control_change_requests",
        "risk_policies",
        ["source_policy_id"],
        ["policy_id"],
    )
    op.drop_constraint(
        "fk_risk_control_change_requests_team",
        "risk_control_change_requests",
        type_="foreignkey",
    )
    op.drop_column("risk_control_change_requests", "team_id")

    if default_team_id is not None:
        connection.execute(
            sa.text("DELETE FROM risk_policies WHERE team_id <> :default_team_id"),
            {"default_team_id": default_team_id},
        )
    op.drop_index("uq_risk_policies_one_active", table_name="risk_policies")
    op.create_index(
        "uq_risk_policies_one_active",
        "risk_policies",
        ["active"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.drop_constraint("ck_risk_policies_limits_all_or_none", "risk_policies", type_="check")
    op.drop_constraint(
        "ck_risk_policies_loss_cooldown_positive", "risk_policies", type_="check"
    )
    op.drop_constraint(
        "ck_risk_policies_consecutive_losses_positive", "risk_policies", type_="check"
    )
    op.drop_constraint(
        "ck_risk_policies_single_loss_positive", "risk_policies", type_="check"
    )
    op.drop_constraint(
        "ck_risk_policies_account_risk_positive", "risk_policies", type_="check"
    )
    op.drop_constraint("uq_risk_policies_team_revision", "risk_policies", type_="unique")
    op.drop_constraint("uq_risk_policies_team_version", "risk_policies", type_="unique")
    op.drop_constraint("uq_risk_policies_team_identity", "risk_policies", type_="unique")
    op.create_unique_constraint("risk_policies_version_key", "risk_policies", ["version"])
    op.drop_constraint("fk_risk_policies_team", "risk_policies", type_="foreignkey")
    op.drop_column("risk_policies", "loss_cooldown_seconds")
    op.drop_column("risk_policies", "max_consecutive_losses")
    op.drop_column("risk_policies", "max_single_loss")
    op.drop_column("risk_policies", "max_account_risk")
    op.drop_column("risk_policies", "team_id")
