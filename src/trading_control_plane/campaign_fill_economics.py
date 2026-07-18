from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_control_plane.campaign_fill_economics_models import CampaignFillEconomicEntry
from trading_control_plane.commands import CommandRejected, hash_json
from trading_control_plane.execution_models import ExecutionFact, OrderIntent
from trading_control_plane.metrics import CAMPAIGN_FILL_ECONOMIC_ENTRIES
from trading_control_plane.venue_fact_models import VenueFill

ENTRY_VERSION = "campaign-fill-economic-entry-v1"
ECONOMIC_EFFECT = "POSITION_INCREASE"
ZERO = Decimal("0")


class CampaignFillEconomicEntrySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_fill_economic_entry_id: UUID
    campaign_id: UUID
    order_intent_id: UUID
    execution_fact_id: UUID
    venue_fill_id: UUID
    add_unit_id: UUID | None
    organization_id: str = Field(min_length=1, max_length=120)
    intent_kind: str = Field(pattern=r"^(INITIAL|ADD)$")
    economic_effect: str = Field(pattern=r"^POSITION_INCREASE$")
    venue: str = Field(min_length=1, max_length=80)
    execution_domain: str = Field(min_length=1, max_length=120)
    account_id: str = Field(min_length=1, max_length=160)
    instrument_id: str = Field(min_length=1, max_length=255)
    direction: str = Field(pattern=r"^(LONG|SHORT)$")
    side: str = Field(pattern=r"^(BUY|SELL)$")
    position_side: str = Field(pattern=r"^(LONG|SHORT|BOTH)$")
    reduce_only: bool
    margin_mode: str = Field(min_length=1, max_length=80)
    collateral_scope: str = Field(min_length=1, max_length=120)
    collateral_pool_id: str = Field(min_length=1, max_length=160)
    risk_currency: str = Field(min_length=1, max_length=80)
    venue_order_id: str = Field(min_length=1, max_length=255)
    venue_trade_id: str = Field(min_length=1, max_length=255)
    quantity: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    price: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    contract_multiplier: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    notional: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    liquidity_role: str = Field(pattern=r"^(MAKER|TAKER|UNKNOWN)$")
    fee_amount: Decimal = Field(max_digits=38, decimal_places=18)
    fee_currency: str = Field(min_length=1, max_length=80)
    fee_effect: str = Field(pattern=r"^(CHARGE|REBATE|ZERO)$")
    realized_pnl: Decimal | None = Field(default=None, max_digits=38, decimal_places=18)
    realized_pnl_status: str = Field(pattern=r"^(KNOWN|UNKNOWN)$")
    settlement_currency: str = Field(min_length=1, max_length=80)
    fill_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_fact_evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    entry_version: str = Field(pattern=r"^campaign-fill-economic-entry-v[0-9]+$")
    entry_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment: str = Field(pattern=r"^SHADOW$")
    real_funds_eligible: bool
    facts_event_time: datetime
    recorded_at: datetime

    @model_validator(mode="after")
    def entry_is_self_consistent(self) -> Self:
        if self.real_funds_eligible or self.reduce_only:
            raise ValueError("Campaign fill economic entry cannot grant real-funds or reduce-only")
        if (self.intent_kind == "INITIAL") != (self.add_unit_id is None):
            raise ValueError("Campaign fill economic entry add-unit ownership is inconsistent")
        if self.notional != self.quantity * self.price * self.contract_multiplier:
            raise ValueError("Campaign fill economic entry notional is inconsistent")
        fee_valid = (
            (self.fee_effect == "CHARGE" and self.fee_amount > ZERO)
            or (self.fee_effect == "REBATE" and self.fee_amount < ZERO)
            or (self.fee_effect == "ZERO" and self.fee_amount == ZERO)
        )
        if not fee_valid:
            raise ValueError("Campaign fill economic entry fee semantics are inconsistent")
        if (self.realized_pnl_status == "KNOWN") != (self.realized_pnl is not None):
            raise ValueError("Campaign fill economic entry realized PnL status is inconsistent")
        if (
            self.facts_event_time.tzinfo is None
            or self.recorded_at.tzinfo is None
            or self.facts_event_time > self.recorded_at
        ):
            raise ValueError("Campaign fill economic entry time order is invalid")
        material = self.model_dump(mode="json", exclude={"entry_hash"})
        if self.entry_hash != hash_json(material):
            raise ValueError("Campaign fill economic entry hash mismatch")
        return self


def fill_entry_snapshot_from_record(
    record: CampaignFillEconomicEntry,
) -> CampaignFillEconomicEntrySnapshot:
    return CampaignFillEconomicEntrySnapshot.model_validate(
        {
            column.name: getattr(record, column.name)
            for column in CampaignFillEconomicEntry.__table__.columns
        }
    )


