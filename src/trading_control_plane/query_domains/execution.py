from __future__ import annotations

from trading_control_plane.query_component import QueryComponent

# ruff: noqa: F403, F405
from trading_control_plane.query_core import *


class ExecutionQueries(QueryComponent):
    def actual_results(
        self,
        user_id: UUID,
        environment: str,
        *,
        source: str | None = None,
        source_type: str | None = None,
        source_candidate_id: str | None = None,
        source_version: str | None = None,
        strategy_id: str | None = None,
        strategy_version: str | None = None,
        signal_source_mode: str | None = None,
        signal_provider: str | None = None,
        venue: str | None = None,
        account_id: str | None = None,
        instrument_id: UUID | None = None,
        direction: str | None = None,
        risk_tier: str | None = None,
        campaign_id: UUID | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
    ) -> dict[str, Any]:
        if environment not in {"SHADOW", "TESTNET", "LIVE"}:
            raise DomainRejected("ENVIRONMENT_INVALID", "results require an exact environment")
        if signal_source_mode not in {None, "PERPTAPE", "WEBHOOK", "MANUAL", "SYSTEM"}:
            raise DomainRejected(
                "SIGNAL_SOURCE_MODE_INVALID",
                "results require an exact supported signal source mode",
            )
        if signal_provider not in {None, "TRADINGVIEW", "MODEL", "PERPTAPE"}:
            raise DomainRejected(
                "SIGNAL_PROVIDER_INVALID",
                "results require an exact supported signal provider",
            )
        if from_time is not None and to_time is not None and from_time > to_time:
            raise DomainRejected("TIME_RANGE_INVALID", "results from_time must not exceed to_time")
        workspace_id, team_id = self.facade._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            campaign_query = select(Campaign).where(
                Campaign.environment == environment,
                Campaign.team_id == team_id,
            )
            for field, value in (
                (Campaign.venue, venue),
                (Campaign.account_id, account_id),
                (Campaign.instrument_id, instrument_id),
                (Campaign.direction, direction),
                (Campaign.campaign_id, campaign_id),
            ):
                if value is not None:
                    campaign_query = campaign_query.where(field == value)
            if from_time is not None:
                campaign_query = campaign_query.where(Campaign.updated_at >= from_time)
            if to_time is not None:
                campaign_query = campaign_query.where(Campaign.updated_at <= to_time)
            campaigns = [
                item
                for item in session.scalars(
                    campaign_query.order_by(Campaign.updated_at, Campaign.campaign_id)
                ).all()
                if self.service.can_user(user_id, "view", item.account_id, item.venue)
            ]
            attribution_cache: dict[UUID, dict[str, Any]] = {}

            def report_attribution(proposal: Proposal) -> dict[str, Any]:
                attribution = attribution_cache.get(proposal.proposal_id)
                if attribution is None:
                    attribution = _report_attribution(session, proposal)
                    attribution_cache[proposal.proposal_id] = attribution
                return attribution

            rows: list[dict[str, Any]] = []
            totals: dict[str, dict[str, Decimal]] = {}
            for campaign in campaigns:
                proposal = session.get(Proposal, campaign.proposal_id)
                attribution = None if proposal is None else report_attribution(proposal)
                if source is not None and (proposal is None or proposal.source != source):
                    continue
                if source_type is not None and (
                    attribution is None or attribution["source_type"] != source_type
                ):
                    continue
                if source_candidate_id is not None and (
                    proposal is None or proposal.source_candidate_id != source_candidate_id
                ):
                    continue
                if source_version is not None and (
                    attribution is None or attribution["strategy_version"] != source_version
                ):
                    continue
                if strategy_id is not None and (
                    attribution is None or attribution["strategy_id"] != strategy_id
                ):
                    continue
                if strategy_version is not None and (
                    attribution is None or attribution["strategy_version"] != strategy_version
                ):
                    continue
                if signal_source_mode is not None and (
                    attribution is None or attribution["signal_source_mode"] != signal_source_mode
                ):
                    continue
                if signal_provider is not None and (
                    attribution is None or attribution["signal_provider"] != signal_provider
                ):
                    continue
                if risk_tier is not None and (proposal is None or proposal.risk_tier != risk_tier):
                    continue
                instrument = session.get(Instrument, campaign.instrument_id)
                intents = session.scalars(
                    select(OrderIntent).where(OrderIntent.campaign_id == campaign.campaign_id)
                ).all()
                intent_ids = [item.intent_id for item in intents]
                fills = (
                    session.scalars(
                        select(VenueFill)
                        .where(VenueFill.order_intent_id.in_(intent_ids))
                        .order_by(VenueFill.executed_at, VenueFill.venue_fill_id)
                    ).all()
                    if intent_ids
                    else []
                )
                funding = session.scalars(
                    select(FundingPayment).where(FundingPayment.campaign_id == campaign.campaign_id)
                ).all()
                currency = "UNKNOWN" if instrument is None else instrument.collateral_currency
                fees = sum((item.fee for item in fills), Decimal(0))
                slippage = sum((item.slippage_cost for item in fills), Decimal(0))
                funding_total = sum((item.amount for item in funding), Decimal(0))
                total_bucket = totals.setdefault(
                    currency,
                    {
                        "realized_pnl": Decimal(0),
                        "unrealized_pnl": Decimal(0),
                        "final_pnl": Decimal(0),
                        "fees": Decimal(0),
                        "funding": Decimal(0),
                        "slippage": Decimal(0),
                    },
                )
                total_bucket["realized_pnl"] += campaign.realized_pnl
                total_bucket["unrealized_pnl"] += campaign.unrealized_pnl
                total_bucket["final_pnl"] += campaign.final_pnl
                total_bucket["fees"] += fees
                total_bucket["funding"] += funding_total
                total_bucket["slippage"] += slippage
                rows.append(
                    {
                        "campaign_id": str(campaign.campaign_id),
                        "workspace_id": str(workspace_id),
                        "team_id": str(team_id),
                        "environment": campaign.environment,
                        "actuality": {
                            "SHADOW": "SYNTHETIC_RECORDED_FACTS",
                            "TESTNET": "NON_PRODUCTION_RECORDED_FACTS",
                            "LIVE": "LIVE_RECORDED_FACTS",
                        }[campaign.environment],
                        "status": campaign.status,
                        "account_id": campaign.account_id,
                        "venue": campaign.venue,
                        "instrument_id": str(campaign.instrument_id),
                        "symbol": None if instrument is None else instrument.symbol,
                        "currency": currency,
                        "direction": campaign.direction,
                        "source": None if proposal is None else proposal.source,
                        "source_type": (
                            None if attribution is None else attribution["source_type"]
                        ),
                        "strategy_id": (
                            None if attribution is None else attribution["strategy_id"]
                        ),
                        "strategy_version": (
                            None if attribution is None else attribution["strategy_version"]
                        ),
                        "signal_source_mode": (
                            None if attribution is None else attribution["signal_source_mode"]
                        ),
                        "signal_source_id": (
                            None if attribution is None else attribution["signal_source_id"]
                        ),
                        "signal_provider": (
                            None if attribution is None else attribution["signal_provider"]
                        ),
                        "signal_external_id": (
                            None if attribution is None else attribution["signal_external_id"]
                        ),
                        "source_attribution": (
                            None if attribution is None else attribution["attribution"]
                        ),
                        "source_candidate_id": (
                            None if proposal is None else proposal.source_candidate_id
                        ),
                        "source_version": (
                            None if attribution is None else attribution["strategy_version"]
                        ),
                        "risk_tier": None if proposal is None else proposal.risk_tier,
                        "fill_count": len(fills),
                        "filled_quantity": str(sum((item.quantity for item in fills), Decimal(0))),
                        "realized_pnl": str(campaign.realized_pnl),
                        "unrealized_pnl": str(campaign.unrealized_pnl),
                        "final_pnl": str(campaign.final_pnl),
                        "fees": str(fees),
                        "funding": str(funding_total),
                        "slippage": str(slippage),
                        "created_at": _iso(campaign.created_at),
                        "updated_at": _iso(campaign.updated_at),
                    }
                )

            risk_proposal_query = select(Proposal).where(
                Proposal.environment == environment,
                Proposal.team_id == team_id,
            )
            for field, value in (
                (Proposal.venue, venue),
                (Proposal.account_id, account_id),
                (Proposal.instrument_id, instrument_id),
                (Proposal.direction, direction),
            ):
                if value is not None:
                    risk_proposal_query = risk_proposal_query.where(field == value)
            campaign_proposal_ids = {
                item.proposal_id for item in campaigns if campaign_id in {None, item.campaign_id}
            }
            risk_proposals: dict[UUID, tuple[Proposal, dict[str, Any]]] = {}
            for proposal in session.scalars(risk_proposal_query).all():
                if not self.service.can_user(user_id, "view", proposal.account_id, proposal.venue):
                    continue
                if campaign_id is not None and proposal.proposal_id not in campaign_proposal_ids:
                    continue
                attribution = report_attribution(proposal)
                if source is not None and proposal.source != source:
                    continue
                if source_type is not None and attribution["source_type"] != source_type:
                    continue
                if source_candidate_id is not None and (
                    proposal.source_candidate_id != source_candidate_id
                ):
                    continue
                if source_version is not None and (
                    attribution["strategy_version"] != source_version
                ):
                    continue
                if strategy_id is not None and attribution["strategy_id"] != strategy_id:
                    continue
                if strategy_version is not None and (
                    attribution["strategy_version"] != strategy_version
                ):
                    continue
                if signal_source_mode is not None and (
                    attribution["signal_source_mode"] != signal_source_mode
                ):
                    continue
                if signal_provider is not None and (
                    attribution["signal_provider"] != signal_provider
                ):
                    continue
                if risk_tier is not None and proposal.risk_tier != risk_tier:
                    continue
                risk_proposals[proposal.proposal_id] = (proposal, attribution)

            risk_events: list[dict[str, Any]] = []
            if risk_proposals:
                decision_query = select(RiskDecision).where(
                    RiskDecision.team_id == team_id,
                    RiskDecision.proposal_id.in_(risk_proposals),
                )
                if from_time is not None:
                    decision_query = decision_query.where(RiskDecision.created_at >= from_time)
                if to_time is not None:
                    decision_query = decision_query.where(RiskDecision.created_at <= to_time)
                for decision in session.scalars(
                    decision_query.order_by(RiskDecision.created_at, RiskDecision.decision_id)
                ).all():
                    proposal, attribution = risk_proposals[decision.proposal_id]
                    policy = session.get(RiskPolicy, decision.policy_id)
                    risk_events.append(
                        {
                            "decision_id": str(decision.decision_id),
                            "proposal_id": str(proposal.proposal_id),
                            "workspace_id": str(workspace_id),
                            "team_id": str(team_id),
                            "environment": environment,
                            "account_id": proposal.account_id,
                            "venue": proposal.venue,
                            "instrument_id": str(proposal.instrument_id),
                            "direction": proposal.direction,
                            "risk_tier": proposal.risk_tier,
                            "source": proposal.source,
                            **attribution,
                            "result": decision.result,
                            "reasons": list(decision.reasons),
                            "risk_amount": str(decision.risk_amount),
                            "approved_quantity": str(decision.approved_quantity),
                            "policy_id": str(decision.policy_id),
                            "policy_version": None if policy is None else policy.version,
                            "policy_revision": None if policy is None else policy.revision,
                            "data_as_of": _iso(decision.data_as_of),
                            "created_at": _iso(decision.created_at),
                        }
                    )

            curves: dict[str, dict[str, Any]] = {}
            for currency in totals:
                metrics = _performance_metrics(
                    [item for item in rows if item["currency"] == currency]
                )
                curves[currency] = {
                    "points": metrics["curve"],
                    "maximum_drawdown": metrics["maximum_drawdown"],
                    "unit": currency,
                    "percentage_available": False,
                }

            team = session.get(Team, team_id)
            team_name = "Unknown Team" if team is None else team.name
            dimension_buckets: dict[str, dict[str, dict[str, Any]]] = {
                "team": {
                    str(team_id): {
                        "key": str(team_id),
                        "label": team_name,
                        "scope": {"team_id": str(team_id), "team_name": team_name},
                        "campaigns": [],
                        "risk_events": [],
                    }
                },
                "account": {},
                "strategy": {},
                "signal_source": {},
            }

            def add_dimension_record(record: dict[str, Any], *, risk_event: bool) -> None:
                strategy_key = ":".join(
                    [
                        str(record.get("strategy_id") or "MANUAL"),
                        str(record.get("strategy_version") or "UNVERSIONED"),
                    ]
                )
                signal_key = ":".join(
                    [
                        str(record.get("signal_source_mode") or "UNKNOWN"),
                        str(record.get("signal_source_id") or "NO_SOURCE_ID"),
                        str(record.get("signal_provider") or "NO_PROVIDER"),
                    ]
                )
                descriptors = {
                    "team": (
                        str(team_id),
                        team_name,
                        {"team_id": str(team_id), "team_name": team_name},
                    ),
                    "account": (
                        f"{record['venue']}:{record['account_id']}",
                        f"{record['account_id']} / {record['venue']}",
                        {
                            "account_id": record["account_id"],
                            "venue": record["venue"],
                        },
                    ),
                    "strategy": (
                        strategy_key,
                        (
                            "MANUAL"
                            if record.get("strategy_id") is None
                            else (
                                f"{record['strategy_id']} / {record.get('strategy_version') or '—'}"
                            )
                        ),
                        {
                            "strategy_id": record.get("strategy_id"),
                            "strategy_version": record.get("strategy_version"),
                        },
                    ),
                    "signal_source": (
                        signal_key,
                        " / ".join(
                            filter(
                                None,
                                [
                                    record.get("signal_source_mode"),
                                    record.get("signal_provider"),
                                ],
                            )
                        ),
                        {
                            "signal_source_mode": record.get("signal_source_mode"),
                            "signal_source_id": record.get("signal_source_id"),
                            "signal_provider": record.get("signal_provider"),
                        },
                    ),
                }
                target = "risk_events" if risk_event else "campaigns"
                for dimension, (key, label, scope) in descriptors.items():
                    bucket = dimension_buckets[dimension].setdefault(
                        key,
                        {
                            "key": key,
                            "label": label,
                            "scope": scope,
                            "campaigns": [],
                            "risk_events": [],
                        },
                    )
                    bucket[target].append(record)

            for row in rows:
                add_dimension_record(row, risk_event=False)
            for event in risk_events:
                add_dimension_record(event, risk_event=True)

            dimensions: dict[str, list[dict[str, Any]]] = {}
            for dimension, buckets in dimension_buckets.items():
                groups: list[dict[str, Any]] = []
                for dimension_bucket in buckets.values():
                    currency_rows: dict[str, list[dict[str, Any]]] = {}
                    for row in dimension_bucket["campaigns"]:
                        currency_rows.setdefault(str(row["currency"]), []).append(row)
                    result_counts: dict[str, int] = {}
                    reason_counts: dict[str, int] = {}
                    for event in dimension_bucket["risk_events"]:
                        result_counts[event["result"]] = result_counts.get(event["result"], 0) + 1
                        for reason in event["reasons"]:
                            reason_counts[reason] = reason_counts.get(reason, 0) + 1
                    groups.append(
                        {
                            "key": dimension_bucket["key"],
                            "label": dimension_bucket["label"],
                            "scope": dimension_bucket["scope"],
                            "campaign_count": len(dimension_bucket["campaigns"]),
                            "risk_event_count": len(dimension_bucket["risk_events"]),
                            "risk_events_by_result": result_counts,
                            "risk_events_by_reason": reason_counts,
                            "metrics_by_currency": {
                                currency: _performance_metrics(currency_campaigns)
                                for currency, currency_campaigns in currency_rows.items()
                            },
                        }
                    )
                dimensions[dimension] = sorted(groups, key=lambda item: item["label"])

            return {
                "scope": {
                    "workspace_id": str(workspace_id),
                    "team_id": str(team_id),
                    "team_name": team_name,
                },
                "environment": environment,
                "report_state": "RECORDED_HISTORY",
                "data_status": "AVAILABLE" if rows or risk_events else "EMPTY",
                "filters": {
                    "source": source,
                    "source_type": source_type,
                    "source_candidate_id": source_candidate_id,
                    "source_version": source_version,
                    "strategy_id": strategy_id,
                    "strategy_version": strategy_version,
                    "signal_source_mode": signal_source_mode,
                    "signal_provider": signal_provider,
                    "venue": venue,
                    "account_id": account_id,
                    "instrument_id": None if instrument_id is None else str(instrument_id),
                    "direction": direction,
                    "risk_tier": risk_tier,
                    "campaign_id": None if campaign_id is None else str(campaign_id),
                    "from": _iso(from_time),
                    "to": _iso(to_time),
                },
                "environment_notice": {
                    "SHADOW": "Synthetic facts; not exchange execution or profit",
                    "TESTNET": "Recorded non-production facts; not live profit",
                    "LIVE": "Recorded LIVE facts; no profitability guarantee",
                }[environment],
                "campaigns": rows,
                "risk_events": risk_events,
                "dimensions": dimensions,
                "coverage": {
                    "campaign_count": len(rows),
                    "closed_campaign_count": sum(1 for item in rows if item["status"] == "CLOSED"),
                    "risk_event_count": len(risk_events),
                    "currency_mixing": "SEPARATED",
                    "percentage_metrics": "OPENING_CAPITAL_UNAVAILABLE",
                    "time_filter_semantics": {
                        "campaigns": "campaign.updated_at",
                        "risk_events": "risk_decision.created_at",
                    },
                },
                "totals_by_currency": {
                    currency: {key: str(value) for key, value in values.items()}
                    for currency, values in totals.items()
                },
                "curves_by_currency": curves,
            }

    def audit_timeline(
        self, user_id: UUID, environment: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        if environment not in {"SHADOW", "TESTNET", "LIVE"}:
            raise DomainRejected("ENVIRONMENT_INVALID", "audit requires an exact environment")
        workspace_id, team_id = self.facade._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            object_ids: set[str] = set()
            proposals = [
                item
                for item in session.scalars(
                    select(Proposal).where(
                        Proposal.environment == environment,
                        Proposal.team_id == team_id,
                    )
                ).all()
                if self.service.can_user(user_id, "view", item.account_id, item.venue)
            ]
            proposal_ids = [item.proposal_id for item in proposals]
            object_ids.update(str(item.proposal_id) for item in proposals)
            campaigns = [
                item
                for item in session.scalars(
                    select(Campaign).where(
                        Campaign.environment == environment,
                        Campaign.team_id == team_id,
                    )
                ).all()
                if self.service.can_user(user_id, "view", item.account_id, item.venue)
            ]
            campaign_ids = [item.campaign_id for item in campaigns]
            object_ids.update(str(item.campaign_id) for item in campaigns)
            if proposal_ids:
                object_ids.update(
                    str(item.decision_id)
                    for item in session.scalars(
                        select(RiskDecision).where(RiskDecision.proposal_id.in_(proposal_ids))
                    ).all()
                )
                object_ids.update(
                    str(item.authorization_id)
                    for item in session.scalars(
                        select(TradingAuthorization).where(
                            TradingAuthorization.proposal_id.in_(proposal_ids)
                        )
                    ).all()
                )
            if campaign_ids:
                intents = session.scalars(
                    select(OrderIntent).where(OrderIntent.campaign_id.in_(campaign_ids))
                ).all()
                intent_ids = [item.intent_id for item in intents]
                object_ids.update(str(item.intent_id) for item in intents)
                object_ids.update(
                    str(item.reservation_id)
                    for item in session.scalars(
                        select(RiskReservation).where(RiskReservation.campaign_id.in_(campaign_ids))
                    ).all()
                )
                object_ids.update(
                    str(item.funding_payment_id)
                    for item in session.scalars(
                        select(FundingPayment).where(FundingPayment.campaign_id.in_(campaign_ids))
                    ).all()
                )
                object_ids.update(
                    str(item.reconciliation_id)
                    for item in session.scalars(
                        select(ReconciliationRun).where(
                            ReconciliationRun.campaign_id.in_(campaign_ids)
                        )
                    ).all()
                )
                if intent_ids:
                    object_ids.update(
                        str(item.venue_order_fact_id)
                        for item in session.scalars(
                            select(VenueOrder).where(VenueOrder.order_intent_id.in_(intent_ids))
                        ).all()
                    )
                    object_ids.update(
                        str(item.venue_fill_fact_id)
                        for item in session.scalars(
                            select(VenueFill).where(VenueFill.order_intent_id.in_(intent_ids))
                        ).all()
                    )
            transfer_proposals = [
                item
                for item in session.scalars(
                    select(TransferProposal).where(
                        TransferProposal.team_id == team_id,
                        TransferProposal.environment == environment,
                    )
                ).all()
                if self.service.can_user(user_id, "capital.view", item.account_id, item.venue)
            ]
            transfer_proposal_ids = [item.transfer_proposal_id for item in transfer_proposals]
            object_ids.update(str(item) for item in transfer_proposal_ids)
            if transfer_proposal_ids:
                transfer_authorizations = session.scalars(
                    select(TransferAuthorization).where(
                        TransferAuthorization.team_id == team_id,
                        TransferAuthorization.transfer_proposal_id.in_(transfer_proposal_ids),
                    )
                ).all()
                authorization_ids = [
                    item.transfer_authorization_id for item in transfer_authorizations
                ]
                object_ids.update(str(item) for item in authorization_ids)
                if authorization_ids:
                    object_ids.update(
                        str(item.capital_transfer_id)
                        for item in session.scalars(
                            select(CapitalTransfer).where(
                                CapitalTransfer.team_id == team_id,
                                CapitalTransfer.transfer_authorization_id.in_(authorization_ids),
                            )
                        ).all()
                    )
            policies = [
                item
                for item in session.scalars(
                    select(CapitalAutomationPolicy).where(
                        CapitalAutomationPolicy.team_id == team_id,
                        CapitalAutomationPolicy.environment == environment,
                    )
                ).all()
                if self.service.can_user(user_id, "capital.view", item.account_id, item.venue)
            ]
            object_ids.update(str(item.policy_id) for item in policies)
            if not object_ids:
                return []
            events = session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.object_id.in_(object_ids),
                    AuditEvent.workspace_id == workspace_id,
                    AuditEvent.team_id == team_id,
                )
                .order_by(AuditEvent.created_at.desc(), AuditEvent.audit_event_id)
                .limit(limit)
            ).all()
            parsed_actor_ids = {
                item.actor_id: parsed
                for item in events
                if (parsed := _uuid_or_none(item.actor_id)) is not None
            }
            actors = {
                item.user_id: item.username
                for item in session.scalars(
                    select(User).where(User.user_id.in_(parsed_actor_ids.values()))
                ).all()
            }
            return [
                {
                    "audit_event_id": str(item.audit_event_id),
                    "workspace_id": str(workspace_id),
                    "team_id": str(team_id),
                    "account_id": item.account_id,
                    "actor_id": item.actor_id,
                    "api_client_id": (
                        None if item.api_client_id is None else str(item.api_client_id)
                    ),
                    "actor": actors.get(parsed_actor_ids[item.actor_id], item.actor_id)
                    if item.actor_id in parsed_actor_ids
                    else item.actor_id,
                    "event_type": item.event_type,
                    "object_type": item.object_type,
                    "object_id": item.object_id,
                    "reason": item.reason,
                    "correlation_id": str(item.correlation_id),
                    "idempotency_key": item.idempotency_key,
                    "object_version": item.object_version,
                    "created_at": _iso(item.created_at),
                }
                for item in events
            ]

    def runtime_snapshot(self, user_id: UUID) -> dict[str, Any]:
        _workspace_id, team_id = self.facade._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            gates = session.scalars(
                select(CapabilityGate).order_by(CapabilityGate.capability_key)
            ).all()
            revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            table_count = session.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name <> 'alembic_version'"
                )
            ).scalar_one()
            perptape_feed = session.get(PerptapeFeed, (team_id, "BREAKOUTS"))
            source_health = session.scalars(
                select(RuntimeSourceHealth)
                .where(RuntimeSourceHealth.team_id == team_id)
                .order_by(
                    RuntimeSourceHealth.source_name,
                    RuntimeSourceHealth.account_id,
                )
            ).all()
            runtime_binding_counts = {
                venue: int(count)
                for venue, count in session.execute(
                    select(ExchangeAccount.venue, func.count())
                    .where(
                        ExchangeAccount.team_id == team_id,
                        ExchangeAccount.runtime_sync_enabled.is_(True),
                    )
                    .group_by(ExchangeAccount.venue)
                ).all()
            }
            freqtrade_binding_counts = {
                venue: int(count)
                for venue, count in session.execute(
                    select(ExchangeAccount.venue, func.count())
                    .where(
                        ExchangeAccount.team_id == team_id,
                        ExchangeAccount.freqtrade_worker_mode != "UNCONFIGURED",
                    )
                    .group_by(ExchangeAccount.venue)
                ).all()
            }
            return {
                "database_ready": self.database.is_ready()[0],
                "schema_revision": revision,
                "business_table_count": int(table_count),
                "capability_gates": {
                    item.capability_key: {
                        "status": item.status,
                        "reason": item.reason,
                        "updated_at": _iso(item.updated_at),
                    }
                    for item in gates
                },
                "perptape_feed": (
                    {
                        "available": True,
                        "contract_version": perptape_feed.contract_version,
                        "candidate_count": len(perptape_feed.candidates),
                        "generated_at": _iso(perptape_feed.generated_at),
                        "fetched_at": _iso(perptape_feed.fetched_at),
                        "updated_at": _iso(perptape_feed.updated_at),
                    }
                    if perptape_feed is not None
                    else {
                        "available": False,
                        "contract_version": None,
                        "candidate_count": 0,
                        "generated_at": None,
                        "fetched_at": None,
                        "updated_at": None,
                    }
                ),
                "source_health": {
                    (
                        item.source_name
                        if item.account_id is None
                        else f"{item.source_name}:{item.account_id}"
                    ): {
                        "status": item.status,
                        "account_id": item.account_id,
                        "venue": item.venue,
                        "items_observed": item.items_observed,
                        "error_code": item.error_code,
                        "checked_at": _iso(item.checked_at),
                        "last_success_at": _iso(item.last_success_at),
                        "retry_at": _iso(item.retry_at),
                        "consecutive_failures": item.consecutive_failures,
                    }
                    for item in source_health
                },
                "runtime_binding_counts": runtime_binding_counts,
                "freqtrade_binding_counts": freqtrade_binding_counts,
            }

    def runtime_source_health(
        self,
        user_id: UUID,
        source_name: str,
        *,
        account_id: str | None = None,
        venue: str | None = None,
    ) -> dict[str, Any] | None:
        _workspace_id, team_id = self.facade._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            item = session.scalar(
                select(RuntimeSourceHealth).where(
                    RuntimeSourceHealth.team_id == team_id,
                    RuntimeSourceHealth.source_name == source_name,
                    RuntimeSourceHealth.account_id == account_id,
                    RuntimeSourceHealth.venue == venue,
                )
            )
            if item is None:
                return None
            return {
                "status": item.status,
                "account_id": item.account_id,
                "venue": item.venue,
                "items_observed": item.items_observed,
                "error_code": item.error_code,
                "checked_at": _iso(item.checked_at),
                "last_success_at": _iso(item.last_success_at),
                "retry_at": _iso(item.retry_at),
                "consecutive_failures": item.consecutive_failures,
            }

    def list_campaigns(self, user_id: UUID) -> list[dict[str, Any]]:
        workspace_id, team_id = self.facade._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            values = session.execute(
                select(Campaign, Instrument)
                .outerjoin(Instrument, Instrument.instrument_id == Campaign.instrument_id)
                .where(Campaign.team_id == team_id)
                .order_by(Campaign.updated_at.desc(), Campaign.campaign_id)
            ).all()
            result: list[dict[str, Any]] = []
            for campaign, instrument in values:
                if not self.service.can_user(user_id, "view", campaign.account_id, campaign.venue):
                    continue
                summary = self._campaign_summary(campaign, instrument)
                summary["workspace_id"] = str(workspace_id)
                result.append(summary)
            return result

    def shadow_workspace(self, user_id: UUID) -> dict[str, Any]:
        """Project team-scoped virtual capital without exposing live credentials."""

        workspace_id, team_id = self.facade._active_scope_ids(user_id)
        activation = self.service.shadow_activation_status(user_id)
        with self.database.session_factory() as session:
            team = session.get(Team, team_id)
            if team is None or not team.active or team.workspace_id != workspace_id:
                raise DomainRejected("TEAM_SCOPE_DENIED", "active team is unavailable")
            assignments = session.scalars(
                select(RoleAssignment).where(
                    RoleAssignment.user_id == user_id,
                    RoleAssignment.team_id == team_id,
                )
            ).all()

            def granted(action: str, account_id: str, venue: str) -> bool:
                return any(
                    (item.account_scope is None or item.account_scope == account_id)
                    and (item.venue_scope is None or item.venue_scope == venue)
                    and (
                        action in ROLE_ACTIONS[Role(item.role)]
                        or "*" in ROLE_ACTIONS[Role(item.role)]
                    )
                    for item in assignments
                )

            accounts = [
                item
                for item in session.scalars(
                    select(ExchangeAccount)
                    .where(ExchangeAccount.team_id == team_id, ExchangeAccount.active)
                    .order_by(
                        ExchangeAccount.venue,
                        ExchangeAccount.label,
                        ExchangeAccount.account_id,
                    )
                ).all()
                if granted("venue.view", item.account_id, item.venue)
            ]
            equities = session.scalars(
                select(AccountEquity).where(
                    AccountEquity.team_id == team_id,
                    AccountEquity.environment == "SHADOW",
                )
            ).all()
            positions = session.execute(
                select(Position, Instrument)
                .join(Instrument, Instrument.instrument_id == Position.instrument_id)
                .where(
                    Position.team_id == team_id,
                    Position.environment == "SHADOW",
                )
                .order_by(Position.venue, Position.account_id, Instrument.symbol)
            ).all()
            campaigns = session.execute(
                select(Campaign, Instrument)
                .outerjoin(Instrument, Instrument.instrument_id == Campaign.instrument_id)
                .where(
                    Campaign.team_id == team_id,
                    Campaign.environment == "SHADOW",
                )
                .order_by(Campaign.updated_at.desc(), Campaign.campaign_id)
            ).all()
            occupied = {
                (account_id, venue): Decimal(str(amount))
                for account_id, venue, amount in session.execute(
                    select(
                        Campaign.account_id,
                        Campaign.venue,
                        func.coalesce(func.sum(RiskReservation.amount), 0),
                    )
                    .join(Campaign, Campaign.campaign_id == RiskReservation.campaign_id)
                    .where(
                        Campaign.team_id == team_id,
                        Campaign.environment == "SHADOW",
                        RiskReservation.status.in_(("RESERVED", "OPEN", "UNKNOWN")),
                    )
                    .group_by(Campaign.account_id, Campaign.venue)
                ).all()
            }
            instrument_rows = session.scalars(
                select(Instrument)
                .where(
                    Instrument.active,
                    Instrument.collateral_currency.in_(tuple(USD_STABLE_ASSETS)),
                    Instrument.quote_currency == Instrument.collateral_currency,
                )
                .order_by(Instrument.venue, Instrument.symbol)
            ).all()
            gates = {
                item.capability_key: item.status
                for item in session.scalars(
                    select(CapabilityGate).where(
                        CapabilityGate.capability_key.in_(
                            ("LIVE_ORDER_SEND", "CAPITAL_TRANSFER", "SIGNING", "BROADCAST")
                        )
                    )
                ).all()
            }

            equity_by_scope: dict[tuple[str, str], list[AccountEquity]] = {}
            for item in equities:
                equity_by_scope.setdefault((item.account_id, item.venue), []).append(item)
            position_by_scope: dict[tuple[str, str], list[tuple[Position, Instrument]]] = {}
            for position, instrument in positions:
                position_by_scope.setdefault((position.account_id, position.venue), []).append(
                    (position, instrument)
                )

            account_rows: list[dict[str, Any]] = []
            for account in accounts:
                scope = (account.account_id, account.venue)
                scope_equities = equity_by_scope.get(scope, [])
                scope_occupied = occupied.get(scope, Decimal(0))
                account_rows.append(
                    {
                        "exchange_account_id": str(account.exchange_account_id),
                        "account_id": account.account_id,
                        "venue": account.venue,
                        "label": account.label,
                        "connection_status": account.connection_status,
                        "trading_status": account.trading_status,
                        "credential_state": (
                            "UNCONFIGURED"
                            if account.credential_version == 0
                            else "CONFIGURED_REDACTED"
                        ),
                        "can_initialize": granted(
                            "account.manage", account.account_id, account.venue
                        ),
                        "occupied_risk": str(scope_occupied),
                        "virtual_capital": [
                            {
                                "account_equity_id": str(item.account_equity_id),
                                "currency": item.currency,
                                "equity": str(item.equity),
                                "available_balance": str(item.available_balance),
                                "risk_available": str(
                                    max(Decimal(0), item.available_balance - scope_occupied)
                                ),
                                "fact_status": item.fact_status,
                                "control_status": item.control_status,
                                "observed_at": _iso(item.observed_at),
                            }
                            for item in scope_equities
                        ],
                        "positions": [
                            {
                                "position_id": str(position.position_id),
                                "instrument_id": str(position.instrument_id),
                                "symbol": instrument.symbol,
                                "quantity": str(position.quantity),
                                "average_entry_price": str(position.average_entry_price),
                                "mark_price": str(position.mark_price),
                                "fact_status": position.fact_status,
                                "observed_at": _iso(position.observed_at),
                            }
                            for position, instrument in position_by_scope.get(scope, [])
                        ],
                        "instruments": [
                            {
                                "instrument_id": str(item.instrument_id),
                                "symbol": item.symbol,
                                "currency": item.collateral_currency,
                                "tick_size": str(item.tick_size),
                                "lot_size": str(item.lot_size),
                            }
                            for item in instrument_rows
                            if item.venue == account.venue
                        ],
                    }
                )

            campaign_rows = []
            for campaign, instrument in campaigns:
                if not granted("view", campaign.account_id, campaign.venue):
                    continue
                campaign_summary = self._campaign_summary(campaign, instrument)
                campaign_summary["workspace_id"] = str(workspace_id)
                campaign_rows.append(campaign_summary)

            return {
                "workspace_id": str(workspace_id),
                "team_id": str(team_id),
                "team_name": team.name,
                "execution_mode": team.execution_mode,
                "trading_enabled": team.trading_enabled,
                "version": team.version,
                "activation": activation,
                "safety_boundary": {
                    "environment": "SHADOW",
                    "capital": "VIRTUAL_ONLY",
                    "venue_connectors_used": False,
                    "live_order_send": False,
                    "funding": False,
                    "signing": False,
                    "broadcast": False,
                    "runtime_gate_status": gates,
                },
                "accounts": account_rows,
                "campaigns": campaign_rows,
            }

    def campaign_detail(self, user_id: UUID, campaign_id: UUID) -> dict[str, Any]:
        workspace_id, team_id = self.facade._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise DomainRejected("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            if campaign.team_id != team_id:
                raise DomainRejected("TEAM_SCOPE_DENIED", "campaign is outside active team")
            if not self.service.can_user(user_id, "view", campaign.account_id, campaign.venue):
                raise DomainRejected("RBAC_DENIED", "campaign is outside the current scope")
            instrument = session.get(Instrument, campaign.instrument_id)
            proposal = session.get(Proposal, campaign.proposal_id)
            authorization = session.get(TradingAuthorization, campaign.authorization_id)
            auto_add_gate = session.get(CapabilityGate, "AUTO_ADD")
            reservations = session.scalars(
                select(RiskReservation)
                .where(RiskReservation.campaign_id == campaign_id)
                .order_by(RiskReservation.created_at)
            ).all()
            intents = session.scalars(
                select(OrderIntent)
                .where(OrderIntent.campaign_id == campaign_id)
                .order_by(OrderIntent.created_at, OrderIntent.intent_id)
            ).all()
            intent_ids = [item.intent_id for item in intents]
            orders = (
                session.scalars(
                    select(VenueOrder).where(VenueOrder.order_intent_id.in_(intent_ids))
                ).all()
                if intent_ids
                else []
            )
            fills = session.scalars(
                select(VenueFill)
                .where(VenueFill.campaign_id == campaign_id)
                .order_by(VenueFill.executed_at, VenueFill.venue_fill_fact_id)
            ).all()
            position = session.scalar(
                select(Position).where(
                    Position.team_id == campaign.team_id,
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.environment == campaign.environment,
                    Position.instrument_id == campaign.instrument_id,
                )
            )
            protection = (
                session.scalar(
                    select(ProtectionOrder).where(
                        ProtectionOrder.position_id == position.position_id
                    )
                )
                if position is not None
                else None
            )
            funding = session.scalars(
                select(FundingPayment)
                .where(FundingPayment.campaign_id == campaign_id)
                .order_by(FundingPayment.paid_at)
            ).all()
            scope = (
                f"{campaign.account_id}:{campaign.venue}"
                if campaign.environment == "SHADOW"
                else f"{campaign.environment}:{campaign.account_id}:{campaign.venue}"
            )
            reconciliation = session.scalar(
                select(ReconciliationRun)
                .where(
                    ReconciliationRun.team_id == campaign.team_id,
                    ReconciliationRun.execution_scope == scope,
                )
                .order_by(ReconciliationRun.completed_at.desc())
                .limit(1)
            )
            lease = session.get(SenderLease, (campaign.team_id, scope))
            orders_by_intent = {item.order_intent_id: item for item in orders}
            result = self._campaign_summary(campaign, instrument)
            result["workspace_id"] = str(workspace_id)
            result.update(
                {
                    "instrument": None
                    if instrument is None
                    else {
                        "symbol": instrument.symbol,
                        "collateral_currency": instrument.collateral_currency,
                    },
                    "authorization": None
                    if authorization is None
                    else {
                        "authorization_id": str(authorization.authorization_id),
                        "environment": authorization.environment,
                        "active": authorization.active,
                        "quantity_limit": str(authorization.quantity_limit),
                        "used_quantity": str(authorization.used_quantity),
                        "allowed_adds": authorization.allowed_adds,
                        "used_adds": authorization.used_adds,
                        "add_revoked_at": _iso(authorization.add_revoked_at),
                        "expires_at": _iso(authorization.expires_at),
                    },
                    "reservations": [
                        {
                            "reservation_id": str(item.reservation_id),
                            "workspace_id": str(workspace_id),
                            "team_id": str(campaign.team_id),
                            "account_id": campaign.account_id,
                            "status": item.status,
                            "amount": str(item.amount),
                            "version": item.version,
                            "created_at": _iso(item.created_at),
                            "updated_at": _iso(item.updated_at),
                        }
                        for item in reservations
                    ],
                    "intents": [
                        {
                            "intent_id": str(item.intent_id),
                            "workspace_id": str(workspace_id),
                            "team_id": str(campaign.team_id),
                            "account_id": campaign.account_id,
                            "kind": item.kind,
                            "side": item.side,
                            "quantity": str(item.quantity),
                            "limit_price": (
                                None if item.limit_price is None else str(item.limit_price)
                            ),
                            "reduce_only": item.reduce_only,
                            "trigger_source": item.trigger_source,
                            "trigger_observed_at": _iso(item.trigger_observed_at),
                            "add_unit_consumed": item.add_unit_consumed,
                            "status": item.status,
                            "dispatch": (
                                None
                                if item.dispatch_backend is None
                                else {
                                    "backend": item.dispatch_backend,
                                    "account_version": item.dispatch_account_version,
                                    "auth_version": item.dispatch_auth_version,
                                    "started_at": _iso(item.dispatch_started_at),
                                }
                            ),
                            "version": item.version,
                            "created_at": _iso(item.created_at),
                            "updated_at": _iso(item.updated_at),
                            "order": self._order_summary(
                                orders_by_intent.get(item.intent_id),
                                workspace_id=workspace_id,
                                team_id=campaign.team_id,
                                account_id=campaign.account_id,
                            ),
                        }
                        for item in intents
                    ],
                    "fills": [
                        {
                            "fill_id": str(item.venue_fill_fact_id),
                            "workspace_id": str(workspace_id),
                            "team_id": str(campaign.team_id),
                            "account_id": campaign.account_id,
                            "venue_fill_id": item.venue_fill_id,
                            "intent_id": str(item.order_intent_id),
                            "side": item.side,
                            "quantity": str(item.quantity),
                            "price": str(item.price),
                            "fee": str(item.fee),
                            "fee_currency": item.fee_currency,
                            "slippage_cost": str(item.slippage_cost),
                            "executed_at": _iso(item.executed_at),
                        }
                        for item in fills
                    ],
                    "position": None
                    if position is None
                    else {
                        "position_id": str(position.position_id),
                        "workspace_id": str(workspace_id),
                        "team_id": str(campaign.team_id),
                        "account_id": campaign.account_id,
                        "quantity": str(position.quantity),
                        "average_entry_price": str(position.average_entry_price),
                        "mark_price": str(position.mark_price),
                        "fact_status": position.fact_status,
                        "observed_at": _iso(position.observed_at),
                    },
                    "protection": None
                    if protection is None
                    else {
                        "protection_id": str(protection.protection_id),
                        "venue_order_id": protection.venue_order_id,
                        "quantity": str(protection.quantity),
                        "trigger_price": str(protection.trigger_price),
                        "status": protection.status,
                        "fully_covered": protection.fully_covered,
                        "observed_at": _iso(protection.observed_at),
                    },
                    "funding": [
                        {
                            "venue_payment_id": item.venue_payment_id,
                            "workspace_id": str(workspace_id),
                            "team_id": str(campaign.team_id),
                            "account_id": campaign.account_id,
                            "amount": str(item.amount),
                            "currency": item.currency,
                            "paid_at": _iso(item.paid_at),
                        }
                        for item in funding
                    ],
                    "reconciliation": None
                    if reconciliation is None
                    else {
                        "reconciliation_id": str(reconciliation.reconciliation_id),
                        "workspace_id": str(workspace_id),
                        "team_id": str(campaign.team_id),
                        "account_id": campaign.account_id,
                        "status": reconciliation.status,
                        "is_computed": reconciliation.is_computed,
                        "differences": reconciliation.differences,
                        "resolution_reason": reconciliation.resolution_reason,
                        "completed_at": _iso(reconciliation.completed_at),
                    },
                    "sender_lease": None
                    if lease is None
                    else {
                        "execution_scope": lease.execution_scope,
                        "owner_id": lease.owner_id,
                        "fencing_token": lease.fencing_token,
                        "expires_at": _iso(lease.expires_at),
                    },
                    "management": {
                        "auto_add_gate": (
                            "UNKNOWN" if auto_add_gate is None else auto_add_gate.status
                        ),
                        "allow_auto_add": bool(
                            proposal is not None
                            and isinstance(proposal.frozen_payload.get("details"), dict)
                            and proposal.frozen_payload["details"].get("allow_auto_add") is True
                        ),
                        "initial_quantity": (
                            None
                            if proposal is None
                            or not isinstance(proposal.frozen_payload.get("details"), dict)
                            else proposal.frozen_payload["details"].get("initial_quantity")
                        ),
                        "add_trigger_price": (
                            None
                            if proposal is None
                            or not isinstance(proposal.frozen_payload.get("details"), dict)
                            else proposal.frozen_payload["details"].get("add_trigger_price")
                        ),
                        "requested_adds": (
                            0
                            if proposal is None
                            or not isinstance(proposal.frozen_payload.get("details"), dict)
                            else proposal.frozen_payload["details"].get("requested_adds", 0)
                        ),
                        "remaining_quantity": (
                            "0"
                            if authorization is None or not authorization.active
                            else str(authorization.quantity_limit - authorization.used_quantity)
                        ),
                        "remaining_adds": (
                            0
                            if authorization is None
                            or not authorization.active
                            or authorization.add_revoked_at is not None
                            else authorization.allowed_adds - authorization.used_adds
                        ),
                    },
                }
            )
            return result

    def campaign_id_for_intent(self, user_id: UUID, intent_id: UUID) -> UUID:
        _workspace_id, team_id = self.facade._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            intent = session.get(OrderIntent, intent_id)
            if intent is None:
                raise DomainRejected("ORDER_INTENT_NOT_FOUND", "intent does not exist")
            campaign = session.get(Campaign, intent.campaign_id)
            if campaign is None:
                raise DomainRejected("CAMPAIGN_NOT_FOUND", "intent campaign is missing")
            if campaign.team_id != team_id:
                raise DomainRejected("TEAM_SCOPE_DENIED", "intent is outside active team")
            if not self.service.can_user(user_id, "view", campaign.account_id, campaign.venue):
                raise DomainRejected("RBAC_DENIED", "intent is outside the current scope")
            return campaign.campaign_id

    @staticmethod
    def _order_summary(
        order: VenueOrder | None,
        *,
        workspace_id: UUID | None = None,
        team_id: UUID | None = None,
        account_id: str | None = None,
    ) -> dict[str, Any] | None:
        if order is None:
            return None
        return {
            "venue_order_fact_id": str(order.venue_order_fact_id),
            "workspace_id": None if workspace_id is None else str(workspace_id),
            "team_id": None if team_id is None else str(team_id),
            "account_id": account_id or order.account_id,
            "venue_order_id": order.venue_order_id,
            "client_order_id": order.client_order_id,
            "status": order.status,
            "side": order.side,
            "order_type": order.order_type,
            "reduce_only": order.reduce_only,
            "ordered_quantity": str(order.ordered_quantity),
            "filled_quantity": str(order.filled_quantity),
            "observed_at": _iso(order.observed_at),
        }

    @staticmethod
    def _campaign_summary(
        campaign: Campaign, instrument: Instrument | None = None
    ) -> dict[str, Any]:
        return {
            "campaign_id": str(campaign.campaign_id),
            "team_id": str(campaign.team_id),
            "proposal_id": str(campaign.proposal_id),
            "authorization_id": str(campaign.authorization_id),
            "account_id": campaign.account_id,
            "venue": campaign.venue,
            "environment": campaign.environment,
            "instrument_id": str(campaign.instrument_id),
            "symbol": None if instrument is None else instrument.symbol,
            "collateral_currency": (None if instrument is None else instrument.collateral_currency),
            "direction": campaign.direction,
            "status": campaign.status,
            "current_target_quantity": str(campaign.current_target_quantity),
            "target_version": campaign.target_version,
            "target_reason": campaign.target_reason,
            "target_urgency": campaign.target_urgency,
            "target_calculated_at": _iso(campaign.target_calculated_at),
            "realized_pnl": str(campaign.realized_pnl),
            "unrealized_pnl": str(campaign.unrealized_pnl),
            "final_pnl": str(campaign.final_pnl),
            "created_at": _iso(campaign.created_at),
            "updated_at": _iso(campaign.updated_at),
        }

    @staticmethod
    def _proposal_summary(
        proposal: Proposal, instrument: Instrument | None = None
    ) -> dict[str, Any]:
        details = proposal.frozen_payload.get("details", {})
        candidate = details.get("candidate", {}) if isinstance(details, dict) else {}
        reference_price = (
            details.get("trigger_price")
            or candidate.get("reference_price")
            or candidate.get("threshold_price")
            if isinstance(details, dict) and isinstance(candidate, dict)
            else None
        )
        try:
            estimated_notional = (
                None
                if reference_price is None
                else (
                    proposal.quantity
                    * Decimal(str(reference_price))
                    * (Decimal(1) if instrument is None else instrument.contract_multiplier)
                ).quantize(Decimal("0.000000000000000001"))
            )
        except (ArithmeticError, TypeError, ValueError):
            estimated_notional = None
        return {
            "proposal_id": str(proposal.proposal_id),
            "team_id": str(proposal.team_id),
            "source": proposal.source,
            "environment": proposal.environment,
            "proposer_id": str(proposal.proposer_id),
            "strategy_id": proposal.strategy_id,
            "strategy_version": proposal.strategy_version,
            "source_candidate_id": proposal.source_candidate_id,
            "source_link": proposal.source_link,
            "source_observed_at": _iso(proposal.source_observed_at),
            "source_readiness": proposal.source_readiness,
            "signal_event_id": (
                None if proposal.signal_event_id is None else str(proposal.signal_event_id)
            ),
            "status": proposal.status,
            "version": proposal.version,
            "risk_tier": proposal.risk_tier,
            "account_id": proposal.account_id,
            "venue": proposal.venue,
            "instrument_id": str(proposal.instrument_id),
            "symbol": None if instrument is None else instrument.symbol,
            "quote_currency": None if instrument is None else instrument.quote_currency,
            "collateral_currency": (None if instrument is None else instrument.collateral_currency),
            "direction": proposal.direction,
            "quantity": str(proposal.quantity),
            "estimated_notional": (None if estimated_notional is None else str(estimated_notional)),
            "max_risk": str(proposal.max_risk),
            "expires_at": _iso(proposal.expires_at),
            "created_at": _iso(proposal.created_at),
            "updated_at": _iso(proposal.updated_at),
        }
