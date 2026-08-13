from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from conftest import add_exchange_account_fixture, set_test_team_environment
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from trading_control_plane.api import create_app
from trading_control_plane.binance import (
    BinanceEquity,
    BinanceFill,
    BinanceInstrument,
    BinanceOrder,
    BinancePosition,
    BinanceReadOnlySnapshot,
)
from trading_control_plane.binance_execution import BinanceTestnetOrder
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    CampaignStatus,
    CapabilityStatus,
    Direction,
    DomainRejected,
    ExecutionEnvironment,
    IntentKind,
    ProposalSource,
    ReviewDecision,
    RiskTier,
    Role,
    SystemRiskState,
)
from trading_control_plane.models import (
    CapabilityGate,
    OrderIntent,
    TradingAuthorization,
    VenueOrder,
)
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.service import TradingService
from trading_control_plane.telegram import MockTelegramGateway


class AcceptedBinanceTestnetClient:
    configured = True

    def ensure_order(self, command: Any, *, now: datetime) -> BinanceTestnetOrder:
        return BinanceTestnetOrder(
            order_id=f"fixture-{command.client_order_id}",
            client_order_id=command.client_order_id,
            status="SENT",
            side=command.side,
            order_type="MARKET",
            ordered_quantity=command.quantity,
            filled_quantity=Decimal(0),
            stop_price=Decimal(0),
            reduce_only=command.reduce_only,
            close_position=False,
            observed_at=now,
        )


def record_accepted_testnet_order(
    service: TradingService,
    intent_id: UUID,
    actor_id: UUID,
    execution_scope: str,
    owner_id: str,
    fencing_token: int,
    *,
    now: datetime,
) -> BinanceTestnetOrder:
    command = service.prepare_binance_testnet_send(
        intent_id,
        actor_id,
        execution_scope,
        owner_id,
        fencing_token,
        now=now,
    )
    result = AcceptedBinanceTestnetClient().ensure_order(command, now=now)
    service.record_binance_testnet_order(
        intent_id,
        actor_id,
        execution_scope,
        owner_id,
        fencing_token,
        command,
        result,
        now=now,
    )
    return result


def ingest_testnet_fills(
    service: TradingService,
    intent_id: UUID,
    actor_id: UUID,
    fills: tuple[tuple[str, Decimal], ...],
    *,
    position_quantity: Decimal,
    now: datetime,
) -> None:
    with service.database.session_factory() as session:
        order = session.scalar(select(VenueOrder).where(VenueOrder.order_intent_id == intent_id))
        assert order is not None
        ordered_quantity = order.ordered_quantity
        side = order.side
        order_id = order.venue_order_id
        client_order_id = order.client_order_id
    filled_quantity = sum((quantity for _, quantity in fills), Decimal(0))
    status = "FILLED" if filled_quantity == ordered_quantity else "PARTIALLY_FILLED"
    service.ingest_binance_read_only_snapshot(
        "acct-1",
        actor_id,
        BinanceReadOnlySnapshot(
            symbol="BTCUSDT",
            observed_at=now,
            instrument=BinanceInstrument(
                "BTCUSDT",
                Decimal("0.1"),
                Decimal("0.001"),
                Decimal("5"),
                "USDT",
                "USDT",
                True,
            ),
            orders=(
                BinanceOrder(
                    order_id,
                    client_order_id,
                    status,
                    side,
                    "MARKET",
                    ordered_quantity,
                    filled_quantity,
                    Decimal(0),
                    False,
                    False,
                    now,
                ),
            ),
            fills=tuple(
                BinanceFill(
                    fill_id,
                    order_id,
                    side,
                    quantity,
                    Decimal("110"),
                    Decimal(0),
                    "USDT",
                    now,
                )
                for fill_id, quantity in fills
            ),
            position=BinancePosition(position_quantity, Decimal("100"), Decimal("110"), now),
            equity=BinanceEquity(Decimal("10000"), Decimal("9000"), "USDT", now),
            funding=(),
            protection=None,
        ),
        environment=ExecutionEnvironment.TESTNET,
        now=now,
    )


