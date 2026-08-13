from __future__ import annotations

from trading_control_plane.api_core import (
    UUID,
    Any,
    Direction,
    DomainRejected,
    ExecutionEnvironment,
    JSONResponse,
    ProposalSource,
    Query,
    Request,
    SessionIdentity,
    SignalProposalRequest,
    SignalSourceConfigureRequest,
    SignalSourceCreateRequest,
    SignalSourceCredentialRotateRequest,
    SignalSourceDeleteRequest,
    SignalSourceMode,
    SignalSourceStateRequest,
    SignalSourceTestRequest,
    SignalSourceUpdateRequest,
    ValidationError,
    WebhookSignalPayload,
    _now,
    _perptape_runtime_status,
    _perptape_transport_status,
    datetime,
    status,
    timedelta,
)
from trading_control_plane.api_routes.context import ApiRouteContext


def register_signals_routes(context: ApiRouteContext) -> None:
    """Register signals routes against one application dependency context."""

    app = context.app
    dependencies = context.signals
    common = dependencies.common
    identity_dependency = common.identity
    current_perptape_candidates = dependencies.current_perptape_candidates
    notify_reviewers = dependencies.notify_reviewers
    queries = common.queries
    resolved_settings = common.settings
    service = common.service

    def decorated_signal_sources(identity: SessionIdentity) -> dict[str, Any]:
        now = _now()
        result = service().signal_sources_status(identity.user_id)
        runtime = queries().runtime_snapshot(identity.user_id)
        transport = _perptape_transport_status(
            resolved_settings,
            runtime["source_health"],
            now=now,
        )
        feed = runtime["perptape_feed"]
        for source in result["data"]:
            webhook = source.get("webhook")
            if isinstance(webhook, dict):
                webhook["endpoint_url"] = (
                    f"{resolved_settings.public_base_url.rstrip('/')}{webhook['endpoint_path']}"
                )
            if source.get("mode") != "PERPTAPE":
                continue
            source["runtime"] = (
                transport
                if source.get("enabled") is True
                else {
                    "state": "DISABLED",
                    "primary_channel": None,
                    "fallback_active": False,
                    "error_code": None,
                    "websocket": transport.get("websocket"),
                    "polling": transport.get("polling"),
                }
            )
            perptape = source.get("perptape")
            if isinstance(perptape, dict):
                perptape["data_status"] = (
                    "DISABLED"
                    if source.get("enabled") is not True
                    else _perptape_runtime_status(
                        resolved_settings,
                        feed,
                        now=now,
                        configured=source.get("credential", {}).get("state") != "UNCONFIGURED",
                    )
                )
            health = source.get("health")
            observed = [
                item
                for item in (transport.get("polling"), transport.get("websocket"))
                if isinstance(item, dict) and item.get("checked_at")
            ]
            if isinstance(health, dict) and observed:
                latest = max(observed, key=lambda item: str(item["checked_at"]))
                if str(latest["checked_at"]) > str(health.get("last_checked_at") or ""):
                    health.update(
                        {
                            "last_checked_at": latest.get("checked_at"),
                            "last_success_at": max(
                                [
                                    str(item["last_success_at"])
                                    for item in observed
                                    if item.get("last_success_at")
                                ],
                                default=health.get("last_success_at"),
                            ),
                            "last_error_code": latest.get("error_code"),
                            "consecutive_failures": latest.get("consecutive_failures", 0),
                        }
                    )
        return {**result, "as_of": now.isoformat()}

    @app.get("/api/signal-source")
    def signal_source(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        all_sources = decorated_signal_sources(identity)
        result = {
            "configured": all_sources["configured"],
            "can_manage": all_sources["can_manage"],
            "source": next(
                (item for item in all_sources["data"] if item["mode"] == "PERPTAPE"),
                all_sources["data"][0] if all_sources["data"] else None,
            ),
        }
        return {**result, "as_of": all_sources["as_of"]}

    @app.get("/api/signal-sources")
    def signal_sources(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return decorated_signal_sources(identity)

    @app.put("/api/signal-source")
    def update_signal_source(
        payload: SignalSourceConfigureRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        service().configure_signal_source(
            actor_id=identity.user_id,
            mode=SignalSourceMode(payload.mode),
            secret=payload.secret.get_secret_value(),
            enabled=payload.enabled,
            webhook_max_age_seconds=payload.webhook_max_age_seconds,
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            now=_now(),
        )
        return signal_source(identity)

    @app.post("/api/signal-sources")
    def create_signal_source(
        payload: SignalSourceCreateRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> JSONResponse:
        signal_source_id, one_time_secret, replayed = service().create_signal_source(
            actor_id=identity.user_id,
            name=payload.name,
            mode=SignalSourceMode(payload.mode),
            secret=None if payload.secret is None else payload.secret.get_secret_value(),
            enabled=payload.enabled,
            webhook_max_age_seconds=payload.webhook_max_age_seconds,
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            now=_now(),
        )
        current = decorated_signal_sources(identity)
        source = next(
            item for item in current["data"] if item["signal_source_id"] == str(signal_source_id)
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK if replayed else status.HTTP_201_CREATED,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            content={
                "source": source,
                "one_time_secret": one_time_secret,
                "replayed": replayed,
                "as_of": current["as_of"],
            },
        )

    @app.put("/api/signal-sources/{signal_source_id}")
    def update_signal_source_details(
        signal_source_id: UUID,
        payload: SignalSourceUpdateRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        service().update_signal_source_details(
            signal_source_id,
            actor_id=identity.user_id,
            name=payload.name,
            webhook_max_age_seconds=payload.webhook_max_age_seconds,
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            now=_now(),
        )
        return decorated_signal_sources(identity)

    @app.post("/api/signal-sources/{signal_source_id}/credential-rotations")
    def rotate_signal_source_credential(
        signal_source_id: UUID,
        payload: SignalSourceCredentialRotateRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> JSONResponse:
        one_time_secret, _version, replayed = service().rotate_signal_source_credential(
            signal_source_id,
            actor_id=identity.user_id,
            secret=None if payload.secret is None else payload.secret.get_secret_value(),
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            now=_now(),
        )
        current = decorated_signal_sources(identity)
        source = next(
            item for item in current["data"] if item["signal_source_id"] == str(signal_source_id)
        )
        return JSONResponse(
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            content={
                "source": source,
                "one_time_secret": one_time_secret,
                "replayed": replayed,
                "as_of": current["as_of"],
            },
        )

    @app.post("/api/signal-sources/{signal_source_id}/state")
    def set_signal_source_state(
        signal_source_id: UUID,
        payload: SignalSourceStateRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        service().set_signal_source_enabled(
            signal_source_id,
            actor_id=identity.user_id,
            enabled=payload.enabled,
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            now=_now(),
        )
        return decorated_signal_sources(identity)

    @app.post("/api/signal-sources/{signal_source_id}/tests")
    def test_signal_source(
        signal_source_id: UUID,
        payload: SignalSourceTestRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        common.require_capability(identity, "signal.manage")
        visible = decorated_signal_sources(identity)
        source = next(
            (item for item in visible["data"] if item["signal_source_id"] == str(signal_source_id)),
            None,
        )
        if source is None:
            raise DomainRejected("SIGNAL_SOURCE_NOT_FOUND", "signal source does not exist")
        succeeded = False
        error_code: str | None = None
        items_observed = 0
        if source.get("enabled") is not True:
            error_code = "SIGNAL_SOURCE_DISABLED"
        elif source["mode"] == "WEBHOOK":
            succeeded = source.get("credential", {}).get("state") == "CONFIGURED"
            error_code = None if succeeded else "SIGNAL_SOURCE_NOT_CONFIGURED"
        else:
            try:
                items_observed = len(
                    current_perptape_candidates(user_id=identity.user_id, now=_now())
                )
                succeeded = True
            except DomainRejected as exc:
                error_code = exc.code
        result = service().record_signal_source_test(
            signal_source_id,
            actor_id=identity.user_id,
            succeeded=succeeded,
            error_code=error_code,
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            now=_now(),
        )
        return {**result, "items_observed": items_observed}

    @app.delete("/api/signal-sources/{signal_source_id}")
    def delete_signal_source(
        signal_source_id: UUID,
        payload: SignalSourceDeleteRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        service().delete_signal_source(
            signal_source_id,
            actor_id=identity.user_id,
            confirm_name=payload.confirm_name,
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            now=_now(),
        )
        return decorated_signal_sources(identity)

    @app.post("/api/webhooks/signals/{signal_source_id}")
    async def receive_webhook_signal(
        signal_source_id: UUID,
        request: Request,
    ) -> JSONResponse:
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != (
            "application/json"
        ):
            raise DomainRejected("SIGNAL_CONTENT_TYPE_INVALID", "signal body must be JSON")
        raw_body = await request.body()
        if len(raw_body) > 65_536:
            raise DomainRejected("SIGNAL_PAYLOAD_TOO_LARGE", "signal payload exceeds 64 KiB")
        try:
            payload = WebhookSignalPayload.model_validate_json(raw_body)
        except ValidationError as exc:
            raise DomainRejected(
                "SIGNAL_PAYLOAD_INVALID",
                "signal body does not match the versioned Webhook contract",
            ) from exc
        required_headers = {
            "request_timestamp": request.headers.get("x-tradingops-timestamp"),
            "nonce": request.headers.get("x-tradingops-nonce"),
            "signature": request.headers.get("x-tradingops-signature"),
            "idempotency_key": request.headers.get("idempotency-key"),
        }
        if any(value is None for value in required_headers.values()):
            raise DomainRejected(
                "SIGNAL_HEADERS_INVALID",
                "timestamp, nonce, signature and idempotency headers are required",
            )
        event_id, replayed = service().ingest_webhook_signal(
            signal_source_id,
            raw_body=raw_body,
            payload=payload.model_dump(mode="json"),
            request_timestamp=str(required_headers["request_timestamp"]),
            nonce=str(required_headers["nonce"]),
            signature=str(required_headers["signature"]),
            idempotency_key=str(required_headers["idempotency_key"]),
            now=_now(),
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK if replayed else status.HTTP_202_ACCEPTED,
            content={
                "signal_event_id": str(event_id),
                "status": "ACCEPTED",
                "replayed": replayed,
                "proposal_created": False,
            },
        )

    @app.get("/api/signals")
    def signals(
        limit: int = Query(default=100, ge=1, le=200),
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return {
            "data": service().list_signal_events(identity.user_id, limit=limit),
            "as_of": _now().isoformat(),
        }

    @app.get("/api/webhook-signals")
    def webhook_signals(
        signal_source_id: UUID | None = None,
        venue: str | None = Query(
            default=None,
            pattern="^(BINANCE|HYPERLIQUID|OKX|BYBIT)$",
        ),
        symbol: str | None = Query(default=None, min_length=1, max_length=120),
        direction: str | None = Query(default=None, pattern="^(LONG|SHORT)$"),
        timeframe: str | None = Query(default=None, min_length=1, max_length=32),
        freshness: str | None = Query(default=None, pattern="^(CURRENT|STALE)$"),
        proposal_eligibility: str | None = Query(
            default=None,
            pattern="^(ELIGIBLE|BLOCKED|CREATED)$",
        ),
        limit: int = Query(default=200, ge=1, le=200),
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        return {
            **service().list_webhook_signal_events(
                identity.user_id,
                signal_source_id=signal_source_id,
                venue=venue,
                symbol=symbol,
                direction=direction,
                timeframe=timeframe,
                freshness=freshness,
                proposal_eligibility=proposal_eligibility,
                limit=limit,
                now=now,
            ),
            "as_of": now.isoformat(),
        }

    @app.post("/api/signals/{signal_event_id}/proposals")
    def create_signal_proposal(
        signal_event_id: UUID,
        payload: SignalProposalRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        event = service().signal_event(identity.user_id, signal_event_id)
        frozen_signal = {
            key: event[key]
            for key in (
                "signal_event_id",
                "workspace_id",
                "team_id",
                "signal_source_id",
                "provider",
                "external_id",
                "venue",
                "symbol",
                "direction",
                "strategy_id",
                "strategy_version",
                "timeframe",
                "reference_price",
                "occurred_at",
                "received_at",
                "payload_version",
            )
        }
        proposal_id = service().create_proposal(
            actor_id=identity.user_id,
            source=ProposalSource.MANUAL,
            risk_tier=payload.risk_tier,
            account_id=payload.account_id,
            venue=str(event["venue"]),
            instrument_id=payload.instrument_id,
            direction=Direction(str(event["direction"])),
            quantity=payload.quantity,
            max_risk=payload.max_risk,
            expires_at=now + timedelta(minutes=payload.expires_in_minutes),
            idempotency_key=payload.idempotency_key,
            environment=(
                None if payload.environment is None else ExecutionEnvironment(payload.environment)
            ),
            source_observed_at=datetime.fromisoformat(str(event["occurred_at"])),
            source_readiness="CURRENT",
            signal_event_id=signal_event_id,
            details={
                "signal": frozen_signal,
                "rationale": payload.rationale,
            },
            idempotency_payload={
                "signal_event_id": str(signal_event_id),
                "environment": payload.environment,
                "account_id": payload.account_id,
                "instrument_id": str(payload.instrument_id),
                "risk_tier": payload.risk_tier.value,
                "quantity": str(payload.quantity),
                "max_risk": str(payload.max_risk),
                "expires_in_minutes": payload.expires_in_minutes,
                "rationale": payload.rationale,
            },
            submit_for_review=True,
            now=now,
        )
        current = queries().proposal_detail(identity.user_id, proposal_id)
        notify_reviewers(proposal_id, int(current["version"]), str(current["environment"]))
        return current
