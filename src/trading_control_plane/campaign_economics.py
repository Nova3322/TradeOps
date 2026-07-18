from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_control_plane.campaign_economics_models import CampaignEconomicBaseline
from trading_control_plane.commands import CommandRejected, hash_json
from trading_control_plane.execution_models import ExecutionFact, OrderIntent
from trading_control_plane.metrics import CAMPAIGN_ECONOMIC_BASELINES
from trading_control_plane.venue_fact_models import VenuePositionSnapshot

BASELINE_VERSION = "campaign-economic-baseline-v1"
MARGIN_REFERENCE_SOURCE = "VENUE_POSITION_INITIAL_MARGIN"
ZERO = Decimal("0")


class CampaignEconomicBaselineSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_economic_baseline_id: UUID
    campaign_id: UUID
    initial_order_intent_id: UUID
    initial_execution_fact_id: UUID
    position_snapshot_id: UUID
    organization_id: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=80)
    execution_domain: str = Field(min_length=1, max_length=120)
    account_id: str = Field(min_length=1, max_length=160)
    instrument_id: str = Field(min_length=1, max_length=255)
    direction: str = Field(pattern=r"^(LONG|SHORT)$")
    position_mode: str = Field(min_length=1, max_length=80)
    position_side: str = Field(min_length=1, max_length=20)
    margin_mode: str = Field(pattern=r"^ISOLATED$")
    collateral_scope: str = Field(min_length=1, max_length=120)
    collateral_pool_id: str = Field(min_length=1, max_length=160)
    settlement_currency: str = Field(min_length=1, max_length=80)
    initial_quantity: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    initial_entry_price: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    initial_mark_price: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    contract_multiplier: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    initial_notional: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    frozen_initial_margin_reference: Decimal = Field(
        gt=0,
        max_digits=38,
        decimal_places=18,
    )
    margin_reference_source: str = Field(pattern=r"^VENUE_POSITION_INITIAL_MARGIN$")
    position_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_fact_evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_version: str = Field(pattern=r"^campaign-economic-baseline-v[0-9]+$")
    baseline_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment: str = Field(pattern=r"^SHADOW$")
    real_funds_eligible: bool
    facts_event_time: datetime
    recorded_at: datetime

    @model_validator(mode="after")
    def baseline_is_self_consistent(self) -> Self:
        if self.real_funds_eligible:
            raise ValueError("campaign economic baseline cannot be real-funds eligible")
        if (
            self.facts_event_time.tzinfo is None
            or self.recorded_at.tzinfo is None
            or self.facts_event_time > self.recorded_at
        ):
            raise ValueError("campaign economic baseline time order is invalid")
        if (
            self.initial_quantity * self.initial_mark_price * self.contract_multiplier
            != self.initial_notional
        ):
            raise ValueError("campaign economic baseline position economics are inconsistent")
        material = self.model_dump(mode="json", exclude={"baseline_hash"})
        if self.baseline_hash != hash_json(material):
            raise ValueError("campaign economic baseline hash mismatch")
        return self


def baseline_snapshot_from_record(
    record: CampaignEconomicBaseline,
) -> CampaignEconomicBaselineSnapshot:
    return CampaignEconomicBaselineSnapshot.model_validate(
        {
            column.name: getattr(record, column.name)
            for column in CampaignEconomicBaseline.__table__.columns
        }
    )


