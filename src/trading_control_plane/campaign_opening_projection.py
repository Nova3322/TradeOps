from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_control_plane.campaign_fill_economics import fill_entry_snapshot_from_record
from trading_control_plane.campaign_fill_economics_models import CampaignFillEconomicEntry
from trading_control_plane.commands import CommandRejected, hash_json
from trading_control_plane.metrics import CAMPAIGN_OPENING_FILL_PROJECTIONS

PROJECTION_VERSION = "campaign-opening-fill-projection-v1"
UNAVAILABLE_REASONS = (
    "CURRENT_POSITION_ECONOMICS_UNBOUND",
    "FUNDING_FACTS_UNAVAILABLE",
    "FX_VALUATION_UNAVAILABLE",
    "REDUCE_EXIT_LEDGER_UNAVAILABLE",
)


class NativeAmountTotal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    currency: str = Field(min_length=1, max_length=80)
    amount: Decimal = Field(max_digits=38, decimal_places=18)


class CampaignOpeningFillSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_fill_economic_entry_id: UUID
    entry_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    order_intent_id: UUID
    add_unit_id: UUID | None
    intent_kind: str = Field(pattern=r"^(INITIAL|ADD)$")
    quantity: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    notional: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    contract_multiplier: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    fee_amount: Decimal = Field(max_digits=38, decimal_places=18)
    fee_currency: str = Field(min_length=1, max_length=80)
    realized_pnl: Decimal | None = Field(default=None, max_digits=38, decimal_places=18)
    realized_pnl_status: str = Field(pattern=r"^(KNOWN|UNKNOWN)$")
    settlement_currency: str = Field(min_length=1, max_length=80)
    facts_event_time: datetime


