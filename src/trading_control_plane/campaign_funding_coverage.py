from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_control_plane.campaign_opening_projection import (
    CampaignOpeningFillProjectionService,
    NativeAmountTotal,
)
from trading_control_plane.campaign_position_binding import (
    CampaignCurrentPositionBindingService,
)
from trading_control_plane.commands import CommandRejected, hash_json
from trading_control_plane.metrics import CAMPAIGN_FUNDING_COVERAGE_PROJECTIONS
from trading_control_plane.projections import ProjectionQueryContext
from trading_control_plane.reconciliation_models import (
    ExecutionReconciliationInput,
    ExecutionReconciliationRun,
    ExecutionReconciliationRunState,
)
from trading_control_plane.sender_fencing_models import (
    ExecutionSenderScope,
    ShadowDispatchClaim,
)
from trading_control_plane.venue_fact_models import (
    VenueFactInputLink,
    VenueFundingPayment,
)

PROJECTION_VERSION = "campaign-funding-coverage-projection-v1"
UNAVAILABLE_REASONS = (
    "CONTROLLED_ORDER_OUTLET_UNCERTIFIED",
    "FUNDING_PAYMENT_CAMPAIGN_OWNERSHIP_UNCERTIFIED",
    "FX_VALUATION_UNAVAILABLE",
    "EXIT_COST_MODEL_UNAVAILABLE",
)


class CampaignFundingCandidateSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    venue_fact_input_link_id: UUID
    venue_funding_payment_id: UUID
    funding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    venue_payment_id: str = Field(min_length=1, max_length=255)
    position_side: str = Field(pattern=r"^(LONG|SHORT|BOTH)$")
    funding_amount: Decimal = Field(max_digits=38, decimal_places=18)
    funding_currency: str = Field(min_length=1, max_length=80)
    funding_effect: str = Field(pattern=r"^(PAYMENT|RECEIPT|ZERO)$")
    event_time: datetime

    @model_validator(mode="after")
    def effect_matches_signed_cost(self) -> Self:
        valid = (
            (self.funding_effect == "PAYMENT" and self.funding_amount > 0)
            or (self.funding_effect == "RECEIPT" and self.funding_amount < 0)
            or (self.funding_effect == "ZERO" and self.funding_amount == 0)
        )
        if not valid:
            raise ValueError("Campaign funding candidate effect/sign mismatch")
        return self


