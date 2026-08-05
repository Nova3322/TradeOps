from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from trading_control_plane.api import create_app
from trading_control_plane.capital import MockCapitalTransferAdapter
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    CapabilityStatus,
    CapitalDirection,
    CapitalTransferStatus,
    DomainRejected,
    ExecutionEnvironment,
    IdempotencyConflict,
    ReviewDecision,
    Role,
    SystemRiskState,
)
from trading_control_plane.models import AccountEquity
from trading_control_plane.notilt import (
    NoTiltAssetBudget,
    NoTiltGateway,
    NoTiltReceipt,
    NoTiltUnsignedTransaction,
    NoTiltUsdValuator,
    NoTiltVaultSnapshot,
    UsdValuation,
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


def notilt_budget(
    *,
    asset: str,
    balance: Decimal,
    vault: str,
    agent: str,
    now: datetime,
) -> NoTiltAssetBudget:
    return NoTiltAssetBudget(
        chain_id=42161,
        chain="ARBITRUM",
        block_number=123,
        block_timestamp=now,
        vault=vault,
        agent=agent,
        owner="0x3333333333333333333333333333333333333333",
        asset_address="0x4444444444444444444444444444444444444444",
        asset=asset,
        decimals=6,
        native=False,
        is_official_vault=True,
        is_active_whitelist=True,
        assigned_whitelist_vault=vault,
        balance=balance,
        max_release_net=balance,
        pending_net=Decimal(0),
        panic_locked=False,
        daily_release_rate=Decimal("0.1"),
        daily_fee_rate=Decimal("0.001"),
    )


def test_live_net_worth_and_risk_capital_combine_two_venues_and_vault(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    now = datetime.now(UTC)
    service.record_account_equity(
        "binance-main",
        "BINANCE",
        Decimal("10"),
        Decimal("10"),
        "USDT",
        True,
        ids["admin"],
        environment=ExecutionEnvironment.LIVE,
        now=now,
    )
    service.record_account_equity(
        "hyperliquid-main",
        "HYPERLIQUID",
        Decimal("20"),
        Decimal("20"),
        "USDC",
        True,
        ids["admin"],
        environment=ExecutionEnvironment.LIVE,
        now=now,
    )
    vault = "0x1111111111111111111111111111111111111111"
    agent = "0x2222222222222222222222222222222222222222"
    snapshot = NoTiltVaultSnapshot(
        chain_id=42161,
        chain="ARBITRUM",
        vault=vault,
        agent=agent,
        budgets=(
            notilt_budget(
                asset="USDC",
                balance=Decimal("30"),
                vault=vault,
                agent=agent,
                now=now,
            ),
            notilt_budget(
                asset="ETH",
                balance=Decimal("0.01"),
                vault=vault,
                agent=agent,
                now=now,
            ),
        ),
    )
    service.record_notilt_vault_snapshot(
        actor_id=ids["proposer"],
        snapshot=snapshot,
        valuations={
            "USDC": UsdValuation(Decimal(1), Decimal("30"), now),
            "ETH": UsdValuation(Decimal("3000"), Decimal("30"), now),
        },
        now=now,
    )

    center = TradingQueries(database).capital_center(ids["proposer"])
    assert center["net_worth"] == {
        "environment": "LIVE",
        "currency": "USD",
        "max_fact_age_seconds": 300,
        "alignment_tolerance_seconds": 60,
        "source_as_of": {
            "BINANCE": now.isoformat(),
            "HYPERLIQUID": now.isoformat(),
            "VAULT": now.isoformat(),
        },
        "venues": {"BINANCE": "10.000000000000000000", "HYPERLIQUID": "20.000000000000000000"},
        "vault": "60.000000000000000000",
        "total": "90.000000000000000000",
        "complete": True,
        "issues": [],
        "as_of": center["net_worth"]["as_of"],
    }
    assert sorted(item["source"] for item in center["history"]) == [
        "BINANCE",
        "HYPERLIQUID",
        "VAULT",
        "VAULT",
    ]
    assert all(item["usd_equity"] is not None for item in center["history"])
    with database.session_factory() as session:
        known, total, facts, _ = service._managed_capital_context(
            session,
            environment=ExecutionEnvironment.LIVE.value,
            now=now,
            max_age=timedelta(minutes=5),
        )
    assert known is True
    assert total == Decimal("90")
    assert {item.get("location_type") for item in facts} >= {"VENUE", "VAULT"}

    service.record_account_equity(
        "retired-binance-account",
        "BINANCE",
        Decimal("999"),
        Decimal("999"),
        "USDT",
        True,
        ids["admin"],
        environment=ExecutionEnvironment.LIVE,
        now=now,
    )
    service.record_account_equity(
        "retired-hyperliquid-account",
        "HYPERLIQUID",
        Decimal("999"),
        Decimal("999"),
        "USDC",
        True,
        ids["admin"],
        environment=ExecutionEnvironment.LIVE,
        now=now,
    )
    single_account_center = TradingQueries(database).capital_center(
        ids["proposer"],
        authoritative_live_accounts={
            "BINANCE": "binance-main",
            "HYPERLIQUID": "hyperliquid-main",
        },
    )
    live_venue_balances = [
        item
        for item in single_account_center["balances"]
        if item["environment"] == "LIVE" and item["location_type"] == "VENUE"
    ]
    assert {item["location_id"] for item in live_venue_balances} == {
        "binance-main",
        "hyperliquid-main",
    }
    assert single_account_center["net_worth"]["total"] == "90.000000000000000000"
    assert not any(
        item["location_id"].startswith("retired-") for item in single_account_center["history"]
    )

    async def api_scope_scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(
                app=build_app(
                    database,
                    MockTelegramGateway(),
                    runtime_binance_account_id="binance-main",
                    runtime_hyperliquid_account_id="hyperliquid-main",
                )
            ),
            base_url="http://test",
        ) as client:
            await login(client, "treasury-proposer")
            response = await client.get("/api/capital")
            assert response.status_code == 200
            payload = response.json()["data"]
            assert {
                item["location_id"]
                for item in payload["balances"]
                if item["environment"] == "LIVE" and item["location_type"] == "VENUE"
            } == {"binance-main", "hyperliquid-main"}
            assert not any(
                item["location_id"].startswith("retired-") for item in payload["history"]
            )

    asyncio.run(api_scope_scenario())
    authoritative_service = TradingService(
        database,
        authoritative_live_accounts={
            "BINANCE": "binance-main",
            "HYPERLIQUID": "hyperliquid-main",
        },
    )
    with database.session_factory() as session:
        known, total, facts, _ = authoritative_service._managed_capital_context(
            session,
            environment=ExecutionEnvironment.LIVE.value,
            now=now,
            max_age=timedelta(minutes=5),
        )
    assert known is True
    assert total == Decimal("90")
    assert not any(item.get("location_id", "").startswith("retired-") for item in facts)

    with database.session_factory.begin() as session:
        vault_fact = session.scalar(
            select(AccountEquity).where(
                AccountEquity.environment == ExecutionEnvironment.LIVE.value,
                AccountEquity.location_type == "VAULT",
                AccountEquity.currency == "USDC",
            )
        )
        assert vault_fact is not None
        vault_fact.control_status = "READ_ONLY"
    with database.session_factory() as session:
        controlled, _, _, _ = service._managed_capital_context(
            session,
            environment=ExecutionEnvironment.LIVE.value,
            now=now,
            max_age=timedelta(minutes=5),
        )
    assert controlled is False

    with database.session_factory.begin() as session:
        stale_binance = session.scalar(
            select(AccountEquity).where(
                AccountEquity.environment == ExecutionEnvironment.LIVE.value,
                AccountEquity.location_type == "VENUE",
                AccountEquity.account_id == "binance-main",
            )
        )
        assert stale_binance is not None
        stale_binance.observed_at = now - timedelta(days=1)
    stale_center = TradingQueries(database).capital_center(
        ids["proposer"],
        authoritative_live_accounts={
            "BINANCE": "binance-main",
            "HYPERLIQUID": "hyperliquid-main",
        },
    )
    assert stale_center["net_worth"]["venues"]["BINANCE"] is None
    assert stale_center["net_worth"]["total"] is None
    assert stale_center["net_worth"]["issues"].count("STALE_LIVE_SOURCE:BINANCE") == 1
    assert not any(issue.startswith("VENUE:") for issue in stale_center["net_worth"]["issues"])


def test_live_net_worth_rejects_time_misaligned_sources(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    now = datetime.now(UTC)
    service.record_account_equity(
        "binance-main", "BINANCE", Decimal("10"), Decimal("10"), "USDT", True,
        ids["admin"], environment=ExecutionEnvironment.LIVE, now=now,
    )
    service.record_account_equity(
        "hyperliquid-main", "HYPERLIQUID", Decimal("20"), Decimal("20"), "USDC", True,
        ids["admin"], environment=ExecutionEnvironment.LIVE, now=now - timedelta(seconds=90),
    )
    vault = "0x1111111111111111111111111111111111111111"
    agent = "0x2222222222222222222222222222222222222222"
    service.record_notilt_vault_snapshot(
        actor_id=ids["proposer"],
        snapshot=NoTiltVaultSnapshot(
            chain_id=42161,
            chain="ARBITRUM",
            vault=vault,
            agent=agent,
            budgets=(
                notilt_budget(
                    asset="USDC",
                    balance=Decimal("30"),
                    vault=vault,
                    agent=agent,
                    now=now,
                ),
            ),
        ),
        valuations={"USDC": UsdValuation(Decimal(1), Decimal("30"), now)},
        now=now,
    )

    center = TradingQueries(database).capital_center(ids["proposer"])
    assert center["net_worth"]["total"] is None
    assert center["net_worth"]["complete"] is False
    assert center["net_worth"]["issues"] == ["TIME_MISALIGNED_SOURCE:HYPERLIQUID"]
    assert center["net_worth"]["venues"]["HYPERLIQUID"] == "20.000000000000000000"


def test_live_unsigned_transfer_still_requires_disabled_by_default_gate(
    service: TradingService,
) -> None:
    ids = seed(service)
    now = datetime.now(UTC)
    proposal = service.create_transfer_proposal(
        actor_id=ids["proposer"],
        environment=ExecutionEnvironment.LIVE,
        direction=CapitalDirection.VAULT_TO_VENUE,
        account_id="binance-main",
        venue="BINANCE",
        vault_id="0x1111111111111111111111111111111111111111",
        asset="USDC",
        network="ARBITRUM",
        destination_reference="approved-destination-reference",
        amount=Decimal("1"),
        max_fee=Decimal("0.01"),
        min_received=Decimal("0.99"),
        reason="reviewed operating capital allocation",
        expires_at=now + timedelta(hours=1),
        idempotency_key="m7-live-unsigned-proposal",
        now=now,
        allow_live_unsigned=True,
    )
    service.submit_transfer_proposal(proposal, ids["proposer"], now=now)
    service.review_transfer_proposal(
        proposal,
        ids["reviewer_one"],
        ReviewDecision.APPROVE,
        "first independent review",
        2,
        now=now,
    )
    service.review_transfer_proposal(
        proposal,
        ids["reviewer_two"],
        ReviewDecision.APPROVE,
        "second independent review",
        3,
        now=now,
    )
    authorization = service.issue_transfer_authorization(
        proposal,
        ids["reviewer_one"],
        now + timedelta(minutes=30),
        "m7-live-unsigned-authorization",
        now=now,
    )

    with pytest.raises(DomainRejected, match="CAPABILITY_DISABLED"):
        service.reserve_capital_transfer(
            authorization,
            ids["reviewer_two"],
            "m7-live-unsigned-reservation",
            now=now,
            allow_live_unsigned=True,
        )


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


def build_app(
    database: Database,
    telegram: MockTelegramGateway,
    *,
    runtime_binance_account_id: str | None = None,
    runtime_hyperliquid_account_id: str | None = None,
) -> FastAPI:
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="m7-test-signing-secret-that-is-long-enough",  # noqa: S106
        public_base_url="http://test",
        runtime_binance_account_id=runtime_binance_account_id,
        runtime_hyperliquid_account_id=runtime_hyperliquid_account_id,
        _env_file=None,
    )
    perptape = PerptapeClient(
        base_url="https://perptape.com",
        api_key=None,
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
    )
    return create_app(settings, database, perptape, telegram)


def build_notilt_app(database: Database, telegram: MockTelegramGateway) -> FastAPI:
    agent = "0x2222222222222222222222222222222222222222"
    vault = "0x1111111111111111111111111111111111111111"
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="m7-test-signing-secret-that-is-long-enough",  # noqa: S106
        public_base_url="http://test",
        notilt_enabled=True,
        notilt_agent_address=agent,
        notilt_arbitrum_vault_address=vault,
        _env_file=None,
    )
    perptape = PerptapeClient(
        base_url="https://perptape.com",
        api_key=None,
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
    )

    def execute(payload: dict[str, object]) -> dict[str, object]:
        if payload["operation"] == "resolve-assignment":
            return {"assignedVault": vault, "active": True}
        if payload["operation"] == "read-vault":
            timestamp = int(datetime.now(UTC).timestamp())
            return {
                "chain": "arbitrum",
                "vault": vault,
                "agent": agent,
                "budgets": [
                    {
                        "blockNumber": "123",
                        "blockTimestamp": str(timestamp),
                        "vault": vault,
                        "agent": agent,
                        "owner": "0x3333333333333333333333333333333333333333",
                        "asset": {
                            "address": "0x4444444444444444444444444444444444444444",
                            "symbol": "USDC",
                            "decimals": 6,
                            "native": False,
                        },
                        "isOfficialVault": True,
                        "isActiveWhitelist": True,
                        "assignedWhitelistVault": vault,
                        "balance": "10000000",
                        "maxReleaseNet": "9000000",
                        "pendingNet": "0",
                        "panicLocked": False,
                        "dailyReleaseRate": "100000000000000000",
                        "dailyFeeRate": "1000000000000000",
                    }
                ],
            }
        if payload["operation"] == "prepare-release-request":
            return {
                "transaction": {
                    "chainId": 42161,
                    "to": vault,
                    "data": "0x1234",
                    "value": "0",
                    "contract": "vault",
                    "functionName": "requestWhitelistRelease",
                    "summary": "Request reviewed NoTilt release",
                }
            }
        if payload["operation"] == "prepare-release-execution":
            return {
                "chainId": 42161,
                "to": vault,
                "data": "0x5678",
                "value": "0",
                "contract": "vault",
                "functionName": "executeWhitelistRelease",
                "summary": "Execute reviewed NoTilt release",
            }
        if payload["operation"] == "prepare-release-cancellation":
            return {
                "chainId": 42161,
                "to": vault,
                "data": "0x9abc",
                "value": "0",
                "contract": "vault",
                "functionName": "cancelWhitelistRelease",
                "summary": "Cancel reviewed NoTilt release",
            }
        if payload["operation"] == "verify-receipt":
            now_timestamp = int(datetime.now(UTC).timestamp())
            receipt_kind = str(payload["receiptKind"])
            result: dict[str, object] = {
                "receiptKind": receipt_kind,
                "chainId": 42161,
                "chain": "arbitrum",
                "transactionHash": payload["transactionHash"],
                "vault": vault,
                "agent": agent,
                "blockNumber": "123",
                "blockTimestamp": str(now_timestamp - 1),
                "confirmations": "20",
            }
            if receipt_kind == "RELEASE_REQUEST":
                result.update(
                    {
                        "asset": "USDC",
                        "requestId": f"0x{'a' * 64}",
                        "netAmount": "0.99",
                        "fee": "0.01",
                        "executeAfter": str(now_timestamp - 2),
                        "expiresAt": str(now_timestamp + 3600),
                    }
                )
            else:
                result["requestId"] = payload["requestId"]
            return result
        raise AssertionError("unexpected NoTilt test operation")

    return create_app(
        settings,
        database,
        perptape,
        telegram,
        notilt_gateway=NoTiltGateway(executor=execute),
        notilt_valuator=NoTiltUsdValuator(),
    )


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
            admin_center = await client.get("/api/capital")
            assert admin_center.status_code == 200
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
                assert notifications.json()["scope"] == "PROPOSAL_REVIEW_ONLY"
                assert "capital_data" not in notifications.json()
                assert telegram.capital_notifications()
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
            assert "<title>交易控制台</title>" in page.text

    asyncio.run(scenario())