class CampaignEconomicBaselineService:
    """Freezes the isolated INITIAL margin denominator from an exact position fact."""

    @staticmethod
    def validate_initial_margin_source(
        intent: OrderIntent,
        position: VenuePositionSnapshot,
        organization_id: str,
        expected_quantity: Decimal,
        expected_snapshot_hash: str,
    ) -> None:
        if intent.intent_kind != "INITIAL":
            CAMPAIGN_ECONOMIC_BASELINES.labels("OWNERSHIP_REJECTED").inc()
            raise CommandRejected(
                "CAMPAIGN_ECONOMIC_BASELINE_OWNERSHIP_MISMATCH",
                "baseline requires an INITIAL intent",
            )
        if (
            position.organization_id != organization_id
            or position.venue != intent.venue
            or position.execution_domain != intent.execution_domain
            or position.account_id != intent.account_id
            or position.instrument_id != intent.instrument_id
            or position.direction != intent.position_side
            or position.position_side not in {"BOTH", intent.position_side}
            or position.margin_mode != intent.margin_mode
            or position.collateral_pool_id != intent.collateral_pool_id
            or position.settlement_currency != intent.risk_currency
            or position.position_state != "OPEN"
            or position.quantity != expected_quantity
            or position.snapshot_hash != expected_snapshot_hash
        ):
            CAMPAIGN_ECONOMIC_BASELINES.labels("FACT_MISMATCH").inc()
            raise CommandRejected(
                "CAMPAIGN_ECONOMIC_BASELINE_FACT_MISMATCH",
                "canonical position cannot establish the Campaign economic baseline",
            )
        if position.margin_mode != "ISOLATED":
            CAMPAIGN_ECONOMIC_BASELINES.labels("MARGIN_MODE_UNSUPPORTED").inc()
            raise CommandRejected(
                "CAMPAIGN_MARGIN_BASELINE_UNSUPPORTED",
                "cross-margin Campaign attribution is not yet certified",
            )
        if position.initial_margin is None or position.initial_margin <= ZERO:
            CAMPAIGN_ECONOMIC_BASELINES.labels("MARGIN_UNAVAILABLE").inc()
            raise CommandRejected(
                "CAMPAIGN_INITIAL_MARGIN_REFERENCE_UNAVAILABLE",
                "canonical INITIAL position lacks a positive initial-margin reference",
            )

    @staticmethod
    def freeze_initial_margin(
        session: Session,
        intent: OrderIntent,
        fact: ExecutionFact,
        position: VenuePositionSnapshot,
        organization_id: str,
        now: datetime,
    ) -> CampaignEconomicBaseline:
        existing = session.execute(
            select(CampaignEconomicBaseline)
            .where(CampaignEconomicBaseline.campaign_id == intent.campaign_id)
            .with_for_update()
        ).scalar_one_or_none()
        if existing is not None:
            if (
                existing.initial_order_intent_id != intent.order_intent_id
                or existing.initial_execution_fact_id != fact.execution_fact_id
                or existing.position_snapshot_id != position.venue_position_snapshot_id
                or existing.position_snapshot_hash != position.snapshot_hash
            ):
                CAMPAIGN_ECONOMIC_BASELINES.labels("CONFLICT").inc()
                raise CommandRejected(
                    "CAMPAIGN_ECONOMIC_BASELINE_CONFLICT",
                    "Campaign already has a different immutable economic baseline",
                )
            baseline_snapshot_from_record(existing)
            CAMPAIGN_ECONOMIC_BASELINES.labels("REPLAYED").inc()
            return existing

        if (
            fact.order_intent_id != intent.order_intent_id
            or fact.fact_kind != "VENUE_POSITION"
            or fact.target_status != "POSITION_RECONCILED"
            or fact.venue_position_snapshot_id != position.venue_position_snapshot_id
        ):
            CAMPAIGN_ECONOMIC_BASELINES.labels("OWNERSHIP_REJECTED").inc()
            raise CommandRejected(
                "CAMPAIGN_ECONOMIC_BASELINE_OWNERSHIP_MISMATCH",
                "baseline requires the INITIAL intent's canonical position-reconciled fact",
            )
        CampaignEconomicBaselineService.validate_initial_margin_source(
            intent,
            position,
            organization_id,
            fact.cumulative_filled_quantity,
            fact.venue_fact_hash or "",
        )
        required_economics = (
            position.quantity,
            position.entry_price,
            position.mark_price,
            position.notional,
        )
        if any(value is None for value in required_economics):  # pragma: no cover - DB enforces
            raise RuntimeError("open canonical position lacks baseline economics")

        baseline_id = uuid4()
        draft = CampaignEconomicBaselineSnapshot.model_construct(
            campaign_economic_baseline_id=baseline_id,
            campaign_id=intent.campaign_id,
            initial_order_intent_id=intent.order_intent_id,
            initial_execution_fact_id=fact.execution_fact_id,
            position_snapshot_id=position.venue_position_snapshot_id,
            organization_id=position.organization_id,
            venue=position.venue,
            execution_domain=position.execution_domain,
            account_id=position.account_id,
            instrument_id=position.instrument_id,
            direction=position.direction,
            position_mode=position.position_mode,
            position_side=position.position_side,
            margin_mode=position.margin_mode,
            collateral_scope=intent.collateral_scope,
            collateral_pool_id=position.collateral_pool_id,
            settlement_currency=position.settlement_currency,
            initial_quantity=position.quantity,
            initial_entry_price=position.entry_price,
            initial_mark_price=position.mark_price,
            contract_multiplier=position.contract_multiplier,
            initial_notional=position.notional,
            frozen_initial_margin_reference=position.initial_margin,
            margin_reference_source=MARGIN_REFERENCE_SOURCE,
            position_snapshot_hash=position.snapshot_hash,
            execution_fact_evidence_hash=fact.evidence_hash,
            baseline_version=BASELINE_VERSION,
            baseline_hash="0" * 64,
            environment="SHADOW",
            real_funds_eligible=False,
            facts_event_time=position.event_time,
            recorded_at=now,
        )
        snapshot = CampaignEconomicBaselineSnapshot.model_validate(
            {
                **draft.model_dump(mode="python"),
                "baseline_hash": hash_json(
                    draft.model_dump(mode="json", exclude={"baseline_hash"})
                ),
            }
        )
        record = CampaignEconomicBaseline(**snapshot.model_dump(mode="python"))
        session.add(record)
        session.flush()
        CAMPAIGN_ECONOMIC_BASELINES.labels("FROZEN").inc()
        return record
