from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_control_plane.database import Base

AMOUNT = Numeric(38, 18)


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_workspaces_slug"),
        CheckConstraint("version >= 1", name="ck_workspaces_version"),
    )

    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (
        UniqueConstraint("workspace_id", "slug", name="uq_teams_workspace_slug"),
        CheckConstraint("version >= 1", name="ck_teams_version"),
        CheckConstraint(
            "execution_mode IN ('SETUP','SHADOW','LIVE')",
            name="ck_teams_execution_mode",
        ),
        Index("ix_teams_workspace_active", "workspace_id", "active"),
    )

    team_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    trading_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="SETUP")
    execution_mode_locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TeamShadowAccount(Base):
    __tablename__ = "team_shadow_accounts"
    __table_args__ = (
        CheckConstraint("generation >= 1", name="ck_team_shadow_accounts_generation"),
        CheckConstraint(
            "initial_equity = 100000 AND equity >= 0 AND available_balance >= 0",
            name="ck_team_shadow_accounts_balances",
        ),
        CheckConstraint("fees_paid >= 0", name="ck_team_shadow_accounts_fees_nonnegative"),
        CheckConstraint("status IN ('ACTIVE','ARCHIVED')", name="ck_team_shadow_accounts_status"),
        CheckConstraint("version >= 1", name="ck_team_shadow_accounts_version"),
        UniqueConstraint("team_id", "generation", name="uq_team_shadow_accounts_generation"),
        Index(
            "uq_team_shadow_accounts_active",
            "team_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    shadow_account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_equity: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    equity: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    available_balance: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    fees_paid: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AnalyticsEquitySnapshot(Base):
    __tablename__ = "analytics_equity_snapshots"
    __table_args__ = (
        CheckConstraint(
            "environment IN ('SHADOW','LIVE')",
            name="ck_analytics_equity_snapshots_environment",
        ),
        CheckConstraint(
            "(environment = 'SHADOW' AND generation IS NOT NULL) OR "
            "(environment = 'LIVE' AND generation IS NULL)",
            name="ck_analytics_equity_snapshots_generation",
        ),
        CheckConstraint("equity >= 0", name="ck_analytics_equity_snapshots_equity"),
        CheckConstraint("version >= 1", name="ck_analytics_equity_snapshots_version"),
        UniqueConstraint(
            "team_id",
            "environment",
            "source_kind",
            "source_id",
            name="uq_analytics_equity_snapshots_source",
        ),
        Index(
            "ix_analytics_equity_snapshots_scope_time",
            "team_id",
            "environment",
            "generation",
            "observed_at",
        ),
    )

    snapshot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    account_id: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    equity: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    currency: Mapped[str] = mapped_column(String(32), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    fact_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AnalyticsReport(Base):
    __tablename__ = "analytics_reports"
    __table_args__ = (
        CheckConstraint(
            "engine IN ('QUANTSTATS','PYFOLIO')",
            name="ck_analytics_reports_engine",
        ),
        CheckConstraint(
            "environment IN ('SHADOW','LIVE')",
            name="ck_analytics_reports_environment",
        ),
        CheckConstraint(
            "(environment = 'SHADOW' AND generation IS NOT NULL) OR "
            "(environment = 'LIVE' AND generation IS NULL)",
            name="ck_analytics_reports_generation",
        ),
        CheckConstraint("status IN ('READY','FAILED')", name="ck_analytics_reports_status"),
        CheckConstraint("chart_count >= 0", name="ck_analytics_reports_chart_count"),
        CheckConstraint("version >= 1", name="ck_analytics_reports_version"),
        UniqueConstraint(
            "team_id",
            "created_by",
            "idempotency_key",
            name="uq_analytics_reports_idempotency",
        ),
        Index(
            "ix_analytics_reports_scope_created",
            "team_id",
            "environment",
            "generation",
            "created_at",
        ),
    )

    report_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.workspace_id", ondelete="RESTRICT"), nullable=False
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    created_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False
    )
    engine: Mapped[str] = mapped_column(String(32), nullable=False)
    library_name: Mapped[str] = mapped_column(String(80), nullable=False)
    library_version: Mapped[str] = mapped_column(String(32), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    account_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    venues: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    from_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    to_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    chart_count: Mapped[int] = mapped_column(Integer, nullable=False)
    coverage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    report_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False)
    artifact_html: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ShadowInstrument(Base):
    __tablename__ = "shadow_instruments"
    __table_args__ = (
        CheckConstraint(
            "venue IN ('BINANCE','HYPERLIQUID','OKX','BYBIT')",
            name="ck_shadow_instruments_venue",
        ),
        CheckConstraint(
            "price_tick IS NULL OR price_tick > 0", name="ck_shadow_instruments_price_tick"
        ),
        CheckConstraint(
            "quantity_step IS NULL OR quantity_step > 0",
            name="ck_shadow_instruments_quantity_step",
        ),
        CheckConstraint(
            "contract_multiplier IS NULL OR contract_multiplier > 0",
            name="ck_shadow_instruments_multiplier",
        ),
        CheckConstraint(
            "latest_price IS NULL OR latest_price > 0",
            name="ck_shadow_instruments_latest_price",
        ),
        CheckConstraint("version >= 1", name="ck_shadow_instruments_version"),
        UniqueConstraint("team_id", "venue", "symbol", name="uq_shadow_instruments_scope"),
    )

    shadow_instrument_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    catalog_instrument_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("instruments.instrument_id", ondelete="SET NULL"), nullable=True
    )
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(120), nullable=False)
    price_tick: Mapped[Decimal | None] = mapped_column(AMOUNT, nullable=True)
    quantity_step: Mapped[Decimal | None] = mapped_column(AMOUNT, nullable=True)
    contract_multiplier: Mapped[Decimal | None] = mapped_column(AMOUNT, nullable=True)
    is_derivative: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    latest_price: Mapped[Decimal | None] = mapped_column(AMOUNT, nullable=True)
    price_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExchangeAccount(Base):
    __tablename__ = "exchange_accounts"
    __table_args__ = (
        CheckConstraint(
            "venue IN ('BINANCE','HYPERLIQUID','OKX','BYBIT')",
            name="ck_exchange_accounts_venue",
        ),
        CheckConstraint(
            "connection_status IN ('UNCONFIGURED','NOT_VERIFIED','VERIFIED','FAILED','STALE')",
            name="ck_exchange_accounts_connection_status",
        ),
        CheckConstraint(
            "trading_status IN ('DISABLED','BLOCKED','ELIGIBLE')",
            name="ck_exchange_accounts_trading_status",
        ),
        CheckConstraint(
            "registration_source IN ('MIGRATION','MANUAL','WORKFLOW_REFERENCE')",
            name="ck_exchange_accounts_registration_source",
        ),
        CheckConstraint("version >= 1", name="ck_exchange_accounts_version"),
        CheckConstraint("credential_version >= 0", name="ck_exchange_accounts_credential_version"),
        CheckConstraint(
            "freqtrade_worker_mode IN ('UNCONFIGURED','DRY_RUN','LIVE')",
            name="ck_exchange_accounts_freqtrade_worker_mode",
        ),
        CheckConstraint(
            "freqtrade_worker_status IN "
            "('UNCONFIGURED','NOT_VERIFIED','VERIFIED','FAILED','STALE')",
            name="ck_exchange_accounts_freqtrade_worker_status",
        ),
        CheckConstraint(
            "freqtrade_auth_version >= 0",
            name="ck_exchange_accounts_freqtrade_auth_version",
        ),
        CheckConstraint(
            "(freqtrade_worker_mode = 'UNCONFIGURED' "
            "AND freqtrade_worker_status = 'UNCONFIGURED' "
            "AND freqtrade_worker_name IS NULL AND freqtrade_worker_url IS NULL "
            "AND freqtrade_auth_ciphertext IS NULL AND freqtrade_auth_version = 0) OR "
            "(freqtrade_worker_mode IN ('DRY_RUN','LIVE') "
            "AND freqtrade_worker_status <> 'UNCONFIGURED' "
            "AND freqtrade_worker_name IS NOT NULL AND freqtrade_worker_url IS NOT NULL "
            "AND freqtrade_auth_ciphertext IS NOT NULL AND freqtrade_auth_version >= 1 "
            "AND venue IN ('BINANCE','HYPERLIQUID','OKX','BYBIT'))",
            name="ck_exchange_accounts_freqtrade_worker_shape",
        ),
        CheckConstraint(
            "(credentials_ciphertext IS NULL AND credential_version = 0) OR "
            "(credentials_ciphertext IS NOT NULL AND credential_version >= 1)",
            name="ck_exchange_accounts_credential_envelope",
        ),
        CheckConstraint(
            "NOT runtime_sync_enabled OR (active AND connection_status = 'VERIFIED' "
            "AND credential_version >= 1 "
            "AND venue IN ('BINANCE','HYPERLIQUID','OKX','BYBIT') "
            "AND runtime_service_principal_id IS NOT NULL)",
            name="ck_exchange_accounts_runtime_sync_ready",
        ),
        UniqueConstraint(
            "team_id",
            "account_id",
            "venue",
            name="uq_exchange_accounts_team_account_venue",
        ),
        Index("ix_exchange_accounts_team_active", "team_id", "active"),
        Index(
            "ix_exchange_accounts_runtime_sync",
            "team_id",
            "runtime_sync_enabled",
            "venue",
        ),
    )

    exchange_account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    registration_source: Mapped[str] = mapped_column(String(32), nullable=False)
    connection_status: Mapped[str] = mapped_column(String(32), nullable=False)
    trading_status: Mapped[str] = mapped_column(String(16), nullable=False)
    credentials_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    credential_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    connection_error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_connection_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    runtime_sync_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    runtime_service_principal_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=True
    )
    freqtrade_worker_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    freqtrade_worker_url: Mapped[str | None] = mapped_column(String(2_048), nullable=True)
    freqtrade_worker_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="UNCONFIGURED"
    )
    freqtrade_worker_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="UNCONFIGURED"
    )
    freqtrade_auth_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    freqtrade_auth_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    freqtrade_auth_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    freqtrade_hip3_dexes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    freqtrade_error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    freqtrade_last_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    freqtrade_last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    updated_by: Mapped[UUID] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("principal_type IN ('HUMAN','SERVICE')", name="ck_users_principal_type"),
        CheckConstraint(
            "(principal_type = 'HUMAN' AND service_kind IS NULL) OR "
            "(principal_type = 'SERVICE' AND service_kind IN ('INTERNAL','AGENT'))",
            name="ck_users_service_kind",
        ),
        CheckConstraint("agent_token_version >= 0", name="ck_users_agent_token_version"),
        CheckConstraint(
            "(service_kind = 'AGENT' AND agent_token_version >= 1 "
            "AND agent_token_digest IS NOT NULL AND agent_token_hint IS NOT NULL "
            "AND agent_token_created_at IS NOT NULL AND agent_token_expires_at IS NOT NULL) OR "
            "(service_kind IS DISTINCT FROM 'AGENT' AND agent_token_version = 0 "
            "AND agent_token_digest IS NULL AND agent_token_hint IS NULL "
            "AND agent_token_created_at IS NULL AND agent_token_expires_at IS NULL "
            "AND agent_token_last_used_at IS NULL)",
            name="ck_users_agent_token_shape",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    identity_subject: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True)
    active_workspace_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "workspaces.workspace_id",
            name="fk_users_active_workspace_id_workspaces",
            use_alter=True,
        ),
        nullable=True,
    )
    active_team_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "teams.team_id",
            name="fk_users_active_team_id_teams",
            use_alter=True,
        ),
        nullable=True,
    )
    principal_type: Mapped[str] = mapped_column(String(16), nullable=False)
    service_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    agent_token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_token_hint: Mapped[str | None] = mapped_column(String(32), nullable=True)
    agent_token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    agent_token_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    agent_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    agent_token_last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ApiClient(Base):
    __tablename__ = "api_clients"
    __table_args__ = (
        CheckConstraint(
            "state IN ('ACTIVE','DISABLED','REVOKED')",
            name="ck_api_clients_state",
        ),
        CheckConstraint("token_version >= 1", name="ck_api_clients_token_version"),
        CheckConstraint("version >= 1", name="ck_api_clients_version"),
        CheckConstraint(
            "(state = 'REVOKED' AND revoked_at IS NOT NULL) OR "
            "(state <> 'REVOKED' AND revoked_at IS NULL)",
            name="ck_api_clients_revocation_shape",
        ),
        UniqueConstraint("owner_user_id", "name", name="uq_api_clients_owner_name"),
        ForeignKeyConstraint(
            ["team_id", "account_id", "venue"],
            [
                "exchange_accounts.team_id",
                "exchange_accounts.account_id",
                "exchange_accounts.venue",
            ],
            name="fk_api_clients_exchange_account_scope",
            ondelete="RESTRICT",
        ),
        Index("ix_api_clients_owner_state", "owner_user_id", "state"),
        Index("ix_api_clients_team_scope", "team_id", "account_id", "venue"),
    )

    api_client_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.workspace_id", ondelete="RESTRICT"), nullable=False
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hint: Mapped[str] = mapped_column(String(32), nullable=False)
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    token_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    token_last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkspaceMembership(Base):
    __tablename__ = "workspace_memberships"
    __table_args__ = (
        CheckConstraint("role IN ('MEMBER','ADMIN')", name="ck_workspace_memberships_role"),
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_memberships_workspace_user"),
        Index("ix_workspace_memberships_user_active", "user_id", "active"),
    )

    membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    invited_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.user_id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TeamMembership(Base):
    __tablename__ = "team_memberships"
    __table_args__ = (
        UniqueConstraint("team_id", "user_id", name="uq_team_memberships_team_user"),
        Index("ix_team_memberships_user_active", "user_id", "active"),
    )

    membership_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    invited_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.user_id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RoleAssignment(Base):
    __tablename__ = "role_assignments"
    __table_args__ = (
        CheckConstraint(
            "role IN ('OBSERVER','PROPOSER','REVIEWER','OPERATOR','TREASURY_ADMIN','SYSTEM_ADMIN')",
            name="ck_role_assignments_role",
        ),
        Index("ix_role_assignments_user", "user_id"),
        Index("ix_role_assignments_team_user", "team_id", "user_id"),
    )

    assignment_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    account_scope: Mapped[str | None] = mapped_column(String(120), nullable=True)
    venue_scope: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Instrument(Base):
    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint("venue", "symbol", name="uq_instruments_venue_symbol"),
        CheckConstraint("tick_size > 0", name="ck_instruments_tick_size_positive"),
        CheckConstraint("lot_size > 0", name="ck_instruments_lot_size_positive"),
        CheckConstraint("minimum_notional >= 0", name="ck_instruments_min_notional_nonnegative"),
        CheckConstraint("contract_multiplier > 0", name="ck_instruments_multiplier_positive"),
    )

    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(120), nullable=False)
    tick_size: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    lot_size: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    minimum_notional: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    contract_multiplier: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(32), nullable=False)
    collateral_currency: Mapped[str] = mapped_column(String(32), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    protection_supported: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PerptapeFeed(Base):
    __tablename__ = "perptape_feeds"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(candidates) = 'array'",
            name="ck_perptape_feeds_candidates_array",
        ),
        CheckConstraint(
            "next_allowed_at >= generated_at",
            name="ck_perptape_feeds_refresh_window",
        ),
        CheckConstraint("version >= 1", name="ck_perptape_feeds_version"),
    )

    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="CASCADE"), primary_key=True
    )
    feed_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    contract_version: Mapped[str] = mapped_column(String(120), nullable=False)
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    next_allowed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TeamSignalSource(Base):
    __tablename__ = "team_signal_sources"
    __table_args__ = (
        CheckConstraint("mode IN ('PERPTAPE','WEBHOOK')", name="ck_team_signal_sources_mode"),
        CheckConstraint("version >= 1", name="ck_team_signal_sources_version"),
        CheckConstraint(
            "credential_version >= 0", name="ck_team_signal_sources_credential_version"
        ),
        CheckConstraint(
            "(credential_ciphertext IS NULL AND credential_version = 0) OR "
            "(credential_ciphertext IS NOT NULL AND credential_version >= 1)",
            name="ck_team_signal_sources_credential_envelope",
        ),
        CheckConstraint(
            "webhook_max_age_seconds BETWEEN 30 AND 900",
            name="ck_team_signal_sources_max_age",
        ),
        CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_team_signal_sources_consecutive_failures",
        ),
        UniqueConstraint(
            "team_id",
            "signal_source_id",
            name="uq_team_signal_sources_team_identity",
        ),
        Index("ix_team_signal_sources_enabled_mode", "enabled", "mode"),
        Index("ix_team_signal_sources_team_deleted", "team_id", "deleted_at"),
        Index(
            "uq_team_signal_sources_active_perptape",
            "team_id",
            unique=True,
            postgresql_where=text("mode = 'PERPTAPE' AND deleted_at IS NULL"),
        ),
        Index(
            "uq_team_signal_sources_active_name",
            "team_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    signal_source_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    credential_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    credential_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    webhook_max_age_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    service_principal_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=True
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    updated_by: Mapped[UUID] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=True
    )