def perptape_payload(observed_at: datetime) -> dict[str, Any]:
    timestamp = int(observed_at.timestamp() * 1_000)
    return {
        "type": "breakouts",
        "generatedAt": timestamp,
        "data": [
            {
                "exchange": "BN",
                "symbol": "BTCUSDT",
                "canonicalSymbol": "BTC-USDT",
                "direction": "HH",
                "timeframe": "1h",
                "price": "110",
                "breakoutPrice": "109",
                "threshold": "105",
                "updatedAt": timestamp,
                "triggeredAt": timestamp,
                "klineReadiness": {"status": "ready"},
            }
        ],
    }


def seed_campaign(database: Database) -> dict[str, UUID]:
    service = TradingService(database)
    now = datetime.now(UTC) - timedelta(seconds=2)
    admin = service.bootstrap_admin("admin", now=now)
    set_test_team_environment(database, admin, "TESTNET")
    add_exchange_account_fixture(database, admin, "acct-1", "BINANCE")
    proposer = service.create_user("proposer", admin, now=now)
    reviewer = service.create_user("reviewer", admin, now=now)
    operator = service.create_user("operator", admin, now=now)
    for user_id, role in (
        (proposer, Role.PROPOSER),
        (reviewer, Role.REVIEWER),
        (operator, Role.OPERATOR),
    ):
        service.assign_role(user_id, role, admin, "acct-1", "BINANCE", now=now)
    instrument = service.register_instrument(
        actor_id=admin,
        venue="BINANCE",
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        lot_size=Decimal("0.001"),
        minimum_notional=Decimal("5"),
        contract_multiplier=Decimal(1),
        quote_currency="USDT",
        collateral_currency="USDT",
        protection_supported=True,
        now=now,
    )
    service.set_risk_policy(
        actor_id=admin,
        version="m6-risk-v1",
        system_state=SystemRiskState.NORMAL,
        max_total_risk=Decimal("100"),
        max_account_risk=Decimal("100"),
        max_single_loss=Decimal("100"),
        max_consecutive_losses=3,
        loss_cooldown=timedelta(hours=1),
        max_fact_age=timedelta(minutes=5),
        now=now,
    )
    service.record_position(
        "acct-1",
        "BINANCE",
        instrument,
        Decimal(0),
        Decimal(0),
        Decimal("100"),
        True,
        operator,
        now=now,
    )
    service.record_account_equity(
        "acct-1",
        "BINANCE",
        Decimal("10000"),
        Decimal("9000"),
        "USDT",
        True,
        operator,
        now=now,
    )
    proposal = service.create_proposal(
        actor_id=proposer,
        source=ProposalSource.MANUAL,
        risk_tier=RiskTier.LOW,
        account_id="acct-1",
        venue="BINANCE",
        instrument_id=instrument,
        direction=Direction.LONG,
        quantity=Decimal(1),
        max_risk=Decimal("40"),
        expires_at=now + timedelta(hours=2),
        idempotency_key="m6-proposal",
        details={
            "initial_quantity": "0.5",
            "invalidation_price": "95",
            "allow_auto_add": True,
            "requested_adds": 1,
            "add_trigger_price": "105",
        },
        now=now,
    )
    service.submit_proposal(proposal, proposer, now=now)
    service.review_proposal(proposal, reviewer, ReviewDecision.APPROVE, "reviewed", now=now)
    service.decide_risk(
        proposal_id=proposal,
        actor_id=operator,
        kind=IntentKind.INITIAL,
        idempotency_key="m6-risk",
        now=now,
    )
    with database.session_factory.begin() as session:
        gate = session.get(CapabilityGate, "AUTO_ADD", with_for_update=True)
        assert gate is not None
        gate.status = CapabilityStatus.ENABLED.value
        gate.reason = "M6 integration fixture precondition"
        gate.operator_id = str(admin)
        gate.version += 1
        gate.updated_at = now
    with pytest.raises(DomainRejected, match="AUTHORIZATION_ADD_LIMIT_INVALID"):
        service.issue_authorization(
            proposal_id=proposal,
            actor_id=operator,
            expires_at=now + timedelta(minutes=30),
            allowed_adds=2,
            idempotency_key="m6-invalid-low-risk-adds",
            now=now,
        )
    authorization = service.issue_authorization(
        proposal_id=proposal,
        actor_id=operator,
        expires_at=now + timedelta(minutes=30),
        allowed_adds=1,
        idempotency_key="m6-authorization",
        now=now,
    )
    opening = service.create_order_intent(
        authorization,
        operator,
        IntentKind.INITIAL,
        "acct-1",
        "BINANCE",
        instrument,
        Direction.LONG,
        Decimal("0.5"),
        "m6-opening",
        now=now,
    )
    token = service.acquire_sender("TESTNET:acct-1:BINANCE", "m6-worker", operator, now)
    record_accepted_testnet_order(
        service,
        opening.intent_id,
        operator,
        "TESTNET:acct-1:BINANCE",
        "m6-worker",
        token,
        now=now,
    )
    ingest_testnet_fills(
        service,
        opening.intent_id,
        operator,
        (("m6-opening-fill", Decimal("0.5")),),
        position_quantity=Decimal("0.5"),
        now=now,
    )
    position = service.record_position(
        "acct-1",
        "BINANCE",
        instrument,
        Decimal("0.5"),
        Decimal("100"),
        Decimal("110"),
        True,
        operator,
        now=now,
    )
    service.record_protection(
        position,
        "m6-stop",
        Decimal("0.5"),
        Decimal("95"),
        True,
        operator,
        now=now,
    )
    return {
        "admin": admin,
        "operator": operator,
        "instrument": instrument,
        "proposal": proposal,
        "authorization": authorization,
        "campaign": opening.campaign_id,
        "position": position,
    }


