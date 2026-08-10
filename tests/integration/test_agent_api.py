from __future__ import annotations

import asyncio
import base64
import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.models import AuditEvent, CommandReceipt, User
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.service import TradingService


def encryption_key() -> str:
    return base64.urlsafe_b64encode(b"agent-api-integration-key-32bytes"[:32]).decode().rstrip("=")


def agent_app(database: Database):
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret=secrets.token_urlsafe(32),
        credential_encryption_key=encryption_key(),
        public_base_url="http://test",
        runtime_sync_enabled=False,
        _env_file=None,
    )
    perptape = PerptapeClient(
        base_url="https://perptape.com",
        api_key=None,
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
        fetcher=lambda *_args: {"data": []},
    )
    return create_app(settings, database, perptape)


def test_agent_api_uses_team_rbac_independent_review_idempotency_and_token_rotation(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin_id = service.bootstrap_admin("agent-admin", now=now)
    instrument_id = service.register_instrument(
        actor_id=admin_id,
        venue="BINANCE",
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        lot_size=Decimal("0.001"),
        minimum_notional=Decimal("5"),
        contract_multiplier=Decimal("1"),
        quote_currency="USDT",
        collateral_currency="USDT",
        protection_supported=True,
        now=now,
    )
    app = agent_app(database)

    async def scenario() -> tuple[str, str, str, str]:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as admin:
            login = await admin.post("/api/auth/mock/login", json={"username": "agent-admin"})
            assert login.status_code == 200, login.text
            page = await admin.get("/admin/agents")
            assert page.status_code == 200
            assert 'href="/admin/agents"' in page.text
            account = await admin.post(
                "/api/exchange-accounts",
                json={
                    "account_id": "paper-agent-1",
                    "venue": "BINANCE",
                    "label": "Agent paper account",
                    "idempotency_key": "agent-account-create",
                },
            )
            assert account.status_code == 200, account.text
            exchange_account_id = account.json()["exchange_account_id"]

            invalid_scope = await admin.post(
                "/api/admin/agents",
                json={
                    "username": "invalid-scope-agent",
                    "roles": ["PROPOSER"],
                    "account_scope": "missing-account",
                    "venue_scope": "BINANCE",
                    "idempotency_key": "invalid-agent-scope",
                },
            )
            assert invalid_scope.status_code == 422
            assert invalid_scope.json()["error"]["code"] == "AGENT_SCOPE_INVALID"

            proposer = await admin.post(
                "/api/admin/agents",
                json={
                    "username": "proposal-model-agent",
                    "roles": ["PROPOSER"],
                    "account_scope": "paper-agent-1",
                    "venue_scope": "BINANCE",
                    "expires_in_days": 30,
                    "idempotency_key": "create-proposal-agent",
                },
            )
            assert proposer.status_code == 200, proposer.text
            proposer_result = proposer.json()["result"]
            proposer_token = proposer_result["token"]
            proposer_id = proposer_result["agent_id"]
            assert proposer_result["display_once"] is True

            replayed_create = await admin.post(
                "/api/admin/agents",
                json={
                    "username": "proposal-model-agent",
                    "roles": ["PROPOSER"],
                    "account_scope": "paper-agent-1",
                    "venue_scope": "BINANCE",
                    "expires_in_days": 30,
                    "idempotency_key": "create-proposal-agent",
                },
            )
            assert replayed_create.status_code == 200, replayed_create.text
            assert replayed_create.json()["result"]["token"] is None
            assert replayed_create.json()["result"]["display_once"] is False

            reviewer = await admin.post(
                "/api/admin/agents",
                json={
                    "username": "review-model-agent",
                    "roles": ["REVIEWER"],
                    "account_scope": "paper-agent-1",
                    "venue_scope": "BINANCE",
                    "expires_in_days": 30,
                    "idempotency_key": "create-review-agent",
                },
            )
            assert reviewer.status_code == 200, reviewer.text
            reviewer_result = reviewer.json()["result"]
            reviewer_token = reviewer_result["token"]
            reviewer_id = reviewer_result["agent_id"]

            listed = await admin.get("/api/admin/agents")
            assert listed.status_code == 200, listed.text
            assert proposer_token not in listed.text
            assert reviewer_token not in listed.text
            assert "agent_token_digest" not in listed.text
            assert {item["service_kind"] for item in listed.json()["data"]} == {"AGENT"}

            ambiguous = await admin.get(
                "/api/auth/session",
                headers={"Authorization": f"Bearer {proposer_token}"},
            )
            assert ambiguous.status_code == 400
            assert ambiguous.json()["error"]["code"] == "AUTH_CREDENTIAL_AMBIGUOUS"

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {proposer_token}"},
        ) as proposer_client:
            session = await proposer_client.get("/api/auth/session")
            assert session.status_code == 200, session.text
            assert session.json()["authentication_method"] == "agent-token-v1"
            assert session.json()["session"]["principal_type"] == "SERVICE"
            assert session.json()["session"]["service_kind"] == "AGENT"

            general_proposal = await proposer_client.post(
                "/api/opportunities/not-an-agent-contract/proposals",
                json={
                    "risk_tier": "LOW",
                    "account_id": "paper-agent-1",
                    "quantity": "0.01",
                    "max_risk": "25",
                    "invalidation_price": "95000",
                    "rationale": "Agent must use the attributed proposal endpoint",
                },
            )
            assert general_proposal.status_code == 403
            assert (
                general_proposal.json()["error"]["code"]
                == "AGENT_PROPOSAL_ENDPOINT_REQUIRED"
            )

            proposal_payload = {
                "environment": "SHADOW",
                "account_id": "paper-agent-1",
                "venue": "BINANCE",
                "instrument_id": str(instrument_id),
                "direction": "LONG",
                "risk_tier": "LOW",
                "quantity": "0.01",
                "max_risk": "25",
                "expires_in_minutes": 480,
                "trigger_price": "100000",
                "invalidation_price": "95000",
                "limit_price": "99900",
                "model_id": "alpha-breakout",
                "model_version": "2026.08.1",
                "request_id": "request-00000001",
                "generated_at": datetime.now(UTC).isoformat(),
                "rationale": "current model facts support a frozen long proposal",
                "idempotency_key": "agent-proposal-0001",
            }
            proposed = await proposer_client.post("/api/agent/proposals", json=proposal_payload)
            assert proposed.status_code == 200, proposed.text
            assert proposed.json()["status"] == "PENDING_REVIEW"
            proposal_id = proposed.json()["proposal_id"]
            assert proposed.json()["detail"]["source"] == "SYSTEM"
            assert proposed.json()["detail"]["proposer_id"] == proposer_id

            proposed_replay = await proposer_client.post(
                "/api/agent/proposals", json=proposal_payload
            )
            assert proposed_replay.status_code == 200, proposed_replay.text
            assert proposed_replay.json()["proposal_id"] == proposal_id

            proposal_conflict = await proposer_client.post(
                "/api/agent/proposals",
                json={**proposal_payload, "max_risk": "26"},
            )
            assert proposal_conflict.status_code == 409
            assert proposal_conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

            self_review = await proposer_client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={
                    "decision": "APPROVE",
                    "reason": "self review must remain blocked",
                    "expected_version": 2,
                    "idempotency_key": "agent-self-review",
                },
            )
            assert self_review.status_code == 403
            assert self_review.json()["error"]["code"] == "SELF_REVIEW_FORBIDDEN"

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {reviewer_token}"},
        ) as reviewer_client:
            missing_key = await reviewer_client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={
                    "decision": "APPROVE",
                    "reason": "review independent frozen proposal",
                    "expected_version": 2,
                },
            )
            assert missing_key.status_code == 422
            assert missing_key.json()["error"]["code"] == "AGENT_IDEMPOTENCY_REQUIRED"

            reviewed = await reviewer_client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={
                    "decision": "APPROVE",
                    "reason": "review independent frozen proposal",
                    "expected_version": 2,
                    "idempotency_key": "agent-review-0001",
                },
            )
            assert reviewed.status_code == 200, reviewed.text
            assert reviewed.json()["status"] == "APPROVED"

            reviewed_replay = await reviewer_client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={
                    "decision": "APPROVE",
                    "reason": "review independent frozen proposal",
                    "expected_version": 2,
                    "idempotency_key": "agent-review-0001",
                },
            )
            assert reviewed_replay.status_code == 200, reviewed_replay.text
            assert reviewed_replay.json()["status"] == "APPROVED"

            step_up = await reviewer_client.post(
                "/api/auth/mock/step-up",
                json={
                    "action": "proposal.approve",
                    "object_id": proposal_id,
                    "object_version": 3,
                },
            )
            assert step_up.status_code == 403
            assert step_up.json()["error"]["code"] == "AGENT_STEP_UP_FORBIDDEN"

            risk = await reviewer_client.post(
                f"/api/proposals/{proposal_id}/risk-decisions",
                json={"idempotency_key": "agent-risk-denied"},
            )
            assert risk.status_code == 403
            assert risk.json()["error"]["code"] == "RBAC_DENIED"

            credentials = await reviewer_client.put(
                f"/api/exchange-accounts/{exchange_account_id}/credentials",
                json={
                    "credentials": {"api_key": "x", "api_secret": "secret"},
                    "expected_version": 1,
                    "idempotency_key": "agent-credential-denied",
                },
            )
            assert credentials.status_code == 403
            assert credentials.json()["error"]["code"] == "RBAC_DENIED"

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as anonymous:
            invalid = await anonymous.get(
                "/api/auth/session",
                headers={"Authorization": "Bearer tradingops_agent_v1.invalid.short"},
            )
            assert invalid.status_code == 401
            assert invalid.json()["error"]["code"] == "AGENT_TOKEN_INVALID"

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as admin:
            login = await admin.post("/api/auth/mock/login", json={"username": "agent-admin"})
            assert login.status_code == 200
            rotated = await admin.post(
                f"/api/admin/agents/{reviewer_id}/token-rotations",
                json={
                    "expected_token_version": 1,
                    "expires_in_days": 30,
                    "idempotency_key": "rotate-review-agent",
                },
            )
            assert rotated.status_code == 200, rotated.text
            new_reviewer_token = rotated.json()["result"]["token"]
            assert rotated.json()["result"]["token_version"] == 2
            rotated_replay = await admin.post(
                f"/api/admin/agents/{reviewer_id}/token-rotations",
                json={
                    "expected_token_version": 1,
                    "expires_in_days": 30,
                    "idempotency_key": "rotate-review-agent",
                },
            )
            assert rotated_replay.status_code == 200
            assert rotated_replay.json()["result"]["token"] is None

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as anonymous:
            old = await anonymous.get(
                "/api/auth/session",
                headers={"Authorization": f"Bearer {reviewer_token}"},
            )
            assert old.status_code == 401
            current = await anonymous.get(
                "/api/auth/session",
                headers={"Authorization": f"Bearer {new_reviewer_token}"},
            )
            assert current.status_code == 200, current.text

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as admin:
            login = await admin.post("/api/auth/mock/login", json={"username": "agent-admin"})
            assert login.status_code == 200
            disabled = await admin.put(
                f"/api/admin/agents/{reviewer_id}/access",
                json={
                    "roles": [],
                    "active": False,
                    "account_scope": "paper-agent-1",
                    "venue_scope": "BINANCE",
                    "expected_auth_version": 2,
                    "idempotency_key": "disable-review-agent",
                },
            )
            assert disabled.status_code == 200, disabled.text
            assert disabled.json()["result"]["active"] is False

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as anonymous:
            revoked = await anonymous.get(
                "/api/auth/session",
                headers={"Authorization": f"Bearer {new_reviewer_token}"},
            )
            assert revoked.status_code == 401

        return proposer_token, reviewer_token, new_reviewer_token, reviewer_id

    proposer_token, reviewer_token, new_reviewer_token, reviewer_id = asyncio.run(scenario())

    with database.session_factory() as session:
        reviewer = session.get(User, UUID(reviewer_id))
        audits = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type.in_(
                    [
                        "AGENT_CREATED",
                        "AGENT_TOKEN_ROTATED",
                        "AGENT_ACCESS_UPDATED",
                        "PROPOSAL_CREATED",
                        "PROPOSAL_REVIEWED",
                    ]
                )
            )
        ).all()
        receipts = session.scalars(select(CommandReceipt)).all()
        serialized = json.dumps(
            {
                "audits": [item.reason for item in audits],
                "receipts": [item.response for item in receipts],
            },
            sort_keys=True,
        )
        assert reviewer is not None
        assert reviewer.agent_token_digest is not None
        assert reviewer.agent_token_digest not in {
            proposer_token,
            reviewer_token,
            new_reviewer_token,
        }
        for token in (proposer_token, reviewer_token, new_reviewer_token):
            assert token not in serialized
        assert any(item.actor_id == reviewer_id for item in audits)
