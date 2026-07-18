from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote
from uuid import UUID

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from trading_control_plane import __version__
from trading_control_plane.api_schemas import (
    AccountEquityFactRequest,
    AuthorizationRequest,
    BinanceReadOnlySyncRequest,
    CampaignTargetRequest,
    FundingFactRequest,
    IntentReleaseRequest,
    IntentUnknownRequest,
    ManualProposalRequest,
    MockLoginRequest,
    MockStepUpRequest,
    OrderIntentRequest,
    PositionFactRequest,
    ProtectionFactRequest,
    ReconciliationReasonRequest,
    ReconciliationRequest,
    ReductionIntentRequest,
    ReviewRequest,
    RiskDecisionRequest,
    SenderLeaseRequest,
    ShadowFillRequest,
    ShadowSendRequest,
    SystemProposalRequest,
)
from trading_control_plane.auth import SessionIdentity, SignedTokenService
from trading_control_plane.binance import BinanceReadOnlyClient
from trading_control_plane.config import Settings, get_settings
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    DomainRejected,
    ExecutionEnvironment,
    IntentKind,
    OrderIntentStatus,
    ProposalSource,
    ProposalStatus,
    ReviewDecision,
    TargetCandidate,
)
from trading_control_plane.logging import configure_logging
from trading_control_plane.metrics import DATABASE_READY
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.queries import TradingQueries
from trading_control_plane.service import TradingService
from trading_control_plane.telegram import (
    CampaignNotification,
    MockTelegramGateway,
    ProposalNotification,
)

logger = logging.getLogger(__name__)
WEB_ROOT = Path(__file__).parent / "web"
SESSION_COOKIE = "trading_session"


class ReadinessDatabase(Protocol):
    def is_ready(self) -> tuple[bool, str | None]: ...

    def dispose(self) -> None: ...


def _now() -> datetime:
    return datetime.now(UTC)


def _domain_status(code: str) -> int:
    if code in {"LOGIN_DENIED", "AUTH_TOKEN_INVALID", "SESSION_EXPIRED", "SESSION_REVOKED"}:
        return status.HTTP_401_UNAUTHORIZED
    if code in {
        "RBAC_DENIED",
        "SELF_REVIEW_FORBIDDEN",
        "ACTION_GRANT_REQUIRED",
        "ACTION_GRANT_SCOPE_INVALID",
        "ACTION_GRANT_EXPIRED",
    }:
        return status.HTTP_403_FORBIDDEN
    if code.endswith("_NOT_FOUND"):
        return status.HTTP_404_NOT_FOUND
    if code in {
        "IDEMPOTENCY_CONFLICT",
        "VERSION_CONFLICT",
        "REVIEW_ALREADY_RECORDED",
        "PROPOSAL_NOT_DRAFT",
        "PROPOSAL_NOT_REVIEWABLE",
        "PROPOSAL_NOT_APPROVED",
    }:
        return status.HTTP_409_CONFLICT
    if code in {
        "PERPTAPE_UNAVAILABLE",
        "PERPTAPE_NOT_CONFIGURED",
        "BINANCE_READ_ONLY_DISABLED",
        "BINANCE_READ_ONLY_NOT_CONFIGURED",
        "BINANCE_READ_ONLY_UNAVAILABLE",
    }:
        return status.HTTP_503_SERVICE_UNAVAILABLE
    if code == "BINANCE_RESPONSE_INVALID":
        return status.HTTP_502_BAD_GATEWAY
    return status.HTTP_422_UNPROCESSABLE_CONTENT


