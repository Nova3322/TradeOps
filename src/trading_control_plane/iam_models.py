from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_control_plane.database import Base


class IdentityPrincipal(Base):
    __tablename__ = "identity_principals"
    __table_args__ = (
        CheckConstraint(
            "principal_type IN ('HUMAN', 'SERVICE', 'EXECUTION', 'TREASURY', 'BREAK_GLASS')",
            name="ck_identity_principals_type",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED')",
            name="ck_identity_principals_status",
        ),
        CheckConstraint("version >= 1", name="ck_identity_principals_version_positive"),
        UniqueConstraint(
            "organization_id",
            "external_subject_ref",
            name="uq_identity_principals_external_subject",
        ),
    )

    principal_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    principal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    external_subject_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RoleAssignment(Base):
    __tablename__ = "role_assignments"
    __table_args__ = (
        CheckConstraint(
            "role_key IN "
            "('OBSERVER', 'PROPOSER', 'REVIEWER', 'OPERATOR', "
            "'RISK_ADMIN', 'TREASURY_ADMIN', 'SYSTEM_ADMIN')",
            name="ck_role_assignments_role",
        ),
        CheckConstraint("version >= 1", name="ck_role_assignments_version_positive"),
        CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="ck_role_assignments_valid_window",
        ),
        Index("ix_role_assignments_principal_active", "principal_id", "revoked_at"),
    )

    assignment_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    principal_id: Mapped[UUID] = mapped_column(
        ForeignKey("identity_principals.principal_id", ondelete="RESTRICT"), nullable=False
    )
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    role_key: Mapped[str] = mapped_column(String(40), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PermissionScope(Base):
    __tablename__ = "permission_scopes"
    __table_args__ = (
        CheckConstraint(
            "risk_tier IS NULL OR risk_tier IN ('LOW', 'MEDIUM', 'HIGH')",
            name="ck_permission_scopes_risk_tier",
        ),
        CheckConstraint(
            "channel IS NULL OR channel IN ('WEB', 'PWA', 'TELEGRAM', 'SYSTEM', 'INTERNAL')",
            name="ck_permission_scopes_channel",
        ),
        Index("ix_permission_scopes_assignment", "assignment_id"),
    )

    scope_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    assignment_id: Mapped[UUID] = mapped_column(
        ForeignKey("role_assignments.assignment_id", ondelete="RESTRICT"), nullable=False
    )
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    venue: Mapped[str | None] = mapped_column(String(80), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(80), nullable=True)
    risk_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    action_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    channel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExplicitDeny(Base):
    __tablename__ = "explicit_denies"
    __table_args__ = (
        CheckConstraint(
            "risk_tier IS NULL OR risk_tier IN ('LOW', 'MEDIUM', 'HIGH')",
            name="ck_explicit_denies_risk_tier",
        ),
        CheckConstraint(
            "channel IS NULL OR channel IN ('WEB', 'PWA', 'TELEGRAM', 'SYSTEM', 'INTERNAL')",
            name="ck_explicit_denies_channel",
        ),
        CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="ck_explicit_denies_valid_window",
        ),
        Index("ix_explicit_denies_principal_active", "principal_id", "revoked_at"),
    )

    deny_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    principal_id: Mapped[UUID] = mapped_column(
        ForeignKey("identity_principals.principal_id", ondelete="RESTRICT"), nullable=False
    )
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    action_id: Mapped[str] = mapped_column(String(160), nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    venue: Mapped[str | None] = mapped_column(String(80), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(80), nullable=True)
    risk_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    channel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ActionAssurance(Base):
    __tablename__ = "action_assurances"
    __table_args__ = (
        CheckConstraint(
            "status IN ('VERIFIED', 'REVOKED')",
            name="ck_action_assurances_status",
        ),
        CheckConstraint(
            "channel IN ('WEB', 'PWA', 'TELEGRAM', 'SYSTEM', 'INTERNAL')",
            name="ck_action_assurances_channel",
        ),
        CheckConstraint(
            "expires_at > issued_at",
            name="ck_action_assurances_valid_window",
        ),
        CheckConstraint(
            "assurance_method = 'PASSKEY_WEBAUTHN'",
            name="ck_action_assurances_method",
        ),
        CheckConstraint(
            "assurance_level = 'ACTION_STEP_UP'",
            name="ck_action_assurances_level",
        ),
        CheckConstraint(
            "object_version >= 1",
            name="ck_action_assurances_object_version_positive",
        ),
        UniqueConstraint("auth_context_ref", name="uq_action_assurances_auth_context"),
        Index("ix_action_assurances_principal", "principal_id", "expires_at"),
    )

    assurance_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    principal_id: Mapped[UUID] = mapped_column(
        ForeignKey("identity_principals.principal_id", ondelete="RESTRICT"), nullable=False
    )
    auth_context_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    device_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    action_id: Mapped[str] = mapped_column(String(160), nullable=False)
    object_type: Mapped[str] = mapped_column(String(120), nullable=False)
    object_id: Mapped[str] = mapped_column(String(255), nullable=False)
    object_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assurance_method: Mapped[str] = mapped_column(String(40), nullable=False)
    assurance_level: Mapped[str] = mapped_column(String(40), nullable=False)
    verifier_ref: Mapped[str] = mapped_column(String(255), nullable=False)


class AuthorizationDecision(Base):
    __tablename__ = "authorization_decisions"
    __table_args__ = (
        CheckConstraint(
            "result IN ('ALLOW', 'DENY')",
            name="ck_authorization_decisions_result",
        ),
        CheckConstraint(
            "risk_tier IS NULL OR risk_tier IN ('LOW', 'MEDIUM', 'HIGH')",
            name="ck_authorization_decisions_risk_tier",
        ),
        CheckConstraint(
            "required_quorum IS NULL OR required_quorum IN (1, 2)",
            name="ck_authorization_decisions_quorum",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_authorization_decisions_hash_length",
        ),
        UniqueConstraint("request_id", name="uq_authorization_decisions_request"),
        Index("ix_authorization_decisions_principal", "principal_id", "decided_at"),
        Index("ix_authorization_decisions_object", "object_type", "object_id", "decided_at"),
    )

    decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    command_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    principal_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    action_id: Mapped[str] = mapped_column(String(160), nullable=False)
    object_type: Mapped[str] = mapped_column(String(120), nullable=False)
    object_id: Mapped[str] = mapped_column(String(255), nullable=False)
    object_version: Mapped[int] = mapped_column(Integer, nullable=False)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    venue: Mapped[str | None] = mapped_column(String(80), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(80), nullable=True)
    risk_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    device_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    auth_context_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    result: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    is_self_review: Mapped[bool] = mapped_column(Boolean, nullable=False)
    required_quorum: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matched_assignment_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    matched_deny_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
