from __future__ import annotations

from typing import cast

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
    SignalSourceMode,
    ValidationError,
    WebhookSignalPayload,
    _now,
    _perptape_transport_status,
    datetime,
    status,
    timedelta,
)
from trading_control_plane.api_routes.context import ApiRouteContext


def register_signals_routes(context: ApiRouteContext) -> None:
    """Register signals routes against one application dependency context."""

    app = context.app
    identity_dependency = context.require("identity_dependency")
    notify_reviewers = context.require("notify_reviewers")
    queries = context.require("queries")
    resolved_settings = context.require("resolved_settings")
    service = context.require("service")

    @app.get("/api/signal-source")
    def signal_source(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        result = service().signal_source_status(identity.user_id)
        source = result.get("source")
        if isinstance(source, dict) and isinstance(source.get("webhook"), dict):
            source["webhook"]["endpoint_url"] = (
                f"{resolved_settings.public_base_url.rstrip('/')}"
                f"{source['webhook']['endpoint_path']}"
            )
        if (
            isinstance(source, dict)
            and source.get("enabled") is True
            and source.get("mode") == "PERPTAPE"
        ):
            runtime = queries().runtime_snapshot(identity.user_id)
            source["runtime"] = _perptape_transport_status(
                resolved_settings,
                runtime["source_health"],
                now=_now(),
            )
        return {**result, "as_of": _now().isoformat()}

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
            environment=ExecutionEnvironment(payload.environment),
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
        return cast(dict[str, Any], current)