def create_app(
    settings: Settings | None = None,
    database: ReadinessDatabase | None = None,
    perptape_client: PerptapeClient | None = None,
    telegram_gateway: MockTelegramGateway | None = None,
    binance_client: BinanceReadOnlyClient | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    resolved_settings.validate_runtime_security()
    configure_logging(resolved_settings.log_level)
    resolved_database = database or Database(resolved_settings.database_url)
    token_service = SignedTokenService(resolved_settings.session_signing_secret)
    resolved_perptape = perptape_client or PerptapeClient(
        base_url=resolved_settings.perptape_base_url,
        api_key=resolved_settings.perptape_api_key,
        contract_version=resolved_settings.perptape_contract_version,
        cache_ttl=timedelta(seconds=resolved_settings.perptape_cache_seconds),
    )
    resolved_telegram = telegram_gateway or MockTelegramGateway()
    resolved_binance = binance_client or BinanceReadOnlyClient(
        base_url=resolved_settings.binance_futures_base_url,
        api_key=resolved_settings.binance_api_key,
        api_secret=resolved_settings.binance_api_secret,
        recv_window_ms=resolved_settings.binance_recv_window_ms,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        resolved_database.dispose()

    app = FastAPI(
        title="Trading Control Plane",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = resolved_settings
    app.state.database = resolved_database
    app.state.perptape_client = resolved_perptape
    app.state.telegram_gateway = resolved_telegram
    app.state.binance_client = resolved_binance

    @app.exception_handler(DomainRejected)
    async def domain_rejected(_: Request, exc: DomainRejected) -> JSONResponse:
        return JSONResponse(
            status_code=_domain_status(exc.code),
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.detail,
                    "retryable": exc.code
                    in {"PERPTAPE_UNAVAILABLE", "BINANCE_READ_ONLY_UNAVAILABLE"},
                }
            },
        )

    def business_database() -> Database:
        if not isinstance(resolved_database, Database):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": {"code": "BUSINESS_API_UNAVAILABLE"}},
            )
        return resolved_database

    def queries() -> TradingQueries:
        return TradingQueries(business_database())

    def service() -> TradingService:
        return TradingService(business_database())

    def current_identity(
        trading_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    ) -> SessionIdentity:
        if trading_session is None:
            raise DomainRejected("LOGIN_DENIED", "an internal login session is required")
        identity = token_service.verify_session(trading_session, now=_now())
        queries().user_context(identity.user_id)
        return identity

    identity_dependency = Depends(current_identity)

    @app.get("/health/live", tags=["health"])
    def liveness() -> dict[str, str]:
        return {"status": "live", "version": __version__}

    @app.get("/health/ready", tags=["health"])
    def readiness() -> dict[str, Any]:
        ready, error_code = resolved_database.is_ready()
        DATABASE_READY.set(1 if ready else 0)
        if not ready:
            logger.warning(
                "durable trading core is not ready",
                extra={
                    "event": "readiness_failed",
                    "error_code": error_code or "READINESS_FAILED",
                    "component": "database",
                },
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "status": "not_ready",
                    "error_code": error_code or "READINESS_FAILED",
                },
            )
        return {"status": "ready", "durable_store": "postgresql"}

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/api/auth/status")
    def auth_status() -> dict[str, Any]:
        return {
            "provider": "MANAGED_IDP_PASSKEY",
            "provider_configured": False,
            "mock_identity_available": (
                resolved_settings.allow_mock_identity
                and resolved_settings.environment in {"local", "test"}
            ),
            "environment": resolved_settings.environment,
        }

    @app.post("/api/auth/mock/login")
    def mock_login(payload: MockLoginRequest, response: Response) -> dict[str, Any]:
        if not (
            resolved_settings.allow_mock_identity
            and resolved_settings.environment in {"local", "test"}
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        user = queries().user_by_username(payload.username)
        now = _now()
        token = token_service.issue_session(
            user_id=user.user_id,
            username=user.username,
            now=now,
            ttl=timedelta(seconds=resolved_settings.session_ttl_seconds),
            authentication_method="mock-internal-user",
        )
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            secure=False,
            samesite="strict",
            max_age=resolved_settings.session_ttl_seconds,
            path="/",
        )
        return {
            "session": queries().user_context(user.user_id),
            "authentication_method": "MOCK_NON_PRODUCTION",
            "expires_at": (
                now + timedelta(seconds=resolved_settings.session_ttl_seconds)
            ).isoformat(),
        }

    @app.post("/api/auth/logout")
    def logout(response: Response) -> dict[str, str]:
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"status": "logged_out"}

    @app.get("/api/auth/session")
    def auth_session(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return {
            "session": queries().user_context(identity.user_id),
            "authentication_method": identity.authentication_method,
            "expires_at": identity.expires_at.isoformat(),
        }

    @app.post("/api/auth/mock/step-up")
    def mock_step_up(
        payload: MockStepUpRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        if not (
            resolved_settings.allow_mock_identity
            and resolved_settings.environment in {"local", "test"}
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        current_version = queries().proposal_version(payload.object_id)
        if current_version != payload.object_version:
            raise DomainRejected("VERSION_CONFLICT", "proposal changed before step-up")
        detail = queries().proposal_detail(identity.user_id, payload.object_id)
        if not service().can_user(
            identity.user_id,
            "proposal.review",
            str(detail["account_id"]),
            str(detail["venue"]),
        ):
            raise DomainRejected("RBAC_DENIED", "proposal approval is outside the current scope")
        token = token_service.issue_action_grant(
            user_id=identity.user_id,
            action=payload.action,
            object_id=payload.object_id,
            object_version=payload.object_version,
            now=_now(),
            ttl=timedelta(seconds=resolved_settings.action_token_ttl_seconds),
            authentication_method="mock-passkey-step-up",
        )
        return {
            "action_grant": token,
            "authentication_method": "MOCK_PASSKEY_NON_PRODUCTION",
            "expires_in_seconds": resolved_settings.action_token_ttl_seconds,
        }

    @app.get("/api/instruments")
    def instruments(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return {"data": queries().list_instruments(identity.user_id), "as_of": _now().isoformat()}

    def notify_reviewers(proposal_id: UUID, proposal_version: int) -> None:
        for reviewer in queries().reviewers_for_proposal(proposal_id):
            code = token_service.issue_review_reference(
                user_id=reviewer.user_id,
                object_id=proposal_id,
                object_version=proposal_version,
                now=_now(),
                ttl=timedelta(seconds=resolved_settings.action_token_ttl_seconds),
            )
            review_url = (
                f"{resolved_settings.public_base_url.rstrip('/')}/proposals/{proposal_id}"
                f"?review_code={quote(code)}"
            )
            notification_key = f"{proposal_id}:{proposal_version}:{reviewer.user_id}"
            resolved_telegram.send(
                ProposalNotification(
                    notification_id="tg_"
                    + hashlib.sha256(notification_key.encode()).hexdigest()[:20],
                    reviewer_id=reviewer.user_id,
                    proposal_id=proposal_id,
                    proposal_version=proposal_version,
                    environment="SHADOW",
                    summary=f"SHADOW proposal {proposal_id} is pending review",
                    review_code=code,
                    review_url=review_url,
                    created_at=_now(),
                )
            )

    @app.get("/api/opportunities")
    def opportunities(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        queries().user_context(identity.user_id)
        now = _now()
        candidates = resolved_perptape.list_candidates(now=now)
        return {
            "source": "PERPTAPE",
            "source_contract_version": resolved_settings.perptape_contract_version,
            "environment": "SHADOW",
            "as_of": now.isoformat(),
            "data": [candidate.to_dict() for candidate in candidates],
        }

    @app.post("/api/opportunities/{candidate_id}/proposals")
    def create_system_proposal(
        candidate_id: str,
        payload: SystemProposalRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        candidate = resolved_perptape.get_candidate(candidate_id, now=now)
        if candidate.readiness != "READY":
            raise DomainRejected(
                "PERPTAPE_CANDIDATE_NOT_READY", "candidate data is not ready for proposal review"
            )
        if not service().can_user(
            identity.user_id, "proposal.create", payload.account_id, candidate.venue
        ):
            raise DomainRejected("RBAC_DENIED", "proposal creation is outside the current scope")
        principal = queries().service_principal_by_username(
            resolved_settings.perptape_service_username
        )
        instrument_id = queries().instrument_id_by_venue_symbol(candidate.venue, candidate.symbol)
        proposal_id = service().create_proposal(
            actor_id=principal.user_id,
            source=ProposalSource.SYSTEM,
            risk_tier=payload.risk_tier,
            account_id=payload.account_id,
            venue=candidate.venue,
            instrument_id=instrument_id,
            direction=candidate.direction,
            quantity=payload.quantity,
            max_risk=payload.max_risk,
            expires_at=now + timedelta(minutes=payload.expires_in_minutes),
            idempotency_key=f"perptape:{candidate.candidate_id}",
            strategy_id="perptape",
            strategy_version=candidate.source_contract_version,
            environment=ExecutionEnvironment.SHADOW,
            source_candidate_id=candidate.candidate_id,
            source_link=candidate.detail_url,
            source_observed_at=candidate.observed_at,
            source_readiness=candidate.readiness,
            details={
                "candidate": candidate.to_dict(),
                "invalidation_price": str(payload.invalidation_price),
                "rationale": payload.rationale,
            },
            idempotency_payload={
                "candidate_id": candidate.candidate_id,
                "account_id": payload.account_id,
                "risk_tier": payload.risk_tier.value,
                "quantity": str(payload.quantity),
                "max_risk": str(payload.max_risk),
                "expires_in_minutes": payload.expires_in_minutes,
                "invalidation_price": str(payload.invalidation_price),
                "rationale": payload.rationale,
            },
            now=now,
        )
        current = queries().proposal_detail(principal.user_id, proposal_id)
        if current["status"] == ProposalStatus.DRAFT.value:
            service().submit_proposal(proposal_id, principal.user_id, now=now)
            current = queries().proposal_detail(identity.user_id, proposal_id)
            notify_reviewers(proposal_id, int(current["version"]))
        return current

    @app.post("/api/proposals/manual")
    def create_manual_proposal(
        payload: ManualProposalRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        proposal_id = service().create_proposal(
            actor_id=identity.user_id,
            source=ProposalSource.MANUAL,
            risk_tier=payload.risk_tier,
            account_id=payload.account_id,
            venue=payload.venue,
            instrument_id=payload.instrument_id,
            direction=payload.direction,
            quantity=payload.quantity,
            max_risk=payload.max_risk,
            expires_at=now + timedelta(minutes=payload.expires_in_minutes),
            idempotency_key=payload.idempotency_key,
            environment=ExecutionEnvironment.SHADOW,
            details={
                "trigger_price": str(payload.trigger_price),
                "limit_price": None if payload.limit_price is None else str(payload.limit_price),
                "invalidation_price": str(payload.invalidation_price),
                "rationale": payload.rationale,
            },
            idempotency_payload={
                "source": "MANUAL",
                "account_id": payload.account_id,
                "venue": payload.venue,
                "instrument_id": str(payload.instrument_id),
                "direction": payload.direction.value,
                "risk_tier": payload.risk_tier.value,
                "quantity": str(payload.quantity),
                "max_risk": str(payload.max_risk),
                "expires_in_minutes": payload.expires_in_minutes,
                "trigger_price": str(payload.trigger_price),
                "limit_price": (None if payload.limit_price is None else str(payload.limit_price)),
                "invalidation_price": str(payload.invalidation_price),
                "rationale": payload.rationale,
            },
            now=now,
        )
        current = queries().proposal_detail(identity.user_id, proposal_id)
        if current["status"] == ProposalStatus.DRAFT.value:
            service().submit_proposal(proposal_id, identity.user_id, now=now)
            current = queries().proposal_detail(identity.user_id, proposal_id)
            notify_reviewers(proposal_id, int(current["version"]))
        return current

    @app.get("/api/proposals")
    def proposals(
        identity: SessionIdentity = identity_dependency,
        proposal_status: str | None = None,
    ) -> dict[str, Any]:
        return {
            "data": queries().list_proposals(identity.user_id, status=proposal_status),
            "as_of": _now().isoformat(),
        }

    @app.get("/api/proposals/{proposal_id}")
    def proposal_detail(
        proposal_id: UUID,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return queries().proposal_detail(identity.user_id, proposal_id)

    @app.post("/api/proposals/{proposal_id}/reviews")
    def review_proposal(
        proposal_id: UUID,
        payload: ReviewRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        if payload.decision == "APPROVE":
            if payload.action_grant is None:
                raise DomainRejected(
                    "ACTION_GRANT_REQUIRED", "proposal approval requires action-level step-up"
                )
            token_service.verify_action_grant(
                payload.action_grant,
                user_id=identity.user_id,
                action="proposal.approve",
                object_id=proposal_id,
                object_version=payload.expected_version,
                now=now,
            )
        result = service().review_proposal(
            proposal_id,
            identity.user_id,
            ReviewDecision(payload.decision),
            payload.reason,
            expected_version=payload.expected_version,
            now=now,
        )
        return {
            "proposal_id": str(proposal_id),
            "status": result.value,
            "detail": queries().proposal_detail(identity.user_id, proposal_id),
        }

    @app.post("/api/proposals/{proposal_id}/risk-decisions")
    def decide_risk(
        proposal_id: UUID,
        payload: RiskDecisionRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        decision_id = service().decide_risk(
            proposal_id=proposal_id,
            actor_id=identity.user_id,
            kind=IntentKind.INITIAL,
            idempotency_key=payload.idempotency_key,
            requested_quantity=payload.requested_quantity,
            now=_now(),
        )
        return {
            "decision_id": str(decision_id),
            "detail": queries().proposal_detail(identity.user_id, proposal_id),
        }

    @app.post("/api/proposals/{proposal_id}/authorizations")
    def issue_authorization(
        proposal_id: UUID,
        payload: AuthorizationRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        now = _now()
        proposal = queries().proposal_detail(identity.user_id, proposal_id)
        proposal_expiry = datetime.fromisoformat(str(proposal["expires_at"]))
        requested_expiry = now + timedelta(minutes=payload.expires_in_minutes)
        expires_at = min(proposal_expiry, requested_expiry)
        authorization_id = service().issue_authorization(
            proposal_id=proposal_id,
            actor_id=identity.user_id,
            expires_at=expires_at,
            allowed_adds=payload.allowed_adds,
            idempotency_key=payload.idempotency_key,
            now=now,
        )
        return {
            "authorization_id": str(authorization_id),
            "detail": queries().proposal_detail(identity.user_id, proposal_id),
        }

    def notify_campaign(
        recipient_id: UUID,
        campaign_id: UUID,
        event_type: str,
        event_key: str,
        summary: str,
    ) -> None:
        notification_key = f"{campaign_id}:{event_type}:{event_key}:{recipient_id}"
        resolved_telegram.send_campaign(
            CampaignNotification(
                notification_id="tg_" + hashlib.sha256(notification_key.encode()).hexdigest()[:20],
                recipient_id=recipient_id,
                campaign_id=campaign_id,
                event_type=event_type,
                environment="SHADOW",
                summary=summary,
                created_at=_now(),
            )
        )

    @app.get("/api/venues/binance/status")
    def binance_read_only_status(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        del identity
        return {
            "venue": "BINANCE",
            "mode": "USER_DATA_READ_ONLY",
            "enabled": resolved_settings.binance_read_only_enabled,
            "configured": resolved_binance.configured,
            "order_send_available": False,
            "fact_environment": resolved_settings.binance_fact_environment,
            "environment": resolved_settings.environment,
        }

    @app.get("/api/venues/binance/facts")
    def binance_read_only_facts(
        account_id: str,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return {
            "mode": "USER_DATA_READ_ONLY",
            "data": queries().venue_facts(
                identity.user_id,
                account_id,
                "BINANCE",
                resolved_settings.binance_fact_environment,
            ),
            "as_of": _now().isoformat(),
        }

    @app.post("/api/venues/binance/sync")
    def sync_binance_read_only_facts(
        payload: BinanceReadOnlySyncRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        if not resolved_settings.binance_read_only_enabled:
            raise DomainRejected(
                "BINANCE_READ_ONLY_DISABLED",
                "Binance USER_DATA read-only synchronization is disabled",
            )
        if not resolved_binance.configured:
            raise DomainRejected(
                "BINANCE_READ_ONLY_NOT_CONFIGURED",
                "Binance read-only credentials are not configured",
            )
        if not service().can_user(identity.user_id, "venue.record", payload.account_id, "BINANCE"):
            raise DomainRejected(
                "RBAC_DENIED", "Binance facts are outside the current operator scope"
            )
        now = _now()
        snapshot = resolved_binance.read_snapshot(payload.symbol, now=now)
        persisted = service().ingest_binance_read_only_snapshot(
            payload.account_id,
            identity.user_id,
            snapshot,
            environment=ExecutionEnvironment(resolved_settings.binance_fact_environment),
            now=now,
        )
        execution_scope = (
            f"{resolved_settings.binance_fact_environment}:{payload.account_id}:BINANCE"
        )
        reconciliation_id = service().reconcile_scope(
            execution_scope,
            identity.user_id,
            now=now,
        )
        reconciliation_status = service().reconciliation_status(reconciliation_id)
        return {
            "source": "BINANCE_USER_DATA",
            "mode": "READ_ONLY",
            "environment": resolved_settings.binance_fact_environment,
            "symbol": payload.symbol,
            "observed_at": snapshot.observed_at.isoformat(),
            "persisted": persisted,
            "reconciliation": {
                "reconciliation_id": str(reconciliation_id),
                "execution_scope": execution_scope,
                "status": reconciliation_status.value,
            },
            "facts": queries().venue_facts(
                identity.user_id,
                payload.account_id,
                "BINANCE",
                resolved_settings.binance_fact_environment,
            ),
        }

    @app.get("/api/campaigns")
    def campaigns(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return {"data": queries().list_campaigns(identity.user_id), "as_of": _now().isoformat()}

    @app.get("/api/campaign-exceptions")
    def campaign_exceptions(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return {
            "data": queries().list_exceptions(identity.user_id),
            "as_of": _now().isoformat(),
        }

    @app.get("/api/campaigns/{campaign_id}")
    def campaign_detail(
        campaign_id: UUID,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        return queries().campaign_detail(identity.user_id, campaign_id)

    @app.post("/api/authorizations/{authorization_id}/intents")
    def create_order_intent(
        authorization_id: UUID,
        payload: OrderIntentRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        created = service().create_order_intent(
            authorization_id,
            identity.user_id,
            payload.kind,
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
            f"SHADOW fill {payload.venue_fill_id} recorded; no venue order was sent",
        )
        return {
            "venue_fill_fact_id": str(fact_id),
            "environment": "SHADOW",
            "detail": queries().campaign_detail(identity.user_id, campaign_id),
        }

    @app.post("/api/intents/{intent_id}/unknown")
    def mark_intent_unknown(
        intent_id: UUID,
        payload: IntentUnknownRequest,
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        campaign_id = queries().campaign_id_for_intent(identity.user_id, intent_id)
        service().mark_intent_unknown(intent_id, identity.user_id, payload.reason, now=_now())
        notify_campaign(
            identity.user_id,
            campaign_id,
            "ORDER_INTENT_UNKNOWN",
            str(intent_id),
            "Order outcome is UNKNOWN; risk remains occupied and automatic retry is blocked",
        )
        return queries().campaign_detail(identity.user_id, campaign_id)

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
            now=_now(),
        )
        return queries().campaign_detail(identity.user_id, campaign_id)

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
            "Protection fact recorded for SHADOW position",
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
            campaign_id, identity.user_id, payload.idempotency_key, now=_now()
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
                f"Reconciliation requires attention: {reconciliation['status']}",
            )
        return {"reconciliation_id": str(reconciliation_id), "detail": detail}

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
            "SHADOW Campaign closed after flat position and reconciliation MATCH",
        )
        return queries().campaign_detail(identity.user_id, campaign_id)

    @app.get("/api/telegram/mock/notifications")
    def mock_telegram_notifications(
        identity: SessionIdentity = identity_dependency,
    ) -> dict[str, Any]:
        if resolved_settings.environment not in {"local", "test"}:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        data = [
            {
                "notification_id": item.notification_id,
                "proposal_id": str(item.proposal_id),
                "proposal_version": item.proposal_version,
                "environment": item.environment,
                "summary": item.summary,
                "review_code": item.review_code,
                "review_url": item.review_url,
                "created_at": item.created_at.isoformat(),
            }
            for item in resolved_telegram.notifications()
            if item.reviewer_id == identity.user_id
        ]
        campaign_data = [
            {
                "notification_id": item.notification_id,
                "campaign_id": str(item.campaign_id),
                "event_type": item.event_type,
                "environment": item.environment,
                "summary": item.summary,
                "created_at": item.created_at.isoformat(),
            }
            for item in resolved_telegram.campaign_notifications()
            if item.recipient_id == identity.user_id
        ]
        return {
            "transport": "MOCK_ONLY",
            "data": data,
            "campaign_data": campaign_data,
        }

    if WEB_ROOT.exists():
        app.mount("/assets", StaticFiles(directory=WEB_ROOT), name="web-assets")

        @app.get("/manifest.webmanifest", include_in_schema=False)
        def manifest() -> FileResponse:
            return FileResponse(WEB_ROOT / "manifest.webmanifest")

        @app.get("/sw.js", include_in_schema=False)
        def service_worker() -> FileResponse:
            return FileResponse(WEB_ROOT / "sw.js", media_type="application/javascript")

        @app.get("/", include_in_schema=False)
        @app.get("/opportunities", include_in_schema=False)
        @app.get("/proposals/new", include_in_schema=False)
        @app.get("/reviews", include_in_schema=False)
        @app.get("/campaigns", include_in_schema=False)
        @app.get("/positions", include_in_schema=False)
        @app.get("/orders", include_in_schema=False)
        @app.get("/risk", include_in_schema=False)
        @app.get("/exceptions", include_in_schema=False)
        @app.get("/venues/binance", include_in_schema=False)
        @app.get("/proposals/{proposal_id}", include_in_schema=False)
        @app.get("/campaigns/{campaign_id}", include_in_schema=False)
        def web_app(proposal_id: str | None = None, campaign_id: str | None = None) -> FileResponse:
            del proposal_id
            del campaign_id
            return FileResponse(WEB_ROOT / "index.html")

    return app