def build_app(
    database: Database,
    perptape: PerptapeClient,
    telegram: MockTelegramGateway,
) -> FastAPI:
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="m6-test-signing-secret-that-is-long-enough",  # noqa: S106
        public_base_url="http://test",
        execution_backend="DIRECT_LEGACY",
        binance_testnet_order_send_enabled=True,
        binance_testnet_api_key="fixture-key",
        binance_testnet_api_secret="fixture-secret",  # noqa: S106
        _env_file=None,
    )
    return create_app(
        settings,
        database,
        perptape,
        telegram,
        binance_testnet_client=AcceptedBinanceTestnetClient(),  # type: ignore[arg-type]
    )


async def login(client: AsyncClient) -> None:
    response = await client.post("/api/auth/mock/login", json={"username": "operator"})
    assert response.status_code == 200, response.text


async def run_m6_flow(database: Database) -> None:
    ids = seed_campaign(database)
    candidate_time = datetime.now(UTC)
    perptape = PerptapeClient(
        base_url="https://perptape.com",
        api_key="contract-only-not-a-secret",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
        fetcher=lambda _url, _headers, _timeout: perptape_payload(candidate_time),
    )
    telegram = MockTelegramGateway()
    async with AsyncClient(
        transport=ASGITransport(app=build_app(database, perptape, telegram)),
        base_url="http://test",
    ) as client:
        await login(client)
        campaign_path = f"/api/campaigns/{ids['campaign']}"
        detail = await client.get(campaign_path)
        assert detail.status_code == 200, detail.text
        assert detail.json()["management"] == {
            "auto_add_gate": "ENABLED",
            "allow_auto_add": True,
            "initial_quantity": "0.5",
            "add_trigger_price": "105",
            "requested_adds": 1,
            "remaining_quantity": "0.500000000000000000",
            "remaining_adds": 1,
        }

        candidates = await client.get(f"{campaign_path}/add-candidates")
        assert candidates.status_code == 200, candidates.text
        candidate_id = candidates.json()["data"][0]["candidate_id"]
        add_payload = {
            "candidate_id": candidate_id,
            "quantity": "0.25",
            "idempotency_key": "m6-auto-add",
        }
        created = await client.post(f"{campaign_path}/auto-add", json=add_payload)
        duplicate = await client.post(f"{campaign_path}/auto-add", json=add_payload)
        conflict = await client.post(
            f"{campaign_path}/auto-add",
            json={**add_payload, "quantity": "0.20"},
        )
        assert created.status_code == duplicate.status_code == 200
        assert duplicate.json()["intent_id"] == created.json()["intent_id"]
        assert conflict.status_code == 409
        add_intent_id = UUID(created.json()["intent_id"])
        with database.session_factory() as session:
            authorization = session.get(TradingAuthorization, ids["authorization"])
            intent = session.get(OrderIntent, add_intent_id)
            assert authorization is not None and authorization.used_adds == 0
            assert intent is not None and not intent.add_unit_consumed
            assert intent.trigger_source == f"PERPTAPE:{candidate_id}"

        lease = await client.post(
            "/api/sender-leases",
            json={
                "execution_scope": "TESTNET:acct-1:BINANCE",
                "owner_id": "m6-worker",
                "lease_seconds": 120,
            },
        )
        assert lease.status_code == 200, lease.text
        send = await client.post(
            f"/api/intents/{add_intent_id}/binance-testnet/send",
            json={
                "execution_scope": "TESTNET:acct-1:BINANCE",
                "owner_id": "m6-worker",
                "fencing_token": lease.json()["fencing_token"],
            },
        )
        assert send.status_code == 200, send.text
        cumulative_fills: list[tuple[str, Decimal]] = []
        for fill_id, quantity in (
            ("m6-add-fill-1", Decimal("0.1")),
            ("m6-add-fill-2", Decimal("0.15")),
        ):
            cumulative_fills.append((fill_id, quantity))
            ingest_testnet_fills(
                TradingService(database),
                add_intent_id,
                ids["operator"],
                tuple(cumulative_fills),
                position_quantity=Decimal("0.5")
                + sum((item[1] for item in cumulative_fills), Decimal(0)),
                now=datetime.now(UTC),
            )
            with database.session_factory() as session:
                authorization = session.get(TradingAuthorization, ids["authorization"])
                assert authorization is not None and authorization.used_adds == 1

        position = await client.post(
            "/api/facts/positions",
            json={
                "account_id": "acct-1",
                "venue": "BINANCE",
                "instrument_id": str(ids["instrument"]),
                "quantity": "0.75",
                "average_entry_price": "103.333333333333333333",
                "mark_price": "110",
                "known": True,
            },
        )
        assert position.status_code == 200, position.text
        protection = await client.post(
            f"{campaign_path}/protection",
            json={
                "position_id": position.json()["position_id"],
                "venue_order_id": "m6-stop-expanded",
                "quantity": "0.75",
                "trigger_price": "95",
                "fully_covered": True,
                "known": True,
            },
        )
        assert protection.status_code == 200, protection.text

        reduction_payload = {
            "target_quantity": "0.60",
            "urgency": "URGENT",
            "reason": "Web risk reduction",
            "idempotency_key": "m6-web-reduction",
        }
        reduction = await client.post(f"{campaign_path}/managed-reductions", json=reduction_payload)
        reduction_duplicate = await client.post(
            f"{campaign_path}/managed-reductions", json=reduction_payload
        )
        reduction_conflict = await client.post(
            f"{campaign_path}/managed-reductions",
            json={**reduction_payload, "target_quantity": "0.50"},
        )
        assert reduction.status_code == reduction_duplicate.status_code == 200
        assert reduction_duplicate.json()["intent_id"] == reduction.json()["intent_id"]
        assert reduction_conflict.status_code == 409
        assert reduction.json()["detail"]["intents"][-1]["reduce_only"] is True
        assert reduction.json()["detail"]["intents"][-1]["quantity"] == "0.150000000000000000"
        release = await client.post(
            f"/api/intents/{reduction.json()['intent_id']}/release",
            json={"terminal_status": "CANCELLED", "reason": "confirmed zero fill"},
        )
        assert release.status_code == 200, release.text

        notifications = await client.get("/api/telegram/mock/notifications")
        assert notifications.status_code == 200, notifications.text
        assert notifications.json()["scope"] == "PROPOSAL_REVIEW_ONLY"
        assert "campaign_data" not in notifications.json()
        delivered_notification = next(
            item
            for item in reversed(telegram.campaign_notifications())
            if item.campaign_version == 1
        )
        assert delivered_notification.status == CampaignStatus.REDUCING.value
        assert delivered_notification.action_references == ()
        telegram_action = await client.post(
            "/api/telegram/mock/campaign-actions",
            json={
                "action": "EMERGENCY_REDUCE",
                "action_reference": "not-issued",
                "campaign_version": 1,
                "idempotency_key": "m6-telegram-reduce",
                "target_quantity": "0.50",
            },
        )
        assert telegram_action.status_code == 404

        before_trigger = await client.post(
            f"{campaign_path}/automatic-exit",
            json={"idempotency_key": "m6-exit-not-triggered"},
        )
        assert before_trigger.status_code == 200, before_trigger.text
        assert before_trigger.json()["triggered"] is False
        assert before_trigger.json()["reason"] == "EXIT_TRIGGER_NOT_MET"
        mark_invalidated = await client.post(
            "/api/facts/positions",
            json={
                "account_id": "acct-1",
                "venue": "BINANCE",
                "instrument_id": str(ids["instrument"]),
                "quantity": "0.75",
                "average_entry_price": "103.333333333333333333",
                "mark_price": "94",
                "known": True,
            },
        )
        assert mark_invalidated.status_code == 200, mark_invalidated.text
        no_op_duplicate = await client.post(
            f"{campaign_path}/automatic-exit",
            json={"idempotency_key": "m6-exit-not-triggered"},
        )
        assert no_op_duplicate.status_code == 200, no_op_duplicate.text
        assert no_op_duplicate.json()["triggered"] is False
        exit_payload = {"idempotency_key": "m6-automatic-exit"}
        automatic_exit = await client.post(f"{campaign_path}/automatic-exit", json=exit_payload)
        automatic_exit_duplicate = await client.post(
            f"{campaign_path}/automatic-exit", json=exit_payload
        )
        assert automatic_exit.status_code == automatic_exit_duplicate.status_code == 200
        assert automatic_exit.json()["triggered"] is True
        assert automatic_exit.json()["reason"] == "FROZEN_INVALIDATION_REACHED"
        assert automatic_exit_duplicate.json()["intent_id"] == automatic_exit.json()["intent_id"]
        exit_intent = automatic_exit.json()["detail"]["intents"][-1]
        assert exit_intent["kind"] == "EXIT"
        assert exit_intent["reduce_only"] is True
        assert exit_intent["quantity"] == "0.750000000000000000"

        scoped_global_pause = await client.post(
            "/api/operations/pause-new-risk",
            json={"reason": "scoped operator", "idempotency_key": "m6-scoped-pause"},
        )
        assert scoped_global_pause.status_code == 403
        await client.post("/api/auth/logout")
        admin_login = await client.post("/api/auth/mock/login", json={"username": "admin"})
        assert admin_login.status_code == 200, admin_login.text
        global_add = await client.post(
            "/api/operations/auto-add/disable",
            json={"reason": "administrator halt", "idempotency_key": "m6-global-add-off"},
        )
        pause = await client.post(
            "/api/operations/pause-new-risk",
            json={"reason": "administrator pause", "idempotency_key": "m6-pause-new-risk"},
        )
        pause_duplicate = await client.post(
            "/api/operations/pause-new-risk",
            json={"reason": "administrator pause", "idempotency_key": "m6-pause-new-risk"},
        )
        assert global_add.status_code == 200, global_add.text
        assert global_add.json()["status"] == "DISABLED"
        assert pause.status_code == pause_duplicate.status_code == 200
        assert pause.json()["system_state"] == "REDUCE_ONLY"


