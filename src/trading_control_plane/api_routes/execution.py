from __future__ import annotations

from typing import cast

from trading_control_plane.api_core import (
    UUID,
    AccountEquityFactRequest,
    AddCandidateFacts,
    Any,
    AutoAddRequest,
    AutomaticExitRequest,
    BinanceTestnetActionRequest,
    BinanceTestnetOrder,
    BinanceTestnetProtectionRequest,
    CampaignTargetRequest,
    Decimal,
    DomainRejected,
    ExecutionEnvironment,
    FreqtradeEntryCommand,
    FreqtradeExitCommand,
    FreqtradeLiveActionRequest,
    FundingFactRequest,
    HyperliquidTestnetProtectionRequest,
    IntentKind,
    IntentReleaseRequest,
    IntentUnknownRequest,
    ManagedReductionRequest,
    OrderIntentRequest,
    OrderIntentStatus,
    PositionFactRequest,
    ProtectionFactRequest,
    Query,
    ReconciliationReasonRequest,
    ReconciliationRequest,
    ReductionIntentRequest,
    RiskTightenRequest,
    SenderLeaseRequest,
    SessionIdentity,
    ShadowFillRequest,
    ShadowSendRequest,
    ShadowSimulationRequest,
    TargetCandidate,
    TelegramBotGateway,
    __version__,
    _now,
    _perptape_runtime_status,
    _perptape_transport_status,
    datetime,
    freqtrade_pair,
    perptape_legacy_candidate_id,
    project_runtime_connections,
    timedelta,
)
from trading_control_plane.api_routes.context import ApiRouteContext


