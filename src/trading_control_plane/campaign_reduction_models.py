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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading_control_plane.database import Base


class CampaignReductionPlanSnapshotRecord(Base):
    """Immutable, non-dispatchable preparation for one durable Campaign target."""

    __tablename__ = "campaign_reduction_plan_snapshots"
    __table_args__ = (
        CheckConstraint("target_version > 0", name="ck_campaign_reduction_plans_version"),
        CheckConstraint(
            "jsonb_typeof(plan_payload) = 'object'",
            name="ck_campaign_reduction_plans_payload",
        ),
        CheckConstraint(
            "record_version = 'campaign-reduction-plan-snapshot-v1'",
            name="ck_campaign_reduction_plans_record_version",
        ),
        CheckConstraint(
            "environment = 'SHADOW' AND dispatch_eligible = false",
            name="ck_campaign_reduction_plans_shadow_only",
        ),
        CheckConstraint(
            "length(target_semantic_hash) = 64 "
            "AND length(current_position_binding_hash) = 64 "
            "AND length(plan_hash) = 64 "
            "AND length(record_hash) = 64",
            name="ck_campaign_reduction_plans_hashes",
        ),
        UniqueConstraint(
            "campaign_target_position_fact_id",
            name="uq_campaign_reduction_plans_target_fact",
        ),
        UniqueConstraint(
            "campaign_id",
            "plan_idempotency_ref",
            name="uq_campaign_reduction_plans_idempotency",
        ),
        Index(
            "ix_campaign_reduction_plans_campaign_target",
            "campaign_id",
            "target_version",
        ),
    )

    campaign_reduction_plan_snapshot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True
    )
    campaign_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaigns.campaign_id", ondelete="RESTRICT"), nullable=False
    )
    organization_id: Mapped[str] = mapped_column(String(120), nullable=False)
    campaign_target_position_fact_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "campaign_target_position_facts.campaign_target_position_fact_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    target_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    current_position_binding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_idempotency_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    plan_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    record_version: Mapped[str] = mapped_column(String(80), nullable=False)
    record_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    dispatch_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def contract(self) -> dict[str, Any]:
        return {
            column.name: getattr(self, column.name)
            for column in CampaignReductionPlanSnapshotRecord.__table__.columns
        }
