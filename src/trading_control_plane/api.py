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
    AuthorizationRequest,
    ManualProposalRequest,
    MockLoginRequest,
    MockStepUpRequest,
    ReviewRequest,
    RiskDecisionRequest,
    SystemProposalRequest,
)
from trading_control_plane.auth import SessionIdentity, SignedTokenService
from trading_control_plane.config import Settings, get_settings
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    DomainRejected,
    ExecutionEnvironment,
    IntentKind,
    ProposalSource,
    ProposalStatus,
    ReviewDecision,
)
from trading_control_plane.logging import configure_logging
from trading_control_plane.metrics import DATABASE_READY
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.queries import TradingQueries
from trading_control_plane.service import TradingService
from trading_control_plane.telegram import MockTelegramGateway, ProposalNotification

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
    if code in {"PERPTAPE_UNAVAILABLE", "PERPTAPE_NOT_CONFIGURED"}:
        return status.HTTP_503_SERVICE_UNAVAILABLE
    return status.HTTP_422_UNPROCESSABLE_ENTITY


def create_app(
    settings: Settings | None = None,
    database: ReadinessDatabase | None = None,
    perptape_client: PerptapeClient | None = None,
    telegram_gateway: MockTelegramGateway | None = None,
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

    @app.exception_handler(DomainRejected)
    async def domain_rejected(_: Request, exc: DomainRejected) -> JSONResponse:
        return JSONResponse(
            status_code=_domain_status(exc.code),
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.detail,
                    "retryable": exc.code in {"PERPTAPE_UNAVAILABLE"},
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
        return {"transport": "MOCK_ONLY", "data": data}

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
        @app.get("/proposals/{proposal_id}", include_in_schema=False)
        def web_app(proposal_id: str | None = None) -> FileResponse:
            del proposal_id
            return FileResponse(WEB_ROOT / "index.html")

    return app
