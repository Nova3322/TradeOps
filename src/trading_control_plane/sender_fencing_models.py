from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_control_plane.database import Base


class ExecutionSenderScope(Base):
    """Immutable identity of one mutually exclusive order-sender domain."""

    __tablename__ = "execution_sender_scopes"
    __table_args__ = (
        CheckConstraint("schema_version = 1", name="ck_execution_sender_scopes_schema"),
        CheckConstraint(
            "environment = 'SHADOW' AND live_dispatch_eligible = false",
            name="ck_execution_sender_scopes_shadow_only",
        ),
        CheckConstraint("length(scope_hash) = 64", name="ck_execution_sender_scopes_hash"),
        UniqueConstraint(
            "organization_id",
            "venue",
            "execution_domain",
            "account_id",
            "account_abstraction",
            "position_mode",
            "margin_mode",
            "collateral_scope",
            "collateral_pool_id",
            name="uq_execution_sender_scopes_exact_scope",
        ),
        UniqueConstraint(
            "scope_id", "organization_id", name="uq_execution_sender_scopes_org_binding"
        ),
    )

    scope_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    venue: Mapped[str] = mapped_column(String(80), nullable=False)
    execution_domain: Mapped[str] = mapped_column(String(120), nullable=False)
    account_id: Mapped[str] = mapped_column(String(160), nullable=False)
    account_abstraction: Mapped[str] = mapped_column(String(80), nullable=False)
    position_mode: Mapped[str] = mapped_column(String(80), nullable=False)
    margin_mode: Mapped[str] = mapped_column(String(80), nullable=False)
    collateral_scope: Mapped[str] = mapped_column(String(120), nullable=False)
    collateral_pool_id: Mapped[str] = mapped_column(String(160), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    live_dispatch_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExecutionSenderLease(Base):
    """Immutable lease issuance; current authority is projected by the scope state."""

    __tablename__ = "execution_sender_leases"
    __table_args__ = (
        CheckConstraint("fencing_token > 0", name="ck_execution_sender_leases_token"),
        CheckConstraint(
            "environment = 'SHADOW' AND live_dispatch_eligible = false",
            name="ck_execution_sender_leases_shadow_only",
        ),
        CheckConstraint(
            "issued_at < initial_expires_at AND initial_expires_at <= max_expires_at",
            name="ck_execution_sender_leases_validity",
        ),
        CheckConstraint(
            "length(worker_config_hash) = 64 AND length(credential_fingerprint) = 64 "
            "AND length(lease_hash) = 64",
            name="ck_execution_sender_leases_hashes",
        ),
        ForeignKeyConstraint(
            ["scope_id", "organization_id"],
            ["execution_sender_scopes.scope_id", "execution_sender_scopes.organization_id"],
            name="fk_execution_sender_leases_scope_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "scope_id", "fencing_token", name="uq_execution_sender_leases_scope_token"
        ),
        UniqueConstraint(
            "scope_id",
            "lease_id",
            "fencing_token",
            name="uq_execution_sender_leases_state_binding",
        ),
        Index("ix_execution_sender_leases_owner", "owner_worker_id", "issued_at"),
    )

    lease_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(96), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    owner_worker_id: Mapped[str] = mapped_column(String(160), nullable=False)
    worker_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    live_dispatch_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    lease_policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    reconciliation_evidence_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    risk_state_ack_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    worker_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    initial_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class ExecutionSenderScopeState(Base):
    """The only current fencing authority for one exact execution domain."""

    __tablename__ = "execution_sender_scope_states"
    __table_args__ = (
        CheckConstraint(
            "status IN ('UNOWNED', 'LEASED', 'FENCED')",
            name="ck_execution_sender_scope_states_status",
        ),
        CheckConstraint("version >= 1", name="ck_execution_sender_scope_states_version"),
        CheckConstraint(
            "current_fencing_token >= 0", name="ck_execution_sender_scope_states_token"
        ),
        CheckConstraint(
            "(status = 'LEASED' AND active_lease_id IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND current_fencing_token > 0) OR "
            "(status IN ('UNOWNED', 'FENCED') AND active_lease_id IS NULL "
            "AND lease_expires_at IS NULL)",
            name="ck_execution_sender_scope_states_active_binding",
        ),
        ForeignKeyConstraint(
            ["scope_id", "active_lease_id", "current_fencing_token"],
            [
                "execution_sender_leases.scope_id",
                "execution_sender_leases.lease_id",
                "execution_sender_leases.fencing_token",
            ],
            name="fk_execution_sender_scope_states_active_lease",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    scope_id: Mapped[str] = mapped_column(
        ForeignKey("execution_sender_scopes.scope_id", ondelete="RESTRICT"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    current_fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    active_lease_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExecutionSenderScopeStateHistory(Base):
    __tablename__ = "execution_sender_scope_state_history"
    __table_args__ = (
        CheckConstraint("state_version >= 1", name="ck_execution_sender_history_version"),
        CheckConstraint(
            "status IN ('UNOWNED', 'LEASED', 'FENCED')",
            name="ck_execution_sender_history_status",
        ),
        CheckConstraint(
            "jsonb_typeof(state_snapshot) = 'object' AND length(state_hash) = 64",
            name="ck_execution_sender_history_snapshot",
        ),
        UniqueConstraint("scope_id", "state_version", name="uq_execution_sender_history_version"),
        Index("ix_execution_sender_history_time", "scope_id", "changed_at"),
    )

    history_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_id: Mapped[str] = mapped_column(
        ForeignKey("execution_sender_scopes.scope_id", ondelete="RESTRICT"), nullable=False
    )
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    active_lease_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    state_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ShadowDispatchClaim(Base):
    """Immutable proof that a shadow intent passed fencing; never an external-send permit."""

    __tablename__ = "shadow_dispatch_claims"
    __table_args__ = (
        CheckConstraint(
            "execution_mode = 'SHADOW' AND external_send_permitted = false "
            "AND live_gate_status = 'DISABLED'",
            name="ck_shadow_dispatch_claims_non_dispatchable",
        ),
        CheckConstraint(
            "fencing_token > 0 AND claimed_at < lease_expires_at",
            name="ck_shadow_dispatch_claims_lease_window",
        ),
        CheckConstraint(
            "length(worker_config_hash) = 64 AND length(credential_fingerprint) = 64 "
            "AND length(intent_snapshot_hash) = 64 AND length(capability_certificate_hash) = 64 "
            "AND length(scope_hash) = 64 AND length(lease_hash) = 64 "
            "AND length(claim_hash) = 64",
            name="ck_shadow_dispatch_claims_hashes",
        ),
        CheckConstraint(
            "(reconciliation_run_id IS NULL AND reconciliation_result_hash IS NULL) OR "
            "(reconciliation_run_id IS NOT NULL AND length(reconciliation_result_hash) = 64)",
            name="ck_shadow_dispatch_claims_reconciliation",
        ),
        ForeignKeyConstraint(
            ["scope_id", "organization_id"],
            ["execution_sender_scopes.scope_id", "execution_sender_scopes.organization_id"],
            name="fk_shadow_dispatch_claims_scope_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["scope_id", "lease_id", "fencing_token"],
            [
                "execution_sender_leases.scope_id",
                "execution_sender_leases.lease_id",
                "execution_sender_leases.fencing_token",
            ],
            name="fk_shadow_dispatch_claims_lease_binding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["capability_certificate_ref", "organization_id"],
            ["capability_certificates.certificate_id", "capability_certificates.organization_id"],
            name="fk_shadow_dispatch_claims_certificate_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["reconciliation_run_id", "scope_id", "lease_id", "fencing_token"],
            [
                "execution_reconciliation_runs.run_id",
                "execution_reconciliation_runs.scope_id",
                "execution_reconciliation_runs.lease_id",
                "execution_reconciliation_runs.fencing_token",
            ],
            name="fk_shadow_dispatch_claims_reconciliation_binding",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("order_intent_id", name="uq_shadow_dispatch_claims_order_intent"),
        UniqueConstraint(
            "scope_id", "client_order_id", name="uq_shadow_dispatch_claims_client_order_id"
        ),
        Index("ix_shadow_dispatch_claims_scope_time", "scope_id", "claimed_at"),
    )

    claim_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    order_intent_id: Mapped[UUID] = mapped_column(
        ForeignKey("order_intents.order_intent_id", ondelete="RESTRICT"), nullable=False
    )
    scope_id: Mapped[str] = mapped_column(String(96), nullable=False)
    lease_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(80), nullable=False)
    owner_worker_id: Mapped[str] = mapped_column(String(160), nullable=False)
    worker_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_certificate_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    reconciliation_run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reconciliation_result_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    external_send_permitted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    live_gate_status: Mapped[str] = mapped_column(String(20), nullable=False)
    intent_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_certificate_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    worker_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    claim_hash: Mapped[str] = mapped_column(String(64), nullable=False)