class CampaignFillEconomicEntryService:
    """Attributes one accepted canonical venue fill to its Campaign without valuation."""

    @staticmethod
    def validate_fill_source(
        intent: OrderIntent,
        fill: VenueFill,
        organization_id: str,
        expected_fill_hash: str,
    ) -> None:
        if intent.intent_kind not in {"INITIAL", "ADD"} or intent.reduce_only:
            CAMPAIGN_FILL_ECONOMIC_ENTRIES.labels("OWNERSHIP_REJECTED").inc()
            raise CommandRejected(
                "CAMPAIGN_FILL_ECONOMIC_ENTRY_OWNERSHIP_MISMATCH",
                "fill economic entry requires an INITIAL or ADD position increase",
            )
        if (
            fill.organization_id != organization_id
            or fill.venue != intent.venue
            or fill.execution_domain != intent.execution_domain
            or fill.account_id != intent.account_id
            or fill.instrument_id != intent.instrument_id
            or fill.side != intent.side
            or fill.position_side not in {"BOTH", intent.position_side}
            or fill.reduce_only
            or fill.fill_hash != expected_fill_hash
        ):
            CAMPAIGN_FILL_ECONOMIC_ENTRIES.labels("FACT_MISMATCH").inc()
            raise CommandRejected(
                "CAMPAIGN_FILL_ECONOMIC_ENTRY_FACT_MISMATCH",
                "canonical fill cannot be attributed to the Campaign intent",
            )

    @staticmethod
    def record_fill(
        session: Session,
        intent: OrderIntent,
        fact: ExecutionFact,
        fill: VenueFill,
        organization_id: str,
        now: datetime,
    ) -> CampaignFillEconomicEntry:
        existing = session.execute(
            select(CampaignFillEconomicEntry)
            .where(CampaignFillEconomicEntry.venue_fill_id == fill.venue_fill_id)
            .with_for_update()
        ).scalar_one_or_none()
        if existing is not None:
            if (
                existing.campaign_id != intent.campaign_id
                or existing.order_intent_id != intent.order_intent_id
                or existing.execution_fact_id != fact.execution_fact_id
                or existing.fill_hash != fill.fill_hash
            ):
                CAMPAIGN_FILL_ECONOMIC_ENTRIES.labels("CONFLICT").inc()
                raise CommandRejected(
                    "CAMPAIGN_FILL_ECONOMIC_ENTRY_CONFLICT",
                    "venue fill already has a different immutable Campaign attribution",
                )
            fill_entry_snapshot_from_record(existing)
            CAMPAIGN_FILL_ECONOMIC_ENTRIES.labels("REPLAYED").inc()
            return existing

        if (
            fact.order_intent_id != intent.order_intent_id
            or fact.fact_kind != "VENUE_FILL"
            or fact.venue_fill_id != fill.venue_fill_id
            or fact.venue_fact_hash != fill.fill_hash
        ):
            CAMPAIGN_FILL_ECONOMIC_ENTRIES.labels("OWNERSHIP_REJECTED").inc()
            raise CommandRejected(
                "CAMPAIGN_FILL_ECONOMIC_ENTRY_OWNERSHIP_MISMATCH",
                "fill economic entry requires the intent's accepted canonical fill fact",
            )
        CampaignFillEconomicEntryService.validate_fill_source(
            intent,
            fill,
            organization_id,
            fact.venue_fact_hash or "",
        )

        entry_id = uuid4()
        draft = CampaignFillEconomicEntrySnapshot.model_construct(
            campaign_fill_economic_entry_id=entry_id,
            campaign_id=intent.campaign_id,
            order_intent_id=intent.order_intent_id,
            execution_fact_id=fact.execution_fact_id,
            venue_fill_id=fill.venue_fill_id,
            add_unit_id=intent.add_unit_id,
            organization_id=fill.organization_id,
            intent_kind=intent.intent_kind,
            economic_effect=ECONOMIC_EFFECT,
            venue=fill.venue,
            execution_domain=fill.execution_domain,
            account_id=fill.account_id,
            instrument_id=fill.instrument_id,
            direction=intent.position_side,
            side=fill.side,
            position_side=fill.position_side,
            reduce_only=fill.reduce_only,
            margin_mode=intent.margin_mode,
            collateral_scope=intent.collateral_scope,
            collateral_pool_id=intent.collateral_pool_id,
            risk_currency=intent.risk_currency,
            venue_order_id=fill.venue_order_id,
            venue_trade_id=fill.venue_trade_id,
            quantity=fill.quantity,
            price=fill.price,
            contract_multiplier=fill.contract_multiplier,
            notional=fill.notional,
            liquidity_role=fill.liquidity_role,
            fee_amount=fill.fee_amount,
            fee_currency=fill.fee_currency,
            fee_effect=fill.fee_effect,
            realized_pnl=fill.realized_pnl,
            realized_pnl_status="KNOWN" if fill.realized_pnl is not None else "UNKNOWN",
            settlement_currency=fill.settlement_currency,
            fill_hash=fill.fill_hash,
            execution_fact_evidence_hash=fact.evidence_hash,
            entry_version=ENTRY_VERSION,
            entry_hash="0" * 64,
            environment="SHADOW",
            real_funds_eligible=False,
            facts_event_time=fill.event_time,
            recorded_at=now,
        )
        snapshot = CampaignFillEconomicEntrySnapshot.model_validate(
            {
                **draft.model_dump(mode="python"),
                "entry_hash": hash_json(draft.model_dump(mode="json", exclude={"entry_hash"})),
            }
        )
        record = CampaignFillEconomicEntry(**snapshot.model_dump(mode="python"))
        session.add(record)
        session.flush()
        CAMPAIGN_FILL_ECONOMIC_ENTRIES.labels("RECORDED").inc()
        return record