def test_notilt_api_reports_assignment_and_syncs_official_vault_read_only(
    database: Database, service: TradingService
) -> None:
    seed(service)
    telegram = MockTelegramGateway()

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=build_notilt_app(database, telegram)),
            base_url="http://test",
        ) as client:
            await login(client, "treasury-proposer")
            status_response = await client.get("/api/notilt/status")
            assert status_response.status_code == 200
            status_data = status_response.json()
            assert status_data["signing_mode"] == "EXTERNAL_WALLET_ONLY"
            assert status_data["credential_custody"] == "EXTERNAL_WALLET"
            assert status_data["chains"][2] == {
                "chain_id": 42161,
                "chain": "ARBITRUM",
                "vault_configured": True,
            }

            assignment = await client.get("/api/notilt/chains/42161/assignment")
            assert assignment.status_code == 200
            assert assignment.json()["active"] is True
            assert assignment.json()["matches_configured_vault"] is True

            synced = await client.post("/api/notilt/chains/42161/sync")
            assert synced.status_code == 200, synced.text
            assert synced.json()["transport"] == "NOTILT_OFFICIAL_SDK_READ_ONLY"
            assert synced.json()["facts_recorded"] == 1
            vault_balances = [
                item
                for item in synced.json()["data"]["balances"]
                if item["location_type"] == "VAULT" and item["environment"] == "LIVE"
            ]
            assert len(vault_balances) == 1
            assert vault_balances[0]["usd_equity"] == "10.000000000000000000"

    asyncio.run(scenario())


