from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
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
from trading_control_plane.domain import Role, SystemRiskState
from trading_control_plane.models import (
    AuditEvent,
    Proposal,
    RiskDecision,
    RiskPolicy,
    SignalEvent,
    TeamSignalSource,
)
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.service import TradingService


def encryption_key() -> str:
    raw = b"team-signal-source-test-key-32byt"[:32]
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


async def login(client: AsyncClient, username: str) -> None:
    response = await client.post("/api/auth/mock/login", json={"username": username})
    assert response.status_code == 200, response.text


def signature(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    signed = timestamp.encode() + b"." + nonce.encode() + b"." + body
    return "v1=" + hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


def signal_app(database: Database, fetch_headers: list[dict[str, str]]):
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    def fetcher(_url: str, headers: dict[str, str], _timeout: float):
        fetch_headers.append(headers)
        return {
            "type": "breakouts",
            "generatedAt": now_ms,
            "rateLimit": {"nextAllowedAt": now_ms},
            "data": [],
        }

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
        fetcher=fetcher,
    )
    return create_app(settings, database, perptape)


def test_team_signal_source_perptape_key_and_signed_webhook_flow(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("signal-admin", now=now)
    instrument_id = service.register_instrument(
        actor_id=admin,
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
    service.create_exchange_account(
        actor_id=admin,
        account_id="signal-account",
        venue="BINANCE",
        label="Signal Account",
        credentials=None,
        idempotency_key="signal-account-create",
        now=now,
    )
    service.set_risk_policy(
        actor_id=admin,
        version="signal-report-risk-v1",
        system_state=SystemRiskState.NORMAL,
        max_total_risk=Decimal("100"),
        max_account_risk=Decimal("100"),
        max_single_loss=Decimal("20"),
        max_consecutive_losses=3,
        loss_cooldown=timedelta(hours=1),
        max_fact_age=timedelta(minutes=5),
        now=now,
    )
    proposer = service.create_user("signal-proposer", admin, now=now)
    service.assign_role(
        proposer,
        Role.PROPOSER,
        admin,
        "signal-account",
        "BINANCE",
        now=now,
    )
    fetched_headers: list[dict[str, str]] = []

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=signal_app(database, fetched_headers)),
            base_url="http://test",
        ) as client:
            await login(client, "signal-admin")
            legacy = await client.get("/api/signal-source")
            assert legacy.status_code == 200, legacy.text
            assert legacy.json()["source"]["mode"] == "PERPTAPE"
            assert legacy.json()["source"]["credential"]["state"] == "RUNTIME_FALLBACK"

            perptape_key = "team-perptape-key-123"
            configured_perptape = await client.put(
                "/api/signal-source",
                json={
                    "mode": "PERPTAPE",
                    "secret": perptape_key,
                    "enabled": True,
                    "webhook_max_age_seconds": 300,
                    "expected_version": 1,
                    "idempotency_key": "signal-source-perptape-v2",
                },
            )
            assert configured_perptape.status_code == 200, configured_perptape.text
            assert configured_perptape.json()["source"]["credential"]["state"] == "CONFIGURED"
            assert perptape_key not in configured_perptape.text
            opportunities = await client.get("/api/opportunities")
            assert opportunities.status_code == 200, opportunities.text
            assert fetched_headers[-1]["x-api-key"] == perptape_key

            webhook_secret = secrets.token_urlsafe(32)
            configured_webhook = await client.put(
                "/api/signal-source",
                json={
                    "mode": "WEBHOOK",
                    "secret": webhook_secret,
                    "enabled": True,
                    "webhook_max_age_seconds": 300,
                    "expected_version": 2,
                    "idempotency_key": "signal-source-webhook-v3",
                },
            )
            assert configured_webhook.status_code == 200, configured_webhook.text
            source = configured_webhook.json()["source"]
            assert source["mode"] == "WEBHOOK"
            assert source["webhook"]["automatic_proposal_supported"] is False
            assert source["webhook"]["endpoint_url"].startswith("http://test/api/webhooks/")
            assert webhook_secret not in configured_webhook.text
            source_id = source["signal_source_id"]

            signal_at = datetime.now(UTC)
            payload = {
                "payload_version": 1,
                "provider": "TRADINGVIEW",
                "external_id": "tv-btc-001",
                "strategy_id": "breakout-model",
                "strategy_version": "2026.08",
                "venue": "BINANCE",
                "symbol": "BTCUSDT",
                "direction": "LONG",
                "signal_at": signal_at.isoformat(),
                "timeframe": "1h",
                "reference_price": "100",
                "metadata": {"alert": "breakout"},
            }
            body = json.dumps(payload, separators=(",", ":")).encode()
            request_timestamp = str(int(signal_at.timestamp()))
            nonce = "nonce-signal-0000001"
            headers = {
                "content-type": "application/json",
                "x-tradingops-timestamp": request_timestamp,
                "x-tradingops-nonce": nonce,
                "x-tradingops-signature": signature(
                    webhook_secret, request_timestamp, nonce, body
                ),
                "idempotency-key": "signal-event-001",
            }
            accepted = await client.post(
                f"/api/webhooks/signals/{source_id}", content=body, headers=headers
            )
            assert accepted.status_code == 202, accepted.text
            assert accepted.json()["proposal_created"] is False
            event_id = accepted.json()["signal_event_id"]

            replay = await client.post(
                f"/api/webhooks/signals/{source_id}", content=body, headers=headers
            )
            assert replay.status_code == 200, replay.text
            assert replay.json() == {
                "signal_event_id": event_id,
                "status": "ACCEPTED",
                "replayed": True,
                "proposal_created": False,
            }

            replay_headers = {
                **headers,
                "idempotency-key": "signal-event-replay",
            }
            nonce_replay = await client.post(
                f"/api/webhooks/signals/{source_id}",
                content=body,
                headers=replay_headers,
            )
            assert nonce_replay.status_code == 409, nonce_replay.text
            assert nonce_replay.json()["error"]["code"] == "SIGNAL_REPLAY_DETECTED"

            bad_signature = await client.post(
                f"/api/webhooks/signals/{source_id}",
                content=body,
                headers={
                    **headers,
                    "x-tradingops-nonce": "nonce-signal-0000002",
                    "x-tradingops-signature": "v1=" + "0" * 64,
                    "idempotency-key": "signal-event-bad-signature",
                },
            )
            assert bad_signature.status_code == 401, bad_signature.text
            assert bad_signature.json()["error"]["code"] == "SIGNAL_SIGNATURE_INVALID"

            conflict_payload = {**payload, "external_id": "tv-btc-002"}
            conflict_body = json.dumps(conflict_payload, separators=(",", ":")).encode()
            conflict_nonce = "nonce-signal-0000003"
            semantic_conflict = await client.post(
                f"/api/webhooks/signals/{source_id}",
                content=conflict_body,
                headers={
                    **headers,
                    "x-tradingops-nonce": conflict_nonce,
                    "x-tradingops-signature": signature(
                        webhook_secret,
                        request_timestamp,
                        conflict_nonce,
                        conflict_body,
                    ),
                },
            )
            assert semantic_conflict.status_code == 409, semantic_conflict.text
            assert semantic_conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

            stale_payload = {
                **payload,
                "external_id": "tv-btc-stale",
                "signal_at": (signal_at - timedelta(minutes=10)).isoformat(),
            }
            stale_body = json.dumps(stale_payload, separators=(",", ":")).encode()
            stale_nonce = "nonce-signal-0000004"
            stale = await client.post(
                f"/api/webhooks/signals/{source_id}",
                content=stale_body,
                headers={
                    **headers,
                    "x-tradingops-nonce": stale_nonce,
                    "x-tradingops-signature": signature(
                        webhook_secret,
                        request_timestamp,
                        stale_nonce,
                        stale_body,
                    ),
                    "idempotency-key": "signal-event-stale",
                },
            )
            assert stale.status_code == 422, stale.text
            assert stale.json()["error"]["code"] == "SIGNAL_STALE"

            invalid_body = b'{"payload_version":1,"provider":"TRADINGVIEW"}'
            invalid = await client.post(
                f"/api/webhooks/signals/{source_id}",
                content=invalid_body,
                headers={
                    **headers,
                    "x-tradingops-nonce": "nonce-signal-0000005",
                    "x-tradingops-signature": signature(
                        webhook_secret,
                        request_timestamp,
                        "nonce-signal-0000005",
                        invalid_body,
                    ),
                    "idempotency-key": "signal-event-invalid",
                },
            )
            assert invalid.status_code == 422, invalid.text
            assert invalid.json()["error"]["code"] == "SIGNAL_PAYLOAD_INVALID"

            await login(client, "signal-proposer")
            scoped_status = await client.get("/api/signal-source")
            assert scoped_status.status_code == 200, scoped_status.text
            assert scoped_status.json()["can_manage"] is False
            listed = await client.get("/api/signals")
            assert listed.status_code == 200, listed.text
            assert [item["signal_event_id"] for item in listed.json()["data"]] == [event_id]
            assert listed.json()["data"][0]["proposal"] is None

            proposal_request = {
                "environment": "SHADOW",
                "account_id": "signal-account",
                "instrument_id": str(instrument_id),
                "risk_tier": "LOW",
                "quantity": "0.01",
                "max_risk": "1",
                "expires_in_minutes": 480,
                "rationale": "Human accepted this frozen Webhook signal for review.",
                "idempotency_key": "signal-proposal-001",
            }
            created = await client.post(
                f"/api/signals/{event_id}/proposals",
                json=proposal_request,
            )
            assert created.status_code == 200, created.text
            assert created.json()["status"] == "PENDING_REVIEW"
            assert created.json()["signal_event_id"] == event_id
            proposal_id = created.json()["proposal_id"]
            created_replay = await client.post(
                f"/api/signals/{event_id}/proposals",
                json=proposal_request,
            )
            assert created_replay.status_code == 200, created_replay.text
            assert created_replay.json()["proposal_id"] == proposal_id

            consumed = await client.post(
                f"/api/signals/{event_id}/proposals",
                json={
                    "environment": "SHADOW",
                    "account_id": "signal-account",
                    "instrument_id": str(instrument_id),
                    "risk_tier": "LOW",
                    "quantity": "0.01",
                    "max_risk": "1",
                    "expires_in_minutes": 480,
                    "rationale": "A second proposal must not be created from this signal.",
                    "idempotency_key": "signal-proposal-002",
                },
            )
            assert consumed.status_code == 409, consumed.text
            assert consumed.json()["error"]["code"] == "SIGNAL_ALREADY_CONSUMED"

            with database.session_factory() as session:
                persisted_source = session.get(TeamSignalSource, UUID(source_id))
                persisted_event = session.get(SignalEvent, UUID(event_id))
                proposal = session.get(Proposal, UUID(proposal_id))
                audit_reasons = session.scalars(
                    select(AuditEvent.reason).where(
                        AuditEvent.object_type.in_(("TeamSignalSource", "SignalEvent"))
                    )
                ).all()
                assert persisted_source is not None
                assert persisted_source.credential_ciphertext is not None
                assert webhook_secret not in persisted_source.credential_ciphertext
                assert persisted_event is not None
                assert persisted_event.status == "PROPOSAL_CREATED"
                assert proposal is not None and proposal.signal_event_id == UUID(event_id)
                assert proposal.status == "PENDING_REVIEW"
                assert proposal.version == 2
                assert proposal.frozen_at is not None
                assert webhook_secret not in "".join(audit_reasons)

            with database.session_factory.begin() as session:
                proposal = session.get(Proposal, UUID(proposal_id))
                assert proposal is not None
                policy = session.scalar(
                    select(RiskPolicy).where(
                        RiskPolicy.team_id == proposal.team_id,
                        RiskPolicy.active,
                    )
                )
                assert policy is not None
                session.add(
                    RiskDecision(
                        team_id=proposal.team_id,
                        proposal_id=proposal.proposal_id,
                        policy_id=policy.policy_id,
                        input_data={"fixture": "webhook-denial"},
                        result="DENY",
                        approved_quantity=Decimal(0),
                        risk_amount=Decimal(0),
                        reasons=["STALE_FACTS"],
                        data_as_of=signal_at,
                        actor_id=str(admin),
                        correlation_id=proposal.correlation_id,
                        created_at=signal_at,
                    )
                )

            await login(client, "signal-admin")
            report = await client.get(
                "/api/results",
                params={
                    "environment": "SHADOW",
                    "strategy_id": "breakout-model",
                    "strategy_version": "2026.08",
                    "signal_source_mode": "WEBHOOK",
                    "signal_provider": "TRADINGVIEW",
                },
            )
            assert report.status_code == 200, report.text
            report_data = report.json()["data"]
            assert report_data["campaigns"] == []
            assert len(report_data["risk_events"]) == 1
            risk_event = report_data["risk_events"][0]
            assert risk_event["strategy_id"] == "breakout-model"
            assert risk_event["strategy_version"] == "2026.08"
            assert risk_event["signal_source_mode"] == "WEBHOOK"
            assert risk_event["signal_source_id"] == source_id
            assert risk_event["signal_provider"] == "TRADINGVIEW"
            assert risk_event["attribution"] == "FROZEN_SIGNAL_EVENT"
            signal_groups = report_data["dimensions"]["signal_source"]
            assert signal_groups[0]["risk_event_count"] == 1
            assert signal_groups[0]["risk_events_by_result"] == {"DENY": 1}
            assert report_data["coverage"]["percentage_metrics"] == (
                "OPENING_CAPITAL_UNAVAILABLE"
            )

    asyncio.run(scenario())


def test_signal_events_are_hidden_across_team_switches(database: Database) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("signal-scope-admin", now=now)
    original_context = service.signal_source_status(admin)
    original_team_id = UUID(original_context["source"]["team_id"])
    workspace_id = UUID(original_context["source"]["workspace_id"])
    second_team_id = service.create_team(
        actor_id=admin,
        name="Signal Team Two",
        slug="signal-team-two",
        idempotency_key="signal-team-two-create",
        now=now,
    )
    assert second_team_id != original_team_id
    assert service.signal_source_status(admin)["source"] is None
    assert service.list_signal_events(admin) == []

    service.select_scope(
        actor_id=admin,
        workspace_id=workspace_id,
        team_id=original_team_id,
        idempotency_key="signal-return-original",
        now=now + timedelta(seconds=1),
    )
    assert service.signal_source_status(admin)["source"]["mode"] == "PERPTAPE"
