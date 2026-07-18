from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from trading_control_plane.api import create_app
from trading_control_plane.capital import MockCapitalTransferAdapter
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    CapitalDirection,
    CapitalTransferStatus,
    DomainRejected,
    ExecutionEnvironment,
    IdempotencyConflict,
    ReviewDecision,
    Role,
    SystemRiskState,
)
from trading_control_plane.perptape import PerptapeClient
from trading_control_plane.queries import TradingQueries
from trading_control_plane.service import TradingService
from trading_control_plane.telegram import MockTelegramGateway


def seed(service: TradingService) -> dict[str, UUID]:
    now = datetime.now(UTC) - timedelta(seconds=2)
    admin = service.bootstrap_admin("admin", now=now)
    proposer = service.create_user("treasury-proposer", admin, now=now)
    reviewer_one = service.create_user("treasury-reviewer-1", admin, now=now)
    reviewer_two = service.create_user("treasury-reviewer-2", admin, now=now)
    operator = service.create_user("operator", admin, now=now)
    for user_id in (proposer, reviewer_one, reviewer_two):
        service.assign_role(user_id, Role.TREASURY_ADMIN, admin, now=now)
    service.assign_role(operator, Role.OPERATOR, admin, "acct-1", "BINANCE", now=now)
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
        version="m7-risk-v1",
        system_state=SystemRiskState.NORMAL,
        max_total_risk=Decimal("100"),
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
        environment=ExecutionEnvironment.TESTNET,
        now=now,
    )
    service.record_capital_balance(
        actor_id=proposer,
        environment=ExecutionEnvironment.TESTNET,
        location_type="VAULT",
        location_id="vault-1",
        venue="BINANCE",
        equity=Decimal("1000"),
        available_balance=Decimal("1000"),
        withdrawable_balance=Decimal("1000"),
        asset="USDT",
        control_status="CONTROLLED",
        deposit_status="READY",
        network="TESTNET",
        address_reference="masked-vault",
        known=True,
        observed_at=now,
        now=now,
    )
    service.record_capital_balance(
        actor_id=proposer,
        environment=ExecutionEnvironment.TESTNET,
        location_type="VENUE",
        location_id="acct-1",
        venue="BINANCE",
        equity=Decimal("500"),
        available_balance=Decimal("500"),
        withdrawable_balance=Decimal("500"),
        asset="USDT",
        control_status="READ_ONLY",
        deposit_status="READY",
        network="TESTNET",
        address_reference="masked-venue",
        known=True,
        observed_at=now,
        now=now,
    )
    return {
        "admin": admin,
        "proposer": proposer,
        "reviewer_one": reviewer_one,
        "reviewer_two": reviewer_two,
        "operator": operator,
        "instrument": instrument,
    }


def approved_transfer(
    service: TradingService,
    ids: dict[str, UUID],
    *,
    direction: CapitalDirection,
    key: str,
    now: datetime,
) -> tuple[UUID, UUID]:
    proposal = service.create_transfer_proposal(
        actor_id=ids["proposer"],
        environment=ExecutionEnvironment.TESTNET,
        direction=direction,
        account_id="acct-1",
        venue="BINANCE",
        vault_id="vault-1",
        asset="USDT",
        network="TESTNET",
        destination_reference="approved-test-destination",
        amount=Decimal("100"),
        max_fee=Decimal("1"),
        min_received=Decimal("99"),
        reason="manual next-cycle allocation",
        expires_at=now + timedelta(hours=2),
        idempotency_key=key,
        now=now,
    )
    service.submit_transfer_proposal(proposal, ids["proposer"], now=now)
    with pytest.raises(DomainRejected, match="SELF_REVIEW_FORBIDDEN"):
        service.review_transfer_proposal(
            proposal,
            ids["proposer"],
            ReviewDecision.APPROVE,
            "self review",
            2,
            now=now,
        )
    assert (
        service.review_transfer_proposal(
            proposal,
            ids["reviewer_one"],
            ReviewDecision.APPROVE,
            "first independent review",
            2,
            now=now,
        ).value
        == "PENDING_REVIEW"
    )
    assert (
        service.review_transfer_proposal(
            proposal,
            ids["reviewer_two"],
            ReviewDecision.APPROVE,
            "second independent review",
            3,
            now=now,
        ).value
        == "APPROVED"
    )
    with pytest.raises(DomainRejected, match="CAPITAL_DUTY_SEPARATION_REQUIRED"):
        service.issue_transfer_authorization(
            proposal,
            ids["proposer"],
            now + timedelta(minutes=30),
            f"{key}-auth-self",
            now=now,
        )
    authorization = service.issue_transfer_authorization(
        proposal,
        ids["reviewer_one"],
        now + timedelta(minutes=30),
        f"{key}-auth",
        now=now,
    )
    return proposal, authorization