def test_notilt_live_plan_requires_full_capital_authority_and_never_broadcasts(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    now = datetime.now(UTC)
    service.record_position(
        "binance-main",
        "BINANCE",
        ids["instrument"],
        Decimal(0),
        Decimal(0),
        Decimal("100000"),
        True,
        ids["admin"],
        environment=ExecutionEnvironment.LIVE,
        now=now,
    )
    service.record_account_equity(
        "binance-main",
        "BINANCE",
        Decimal("10"),
        Decimal("10"),
        "USDC",
        True,
        ids["admin"],
        environment=ExecutionEnvironment.LIVE,
        now=now,
    )
    service.set_capability_gate(
        "CAPITAL_TRANSFER",
        CapabilityStatus.ENABLED,
        "explicit integration test only",
        ids["admin"],
        now=now,
    )
    telegram = MockTelegramGateway()

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=build_notilt_app(database, telegram)),
            base_url="http://test",
        ) as client:
            await login(client, "treasury-proposer")
            assert (await client.post("/api/notilt/chains/42161/sync")).status_code == 200
            created = await client.post(
                "/api/capital/proposals",
                json={
                    "environment": "LIVE",
                    "direction": "VAULT_TO_VENUE",
                    "account_id": "binance-main",
                    "venue": "BINANCE",
                    "vault_id": "0x1111111111111111111111111111111111111111",
                    "asset": "USDC",
                    "network": "ARBITRUM",
                    "destination_reference": "approved-binance-deposit-reference",
                    "amount": "1",
                    "max_fee": "0.01",
                    "min_received": "0.99",
                    "reason": "reviewed operating capital allocation",
                    "idempotency_key": "m7-notilt-live-api",
                },
            )
            assert created.status_code == 200, created.text
            proposal_id = created.json()["transfer_proposal_id"]
            submitted = await client.post(f"/api/capital/proposals/{proposal_id}/submit")
            version = submitted.json()["version"]
            await client.post("/api/auth/logout")

            for username in ("treasury-reviewer-1", "treasury-reviewer-2"):
                await login(client, username)
                grant = await client.post(
                    "/api/auth/mock/step-up",
                    json={
                        "action": "capital.approve",
                        "object_id": proposal_id,
                        "object_version": version,
                    },
                )
                reviewed = await client.post(
                    f"/api/capital/proposals/{proposal_id}/reviews",
                    json={
                        "decision": "APPROVE",
                        "reason": "independent reviewed release",
                        "expected_version": version,
                        "action_grant": grant.json()["action_grant"],
                    },
                )
                assert reviewed.status_code == 200, reviewed.text
                version = reviewed.json()["version"]
                await client.post("/api/auth/logout")

            await login(client, "treasury-reviewer-1")
            authorization = await client.post(
                f"/api/capital/proposals/{proposal_id}/authorizations",
                json={
                    "idempotency_key": "m7-notilt-live-auth",
                    "expires_in_minutes": 30,
                },
            )
            assert authorization.status_code == 200, authorization.text
            authorization_id = authorization.json()["transfer_authorization_id"]
            await client.post("/api/auth/logout")

            await login(client, "treasury-reviewer-2")
            plan = await client.post(
                f"/api/capital/authorizations/{authorization_id}/transfers/notilt-plan",
                json={"idempotency_key": "m7-notilt-live-plan"},
            )
            assert plan.status_code == 200, plan.text
            body = plan.json()
            assert body["transport"] == "NOTILT_UNSIGNED_TRANSACTION_HANDOFF"
            assert body["broadcast"] is False
            assert body["signing"] == "EXTERNAL_WALLET_REQUIRED"
            assert body["transactions"][0]["function_name"] == "requestWhitelistRelease"
            duplicate = await client.post(
                f"/api/capital/authorizations/{authorization_id}/transfers/notilt-plan",
                json={"idempotency_key": "m7-notilt-live-plan"},
            )
            assert duplicate.status_code == 200
            assert duplicate.json()["capital_transfer_id"] == body["capital_transfer_id"]
            assert duplicate.json()["transactions"] == body["transactions"]
            assert body["reserved_gross_amount"] == "1.000000000000000000"
            assert body["planned_net_amount"] == "0.990000000000000000"

            transfer_id = body["capital_transfer_id"]
            request_hash = f"0x{'b' * 64}"
            request_receipt = await client.post(
                f"/api/capital/transfers/{transfer_id}/notilt-receipt",
                json={"transaction_hash": request_hash},
            )
            assert request_receipt.status_code == 200, request_receipt.text
            request_detail = request_receipt.json()["detail"]
            assert request_detail["transport_state"] == "RELEASE_REQUEST_CONFIRMED"
            assert request_detail["status"] == "IN_FLIGHT"
            assert request_detail["fee_amount"] == "0.010000000000000000"
            assert request_receipt.json()["receipt"]["confirmations"] == 20

            execution_plan = await client.post(
                f"/api/capital/transfers/{transfer_id}/notilt-release-execution-plan"
            )
            assert execution_plan.status_code == 200, execution_plan.text
            assert (
                execution_plan.json()["transactions"][0]["function_name"]
                == "executeWhitelistRelease"
            )

            execution_hash = f"0x{'c' * 64}"
            execution_receipt = await client.post(
                f"/api/capital/transfers/{transfer_id}/notilt-receipt",
                json={"transaction_hash": execution_hash},
            )
            assert execution_receipt.status_code == 200, execution_receipt.text
            assert (
                execution_receipt.json()["detail"]["transport_state"]
                == "RELEASE_EXECUTION_CONFIRMED"
            )
            assert execution_receipt.json()["detail"]["status"] == "IN_FLIGHT"
            assert execution_receipt.json()["vault_sync"]["status"] == "SYNCED"

            duplicate_receipt = await client.post(
                f"/api/capital/transfers/{transfer_id}/notilt-receipt",
                json={"transaction_hash": execution_hash},
            )
            assert duplicate_receipt.status_code == 200
            assert duplicate_receipt.json()["idempotent"] is True

    asyncio.run(scenario())