class CampaignOpeningFillProjection(BaseModel):
    """Rebuildable opening-fill prefix; deliberately not Campaign equity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_id: UUID
    organization_id: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=80)
    execution_domain: str = Field(min_length=1, max_length=120)
    account_id: str = Field(min_length=1, max_length=160)
    instrument_id: str = Field(min_length=1, max_length=255)
    direction: str = Field(pattern=r"^(LONG|SHORT)$")
    margin_mode: str = Field(min_length=1, max_length=80)
    collateral_scope: str = Field(min_length=1, max_length=120)
    collateral_pool_id: str = Field(min_length=1, max_length=160)
    risk_currency: str = Field(min_length=1, max_length=80)
    contract_multiplier: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    fill_count: int = Field(gt=0)
    intent_count: int = Field(gt=0)
    initial_fill_count: int = Field(gt=0)
    add_fill_count: int = Field(ge=0)
    cumulative_quantity: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    cumulative_notional: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    native_fee_totals: tuple[NativeAmountTotal, ...]
    known_realized_pnl_totals: tuple[NativeAmountTotal, ...]
    realized_pnl_unknown_count: int = Field(ge=0)
    settlement_currencies: tuple[str, ...]
    source_entries: tuple[CampaignOpeningFillSource, ...]
    facts_as_of: datetime
    economic_equity_status: str = Field(pattern=r"^UNAVAILABLE$")
    unavailable_reasons: tuple[str, ...]
    projection_version: str = Field(pattern=r"^campaign-opening-fill-projection-v[0-9]+$")
    projection_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def projection_is_self_consistent(self) -> Self:
        if len(self.source_entries) != self.fill_count:
            raise ValueError("opening-fill projection source count is inconsistent")
        ordered = tuple(
            sorted(
                self.source_entries,
                key=lambda item: (
                    item.facts_event_time,
                    str(item.campaign_fill_economic_entry_id),
                ),
            )
        )
        if ordered != self.source_entries:
            raise ValueError("opening-fill projection sources are not canonical ordered")
        if len({item.order_intent_id for item in self.source_entries}) != self.intent_count:
            raise ValueError("opening-fill projection intent count is inconsistent")
        initial_count = sum(item.intent_kind == "INITIAL" for item in self.source_entries)
        add_count = sum(item.intent_kind == "ADD" for item in self.source_entries)
        if initial_count != self.initial_fill_count or add_count != self.add_fill_count:
            raise ValueError("opening-fill projection intent-kind counts are inconsistent")
        if any(
            item.contract_multiplier != self.contract_multiplier for item in self.source_entries
        ):
            raise ValueError("opening-fill projection contract multiplier changed")
        if sum((item.quantity for item in self.source_entries), start=Decimal("0")) != (
            self.cumulative_quantity
        ):
            raise ValueError("opening-fill projection quantity is inconsistent")
        if sum((item.notional for item in self.source_entries), start=Decimal("0")) != (
            self.cumulative_notional
        ):
            raise ValueError("opening-fill projection notional is inconsistent")
        fee_totals: defaultdict[str, Decimal] = defaultdict(Decimal)
        realized_totals: defaultdict[str, Decimal] = defaultdict(Decimal)
        unknown_count = 0
        for item in self.source_entries:
            fee_totals[item.fee_currency] += item.fee_amount
            if item.realized_pnl_status == "UNKNOWN":
                unknown_count += 1
            else:
                assert item.realized_pnl is not None
                realized_totals[item.settlement_currency] += item.realized_pnl
        expected_fees = tuple(
            NativeAmountTotal(currency=currency, amount=amount)
            for currency, amount in sorted(fee_totals.items())
        )
        expected_realized = tuple(
            NativeAmountTotal(currency=currency, amount=amount)
            for currency, amount in sorted(realized_totals.items())
        )
        if self.native_fee_totals != expected_fees:
            raise ValueError("opening-fill projection fee totals are inconsistent")
        if self.known_realized_pnl_totals != expected_realized:
            raise ValueError("opening-fill projection realized PnL totals are inconsistent")
        if self.realized_pnl_unknown_count != unknown_count:
            raise ValueError("opening-fill projection realized PnL completeness is inconsistent")
        expected_currencies = tuple(
            sorted({item.settlement_currency for item in self.source_entries})
        )
        if self.settlement_currencies != expected_currencies:
            raise ValueError("opening-fill projection settlement currencies are inconsistent")
        if self.facts_as_of != max(item.facts_event_time for item in self.source_entries):
            raise ValueError("opening-fill projection facts_as_of is inconsistent")
        if self.unavailable_reasons != UNAVAILABLE_REASONS:
            raise ValueError("opening-fill projection cannot claim Campaign equity readiness")
        material = self.model_dump(mode="json", exclude={"projection_hash"})
        if self.projection_hash != hash_json(material):
            raise ValueError("opening-fill projection hash mismatch")
        return self


class CampaignOpeningFillProjectionService:
    @staticmethod
    def resolve(session: Session, campaign_id: UUID) -> CampaignOpeningFillProjection:
        records = tuple(
            session.execute(
                select(CampaignFillEconomicEntry)
                .where(CampaignFillEconomicEntry.campaign_id == campaign_id)
                .order_by(
                    CampaignFillEconomicEntry.facts_event_time,
                    CampaignFillEconomicEntry.campaign_fill_economic_entry_id,
                )
            ).scalars()
        )
        if not records:
            CAMPAIGN_OPENING_FILL_PROJECTIONS.labels("UNAVAILABLE").inc()
            raise CommandRejected(
                "CAMPAIGN_OPENING_FILL_PROJECTION_UNAVAILABLE",
                "Campaign has no accepted canonical opening fills",
            )
        snapshots = tuple(fill_entry_snapshot_from_record(record) for record in records)
        first = snapshots[0]
        scope = (
            first.organization_id,
            first.venue,
            first.execution_domain,
            first.account_id,
            first.instrument_id,
            first.direction,
            first.margin_mode,
            first.collateral_scope,
            first.collateral_pool_id,
            first.risk_currency,
            first.contract_multiplier,
        )
        if any(
            (
                item.organization_id,
                item.venue,
                item.execution_domain,
                item.account_id,
                item.instrument_id,
                item.direction,
                item.margin_mode,
                item.collateral_scope,
                item.collateral_pool_id,
                item.risk_currency,
                item.contract_multiplier,
            )
            != scope
            for item in snapshots[1:]
        ):
            CAMPAIGN_OPENING_FILL_PROJECTIONS.labels("SCOPE_CONFLICT").inc()
            raise CommandRejected(
                "CAMPAIGN_OPENING_FILL_PROJECTION_SCOPE_CONFLICT",
                "Campaign opening fills do not share one exact economic scope",
            )
        sources = tuple(
            CampaignOpeningFillSource(
                campaign_fill_economic_entry_id=item.campaign_fill_economic_entry_id,
                entry_hash=item.entry_hash,
                order_intent_id=item.order_intent_id,
                add_unit_id=item.add_unit_id,
                intent_kind=item.intent_kind,
                quantity=item.quantity,
                notional=item.notional,
                contract_multiplier=item.contract_multiplier,
                fee_amount=item.fee_amount,
                fee_currency=item.fee_currency,
                realized_pnl=item.realized_pnl,
                realized_pnl_status=item.realized_pnl_status,
                settlement_currency=item.settlement_currency,
                facts_event_time=item.facts_event_time,
            )
            for item in snapshots
        )
        fee_totals: defaultdict[str, Decimal] = defaultdict(Decimal)
        realized_totals: defaultdict[str, Decimal] = defaultdict(Decimal)
        unknown_count = 0
        for item in sources:
            fee_totals[item.fee_currency] += item.fee_amount
            if item.realized_pnl_status == "UNKNOWN":
                unknown_count += 1
            else:
                assert item.realized_pnl is not None
                realized_totals[item.settlement_currency] += item.realized_pnl
        draft = CampaignOpeningFillProjection.model_construct(
            campaign_id=campaign_id,
            organization_id=first.organization_id,
            venue=first.venue,
            execution_domain=first.execution_domain,
            account_id=first.account_id,
            instrument_id=first.instrument_id,
            direction=first.direction,
            margin_mode=first.margin_mode,
            collateral_scope=first.collateral_scope,
            collateral_pool_id=first.collateral_pool_id,
            risk_currency=first.risk_currency,
            contract_multiplier=first.contract_multiplier,
            fill_count=len(sources),
            intent_count=len({item.order_intent_id for item in sources}),
            initial_fill_count=sum(item.intent_kind == "INITIAL" for item in sources),
            add_fill_count=sum(item.intent_kind == "ADD" for item in sources),
            cumulative_quantity=sum(
                (item.quantity for item in sources),
                start=Decimal("0"),
            ),
            cumulative_notional=sum(
                (item.notional for item in sources),
                start=Decimal("0"),
            ),
            native_fee_totals=tuple(
                NativeAmountTotal(currency=currency, amount=amount)
                for currency, amount in sorted(fee_totals.items())
            ),
            known_realized_pnl_totals=tuple(
                NativeAmountTotal(currency=currency, amount=amount)
                for currency, amount in sorted(realized_totals.items())
            ),
            realized_pnl_unknown_count=unknown_count,
            settlement_currencies=tuple(sorted({item.settlement_currency for item in sources})),
            source_entries=sources,
            facts_as_of=max(item.facts_event_time for item in sources),
            economic_equity_status="UNAVAILABLE",
            unavailable_reasons=UNAVAILABLE_REASONS,
            projection_version=PROJECTION_VERSION,
            projection_hash="0" * 64,
        )
        projection = CampaignOpeningFillProjection.model_validate(
            {
                **draft.model_dump(mode="python"),
                "projection_hash": hash_json(
                    draft.model_dump(mode="json", exclude={"projection_hash"})
                ),
            }
        )
        CAMPAIGN_OPENING_FILL_PROJECTIONS.labels("REBUILT").inc()
        return projection
