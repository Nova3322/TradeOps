from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_control_plane.database import Base


class ExecutionReconciliationRun(Base):
    """Immutable identity and frozen input contract for one sender-session reconciliation."""

    __tablename__ = "execution_reconciliation_runs"
    __table_args__ = (
        CheckConstraint(
            "(schema_version = 1 AND jsonb_array_length(required_source_types) = 7) OR "
            "(schema_version = 2 AND jsonb_array_length(required_source_types) = 8)",
            name="ck_execution_reconciliation_runs_schema",
        ),
        CheckConstraint(
            "environment = 'SHADOW' AND live_dispatch_eligible = false",
            name="ck_execution_reconciliation_runs_shadow_only",
        ),
        CheckConstraint(
            "trigger_type IN ('STARTUP', 'PRIVATE_STREAM_RECONNECT', 'ORDER_UNKNOWN', "
            "'PARTIAL_FILL', 'CAMPAIGN_CLOSE', 'MANUAL_RECOVERY')",
            name="ck_execution_reconciliation_runs_trigger",
        ),
        CheckConstraint(
            "jsonb_typeof(required_source_types) = 'array'",
            name="ck_execution_reconciliation_runs_sources",
        ),
        CheckConstraint(
            "observation_window_start < observation_window_end AND started_at < deadline_at",
            name="ck_execution_reconciliation_runs_window",
        ),
        CheckConstraint(
            "fencing_token > 0 AND length(run_hash) = 64",
            name="ck_execution_reconciliation_runs_integrity",
        ),
        ForeignKeyConstraint(
            ["scope_id", "organization_id"],
            ["execution_sender_scopes.scope_id", "execution_sender_scopes.organization_id"],
            name="fk_execution_reconciliation_runs_scope_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["scope_id", "lease_id", "fencing_token"],
            [
                "execution_sender_leases.scope_id",
                "execution_sender_leases.lease_id",
                "execution_sender_leases.fencing_token",
            ],
            name="fk_execution_reconciliation_runs_lease_binding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["supersedes_run_id"],
            ["execution_reconciliation_runs.run_id"],
            name="fk_execution_reconciliation_runs_supersedes",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "run_id", "organization_id", name="uq_execution_reconciliation_runs_org_binding"
        ),
        UniqueConstraint(
            "run_id",
            "scope_id",
            "lease_id",
            "fencing_token",
            name="uq_execution_reconciliation_runs_claim_binding",
        ),
        Index(
            "ix_execution_reconciliation_runs_scope_time",
            "scope_id",
            "started_at",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(96), nullable=False)
    lease_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(40), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    live_dispatch_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    required_source_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    observation_window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    observation_window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    supersedes_run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    initiated_by: Mapped[str] = mapped_column(String(160), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    run_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class ExecutionReconciliationInput(Base):
    """Immutable collection snapshot and watermark from one required fact source."""

    __tablename__ = "execution_reconciliation_inputs"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('TRADING_LEDGER', 'VENUE_ORDERS', 'VENUE_FILLS', "
            "'VENUE_FUNDING', 'VENUE_POSITIONS', 'VENUE_BALANCES', 'VENUE_PROTECTION', "
            "'WORKER_LOCAL')",
            name="ck_execution_reconciliation_inputs_source",
        ),
        CheckConstraint(
            "collection_status IN ('COMPLETE', 'UNKNOWN')",
            name="ck_execution_reconciliation_inputs_status",
        ),
        CheckConstraint(
            "observed_from <= observed_through AND item_count >= 0",
            name="ck_execution_reconciliation_inputs_window",
        ),
        CheckConstraint(
            "length(payload_hash) = 64 AND length(evidence_hash) = 64 AND length(input_hash) = 64",
            name="ck_execution_reconciliation_inputs_hashes",
        ),
        ForeignKeyConstraint(
            ["run_id", "organization_id"],
            [
                "execution_reconciliation_runs.run_id",
                "execution_reconciliation_runs.organization_id",
            ],
            name="fk_execution_reconciliation_inputs_run_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("run_id", "source_type", name="uq_execution_reconciliation_inputs_source"),
        Index(
            "ix_execution_reconciliation_inputs_watermark",
            "source_type",
            "observed_through",
        ),
    )

    input_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    collection_status: Mapped[str] = mapped_column(String(20), nullable=False)
    source_version: Mapped[str] = mapped_column(String(160), nullable=False)
    watermark_type: Mapped[str] = mapped_column(String(80), nullable=False)
    watermark_value: Mapped[str] = mapped_column(String(255), nullable=False)
    observed_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_through: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    item_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class ExecutionReconciliationFinding(Base):
    """Immutable difference discovered while comparing one reconciliation run."""

    __tablename__ = "execution_reconciliation_findings"
    __table_args__ = (
        CheckConstraint("finding_sequence > 0", name="ck_execution_reconciliation_findings_seq"),
        CheckConstraint(
            "category IN ('MISSING_FACT', 'UNEXPLAINED_ORDER', 'POSITION_MISMATCH', "
            "'BALANCE_MISMATCH', 'PROTECTION_GAP', 'HEAT_MISMATCH', 'WORKER_DRIFT', "
            "'STALE_WATERMARK', 'OTHER')",
            name="ck_execution_reconciliation_findings_category",
        ),
        CheckConstraint(
            "severity IN ('INFO', 'WARNING', 'BLOCKING', 'UNKNOWN')",
            name="ck_execution_reconciliation_findings_severity",
        ),
        CheckConstraint(
            "jsonb_typeof(expected_snapshot) = 'object' "
            "AND jsonb_typeof(observed_snapshot) = 'object' "
            "AND length(expected_hash) = 64 AND length(observed_hash) = 64 "
            "AND length(evidence_hash) = 64 AND length(finding_hash) = 64",
            name="ck_execution_reconciliation_findings_integrity",
        ),
        ForeignKeyConstraint(
            ["run_id", "organization_id"],
            [
                "execution_reconciliation_runs.run_id",
                "execution_reconciliation_runs.organization_id",
            ],
            name="fk_execution_reconciliation_findings_run_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "finding_id",
            "run_id",
            "organization_id",
            name="uq_execution_reconciliation_findings_resolution_binding",
        ),
        UniqueConstraint(
            "run_id",
            "finding_sequence",
            name="uq_execution_reconciliation_findings_sequence",
        ),
        Index(
            "ix_execution_reconciliation_findings_run_severity",
            "run_id",
            "severity",
        ),
    )

    finding_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    finding_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(80), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    expected_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expected_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    observed_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    finding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExecutionReconciliationFindingResolution(Base):
    """Immutable, evidence-backed closure for a finding; absence means unresolved."""

    __tablename__ = "execution_reconciliation_finding_resolutions"
    __table_args__ = (
        CheckConstraint(
            "disposition = 'RESOLVED_SAFE'",
            name="ck_execution_reconciliation_resolutions_disposition",
        ),
        CheckConstraint(
            "resolution_type IN ('VENUE_FACT_CONFIRMED', 'TRADING_PROJECTION_CORRECTED', "
            "'RISK_HELD', 'NO_EXTERNAL_EFFECT_PROVED')",
            name="ck_execution_reconciliation_resolutions_type",
        ),
        CheckConstraint(
            "length(corrective_action_hash) = 64 AND length(evidence_hash) = 64 "
            "AND length(resolution_hash) = 64",
            name="ck_execution_reconciliation_resolutions_hashes",
        ),
        ForeignKeyConstraint(
            ["finding_id", "run_id", "organization_id"],
            [
                "execution_reconciliation_findings.finding_id",
                "execution_reconciliation_findings.run_id",
                "execution_reconciliation_findings.organization_id",
            ],
            name="fk_execution_reconciliation_resolutions_finding",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("finding_id", name="uq_execution_reconciliation_resolutions_finding"),
    )

    resolution_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    finding_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    disposition: Mapped[str] = mapped_column(String(20), nullable=False)
    resolution_type: Mapped[str] = mapped_column(String(40), nullable=False)
    corrective_action_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    corrective_action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    resolved_by: Mapped[str] = mapped_column(String(160), nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolution_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class ExecutionReconciliationRunState(Base):
    """Current, monotonic state of one reconciliation run."""

    __tablename__ = "execution_reconciliation_run_states"
    __table_args__ = (
        CheckConstraint(
            "status IN ('RUNNING', 'UNKNOWN', 'SUCCEEDED', 'FAILED')",
            name="ck_execution_reconciliation_states_status",
        ),
        CheckConstraint(
            "phase IN ('COLLECTING', 'COMPARING', 'ADJUSTING')",
            name="ck_execution_reconciliation_states_phase",
        ),
        CheckConstraint("version >= 1", name="ck_execution_reconciliation_states_version"),
        CheckConstraint(
            "collected_source_count >= 0 AND finding_count >= 0 AND unresolved_blocking_count >= 0",
            name="ck_execution_reconciliation_states_counts",
        ),
        CheckConstraint(
            "(status = 'RUNNING' AND completed_at IS NULL AND result_snapshot IS NULL "
            "AND result_hash IS NULL) OR "
            "(status IN ('UNKNOWN', 'SUCCEEDED', 'FAILED') AND completed_at IS NOT NULL "
            "AND result_snapshot IS NOT NULL AND result_hash IS NOT NULL "
            "AND length(result_hash) = 64)",
            name="ck_execution_reconciliation_states_terminal_result",
        ),
        ForeignKeyConstraint(
            ["run_id", "organization_id"],
            [
                "execution_reconciliation_runs.run_id",
                "execution_reconciliation_runs.organization_id",
            ],
            name="fk_execution_reconciliation_states_run_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "run_id", "scope_id", name="uq_execution_reconciliation_states_scope_binding"
        ),
        Index(
            "uq_execution_reconciliation_states_active_scope",
            "scope_id",
            unique=True,
            postgresql_where=text("status = 'RUNNING'"),
        ),
    )

    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(96), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    phase: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    collected_source_count: Mapped[int] = mapped_column(Integer, nullable=False)
    finding_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unresolved_blocking_count: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    result_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    result_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExecutionReconciliationRunStateHistory(Base):
    __tablename__ = "execution_reconciliation_run_state_history"
    __table_args__ = (
        CheckConstraint("state_version >= 1", name="ck_execution_reconciliation_history_ver"),
        CheckConstraint(
            "jsonb_typeof(state_snapshot) = 'object' AND length(state_hash) = 64",
            name="ck_execution_reconciliation_history_integrity",
        ),
        ForeignKeyConstraint(
            ["run_id", "scope_id"],
            [
                "execution_reconciliation_run_states.run_id",
                "execution_reconciliation_run_states.scope_id",
            ],
            name="fk_execution_reconciliation_history_state",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint(
            "run_id", "state_version", name="uq_execution_reconciliation_history_version"
        ),
        Index(
            "ix_execution_reconciliation_history_time",
            "run_id",
            "changed_at",
        ),
    )

    history_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(96), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    phase: Mapped[str] = mapped_column(String(20), nullable=False)
    collected_source_count: Mapped[int] = mapped_column(Integer, nullable=False)
    finding_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unresolved_blocking_count: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    state_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