def test_notilt_fee_outside_authorization_requires_verified_cancellation(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    now = datetime.now(UTC)
    vault = "0x1111111111111111111111111111111111111111"
    agent = "0x2222222222222222222222222222222222222222"
    service.record_position(
        "binance-main",
        "BINANCE",
        ids["instrument"],
        Decimal(0),
        Decimal(0),
        Decimal("100000"),
        True,
        ids["admin"],
        environment=ExecutionEnvironment.LIVE,
        now=now,
    )
    service.record_account_equity(
        "binance-main",
        "BINANCE",
        Decimal("10"),
        Decimal("10"),
        "USDC",
        True,
        ids["admin"],
        environment=ExecutionEnvironment.LIVE,
        now=now,
    )
    snapshot = NoTiltVaultSnapshot(
        chain_id=42161,
        chain="ARBITRUM",
        vault=vault,
        agent=agent,
        budgets=(
            notilt_budget(
                asset="USDC",
                balance=Decimal("10"),
                vault=vault,
                agent=agent,
                now=now,
            ),
        ),
    )
    service.record_notilt_vault_snapshot(
        actor_id=ids["proposer"],
        snapshot=snapshot,
        valuations={"USDC": UsdValuation(Decimal(1), Decimal("10"), now)},
        now=now,
    )
    service.set_capability_gate(
        "CAPITAL_TRANSFER",
        CapabilityStatus.ENABLED,
        "explicit cancellation test only",
        ids["admin"],
        now=now,
    )
    proposal = service.create_transfer_proposal(
        actor_id=ids["proposer"],
        environment=ExecutionEnvironment.LIVE,
        direction=CapitalDirection.VAULT_TO_VENUE,
        account_id="binance-main",
        venue="BINANCE",
        vault_id=vault,
        asset="USDC",
        network="ARBITRUM",
        destination_reference="approved-binance-deposit-reference",
        amount=Decimal("1"),
        max_fee=Decimal("0.01"),
        min_received=Decimal("0.99"),
        reason="verified cancellation path",
        expires_at=now + timedelta(hours=1),
        idempotency_key="m7-notilt-fee-proposal",
        now=now,
        allow_live_unsigned=True,
    )
    service.submit_transfer_proposal(proposal, ids["proposer"], now=now)
    service.review_transfer_proposal(
        proposal,
        ids["reviewer_one"],
        ReviewDecision.APPROVE,
        "first independent review",
        2,
        now=now,
    )
    service.review_transfer_proposal(
        proposal,
        ids["reviewer_two"],
        ReviewDecision.APPROVE,
        "second independent review",
        3,
        now=now,
    )
    authorization = service.issue_transfer_authorization(
        proposal,
        ids["reviewer_one"],
        now + timedelta(minutes=30),
        "m7-notilt-fee-auth",
        now=now,
    )
    transfer = service.reserve_capital_transfer(
        authorization,
        ids["reviewer_two"],
        "m7-notilt-fee-reserve",
        now=now,
        allow_live_unsigned=True,
    )
    request_transaction = NoTiltUnsignedTransaction(
        chain_id=42161,
        to=vault,
        data="0x1234",
        value=0,
        contract="vault",
        function_name="requestWhitelistRelease",
        summary="request reviewed release",
    )
    service.record_notilt_plan(
        transfer,
        ids["reviewer_two"],
        chain_id=42161,
        transport_state="RELEASE_REQUEST_PLAN_READY",
        transactions=(request_transaction,),
        now=now,
    )
    request_id = f"0x{'d' * 64}"
    state = service.record_notilt_receipt(
        transfer,
        ids["reviewer_two"],
        NoTiltReceipt(
            receipt_kind="RELEASE_REQUEST",
            chain_id=42161,
            chain="ARBITRUM",
            transaction_hash=f"0x{'e' * 64}",
            vault=vault,
            agent=agent,
            block_number=123,
            block_timestamp=now,
            confirmations=20,
            asset="USDC",
            request_id=request_id,
            net_amount=Decimal("0.99"),
            fee=Decimal("0.02"),
            execute_after=now + timedelta(minutes=10),
            expires_at=now + timedelta(hours=1),
        ),
        now=now,
    )
    assert state == "RELEASE_REQUEST_CONFIRMED"
    assert (
        TradingQueries(database).capital_transfer_detail(ids["reviewer_two"], transfer)["status"]
        == CapitalTransferStatus.MANUAL_REQUIRED.value
    )

    cancellation_transaction = NoTiltUnsignedTransaction(
        chain_id=42161,
        to=vault,
        data="0x5678",
        value=0,
        contract="vault",
        function_name="cancelWhitelistRelease",
        summary="cancel release outside fee authority",
    )
    service.record_notilt_plan(
        transfer,
        ids["reviewer_two"],
        chain_id=42161,
        transport_state="RELEASE_CANCELLATION_PLAN_READY",
        transactions=(cancellation_transaction,),
        now=now,
    )
    state = service.record_notilt_receipt(
        transfer,
        ids["reviewer_two"],
        NoTiltReceipt(
            receipt_kind="RELEASE_CANCELLATION",
            chain_id=42161,
            chain="ARBITRUM",
            transaction_hash=f"0x{'f' * 64}",
            vault=vault,
            agent=agent,
            block_number=124,
            block_timestamp=now,
            confirmations=20,
            request_id=request_id,
        ),
        now=now,
    )
    detail = TradingQueries(database).capital_transfer_detail(ids["reviewer_two"], transfer)
    assert state == "RELEASE_CANCELLED"
    assert detail["status"] == CapitalTransferStatus.FAILED_SOURCE_RESTORED.value
    assert detail["confirmed_transaction_hashes"] == [f"0x{'e' * 64}", f"0x{'f' * 64}"]


def test_notilt_deposit_receipt_and_fresh_source_facts_settle_exactly_once(
    database: Database, service: TradingService
) -> None:
    ids = seed(service)
    now = datetime.now(UTC)
    vault = "0x1111111111111111111111111111111111111111"
    agent = "0x2222222222222222222222222222222222222222"
    service.record_position(
        "binance-main",
        "BINANCE",
        ids["instrument"],
        Decimal(0),
        Decimal(0),
        Decimal("100000"),
        True,
        ids["admin"],
        environment=ExecutionEnvironment.LIVE,
        now=now,
    )
    service.record_account_equity(
        "binance-main",
        "BINANCE",
        Decimal("10"),
        Decimal("10"),
        "USDC",
        True,
        ids["admin"],
        environment=ExecutionEnvironment.LIVE,
        now=now,
    )
    initial_snapshot = NoTiltVaultSnapshot(
        chain_id=42161,
        chain="ARBITRUM",
        vault=vault,
        agent=agent,
        budgets=(
            notilt_budget(
                asset="USDC",
                balance=Decimal("10"),
                vault=vault,
                agent=agent,
                now=now,
            ),
        ),
    )
    service.record_notilt_vault_snapshot(
        actor_id=ids["proposer"],
        snapshot=initial_snapshot,
        valuations={"USDC": UsdValuation(Decimal(1), Decimal("10"), now)},
        now=now,
    )
    service.record_capital_scope_reconciliation(
        actor_id=ids["reviewer_one"],
        environment=ExecutionEnvironment.LIVE,
        account_id="binance-main",
        venue="BINANCE",
        now=now,
    )
    service.set_capability_gate(
        "CAPITAL_TRANSFER",
        CapabilityStatus.ENABLED,
        "explicit deposit receipt test only",
        ids["admin"],
        now=now,
    )
    proposal = service.create_transfer_proposal(
        actor_id=ids["proposer"],
        environment=ExecutionEnvironment.LIVE,
        direction=CapitalDirection.VENUE_TO_VAULT,
        account_id="binance-main",
        venue="BINANCE",
        vault_id=vault,
        asset="USDC",
        network="ARBITRUM",
        destination_reference=vault,
        amount=Decimal("1"),
        max_fee=Decimal("0.01"),
        min_received=Decimal("0.99"),
        reason="settled profit sweep",
        expires_at=now + timedelta(hours=1),
        idempotency_key="m7-notilt-deposit-proposal",
        now=now,
        allow_live_unsigned=True,
    )
    service.submit_transfer_proposal(proposal, ids["proposer"], now=now)
    service.review_transfer_proposal(
        proposal,
        ids["reviewer_one"],
        ReviewDecision.APPROVE,
        "first independent review",
        2,
        now=now,
    )
    service.review_transfer_proposal(
        proposal,
        ids["reviewer_two"],
        ReviewDecision.APPROVE,
        "second independent review",
        3,
        now=now,
    )
    authorization = service.issue_transfer_authorization(
        proposal,
        ids["reviewer_one"],
        now + timedelta(minutes=30),
        "m7-notilt-deposit-auth",
        now=now,
    )
    transfer = service.reserve_capital_transfer(
        authorization,
        ids["reviewer_two"],
        "m7-notilt-deposit-reserve",
        now=now,
        allow_live_unsigned=True,
    )
    service.record_notilt_plan(
        transfer,
        ids["reviewer_two"],
        chain_id=42161,
        transport_state="DEPOSIT_PLAN_READY",
        transactions=(
            NoTiltUnsignedTransaction(
                chain_id=42161,
                to="0x4444444444444444444444444444444444444444",
                data="0x1234",
                value=0,
                contract="erc20",
                function_name="approve",
                summary="approve exact deposit",
            ),
            NoTiltUnsignedTransaction(
                chain_id=42161,
                to=vault,
                data="0x5678",
                value=0,
                contract="vault",
                function_name="deposit",
                summary="deposit exact authorized net",
            ),
        ),
        now=now,
    )
    transaction_hash = f"0x{'1' * 64}"
    state = service.record_notilt_receipt(
        transfer,
        ids["reviewer_two"],
        NoTiltReceipt(
            receipt_kind="DEPOSIT",
            chain_id=42161,
            chain="ARBITRUM",
            transaction_hash=transaction_hash,
            vault=vault,
            agent=agent,
            block_number=123,
            block_timestamp=now,
            confirmations=20,
            asset="USDC",
            requested_amount=Decimal("0.99"),
            credited_amount=Decimal("0.99"),
        ),
        now=now,
    )
    assert state == "DEPOSIT_CONFIRMED"
    assert (
        service.record_notilt_receipt(
            transfer,
            ids["reviewer_two"],
            NoTiltReceipt(
                receipt_kind="DEPOSIT",
                chain_id=42161,
                chain="ARBITRUM",
                transaction_hash=transaction_hash,
                vault=vault,
                agent=agent,
                block_number=123,
                block_timestamp=now,
                confirmations=20,
                asset="USDC",
                requested_amount=Decimal("0.99"),
                credited_amount=Decimal("0.99"),
            ),
            now=now,
        )
        == "DEPOSIT_CONFIRMED"
    )

    observed = now + timedelta(seconds=1)
    service.record_account_equity(
        "binance-main",
        "BINANCE",
        Decimal("9"),
        Decimal("9"),
        "USDC",
        True,
        ids["admin"],
        environment=ExecutionEnvironment.LIVE,
        now=observed,
    )
    final_snapshot = NoTiltVaultSnapshot(
        chain_id=42161,
        chain="ARBITRUM",
        vault=vault,
        agent=agent,
        budgets=(
            notilt_budget(
                asset="USDC",
                balance=Decimal("10.99"),
                vault=vault,
                agent=agent,
                now=observed,
            ),
        ),
    )
    service.record_notilt_vault_snapshot(
        actor_id=ids["proposer"],
        snapshot=final_snapshot,
        valuations={"USDC": UsdValuation(Decimal(1), Decimal("10.99"), observed)},
        now=observed,
    )
    assert (
        service.reconcile_capital_transfer(
            transfer,
            ids["reviewer_two"],
            now=observed,
        )
        == "MATCH"
    )
    detail = TradingQueries(database).capital_transfer_detail(ids["reviewer_two"], transfer)
    assert detail["status"] == CapitalTransferStatus.SETTLED.value
    assert detail["fee_amount"] == "0.010000000000000000"
    assert detail["net_received"] == "0.990000000000000000"
