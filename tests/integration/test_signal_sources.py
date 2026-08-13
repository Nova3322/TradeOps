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

import pytest
from conftest import set_test_team_environment
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    DomainRejected,
    Role,
    SignalSourceMode,
    SystemRiskState,
)
from trading_control_plane.models import (
    AuditEvent,
    Proposal,
    RiskDecision,
    RiskPolicy,
    RoleAssignment,
    SignalEvent,
    TeamSignalSource,
    User,
)
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.queries import TradingQueries
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
    set_test_team_environment(database, admin, "TESTNET")
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
        environment="TESTNET",
        account_id="signal-account",
        venue="BINANCE",
        label="Signal Account",
        credentials={"api_key": "signal-testnet-key", "api_secret": "signal-testnet-secret"},
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
            assert configured_perptape.json()["source"]["runtime"]["state"] == "WAITING"
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
                "x-tradingops-signature": signature(webhook_secret, request_timestamp, nonce, body),
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
                "environment": "TESTNET",
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
                    "environment": "TESTNET",
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
                    "environment": "TESTNET",
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
            assert report_data["coverage"]["percentage_metrics"] == ("OPENING_CAPITAL_UNAVAILABLE")

    asyncio.run(scenario())


def test_multiple_webhooks_coexist_with_perptape_and_retain_source_history(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("multi-signal-admin", now=now)
    set_test_team_environment(database, admin, "TESTNET")
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
    original_context = service.signal_source_status(admin)
    workspace_id = UUID(original_context["source"]["workspace_id"])
    original_team_id = UUID(original_context["source"]["team_id"])
    fetched_headers: list[dict[str, str]] = []

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=signal_app(database, fetched_headers)),
            base_url="http://test",
        ) as client:
            await login(client, "multi-signal-admin")
            initial = await client.get("/api/signal-sources")
            assert initial.status_code == 200, initial.text
            perptape = initial.json()["data"][0]
            assert perptape["mode"] == "PERPTAPE"
            assert perptape["enabled"] is True
            perptape_key = "multi-source-perptape-key"
            configured_perptape = await client.put(
                "/api/signal-source",
                json={
                    "mode": "PERPTAPE",
                    "secret": perptape_key,
                    "enabled": True,
                    "webhook_max_age_seconds": 300,
                    "expected_version": perptape["version"],
                    "idempotency_key": "configure-multi-source-perptape",
                },
            )
            assert configured_perptape.status_code == 200, configured_perptape.text
            assert perptape_key not in configured_perptape.text
            perptape = configured_perptape.json()["source"]

            first_secret = secrets.token_urlsafe(32)
            first_create_body = {
                "name": "TradingView BTC",
                "mode": "WEBHOOK",
                "secret": first_secret,
                "enabled": True,
                "webhook_max_age_seconds": 180,
                "expected_version": 0,
                "idempotency_key": "multi-webhook-first",
            }
            first = await client.post("/api/signal-sources", json=first_create_body)
            assert first.status_code == 201, first.text
            assert first.headers["cache-control"] == "no-store"
            assert first.json()["one_time_secret"] == first_secret
            first_source = first.json()["source"]
            first_id = first_source["signal_source_id"]

            create_replay = await client.post("/api/signal-sources", json=first_create_body)
            assert create_replay.status_code == 200, create_replay.text
            assert create_replay.json()["source"]["signal_source_id"] == first_id
            assert create_replay.json()["one_time_secret"] is None

            second = await client.post(
                "/api/signal-sources",
                json={
                    "name": "Model ETH",
                    "mode": "WEBHOOK",
                    "enabled": True,
                    "webhook_max_age_seconds": 300,
                    "expected_version": 0,
                    "idempotency_key": "multi-webhook-second",
                },
            )
            assert second.status_code == 201, second.text
            second_secret = second.json()["one_time_secret"]
            assert isinstance(second_secret, str) and len(second_secret) >= 32
            second_id = second.json()["source"]["signal_source_id"]
            assert second_id != first_id

            listed = await client.get("/api/signal-sources")
            assert listed.status_code == 200, listed.text
            sources = listed.json()["data"]
            assert {(item["mode"], item["name"]) for item in sources} == {
                ("PERPTAPE", "Perptape"),
                ("WEBHOOK", "TradingView BTC"),
                ("WEBHOOK", "Model ETH"),
            }
            assert all(item["enabled"] for item in sources)
            assert first_secret not in listed.text
            assert second_secret not in listed.text

            perptape_test = await client.post(
                f"/api/signal-sources/{perptape['signal_source_id']}/tests",
                json={
                    "expected_version": perptape["version"],
                    "idempotency_key": "test-perptape-source",
                },
            )
            assert perptape_test.status_code == 200, perptape_test.text
            assert perptape_test.json()["status"] == "SUCCESS"

            webhook_test = await client.post(
                f"/api/signal-sources/{first_id}/tests",
                json={
                    "expected_version": 1,
                    "idempotency_key": "test-first-webhook",
                },
            )
            assert webhook_test.status_code == 200, webhook_test.text
            assert webhook_test.json()["status"] == "SUCCESS"
            assert webhook_test.json()["version"] == 2
            webhook_test_replay = await client.post(
                f"/api/signal-sources/{first_id}/tests",
                json={
                    "expected_version": 1,
                    "idempotency_key": "test-first-webhook",
                },
            )
            assert webhook_test_replay.status_code == 200, webhook_test_replay.text
            assert webhook_test_replay.json()["version"] == 2

            rotated_secret = secrets.token_urlsafe(32)
            rotation_body = {
                "secret": rotated_secret,
                "expected_version": 2,
                "idempotency_key": "rotate-first-webhook",
            }
            rotated = await client.post(
                f"/api/signal-sources/{first_id}/credential-rotations",
                json=rotation_body,
            )
            assert rotated.status_code == 200, rotated.text
            assert rotated.json()["one_time_secret"] == rotated_secret
            assert rotated.json()["source"]["credential"]["version"] == 2
            rotation_replay = await client.post(
                f"/api/signal-sources/{first_id}/credential-rotations",
                json=rotation_body,
            )
            assert rotation_replay.status_code == 200, rotation_replay.text
            assert rotation_replay.json()["one_time_secret"] is None

            version_conflict = await client.put(
                f"/api/signal-sources/{first_id}",
                json={
                    "name": "TradingView BTC",
                    "webhook_max_age_seconds": 240,
                    "expected_version": 2,
                    "idempotency_key": "stale-webhook-update",
                },
            )
            assert version_conflict.status_code == 409, version_conflict.text
            assert version_conflict.json()["error"]["code"] == "VERSION_CONFLICT"

            signal_at = datetime.now(UTC)
            payload = {
                "payload_version": 1,
                "provider": "TRADINGVIEW",
                "external_id": "multi-source-btc-001",
                "strategy_id": "multi-source",
                "strategy_version": "v1",
                "venue": "BINANCE",
                "symbol": "BTCUSDT",
                "direction": "LONG",
                "signal_at": signal_at.isoformat(),
                "timeframe": "1h",
                "reference_price": "100",
                "metadata": {"retained": True},
            }
            body = json.dumps(payload, separators=(",", ":")).encode()
            request_timestamp = str(int(signal_at.timestamp()))
            nonce = "multi-source-nonce-0001"
            accepted = await client.post(
                f"/api/webhooks/signals/{first_id}",
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-tradingops-timestamp": request_timestamp,
                    "x-tradingops-nonce": nonce,
                    "x-tradingops-signature": signature(
                        rotated_secret, request_timestamp, nonce, body
                    ),
                    "idempotency-key": "multi-source-event-001",
                },
            )
            assert accepted.status_code == 202, accepted.text
            event_id = UUID(accepted.json()["signal_event_id"])
            accepted_by_second_source = await client.post(
                f"/api/webhooks/signals/{second_id}",
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-tradingops-timestamp": request_timestamp,
                    "x-tradingops-nonce": nonce,
                    "x-tradingops-signature": signature(
                        second_secret, request_timestamp, nonce, body
                    ),
                    "idempotency-key": "multi-source-event-001",
                },
            )
            assert accepted_by_second_source.status_code == 202, accepted_by_second_source.text
            second_event_id = accepted_by_second_source.json()["signal_event_id"]
            assert second_event_id != str(event_id)

            webhook_page = await client.get("/api/webhook-signals")
            assert webhook_page.status_code == 200, webhook_page.text
            webhook_payload = webhook_page.json()
            assert webhook_payload["total"] == 2
            assert {
                (item["signal_source_id"], item["signal_event_id"])
                for item in webhook_payload["data"]
            } == {(first_id, str(event_id)), (second_id, second_event_id)}
            assert {
                (item["signal_source_id"], item["name"]) for item in webhook_payload["sources"]
            } == {(first_id, "TradingView BTC"), (second_id, "Model ETH")}
            assert all(
                item["proposal_eligibility"] == "ELIGIBLE"
                and item["freshness"]["status"] == "CURRENT"
                for item in webhook_payload["data"]
            )

            first_source_page = await client.get(
                "/api/webhook-signals",
                params={
                    "signal_source_id": first_id,
                    "venue": "BINANCE",
                    "symbol": "BTCUSDT",
                    "direction": "LONG",
                    "timeframe": "1h",
                    "freshness": "CURRENT",
                    "proposal_eligibility": "ELIGIBLE",
                },
            )
            assert first_source_page.status_code == 200, first_source_page.text
            assert first_source_page.json()["total"] == 1
            assert first_source_page.json()["data"][0]["signal_event_id"] == str(event_id)

            with database.session_factory.begin() as session:
                second_event = session.get(SignalEvent, UUID(second_event_id))
                assert second_event is not None
                second_event.occurred_at = signal_at - timedelta(minutes=10)

            stale_source_page = await client.get(
                "/api/webhook-signals",
                params={
                    "signal_source_id": second_id,
                    "freshness": "STALE",
                    "proposal_eligibility": "BLOCKED",
                },
            )
            assert stale_source_page.status_code == 200, stale_source_page.text
            assert stale_source_page.json()["total"] == 1
            stale_signal = stale_source_page.json()["data"][0]
            assert stale_signal["signal_event_id"] == second_event_id
            assert stale_signal["freshness"]["status"] == "STALE"
            assert stale_signal["proposal_blocker"] == "SIGNAL_STALE"

            stale_proposal = await client.post(
                f"/api/signals/{second_event_id}/proposals",
                json={
                    "environment": "TESTNET",
                    "account_id": "signal-account",
                    "instrument_id": str(instrument_id),
                    "risk_tier": "LOW",
                    "quantity": "0.01",
                    "max_risk": "1",
                    "expires_in_minutes": 480,
                    "rationale": "A stale Webhook signal must remain blocked server-side.",
                    "idempotency_key": "stale-multi-source-proposal",
                },
            )
            assert stale_proposal.status_code == 422, stale_proposal.text
            assert stale_proposal.json()["error"]["code"] == "SIGNAL_STALE"

            after_signal = await client.get("/api/signal-sources")
            first_after_signal = next(
                item for item in after_signal.json()["data"] if item["signal_source_id"] == first_id
            )
            assert first_after_signal["signals"]["count"] == 1
            assert first_after_signal["signals"]["last_received_at"] is not None
            assert first_after_signal["webhook"]["last_valid_event_at"] is not None
            second_after_signal = next(
                item
                for item in after_signal.json()["data"]
                if item["signal_source_id"] == second_id
            )
            assert second_after_signal["signals"]["count"] == 1

            service.create_team(
                actor_id=admin,
                name="Other Signal Space",
                slug="other-signal-space",
                idempotency_key="create-other-signal-space",
                now=now + timedelta(seconds=1),
            )
            cross_space = await client.post(
                f"/api/signal-sources/{first_id}/state",
                json={
                    "enabled": False,
                    "expected_version": 3,
                    "idempotency_key": "cross-space-disable",
                },
            )
            assert cross_space.status_code == 404, cross_space.text
            assert cross_space.json()["error"]["code"] == "SIGNAL_SOURCE_NOT_FOUND"
            isolated_page = await client.get("/api/webhook-signals")
            assert isolated_page.status_code == 200, isolated_page.text
            assert isolated_page.json()["sources"] == []
            assert isolated_page.json()["data"] == []
            assert isolated_page.json()["total"] == 0
            service.select_scope(
                actor_id=admin,
                workspace_id=workspace_id,
                team_id=original_team_id,
                idempotency_key="return-to-original-signal-space",
                now=now + timedelta(seconds=2),
            )

            disabled = await client.post(
                f"/api/signal-sources/{first_id}/state",
                json={
                    "enabled": False,
                    "expected_version": 3,
                    "idempotency_key": "disable-first-webhook",
                },
            )
            assert disabled.status_code == 200, disabled.text
            disabled_source = next(
                item for item in disabled.json()["data"] if item["signal_source_id"] == first_id
            )
            assert disabled_source["enabled"] is False
            assert disabled_source["version"] == 4

            enabled = await client.post(
                f"/api/signal-sources/{first_id}/state",
                json={
                    "enabled": True,
                    "expected_version": 4,
                    "idempotency_key": "reenable-first-webhook",
                },
            )
            assert enabled.status_code == 200, enabled.text

            deleted = await client.request(
                "DELETE",
                f"/api/signal-sources/{first_id}",
                json={
                    "confirm_name": "TradingView BTC",
                    "expected_version": 5,
                    "idempotency_key": "delete-first-webhook",
                },
            )
            assert deleted.status_code == 200, deleted.text
            assert first_id not in {item["signal_source_id"] for item in deleted.json()["data"]}

            after_delete = await client.post(
                f"/api/webhooks/signals/{first_id}",
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-tradingops-timestamp": request_timestamp,
                    "x-tradingops-nonce": "multi-source-nonce-0002",
                    "x-tradingops-signature": signature(
                        rotated_secret,
                        request_timestamp,
                        "multi-source-nonce-0002",
                        body,
                    ),
                    "idempotency-key": "multi-source-event-after-delete",
                },
            )
            assert after_delete.status_code == 404, after_delete.text

            with database.session_factory() as session:
                retained_source = session.get(TeamSignalSource, UUID(first_id))
                retained_event = session.get(SignalEvent, event_id)
                events = set(
                    session.scalars(
                        select(AuditEvent.event_type).where(AuditEvent.object_id == first_id)
                    ).all()
                )
                assert retained_source is not None
                assert retained_source.deleted_at is not None
                assert retained_source.enabled is False
                assert retained_event is not None
                assert retained_event.signal_source_id == retained_source.signal_source_id
                assert {
                    "SIGNAL_SOURCE_CREATED",
                    "SIGNAL_SOURCE_TEST_SUCCEEDED",
                    "SIGNAL_SOURCE_CREDENTIAL_ROTATED",
                    "SIGNAL_SOURCE_DISABLED",
                    "SIGNAL_SOURCE_ENABLED",
                    "SIGNAL_SOURCE_DELETED",
                } <= events

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