class NotificationRoute(Base):
    __tablename__ = "notification_routes"
    __table_args__ = (
        CheckConstraint(
            "channel IN ('TELEGRAM','SLACK','LARK','EMAIL')",
            name="ck_notification_routes_channel",
        ),
        CheckConstraint(
            "jsonb_typeof(event_types) = 'array'", name="ck_notification_routes_events"
        ),
        CheckConstraint("version >= 1", name="ck_notification_routes_version"),
        CheckConstraint(
            "credential_version >= 1", name="ck_notification_routes_credential_version"
        ),
        UniqueConstraint("team_id", "name", name="uq_notification_routes_team_name"),
        UniqueConstraint(
            "team_id", "notification_route_id", name="uq_notification_routes_team_identity"
        ),
        Index("ix_notification_routes_team_enabled", "team_id", "enabled"),
    )

    notification_route_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    event_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    configuration_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    configuration_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    updated_by: Mapped[UUID] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        CheckConstraint(
            "channel IN ('TELEGRAM','SLACK','LARK','EMAIL')",
            name="ck_notification_deliveries_channel",
        ),
        CheckConstraint(
            "status IN ('PENDING','RETRY_WAIT','SENDING','SENT','DEAD_LETTER',"
            "'OUTCOME_UNKNOWN','CANCELLED')",
            name="ck_notification_deliveries_status",
        ),
        CheckConstraint("template_version >= 1", name="ck_notification_deliveries_template"),
        CheckConstraint("route_version >= 1", name="ck_notification_deliveries_route_version"),
        CheckConstraint("attempt_count >= 0", name="ck_notification_deliveries_attempt_count"),
        CheckConstraint(
            "max_attempts BETWEEN 1 AND 10", name="ck_notification_deliveries_max_attempts"
        ),
        CheckConstraint(
            "length(semantic_hash) = 64", name="ck_notification_deliveries_semantic_hash"
        ),
        ForeignKeyConstraint(
            ["team_id", "notification_route_id"],
            ["notification_routes.team_id", "notification_routes.notification_route_id"],
            name="fk_notification_deliveries_team_route",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "notification_route_id",
            "notification_event_id",
            name="uq_notification_deliveries_route_event",
        ),
        Index(
            "ix_notification_deliveries_due",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_notification_deliveries_team_created",
            "team_id",
            "created_at",
        ),
    )

    notification_delivery_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    notification_event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    notification_route_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    route_version: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    template_key: Mapped[str] = mapped_column(String(120), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    object_type: Mapped[str] = mapped_column(String(120), nullable=False)
    object_id: Mapped[str] = mapped_column(String(255), nullable=False)
    object_version: Mapped[int] = mapped_column(Integer, nullable=False)
    environment: Mapped[str | None] = mapped_column(String(16), nullable=True)
    account_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    venue: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    external_delivery_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SignalEvent(Base):
    __tablename__ = "signal_events"
    __table_args__ = (
        CheckConstraint("provider IN ('TRADINGVIEW','MODEL')", name="ck_signal_events_provider"),
        CheckConstraint("direction IN ('LONG','SHORT')", name="ck_signal_events_direction"),
        CheckConstraint(
            "venue IN ('BINANCE','HYPERLIQUID','OKX','BYBIT')",
            name="ck_signal_events_venue",
        ),
        CheckConstraint(
            "status IN ('RECEIVED','PROPOSAL_CREATED')", name="ck_signal_events_status"
        ),
        CheckConstraint("payload_version >= 1", name="ck_signal_events_payload_version"),
        CheckConstraint(
            "reference_price IS NULL OR reference_price > 0",
            name="ck_signal_events_reference_price",
        ),
        ForeignKeyConstraint(
            ["team_id", "signal_source_id"],
            ["team_signal_sources.team_id", "team_signal_sources.signal_source_id"],
            name="fk_signal_events_team_source",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("team_id", "signal_event_id", name="uq_signal_events_team_identity"),
        UniqueConstraint(
            "signal_source_id",
            "idempotency_key",
            name="uq_signal_events_source_idempotency",
        ),
        UniqueConstraint("signal_source_id", "nonce", name="uq_signal_events_source_nonce"),
        UniqueConstraint(
            "signal_source_id",
            "provider",
            "external_id",
            name="uq_signal_events_source_provider_external",
        ),
        Index("ix_signal_events_team_received", "team_id", "received_at"),
    )

    signal_event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    signal_source_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(160), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    nonce: Mapped[str] = mapped_column(String(160), nullable=False)
    payload_version: Mapped[int] = mapped_column(Integer, nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(120), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(120), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    timeframe: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reference_price: Mapped[Decimal | None] = mapped_column(AMOUNT, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    normalized_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    signature_version: Mapped[str] = mapped_column(String(16), nullable=False)


class Proposal(Base):
    __tablename__ = "proposals"
    __table_args__ = (
        CheckConstraint("source IN ('SYSTEM','MANUAL')", name="ck_proposals_source"),
        CheckConstraint(
            "environment IN ('SHADOW','TESTNET','LIVE')", name="ck_proposals_environment"
        ),
        CheckConstraint("risk_tier IN ('LOW','MEDIUM','HIGH')", name="ck_proposals_risk_tier"),
        CheckConstraint("direction IN ('LONG','SHORT')", name="ck_proposals_direction"),
        CheckConstraint(
            "status IN ('DRAFT','PENDING_REVIEW','APPROVED','REJECTED','EXPIRED')",
            name="ck_proposals_status",
        ),
        CheckConstraint("quantity > 0", name="ck_proposals_quantity_positive"),
        CheckConstraint("max_risk > 0", name="ck_proposals_risk_positive"),
        CheckConstraint(
            "source = 'MANUAL' OR (strategy_id IS NOT NULL AND strategy_version IS NOT NULL)",
            name="ck_proposals_system_strategy",
        ),
        Index("ix_proposals_status_expires", "status", "expires_at"),
        Index("ix_proposals_team_status_expires", "team_id", "status", "expires_at"),
        UniqueConstraint("team_id", "proposal_id", name="uq_proposals_team_identity"),
        ForeignKeyConstraint(
            ["team_id", "account_id", "venue"],
            [
                "exchange_accounts.team_id",
                "exchange_accounts.account_id",
                "exchange_accounts.venue",
            ],
            name="fk_proposals_team_exchange_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["team_id", "signal_event_id"],
            ["signal_events.team_id", "signal_events.signal_event_id"],
            name="fk_proposals_team_signal_event",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_proposals_system_candidate",
            "team_id",
            "source_candidate_id",
            unique=True,
            postgresql_where=text("source = 'SYSTEM' AND source_candidate_id IS NOT NULL"),
        ),
        Index(
            "uq_proposals_signal_event",
            "team_id",
            "signal_event_id",
            unique=True,
            postgresql_where=text("signal_event_id IS NOT NULL"),
        ),
    )

    proposal_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False, default="SHADOW")
    proposer_id: Mapped[UUID] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    strategy_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    strategy_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source_candidate_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    source_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_readiness: Mapped[str | None] = mapped_column(String(32), nullable=True)
    signal_event_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    risk_tier: Mapped[str] = mapped_column(String(16), nullable=False)
    account_id: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.instrument_id"))
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    max_risk: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    frozen_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProposalDefaultConfig(Base):
    __tablename__ = "proposal_default_configs"
    __table_args__ = (
        CheckConstraint("environment = 'LIVE'", name="ck_proposal_defaults_live"),
        CheckConstraint(
            "risk_tier IN ('LOW','MEDIUM','HIGH')", name="ck_proposal_defaults_risk_tier"
        ),
        CheckConstraint("notional > 0", name="ck_proposal_defaults_notional_positive"),
        CheckConstraint("max_risk > 0", name="ck_proposal_defaults_risk_positive"),
        CheckConstraint("invalidation_bps BETWEEN 1 AND 5000", name="ck_proposal_defaults_bps"),
        CheckConstraint(
            "expires_in_minutes BETWEEN 5 AND 1440", name="ck_proposal_defaults_expiry"
        ),
        CheckConstraint(
            "auto_proposal_min_timeframes IN (3, 4)",
            name="ck_proposal_defaults_auto_timeframes",
        ),
        UniqueConstraint("team_id", "version", name="uq_proposal_default_configs_team_version"),
        Index(
            "uq_proposal_default_configs_active",
            "team_id",
            "active",
            unique=True,
            postgresql_where=text("active"),
        ),
    )

    config_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    account_id: Mapped[str] = mapped_column(String(120), nullable=False)
    risk_tier: Mapped[str] = mapped_column(String(16), nullable=False)
    notional: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    max_risk: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    invalidation_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_in_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    auto_proposal_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auto_proposal_min_timeframes: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_by: Mapped[UUID] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RuntimeSourceHealth(Base):
    __tablename__ = "runtime_source_health"
    __table_args__ = (
        ForeignKeyConstraint(
            ["team_id", "account_id", "venue"],
            [
                "exchange_accounts.team_id",
                "exchange_accounts.account_id",
                "exchange_accounts.venue",
            ],
            name="fk_runtime_source_health_team_exchange_account",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "team_id",
            "source_name",
            "account_id",
            "venue",
            name="uq_runtime_source_health_scope",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "status IN ('SUCCESS','FAILED','SKIPPED')",
            name="ck_runtime_source_health_status",
        ),
        CheckConstraint(
            "items_observed >= 0",
            name="ck_runtime_source_health_items_nonnegative",
        ),
        CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_runtime_source_health_failures_nonnegative",
        ),
        CheckConstraint(
            "(account_id IS NULL AND venue IS NULL) OR "
            "(account_id IS NOT NULL AND venue IN "
            "('BINANCE','HYPERLIQUID','OKX','BYBIT'))",
            name="ck_runtime_source_health_account_scope",
        ),
        Index("ix_runtime_source_health_team_checked", "team_id", "checked_at"),
    )

    runtime_source_health_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="CASCADE"), nullable=False
    )
    source_name: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    venue: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    items_observed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_by: Mapped[UUID] = mapped_column(ForeignKey("users.user_id"), nullable=False)


