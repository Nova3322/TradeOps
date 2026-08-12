from __future__ import annotations

from trading_control_plane.api_core import (
    ROUND_DOWN,
    SESSION_COOKIE,
    UTC,
    UUID,
    AgentProposalRequest,
    Any,
    AuthorizationRequest,
    Decimal,
    DomainRejected,
    ExecutionEnvironment,
    IntentKind,
    ManualProposalRequest,
    ProposalDefaultConfigRequest,
    ProposalSource,
    ProposalStatus,
    ReviewDecision,
    ReviewRequest,
    RiskDecisionRequest,
    Role,
    SessionIdentity,
    SystemProposalRequest,
    WebSocket,
    WebSocketDisconnect,
    _now,
    asyncio,
    datetime,
    hashlib,
    json,
    perptape_legacy_candidate_id,
    timedelta,
)
from trading_control_plane.api_routes.context import ApiRouteContext


class _ProposalsRoutes:
    def __init__(self, context: ApiRouteContext) -> None:
        dependencies = context.proposals
        common = dependencies.common
        self.app = context.app
        self.current_perptape_candidate = dependencies.current_perptape_candidate
        self.identity_dependency = common.identity
        self.is_agent_identity = dependencies.is_agent_identity
        self.notify_reviewers = dependencies.notify_reviewers
        self.opportunity_snapshot = dependencies.opportunity_snapshot
        self.queries = common.queries
        self.require_capability = common.require_capability
        self.resolved_settings = common.settings
        self.service = common.service
        self.token_service = dependencies.token_service

    def register_opportunity(self) -> None:
        @self.app.get("/api/opportunities")
        def opportunities(
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_capability(identity, "opportunity.view")
            return self.opportunity_snapshot(user_id=identity.user_id, now=_now())

        @self.app.get("/api/proposal-defaults")
        def proposal_defaults(
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_capability(identity, "proposal.create")
            context = self.queries().user_context(identity.user_id)
            config = self.service().proposal_default_config(identity.user_id)
            return {
                "configured": config is not None,
                "can_manage": any(
                    item["role"] == Role.SYSTEM_ADMIN.value for item in context["roles"]
                ),
                "data": config,
                "as_of": _now().isoformat(),
            }

        @self.app.put("/api/proposal-defaults")
        def update_proposal_defaults(
            payload: ProposalDefaultConfigRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.service().set_proposal_default_config(
                identity.user_id,
                payload.idempotency_key,
                account_id=payload.account_id,
                risk_tier=payload.risk_tier,
                notional=payload.notional,
                max_risk=payload.max_risk,
                invalidation_bps=payload.invalidation_bps,
                expires_in_minutes=payload.expires_in_minutes,
                rationale=payload.rationale,
                auto_proposal_enabled=payload.auto_proposal_enabled,
                auto_proposal_min_timeframes=payload.auto_proposal_min_timeframes,
                now=_now(),
            )
            return {
                "configured": True,
                "can_manage": True,
                "data": self.service().proposal_default_config(identity.user_id),
                "as_of": _now().isoformat(),
            }

        @self.app.websocket("/ws/opportunities")
        async def opportunity_stream(websocket: WebSocket) -> None:
            session_token = websocket.cookies.get(SESSION_COOKIE)
            if session_token is None:
                await websocket.close(code=4401)
                return
            try:
                identity = self.token_service.verify_session(session_token, now=_now())
                self.queries().user_context(identity.user_id)
                self.require_capability(identity, "opportunity.view")
            except DomainRejected as exc:
                await websocket.close(code=4403 if exc.code == "RBAC_DENIED" else 4401)
                return

            await websocket.accept()
            last_digest: str | None = None
            last_error: str | None = None
            last_heartbeat = 0.0
            loop = asyncio.get_running_loop()
            try:
                while True:
                    try:
                        snapshot = self.opportunity_snapshot(user_id=identity.user_id, now=_now())
                    except DomainRejected as exc:
                        if last_error != exc.code:
                            await websocket.send_json(
                                {
                                    "type": "error",
                                    "error": {"code": exc.code, "message": exc.detail},
                                }
                            )
                            last_error = exc.code
                    else:
                        digest = hashlib.sha256(
                            json.dumps(
                                snapshot["data"], sort_keys=True, separators=(",", ":")
                            ).encode()
                        ).hexdigest()
                        monotonic_now = loop.time()
                        if digest != last_digest:
                            await websocket.send_json({"type": "snapshot", **snapshot})
                            last_digest = digest
                            last_error = None
                            last_heartbeat = monotonic_now
                        elif monotonic_now - last_heartbeat >= 15:
                            await websocket.send_json(
                                {"type": "heartbeat", "as_of": snapshot["as_of"]}
                            )
                            last_heartbeat = monotonic_now
                    await asyncio.sleep(2)
            except WebSocketDisconnect:
                return

        @self.app.post("/api/opportunities/{candidate_id}/proposals")
        def create_system_proposal(
            candidate_id: str,
            payload: SystemProposalRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            if self.is_agent_identity(identity):
                raise DomainRejected(
                    "AGENT_PROPOSAL_ENDPOINT_REQUIRED",
                    "Agents must use the audited /api/agent/proposals contract",
                )
            now = _now()
            candidate = self.current_perptape_candidate(
                candidate_id,
                user_id=identity.user_id,
                now=now,
            )
            if candidate.readiness != "READY" or candidate.data_health != "CURRENT":
                raise DomainRejected(
                    "PERPTAPE_CANDIDATE_NOT_READY",
                    "candidate data is not ready for proposal review",
                )
            default_config: dict[str, Any] | None = None
            if payload.configuration_mode == "DEFAULT":
                default_config = self.service().proposal_default_config(identity.user_id)
                if default_config is None:
                    raise DomainRejected(
                        "PROPOSAL_DEFAULT_NOT_CONFIGURED",
                        "an administrator must configure one-click proposal defaults",
                    )
                expected_quantity = (
                    Decimal(str(default_config["notional"])) / candidate.reference_price
                ).quantize(Decimal("0.000000000000000001"), rounding=ROUND_DOWN)
                invalidation_factor = Decimal(int(default_config["invalidation_bps"])) / Decimal(
                    10_000
                )
                expected_invalidation = (
                    candidate.reference_price
                    * (
                        Decimal(1) - invalidation_factor
                        if candidate.direction.value == "LONG"
                        else Decimal(1) + invalidation_factor
                    )
                ).quantize(Decimal("0.000000000000000001"), rounding=ROUND_DOWN)
                if (
                    payload.default_config_version != default_config["version"]
                    or payload.environment != default_config["environment"]
                    or payload.account_id != default_config["account_id"]
                    or payload.risk_tier.value != default_config["risk_tier"]
                    or payload.quantity != expected_quantity
                    or payload.initial_quantity is not None
                    or payload.max_risk != Decimal(str(default_config["max_risk"]))
                    or payload.invalidation_price != expected_invalidation
                    or payload.allow_auto_add
                    or payload.requested_adds != 0
                    or payload.add_trigger_price is not None
                    or payload.expires_in_minutes != default_config["expires_in_minutes"]
                    or payload.rationale != default_config["rationale"]
                ):
                    raise DomainRejected(
                        "PROPOSAL_DEFAULT_VERSION_MISMATCH",
                        "one-click payload does not match the active server default version",
                    )
            if not self.service().can_user(
                identity.user_id, "proposal.create", payload.account_id, candidate.venue
            ):
                raise DomainRejected(
                    "RBAC_DENIED", "proposal creation is outside the current scope"
                )
            principal_id = self.service().signal_service_principal(identity.user_id)
            principal = (
                self.queries().service_principal_by_username(
                    self.resolved_settings.perptape_service_username
                )
                if principal_id is None
                else None
            )
            proposal_actor_id = principal.user_id if principal is not None else principal_id
            assert proposal_actor_id is not None
            instrument_id = self.queries().instrument_id_by_venue_symbol(
                candidate.venue, candidate.symbol
            )
            legacy_candidate_id = perptape_legacy_candidate_id(candidate)
            source_candidate_id = (
                self.queries().compatible_legacy_system_candidate_id(
                    legacy_candidate_id,
                    candidate,
                    instrument_id,
                )
                or candidate.candidate_id
            )
            idempotency_payload = {
                "candidate_id": source_candidate_id,
                "account_id": payload.account_id,
                "risk_tier": payload.risk_tier.value,
                "quantity": str(payload.quantity),
                "initial_quantity": (
                    None if payload.initial_quantity is None else str(payload.initial_quantity)
                ),
                "max_risk": str(payload.max_risk),
                "expires_in_minutes": payload.expires_in_minutes,
                "invalidation_price": str(payload.invalidation_price),
                "allow_auto_add": payload.allow_auto_add,
                "requested_adds": payload.requested_adds,
                "add_trigger_price": (
                    None if payload.add_trigger_price is None else str(payload.add_trigger_price)
                ),
                "rationale": payload.rationale,
            }
            if payload.configuration_mode == "DEFAULT":
                idempotency_payload.update(
                    {
                        "configuration_mode": payload.configuration_mode,
                        "default_config_version": payload.default_config_version,
                    }
                )
            proposal_id = self.service().create_proposal(
                actor_id=proposal_actor_id,
                source=ProposalSource.SYSTEM,
                risk_tier=payload.risk_tier,
                account_id=payload.account_id,
                venue=candidate.venue,
                instrument_id=instrument_id,
                direction=candidate.direction,
                quantity=payload.quantity,
                max_risk=payload.max_risk,
                expires_at=now + timedelta(minutes=payload.expires_in_minutes),
                idempotency_key=f"perptape:{source_candidate_id}",
                strategy_id="perptape",
                strategy_version=candidate.source_contract_version,
                environment=ExecutionEnvironment(payload.environment),
                source_candidate_id=source_candidate_id,
                source_link=candidate.detail_url,
                source_observed_at=candidate.observed_at,
                source_readiness=candidate.readiness,
                details={
                    "candidate": candidate.to_dict(),
                    "invalidation_price": str(payload.invalidation_price),
                    "initial_quantity": str(
                        payload.quantity
                        if payload.initial_quantity is None
                        else payload.initial_quantity
                    ),
                    "allow_auto_add": payload.allow_auto_add,
                    "requested_adds": payload.requested_adds,
                    "add_trigger_price": (
                        None
                        if payload.add_trigger_price is None
                        else str(payload.add_trigger_price)
                    ),
                    "rationale": payload.rationale,
                    "configuration_mode": payload.configuration_mode,
                    "default_config_id": (
                        None if default_config is None else default_config["config_id"]
                    ),
                    "default_config_version": payload.default_config_version,
                },
                idempotency_payload=idempotency_payload,
                deduplicate_active_system_scope=True,
                now=now,
            )
            current = self.queries().proposal_detail(proposal_actor_id, proposal_id)
            if current["status"] == ProposalStatus.DRAFT.value:
                self.service().submit_proposal(proposal_id, proposal_actor_id, now=now)
                current = self.queries().proposal_detail(identity.user_id, proposal_id)
                self.notify_reviewers(proposal_id, int(current["version"]), current["environment"])
            return current

        @self.app.post("/api/opportunities/{candidate_id}/proposals/default")
        def create_default_system_proposal(
            candidate_id: str,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            candidate = self.current_perptape_candidate(
                candidate_id,
                user_id=identity.user_id,
                now=now,
            )
            config = self.service().proposal_default_config(identity.user_id)
            if config is None:
                raise DomainRejected(
                    "PROPOSAL_DEFAULT_NOT_CONFIGURED",
                    "an administrator must configure one-click proposal defaults",
                )
            quantity = (Decimal(str(config["notional"])) / candidate.reference_price).quantize(
                Decimal("0.000000000000000001"), rounding=ROUND_DOWN
            )
            factor = Decimal(int(config["invalidation_bps"])) / Decimal(10_000)
            invalidation = (
                candidate.reference_price
                * (
                    Decimal(1) - factor
                    if candidate.direction.value == "LONG"
                    else Decimal(1) + factor
                )
            ).quantize(Decimal("0.000000000000000001"), rounding=ROUND_DOWN)
            return create_system_proposal(
                candidate_id,
                SystemProposalRequest(
                    environment=config["environment"],
                    account_id=config["account_id"],
                    risk_tier=config["risk_tier"],
                    quantity=quantity,
                    initial_quantity=None,
                    max_risk=Decimal(str(config["max_risk"])),
                    invalidation_price=invalidation,
                    allow_auto_add=False,
                    requested_adds=0,
                    add_trigger_price=None,
                    expires_in_minutes=int(config["expires_in_minutes"]),
                    rationale=str(config["rationale"]),
                    configuration_mode="DEFAULT",
                    default_config_version=int(config["version"]),
                ),
                identity,
            )

    def register_creation(self) -> None:
        @self.app.post("/api/agent/proposals")
        def create_agent_proposal(
            payload: AgentProposalRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            if not self.is_agent_identity(identity):
                raise DomainRejected(
                    "AGENT_IDENTITY_REQUIRED",
                    "this endpoint requires a team-scoped Agent Bearer credential",
                )
            now = _now()
            generated_at = payload.generated_at.astimezone(UTC)
            if generated_at > now + timedelta(seconds=30) or now - generated_at > timedelta(
                minutes=5
            ):
                raise DomainRejected(
                    "AGENT_PROPOSAL_STALE",
                    "Agent proposal facts must be current within five minutes",
                )
            details = {
                "trigger_price": str(payload.trigger_price),
                "limit_price": None if payload.limit_price is None else str(payload.limit_price),
                "invalidation_price": str(payload.invalidation_price),
                "initial_quantity": str(payload.quantity),
                "allow_auto_add": False,
                "requested_adds": 0,
                "add_trigger_price": None,
                "rationale": payload.rationale,
                "agent": {
                    "owner_user_id": str(identity.user_id),
                    "api_client_id": str(identity.api_client_id),
                    "model_id": payload.model_id,
                    "model_version": payload.model_version,
                    "request_id": payload.request_id,
                    "generated_at": generated_at.isoformat(),
                },
            }
            idempotency_payload = {
                "source": ProposalSource.SYSTEM.value,
                "environment": payload.environment,
                "account_id": payload.account_id,
                "venue": payload.venue,
                "instrument_id": str(payload.instrument_id),
                "direction": payload.direction.value,
                "risk_tier": payload.risk_tier.value,
                "quantity": str(payload.quantity),
                "max_risk": str(payload.max_risk),
                "expires_in_minutes": payload.expires_in_minutes,
                "strategy_id": payload.model_id,
                "strategy_version": payload.model_version,
                "request_id": payload.request_id,
                "generated_at": generated_at.isoformat(),
                "details": details,
                "submit_for_review": True,
            }
            proposal_id = self.service().create_proposal(
                actor_id=identity.user_id,
                source=ProposalSource.SYSTEM,
                risk_tier=payload.risk_tier,
                account_id=payload.account_id,
                venue=payload.venue,
                instrument_id=payload.instrument_id,
                direction=payload.direction,
                quantity=payload.quantity,
                max_risk=payload.max_risk,
                expires_at=now + timedelta(minutes=payload.expires_in_minutes),
                idempotency_key=payload.idempotency_key,
                strategy_id=payload.model_id,
                strategy_version=payload.model_version,
                environment=ExecutionEnvironment(payload.environment),
                source_candidate_id=(f"api-client:{identity.api_client_id}:{payload.request_id}"),
                source_link=None,
                source_observed_at=generated_at,
                source_readiness="READY",
                details=details,
                idempotency_payload=idempotency_payload,
                submit_for_review=True,
                now=now,
            )
            detail = self.queries().proposal_detail(identity.user_id, proposal_id, now=now)
            return {
                "proposal_id": str(proposal_id),
                "status": detail["status"],
                "detail": detail,
            }

        @self.app.post("/api/proposals/manual")
        def create_manual_proposal(
            payload: ManualProposalRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            instrument = next(
                (
                    item
                    for item in self.queries().list_instruments(identity.user_id)
                    if item["instrument_id"] == str(payload.instrument_id)
                ),
                None,
            )
            if instrument is None or instrument["venue"] != payload.venue:
                raise DomainRejected(
                    "INSTRUMENT_UNAVAILABLE",
                    "instrument is outside the current U-margined venue catalog and permission "
                    "scope",
                )
            quantity = payload.quantity
            initial_quantity = payload.initial_quantity
            resolved_position_notional: Decimal | None = None
            position_notional_currency: str | None = None
            if payload.max_position_notional is not None:
                quote_currency = str(instrument["quote_currency"])
                collateral_currency = str(instrument["collateral_currency"])
                if quote_currency != collateral_currency or quote_currency not in {"USDT", "USDC"}:
                    raise DomainRejected(
                        "POSITION_NOTIONAL_CURRENCY_UNSUPPORTED",
                        "manual position amount requires one matching U-margined quote "
                        "and collateral currency",
                    )
                lot_size = Decimal(str(instrument["lot_size"]))
                contract_multiplier = Decimal(str(instrument["contract_multiplier"]))
                minimum_notional = Decimal(str(instrument["minimum_notional"]))

                def quantity_from_notional(value: Decimal, *, error_code: str) -> Decimal:
                    raw_quantity = value / (payload.trigger_price * contract_multiplier)
                    steps = (raw_quantity / lot_size).to_integral_value(rounding=ROUND_DOWN)
                    resolved = steps * lot_size
                    resolved_notional = resolved * payload.trigger_price * contract_multiplier
                    if resolved <= 0 or resolved_notional < minimum_notional:
                        raise DomainRejected(
                            error_code,
                            "position amount is below the current contract lot size "
                            "or minimum notional",
                        )
                    return resolved

                quantity = quantity_from_notional(
                    payload.max_position_notional,
                    error_code="POSITION_NOTIONAL_TOO_SMALL",
                )
                initial_quantity = (
                    None
                    if payload.initial_position_notional is None
                    else quantity_from_notional(
                        payload.initial_position_notional,
                        error_code="INITIAL_POSITION_NOTIONAL_TOO_SMALL",
                    )
                )
                if initial_quantity is not None and initial_quantity > quantity:
                    raise DomainRejected(
                        "INITIAL_POSITION_EXCEEDS_CAP",
                        "resolved initial position exceeds the maximum position amount",
                    )
                if (
                    payload.allow_auto_add
                    and initial_quantity is not None
                    and initial_quantity >= quantity
                ):
                    raise DomainRejected(
                        "INITIAL_POSITION_EXHAUSTS_CAP",
                        "resolved initial position leaves no quantity capacity for AUTO_ADD",
                    )
                resolved_position_notional = quantity * payload.trigger_price * contract_multiplier
                position_notional_currency = quote_currency
            assert quantity is not None
            proposal_id = self.service().create_proposal(
                actor_id=identity.user_id,
                source=ProposalSource.MANUAL,
                risk_tier=payload.risk_tier,
                account_id=payload.account_id,
                venue=payload.venue,
                instrument_id=payload.instrument_id,
                direction=payload.direction,
                quantity=quantity,
                max_risk=payload.max_risk,
                expires_at=now + timedelta(minutes=payload.expires_in_minutes),
                idempotency_key=payload.idempotency_key,
                environment=ExecutionEnvironment(payload.environment),
                details={
                    "trigger_price": str(payload.trigger_price),
                    "limit_price": None
                    if payload.limit_price is None
                    else str(payload.limit_price),
                    "invalidation_price": str(payload.invalidation_price),
                    "initial_quantity": str(
                        quantity if initial_quantity is None else initial_quantity
                    ),
                    "requested_max_position_notional": (
                        None
                        if payload.max_position_notional is None
                        else str(payload.max_position_notional)
                    ),
                    "resolved_position_notional": (
                        None
                        if resolved_position_notional is None
                        else str(resolved_position_notional)
                    ),
                    "position_notional_currency": position_notional_currency,
                    "allow_auto_add": payload.allow_auto_add,
                    "requested_adds": payload.requested_adds,
                    "add_trigger_price": (
                        None
                        if payload.add_trigger_price is None
                        else str(payload.add_trigger_price)
                    ),
                    "rationale": payload.rationale,
                },
                idempotency_payload={
                    "source": "MANUAL",
                    "environment": payload.environment,
                    "account_id": payload.account_id,
                    "venue": payload.venue,
                    "instrument_id": str(payload.instrument_id),
                    "direction": payload.direction.value,
                    "risk_tier": payload.risk_tier.value,
                    "quantity": str(quantity),
                    "max_position_notional": (
                        None
                        if payload.max_position_notional is None
                        else str(payload.max_position_notional)
                    ),
                    "initial_quantity": (
                        None if initial_quantity is None else str(initial_quantity)
                    ),
                    "initial_position_notional": (
                        None
                        if payload.initial_position_notional is None
                        else str(payload.initial_position_notional)
                    ),
                    "max_risk": str(payload.max_risk),
                    "expires_in_minutes": payload.expires_in_minutes,
                    "trigger_price": str(payload.trigger_price),
                    "limit_price": (
                        None if payload.limit_price is None else str(payload.limit_price)
                    ),
                    "invalidation_price": str(payload.invalidation_price),
                    "allow_auto_add": payload.allow_auto_add,
                    "requested_adds": payload.requested_adds,
                    "add_trigger_price": (
                        None
                        if payload.add_trigger_price is None
                        else str(payload.add_trigger_price)
                    ),
                    "rationale": payload.rationale,
                },
                deduplicate_active_manual_semantics=True,
                now=now,
            )
            current = self.queries().proposal_detail(identity.user_id, proposal_id)
            if current["status"] == ProposalStatus.DRAFT.value:
                self.service().submit_proposal(proposal_id, identity.user_id, now=now)
                current = self.queries().proposal_detail(identity.user_id, proposal_id)
                self.notify_reviewers(proposal_id, int(current["version"]), payload.environment)
            return current

    def register_review(self) -> None:
        @self.app.get("/api/proposals")
        def proposals(
            identity: SessionIdentity = self.identity_dependency,
            proposal_status: str | None = None,
        ) -> dict[str, Any]:
            self.require_capability(identity, "proposal.view")
            now = _now()
            return {
                "data": self.queries().list_proposals(
                    identity.user_id,
                    status=proposal_status,
                    now=now,
                ),
                "as_of": now.isoformat(),
            }

        @self.app.get("/api/proposals/{proposal_id}")
        def proposal_detail(
            proposal_id: UUID,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            self.require_capability(identity, "proposal.view")
            return self.queries().proposal_detail(identity.user_id, proposal_id, now=_now())

        @self.app.post("/api/proposals/{proposal_id}/reviews")
        def review_proposal(
            proposal_id: UUID,
            payload: ReviewRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            agent_call = self.is_agent_identity(identity)
            if agent_call and payload.idempotency_key is None:
                raise DomainRejected(
                    "AGENT_IDEMPOTENCY_REQUIRED",
                    "Agent reviews require an explicit idempotency key",
                )
            if payload.decision == "APPROVE":
                if agent_call:
                    raise DomainRejected(
                        "HUMAN_WEB_CONFIRMATION_REQUIRED",
                        "proposal approval requires the owner to complete step-up in the web UI",
                    )
                if payload.action_grant is None:
                    raise DomainRejected(
                        "ACTION_GRANT_REQUIRED", "proposal approval requires action-level step-up"
                    )
                self.token_service.verify_action_grant(
                    payload.action_grant,
                    user_id=identity.user_id,
                    action="proposal.approve",
                    object_id=proposal_id,
                    object_version=payload.expected_version,
                    now=now,
                )
            result = self.service().review_proposal(
                proposal_id,
                identity.user_id,
                ReviewDecision(payload.decision),
                payload.reason,
                expected_version=payload.expected_version,
                idempotency_key=payload.idempotency_key,
                now=now,
            )
            detail = self.queries().proposal_detail(identity.user_id, proposal_id, now=now)
            if result is ProposalStatus.PENDING_REVIEW:
                self.notify_reviewers(
                    proposal_id,
                    int(detail["version"]),
                    str(detail["environment"]),
                )
            return {
                "proposal_id": str(proposal_id),
                "status": result.value,
                "detail": detail,
            }

        @self.app.post("/api/proposals/{proposal_id}/risk-decisions")
        def decide_risk(
            proposal_id: UUID,
            payload: RiskDecisionRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            decision_id = self.service().decide_risk(
                proposal_id=proposal_id,
                actor_id=identity.user_id,
                kind=IntentKind.INITIAL,
                idempotency_key=payload.idempotency_key,
                requested_quantity=payload.requested_quantity,
                now=_now(),
            )
            return {
                "decision_id": str(decision_id),
                "detail": self.queries().proposal_detail(identity.user_id, proposal_id),
            }

        @self.app.post("/api/proposals/{proposal_id}/authorizations")
        def issue_authorization(
            proposal_id: UUID,
            payload: AuthorizationRequest,
            identity: SessionIdentity = self.identity_dependency,
        ) -> dict[str, Any]:
            now = _now()
            proposal = self.queries().proposal_detail(identity.user_id, proposal_id)
            proposal_expiry = datetime.fromisoformat(str(proposal["expires_at"]))
            requested_expiry = now + timedelta(minutes=payload.expires_in_minutes)
            expires_at = min(proposal_expiry, requested_expiry)
            authorization_id = self.service().issue_authorization(
                proposal_id=proposal_id,
                actor_id=identity.user_id,
                expires_at=expires_at,
                allowed_adds=payload.allowed_adds,
                idempotency_key=payload.idempotency_key,
                now=now,
            )
            return {
                "authorization_id": str(authorization_id),
                "detail": self.queries().proposal_detail(identity.user_id, proposal_id),
            }


def register_proposals_routes(context: ApiRouteContext) -> None:
    """Register proposals routes from bounded lifecycle groups."""

    routes = _ProposalsRoutes(context)
    routes.register_opportunity()
    routes.register_creation()
    routes.register_review()
