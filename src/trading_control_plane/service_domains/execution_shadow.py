from __future__ import annotations

from trading_control_plane.service_component import ServiceComponent

# The domain implementation intentionally consumes the explicit service_core export surface.
# ruff: noqa: F403, F405
from trading_control_plane.service_core import *


class ShadowExecutionService(ServiceComponent):
    def initialize_shadow_scope(
        self,
        *,
        actor_id: UUID,
        account_id: str,
        venue: str,
        instrument_id: UUID,
        currency: str,
        initial_equity: Decimal | None,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        """Compatibility endpoint resolving to the single active Team generation."""

        operation = "shadow.scope.resolve"
        normalized_currency = currency.upper()
        if initial_equity is not None and initial_equity != Decimal("100000"):
            _reject(
                "SHADOW_INITIAL_EQUITY_FIXED",
                "Team virtual capital is fixed at 100000 U and cannot be initialized per scope",
            )
        payload = {
            "account_id": account_id,
            "venue": venue,
            "instrument_id": str(instrument_id),
            "currency": normalized_currency,
            "initial_equity": None if initial_equity is None else str(initial_equity),
        }
        with self.database.session_factory.begin() as session:
            team = self.transactions._require_role(
                session, actor_id, "account.manage", account_id, venue
            )
            self.transactions._require_team_environment(team, ExecutionEnvironment.SHADOW)
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload={**payload, "team_id": str(team.team_id)},
            )
            if replay is not None:
                return replay
            account = session.scalar(
                select(ExchangeAccount).where(
                    ExchangeAccount.team_id == team.team_id,
                    ExchangeAccount.account_id == account_id,
                    ExchangeAccount.venue == venue,
                    ExchangeAccount.active,
                )
            )
            if account is None:
                _reject(
                    "EXCHANGE_ACCOUNT_NOT_FOUND",
                    "shadow source requires an active account in the current Team",
                )
            instrument = session.get(Instrument, instrument_id)
            if instrument is None or not instrument.active or instrument.venue != venue:
                _reject(
                    "INSTRUMENT_UNAVAILABLE",
                    "shadow instrument is outside the source account venue",
                )
            if normalized_currency not in {
                "U",
                "USD",
                instrument.collateral_currency.upper(),
            }:
                _reject(
                    "SHADOW_CURRENCY_MISMATCH",
                    "Team virtual capital is a USDT-equivalent U ledger",
                )
            shadow_account = session.scalar(
                select(TeamShadowAccount)
                .where(
                    TeamShadowAccount.team_id == team.team_id,
                    TeamShadowAccount.status == "ACTIVE",
                )
                .with_for_update()
            )
            if shadow_account is None:
                _reject(
                    "SHADOW_ACCOUNT_MISSING",
                    "Team virtual account is not initialized",
                )
            result = {
                "workspace_id": str(team.workspace_id),
                "team_id": str(team.team_id),
                "environment": ExecutionEnvironment.SHADOW.value,
                "shadow_account_id": str(shadow_account.shadow_account_id),
                "generation": shadow_account.generation,
                "initial_equity": str(shadow_account.initial_equity),
                "equity": str(shadow_account.equity),
                "account_equity_id": None,
                "position_id": None,
                "equity_created": False,
                "position_created": False,
                "scope_initialization_retired": True,
            }
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="SHADOW_SCOPE_RESOLVED",
                object_type="TeamShadowAccount",
                object_id=shadow_account.shadow_account_id,
                reason="legacy scope request resolved to unified Team generation",
                correlation_id=uuid4(),
                object_version=shadow_account.version,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                account_id=account_id,
                environment=ExecutionEnvironment.SHADOW.value,
                generation=shadow_account.generation,
                rule_summary={
                    **payload,
                    "scope_initialization_retired": True,
                    "venue_write_adapter_calls": 0,
                },
                now=now,
            )
            return result

    def simulate_shadow_execution(
        self,
        *,
        intent_id: UUID,
        actor_id: UUID,
        expected_version: int,
        reference_price: Decimal,
        fee_bps: Decimal,
        slippage_bps: Decimal,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        operation = "shadow.execution.simulate"
        with self.database.session_factory.begin() as session:
            intent = session.get(OrderIntent, intent_id, with_for_update=True)
            if intent is None:
                _reject("ORDER_INTENT_NOT_FOUND", "intent does not exist")
            campaign = session.get(Campaign, intent.campaign_id, with_for_update=True)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "intent campaign is missing")
            team = self.transactions._require_role(
                session,
                actor_id,
                "venue.record",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            self.transactions._require_team_environment(team, ExecutionEnvironment.SHADOW)
            if campaign.environment != ExecutionEnvironment.SHADOW.value:
                _reject(
                    "SHADOW_SCOPE_REQUIRED",
                    "the deterministic simulator accepts SHADOW intents only",
                )
            payload = {
                "intent_id": str(intent_id),
                "expected_version": expected_version,
                "reference_price": str(reference_price),
                "fee_bps": str(fee_bps),
                "slippage_bps": str(slippage_bps),
                "team_id": str(team.team_id),
            }
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return replay
            if intent.version != expected_version:
                _reject("VERSION_CONFLICT", "intent changed before shadow simulation")
            if intent.status != OrderIntentStatus.READY.value:
                _reject("ORDER_INTENT_NOT_READY", "one-shot simulation requires a ready intent")
            if session.scalar(
                select(ShadowOrder.shadow_order_id).where(
                    ShadowOrder.order_intent_id == intent_id
                )
            ):
                _reject("SHADOW_ORDER_EXISTS", "intent already belongs to a unified shadow order")
            instrument = session.get(Instrument, campaign.instrument_id)
            proposal = session.get(Proposal, campaign.proposal_id)
            if instrument is None or proposal is None:
                _reject("SHADOW_SCOPE_INCOMPLETE", "instrument or frozen proposal is missing")
            result = self.facade._simulate_linked_shadow_intent(
                session,
                actor_id=actor_id,
                team=team,
                campaign=campaign,
                intent=intent,
                proposal=proposal,
                catalog=instrument,
                reference_price=reference_price,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
                idempotency_key=idempotency_key,
                now=now,
            )
            session.flush()
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="SHADOW_EXECUTION_SIMULATED",
                object_type="ShadowOrder",
                object_id=_as_uuid(str(result["shadow_order_id"])),
                reason=(
                    f"reference={reference_price};status={result['status']};"
                    f"fee_bps={fee_bps};slippage_bps={slippage_bps}"
                ),
                correlation_id=intent.correlation_id,
                object_version=intent.version,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                account_id=campaign.account_id,
                environment=ExecutionEnvironment.SHADOW.value,
                generation=int(result["generation"]),
                rule_summary={
                    "proposal_id": str(proposal.proposal_id),
                    "campaign_id": str(campaign.campaign_id),
                    "intent_id": str(intent.intent_id),
                    "shadow_order_id": str(result["shadow_order_id"]),
                    "status": result["status"],
                    "reference_price": str(reference_price),
                    "fee_bps": str(fee_bps),
                    "slippage_bps": str(slippage_bps),
                    "venue_write_adapter_calls": 0,
                },
                now=now,
            )
            return result

    def record_shadow_order(
        self,
        intent_id: UUID,
        actor_id: UUID,
        execution_scope: str,
        owner_id: str,
        fencing_token: int,
        venue_order_id: str,
        *,
        now: datetime,
    ) -> UUID:
        """Record a synthetic SHADOW send; this method never connects to a venue."""

        with self.database.session_factory.begin() as session:
            intent = session.get(OrderIntent, intent_id, with_for_update=True)
            if intent is None or intent.status != OrderIntentStatus.READY.value:
                _reject("ORDER_INTENT_NOT_READY", "only a ready intent can be shadow-sent")
            campaign = session.get(Campaign, intent.campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "intent campaign is missing")
            self.facade._validate_sender(
                session,
                campaign.team_id,
                execution_scope,
                owner_id,
                fencing_token,
                now,
            )
            if campaign.environment != ExecutionEnvironment.SHADOW.value:
                _reject("SHADOW_SCOPE_REQUIRED", "synthetic order recording is SHADOW-only")
            expected_scope = _scope_key(campaign.environment, campaign.account_id, campaign.venue)
            if execution_scope != expected_scope:
                _reject("EXECUTION_SCOPE_MISMATCH", "sender scope does not match campaign scope")
            team = self.transactions._require_role(
                session,
                actor_id,
                "venue.record",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            self.transactions._require_team_environment(team, ExecutionEnvironment.SHADOW)
            if team.execution_mode_locked_at is not None:
                _reject(
                    "SHADOW_LEGACY_EXECUTION_RETIRED",
                    "mode-locked Teams must execute proposal intents through the "
                    "unified shadow ledger",
                )
            fact = VenueOrder(
                team_id=campaign.team_id,
                order_intent_id=intent_id,
                account_id=campaign.account_id,
                venue=campaign.venue,
                environment=campaign.environment,
                instrument_id=campaign.instrument_id,
                venue_order_id=venue_order_id,
                client_order_id=venue_order_id,
                side=intent.side,
                order_type="MARKET",
                reduce_only=intent.reduce_only,
                status=VenueOrderStatus.SENT.value,
                ordered_quantity=intent.quantity,
                filled_quantity=Decimal(0),
                observed_at=now,
                updated_at=now,
            )
            session.add(fact)
            previous = intent.status
            intent.status = OrderIntentStatus.SENT.value
            intent.updated_at = now
            intent.version += 1
            session.flush()
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="SHADOW_ORDER_RECORDED",
                object_type="VenueOrder",
                object_id=fact.venue_order_fact_id,
                reason=venue_order_id,
                correlation_id=intent.correlation_id,
                object_version=1,
                now=now,
            )
            INTENT_TRANSITIONS.labels(previous, intent.status).inc()
            return fact.venue_order_fact_id