class TransferProposal(Base):
    __tablename__ = "transfer_proposals"
    __table_args__ = (
        UniqueConstraint(
            "team_id",
            "transfer_proposal_id",
            name="uq_transfer_proposals_team_identity",
        ),
        ForeignKeyConstraint(
            ["team_id", "account_id", "venue"],
            [
                "exchange_accounts.team_id",
                "exchange_accounts.account_id",
                "exchange_accounts.venue",
            ],
            name="fk_transfer_proposals_team_exchange_account",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "environment IN ('SHADOW','TESTNET','LIVE')",
            name="ck_transfer_proposals_environment",
        ),
        CheckConstraint(
            "direction IN ('VAULT_TO_VENUE','VENUE_TO_VAULT')",
            name="ck_transfer_proposals_direction",
        ),
        CheckConstraint(
            "status IN ('DRAFT','PENDING_REVIEW','APPROVED','REJECTED','EXPIRED')",
            name="ck_transfer_proposals_status",
        ),
        CheckConstraint(
            "((direction = 'VAULT_TO_VENUE' "
            "AND source_type = 'VAULT' AND destination_type = 'VENUE') "
            "OR (direction = 'VENUE_TO_VAULT' "
            "AND source_type = 'VENUE' AND destination_type = 'VAULT'))",
            name="ck_transfer_proposals_endpoint_direction",
        ),
        CheckConstraint("amount > 0", name="ck_transfer_proposals_amount_positive"),
        CheckConstraint("max_fee >= 0", name="ck_transfer_proposals_fee_nonnegative"),
        CheckConstraint(
            "min_received > 0 AND min_received <= amount",
            name="ck_transfer_proposals_min_received",
        ),
        Index(
            "ix_transfer_proposals_status_expires",
            "team_id",
            "status",
            "expires_at",
        ),
    )

    transfer_proposal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    proposer_id: Mapped[UUID] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    direction: Mapped[str] = mapped_column(String(24), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    account_id: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[str] = mapped_column(String(160), nullable=False)
    destination_type: Mapped[str] = mapped_column(String(16), nullable=False)
    destination_id: Mapped[str] = mapped_column(String(160), nullable=False)
    asset: Mapped[str] = mapped_column(String(32), nullable=False)
    network: Mapped[str] = mapped_column(String(64), nullable=False)
    destination_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    max_fee: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    min_received: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    frozen_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            "((proposal_id IS NOT NULL)::integer + "
            "(transfer_proposal_id IS NOT NULL)::integer + "
            "(risk_control_change_request_id IS NOT NULL)::integer) = 1",
            name="ck_approvals_one_parent",
        ),
        CheckConstraint("decision IN ('APPROVE','REJECT')", name="ck_approvals_decision"),
        Index(
            "uq_approvals_proposal_reviewer",
            "proposal_id",
            "reviewer_id",
            unique=True,
            postgresql_where=text("proposal_id IS NOT NULL"),
        ),
        Index(
            "uq_approvals_transfer_reviewer",
            "transfer_proposal_id",
            "reviewer_id",
            unique=True,
            postgresql_where=text("transfer_proposal_id IS NOT NULL"),
        ),
        Index(
            "uq_approvals_risk_control_reviewer",
            "risk_control_change_request_id",
            "reviewer_id",
            unique=True,
            postgresql_where=text("risk_control_change_request_id IS NOT NULL"),
        ),
    )

    approval_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    proposal_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("proposals.proposal_id", ondelete="CASCADE"), nullable=True
    )
    transfer_proposal_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("transfer_proposals.transfer_proposal_id", ondelete="CASCADE"), nullable=True
    )
    risk_control_change_request_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("risk_control_change_requests.request_id", ondelete="CASCADE"), nullable=True
    )
    reviewer_id: Mapped[UUID] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TransferAuthorization(Base):
    __tablename__ = "transfer_authorizations"
    __table_args__ = (
        UniqueConstraint(
            "team_id",
            "transfer_authorization_id",
            name="uq_transfer_authorizations_team_identity",
        ),
        ForeignKeyConstraint(
            ["team_id", "transfer_proposal_id"],
            ["transfer_proposals.team_id", "transfer_proposals.transfer_proposal_id"],
            name="fk_transfer_authorizations_team_proposal",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "team_id",
            "transfer_proposal_id",
            name="uq_transfer_authorizations_proposal",
        ),
        CheckConstraint(
            "environment IN ('SHADOW','TESTNET','LIVE')",
            name="ck_transfer_authorizations_environment",
        ),
        CheckConstraint(
            "direction IN ('VAULT_TO_VENUE','VENUE_TO_VAULT')",
            name="ck_transfer_authorizations_direction",
        ),
        CheckConstraint("amount_limit > 0", name="ck_transfer_authorizations_amount_positive"),
        CheckConstraint("max_fee >= 0", name="ck_transfer_authorizations_fee_nonnegative"),
        CheckConstraint(
            "min_received > 0 AND min_received <= amount_limit",
            name="ck_transfer_authorizations_min_received",
        ),
    )

    transfer_authorization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    transfer_proposal_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    direction: Mapped[str] = mapped_column(String(24), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    account_id: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[str] = mapped_column(String(160), nullable=False)
    destination_type: Mapped[str] = mapped_column(String(16), nullable=False)
    destination_id: Mapped[str] = mapped_column(String(160), nullable=False)
    asset: Mapped[str] = mapped_column(String(32), nullable=False)
    network: Mapped[str] = mapped_column(String(64), nullable=False)
    destination_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    amount_limit: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    max_fee: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    min_received: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CapitalTransfer(Base):
    __tablename__ = "capital_transfers"
    __table_args__ = (
        UniqueConstraint(
            "team_id",
            "transfer_authorization_id",
            name="uq_capital_transfers_authorization",
        ),
        ForeignKeyConstraint(
            ["team_id", "transfer_authorization_id"],
            [
                "transfer_authorizations.team_id",
                "transfer_authorizations.transfer_authorization_id",
            ],
            name="fk_capital_transfers_team_authorization",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["team_id", "account_id", "venue"],
            [
                "exchange_accounts.team_id",
                "exchange_accounts.account_id",
                "exchange_accounts.venue",
            ],
            name="fk_capital_transfers_team_exchange_account",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('SOURCE_RESERVED','SUBMITTED','IN_FLIGHT','DESTINATION_CONFIRMED',"
            "'SETTLED','UNKNOWN','FAILED_SOURCE_RESTORED','MANUAL_REQUIRED')",
            name="ck_capital_transfers_status",
        ),
        CheckConstraint("gross_amount > 0", name="ck_capital_transfers_gross_positive"),
        CheckConstraint(
            "reserved_amount = gross_amount", name="ck_capital_transfers_reserved_exact"
        ),
        CheckConstraint(
            "fee_amount IS NULL OR fee_amount >= 0", name="ck_capital_transfers_fee_nonnegative"
        ),
        CheckConstraint(
            "net_received IS NULL OR net_received > 0",
            name="ck_capital_transfers_net_positive",
        ),
        CheckConstraint(
            "transport IN ('MOCK','NOTILT')",
            name="ck_capital_transfers_transport",
        ),
        CheckConstraint(
            "chain_id IS NULL OR chain_id IN (1,56,42161)",
            name="ck_capital_transfers_chain",
        ),
        CheckConstraint(
            "transport_state IS NULL OR transport_state IN ("
            "'DEPOSIT_PLAN_READY','DEPOSIT_CONFIRMED',"
            "'RELEASE_REQUEST_PLAN_READY','RELEASE_REQUEST_CONFIRMED',"
            "'RELEASE_EXECUTION_PLAN_READY','RELEASE_EXECUTION_CONFIRMED',"
            "'RELEASE_CANCELLATION_PLAN_READY','RELEASE_CANCELLED')",
            name="ck_capital_transfers_transport_state",
        ),
        Index(
            "ix_capital_transfers_status_updated",
            "team_id",
            "status",
            "updated_at",
        ),
    )

    capital_transfer_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    transfer_authorization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    account_id: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    direction: Mapped[str] = mapped_column(String(24), nullable=False)
    source_id: Mapped[str] = mapped_column(String(160), nullable=False)
    destination_id: Mapped[str] = mapped_column(String(160), nullable=False)
    asset: Mapped[str] = mapped_column(String(32), nullable=False)
    network: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    gross_amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    reserved_amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    source_balance_before: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    destination_balance_before: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    fee_amount: Mapped[Decimal | None] = mapped_column(AMOUNT, nullable=True)
    net_received: Mapped[Decimal | None] = mapped_column(AMOUNT, nullable=True)
    external_transfer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    transaction_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    transport: Mapped[str] = mapped_column(String(16), nullable=False, default="MOCK")
    chain_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transport_state: Mapped[str | None] = mapped_column(String(48), nullable=True)
    planned_transactions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    confirmed_transaction_hashes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    protocol_request_id: Mapped[str | None] = mapped_column(String(66), nullable=True)
    protocol_execute_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    protocol_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reconciliation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reconciliation_details: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DirectCapitalConfiguration(Base):
    __tablename__ = "direct_capital_configurations"
    __table_args__ = (
        UniqueConstraint(
            "team_id",
            "version",
            name="uq_direct_capital_configurations_version",
        ),
        CheckConstraint("version >= 1", name="ck_direct_capital_configuration_version"),
        CheckConstraint("network = 'ARBITRUM'", name="ck_direct_capital_configuration_network"),
        CheckConstraint("asset = 'USDC'", name="ck_direct_capital_configuration_asset"),
        CheckConstraint(
            "treasury_provider IN ('NOTILT_VAULT','SAFE_SPENDING_LIMIT')",
            name="ck_direct_capital_configuration_treasury_provider",
        ),
        CheckConstraint(
            "max_amount IS NULL OR max_amount > 0",
            name="ck_direct_capital_configuration_max_amount",
        ),
        CheckConstraint(
            "max_fee IS NULL OR max_fee >= 0",
            name="ck_direct_capital_configuration_max_fee",
        ),
        Index(
            "uq_direct_capital_configuration_active",
            "team_id",
            "active",
            unique=True,
            postgresql_where=text("active"),
        ),
    )

    config_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    network: Mapped[str] = mapped_column(String(64), nullable=False, default="ARBITRUM")
    asset: Mapped[str] = mapped_column(String(32), nullable=False, default="USDC")
    treasury_provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="NOTILT_VAULT"
    )
    vault_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    vault_address: Mapped[str | None] = mapped_column(String(42), nullable=True)
    owned_arbitrum_address: Mapped[str | None] = mapped_column(String(42), nullable=True)
    binance_account_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    binance_deposit_address: Mapped[str | None] = mapped_column(String(42), nullable=True)
    binance_withdrawal_address: Mapped[str | None] = mapped_column(String(42), nullable=True)
    hyperliquid_account_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    hyperliquid_bridge_address: Mapped[str | None] = mapped_column(String(42), nullable=True)
    safe_address: Mapped[str | None] = mapped_column(String(42), nullable=True)
    safe_delegate_address: Mapped[str | None] = mapped_column(String(42), nullable=True)
    max_amount: Mapped[Decimal | None] = mapped_column(AMOUNT, nullable=True)
    max_fee: Mapped[Decimal | None] = mapped_column(AMOUNT, nullable=True)
    updated_by: Mapped[UUID] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DirectCapitalOperation(Base):
    __tablename__ = "direct_capital_operations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["team_id", "account_id", "venue"],
            [
                "exchange_accounts.team_id",
                "exchange_accounts.account_id",
                "exchange_accounts.venue",
            ],
            name="fk_direct_capital_operations_team_exchange_account",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "path IN ('VAULT_TO_BINANCE','VAULT_TO_HYPERLIQUID',"
            "'BINANCE_TO_VAULT','HYPERLIQUID_TO_VAULT')",
            name="ck_direct_capital_operations_path",
        ),
        CheckConstraint("venue IN ('BINANCE','HYPERLIQUID')", name="ck_direct_capital_venue"),
        CheckConstraint(
            "treasury_provider IN ('NOTILT_VAULT','SAFE_SPENDING_LIMIT')",
            name="ck_direct_capital_treasury_provider",
        ),
        CheckConstraint(
            "status IN ('BLOCKED','UNSIGNED_PLAN_READY','AWAITING_RECEIPT','SETTLED','UNKNOWN')",
            name="ck_direct_capital_status",
        ),
        CheckConstraint(
            "receipt_status IN ('NOT_SUBMITTED','PENDING','CONFIRMED','UNKNOWN')",
            name="ck_direct_capital_receipt_status",
        ),
        CheckConstraint("amount > 0", name="ck_direct_capital_amount_positive"),
        CheckConstraint(
            "max_fee IS NULL OR max_fee >= 0", name="ck_direct_capital_fee_nonnegative"
        ),
        CheckConstraint(
            "min_received IS NULL OR (min_received > 0 AND min_received <= amount)",
            name="ck_direct_capital_min_received",
        ),
        CheckConstraint("jsonb_typeof(stages) = 'array'", name="ck_direct_capital_stages_array"),
        CheckConstraint(
            "jsonb_typeof(blockers) = 'array'", name="ck_direct_capital_blockers_array"
        ),
        Index("ix_direct_capital_operations_updated", "team_id", "updated_at"),
    )

    operation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    treasury_provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="NOTILT_VAULT"
    )
    path: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    receipt_status: Mapped[str] = mapped_column(String(32), nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    vault_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    asset: Mapped[str] = mapped_column(String(32), nullable=False)
    network: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    max_fee: Mapped[Decimal | None] = mapped_column(AMOUNT, nullable=True)
    min_received: Mapped[Decimal | None] = mapped_column(AMOUNT, nullable=True)
    source_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    destination_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    blockers: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    execute_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    final_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CapitalAutomationPolicy(Base):
    __tablename__ = "capital_automation_policies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["team_id", "account_id", "venue"],
            [
                "exchange_accounts.team_id",
                "exchange_accounts.account_id",
                "exchange_accounts.venue",
            ],
            name="fk_capital_automation_policies_team_exchange_account",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "team_id",
            "environment",
            "account_id",
            "venue",
            "asset",
            name="uq_capital_automation_policies_scope",
        ),
        CheckConstraint(
            "environment IN ('SHADOW','TESTNET')",
            name="ck_capital_automation_policies_environment",
        ),
        CheckConstraint(
            "operating_low >= 0 AND operating_low <= operating_target "
            "AND operating_target <= operating_high",
            name="ck_capital_automation_policies_thresholds",
        ),
        CheckConstraint(
            "vault_minimum_reserve >= 0",
            name="ck_capital_automation_policies_vault_reserve",
        ),
        CheckConstraint(
            "minimum_transfer > 0 AND maximum_transfer >= minimum_transfer",
            name="ck_capital_automation_policies_transfer_limits",
        ),
        CheckConstraint(
            "max_fee >= 0 AND max_fee < minimum_transfer",
            name="ck_capital_automation_policies_fee",
        ),
    )

    policy_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    account_id: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    vault_id: Mapped[str] = mapped_column(String(160), nullable=False)
    asset: Mapped[str] = mapped_column(String(32), nullable=False)
    network: Mapped[str] = mapped_column(String(64), nullable=False)
    vault_destination_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    venue_destination_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    operating_low: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    operating_target: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    operating_high: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    vault_minimum_reserve: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    minimum_transfer: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    maximum_transfer: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    max_fee: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RiskPolicy(Base):
    __tablename__ = "risk_policies"
    __table_args__ = (
        UniqueConstraint("team_id", "policy_id", name="uq_risk_policies_team_identity"),
        UniqueConstraint("team_id", "version", name="uq_risk_policies_team_version"),
        UniqueConstraint("team_id", "revision", name="uq_risk_policies_team_revision"),
        CheckConstraint(
            "system_state IN ('NORMAL','NO_PYRAMID','REDUCE_ONLY','KILL_SWITCH')",
            name="ck_risk_policies_system_state",
        ),
        CheckConstraint("max_total_risk > 0", name="ck_risk_policies_max_risk_positive"),
        CheckConstraint("max_account_risk > 0", name="ck_risk_policies_account_risk_positive"),
        CheckConstraint("max_single_loss > 0", name="ck_risk_policies_single_loss_positive"),
        CheckConstraint(
            "max_consecutive_losses > 0",
            name="ck_risk_policies_consecutive_losses_positive",
        ),
        CheckConstraint(
            "loss_cooldown_seconds > 0",
            name="ck_risk_policies_loss_cooldown_positive",
        ),
        CheckConstraint("max_fact_age_seconds > 0", name="ck_risk_policies_age_positive"),
        CheckConstraint(
            "(max_account_risk IS NULL AND max_single_loss IS NULL "
            "AND max_consecutive_losses IS NULL AND loss_cooldown_seconds IS NULL) OR "
            "(max_account_risk IS NOT NULL AND max_single_loss IS NOT NULL "
            "AND max_consecutive_losses IS NOT NULL AND loss_cooldown_seconds IS NOT NULL)",
            name="ck_risk_policies_limits_all_or_none",
        ),
        CheckConstraint("revision >= 1", name="ck_risk_policies_revision"),
        Index(
            "uq_risk_policies_one_active",
            "team_id",
            "active",
            unique=True,
            postgresql_where=text("active"),
        ),
    )

    policy_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[str] = mapped_column(String(120), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    system_state: Mapped[str] = mapped_column(String(32), nullable=False)
    max_total_risk: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    max_account_risk: Mapped[Decimal | None] = mapped_column(AMOUNT, nullable=True)
    max_single_loss: Mapped[Decimal | None] = mapped_column(AMOUNT, nullable=True)
    max_consecutive_losses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    loss_cooldown_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_fact_age_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RiskControlChangeRequest(Base):
    __tablename__ = "risk_control_change_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING_REVIEW','APPROVED','REJECTED','EXPIRED','EXECUTED')",
            name="ck_risk_control_change_requests_status",
        ),
        CheckConstraint("version >= 1", name="ck_risk_control_change_requests_version"),
        CheckConstraint(
            "source_policy_revision >= 1 AND source_auto_add_version >= 1",
            name="ck_risk_control_change_requests_source_versions",
        ),
        CheckConstraint(
            "source_auto_add_status IN ('DISABLED','ENABLED')",
            name="ck_risk_control_change_requests_auto_add_status",
        ),
        CheckConstraint(
            "execute_after >= created_at AND expires_at > execute_after",
            name="ck_risk_control_change_requests_window",
        ),
        Index(
            "uq_risk_control_change_requests_pending",
            "team_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING_REVIEW','APPROVED')"),
        ),
        ForeignKeyConstraint(
            ["team_id", "source_policy_id"],
            ["risk_policies.team_id", "risk_policies.policy_id"],
            name="fk_risk_control_change_requests_team_source_policy",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["team_id", "resulting_policy_id"],
            ["risk_policies.team_id", "risk_policies.policy_id"],
            name="fk_risk_control_change_requests_team_result_policy",
            ondelete="RESTRICT",
        ),
        Index("ix_risk_control_change_requests_created", "created_at"),
    )

    request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    requester_id: Mapped[UUID] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    restore_auto_add: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    require_live_scope: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_policy_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    source_policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_auto_add_status: Mapped[str] = mapped_column(String(16), nullable=False)
    source_auto_add_version: Mapped[int] = mapped_column(Integer, nullable=False)
    required_scopes: Mapped[list[dict[str, str]]] = mapped_column(JSONB, nullable=False)
    resulting_policy_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    execute_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RiskDecision(Base):
    __tablename__ = "risk_decisions"
    __table_args__ = (
        CheckConstraint("result IN ('ALLOW','SCALE','DENY')", name="ck_risk_decisions_result"),
        CheckConstraint("approved_quantity >= 0", name="ck_risk_decisions_quantity_nonnegative"),
        CheckConstraint("risk_amount >= 0", name="ck_risk_decisions_risk_nonnegative"),
        Index("ix_risk_decisions_proposal_created", "proposal_id", "created_at"),
        ForeignKeyConstraint(
            ["team_id", "proposal_id"],
            ["proposals.team_id", "proposals.proposal_id"],
            name="fk_risk_decisions_team_proposal",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["team_id", "policy_id"],
            ["risk_policies.team_id", "risk_policies.policy_id"],
            name="fk_risk_decisions_team_policy",
            ondelete="RESTRICT",
        ),
    )

    decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    proposal_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    policy_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    input_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    approved_quantity: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    risk_amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    data_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TradingAuthorization(Base):
    __tablename__ = "trading_authorizations"
    __table_args__ = (
        UniqueConstraint("proposal_id", name="uq_trading_authorizations_proposal"),
        UniqueConstraint(
            "team_id",
            "authorization_id",
            name="uq_trading_authorizations_team_identity",
        ),
        CheckConstraint("direction IN ('LONG','SHORT')", name="ck_authorizations_direction"),
        CheckConstraint(
            "environment IN ('SHADOW','TESTNET','LIVE')",
            name="ck_authorizations_environment",
        ),
        CheckConstraint("quantity_limit > 0", name="ck_authorizations_quantity_positive"),
        CheckConstraint("used_quantity >= 0", name="ck_authorizations_used_nonnegative"),
        CheckConstraint(
            "used_quantity <= quantity_limit", name="ck_authorizations_used_within_limit"
        ),
        CheckConstraint("risk_limit > 0", name="ck_authorizations_risk_positive"),
        CheckConstraint("allowed_adds >= 0", name="ck_authorizations_adds_nonnegative"),
        CheckConstraint(
            "used_adds >= 0 AND used_adds <= allowed_adds", name="ck_authorizations_adds"
        ),
        ForeignKeyConstraint(
            ["team_id", "proposal_id"],
            ["proposals.team_id", "proposals.proposal_id"],
            name="fk_trading_authorizations_team_proposal",
            ondelete="CASCADE",
        ),
    )

    authorization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    proposal_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    risk_decision_id: Mapped[UUID] = mapped_column(ForeignKey("risk_decisions.decision_id"))
    account_id: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.instrument_id"))
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity_limit: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    used_quantity: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, default=Decimal(0))
    risk_limit: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    allowed_adds: Mapped[int] = mapped_column(Integer, nullable=False)
    used_adds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    add_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        UniqueConstraint("authorization_id", name="uq_campaigns_authorization"),
        UniqueConstraint("team_id", "campaign_id", name="uq_campaigns_team_identity"),
        CheckConstraint("direction IN ('LONG','SHORT')", name="ck_campaigns_direction"),
        CheckConstraint(
            "environment IN ('SHADOW','TESTNET','LIVE')", name="ck_campaigns_environment"
        ),
        CheckConstraint(
            "status IN ('OPENING','OPEN','REDUCING','CLOSING','CLOSED','UNKNOWN')",
            name="ck_campaigns_status",
        ),
        CheckConstraint("current_target_quantity >= 0", name="ck_campaigns_target_nonnegative"),
        CheckConstraint("target_version >= 0", name="ck_campaigns_target_version_nonnegative"),
        ForeignKeyConstraint(
            ["team_id", "proposal_id"],
            ["proposals.team_id", "proposals.proposal_id"],
            name="fk_campaigns_team_proposal",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["team_id", "authorization_id"],
            ["trading_authorizations.team_id", "trading_authorizations.authorization_id"],
            name="fk_campaigns_team_authorization",
            ondelete="CASCADE",
        ),
        Index(
            "uq_campaigns_one_unclosed_scope",
            "team_id",
            "account_id",
            "venue",
            "environment",
            "instrument_id",
            unique=True,
            postgresql_where=text("status <> 'CLOSED'"),
        ),
    )

    campaign_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    proposal_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    authorization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.instrument_id"))
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    current_target_quantity: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    target_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    target_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_urgency: Mapped[str | None] = mapped_column(String(24), nullable=True)
    target_calculated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    realized_pnl: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, default=Decimal(0))
    unrealized_pnl: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, default=Decimal(0))
    final_pnl: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False, default=Decimal(0))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RiskReservation(Base):
    __tablename__ = "risk_reservations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('RESERVED','OPEN','UNKNOWN','RELEASED')",
            name="ck_risk_reservations_status",
        ),
        CheckConstraint("amount >= 0", name="ck_risk_reservations_amount_nonnegative"),
        ForeignKeyConstraint(
            ["team_id", "campaign_id"],
            ["campaigns.team_id", "campaigns.campaign_id"],
            name="fk_risk_reservations_team_campaign",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["team_id", "authorization_id"],
            ["trading_authorizations.team_id", "trading_authorizations.authorization_id"],
            name="fk_risk_reservations_team_authorization",
            ondelete="CASCADE",
        ),
        Index("ix_risk_reservations_campaign_status", "campaign_id", "status"),
        Index("ix_risk_reservations_team_status", "team_id", "status"),
    )

    reservation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    campaign_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    authorization_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OrderIntent(Base):
    __tablename__ = "order_intents"
    __table_args__ = (
        UniqueConstraint("reservation_id", name="uq_order_intents_reservation"),
        CheckConstraint("kind IN ('INITIAL','ADD','REDUCE','EXIT')", name="ck_order_intents_kind"),
        CheckConstraint("side IN ('BUY','SELL')", name="ck_order_intents_side"),
        CheckConstraint(
            "status IN ('PENDING','RESERVED','READY','DISPATCHING','SENT',"
            "'PARTIALLY_FILLED','FILLED','CANCELLED','REJECTED','UNKNOWN')",
            name="ck_order_intents_status",
        ),
        CheckConstraint(
            "dispatch_backend IS NULL OR dispatch_backend = 'FREQTRADE'",
            name="ck_order_intents_dispatch_backend",
        ),
        CheckConstraint(
            "dispatch_account_version IS NULL OR dispatch_account_version >= 1",
            name="ck_order_intents_dispatch_account_version",
        ),
        CheckConstraint(
            "dispatch_auth_version IS NULL OR dispatch_auth_version >= 1",
            name="ck_order_intents_dispatch_auth_version",
        ),
        CheckConstraint(
            "dispatch_fencing_token IS NULL OR dispatch_fencing_token >= 1",
            name="ck_order_intents_dispatch_fencing_token",
        ),
        CheckConstraint(
            "(dispatch_backend IS NULL AND dispatch_account_version IS NULL "
            "AND dispatch_auth_version IS NULL AND dispatch_owner_id IS NULL "
            "AND dispatch_fencing_token IS NULL AND dispatch_started_at IS NULL "
            "AND dispatch_external_id IS NULL) OR "
            "(dispatch_backend = 'FREQTRADE' AND dispatch_account_version IS NOT NULL "
            "AND dispatch_auth_version IS NOT NULL AND dispatch_owner_id IS NOT NULL "
            "AND dispatch_fencing_token IS NOT NULL AND dispatch_started_at IS NOT NULL)",
            name="ck_order_intents_dispatch_shape",
        ),
        CheckConstraint("quantity > 0", name="ck_order_intents_quantity_positive"),
        CheckConstraint(
            "limit_price IS NULL OR limit_price > 0",
            name="ck_order_intents_limit_price_positive",
        ),
        CheckConstraint(
            "kind = 'ADD' OR add_unit_consumed = false",
            name="ck_order_intents_add_unit_kind",
        ),
        Index("ix_order_intents_campaign_status", "campaign_id", "status"),
        Index(
            "uq_order_intents_one_active_campaign",
            "campaign_id",
            unique=True,
            postgresql_where=text(
                "status IN ('PENDING','RESERVED','READY','DISPATCHING','SENT',"
                "'PARTIALLY_FILLED','UNKNOWN')"
            ),
        ),
    )

    intent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    campaign_id: Mapped[UUID] = mapped_column(ForeignKey("campaigns.campaign_id"))
    authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("trading_authorizations.authorization_id")
    )
    reservation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("risk_reservations.reservation_id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(AMOUNT, nullable=True)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    trigger_source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    trigger_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    add_unit_consumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    target_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("positions.position_id"), nullable=True
    )
    position_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    dispatch_backend: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dispatch_account_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dispatch_auth_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dispatch_owner_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dispatch_fencing_token: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dispatch_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dispatch_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CommandReceipt(Base):
    __tablename__ = "command_receipts"
    __table_args__ = (
        UniqueConstraint(
            "caller_id", "operation", "idempotency_key", name="uq_command_receipts_scope"
        ),
        CheckConstraint("length(semantic_hash) = 64", name="ck_command_receipts_hash"),
    )

    receipt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    caller_id: Mapped[str] = mapped_column(String(255), nullable=False)
    operation: Mapped[str] = mapped_column(String(120), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ShadowPosition(Base):
    __tablename__ = "shadow_positions"
    __table_args__ = (
        CheckConstraint("generation >= 1", name="ck_shadow_positions_generation"),
        CheckConstraint(
            "average_entry_price >= 0 AND mark_price > 0",
            name="ck_shadow_positions_prices",
        ),
        CheckConstraint(
            "status IN ('OPEN','CLOSED','ARCHIVED')", name="ck_shadow_positions_status"
        ),
        CheckConstraint("version >= 1", name="ck_shadow_positions_version"),
        UniqueConstraint(
            "team_id",
            "generation",
            "shadow_instrument_id",
            name="uq_shadow_positions_generation_instrument",
        ),
        ForeignKeyConstraint(
            ["team_id", "source_account_id", "venue"],
            [
                "exchange_accounts.team_id",
                "exchange_accounts.account_id",
                "exchange_accounts.venue",
            ],
            name="fk_shadow_positions_exchange_account",
            ondelete="RESTRICT",
        ),
        Index("ix_shadow_positions_active", "team_id", "generation", "status"),
    )

    shadow_position_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    shadow_account_id: Mapped[UUID] = mapped_column(
        ForeignKey("team_shadow_accounts.shadow_account_id", ondelete="RESTRICT"),
        nullable=False,
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    shadow_instrument_id: Mapped[UUID] = mapped_column(
        ForeignKey("shadow_instruments.shadow_instrument_id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_account_id: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    average_entry_price: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    mark_price: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ShadowOrder(Base):
    __tablename__ = "shadow_orders"
    __table_args__ = (
        CheckConstraint("generation >= 1", name="ck_shadow_orders_generation"),
        CheckConstraint("side IN ('BUY','SELL')", name="ck_shadow_orders_side"),
        CheckConstraint(
            "order_type IN ('MARKET','LIMIT','PROTECTION')", name="ck_shadow_orders_type"
        ),
        CheckConstraint(
            "status IN ('OPEN','TRIGGERED','FILLED','CANCELLED','BLOCKED')",
            name="ck_shadow_orders_status",
        ),
        CheckConstraint(
            "quantity > 0 AND filled_quantity >= 0 AND filled_quantity <= quantity",
            name="ck_shadow_orders_quantities",
        ),
        CheckConstraint("fee >= 0", name="ck_shadow_orders_fee"),
        CheckConstraint(
            "(order_type = 'LIMIT' AND limit_price IS NOT NULL AND limit_price > 0) OR "
            "(order_type = 'MARKET' AND limit_price IS NULL) OR "
            "(order_type = 'PROTECTION' AND trigger_price IS NOT NULL "
            "AND trigger_type IN ('STOP_LOSS','TAKE_PROFIT') "
            "AND execution_type IN ('MARKET','LIMIT') "
            "AND ((execution_type = 'LIMIT' AND limit_price IS NOT NULL AND limit_price > 0) "
            "OR (execution_type = 'MARKET' AND limit_price IS NULL)))",
            name="ck_shadow_orders_shape",
        ),
        CheckConstraint(
            "(order_type = 'PROTECTION' AND reduce_only) OR order_type <> 'PROTECTION'",
            name="ck_shadow_orders_protection_reduce_only",
        ),
        CheckConstraint("version >= 1", name="ck_shadow_orders_version"),
        ForeignKeyConstraint(
            ["team_id", "source_account_id", "venue"],
            [
                "exchange_accounts.team_id",
                "exchange_accounts.account_id",
                "exchange_accounts.venue",
            ],
            name="fk_shadow_orders_exchange_account",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("order_intent_id", name="uq_shadow_orders_intent"),
        UniqueConstraint(
            "team_id", "generation", "idempotency_key", name="uq_shadow_orders_idempotency"
        ),
        Index("ix_shadow_orders_open", "team_id", "generation", "status"),
    )

    shadow_order_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    shadow_account_id: Mapped[UUID] = mapped_column(
        ForeignKey("team_shadow_accounts.shadow_account_id", ondelete="RESTRICT"),
        nullable=False,
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    shadow_instrument_id: Mapped[UUID] = mapped_column(
        ForeignKey("shadow_instruments.shadow_instrument_id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_account_id: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    campaign_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("campaigns.campaign_id", ondelete="SET NULL"), nullable=True
    )
    order_intent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("order_intents.intent_id", ondelete="SET NULL"), nullable=True
    )
    shadow_position_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("shadow_positions.shadow_position_id", ondelete="SET NULL"), nullable=True
    )
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(AMOUNT, nullable=True)
    trigger_price: Mapped[Decimal | None] = mapped_column(AMOUNT, nullable=True)
    trigger_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    execution_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    fill_price: Mapped[Decimal | None] = mapped_column(AMOUNT, nullable=True)
    fee: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ShadowFill(Base):
    __tablename__ = "shadow_fills"
    __table_args__ = (
        CheckConstraint("generation >= 1", name="ck_shadow_fills_generation"),
        CheckConstraint("side IN ('BUY','SELL')", name="ck_shadow_fills_side"),
        CheckConstraint(
            "quantity > 0 AND price > 0 AND notional > 0 AND fee >= 0",
            name="ck_shadow_fills_amounts",
        ),
        UniqueConstraint("shadow_order_id", name="uq_shadow_fills_order"),
        Index("ix_shadow_fills_team_time", "team_id", "generation", "executed_at"),
    )

    shadow_fill_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    shadow_order_id: Mapped[UUID] = mapped_column(
        ForeignKey("shadow_orders.shadow_order_id", ondelete="RESTRICT"), nullable=False
    )
    shadow_account_id: Mapped[UUID] = mapped_column(
        ForeignKey("team_shadow_accounts.shadow_account_id", ondelete="RESTRICT"),
        nullable=False,
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    shadow_instrument_id: Mapped[UUID] = mapped_column(
        ForeignKey("shadow_instruments.shadow_instrument_id", ondelete="RESTRICT"),
        nullable=False,
    )
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    price: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    notional: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    fee: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VenueOrder(Base):
    __tablename__ = "venue_orders"
    __table_args__ = (
        UniqueConstraint(
            "team_id",
            "environment",
            "account_id",
            "venue",
            "venue_order_id",
            name="uq_venue_orders_external",
        ),
        UniqueConstraint(
            "team_id",
            "environment",
            "account_id",
            "venue",
            "client_order_id",
            name="uq_venue_orders_client_identity",
        ),
        UniqueConstraint("order_intent_id", name="uq_venue_orders_intent"),
        CheckConstraint("side IN ('BUY','SELL')", name="ck_venue_orders_side"),
        CheckConstraint(
            "status IN ('SENT','PARTIALLY_FILLED','FILLED','CANCELLED','REJECTED','UNKNOWN')",
            name="ck_venue_orders_status",
        ),
        CheckConstraint(
            "environment IN ('SHADOW','TESTNET','LIVE')",
            name="ck_venue_orders_environment",
        ),
        CheckConstraint("ordered_quantity >= 0", name="ck_venue_orders_quantity_nonnegative"),
        CheckConstraint("filled_quantity >= 0", name="ck_venue_orders_filled_nonnegative"),
        Index(
            "ix_venue_orders_scope",
            "team_id",
            "environment",
            "account_id",
            "venue",
            "instrument_id",
        ),
        ForeignKeyConstraint(
            ["team_id", "account_id", "venue"],
            [
                "exchange_accounts.team_id",
                "exchange_accounts.account_id",
                "exchange_accounts.venue",
            ],
            name="fk_venue_orders_team_exchange_account",
            ondelete="RESTRICT",
        ),
    )

    venue_order_fact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    order_intent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("order_intents.intent_id"), nullable=True
    )
    account_id: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.instrument_id"))
    venue_order_id: Mapped[str] = mapped_column(String(255), nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    ordered_quantity: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VenueFill(Base):
    __tablename__ = "venue_fills"
    __table_args__ = (
        UniqueConstraint(
            "team_id",
            "environment",
            "account_id",
            "venue",
            "venue_fill_id",
            name="uq_venue_fills_external",
        ),
        CheckConstraint(
            "environment IN ('SHADOW','TESTNET','LIVE')",
            name="ck_venue_fills_environment",
        ),
        CheckConstraint("side IN ('BUY','SELL')", name="ck_venue_fills_side"),
        CheckConstraint("quantity > 0", name="ck_venue_fills_quantity_positive"),
        CheckConstraint("price > 0", name="ck_venue_fills_price_positive"),
        CheckConstraint("fee >= 0", name="ck_venue_fills_fee_nonnegative"),
        CheckConstraint("slippage_cost >= 0", name="ck_venue_fills_slippage_nonnegative"),
        Index("ix_venue_fills_campaign_time", "campaign_id", "executed_at"),
        Index(
            "ix_venue_fills_scope",
            "team_id",
            "environment",
            "account_id",
            "venue",
            "instrument_id",
        ),
        ForeignKeyConstraint(
            ["team_id", "account_id", "venue"],
            [
                "exchange_accounts.team_id",
                "exchange_accounts.account_id",
                "exchange_accounts.venue",
            ],
            name="fk_venue_fills_team_exchange_account",
            ondelete="RESTRICT",
        ),
    )

    venue_fill_fact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    venue_fill_id: Mapped[str] = mapped_column(String(255), nullable=False)
    order_intent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("order_intents.intent_id"), nullable=True
    )
    campaign_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("campaigns.campaign_id"), nullable=True
    )
    account_id: Mapped[str] = mapped_column(String(120), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.instrument_id"))
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    price: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    fee: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    fee_currency: Mapped[str] = mapped_column(String(32), nullable=False)
    slippage_cost: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint(
            "team_id",
            "environment",
            "account_id",
            "venue",
            "instrument_id",
            name="uq_positions_scope",
        ),
        CheckConstraint(
            "environment IN ('SHADOW','TESTNET','LIVE')", name="ck_positions_environment"
        ),
        CheckConstraint("fact_status IN ('KNOWN','UNKNOWN')", name="ck_positions_fact_status"),
        ForeignKeyConstraint(
            ["team_id", "account_id", "venue"],
            [
                "exchange_accounts.team_id",
                "exchange_accounts.account_id",
                "exchange_accounts.venue",
            ],
            name="fk_positions_team_exchange_account",
            ondelete="RESTRICT",
        ),
    )

    position_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.instrument_id"))
    quantity: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    average_entry_price: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    mark_price: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    fact_status: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProtectionOrder(Base):
    __tablename__ = "protection_orders"
    __table_args__ = (
        UniqueConstraint("position_id", name="uq_protection_orders_position"),
        CheckConstraint("status IN ('ACTIVE','DEGRADED','UNKNOWN')", name="ck_protection_status"),
        CheckConstraint("quantity >= 0", name="ck_protection_quantity_nonnegative"),
        CheckConstraint("trigger_price >= 0", name="ck_protection_trigger_nonnegative"),
    )

    protection_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    position_id: Mapped[UUID] = mapped_column(
        ForeignKey("positions.position_id", ondelete="CASCADE"), nullable=False
    )
    venue_order_id: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    trigger_price: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    fully_covered: Mapped[bool] = mapped_column(Boolean, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AccountEquity(Base):
    __tablename__ = "account_equities"
    __table_args__ = (
        UniqueConstraint(
            "team_id",
            "environment",
            "account_id",
            "venue",
            "currency",
            name="uq_account_equities_scope",
        ),
        UniqueConstraint(
            "team_id",
            "account_equity_id",
            "account_id",
            "venue",
            name="uq_account_equities_team_identity",
        ),
        CheckConstraint(
            "environment IN ('SHADOW','TESTNET','LIVE')",
            name="ck_account_equities_environment",
        ),
        CheckConstraint("fact_status IN ('KNOWN','UNKNOWN')", name="ck_account_equities_status"),
        CheckConstraint("equity >= 0", name="ck_account_equities_equity_nonnegative"),
        CheckConstraint("available_balance >= 0", name="ck_account_equities_balance_nonnegative"),
        CheckConstraint(
            "withdrawable_balance IS NULL OR withdrawable_balance >= 0",
            name="ck_account_equities_withdrawable_nonnegative",
        ),
        CheckConstraint(
            "location_type IN ('VENUE','VAULT')", name="ck_account_equities_location_type"
        ),
        CheckConstraint(
            "control_status IN ('CONTROLLED','READ_ONLY','UNKNOWN')",
            name="ck_account_equities_control_status",
        ),
        CheckConstraint(
            "deposit_status IN ('READY','PENDING','UNKNOWN')",
            name="ck_account_equities_deposit_status",
        ),
        CheckConstraint(
            "valuation_price IS NULL OR valuation_price > 0",
            name="ck_account_equities_valuation_price",
        ),
        CheckConstraint(
            "valuation_equity IS NULL OR valuation_equity >= 0",
            name="ck_account_equities_valuation_equity",
        ),
    )

    account_equity_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    equity: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    available_balance: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    withdrawable_balance: Mapped[Decimal | None] = mapped_column(AMOUNT, nullable=True)
    currency: Mapped[str] = mapped_column(String(32), nullable=False)
    location_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="VENUE", server_default="VENUE"
    )
    control_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="READ_ONLY", server_default="READ_ONLY"
    )
    deposit_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="READY", server_default="READY"
    )
    network: Mapped[str | None] = mapped_column(String(64), nullable=True)
    address_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    valuation_currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    valuation_price: Mapped[Decimal | None] = mapped_column(AMOUNT, nullable=True)
    valuation_equity: Mapped[Decimal | None] = mapped_column(AMOUNT, nullable=True)
    valuation_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fact_status: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AccountEquityObservation(Base):
    __tablename__ = "account_equity_observations"
    __table_args__ = (
        UniqueConstraint(
            "account_equity_id",
            "observed_at",
            name="uq_account_equity_observations_fact_time",
        ),
        CheckConstraint(
            "environment IN ('SHADOW','TESTNET','LIVE')",
            name="ck_account_equity_observations_environment",
        ),
        CheckConstraint(
            "location_type IN ('VENUE','VAULT')",
            name="ck_account_equity_observations_location_type",
        ),
        CheckConstraint("equity >= 0", name="ck_account_equity_observations_equity"),
        CheckConstraint(
            "available_balance >= 0",
            name="ck_account_equity_observations_available_balance",
        ),
        CheckConstraint(
            "usd_equity IS NULL OR usd_equity >= 0",
            name="ck_account_equity_observations_usd_equity",
        ),
        Index(
            "ix_account_equity_observations_scope_time",
            "team_id",
            "environment",
            "location_type",
            "venue",
            "account_id",
            "observed_at",
        ),
        ForeignKeyConstraint(
            ["team_id", "account_equity_id", "account_id", "venue"],
            [
                "account_equities.team_id",
                "account_equities.account_equity_id",
                "account_equities.account_id",
                "account_equities.venue",
            ],
            name="fk_account_equity_observations_team_equity",
            ondelete="CASCADE",
        ),
    )

    observation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    account_equity_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    location_type: Mapped[str] = mapped_column(String(16), nullable=False)
    account_id: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(32), nullable=False)
    equity: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    available_balance: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    usd_equity: Mapped[Decimal | None] = mapped_column(AMOUNT, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FundingPayment(Base):
    __tablename__ = "funding_payments"
    __table_args__ = (
        UniqueConstraint(
            "team_id",
            "environment",
            "account_id",
            "venue",
            "venue_payment_id",
            name="uq_funding_payments_external",
        ),
        CheckConstraint(
            "environment IN ('SHADOW','TESTNET','LIVE')",
            name="ck_funding_payments_environment",
        ),
        Index("ix_funding_payments_campaign_time", "campaign_id", "paid_at"),
        Index(
            "ix_funding_payments_scope",
            "team_id",
            "environment",
            "account_id",
            "venue",
            "instrument_id",
        ),
        ForeignKeyConstraint(
            ["team_id", "account_id", "venue"],
            [
                "exchange_accounts.team_id",
                "exchange_accounts.account_id",
                "exchange_accounts.venue",
            ],
            name="fk_funding_payments_team_exchange_account",
            ondelete="RESTRICT",
        ),
    )

    funding_payment_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    campaign_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("campaigns.campaign_id"), nullable=True
    )
    account_id: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.instrument_id"))
    venue_payment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    currency: Mapped[str] = mapped_column(String(32), nullable=False)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('MATCH','DIFFERENCE','UNKNOWN','MANUAL_REQUIRED','RESOLVED')",
            name="ck_reconciliation_runs_status",
        ),
        Index(
            "ix_reconciliation_scope_completed",
            "team_id",
            "execution_scope",
            "completed_at",
        ),
    )

    reconciliation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), nullable=False
    )
    execution_scope: Mapped[str] = mapped_column(String(255), nullable=False)
    campaign_id: Mapped[UUID | None] = mapped_column(ForeignKey("campaigns.campaign_id"))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    is_computed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    differences: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SenderLease(Base):
    __tablename__ = "sender_leases"
    __table_args__ = (
        CheckConstraint("fencing_token >= 1", name="ck_sender_leases_token_positive"),
    )

    team_id: Mapped[UUID] = mapped_column(
        ForeignKey("teams.team_id", ondelete="RESTRICT"), primary_key=True
    )
    execution_scope: Mapped[str] = mapped_column(String(255), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CapabilityGate(Base):
    __tablename__ = "capability_gates"
    __table_args__ = (
        CheckConstraint(
            "capability_key IN ('LIVE_ORDER_SEND','CAPITAL_TRANSFER','AUTO_ADD',"
            "'AUTO_PROFIT_SWEEP','AUTO_OPERATING_REFILL')",
            name="ck_capability_gates_key",
        ),
        CheckConstraint("status IN ('DISABLED','ENABLED')", name="ck_capability_gates_status"),
        CheckConstraint("version >= 1", name="ck_capability_gates_version"),
    )

    capability_key: Mapped[str] = mapped_column(String(120), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    operator_id: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_object", "object_type", "object_id", "created_at"),
        Index("ix_audit_events_correlation", "correlation_id", "created_at"),
        Index("ix_audit_events_workspace_created", "workspace_id", "created_at"),
        Index("ix_audit_events_team_created", "team_id", "created_at"),
        Index("ix_audit_events_api_client_created", "api_client_id", "created_at"),
        CheckConstraint(
            "environment IS NULL OR environment IN ('SHADOW','TESTNET','LIVE')",
            name="ck_audit_events_environment",
        ),
        CheckConstraint(
            "generation IS NULL OR generation >= 1",
            name="ck_audit_events_generation",
        ),
        Index(
            "ix_audit_events_team_account_created",
            "team_id",
            "account_id",
            "created_at",
        ),
    )

    audit_event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    workspace_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workspaces.workspace_id"), nullable=True
    )
    team_id: Mapped[UUID | None] = mapped_column(ForeignKey("teams.team_id"), nullable=True)
    account_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    environment: Mapped[str | None] = mapped_column(String(16), nullable=True)
    generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rule_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    api_client_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("api_clients.api_client_id", ondelete="RESTRICT"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    object_type: Mapped[str] = mapped_column(String(120), nullable=False)
    object_id: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    object_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