def register_execution_routes(context: ApiRouteContext) -> None:
    """Register execution routes against one application dependency context."""

    app = context.app
    current_perptape_candidate = context.require("current_perptape_candidate")
    current_perptape_candidates = context.require("current_perptape_candidates")
    identity_dependency = context.require("identity_dependency")
    notify_campaign = context.require("notify_campaign")
    queries = context.require("queries")
    rejected_hyperliquid_order = context.require("rejected_hyperliquid_order")
    rejected_testnet_order = context.require("rejected_testnet_order")
    require_binance_live = context.require("require_binance_live")
    require_binance_testnet = context.require("require_binance_testnet")
    require_capability = context.require("require_capability")
    require_freqtrade_live_enabled = context.require("require_freqtrade_live_enabled")
    require_freqtrade_live_worker = context.require("require_freqtrade_live_worker")
    require_hyperliquid_live = context.require("require_hyperliquid_live")
    require_hyperliquid_testnet = context.require("require_hyperliquid_testnet")
    resolved_binance_live = context.require("resolved_binance_live")
    resolved_binance_testnet = context.require("resolved_binance_testnet")
    resolved_freqtrade_workers = context.require("resolved_freqtrade_workers")
    resolved_hyperliquid = context.require("resolved_hyperliquid")
    resolved_hyperliquid_live = context.require("resolved_hyperliquid_live")
    resolved_hyperliquid_testnet = context.require("resolved_hyperliquid_testnet")
    resolved_notilt = context.require("resolved_notilt")
    resolved_settings = context.require("resolved_settings")
    resolved_telegram = context.require("resolved_telegram")
    service = context.require("service")
    unknown_hyperliquid_protection = context.require("unknown_hyperliquid_protection")
    unknown_testnet_protection = context.require("unknown_testnet_protection")

    @app.get("/api/campaigns")
    def campaigns(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_capability(identity, "operations.view")
        return {"data": queries().list_campaigns(identity.user_id), "as_of": _now().isoformat()}

    @app.get("/api/campaign-exceptions")
    def campaign_exceptions(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_capability(identity, "operations.view")
        now = _now()
        return {
            "data": queries().list_exceptions(identity.user_id, now=now),
            "as_of": now.isoformat(),
        }

    @app.get("/api/campaigns/{campaign_id}")
    def campaign_detail(
        campaign_id: UUID,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_capability(identity, "operations.view")
        return cast(dict[str, Any], queries().campaign_detail(identity.user_id, campaign_id))

    @app.get("/api/campaigns/{campaign_id}/add-candidates")
    def campaign_add_candidates(
        campaign_id: UUID,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        detail = queries().campaign_detail(identity.user_id, campaign_id)
        symbol = str(detail.get("instrument", {}).get("symbol", ""))
        source_candidate_id = None
        proposal = queries().proposal_detail(identity.user_id, UUID(str(detail["proposal_id"])))
        source_candidate_id = proposal.get("source_candidate_id")
        now = _now()
        candidates = [
            item
            for item in current_perptape_candidates(user_id=identity.user_id, now=now)
            if item.venue == detail["venue"]
            and item.symbol == symbol
            and item.direction.value == detail["direction"]
            and item.readiness == "READY"
            and source_candidate_id not in {item.candidate_id, perptape_legacy_candidate_id(item)}
        ]
        return {
            "source": "PERPTAPE",
            "source_contract_version": resolved_settings.perptape_contract_version,
            "as_of": now.isoformat(),
            "data": [item.to_dict() for item in candidates],
        }

    @app.post("/api/campaigns/{campaign_id}/auto-add")
    def create_auto_add_intent(
        campaign_id: UUID,
        payload: AutoAddRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        detail = queries().campaign_detail(identity.user_id, campaign_id)
        candidate = current_perptape_candidate(
            payload.candidate_id,
            user_id=identity.user_id,
            now=now,
        )
        created = service().create_order_intent(
            UUID(str(detail["authorization_id"])),
            identity.user_id,
            IntentKind.ADD,
            str(detail["account_id"]),
            str(detail["venue"]),
            UUID(str(detail["instrument_id"])),
            candidate.direction,
            payload.quantity,
            payload.idempotency_key,
            add_candidate=AddCandidateFacts(
                candidate_id=candidate.candidate_id,
                contract_version=candidate.source_contract_version,
                venue=candidate.venue,
                symbol=candidate.symbol,
                direction=candidate.direction,
                observed_at=candidate.observed_at,
                reference_price=candidate.reference_price,
                readiness=candidate.readiness,
                legacy_candidate_id=perptape_legacy_candidate_id(candidate),
            ),
            now=now,
        )
        notify_campaign(
            identity.user_id,
            created.campaign_id,
            "ADD_INTENT_READY",
            str(created.intent_id),
            "Perptape 加仓候选已通过冻结条件和最终风险校验。",
            str(detail["environment"]),
        )
        return {
            "campaign_id": str(created.campaign_id),
            "reservation_id": str(created.reservation_id),
            "intent_id": str(created.intent_id),
            "detail": queries().campaign_detail(identity.user_id, created.campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/managed-reductions")
    def create_managed_reduction(
        campaign_id: UUID,
        payload: ManagedReductionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        intent_id = service().create_reduction_intent(
            campaign_id,
            identity.user_id,
            payload.idempotency_key,
            candidates=(TargetCandidate(payload.target_quantity, payload.urgency, payload.reason),),
            limit_price=payload.limit_price,
            now=_now(),
        )
        detail = queries().campaign_detail(identity.user_id, campaign_id)
        notify_campaign(
            identity.user_id,
            campaign_id,
            "RISK_REDUCTION_READY",
            str(intent_id),
            "只减仓目标已就绪；完整目标数量仅在 Web 中展示。",  # noqa: RUF001
            str(detail["environment"]),
        )
        return {"intent_id": str(intent_id), "detail": detail}

    @app.post("/api/campaigns/{campaign_id}/automatic-exit")
    def evaluate_automatic_exit(
        campaign_id: UUID,
        payload: AutomaticExitRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        reason, intent_id = service().create_automatic_exit_intent(
            campaign_id,
            identity.user_id,
            payload.idempotency_key,
            limit_price=payload.limit_price,
            now=_now(),
        )
        detail = queries().campaign_detail(identity.user_id, campaign_id)
        if intent_id is not None:
            notify_campaign(
                identity.user_id,
                campaign_id,
                "AUTOMATIC_EXIT_READY",
                str(intent_id),
                "自动只减仓退出意图已就绪；请在 Web 中核对触发原因。",  # noqa: RUF001
                str(detail["environment"]),
            )
        return {
            "triggered": intent_id is not None,
            "reason": reason,
            "intent_id": None if intent_id is None else str(intent_id),
            "detail": detail,
        }

    @app.post("/api/campaigns/{campaign_id}/auto-add/disable")
    def disable_campaign_auto_add(
        campaign_id: UUID,
        payload: RiskTightenRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        allowed_adds = service().disable_campaign_auto_add(
            campaign_id,
            identity.user_id,
            payload.idempotency_key,
            reason=payload.reason,
            now=_now(),
        )
        detail = queries().campaign_detail(identity.user_id, campaign_id)
        notify_campaign(
            identity.user_id,
            campaign_id,
            "CAMPAIGN_AUTO_ADD_DISABLED",
            payload.idempotency_key,
            "此 Campaign 的后续 AddUnit 已关闭。",
            str(detail["environment"]),
        )
        return {"allowed_adds": allowed_adds, "detail": detail}

    @app.post("/api/operations/auto-add/disable")
    def disable_global_auto_add(
        payload: RiskTightenRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, str]:
        service().disable_global_auto_add(
            identity.user_id,
            payload.idempotency_key,
            reason=payload.reason,
            now=_now(),
        )
        return {"status": "DISABLED"}

    @app.post("/api/operations/pause-new-risk")
    def pause_new_risk(
        payload: RiskTightenRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, str]:
        state = service().pause_new_risk(
            identity.user_id,
            payload.idempotency_key,
            reason=payload.reason,
            now=_now(),
        )
        return {"system_state": state.value}

    @app.post("/api/authorizations/{authorization_id}/intents")
    def create_order_intent(
        authorization_id: UUID,
        payload: OrderIntentRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        created = service().create_order_intent(
            authorization_id,
            identity.user_id,
            IntentKind(payload.kind),
            payload.account_id,
            payload.venue,
            payload.instrument_id,
            payload.direction,
            payload.quantity,
            payload.idempotency_key,
            now=_now(),
        )
        return {
            "campaign_id": str(created.campaign_id),
            "reservation_id": str(created.reservation_id),
            "intent_id": str(created.intent_id),
            "detail": queries().campaign_detail(identity.user_id, created.campaign_id),
        }

    @app.post("/api/facts/positions")
    def record_position(
        payload: PositionFactRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, str]:
        position_id = service().record_position(
            payload.account_id,
            payload.venue,
            payload.instrument_id,
            payload.quantity,
            payload.average_entry_price,
            payload.mark_price,
            payload.known,
            identity.user_id,
            now=_now(),
        )
        return {"position_id": str(position_id), "environment": "SHADOW"}

    @app.post("/api/facts/account-equity")
    def record_account_equity(
        payload: AccountEquityFactRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, str]:
        fact_id = service().record_account_equity(
            payload.account_id,
            payload.venue,
            payload.equity,
            payload.available_balance,
            payload.currency,
            payload.known,
            identity.user_id,
            now=_now(),
        )
        return {"account_equity_id": str(fact_id), "environment": "SHADOW"}

    @app.post("/api/sender-leases")
    def acquire_sender(
        payload: SenderLeaseRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        token = service().acquire_sender(
            payload.execution_scope,
            payload.owner_id,
            identity.user_id,
            now,
            lease_duration=timedelta(seconds=payload.lease_seconds),
        )
        return {
            "execution_scope": payload.execution_scope,
            "owner_id": payload.owner_id,
            "fencing_token": token,
            "expires_at": (now + timedelta(seconds=payload.lease_seconds)).isoformat(),
        }

    @app.post("/api/intents/{intent_id}/shadow-send")
    def shadow_send(
        intent_id: UUID,
        payload: ShadowSendRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        fact_id = service().record_shadow_order(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            payload.venue_order_id,
            now=_now(),
        )
        return {
            "venue_order_fact_id": str(fact_id),
            "environment": "SHADOW",
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/binance-testnet/send")
    def send_binance_testnet_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_binance_testnet()
        now = _now()
        command = service().prepare_binance_testnet_send(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        try:
            result = resolved_binance_testnet.ensure_order(command, now=now)
        except DomainRejected as exc:
            if exc.code == "BINANCE_TESTNET_REJECTED":
                service().record_binance_testnet_order(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    rejected_testnet_order(command, now),
                    now=now,
                )
            elif exc.code != "BINANCE_TESTNET_UNAVAILABLE":
                service().record_binance_testnet_unknown(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    exc.code,
                    now=now,
                )
            raise
        fact_id = service().record_binance_testnet_order(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            command,
            result,
            now=now,
        )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "venue_order_fact_id": str(fact_id),
            "client_order_id": command.client_order_id,
            "environment": "TESTNET",
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/binance-testnet/cancel")
    def cancel_binance_testnet_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_binance_testnet()
        now = _now()
        command = service().prepare_binance_testnet_cancel(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        try:
            result = resolved_binance_testnet.cancel_order(command, now=now)
        except DomainRejected as exc:
            if exc.code != "BINANCE_TESTNET_UNAVAILABLE":
                service().record_binance_testnet_unknown(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    exc.code,
                    now=now,
                )
            raise
        if result is None:
            service().record_binance_testnet_unknown(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                "BINANCE_TESTNET_ORDER_NOT_FOUND",
                now=now,
            )
        else:
            service().record_binance_testnet_order(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                result,
                now=now,
            )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "environment": "TESTNET",
            "confirmed": result is not None,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/binance-testnet/recover")
    def recover_binance_testnet_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_binance_testnet()
        now = _now()
        command = service().prepare_binance_testnet_recovery(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        result = resolved_binance_testnet.recover_order(command, now=now)
        if result is not None:
            service().record_binance_testnet_order(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                result,
                now=now,
            )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "environment": "TESTNET",
            "recovered": result is not None,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/binance-testnet/protection")
    def create_binance_testnet_protection(
        campaign_id: UUID,
        payload: BinanceTestnetProtectionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_binance_testnet()
        now = _now()
        command = service().prepare_binance_testnet_protection(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            payload.trigger_price,
            now=now,
        )
        try:
            result = resolved_binance_testnet.ensure_protection(command, now=now)
        except DomainRejected as exc:
            if exc.code != "BINANCE_TESTNET_UNAVAILABLE":
                service().record_binance_testnet_protection(
                    campaign_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    unknown_testnet_protection(command, now),
                    now=now,
                )
            raise
        protection_id = service().record_binance_testnet_protection(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            command,
            result,
            now=now,
        )
        return {
            "protection_id": str(protection_id),
            "client_order_id": command.client_order_id,
            "environment": "TESTNET",
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/hyperliquid-testnet/send")
    def send_hyperliquid_testnet_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_hyperliquid_testnet()
        now = _now()
        command = service().prepare_hyperliquid_testnet_send(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        try:
            result = resolved_hyperliquid_testnet.ensure_order(command, now=now)
        except DomainRejected as exc:
            if exc.code == "HYPERLIQUID_TESTNET_REJECTED":
                service().record_hyperliquid_testnet_order(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    rejected_hyperliquid_order(command, now),
                    now=now,
                )
            elif exc.code not in {
                "HYPERLIQUID_TESTNET_UNAVAILABLE",
                "HYPERLIQUID_TESTNET_SIGNER_INVALID",
                "HYPERLIQUID_ORDER_PRECISION_INVALID",
            }:
                service().record_hyperliquid_testnet_unknown(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    exc.code,
                    now=now,
                )
            raise
        fact_id = service().record_hyperliquid_testnet_order(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            command,
            result,
            now=now,
        )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "venue_order_fact_id": str(fact_id),
            "client_order_id": command.client_order_id,
            "environment": "TESTNET",
            "domain": "CORE",
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/hyperliquid-testnet/cancel")
    def cancel_hyperliquid_testnet_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_hyperliquid_testnet()
        now = _now()
        command = service().prepare_hyperliquid_testnet_cancel(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        try:
            result = resolved_hyperliquid_testnet.cancel_order(command, now=now)
        except DomainRejected as exc:
            if exc.code not in {
                "HYPERLIQUID_TESTNET_UNAVAILABLE",
                "HYPERLIQUID_TESTNET_SIGNER_INVALID",
            }:
                service().record_hyperliquid_testnet_unknown(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    exc.code,
                    now=now,
                )
            raise
        if result is None:
            service().record_hyperliquid_testnet_unknown(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                "HYPERLIQUID_TESTNET_ORDER_NOT_FOUND",
                now=now,
            )
        else:
            service().record_hyperliquid_testnet_order(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                result,
                now=now,
            )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "environment": "TESTNET",
            "domain": "CORE",
            "confirmed": result is not None,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/hyperliquid-testnet/recover")
    def recover_hyperliquid_testnet_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_hyperliquid_testnet()
        now = _now()
        command = service().prepare_hyperliquid_testnet_recovery(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        result = resolved_hyperliquid_testnet.recover_order(command, now=now)
        if result is not None:
            service().record_hyperliquid_testnet_order(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                result,
                now=now,
            )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "environment": "TESTNET",
            "domain": "CORE",
            "recovered": result is not None,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/hyperliquid-testnet/protection")
    def create_hyperliquid_testnet_protection(
        campaign_id: UUID,
        payload: HyperliquidTestnetProtectionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_hyperliquid_testnet()
        now = _now()
        command = service().prepare_hyperliquid_testnet_protection(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            payload.trigger_price,
            payload.limit_price,
            now=now,
        )
        try:
            result = resolved_hyperliquid_testnet.ensure_protection(command, now=now)
        except DomainRejected as exc:
            if exc.code not in {
                "HYPERLIQUID_TESTNET_UNAVAILABLE",
                "HYPERLIQUID_TESTNET_SIGNER_INVALID",
                "HYPERLIQUID_ORDER_PRECISION_INVALID",
            }:
                service().record_hyperliquid_testnet_protection(
                    campaign_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    unknown_hyperliquid_protection(command, now),
                    now=now,
                )
            raise
        protection_id = service().record_hyperliquid_testnet_protection(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            command,
            result,
            now=now,
        )
        return {
            "protection_id": str(protection_id),
            "client_order_id": command.client_order_id,
            "environment": "TESTNET",
            "domain": "CORE",
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/binance/live/send")
    def send_binance_live_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_binance_live()
        now = _now()
        command = service().prepare_binance_live_send(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        try:
            result = resolved_binance_live.ensure_order(command, now=now)
        except DomainRejected as exc:
            if exc.code == "BINANCE_LIVE_REJECTED":
                service().record_binance_live_order(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    rejected_testnet_order(command, now),
                    now=now,
                )
            elif exc.code == "BINANCE_LIVE_OUTCOME_UNKNOWN":
                service().record_binance_live_unknown(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    exc.code,
                    now=now,
                )
            raise
        fact_id = service().record_binance_live_order(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            command,
            result,
            now=now,
        )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "venue_order_fact_id": str(fact_id),
            "client_order_id": command.client_order_id,
            "environment": "LIVE",
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/binance/live/cancel")
    def cancel_binance_live_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_binance_live()
        now = _now()
        command = service().prepare_binance_live_cancel(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        try:
            result = resolved_binance_live.cancel_order(command, now=now)
        except DomainRejected as exc:
            if exc.code == "BINANCE_LIVE_OUTCOME_UNKNOWN":
                service().record_binance_live_unknown(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    exc.code,
                    now=now,
                )
            raise
        if result is None:
            service().record_binance_live_unknown(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                "BINANCE_LIVE_ORDER_NOT_FOUND",
                now=now,
            )
        else:
            service().record_binance_live_order(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                result,
                now=now,
            )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "environment": "LIVE",
            "confirmed": result is not None,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/binance/live/recover")
    def recover_binance_live_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_binance_live()
        now = _now()
        command = service().prepare_binance_live_recovery(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        result = resolved_binance_live.recover_order(command, now=now)
        if result is not None:
            service().record_binance_live_order(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                result,
                now=now,
            )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "environment": "LIVE",
            "recovered": result is not None,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/binance/live/protection")
    def create_binance_live_protection(
        campaign_id: UUID,
        payload: BinanceTestnetProtectionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_binance_live()
        now = _now()
        command = service().prepare_binance_live_protection(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            payload.trigger_price,
            now=now,
        )
        try:
            result = resolved_binance_live.ensure_protection(command, now=now)
        except DomainRejected as exc:
            if exc.code == "BINANCE_LIVE_OUTCOME_UNKNOWN":
                unknown = BinanceTestnetOrder(
                    order_id=f"UNKNOWN:{command.client_order_id}",
                    client_order_id=command.client_order_id,
                    status="UNKNOWN",
                    side=command.side,
                    order_type="STOP_MARKET",
                    ordered_quantity=command.quantity or Decimal(0),
                    filled_quantity=Decimal(0),
                    stop_price=command.trigger_price,
                    reduce_only=True,
                    close_position=False,
                    observed_at=now,
                )
                service().record_binance_live_protection(
                    campaign_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    unknown,
                    now=now,
                )
            raise
        protection_id = service().record_binance_live_protection(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            command,
            result,
            now=now,
        )
        return {
            "protection_id": str(protection_id),
            "client_order_id": command.client_order_id,
            "environment": "LIVE",
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/binance/live/protection/cancel")
    def cancel_binance_live_protection(
        campaign_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_binance_live()
        now = _now()
        command = service().prepare_live_protection_cancel(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            venue="BINANCE",
            now=now,
        )
        result = resolved_binance_live.cancel_protection(command, now=now)
        service().record_live_protection_cancel(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            command,
            result,
            venue="BINANCE",
            now=now,
        )
        return {
            "environment": "LIVE",
            "confirmed": True,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/hyperliquid/live/send")
    def send_hyperliquid_live_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_hyperliquid_live()
        now = _now()
        command = service().prepare_hyperliquid_live_send(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        try:
            result = resolved_hyperliquid_live.ensure_order(command, now=now)
        except DomainRejected as exc:
            if exc.code == "HYPERLIQUID_LIVE_REJECTED":
                service().record_hyperliquid_live_order(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    rejected_hyperliquid_order(command, now),
                    now=now,
                )
            elif exc.code == "HYPERLIQUID_LIVE_OUTCOME_UNKNOWN":
                service().record_hyperliquid_live_unknown(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    exc.code,
                    now=now,
                )
            raise
        fact_id = service().record_hyperliquid_live_order(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            command,
            result,
            now=now,
        )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "venue_order_fact_id": str(fact_id),
            "client_order_id": command.client_order_id,
            "environment": "LIVE",
            "domain": "CORE",
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/hyperliquid/live/cancel")
    def cancel_hyperliquid_live_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_hyperliquid_live()
        now = _now()
        command = service().prepare_hyperliquid_live_cancel(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        try:
            result = resolved_hyperliquid_live.cancel_order(command, now=now)
        except DomainRejected as exc:
            if exc.code == "HYPERLIQUID_LIVE_OUTCOME_UNKNOWN":
                service().record_hyperliquid_live_unknown(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    exc.code,
                    now=now,
                )
            raise
        if result is None:
            service().record_hyperliquid_live_unknown(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                "HYPERLIQUID_LIVE_ORDER_NOT_FOUND",
                now=now,
            )
        else:
            service().record_hyperliquid_live_order(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                result,
                now=now,
            )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "environment": "LIVE",
            "domain": "CORE",
            "confirmed": result is not None,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/hyperliquid/live/recover")
    def recover_hyperliquid_live_order(
        intent_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_hyperliquid_live()
        now = _now()
        command = service().prepare_hyperliquid_live_recovery(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            now=now,
        )
        result = resolved_hyperliquid_live.recover_order(command, now=now)
        if result is not None:
            service().record_hyperliquid_live_order(
                intent_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                command,
                result,
                now=now,
            )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        return {
            "environment": "LIVE",
            "domain": "CORE",
            "recovered": result is not None,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/hyperliquid/live/protection")
    def create_hyperliquid_live_protection(
        campaign_id: UUID,
        payload: HyperliquidTestnetProtectionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_hyperliquid_live()
        now = _now()
        command = service().prepare_hyperliquid_live_protection(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            payload.trigger_price,
            payload.limit_price,
            now=now,
        )
        try:
            result = resolved_hyperliquid_live.ensure_protection(command, now=now)
        except DomainRejected as exc:
            if exc.code == "HYPERLIQUID_LIVE_OUTCOME_UNKNOWN":
                service().record_hyperliquid_live_protection(
                    campaign_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    unknown_hyperliquid_protection(command, now),
                    now=now,
                )
            raise
        protection_id = service().record_hyperliquid_live_protection(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            command,
            result,
            now=now,
        )
        return {
            "protection_id": str(protection_id),
            "client_order_id": command.client_order_id,
            "environment": "LIVE",
            "domain": "CORE",
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/hyperliquid/live/protection/cancel")
    def cancel_hyperliquid_live_protection(
        campaign_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_hyperliquid_live()
        now = _now()
        command = service().prepare_live_protection_cancel(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            venue="HYPERLIQUID",
            now=now,
        )
        result = resolved_hyperliquid_live.cancel_protection(command, now=now)
        service().record_live_protection_cancel(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            command,
            result,
            venue="HYPERLIQUID",
            now=now,
        )
        return {
            "environment": "LIVE",
            "domain": "CORE",
            "confirmed": True,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/fills")
    def record_fill(
        intent_id: UUID,
        payload: ShadowFillRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        fact_id = service().record_fill(
            intent_id,
            identity.user_id,
            payload.venue_fill_id,
            payload.side,
            payload.quantity,
            payload.price,
            payload.fee,
            payload.fee_currency,
            payload.slippage_cost,
            now=_now(),
        )
        notify_campaign(
            identity.user_id,
            campaign_id,
            "SHADOW_FILL_RECORDED",
            payload.venue_fill_id,
            "SHADOW 成交事实已记录；没有向交易场所发送订单。",  # noqa: RUF001
        )
        return {
            "venue_fill_fact_id": str(fact_id),
            "environment": "SHADOW",
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/shadow-simulations")
    def simulate_shadow_execution(
        intent_id: UUID,
        payload: ShadowSimulationRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        result = service().simulate_shadow_execution(
            intent_id=intent_id,
            actor_id=identity.user_id,
            expected_version=payload.expected_version,
            reference_price=payload.reference_price,
            fee_bps=payload.fee_bps,
            slippage_bps=payload.slippage_bps,
            idempotency_key=payload.idempotency_key,
            now=_now(),
        )
        return {
            "result": result,
            "detail": queries().campaign_detail(
                identity.user_id,
                UUID(str(result["campaign_id"])),
            ),
        }

    @app.post("/api/intents/{intent_id}/unknown")
    def mark_intent_unknown(
        intent_id: UUID,
        payload: IntentUnknownRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        service().mark_intent_unknown(
            intent_id,
            identity.user_id,
            payload.reason,
            required_environment=ExecutionEnvironment.SHADOW,
            now=_now(),
        )
        notify_campaign(
            identity.user_id,
            campaign_id,
            "ORDER_INTENT_UNKNOWN",
            str(intent_id),
            "订单结果为 UNKNOWN；风险占用保持，自动重试已阻止。",  # noqa: RUF001
        )
        return cast(dict[str, Any], queries().campaign_detail(identity.user_id, campaign_id))

    @app.post("/api/intents/{intent_id}/release")
    def release_intent(
        intent_id: UUID,
        payload: IntentReleaseRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        service().release_unfilled_intent(
            intent_id,
            identity.user_id,
            OrderIntentStatus(payload.terminal_status),
            payload.reason,
            required_environment=ExecutionEnvironment.SHADOW,
            now=_now(),
        )
        return cast(dict[str, Any], queries().campaign_detail(identity.user_id, campaign_id))

    @app.post("/api/campaigns/{campaign_id}/protection")
    def record_protection(
        campaign_id: UUID,
        payload: ProtectionFactRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        queries().campaign_detail(identity.user_id, campaign_id)
        protection_id = service().record_protection(
            payload.position_id,
            payload.venue_order_id,
            payload.quantity,
            payload.trigger_price,
            payload.fully_covered,
            identity.user_id,
            campaign_id=campaign_id,
            required_environment=ExecutionEnvironment.SHADOW,
            known=payload.known,
            now=_now(),
        )
        event_type = (
            "PROTECTION_ACTIVE"
            if payload.known and payload.fully_covered
            else "PROTECTION_EXCEPTION"
        )
        notify_campaign(
            identity.user_id,
            campaign_id,
            event_type,
            f"{payload.position_id}:{payload.venue_order_id}",
            "SHADOW 仓位保护事实已记录。",
        )
        return {
            "protection_id": str(protection_id),
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/funding")
    def record_funding(
        campaign_id: UUID,
        payload: FundingFactRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        payment_id = service().record_funding(
            campaign_id,
            payload.venue,
            payload.venue_payment_id,
            payload.amount,
            payload.currency,
            identity.user_id,
            required_environment=ExecutionEnvironment.SHADOW,
            now=_now(),
        )
        return {
            "funding_payment_id": str(payment_id),
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/target")
    def update_campaign_target(
        campaign_id: UUID,
        payload: CampaignTargetRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        decision = service().update_campaign_target(
            campaign_id,
            identity.user_id,
            tuple(
                TargetCandidate(item.target_quantity, item.urgency, item.reason)
                for item in payload.candidates
            ),
            now=_now(),
        )
        return {
            "decision": {
                "target_quantity": str(decision.target_quantity),
                "urgency": decision.urgency.value,
                "reasons": list(decision.reasons),
            },
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/reduction-intents")
    def create_reduction_intent(
        campaign_id: UUID,
        payload: ReductionIntentRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        intent_id = service().create_reduction_intent(
            campaign_id,
            identity.user_id,
            payload.idempotency_key,
            limit_price=payload.limit_price,
            now=_now(),
        )
        return {
            "intent_id": str(intent_id),
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/reconcile")
    def reconcile_campaign(
        campaign_id: UUID,
        payload: ReconciliationRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        reconciliation_id = service().reconcile_campaign(
            campaign_id, payload.execution_scope, identity.user_id, now=_now()
        )
        detail = queries().campaign_detail(identity.user_id, campaign_id)
        reconciliation = detail["reconciliation"]
        if reconciliation is not None and reconciliation["status"] != "MATCH":
            notify_campaign(
                identity.user_id,
                campaign_id,
                "RECONCILIATION_EXCEPTION",
                str(reconciliation_id),
                f"对账需要人工关注；当前状态为 {reconciliation['status']}。",  # noqa: RUF001
            )
        return {"reconciliation_id": str(reconciliation_id), "detail": detail}

    @app.post("/api/campaigns/{campaign_id}/freqtrade/emergency-exit/recover")
    def recover_freqtrade_emergency_exit(
        campaign_id: UUID,
        payload: ReconciliationReasonRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        intent_id = service().recover_freqtrade_emergency_exit(
            campaign_id,
            identity.user_id,
            payload.reason,
            now=_now(),
        )
        return {
            "intent_id": str(intent_id),
            "sent_order": False,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/reconciliations/{reconciliation_id}/manual")
    def require_manual_reconciliation(
        reconciliation_id: UUID,
        payload: ReconciliationReasonRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, str]:
        result = service().require_manual_reconciliation(
            reconciliation_id, identity.user_id, payload.reason, now=_now()
        )
        return {"reconciliation_id": str(result), "status": "MANUAL_REQUIRED"}

    @app.post("/api/reconciliations/{reconciliation_id}/resolve")
    def resolve_reconciliation(
        reconciliation_id: UUID,
        payload: ReconciliationReasonRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, str]:
        result = service().resolve_reconciliation(
            reconciliation_id, identity.user_id, payload.reason, now=_now()
        )
        return {"reconciliation_id": str(result), "status": "RESOLVED"}

    @app.post("/api/campaigns/{campaign_id}/pnl")
    def refresh_campaign_pnl(
        campaign_id: UUID,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        pnl = service().refresh_campaign_pnl(campaign_id, identity.user_id, now=_now())
        return {
            "pnl": {
                "realized_pnl": str(pnl.realized_pnl),
                "unrealized_pnl": str(pnl.unrealized_pnl),
                "fees": str(pnl.fees),
                "funding": str(pnl.funding),
                "slippage": str(pnl.slippage),
                "total_pnl": str(pnl.total_pnl),
                "open_quantity": str(pnl.open_quantity),
                "average_entry_price": str(pnl.average_entry_price),
            },
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/close")
    def close_campaign(
        campaign_id: UUID,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        service().close_campaign(campaign_id, identity.user_id, now=_now())
        notify_campaign(
            identity.user_id,
            campaign_id,
            "CAMPAIGN_CLOSED",
            str(campaign_id),
            "仓位归零且对账 MATCH，SHADOW Campaign 已关闭。",  # noqa: RUF001
        )
        return cast(dict[str, Any], queries().campaign_detail(identity.user_id, campaign_id))

    @app.get("/api/results")
    def actual_results(
        environment: str = Query(default="SHADOW", pattern="^(SHADOW|TESTNET|LIVE)$"),
        source: str | None = Query(default=None, pattern="^(SYSTEM|MANUAL)$"),
        source_type: str | None = Query(default=None, min_length=1, max_length=120),
        source_candidate_id: str | None = Query(default=None, min_length=1, max_length=160),
        source_version: str | None = Query(default=None, min_length=1, max_length=120),
        strategy_id: str | None = Query(default=None, min_length=1, max_length=120),
        strategy_version: str | None = Query(default=None, min_length=1, max_length=120),
        signal_source_mode: str | None = Query(
            default=None, pattern="^(PERPTAPE|WEBHOOK|MANUAL|SYSTEM)$"
        ),
        signal_provider: str | None = Query(default=None, pattern="^(TRADINGVIEW|MODEL|PERPTAPE)$"),
        venue: str | None = Query(default=None, min_length=1, max_length=64),
        account_id: str | None = Query(default=None, min_length=1, max_length=120),
        instrument_id: UUID | None = None,
        direction: str | None = Query(default=None, pattern="^(LONG|SHORT)$"),
        risk_tier: str | None = Query(default=None, pattern="^(LOW|MEDIUM|HIGH)$"),
        campaign_id: UUID | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_capability(identity, "results.view")
        return {
            "data": queries().actual_results(
                identity.user_id,
                environment,
                source=source,
                source_type=source_type,
                source_candidate_id=source_candidate_id,
                source_version=source_version,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                signal_source_mode=signal_source_mode,
                signal_provider=signal_provider,
                venue=venue,
                account_id=account_id,
                instrument_id=instrument_id,
                direction=direction,
                risk_tier=risk_tier,
                campaign_id=campaign_id,
                from_time=from_time,
                to_time=to_time,
            ),
            "as_of": _now().isoformat(),
        }

    @app.get("/api/audit")
    def audit_timeline(
        environment: str = Query(default="SHADOW", pattern="^(SHADOW|TESTNET|LIVE)$"),
        limit: int = Query(default=200, ge=1, le=500),
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_capability(identity, "results.view")
        return {
            "environment": environment,
            "data": queries().audit_timeline(identity.user_id, environment, limit=limit),
            "as_of": _now().isoformat(),
        }

    @app.get("/api/runtime/status")
    def runtime_status(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_capability(identity, "system.view")
        snapshot = queries().runtime_snapshot(identity.user_id)
        perptape_feed = snapshot["perptape_feed"]
        database_binding_counts = snapshot.pop("runtime_binding_counts")
        freqtrade_binding_counts = snapshot.pop("freqtrade_binding_counts")
        signal_source = service().signal_source_status(identity.user_id)["source"]
        database_perptape_configured = bool(
            signal_source
            and signal_source["enabled"]
            and signal_source["mode"] == "PERPTAPE"
            and signal_source["credential"]["state"] == "CONFIGURED"
        )
        connection_states = project_runtime_connections(
            resolved_settings,
            snapshot["source_health"],
            database_binding_counts=database_binding_counts,
            database_perptape_configured=database_perptape_configured,
        )
        perptape_configured = database_perptape_configured or bool(
            resolved_settings.perptape_api_key
        )
        perptape_status = _perptape_runtime_status(
            resolved_settings,
            perptape_feed,
            now=_now(),
            configured=perptape_configured,
        )
        perptape_transport = _perptape_transport_status(
            resolved_settings,
            snapshot["source_health"],
            now=_now(),
        )
        telegram_polling = (
            resolved_telegram.polling_health()
            if isinstance(resolved_telegram, TelegramBotGateway)
            else {
                "state": "DISABLED",
                "running": False,
                "last_success_at": None,
                "last_error_at": None,
                "last_error_code": None,
                "consecutive_failures": 0,
            }
        )
        snapshot.update(
            {
                "application_version": __version__,
                "runtime_environment": resolved_settings.environment,
                "process_model": (
                    "FastAPI plus independent read-only sync worker and PostgreSQL"
                    if resolved_settings.runtime_sync_enabled
                    else "one FastAPI process plus PostgreSQL"
                ),
                "connections": connection_states,
                "external_boundaries": {
                    "execution": {
                        "backend": resolved_settings.execution_backend,
                        "workers_enabled": resolved_settings.freqtrade_workers_enabled,
                        "worker_count": sum(freqtrade_binding_counts.values()),
                        "account_binding_counts": freqtrade_binding_counts,
                        "binding_source": "DATABASE_ACCOUNT_ENVELOPE",
                        "legacy_unbound_default_count": len(resolved_freqtrade_workers),
                        "venues": ["BINANCE", "HYPERLIQUID", "OKX", "BYBIT"],
                        "direct_venue_send": False,
                        "live_order_send": resolved_settings.freqtrade_live_order_send_enabled,
                    },
                    "runtime_sync": {
                        "enabled": resolved_settings.runtime_sync_enabled,
                        "interval_seconds": resolved_settings.runtime_sync_interval_seconds,
                        "binance_target_configured": bool(
                            database_binding_counts.get("BINANCE")
                            or resolved_settings.runtime_binance_account_id
                        ),
                        "hyperliquid_target_configured": bool(
                            database_binding_counts.get("HYPERLIQUID")
                            or resolved_settings.runtime_hyperliquid_account_id
                        ),
                        "database_binding_counts": database_binding_counts,
                        "order_send_supported": False,
                        "capital_broadcast_supported": False,
                    },
                    "perptape": {
                        "configured": perptape_configured,
                        "mode": "READ_ONLY",
                        "status": perptape_status,
                        "contract_version": resolved_settings.perptape_contract_version,
                        "feed_available": perptape_feed["available"],
                        "candidate_count": perptape_feed["candidate_count"],
                        "last_fetched_at": perptape_feed["fetched_at"],
                        "last_generated_at": perptape_feed["generated_at"],
                        "transport": perptape_transport,
                    },
                    "binance_read_only": {
                        "enabled": resolved_settings.binance_read_only_enabled,
                        "configured": bool(
                            resolved_settings.binance_api_key
                            and resolved_settings.binance_api_secret
                        ),
                        "fact_environment": resolved_settings.binance_fact_environment,
                    },
                    "binance_testnet_send": {
                        "enabled": resolved_settings.binance_testnet_order_send_enabled,
                        "configured": bool(
                            resolved_settings.binance_testnet_api_key
                            and resolved_settings.binance_testnet_api_secret
                        ),
                    },
                    "hyperliquid_read_only": {
                        "enabled": resolved_settings.hyperliquid_read_only_enabled,
                        "configured": resolved_hyperliquid.configured,
                        "account_scope": resolved_settings.hyperliquid_account_scope,
                        "fact_environment": resolved_settings.hyperliquid_fact_environment,
                    },
                    "hyperliquid_testnet_send": {
                        "enabled": resolved_settings.hyperliquid_testnet_order_send_enabled,
                        "signer_injected": resolved_hyperliquid_testnet.configured,
                        "account_scope": resolved_settings.hyperliquid_account_scope,
                    },
                    "notilt": {
                        "mode": "OFFICIAL_SDK_UNSIGNED_HANDOFF",
                        "enabled": resolved_settings.notilt_enabled,
                        "gateway_available": resolved_notilt.available,
                        "configured_chains": sorted(resolved_settings.notilt_vaults),
                        "receipt_verification": True,
                        "min_confirmations": resolved_settings.notilt_min_confirmations,
                        "credential_custody": "EXTERNAL_WALLET",
                        "broadcast_supported": False,
                    },
                    "capital_transfer": {
                        "mode": "MOCK_OR_NOTILT_UNSIGNED_HANDOFF",
                        "real_configured": (
                            resolved_settings.notilt_enabled
                            and bool(resolved_settings.notilt_vaults)
                        ),
                    },
                    "telegram": {
                        "mode": (
                            "BOT_API_LONG_POLLING"
                            if isinstance(resolved_telegram, TelegramBotGateway)
                            else "MOCK_ONLY"
                        ),
                        "enabled": resolved_settings.telegram_enabled,
                        "network_configured": bool(resolved_settings.telegram_bot_token),
                        "private_chat_only": True,
                        "polling": telegram_polling,
                    },
                },
            }
        )
        return {"data": snapshot, "as_of": _now().isoformat()}

    @app.get("/api/execution/freqtrade/status")
    def freqtrade_worker_status(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        require_capability(identity, "system.view")
        workers: list[dict[str, Any]] = []
        for worker in (
            item for item in resolved_freqtrade_workers if item.spec.exchange_account_id is None
        ):
            workers.append(
                {
                    "name": worker.spec.name,
                    "venue": worker.spec.venue,
                    "backend": "FREQTRADE",
                    "status": (
                        "DISABLED" if not resolved_settings.freqtrade_workers_enabled else "UNBOUND"
                    ),
                    "reason_code": (
                        "FREQTRADE_WORKERS_DISABLED"
                        if not resolved_settings.freqtrade_workers_enabled
                        else "ACCOUNT_BINDING_REQUIRED"
                    ),
                    "scope_status": "UNBOUND_LEGACY_DEFAULT",
                    "hip3_dexes": list(worker.spec.hip3_dexes),
                    "order_send": False,
                }
            )
        registry = queries().exchange_accounts(identity.user_id)
        account_bindings = [
            {
                "exchange_account_id": item["exchange_account_id"],
                "team_id": item["team_id"],
                "account_id": item["account_id"],
                "venue": item["venue"],
                **item["execution_worker"],
            }
            for item in registry["data"]
            if item["execution_worker"]["supported"]
        ]
        return {
            "backend": resolved_settings.execution_backend,
            "workers_enabled": resolved_settings.freqtrade_workers_enabled,
            "direct_venue_send": False,
            "live_order_send": resolved_settings.freqtrade_live_order_send_enabled,
            "workers": workers,
            "account_bindings": account_bindings,
            "as_of": _now().isoformat(),
        }

    @app.post("/api/intents/{intent_id}/freqtrade/live/send")
    def send_freqtrade_live_order(
        intent_id: UUID,
        payload: FreqtradeLiveActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        parts = payload.execution_scope.split(":")
        if len(parts) != 3 or parts[0] != ExecutionEnvironment.LIVE.value:
            raise DomainRejected(
                "FREQTRADE_LIVE_SCOPE_REQUIRED",
                "Freqtrade LIVE sender requires an explicit LIVE scope",
            )
        require_freqtrade_live_enabled()
        now = _now()
        execution_service = service()
        binding = execution_service.freqtrade_live_worker_binding(
            actor_id=identity.user_id,
            execution_scope=payload.execution_scope,
            owner_id=payload.owner_id,
            fencing_token=payload.fencing_token,
            now=now,
        )
        worker = require_freqtrade_live_worker(binding)
        command = execution_service.prepare_freqtrade_live_order(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            hip3_dexes=binding.hip3_dexes,
            leverage=resolved_settings.freqtrade_live_leverage,
            now=now,
        )
        worker.probe(expected_mode="LIVE", required_pair=command.pair)
        execution_service.validate_freqtrade_worker_binding(binding)
        external_trade_id = None
        if isinstance(command, FreqtradeExitCommand):
            external_trade_id = execution_service.freqtrade_dispatch_external_id(
                intent_id,
                actor_id=identity.user_id,
                execution_scope=payload.execution_scope,
            )
            if external_trade_id is None:
                current = worker.find_open_trade(pair=command.pair)
                if current is None:
                    raise DomainRejected(
                        "FREQTRADE_POSITION_NOT_FOUND",
                        "Freqtrade has no unique open trade for the controlled exit",
                    )
                if current.amount > command.max_quantity:
                    raise DomainRejected(
                        "FREQTRADE_ORDER_IDENTITY_CONFLICT",
                        "Freqtrade open amount exceeds the frozen exit boundary",
                    )
                external_trade_id = current.trade_id
        dispatch = execution_service.start_freqtrade_live_dispatch(
            intent_id,
            actor_id=identity.user_id,
            execution_scope=payload.execution_scope,
            owner_id=payload.owner_id,
            fencing_token=payload.fencing_token,
            binding=binding,
            command=command,
            external_trade_id=external_trade_id,
            idempotency_key=payload.idempotency_key,
            now=_now(),
        )
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        if dispatch.mode == "COMPLETED":
            detail = queries().campaign_detail(identity.user_id, campaign_id)
            completed_intent = next(
                (item for item in detail["intents"] if item["intent_id"] == str(intent_id)),
                None,
            )
            completed_order = None if completed_intent is None else completed_intent.get("order")
            completed_protection = detail.get("protection")
            return {
                "venue_order_fact_id": (
                    None if completed_order is None else completed_order["venue_order_fact_id"]
                ),
                "protection_id": (
                    None if completed_protection is None else completed_protection["protection_id"]
                ),
                "backend": "FREQTRADE",
                "environment": "LIVE",
                "worker": worker.spec.name,
                "trade_id": dispatch.external_trade_id,
                "pair": command.pair,
                "is_open": isinstance(command, FreqtradeEntryCommand),
                "replayed": True,
                "detail": detail,
            }
        try:
            execution_service.validate_freqtrade_worker_binding(binding)
            if isinstance(command, FreqtradeEntryCommand):
                trade = (
                    worker.force_enter(command)
                    if dispatch.mode == "SEND"
                    else worker.recover_entry(command)
                )
            else:
                assert isinstance(command, FreqtradeExitCommand)
                assert dispatch.external_trade_id is not None
                trade = (
                    worker.force_exit(dispatch.external_trade_id, pair=command.pair)
                    if dispatch.mode == "SEND"
                    else worker.recover_exit(dispatch.external_trade_id, pair=command.pair)
                )
        except DomainRejected as exc:
            if exc.code in {
                "FREQTRADE_LIVE_OUTCOME_UNKNOWN",
                "FREQTRADE_PROTECTION_UNCONFIRMED",
            }:
                execution_service.record_freqtrade_live_unknown(
                    intent_id,
                    identity.user_id,
                    payload.execution_scope,
                    payload.owner_id,
                    payload.fencing_token,
                    command,
                    exc.code,
                    now=_now(),
                )
            raise
        fact_id = execution_service.record_freqtrade_live_order(
            intent_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            command,
            trade,
            now=_now(),
        )
        protection_id = None
        if isinstance(command, FreqtradeEntryCommand):
            protection_id = execution_service.record_freqtrade_live_protection(
                campaign_id,
                identity.user_id,
                payload.execution_scope,
                payload.owner_id,
                payload.fencing_token,
                trade,
                now=_now(),
            )
        return {
            "venue_order_fact_id": str(fact_id),
            "protection_id": None if protection_id is None else str(protection_id),
            "backend": "FREQTRADE",
            "environment": "LIVE",
            "worker": worker.spec.name,
            "trade_id": trade.trade_id,
            "pair": trade.pair,
            "is_open": trade.is_open,
            "replayed": dispatch.mode != "SEND",
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/campaigns/{campaign_id}/freqtrade/live/protection")
    def sync_freqtrade_live_protection(
        campaign_id: UUID,
        payload: BinanceTestnetActionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        detail = queries().campaign_detail(identity.user_id, campaign_id)
        venue = str(detail["venue"])
        require_freqtrade_live_enabled()
        execution_service = service()
        binding = execution_service.freqtrade_live_worker_binding(
            actor_id=identity.user_id,
            execution_scope=payload.execution_scope,
            owner_id=payload.owner_id,
            fencing_token=payload.fencing_token,
            now=_now(),
            campaign_id=campaign_id,
        )
        worker = require_freqtrade_live_worker(binding)
        pair = freqtrade_pair(
            venue,
            str(detail["instrument"]["symbol"]),
            hip3_dexes=binding.hip3_dexes,
        )
        worker.probe(expected_mode="LIVE", required_pair=pair)
        execution_service.validate_freqtrade_worker_binding(binding)
        trade = worker.find_open_trade(pair=pair)
        if trade is None:
            raise DomainRejected(
                "FREQTRADE_POSITION_NOT_FOUND",
                "Freqtrade has no unique open trade to verify protection",
            )
        protection_id = execution_service.record_freqtrade_live_protection(
            campaign_id,
            identity.user_id,
            payload.execution_scope,
            payload.owner_id,
            payload.fencing_token,
            trade,
            now=_now(),
        )
        return {
            "protection_id": str(protection_id),
            "backend": "FREQTRADE",
            "environment": "LIVE",
            "worker": worker.spec.name,
            "trade_id": trade.trade_id,
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }
