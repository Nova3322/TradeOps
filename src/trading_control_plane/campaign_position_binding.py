from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_control_plane.campaign_economics import (
    baseline_snapshot_from_record,
)
from trading_control_plane.campaign_economics_models import CampaignEconomicBaseline
from trading_control_plane.campaign_opening_projection import (
    CampaignOpeningFillProjectionService,
)
from trading_control_plane.commands import CommandRejected, hash_json
from trading_control_plane.metrics import CAMPAIGN_CURRENT_POSITION_BINDINGS
from trading_control_plane.projections import (
    CurrentPositionScope,
    CurrentPositionState,
    ProjectionQueryContext,
    ProjectionState,
    VenueCurrentProjectionService,
)

BINDING_VERSION = "campaign-current-position-binding-v1"
UNAVAILABLE_REASONS = (
    "CONTROLLED_ORDER_OUTLET_UNCERTIFIED",
    "FUNDING_FACTS_UNAVAILABLE",
    "FX_VALUATION_UNAVAILABLE",
    "EXIT_COST_MODEL_UNAVAILABLE",
)


class CampaignCurrentPositionBinding(BaseModel):
    """Exact opening-only consistency binding; deliberately not Campaign equity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_id: UUID
    campaign_economic_baseline_id: UUID
    baseline_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_order_intent_id: UUID
    baseline_position_snapshot_id: UUID
    baseline_position_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    opening_projection_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_position_snapshot_id: UUID
    current_position_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
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
    risk_currency: str = Field(min_length=1, max_length=80)
    initial_quantity: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    opening_initial_quantity: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    opening_add_quantity: Decimal = Field(ge=0, max_digits=38, decimal_places=18)
    opening_cumulative_quantity: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    current_quantity: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    current_entry_price: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    current_mark_price: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    contract_multiplier: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    current_notional: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    current_unrealized_pnl: Decimal = Field(max_digits=38, decimal_places=18)
    current_initial_margin: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=38,
        decimal_places=18,
    )
    current_maintenance_margin: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=38,
        decimal_places=18,
    )
    quantity_consistency_status: str = Field(pattern=r"^EXACT$")
    exclusive_ownership_status: str = Field(pattern=r"^UNAVAILABLE$")
    economic_equity_status: str = Field(pattern=r"^UNAVAILABLE$")
    unavailable_reasons: tuple[str, ...]
    baseline_facts_event_time: datetime
    opening_facts_as_of: datetime
    current_facts_as_of: datetime
    max_age_ms: int = Field(gt=0)
    valid_until: datetime
    binding_version: str = Field(pattern=r"^campaign-current-position-binding-v[0-9]+$")
    binding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def binding_is_self_consistent(self) -> Self:
        if self.initial_quantity != self.opening_initial_quantity:
            raise ValueError("Campaign baseline and INITIAL fill quantity diverged")
        if (
            self.opening_initial_quantity + self.opening_add_quantity
            != self.opening_cumulative_quantity
            or self.opening_cumulative_quantity != self.current_quantity
        ):
            raise ValueError("Campaign opening prefix and current quantity diverged")
        if (
            self.current_quantity * self.current_mark_price * self.contract_multiplier
            != self.current_notional
        ):
            raise ValueError("Campaign current position notional is inconsistent")
        if self.current_facts_as_of < max(
            self.baseline_facts_event_time,
            self.opening_facts_as_of,
        ):
            raise ValueError("Campaign current position predates its opening sources")
        if self.valid_until != self.current_facts_as_of + timedelta(milliseconds=self.max_age_ms):
            raise ValueError("Campaign current position validity is inconsistent")
        if self.unavailable_reasons != UNAVAILABLE_REASONS:
            raise ValueError("Campaign position binding cannot claim economic readiness")
        material = self.model_dump(mode="json", exclude={"binding_hash"})
        if self.binding_hash != hash_json(material):
            raise ValueError("Campaign current position binding hash mismatch")
        return self


class CampaignCurrentPositionBindingService:
    @staticmethod
    def resolve(
        session: Session,
        campaign_id: UUID,
        context: ProjectionQueryContext,
    ) -> CampaignCurrentPositionBinding:
        baseline_record = session.execute(
            select(CampaignEconomicBaseline).where(
                CampaignEconomicBaseline.campaign_id == campaign_id
            )
        ).scalar_one_or_none()
        if baseline_record is None:
            CAMPAIGN_CURRENT_POSITION_BINDINGS.labels("BASELINE_UNAVAILABLE").inc()
            raise CommandRejected(
                "CAMPAIGN_CURRENT_POSITION_BASELINE_UNAVAILABLE",
                "Campaign has no immutable INITIAL economic baseline",
            )
        baseline = baseline_snapshot_from_record(baseline_record)
        opening = CampaignOpeningFillProjectionService.resolve(session, campaign_id)
        initial_sources = tuple(
            source for source in opening.source_entries if source.intent_kind == "INITIAL"
        )
        initial_intent_ids = {source.order_intent_id for source in initial_sources}
        opening_initial_quantity = sum(
            (source.quantity for source in initial_sources),
            start=Decimal("0"),
        )
        opening_add_quantity = sum(
            (source.quantity for source in opening.source_entries if source.intent_kind == "ADD"),
            start=Decimal("0"),
        )
        shared_scope_matches = (
            baseline.campaign_id == opening.campaign_id == campaign_id
            and baseline.organization_id == opening.organization_id
            and baseline.venue == opening.venue
            and baseline.execution_domain == opening.execution_domain
            and baseline.account_id == opening.account_id
            and baseline.instrument_id == opening.instrument_id
            and baseline.direction == opening.direction
            and baseline.margin_mode == opening.margin_mode
            and baseline.collateral_scope == opening.collateral_scope
            and baseline.collateral_pool_id == opening.collateral_pool_id
            and baseline.contract_multiplier == opening.contract_multiplier
            and opening.risk_currency == baseline.settlement_currency
            and initial_intent_ids == {baseline.initial_order_intent_id}
            and opening_initial_quantity == baseline.initial_quantity
            and all(
                source.facts_event_time <= baseline.facts_event_time for source in initial_sources
            )
        )
        if not shared_scope_matches:
            CAMPAIGN_CURRENT_POSITION_BINDINGS.labels("SOURCE_CONFLICT").inc()
            raise CommandRejected(
                "CAMPAIGN_CURRENT_POSITION_SOURCE_CONFLICT",
                "Campaign baseline and opening fills do not share one exact source scope",
            )

        scope = CurrentPositionScope(
            organization_id=baseline.organization_id,
            venue=baseline.venue,
            execution_domain=baseline.execution_domain,
            account_id=baseline.account_id,
            instrument_id=baseline.instrument_id,
            position_mode=baseline.position_mode,
            position_side=baseline.position_side,
            margin_mode=baseline.margin_mode,
            collateral_pool_id=baseline.collateral_pool_id,
            settlement_currency=baseline.settlement_currency,
        )
        current = VenueCurrentProjectionService.current_position(session, scope, context)
        if current.projection_state is not ProjectionState.CONFIRMED:
            CAMPAIGN_CURRENT_POSITION_BINDINGS.labels("POSITION_UNAVAILABLE").inc()
            raise CommandRejected(
                "CAMPAIGN_CURRENT_POSITION_UNAVAILABLE",
                f"canonical current position unavailable: {current.reason_code}",
            )
        if (
            current.position_state is not CurrentPositionState.OPEN
            or current.direction.value != baseline.direction
            or current.quantity != opening.cumulative_quantity
            or current.contract_multiplier != baseline.contract_multiplier
            or current.facts_as_of is None
            or current.facts_as_of < max(baseline.facts_event_time, opening.facts_as_of)
        ):
            CAMPAIGN_CURRENT_POSITION_BINDINGS.labels("PREFIX_MISMATCH").inc()
            raise CommandRejected(
                "CAMPAIGN_CURRENT_POSITION_PREFIX_MISMATCH",
                "canonical current position does not equal the accepted opening-fill prefix",
            )
        required = (
            current.source_snapshot_id,
            current.source_snapshot_hash,
            current.quantity,
            current.entry_price,
            current.mark_price,
            current.contract_multiplier,
            current.notional,
            current.unrealized_pnl,
        )
        if any(value is None for value in required):  # pragma: no cover - DB source enforces
            raise RuntimeError("confirmed open position lacks required economics")
        assert current.source_snapshot_id is not None
        assert current.source_snapshot_hash is not None
        assert current.quantity is not None
        assert current.entry_price is not None
        assert current.mark_price is not None
        assert current.contract_multiplier is not None
        assert current.notional is not None
        assert current.unrealized_pnl is not None
        assert current.facts_as_of is not None
        draft = CampaignCurrentPositionBinding.model_construct(
            campaign_id=campaign_id,
            campaign_economic_baseline_id=baseline.campaign_economic_baseline_id,
            baseline_hash=baseline.baseline_hash,
            initial_order_intent_id=baseline.initial_order_intent_id,
            baseline_position_snapshot_id=baseline.position_snapshot_id,
            baseline_position_snapshot_hash=baseline.position_snapshot_hash,
            opening_projection_hash=opening.projection_hash,
            current_position_snapshot_id=current.source_snapshot_id,
            current_position_snapshot_hash=current.source_snapshot_hash,
            organization_id=baseline.organization_id,
            venue=baseline.venue,
            execution_domain=baseline.execution_domain,
            account_id=baseline.account_id,
            instrument_id=baseline.instrument_id,
            direction=baseline.direction,
            position_mode=baseline.position_mode,
            position_side=baseline.position_side,
            margin_mode=baseline.margin_mode,
            collateral_scope=baseline.collateral_scope,
            collateral_pool_id=baseline.collateral_pool_id,
            settlement_currency=baseline.settlement_currency,
            risk_currency=opening.risk_currency,
            initial_quantity=baseline.initial_quantity,
            opening_initial_quantity=opening_initial_quantity,
            opening_add_quantity=opening_add_quantity,
            opening_cumulative_quantity=opening.cumulative_quantity,
            current_quantity=current.quantity,
            current_entry_price=current.entry_price,
            current_mark_price=current.mark_price,
            contract_multiplier=current.contract_multiplier,
            current_notional=current.notional,
            current_unrealized_pnl=current.unrealized_pnl,
            current_initial_margin=current.initial_margin,
            current_maintenance_margin=current.maintenance_margin,
            quantity_consistency_status="EXACT",
            exclusive_ownership_status="UNAVAILABLE",
            economic_equity_status="UNAVAILABLE",
            unavailable_reasons=UNAVAILABLE_REASONS,
            baseline_facts_event_time=baseline.facts_event_time,
            opening_facts_as_of=opening.facts_as_of,
            current_facts_as_of=current.facts_as_of,
            max_age_ms=context.max_age_ms,
            valid_until=current.facts_as_of + timedelta(milliseconds=context.max_age_ms),
            binding_version=BINDING_VERSION,
            binding_hash="0" * 64,
        )
        binding = CampaignCurrentPositionBinding.model_validate(
            {
                **draft.model_dump(mode="python"),
                "binding_hash": hash_json(draft.model_dump(mode="json", exclude={"binding_hash"})),
            }
        )
        CAMPAIGN_CURRENT_POSITION_BINDINGS.labels("BOUND").inc()
        return binding
