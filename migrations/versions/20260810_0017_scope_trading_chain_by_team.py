"""Scope the proposal-to-execution chain by team.

Revision ID: 20260810_0017
Revises: 20260809_0016
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260810_0017"
down_revision: str | None = "20260809_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SCOPED_TABLES = (
    "proposals",
    "proposal_default_configs",
    "risk_decisions",
    "trading_authorizations",
    "campaigns",
)


def upgrade() -> None:
    for table_name in SCOPED_TABLES:
        op.add_column(table_name, sa.Column("team_id", sa.Uuid(), nullable=True))
    op.add_column(
        "audit_events",
        sa.Column("account_id", sa.String(length=120), nullable=True),
    )

    connection = op.get_bind()
    default_team_id = connection.execute(
        sa.text(
            "SELECT t.team_id FROM teams t "
            "JOIN workspaces w ON w.workspace_id = t.workspace_id "
            "WHERE w.slug = 'default' AND t.slug = 'default' "
            "ORDER BY t.created_at, t.team_id LIMIT 1"
        )
    ).scalar_one_or_none()
    if default_team_id is None:
        legacy_rows = sum(
            connection.execute(sa.text(statement)).scalar_one()
            for statement in (
                "SELECT count(*) FROM proposals",
                "SELECT count(*) FROM proposal_default_configs",
                "SELECT count(*) FROM risk_decisions",
                "SELECT count(*) FROM trading_authorizations",
                "SELECT count(*) FROM campaigns",
            )
        )
        if legacy_rows:
            raise RuntimeError("0017 requires the 0016 default team backfill")
    else:
        # New teams created by 0016 remain non-operational, so all pre-0017 trading
        # records are authoritatively owned by the operational default team.
        connection.execute(
            sa.text("UPDATE proposals SET team_id = :team_id WHERE team_id IS NULL"),
            {"team_id": default_team_id},
        )
        connection.execute(
            sa.text(
                "UPDATE proposal_default_configs SET team_id = :team_id "
                "WHERE team_id IS NULL"
            ),
            {"team_id": default_team_id},
        )
        connection.execute(
            sa.text(
                "UPDATE risk_decisions d SET team_id = p.team_id "
                "FROM proposals p WHERE d.proposal_id = p.proposal_id "
                "AND d.team_id IS NULL"
            )
        )
        connection.execute(
            sa.text(
                "UPDATE trading_authorizations a SET team_id = p.team_id "
                "FROM proposals p WHERE a.proposal_id = p.proposal_id "
                "AND a.team_id IS NULL"
            )
        )
        connection.execute(
            sa.text(
                "UPDATE campaigns c SET team_id = p.team_id "
                "FROM proposals p WHERE c.proposal_id = p.proposal_id "
                "AND c.team_id IS NULL"
            )
        )
    missing = {
        table_name: connection.execute(sa.text(statement)).scalar_one()
        for table_name, statement in {
            "proposals": "SELECT count(*) FROM proposals WHERE team_id IS NULL",
            "proposal_default_configs": (
                "SELECT count(*) FROM proposal_default_configs WHERE team_id IS NULL"
            ),
            "risk_decisions": "SELECT count(*) FROM risk_decisions WHERE team_id IS NULL",
            "trading_authorizations": (
                "SELECT count(*) FROM trading_authorizations WHERE team_id IS NULL"
            ),
            "campaigns": "SELECT count(*) FROM campaigns WHERE team_id IS NULL",
        }.items()
    }
    if any(missing.values()):
        raise RuntimeError(f"0017 could not backfill team scope: {missing}")

    # Account is nullable for organization-wide events, but every existing
    # proposal/execution-chain audit receives the authoritative account root.
    account_backfills = (
        "UPDATE audit_events e SET account_id = p.account_id "
        "FROM proposals p WHERE e.object_type = 'Proposal' "
        "AND e.object_id = p.proposal_id::text AND e.account_id IS NULL",
        "UPDATE audit_events e SET account_id = c.account_id "
        "FROM proposal_default_configs c "
        "WHERE e.object_type = 'ProposalDefaultConfig' "
        "AND e.object_id = c.config_id::text AND e.account_id IS NULL",
        "UPDATE audit_events e SET account_id = p.account_id "
        "FROM risk_decisions d JOIN proposals p ON p.proposal_id = d.proposal_id "
        "WHERE e.object_type = 'RiskDecision' AND e.object_id = d.decision_id::text "
        "AND e.account_id IS NULL",
        "UPDATE audit_events e SET account_id = p.account_id "
        "FROM trading_authorizations a JOIN proposals p ON p.proposal_id = a.proposal_id "
        "WHERE e.object_type = 'TradingAuthorization' "
        "AND e.object_id = a.authorization_id::text AND e.account_id IS NULL",
        "UPDATE audit_events e SET account_id = c.account_id "
        "FROM campaigns c WHERE e.object_type = 'Campaign' "
        "AND e.object_id = c.campaign_id::text AND e.account_id IS NULL",
        "UPDATE audit_events e SET account_id = c.account_id "
        "FROM order_intents i JOIN campaigns c ON c.campaign_id = i.campaign_id "
        "WHERE e.object_type = 'OrderIntent' AND e.object_id = i.intent_id::text "
        "AND e.account_id IS NULL",
        "UPDATE audit_events e SET account_id = c.account_id "
        "FROM risk_reservations r JOIN campaigns c ON c.campaign_id = r.campaign_id "
        "WHERE e.object_type = 'RiskReservation' "
        "AND e.object_id = r.reservation_id::text AND e.account_id IS NULL",
        "UPDATE audit_events e SET account_id = o.account_id "
        "FROM venue_orders o WHERE e.object_type = 'VenueOrder' "
        "AND e.object_id = o.venue_order_fact_id::text AND e.account_id IS NULL",
        "UPDATE audit_events e SET account_id = f.account_id "
        "FROM venue_fills f WHERE e.object_type = 'VenueFill' "
        "AND e.object_id = f.venue_fill_fact_id::text AND e.account_id IS NULL",
        "UPDATE audit_events e SET account_id = f.account_id "
        "FROM funding_payments f WHERE e.object_type = 'FundingPayment' "
        "AND e.object_id = f.funding_payment_id::text AND e.account_id IS NULL",
        "UPDATE audit_events e SET account_id = c.account_id "
        "FROM reconciliation_runs r JOIN campaigns c ON c.campaign_id = r.campaign_id "
        "WHERE e.object_type = 'ReconciliationRun' "
        "AND e.object_id = r.reconciliation_id::text AND e.account_id IS NULL",
        "UPDATE audit_events e SET account_id = p.account_id "
        "FROM positions p WHERE e.object_type = 'Position' "
        "AND e.object_id = p.position_id::text AND e.account_id IS NULL",
        "UPDATE audit_events e SET account_id = p.account_id "
        "FROM protection_orders o JOIN positions p ON p.position_id = o.position_id "
        "WHERE e.object_type = 'ProtectionOrder' "
        "AND e.object_id = o.protection_id::text AND e.account_id IS NULL",
        "UPDATE audit_events e SET account_id = a.account_id "
        "FROM account_equities a WHERE e.object_type = 'AccountEquity' "
        "AND e.object_id = a.account_equity_id::text AND e.account_id IS NULL",
    )
    for statement in account_backfills:
        connection.execute(sa.text(statement))

    op.drop_index("uq_proposals_system_candidate", table_name="proposals")
    op.drop_constraint(
        "uq_proposal_default_configs_version",
        "proposal_default_configs",
        type_="unique",
    )
    op.drop_index(
        "uq_proposal_default_configs_active",
        table_name="proposal_default_configs",
    )
    op.drop_index("uq_campaigns_one_unclosed_scope", table_name="campaigns")

    op.drop_constraint(
        "risk_decisions_proposal_id_fkey", "risk_decisions", type_="foreignkey"
    )
    op.drop_constraint(
        "trading_authorizations_proposal_id_fkey",
        "trading_authorizations",
        type_="foreignkey",
    )
    op.drop_constraint("campaigns_proposal_id_fkey", "campaigns", type_="foreignkey")
    op.drop_constraint(
        "campaigns_authorization_id_fkey", "campaigns", type_="foreignkey"
    )

    for table_name in SCOPED_TABLES:
        op.alter_column(table_name, "team_id", nullable=False)
        op.create_foreign_key(
            f"fk_{table_name}_team_id_teams",
            table_name,
            "teams",
            ["team_id"],
            ["team_id"],
            ondelete="RESTRICT",
        )

    op.create_unique_constraint(
        "uq_proposals_team_identity",
        "proposals",
        ["team_id", "proposal_id"],
    )
    op.create_index(
        "ix_proposals_team_status_expires",
        "proposals",
        ["team_id", "status", "expires_at"],
    )
    op.create_index(
        "uq_proposals_system_candidate",
        "proposals",
        ["team_id", "source_candidate_id"],
        unique=True,
        postgresql_where=sa.text("source = 'SYSTEM' AND source_candidate_id IS NOT NULL"),
    )
    op.create_unique_constraint(
        "uq_proposal_default_configs_team_version",
        "proposal_default_configs",
        ["team_id", "version"],
    )
    op.create_index(
        "uq_proposal_default_configs_active",
        "proposal_default_configs",
        ["team_id", "active"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_foreign_key(
        "fk_risk_decisions_team_proposal",
        "risk_decisions",
        "proposals",
        ["team_id", "proposal_id"],
        ["team_id", "proposal_id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_trading_authorizations_team_identity",
        "trading_authorizations",
        ["team_id", "authorization_id"],
    )
    op.create_foreign_key(
        "fk_trading_authorizations_team_proposal",
        "trading_authorizations",
        "proposals",
        ["team_id", "proposal_id"],
        ["team_id", "proposal_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_campaigns_team_proposal",
        "campaigns",
        "proposals",
        ["team_id", "proposal_id"],
        ["team_id", "proposal_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_campaigns_team_authorization",
        "campaigns",
        "trading_authorizations",
        ["team_id", "authorization_id"],
        ["team_id", "authorization_id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "uq_campaigns_one_unclosed_scope",
        "campaigns",
        ["team_id", "account_id", "venue", "environment", "instrument_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'CLOSED'"),
    )
    op.create_index(
        "ix_audit_events_team_account_created",
        "audit_events",
        ["team_id", "account_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_audit_events_team_account_created",
        table_name="audit_events",
    )
    op.drop_index("uq_campaigns_one_unclosed_scope", table_name="campaigns")
    op.drop_constraint(
        "fk_campaigns_team_authorization", "campaigns", type_="foreignkey"
    )
    op.drop_constraint("fk_campaigns_team_proposal", "campaigns", type_="foreignkey")
    op.drop_constraint(
        "fk_trading_authorizations_team_proposal",
        "trading_authorizations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_trading_authorizations_team_identity",
        "trading_authorizations",
        type_="unique",
    )
    op.drop_constraint(
        "fk_risk_decisions_team_proposal", "risk_decisions", type_="foreignkey"
    )
    op.drop_index(
        "uq_proposal_default_configs_active",
        table_name="proposal_default_configs",
    )
    op.drop_constraint(
        "uq_proposal_default_configs_team_version",
        "proposal_default_configs",
        type_="unique",
    )
    op.drop_index("uq_proposals_system_candidate", table_name="proposals")
    op.drop_index("ix_proposals_team_status_expires", table_name="proposals")
    op.drop_constraint("uq_proposals_team_identity", "proposals", type_="unique")

    for table_name in reversed(SCOPED_TABLES):
        op.drop_constraint(
            f"fk_{table_name}_team_id_teams",
            table_name,
            type_="foreignkey",
        )

    op.create_foreign_key(
        "risk_decisions_proposal_id_fkey",
        "risk_decisions",
        "proposals",
        ["proposal_id"],
        ["proposal_id"],
    )
    op.create_foreign_key(
        "trading_authorizations_proposal_id_fkey",
        "trading_authorizations",
        "proposals",
        ["proposal_id"],
        ["proposal_id"],
    )
    op.create_foreign_key(
        "campaigns_proposal_id_fkey",
        "campaigns",
        "proposals",
        ["proposal_id"],
        ["proposal_id"],
    )
    op.create_foreign_key(
        "campaigns_authorization_id_fkey",
        "campaigns",
        "trading_authorizations",
        ["authorization_id"],
        ["authorization_id"],
    )
    op.create_index(
        "uq_proposals_system_candidate",
        "proposals",
        ["source_candidate_id"],
        unique=True,
        postgresql_where=sa.text("source = 'SYSTEM' AND source_candidate_id IS NOT NULL"),
    )
    op.create_unique_constraint(
        "uq_proposal_default_configs_version",
        "proposal_default_configs",
        ["version"],
    )
    op.create_index(
        "uq_proposal_default_configs_active",
        "proposal_default_configs",
        ["active"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_index(
        "uq_campaigns_one_unclosed_scope",
        "campaigns",
        ["account_id", "venue", "environment", "instrument_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'CLOSED'"),
    )
    for table_name in reversed(SCOPED_TABLES):
        op.drop_column(table_name, "team_id")
    op.drop_column("audit_events", "account_id")