def test_perptape_runtime_bindings_are_encrypted_versioned_and_team_scoped(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(database, credential_encryption_key=encryption_key())
    admin = service.bootstrap_admin("perptape-binding-admin", now=now)
    context = TradingQueries(database).user_context(admin)
    workspace_id = UUID(context["active_workspace"]["workspace_id"])
    first_team_id = UUID(context["active_team"]["team_id"])
    service.configure_signal_source(
        actor_id=admin,
        mode=SignalSourceMode.PERPTAPE,
        secret="first-team-perptape-key",  # noqa: S106
        enabled=True,
        webhook_max_age_seconds=300,
        expected_version=1,
        idempotency_key="configure-first-perptape-binding",
        now=now,
    )
    second_team_id = service.create_team(
        actor_id=admin,
        name="Perptape Binding Two",
        slug="perptape-binding-two",
        idempotency_key="create-perptape-binding-two",
        now=now + timedelta(seconds=1),
    )
    service.configure_signal_source(
        actor_id=admin,
        mode=SignalSourceMode.PERPTAPE,
        secret="second-team-perptape-key",  # noqa: S106
        enabled=True,
        webhook_max_age_seconds=300,
        expected_version=0,
        idempotency_key="configure-second-perptape-binding",
        now=now + timedelta(seconds=1),
    )

    bindings = service.perptape_runtime_bindings()
    assert len(bindings) == 2
    by_team = {item.team_id: item for item in bindings}
    assert set(by_team) == {first_team_id, second_team_id}
    assert by_team[first_team_id].api_key == "first-team-perptape-key"
    assert by_team[second_team_id].api_key == "second-team-perptape-key"
    assert all("perptape-key" not in repr(item) for item in bindings)

    first_binding = by_team[first_team_id]
    service.select_scope(
        actor_id=admin,
        workspace_id=workspace_id,
        team_id=first_team_id,
        idempotency_key="return-first-perptape-binding",
        now=now + timedelta(seconds=2),
    )
    service.configure_signal_source(
        actor_id=admin,
        mode=SignalSourceMode.PERPTAPE,
        secret="first-team-rotated-key",  # noqa: S106
        enabled=True,
        webhook_max_age_seconds=300,
        expected_version=2,
        idempotency_key="rotate-first-perptape-binding",
        now=now + timedelta(seconds=3),
    )
    with pytest.raises(DomainRejected, match="SIGNAL_RUNTIME_BINDING_CHANGED"):
        service.validate_perptape_runtime_binding(first_binding)
    with pytest.raises(DomainRejected, match="SIGNAL_RUNTIME_BINDING_CHANGED"):
        service.record_runtime_source_health(
            first_binding.service_principal_id,
            {"PERPTAPE": {"status": "FAILED", "items_observed": 0}},
            perptape_runtime_binding=first_binding,
            now=now + timedelta(seconds=3, milliseconds=100),
        )

    service.configure_signal_source(
        actor_id=admin,
        mode=SignalSourceMode.PERPTAPE,
        secret="first-team-disabled-key",  # noqa: S106
        enabled=False,
        webhook_max_age_seconds=300,
        expected_version=3,
        idempotency_key="disable-first-perptape-binding",
        now=now + timedelta(seconds=3, milliseconds=200),
    )
    with database.session_factory() as session:
        principal = session.get(User, first_binding.service_principal_id)
        assert principal is not None and principal.active is False
    service.configure_signal_source(
        actor_id=admin,
        mode=SignalSourceMode.PERPTAPE,
        secret="first-team-reenabled-key",  # noqa: S106
        enabled=True,
        webhook_max_age_seconds=300,
        expected_version=4,
        idempotency_key="reenable-first-perptape-binding",
        now=now + timedelta(seconds=3, milliseconds=300),
    )
    with database.session_factory() as session:
        principal = session.get(User, first_binding.service_principal_id)
        assert principal is not None and principal.active is True

    second_binding = by_team[second_team_id]
    with database.session_factory.begin() as session:
        session.add(
            RoleAssignment(
                user_id=second_binding.service_principal_id,
                team_id=second_team_id,
                role=Role.OBSERVER.value,
                account_scope=None,
                venue_scope=None,
                created_at=now + timedelta(seconds=4),
            )
        )
    with pytest.raises(DomainRejected, match="SIGNAL_SERVICE_PRINCIPAL_INVALID"):
        service.perptape_runtime_bindings()