class CampaignFundingCoverageProjection(BaseModel):
    """Reconciled scope/time candidates; deliberately not Campaign attribution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_id: UUID
    current_position_binding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    sender_scope_id: str = Field(min_length=1, max_length=96)
    sender_scope_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reconciliation_run_id: UUID
    reconciliation_run_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    funding_input_id: UUID
    funding_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    funding_watermark_type: str = Field(min_length=1, max_length=80)
    funding_watermark_value: str = Field(min_length=1, max_length=255)
    organization_id: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=80)
    execution_domain: str = Field(min_length=1, max_length=120)
    account_id: str = Field(min_length=1, max_length=160)
    instrument_id: str = Field(min_length=1, max_length=255)
    position_side: str = Field(pattern=r"^(LONG|SHORT|BOTH)$")
    margin_mode: str = Field(pattern=r"^ISOLATED$")
    collateral_pool_id: str = Field(min_length=1, max_length=160)
    interval_start: datetime
    interval_end: datetime
    input_observed_from: datetime
    input_observed_through: datetime
    input_item_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    native_signed_cost_totals: tuple[NativeAmountTotal, ...]
    source_payments: tuple[CampaignFundingCandidateSource, ...]
    scope_interval_coverage_status: str = Field(pattern=r"^EXACT$")
    campaign_attribution_status: str = Field(pattern=r"^UNAVAILABLE$")
    economic_equity_status: str = Field(pattern=r"^UNAVAILABLE$")
    unavailable_reasons: tuple[str, ...]
    reconciliation_completed_at: datetime
    projection_version: str = Field(pattern=r"^campaign-funding-coverage-projection-v[0-9]+$")
    projection_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def projection_is_self_consistent(self) -> Self:
        if self.interval_start > self.interval_end:
            raise ValueError("Campaign funding coverage interval is reversed")
        if (
            self.input_observed_from > self.interval_start
            or self.input_observed_through < self.interval_end
        ):
            raise ValueError("Campaign funding input does not cover the Campaign interval")
        if len(self.source_payments) != self.candidate_count:
            raise ValueError("Campaign funding candidate count is inconsistent")
        if self.candidate_count > self.input_item_count:
            raise ValueError("Campaign funding candidates exceed the complete input")
        ordered = tuple(
            sorted(
                self.source_payments,
                key=lambda item: (item.event_time, str(item.venue_funding_payment_id)),
            )
        )
        if ordered != self.source_payments:
            raise ValueError("Campaign funding candidates are not canonical ordered")
        if any(
            source.position_side != self.position_side
            or source.event_time < self.interval_start
            or source.event_time > self.interval_end
            for source in self.source_payments
        ):
            raise ValueError("Campaign funding candidate is outside the exact scope/interval")
        totals: defaultdict[str, Decimal] = defaultdict(Decimal)
        for source in self.source_payments:
            totals[source.funding_currency] += source.funding_amount
        expected_totals = tuple(
            NativeAmountTotal(currency=currency, amount=amount)
            for currency, amount in sorted(totals.items())
        )
        if self.native_signed_cost_totals != expected_totals:
            raise ValueError("Campaign funding candidate totals are inconsistent")
        if self.unavailable_reasons != UNAVAILABLE_REASONS:
            raise ValueError("Campaign funding coverage cannot claim attribution readiness")
        material = self.model_dump(mode="json", exclude={"projection_hash"})
        if self.projection_hash != hash_json(material):
            raise ValueError("Campaign funding coverage projection hash mismatch")
        return self


class CampaignFundingCoverageProjectionService:
    @staticmethod
    def resolve(
        session: Session,
        campaign_id: UUID,
        context: ProjectionQueryContext,
    ) -> CampaignFundingCoverageProjection:
        position = CampaignCurrentPositionBindingService.resolve(session, campaign_id, context)
        opening = CampaignOpeningFillProjectionService.resolve(session, campaign_id)
        interval_start = min(source.facts_event_time for source in opening.source_entries)
        interval_end = position.current_facts_as_of

        claim = session.execute(
            select(ShadowDispatchClaim).where(
                ShadowDispatchClaim.order_intent_id == position.initial_order_intent_id
            )
        ).scalar_one_or_none()
        scope = session.get(ExecutionSenderScope, claim.scope_id) if claim is not None else None
        if claim is None or scope is None:
            CAMPAIGN_FUNDING_COVERAGE_PROJECTIONS.labels("SENDER_SCOPE_UNAVAILABLE").inc()
            raise CommandRejected(
                "CAMPAIGN_FUNDING_SENDER_SCOPE_UNAVAILABLE",
                "Campaign INITIAL intent has no exact sender-scope claim",
            )
        if (
            scope.organization_id != position.organization_id
            or scope.venue != position.venue
            or scope.execution_domain != position.execution_domain
            or scope.account_id != position.account_id
            or scope.position_mode != position.position_mode
            or scope.margin_mode != position.margin_mode
            or scope.collateral_scope != position.collateral_scope
            or scope.collateral_pool_id != position.collateral_pool_id
            or scope.environment != "SHADOW"
            or scope.live_dispatch_eligible
        ):
            CAMPAIGN_FUNDING_COVERAGE_PROJECTIONS.labels("SOURCE_CONFLICT").inc()
            raise CommandRejected(
                "CAMPAIGN_FUNDING_COVERAGE_SOURCE_CONFLICT",
                "Campaign position and sender scope do not share one exact route",
            )

        run = session.execute(
            select(ExecutionReconciliationRun)
            .where(ExecutionReconciliationRun.scope_id == scope.scope_id)
            .order_by(
                ExecutionReconciliationRun.started_at.desc(),
                ExecutionReconciliationRun.run_id.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()
        state = session.get(ExecutionReconciliationRunState, run.run_id) if run else None
        funding_input = (
            session.execute(
                select(ExecutionReconciliationInput).where(
                    ExecutionReconciliationInput.run_id == run.run_id,
                    ExecutionReconciliationInput.source_type == "VENUE_FUNDING",
                )
            ).scalar_one_or_none()
            if run is not None
            else None
        )
        if (
            run is None
            or state is None
            or funding_input is None
            or run.schema_version != 2
            or run.organization_id != position.organization_id
            or run.environment != "SHADOW"
            or run.live_dispatch_eligible
            or state.status != "SUCCEEDED"
            or state.completed_at is None
            or funding_input.collection_status != "COMPLETE"
            or funding_input.observed_from > interval_start
            or funding_input.observed_through < interval_end
        ):
            CAMPAIGN_FUNDING_COVERAGE_PROJECTIONS.labels("COVERAGE_UNAVAILABLE").inc()
            raise CommandRejected(
                "CAMPAIGN_FUNDING_COVERAGE_UNAVAILABLE",
                "latest exact sender-scope reconciliation lacks complete Campaign funding coverage",
            )

        links = tuple(
            session.execute(
                select(VenueFactInputLink)
                .where(
                    VenueFactInputLink.reconciliation_input_id == funding_input.input_id,
                    VenueFactInputLink.source_type == "VENUE_FUNDING",
                )
                .order_by(
                    VenueFactInputLink.linked_at,
                    VenueFactInputLink.venue_fact_input_link_id,
                )
            ).scalars()
        )
        if len(links) != funding_input.item_count or any(
            link.run_id != run.run_id
            or link.organization_id != position.organization_id
            or link.input_hash != funding_input.input_hash
            or link.venue_funding_payment_id is None
            for link in links
        ):
            CAMPAIGN_FUNDING_COVERAGE_PROJECTIONS.labels("SOURCE_CONFLICT").inc()
            raise CommandRejected(
                "CAMPAIGN_FUNDING_COVERAGE_SOURCE_CONFLICT",
                "funding input membership does not equal its frozen item count",
            )

        candidates: list[CampaignFundingCandidateSource] = []
        for link in links:
            assert link.venue_funding_payment_id is not None
            payment = session.get(VenueFundingPayment, link.venue_funding_payment_id)
            if payment is None or payment.funding_hash != link.fact_hash:
                CAMPAIGN_FUNDING_COVERAGE_PROJECTIONS.labels("SOURCE_CONFLICT").inc()
                raise CommandRejected(
                    "CAMPAIGN_FUNDING_COVERAGE_SOURCE_CONFLICT",
                    "funding input membership cannot resolve its immutable payment",
                )
            exact_candidate = (
                payment.organization_id == position.organization_id
                and payment.venue == position.venue
                and payment.execution_domain == position.execution_domain
                and payment.account_id == position.account_id
                and payment.instrument_id == position.instrument_id
                and payment.position_side == position.position_side
                and payment.margin_mode == position.margin_mode
                and payment.collateral_pool_id == position.collateral_pool_id
                and interval_start <= payment.event_time <= interval_end
            )
            if exact_candidate:
                candidates.append(
                    CampaignFundingCandidateSource(
                        venue_fact_input_link_id=link.venue_fact_input_link_id,
                        venue_funding_payment_id=payment.venue_funding_payment_id,
                        funding_hash=payment.funding_hash,
                        venue_payment_id=payment.venue_payment_id,
                        position_side=payment.position_side,
                        funding_amount=payment.funding_amount,
                        funding_currency=payment.funding_currency,
                        funding_effect=payment.funding_effect,
                        event_time=payment.event_time,
                    )
                )
        ordered_candidates = tuple(
            sorted(
                candidates,
                key=lambda item: (item.event_time, str(item.venue_funding_payment_id)),
            )
        )
        totals: defaultdict[str, Decimal] = defaultdict(Decimal)
        for source in ordered_candidates:
            totals[source.funding_currency] += source.funding_amount
        native_totals = tuple(
            NativeAmountTotal(currency=currency, amount=amount)
            for currency, amount in sorted(totals.items())
        )
        draft = CampaignFundingCoverageProjection.model_construct(
            campaign_id=campaign_id,
            current_position_binding_hash=position.binding_hash,
            sender_scope_id=scope.scope_id,
            sender_scope_hash=scope.scope_hash,
            reconciliation_run_id=run.run_id,
            reconciliation_run_hash=run.run_hash,
            funding_input_id=funding_input.input_id,
            funding_input_hash=funding_input.input_hash,
            funding_watermark_type=funding_input.watermark_type,
            funding_watermark_value=funding_input.watermark_value,
            organization_id=position.organization_id,
            venue=position.venue,
            execution_domain=position.execution_domain,
            account_id=position.account_id,
            instrument_id=position.instrument_id,
            position_side=position.position_side,
            margin_mode=position.margin_mode,
            collateral_pool_id=position.collateral_pool_id,
            interval_start=interval_start,
            interval_end=interval_end,
            input_observed_from=funding_input.observed_from,
            input_observed_through=funding_input.observed_through,
            input_item_count=funding_input.item_count,
            candidate_count=len(ordered_candidates),
            native_signed_cost_totals=native_totals,
            source_payments=ordered_candidates,
            scope_interval_coverage_status="EXACT",
            campaign_attribution_status="UNAVAILABLE",
            economic_equity_status="UNAVAILABLE",
            unavailable_reasons=UNAVAILABLE_REASONS,
            reconciliation_completed_at=state.completed_at,
            projection_version=PROJECTION_VERSION,
            projection_hash="0" * 64,
        )
        projection = CampaignFundingCoverageProjection.model_validate(
            {
                **draft.model_dump(mode="python"),
                "projection_hash": hash_json(
                    draft.model_dump(mode="json", exclude={"projection_hash"})
                ),
            }
        )
        CAMPAIGN_FUNDING_COVERAGE_PROJECTIONS.labels("PROJECTED").inc()
        return projection
