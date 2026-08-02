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


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("principal_type IN ('HUMAN','SERVICE')", name="ck_users_principal_type"),
    )

    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    identity_subject: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True)
    principal_type: Mapped[str] = mapped_column(String(16), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RoleAssignment(Base):
    __tablename__ = "role_assignments"
    __table_args__ = (
        CheckConstraint(
            "role IN ('OBSERVER','PROPOSER','REVIEWER','OPERATOR','TREASURY_ADMIN','SYSTEM_ADMIN')",
            name="ck_role_assignments_role",
        ),
        Index("ix_role_assignments_user", "user_id"),
    )

    assignment_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
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

    feed_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    contract_version: Mapped[str] = mapped_column(String(120), nullable=False)
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    next_allowed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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
        Index(
            "uq_proposals_system_candidate",
            "source_candidate_id",
            unique=True,
            postgresql_where=text("source = 'SYSTEM' AND source_candidate_id IS NOT NULL"),
        ),
    )

    proposal_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
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


class TransferProposal(Base):
    __tablename__ = "transfer_proposals"
    __table_args__ = (
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
        Index("ix_transfer_proposals_status_expires", "status", "expires_at"),
    )

    transfer_proposal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
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
        UniqueConstraint("transfer_proposal_id", name="uq_transfer_authorizations_proposal"),
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
    transfer_proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("transfer_proposals.transfer_proposal_id"), nullable=False
    )
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
        UniqueConstraint("transfer_authorization_id", name="uq_capital_transfers_authorization"),
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
        Index("ix_capital_transfers_status_updated", "status", "updated_at"),
    )

    capital_transfer_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    transfer_authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("transfer_authorizations.transfer_authorization_id"), nullable=False
    )
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


class CapitalAutomationPolicy(Base):
    __tablename__ = "capital_automation_policies"
    __table_args__ = (
        UniqueConstraint(
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
        CheckConstraint(
            "system_state IN ('NORMAL','NO_PYRAMID','REDUCE_ONLY','KILL_SWITCH')",
            name="ck_risk_policies_system_state",
        ),
        CheckConstraint("max_total_risk > 0", name="ck_risk_policies_max_risk_positive"),
        CheckConstraint("max_fact_age_seconds > 0", name="ck_risk_policies_age_positive"),
        CheckConstraint("revision >= 1", name="ck_risk_policies_revision"),
        Index(
            "uq_risk_policies_one_active",
            "active",
            unique=True,
            postgresql_where=text("active"),
        ),
    )

    policy_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    version: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    system_state: Mapped[str] = mapped_column(String(32), nullable=False)
    max_total_risk: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
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
            text("(true)"),
            unique=True,
            postgresql_where=text("status IN ('PENDING_REVIEW','APPROVED')"),
        ),
        Index("ix_risk_control_change_requests_created", "created_at"),
    )

    request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    requester_id: Mapped[UUID] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    restore_auto_add: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    require_live_scope: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_policy_id: Mapped[UUID] = mapped_column(
        ForeignKey("risk_policies.policy_id"), nullable=False
    )
    source_policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    source_policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_auto_add_status: Mapped[str] = mapped_column(String(16), nullable=False)
    source_auto_add_version: Mapped[int] = mapped_column(Integer, nullable=False)
    required_scopes: Mapped[list[dict[str, str]]] = mapped_column(JSONB, nullable=False)
    resulting_policy_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("risk_policies.policy_id"), nullable=True
    )
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
    )

    decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    proposal_id: Mapped[UUID] = mapped_column(ForeignKey("proposals.proposal_id"))
    policy_id: Mapped[UUID] = mapped_column(ForeignKey("risk_policies.policy_id"))
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
    )

    authorization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    proposal_id: Mapped[UUID] = mapped_column(ForeignKey("proposals.proposal_id"))
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
        Index(
            "uq_campaigns_one_unclosed_scope",
            "account_id",
            "venue",
            "environment",
            "instrument_id",
            unique=True,
            postgresql_where=text("status <> 'CLOSED'"),
        ),
    )

    campaign_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    proposal_id: Mapped[UUID] = mapped_column(ForeignKey("proposals.proposal_id"))
    authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("trading_authorizations.authorization_id")
    )
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
        Index("ix_risk_reservations_campaign_status", "campaign_id", "status"),
    )

    reservation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    campaign_id: Mapped[UUID] = mapped_column(ForeignKey("campaigns.campaign_id"))
    authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("trading_authorizations.authorization_id")
    )
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
            "status IN ('PENDING','RESERVED','READY','SENT','PARTIALLY_FILLED','FILLED',"
            "'CANCELLED','REJECTED','UNKNOWN')",
            name="ck_order_intents_status",
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
                "status IN ('PENDING','RESERVED','READY','SENT','PARTIALLY_FILLED','UNKNOWN')"
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


class VenueOrder(Base):
    __tablename__ = "venue_orders"
    __table_args__ = (
        UniqueConstraint(
            "environment",
            "account_id",
            "venue",
            "venue_order_id",
            name="uq_venue_orders_external",
        ),
        UniqueConstraint(
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
            "environment",
            "account_id",
            "venue",
            "instrument_id",
        ),
    )

    venue_order_fact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
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
            "environment",
            "account_id",
            "venue",
            "instrument_id",
        ),
    )

    venue_fill_fact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
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
    )

    position_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
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
            "environment",
            "account_id",
            "venue",
            "currency",
            name="uq_account_equities_scope",
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
            "environment",
            "location_type",
            "venue",
            "account_id",
            "observed_at",
        ),
    )

    observation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    account_equity_id: Mapped[UUID] = mapped_column(
        ForeignKey("account_equities.account_equity_id", ondelete="CASCADE"), nullable=False
    )
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
            "environment",
            "account_id",
            "venue",
            "instrument_id",
        ),
    )

    funding_payment_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
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
        Index("ix_reconciliation_scope_completed", "execution_scope", "completed_at"),
    )

    reconciliation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
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
    )

    audit_event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    object_type: Mapped[str] = mapped_column(String(120), nullable=False)
    object_id: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    object_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