def test_bidirectional_mock_capital_transfer_preserves_unique_ownership(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    now = datetime.now(UTC)
    proposal, authorization = approved_transfer(
        service,
        ids,
        direction=CapitalDirection.VAULT_TO_VENUE,
        key="m7-vault-to-venue",
        now=now,
    )
    assert (
        service.create_transfer_proposal(
            actor_id=ids["proposer"],
            environment=ExecutionEnvironment.TESTNET,
            direction=CapitalDirection.VAULT_TO_VENUE,
            account_id="acct-1",
            venue="BINANCE",
            vault_id="vault-1",
            asset="USDT",
            network="TESTNET",
            destination_reference="approved-test-destination",
            amount=Decimal("100"),
            max_fee=Decimal("1"),
            min_received=Decimal("99"),
            reason="manual next-cycle allocation",
            expires_at=now + timedelta(hours=2),
            idempotency_key="m7-vault-to-venue",
            now=now,
        )
        == proposal
    )
    with pytest.raises(IdempotencyConflict):
        service.create_transfer_proposal(
            actor_id=ids["proposer"],
            environment=ExecutionEnvironment.TESTNET,
            direction=CapitalDirection.VAULT_TO_VENUE,
            account_id="acct-1",
            venue="BINANCE",
            vault_id="vault-1",
            asset="USDT",
            network="TESTNET",
            destination_reference="approved-test-destination",
            amount=Decimal("101"),
            max_fee=Decimal("1"),
            min_received=Decimal("99"),
            reason="manual next-cycle allocation",
            expires_at=now + timedelta(hours=2),
            idempotency_key="m7-vault-to-venue",
            now=now,
        )

    transfer = service.reserve_capital_transfer(
        authorization, ids["reviewer_one"], "m7-execute", now=now
    )
    center = TradingQueries(database).capital_center(ids["reviewer_one"])
    vault = next(item for item in center["balances"] if item["location_type"] == "VAULT")
    assert vault["source_reserved"] == "100.000000000000000000"
    assert vault["effective_available"] == "900.000000000000000000"
    assert center["in_transit"] == "100.000000000000000000"

    adapter = MockCapitalTransferAdapter()
    command = service.capital_transfer_command(transfer, ids["reviewer_one"], now=now)
    service.record_capital_submission(
        transfer,
        ids["reviewer_one"],
        adapter.submit(command, now=now),
        now=now,
    )
    assert (
        service.reserve_capital_transfer(authorization, ids["reviewer_one"], "m7-execute", now=now)
        == transfer
    )
    with pytest.raises(DomainRejected, match="TRANSFER_AUTHORIZATION_INACTIVE"):
        service.reserve_capital_transfer(
            authorization, ids["reviewer_one"], "m7-execute-new-key", now=now
        )

    service.record_capital_observation(
        transfer,
        ids["reviewer_one"],
        CapitalTransferStatus.UNKNOWN,
        now=now + timedelta(seconds=1),
    )
    unknown_center = TradingQueries(database).capital_center(ids["reviewer_one"])
    assert unknown_center["in_transit"] == "100.000000000000000000"
    with pytest.raises(DomainRejected, match="CAPITAL_TRANSFER_ALREADY_SUBMITTED"):
        service.capital_transfer_command(
            transfer, ids["reviewer_one"], now=now + timedelta(seconds=1)
        )
    service.record_capital_observation(
        transfer,
        ids["reviewer_one"],
        CapitalTransferStatus.IN_FLIGHT,
        transaction_reference="mock-tx-1",
        now=now + timedelta(seconds=2),
    )
    service.record_capital_observation(
        transfer,
        ids["reviewer_one"],
        CapitalTransferStatus.DESTINATION_CONFIRMED,
        transaction_reference="mock-tx-1",
        fee_amount=Decimal("1"),
        net_received=Decimal("99"),
        now=now + timedelta(seconds=3),
    )
    assert (
        service.reconcile_capital_transfer(
            transfer, ids["reviewer_one"], now=now + timedelta(seconds=3)
        )
        == "DIFFERENCE"
    )
    service.record_capital_balance(
        actor_id=ids["reviewer_one"],
        environment=ExecutionEnvironment.TESTNET,
        location_type="VAULT",
        location_id="vault-1",
        venue="BINANCE",
        equity=Decimal("900"),
        available_balance=Decimal("900"),
        withdrawable_balance=Decimal("900"),
        asset="USDT",
        control_status="CONTROLLED",
        deposit_status="READY",
        network="TESTNET",
        address_reference="masked-vault",
        known=True,
        observed_at=now + timedelta(seconds=4),
        now=now + timedelta(seconds=4),
    )
    service.record_capital_balance(
        actor_id=ids["reviewer_one"],
        environment=ExecutionEnvironment.TESTNET,
        location_type="VENUE",
        location_id="acct-1",
        venue="BINANCE",
        equity=Decimal("599"),
        available_balance=Decimal("599"),
        withdrawable_balance=Decimal("599"),
        asset="USDT",
        control_status="READ_ONLY",
        deposit_status="READY",
        network="TESTNET",
        address_reference="masked-venue",
        known=True,
        observed_at=now + timedelta(seconds=4),
        now=now + timedelta(seconds=4),
    )
    assert (
        service.reconcile_capital_transfer(
            transfer, ids["reviewer_one"], now=now + timedelta(seconds=5)
        )
        == "MATCH"
    )
    settled = TradingQueries(database).capital_transfer_detail(ids["reviewer_one"], transfer)
    assert settled["status"] == "SETTLED"
    assert TradingQueries(database).capital_center(ids["reviewer_one"])["in_transit"] == "0"

    _, reverse_authorization = approved_transfer(
        service,
        ids,
        direction=CapitalDirection.VENUE_TO_VAULT,
        key="m7-venue-to-vault",
        now=now + timedelta(seconds=6),
    )
    with pytest.raises(DomainRejected, match="CAPITAL_RECONCILIATION_REQUIRED"):
        service.reserve_capital_transfer(
            reverse_authorization,
            ids["reviewer_one"],
            "m7-reverse-before-reconcile",
            now=now + timedelta(seconds=6),
        )
    service.record_capital_scope_reconciliation(
        actor_id=ids["reviewer_one"],
        environment=ExecutionEnvironment.TESTNET,
        account_id="acct-1",
        venue="BINANCE",
        now=now + timedelta(seconds=7),
    )
    reverse = service.reserve_capital_transfer(
        reverse_authorization,
        ids["reviewer_one"],
        "m7-reverse-after-reconcile",
        now=now + timedelta(seconds=7),
    )
    assert reverse != transfer

    _, rescue_authorization = approved_transfer(
        service,
        ids,
        direction=CapitalDirection.VAULT_TO_VENUE,
        key="m7-active-position-rescue",
        now=now + timedelta(seconds=8),
    )
    service.record_position(
        "acct-1",
        "BINANCE",
        ids["instrument"],
        Decimal("0.1"),
        Decimal("100"),
        Decimal("99"),
        True,
        ids["operator"],
        environment=ExecutionEnvironment.TESTNET,
        now=now + timedelta(seconds=8),
    )
    with pytest.raises(DomainRejected, match="ACTIVE_POSITION_CAPITAL_RESCUE_FORBIDDEN"):
        service.reserve_capital_transfer(
            rescue_authorization,
            ids["reviewer_one"],
            "m7-rescue-forbidden",
            now=now + timedelta(seconds=8),
        )


def test_capital_contract_rejects_unsafe_facts_reviews_and_transitions(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    now = datetime.now(UTC)
    balance_arguments = {
        "actor_id": ids["proposer"],
        "environment": ExecutionEnvironment.TESTNET,
        "location_type": "VAULT",
        "location_id": "vault-2",
        "venue": "BINANCE",
        "equity": Decimal("100"),
        "available_balance": Decimal("100"),
        "withdrawable_balance": Decimal("100"),
        "asset": "USDT",
        "control_status": "CONTROLLED",
        "deposit_status": "READY",
        "network": "TESTNET",
        "address_reference": "masked-vault",
        "known": True,
        "observed_at": now,
        "now": now,
    }
    with pytest.raises(DomainRejected, match="CAPITAL_LOCATION_INVALID"):
        service.record_capital_balance(**{**balance_arguments, "location_type": "WALLET"})
    with pytest.raises(DomainRejected, match="CAPITAL_LIVE_FACT_DISABLED"):
        service.record_capital_balance(
            **{**balance_arguments, "environment": ExecutionEnvironment.LIVE}
        )
    with pytest.raises(DomainRejected, match="FACT_TIME_INVALID"):
        service.record_capital_balance(
            **{**balance_arguments, "observed_at": now + timedelta(seconds=1)}
        )
    with pytest.raises(DomainRejected, match="CAPITAL_BALANCE_INVALID"):
        service.record_capital_balance(
            **{**balance_arguments, "withdrawable_balance": Decimal("101")}
        )

    proposal_arguments = {
        "actor_id": ids["proposer"],
        "environment": ExecutionEnvironment.TESTNET,
        "direction": CapitalDirection.VAULT_TO_VENUE,
        "account_id": "acct-1",
        "venue": "BINANCE",
        "vault_id": "vault-1",
        "asset": "USDT",
        "network": "TESTNET",
        "destination_reference": "approved-test-destination",
        "amount": Decimal("100"),
        "max_fee": Decimal("1"),
        "min_received": Decimal("99"),
        "reason": "manual next-cycle allocation",
        "expires_at": now + timedelta(hours=1),
        "idempotency_key": "m7-unsafe-proposal",
        "now": now,
    }
    with pytest.raises(DomainRejected, match="CAPITAL_TRANSFER_LIVE_DISABLED"):
        service.create_transfer_proposal(
            **{**proposal_arguments, "environment": ExecutionEnvironment.LIVE}
        )
    with pytest.raises(DomainRejected, match="TRANSFER_PROPOSAL_EXPIRY_INVALID"):
        service.create_transfer_proposal(
            **{**proposal_arguments, "expires_at": now - timedelta(seconds=1)}
        )

    rejected = service.create_transfer_proposal(
        **{**proposal_arguments, "idempotency_key": "m7-rejected-proposal"}
    )
    service.submit_transfer_proposal(rejected, ids["proposer"], now=now)
    assert (
        service.review_transfer_proposal(
            rejected,
            ids["reviewer_one"],
            ReviewDecision.REJECT,
            "policy rejected",
            2,
            now=now,
        ).value
        == "REJECTED"
    )

    duplicate = service.create_transfer_proposal(
        **{**proposal_arguments, "idempotency_key": "m7-duplicate-review"}
    )
    service.submit_transfer_proposal(duplicate, ids["proposer"], now=now)
    service.review_transfer_proposal(
        duplicate,
        ids["reviewer_one"],
        ReviewDecision.APPROVE,
        "first review",
        2,
        now=now,
    )
    with pytest.raises(DomainRejected, match="REVIEW_ALREADY_RECORDED"):
        service.review_transfer_proposal(
            duplicate,
            ids["reviewer_one"],
            ReviewDecision.APPROVE,
            "duplicate review",
            3,
            now=now,
        )

    _, authorization = approved_transfer(
        service,
        ids,
        direction=CapitalDirection.VAULT_TO_VENUE,
        key="m7-transition-contract",
        now=now,
    )
    transfer = service.reserve_capital_transfer(
        authorization, ids["reviewer_one"], "m7-transition-execute", now=now
    )
    adapter = MockCapitalTransferAdapter()
    submission = adapter.submit(
        service.capital_transfer_command(transfer, ids["reviewer_one"], now=now), now=now
    )
    service.record_capital_submission(transfer, ids["reviewer_one"], submission, now=now)
    service.record_capital_submission(transfer, ids["reviewer_one"], submission, now=now)
    with pytest.raises(DomainRejected, match="CAPITAL_TRANSFER_TRANSITION_INVALID"):
        service.record_capital_observation(
            transfer,
            ids["reviewer_one"],
            CapitalTransferStatus.DESTINATION_CONFIRMED,
            fee_amount=Decimal("1"),
            net_received=Decimal("99"),
            now=now,
        )
    assert (
        service.record_capital_observation(
            transfer,
            ids["reviewer_one"],
            CapitalTransferStatus.IN_FLIGHT,
            now=now,
        )
        is CapitalTransferStatus.IN_FLIGHT
    )
    assert (
        service.record_capital_observation(
            transfer,
            ids["reviewer_one"],
            CapitalTransferStatus.IN_FLIGHT,
            now=now,
        )
        is CapitalTransferStatus.IN_FLIGHT
    )
    with pytest.raises(DomainRejected, match="CAPITAL_DESTINATION_AMOUNT_INVALID"):
        service.record_capital_observation(
            transfer,
            ids["reviewer_one"],
            CapitalTransferStatus.DESTINATION_CONFIRMED,
            fee_amount=Decimal("1"),
            net_received=Decimal("98"),
            now=now,
        )
    service.record_capital_observation(
        transfer,
        ids["reviewer_one"],
        CapitalTransferStatus.UNKNOWN,
        now=now,
    )
    assert service.reconcile_capital_transfer(transfer, ids["reviewer_one"], now=now) == "UNKNOWN"


def build_app(database: Database, telegram: MockTelegramGateway) -> FastAPI:
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="m7-test-signing-secret-that-is-long-enough",  # noqa: S106
        public_base_url="http://test",
        _env_file=None,
    )
    perptape = PerptapeClient(
        base_url="https://perptape.invalid",
        api_key=None,
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
    )
    return create_app(settings, database, perptape, telegram)


async def login(client: AsyncClient, username: str) -> None:
    response = await client.post("/api/auth/mock/login", json={"username": username})
    assert response.status_code == 200, response.text


def test_capital_api_requires_treasury_step_up_and_telegram_is_notification_only(
    database: Database, service: TradingService
) -> None:
    seed(service)
    telegram = MockTelegramGateway()

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=build_app(database, telegram)),
            base_url="http://test",
        ) as client:
            await login(client, "admin")
            denied = await client.get("/api/capital")
            assert denied.status_code == 403
            await client.post("/api/auth/logout")

            await login(client, "treasury-proposer")
            created = await client.post(
                "/api/capital/proposals",
                json={
                    "environment": "TESTNET",
                    "direction": "VAULT_TO_VENUE",
                    "account_id": "acct-1",
                    "venue": "BINANCE",
                    "vault_id": "vault-1",
                    "asset": "USDT",
                    "network": "TESTNET",
                    "destination_reference": "approved-test-destination",
                    "amount": "50",
                    "max_fee": "1",
                    "min_received": "49",
                    "reason": "manual test allocation",
                    "idempotency_key": "m7-api-proposal",
                },
            )
            assert created.status_code == 200, created.text
            proposal_id = created.json()["transfer_proposal_id"]
            submitted = await client.post(f"/api/capital/proposals/{proposal_id}/submit")
            assert submitted.status_code == 200, submitted.text
            version = submitted.json()["version"]
            await client.post("/api/auth/logout")

            for username in ("treasury-reviewer-1", "treasury-reviewer-2"):
                await login(client, username)
                without_step_up = await client.post(
                    f"/api/capital/proposals/{proposal_id}/reviews",
                    json={
                        "decision": "APPROVE",
                        "reason": "independent review",
                        "expected_version": version,
                    },
                )
                assert without_step_up.status_code == 403
                grant = await client.post(
                    "/api/auth/mock/step-up",
                    json={
                        "action": "capital.approve",
                        "object_id": proposal_id,
                        "object_version": version,
                    },
                )
                assert grant.status_code == 200, grant.text
                reviewed = await client.post(
                    f"/api/capital/proposals/{proposal_id}/reviews",
                    json={
                        "decision": "APPROVE",
                        "reason": "independent review",
                        "expected_version": version,
                        "action_grant": grant.json()["action_grant"],
                    },
                )
                assert reviewed.status_code == 200, reviewed.text
                version = reviewed.json()["version"]
                notifications = await client.get("/api/telegram/mock/notifications")
                assert notifications.status_code == 200
                capital_data = notifications.json()["capital_data"]
                assert capital_data
                assert all("action_references" not in item for item in capital_data)
                await client.post("/api/auth/logout")

            await login(client, "treasury-reviewer-1")
            authorized = await client.post(
                f"/api/capital/proposals/{proposal_id}/authorizations",
                json={"idempotency_key": "m7-api-auth", "expires_in_minutes": 30},
            )
            assert authorized.status_code == 200, authorized.text
            authorization_id = authorized.json()["transfer_authorization_id"]
            transferred = await client.post(
                f"/api/capital/authorizations/{authorization_id}/transfers/mock",
                json={"idempotency_key": "m7-api-transfer"},
            )
            assert transferred.status_code == 200, transferred.text
            assert transferred.json()["transport"] == "MOCK_ONLY"
            assert transferred.json()["detail"]["status"] == "SUBMITTED"
            transfer_id = transferred.json()["detail"]["capital_transfer_id"]
            transfer_detail = await client.get(f"/api/capital/transfers/{transfer_id}")
            assert transfer_detail.status_code == 200
            scope_match = await client.post(
                "/api/capital/reconciliations",
                json={
                    "environment": "TESTNET",
                    "account_id": "acct-1",
                    "venue": "BINANCE",
                },
            )
            assert scope_match.status_code == 200, scope_match.text
            in_flight = await client.post(
                f"/api/capital/transfers/{transfer_id}/observations/mock",
                json={"status": "IN_FLIGHT", "transaction_reference": "api-mock-tx"},
            )
            assert in_flight.status_code == 200, in_flight.text
            pending_reconciliation = await client.post(
                f"/api/capital/transfers/{transfer_id}/reconcile"
            )
            assert pending_reconciliation.status_code == 200
            assert pending_reconciliation.json()["reconciliation_status"] == "IN_FLIGHT"
            destination = await client.post(
                f"/api/capital/transfers/{transfer_id}/observations/mock",
                json={
                    "status": "DESTINATION_CONFIRMED",
                    "transaction_reference": "api-mock-tx",
                    "fee_amount": "1",
                    "net_received": "49",
                },
            )
            assert destination.status_code == 200, destination.text
            difference = await client.post(f"/api/capital/transfers/{transfer_id}/reconcile")
            assert difference.status_code == 200
            assert difference.json()["reconciliation_status"] == "DIFFERENCE"
            for balance in (
                {
                    "location_type": "VAULT",
                    "location_id": "vault-1",
                    "equity": "950",
                    "available_balance": "950",
                    "withdrawable_balance": "950",
                    "control_status": "CONTROLLED",
                    "address_reference": "masked-vault",
                },
                {
                    "location_type": "VENUE",
                    "location_id": "acct-1",
                    "equity": "549",
                    "available_balance": "549",
                    "withdrawable_balance": "549",
                    "control_status": "READ_ONLY",
                    "address_reference": "masked-venue",
                },
            ):
                recorded = await client.post(
                    "/api/capital/balances/mock",
                    json={
                        "environment": "TESTNET",
                        "venue": "BINANCE",
                        "asset": "USDT",
                        "deposit_status": "READY",
                        "network": "TESTNET",
                        "known": True,
                        **balance,
                    },
                )
                assert recorded.status_code == 200, recorded.text
            matched = await client.post(f"/api/capital/transfers/{transfer_id}/reconcile")
            assert matched.status_code == 200, matched.text
            assert matched.json()["reconciliation_status"] == "MATCH"
            assert matched.json()["detail"]["status"] == "SETTLED"
            center = await client.get("/api/capital")
            assert center.status_code == 200
            assert center.json()["data"]["real_transfer_gate"] == "DISABLED"
            assert center.json()["data"]["in_transit"] == "0"

            page = await client.get("/capital")
            assert page.status_code == 200
            assert "Trading Console" in page.text

    asyncio.run(scenario())