def test_m6_auto_add_reduction_exit_and_telegram_contract(database: Database) -> None:
    asyncio.run(run_m6_flow(database))


async def run_perptape_failure_does_not_block_exit(database: Database) -> None:
    ids = seed_campaign(database)
    service = TradingService(database)
    now = datetime.now(UTC)
    service.record_position(
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Decimal("0.5"),
        Decimal("100"),
        Decimal("94"),
        True,
        ids["operator"],
        now=now,
    )
    unavailable = PerptapeClient(
        base_url="https://perptape.com",
        api_key=None,
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
    )
    async with AsyncClient(
        transport=ASGITransport(app=build_app(database, unavailable, MockTelegramGateway())),
        base_url="http://test",
    ) as client:
        await login(client)
        candidate_response = await client.get(f"/api/campaigns/{ids['campaign']}/add-candidates")
        exit_response = await client.post(
            f"/api/campaigns/{ids['campaign']}/automatic-exit",
            json={"idempotency_key": "m6-perptape-down-exit"},
        )
        assert candidate_response.status_code == 503
        assert candidate_response.json()["error"]["code"] == "PERPTAPE_NOT_CONFIGURED"
        assert exit_response.status_code == 200, exit_response.text
        assert exit_response.json()["triggered"] is True


def test_perptape_unavailable_stops_add_but_not_frozen_exit(database: Database) -> None:
    asyncio.run(run_perptape_failure_does_not_block_exit(database))
