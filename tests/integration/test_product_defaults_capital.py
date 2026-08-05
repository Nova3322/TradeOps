from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import Role
from trading_control_plane.models import (
    AuditEvent,
    DirectCapitalOperation,
    OrderIntent,
    TradingAuthorization,
)
from trading_control_plane.notilt import NoTiltGateway
from trading_control_plane.perptape import PerptapeClient, PerptapeFeedSnapshot
from trading_control_plane.service import TradingService


async def _login(client: AsyncClient, username: str) -> None:
    response = await client.post("/api/auth/mock/login", json={"username": username})
    assert response.status_code == 200, response.text


def _app(database: Database, *, notilt_gateway: NoTiltGateway | None = None):
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="product-flows-test-signing-secret",  # noqa: S106
        runtime_sync_enabled=True,
        capital_direct_vault_id="vault-1",
        capital_direct_vault_address="0x1111111111111111111111111111111111111111",
        capital_direct_owned_arbitrum_address="0x2222222222222222222222222222222222222222",
        capital_direct_binance_account_id="binance-main",
        capital_direct_binance_deposit_address=("0x3333333333333333333333333333333333333333"),
        capital_direct_binance_withdrawal_address=("0x2222222222222222222222222222222222222222"),
        capital_direct_hyperliquid_account_id="hyperliquid-main",
        capital_direct_hyperliquid_bridge_address=("0x4444444444444444444444444444444444444444"),
        capital_direct_max_amount=1000,
        capital_direct_max_fee=1,
        notilt_enabled=True,
        notilt_agent_address="0x5555555555555555555555555555555555555555",
        notilt_arbitrum_vault_address="0x1111111111111111111111111111111111111111",
        _env_file=None,
    )
    perptape = PerptapeClient(
        base_url="https://perptape.com",
        api_key=None,
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
    )
    return create_app(settings, database, perptape, notilt_gateway=notilt_gateway)


def test_proposal_defaults_and_direct_capital_are_permissioned_audited_and_blocked(
    database: Database,
) -> None:
    service = TradingService(database)
    now = datetime.now(UTC)
    admin = service.bootstrap_admin("product-admin", now=now)
    proposer = service.create_user("product-proposer", admin, now=now)
    treasury = service.create_user("product-treasury", admin, now=now)
    observer = service.create_user("product-observer", admin, now=now)
    service.assign_role(proposer, Role.PROPOSER, admin, now=now)
    service.assign_role(treasury, Role.TREASURY_ADMIN, admin, now=now)
    service.assign_role(observer, Role.OBSERVER, admin, now=now)
    perptape_actor = service.create_service_principal("perptape", admin, now=now)
    service.assign_role(perptape_actor, Role.PROPOSER, admin, now=now)
    service.register_instrument(
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
    parser = PerptapeClient(
        base_url="https://perptape.com",
        api_key="fixture-key",
        contract_version="breakouts-v1",
        cache_ttl=timedelta(0),
        fetcher=lambda _url, _headers, _timeout: {},
    )
    candidate = parser.parse_stream_alert(
        {
            "ex": "BN",
            "s": "BTCUSDT",
            "cs": "BTCUSDT",
            "dir": "HH",
            "p": 100_000,
            "th": 99_000,
            "tf": "1h",
            "t": int(now.timestamp() * 1_000),
            "u": int(now.timestamp() * 1_000),
            "kr": {"status": "ready"},
        },
        event_time=now,
    )
    service.record_perptape_feed(
        perptape_actor,
        PerptapeFeedSnapshot(
            contract_version="breakouts-v1",
            generated_at=now,
            fetched_at=now,
            next_allowed_at=now,
            candidates=(candidate,),
        ),
        now=now,
        base_snapshot=None,
    )

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app(database)),
            base_url="http://test",
        ) as client:
            await _login(client, "product-proposer")
            defaults = await client.get("/api/proposal-defaults")
            assert defaults.status_code == 200
            assert defaults.json()["configured"] is False
            assert defaults.json()["can_manage"] is False
            denied_update = await client.put(
                "/api/proposal-defaults",
                json={
                    "account_id": "acct-live",
                    "risk_tier": "LOW",
                    "notional": "100",
                    "max_risk": "1",
                    "invalidation_bps": 200,
                    "expires_in_minutes": 480,
                    "rationale": "safe reviewed default",
                    "idempotency_key": "proposer-cannot-update",
                },
            )
            assert denied_update.status_code == 403
            assert (await client.get("/api/capital")).status_code == 403

            await _login(client, "product-admin")
            update = await client.put(
                "/api/proposal-defaults",
                json={
                    "account_id": "acct-live",
                    "risk_tier": "LOW",
                    "notional": "100",
                    "max_risk": "1",
                    "invalidation_bps": 200,
                    "expires_in_minutes": 480,
                    "rationale": "safe reviewed default",
                    "auto_proposal_enabled": True,
                    "auto_proposal_min_timeframes": 4,
                    "idempotency_key": "admin-default-v1",
                },
            )
            assert update.status_code == 200, update.text
            assert update.json()["data"]["version"] == 1
            assert update.json()["data"]["updated_by_username"] == "product-admin"
            assert update.json()["data"]["auto_proposal_enabled"] is True
            assert update.json()["data"]["auto_proposal_min_timeframes"] == 4

            await _login(client, "product-proposer")
            visible = await client.get("/api/proposal-defaults")
            assert visible.status_code == 200
            assert visible.json()["data"]["version"] == 1
            assert visible.json()["can_manage"] is False
            opportunities = await client.get("/api/opportunities")
            assert opportunities.status_code == 200, opportunities.text
            one_click = await client.post(
                f"/api/opportunities/{candidate.candidate_id}/proposals/default"
            )
            assert one_click.status_code == 200, one_click.text
            assert one_click.json()["status"] == "PENDING_REVIEW"
            proposal_id = one_click.json()["proposal_id"]

            await _login(client, "product-admin")
            update_v2 = await client.put(
                "/api/proposal-defaults",
                json={
                    "account_id": "acct-live",
                    "risk_tier": "MEDIUM",
                    "notional": "200",
                    "max_risk": "2",
                    "invalidation_bps": 250,
                    "expires_in_minutes": 480,
                    "rationale": "new default does not rewrite old proposals",
                    "auto_proposal_enabled": False,
                    "auto_proposal_min_timeframes": 3,
                    "idempotency_key": "admin-default-v2",
                },
            )
            assert update_v2.status_code == 200, update_v2.text
            assert update_v2.json()["data"]["version"] == 2
            frozen = await client.get(f"/api/proposals/{proposal_id}")
            assert frozen.status_code == 200
            assert frozen.json()["frozen_payload"]["details"]["default_config_version"] == 1
            assert frozen.json()["frozen_payload"]["details"]["configuration_mode"] == "DEFAULT"

            await _login(client, "product-observer")
            assert (await client.get("/api/proposal-defaults")).status_code == 403
            denied_capital = await client.post(
                "/api/capital/direct-operations",
                json={
                    "path": "VAULT_TO_BINANCE",
                    "amount": "100",
                    "final_confirmed": True,
                    "idempotency_key": "observer-direct-denied",
                },
            )
            assert denied_capital.status_code == 403

            await _login(client, "product-treasury")
            for index, path in enumerate(
                (
                    "VAULT_TO_BINANCE",
                    "VAULT_TO_HYPERLIQUID",
                    "BINANCE_TO_VAULT",
                    "HYPERLIQUID_TO_VAULT",
                )
            ):
                result = await client.post(
                    "/api/capital/direct-operations",
                    json={
                        "path": path,
                        "amount": "100",
                        "final_confirmed": True,
                        "idempotency_key": f"treasury-direct-{index}",
                    },
                )
                assert result.status_code == 200, result.text
                assert result.json()["status"] == "BLOCKED"
                assert "CAPITAL_TRANSFER_GATE_DISABLED" in result.json()["blockers"]
                assert "0x1111111111111111111111111111111111111111" not in result.text
                assert "0x2222222222222222222222222222222222222222" not in result.text
                assert "0x3333333333333333333333333333333333333333" not in result.text
                assert "0x4444444444444444444444444444444444444444" not in result.text
            capital = await client.get("/api/capital")
            assert capital.status_code == 200
            assert len(capital.json()["data"]["direct_operations"]) == 4
            assert capital.json()["data"]["real_transfer_gate"] == "DISABLED"

    asyncio.run(scenario())
    with database.session_factory() as session:
        events = set(session.scalars(select(AuditEvent.event_type)).all())
    assert "PROPOSAL_DEFAULTS_UPDATED" in events
    assert "CAPITAL_DIRECT_OPERATION_BLOCKED" in events


def test_system_admin_direct_approval_requires_step_up_and_never_authorizes_or_orders(
    database: Database,
) -> None:
    service = TradingService(database)
    now = datetime.now(UTC)
    admin = service.bootstrap_admin("direct-approval-admin", now=now)
    instrument_id = service.register_instrument(
        actor_id=admin,
        venue="BINANCE",
        symbol="ETHUSDT",
        tick_size=Decimal("0.01"),
        lot_size=Decimal("0.001"),
        minimum_notional=Decimal("5"),
        contract_multiplier=Decimal("1"),
        quote_currency="USDT",
        collateral_currency="USDT",
        protection_supported=True,
        now=now,
    )

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app(database)),
            base_url="http://test",
        ) as client:
            await _login(client, "direct-approval-admin")
            created = await client.post(
                "/api/proposals/manual",
                json={
                    "environment": "LIVE",
                    "account_id": "acct-live",
                    "venue": "BINANCE",
                    "instrument_id": str(instrument_id),
                    "direction": "LONG",
                    "risk_tier": "HIGH",
                    "quantity": "0.01",
                    "max_risk": "5",
                    "expires_in_minutes": 480,
                    "trigger_price": "3000",
                    "invalidation_price": "2800",
                    "rationale": "administrator created frozen proposal for explicit review",
                    "idempotency_key": "direct-approval-proposal",
                },
            )
            assert created.status_code == 200, created.text
            proposal_id = created.json()["proposal_id"]
            version = created.json()["version"]

            denied = await client.post(
                f"/api/proposals/{proposal_id}/admin-approve",
                json={
                    "reason": "explicit administrator approval after reviewing frozen facts",
                    "expected_version": version,
                    "action_grant": "missing-step-up",
                },
            )
            assert denied.status_code == 401

            grant = await client.post(
                "/api/auth/mock/step-up",
                json={
                    "action": "proposal.admin_approve",
                    "object_id": proposal_id,
                    "object_version": version,
                },
            )
            assert grant.status_code == 200, grant.text
            approved = await client.post(
                f"/api/proposals/{proposal_id}/admin-approve",
                json={
                    "reason": "explicit administrator approval after reviewing frozen facts",
                    "expected_version": version,
                    "action_grant": grant.json()["action_grant"],
                },
            )
            assert approved.status_code == 200, approved.text
            assert approved.json()["status"] == "APPROVED"

    asyncio.run(scenario())
    with database.session_factory() as session:
        assert session.query(TradingAuthorization).count() == 0
        assert session.query(OrderIntent).count() == 0
        events = set(session.scalars(select(AuditEvent.event_type)).all())
    assert "PROPOSAL_ADMIN_DIRECT_APPROVED" in events


def test_system_admin_cannot_direct_approve_another_creators_proposal(
    database: Database,
) -> None:
    service = TradingService(database)
    now = datetime.now(UTC)
    admin = service.bootstrap_admin("direct-scope-admin", now=now)
    proposer = service.create_user("direct-scope-proposer", admin, now=now)
    service.assign_role(proposer, Role.PROPOSER, admin, now=now)
    instrument_id = service.register_instrument(
        actor_id=admin,
        venue="BINANCE",
        symbol="SOLUSDT",
        tick_size=Decimal("0.01"),
        lot_size=Decimal("0.01"),
        minimum_notional=Decimal("5"),
        contract_multiplier=Decimal("1"),
        quote_currency="USDT",
        collateral_currency="USDT",
        protection_supported=True,
        now=now,
    )

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app(database)),
            base_url="http://test",
        ) as client:
            await _login(client, "direct-scope-proposer")
            created = await client.post(
                "/api/proposals/manual",
                json={
                    "environment": "LIVE",
                    "account_id": "acct-live",
                    "venue": "BINANCE",
                    "instrument_id": str(instrument_id),
                    "direction": "LONG",
                    "risk_tier": "LOW",
                    "quantity": "1",
                    "max_risk": "5",
                    "expires_in_minutes": 480,
                    "trigger_price": "100",
                    "invalidation_price": "95",
                    "rationale": "independent review remains required",
                    "idempotency_key": "direct-scope-proposal",
                },
            )
            assert created.status_code == 200, created.text
            proposal_id = created.json()["proposal_id"]
            version = created.json()["version"]
            await _login(client, "direct-scope-admin")
            grant = await client.post(
                "/api/auth/mock/step-up",
                json={
                    "action": "proposal.admin_approve",
                    "object_id": proposal_id,
                    "object_version": version,
                },
            )
            assert grant.status_code == 200, grant.text
            denied = await client.post(
                f"/api/proposals/{proposal_id}/admin-approve",
                json={
                    "reason": "must not bypass another creator's independent review",
                    "expected_version": version,
                    "action_grant": grant.json()["action_grant"],
                },
            )
            assert denied.status_code == 422, denied.text
            assert denied.json()["error"]["code"] == "PROPOSAL_ADMIN_DIRECT_APPROVAL_SCOPE_INVALID"

    asyncio.run(scenario())


def test_direct_notilt_return_builds_only_audited_unsigned_sdk_plan(
    database: Database,
) -> None:
    service = TradingService(database)
    now = datetime.now(UTC)
    admin = service.bootstrap_admin("notilt-direct-admin", now=now)
    treasury = service.create_user("notilt-direct-treasury", admin, now=now)
    service.assign_role(treasury, Role.TREASURY_ADMIN, admin, now=now)

    def sdk_executor(payload: dict[str, object]) -> dict[str, object]:
        assert payload["chainId"] == 42161
        if payload["operation"] == "read-vault":
            return {
                "chain": "arbitrum",
                "vault": "0x1111111111111111111111111111111111111111",
                "agent": "0x2222222222222222222222222222222222222222",
                "budgets": [
                    {
                        "blockNumber": "123",
                        "blockTimestamp": "2000",
                        "vault": "0x1111111111111111111111111111111111111111",
                        "agent": "0x2222222222222222222222222222222222222222",
                        "owner": "0x7777777777777777777777777777777777777777",
                        "asset": {
                            "address": "0x6666666666666666666666666666666666666666",
                            "symbol": "USDC",
                            "decimals": 6,
                            "native": False,
                        },
                        "isOfficialVault": True,
                        "isActiveWhitelist": False,
                        "assignedWhitelistVault": ("0x0000000000000000000000000000000000000000"),
                        "balance": "0",
                        "maxReleaseNet": "0",
                        "pendingNet": "0",
                        "panicLocked": False,
                        "dailyReleaseRate": "0",
                        "dailyFeeRate": "0",
                    }
                ],
            }
        assert payload["operation"] == "prepare-deposit"
        assert payload["asset"] == "USDC"
        assert payload["amount"] == "99.000000000000000000"
        return {
            "transactions": [
                {
                    "chainId": 42161,
                    "to": "0x6666666666666666666666666666666666666666",
                    "data": "0x1234",
                    "value": "0",
                    "contract": "erc20",
                    "functionName": "approve",
                    "summary": "Approve exact NoTilt deposit allowance",
                },
                {
                    "chainId": 42161,
                    "to": "0x1111111111111111111111111111111111111111",
                    "data": "0xabcd",
                    "value": "0",
                    "contract": "vault",
                    "functionName": "deposit",
                    "summary": "Deposit exact amount into the trusted NoTilt Vault",
                },
            ]
        }

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(
                app=_app(database, notilt_gateway=NoTiltGateway(executor=sdk_executor))
            ),
            base_url="http://test",
        ) as client:
            await _login(client, "notilt-direct-admin")
            configured = await client.put(
                "/api/capital/direct-configuration",
                json={
                    "max_amount": "1000",
                    "max_fee": "1",
                    "idempotency_key": "configure-direct-capital-v1",
                },
            )
            assert configured.status_code == 200, configured.text
            assert configured.json()["data"]["direct_configuration"]["version"] == 1
            assert configured.json()["data"]["direct_configuration"]["can_manage"] is True
            assert "0x1111111111111111111111111111111111111111" not in configured.text

            await _login(client, "notilt-direct-treasury")
            denied_config = await client.put(
                "/api/capital/direct-configuration",
                json={
                    "max_amount": "500",
                    "idempotency_key": "treasury-cannot-configure",
                },
            )
            assert denied_config.status_code == 403
            created = await client.post(
                "/api/capital/direct-operations",
                json={
                    "path": "BINANCE_TO_VAULT",
                    "amount": "100",
                    "final_confirmed": True,
                    "idempotency_key": "binance-notilt-return",
                },
            )
            assert created.status_code == 200, created.text
            operation_id = created.json()["operation_id"]
            assert created.json()["status"] == "BLOCKED"
            assert "CAPITAL_TRANSFER_GATE_DISABLED" in created.json()["blockers"]

            preview = await client.post(
                f"/api/capital/direct-operations/{operation_id}/notilt-unsigned-preview",
                json={
                    "expected_version": 1,
                    "final_confirmed": True,
                    "idempotency_key": "binance-notilt-unsigned-preview",
                },
            )
            assert preview.status_code == 200, preview.text
            body = preview.json()
            assert body["signing"] is False
            assert body["broadcast"] is False
            assert body["execution_blocked"] is True
            assert body["preview_kind"] == "SDK_DEPOSIT_SEQUENCE"
            assert [item["function_name"] for item in body["transactions"]] == [
                "approve",
                "deposit",
            ]
            assert body["data"]["real_transfer_gate"] == "DISABLED"

    asyncio.run(scenario())
    with database.session_factory() as session:
        operation = session.scalar(select(DirectCapitalOperation))
        assert operation is not None
        assert operation.version == 2
        assert operation.status == "BLOCKED"
        assert operation.receipt_status == "NOT_SUBMITTED"
        assert operation.stages[-1]["broadcast"] is False
        assert session.query(TradingAuthorization).count() == 0
        assert session.query(OrderIntent).count() == 0
        events = set(session.scalars(select(AuditEvent.event_type)).all())
    assert "CAPITAL_DIRECT_UNSIGNED_PREVIEW_PREPARED" in events
    assert "CAPITAL_DIRECT_CONFIGURATION_UPDATED" in events


def test_direct_notilt_return_rejects_non_deposit_sdk_function(database: Database) -> None:
    service = TradingService(database)
    now = datetime.now(UTC)
    admin = service.bootstrap_admin("notilt-plan-admin", now=now)
    treasury = service.create_user("notilt-plan-treasury", admin, now=now)
    service.assign_role(treasury, Role.TREASURY_ADMIN, admin, now=now)

    def invalid_plan_executor(payload: dict[str, object]) -> dict[str, object]:
        if payload["operation"] == "read-vault":
            return {
                "chain": "arbitrum",
                "vault": "0x1111111111111111111111111111111111111111",
                "agent": "0x2222222222222222222222222222222222222222",
                "budgets": [
                    {
                        "blockNumber": "123",
                        "blockTimestamp": "2000",
                        "vault": "0x1111111111111111111111111111111111111111",
                        "agent": "0x2222222222222222222222222222222222222222",
                        "owner": "0x7777777777777777777777777777777777777777",
                        "asset": {
                            "address": "0x6666666666666666666666666666666666666666",
                            "symbol": "USDC",
                            "decimals": 6,
                            "native": False,
                        },
                        "isOfficialVault": True,
                        "isActiveWhitelist": False,
                        "assignedWhitelistVault": ("0x0000000000000000000000000000000000000000"),
                        "balance": "0",
                        "maxReleaseNet": "0",
                        "pendingNet": "0",
                        "panicLocked": False,
                        "dailyReleaseRate": "0",
                        "dailyFeeRate": "0",
                    }
                ],
            }
        return {
            "transactions": [
                {
                    "chainId": 42161,
                    "to": "0x1111111111111111111111111111111111111111",
                    "data": "0x1234",
                    "value": "0",
                    "contract": "vault",
                    "functionName": "requestWhitelistRelease",
                    "summary": "Wrong fixed-purpose function",
                }
            ]
        }

    gateway = NoTiltGateway(executor=invalid_plan_executor)

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app(database, notilt_gateway=gateway)),
            base_url="http://test",
        ) as client:
            await _login(client, "notilt-plan-treasury")
            created = await client.post(
                "/api/capital/direct-operations",
                json={
                    "path": "HYPERLIQUID_TO_VAULT",
                    "amount": "100",
                    "final_confirmed": True,
                    "idempotency_key": "hyperliquid-notilt-return",
                },
            )
            operation_id = created.json()["operation_id"]
            rejected = await client.post(
                f"/api/capital/direct-operations/{operation_id}/notilt-unsigned-preview",
                json={
                    "expected_version": 1,
                    "final_confirmed": True,
                    "idempotency_key": "reject-wrong-notilt-function",
                },
            )
            assert rejected.status_code == 422
            assert rejected.json()["error"]["code"] == "NOTILT_PLAN_INVALID"

    asyncio.run(scenario())
    with database.session_factory() as session:
        operation = session.scalar(select(DirectCapitalOperation))
        assert operation is not None
        assert operation.version == 1
        assert session.query(TradingAuthorization).count() == 0
        assert session.query(OrderIntent).count() == 0


@pytest.mark.parametrize(
    ("override", "expected_code"),
    (
        ({}, None),
        ({"isOfficialVault": False}, "NOTILT_VAULT_UNTRUSTED"),
        ({"isActiveWhitelist": False}, "NOTILT_WHITELIST_INACTIVE"),
        (
            {"assignedWhitelistVault": "0x0000000000000000000000000000000000000000"},
            "NOTILT_WHITELIST_INACTIVE",
        ),
        (
            {"owner": "0x5555555555555555555555555555555555555555"},
            "NOTILT_AGENT_OWNER_FORBIDDEN",
        ),
        ({"panicLocked": True}, "NOTILT_PANIC_LOCKED"),
        ({"maxReleaseNet": "98000000"}, "NOTILT_RELEASE_LIMIT_EXCEEDED"),
        ({"blockTimestampOffset": -301}, "NOTILT_FACT_STALE"),
    ),
)
def test_direct_notilt_release_rereads_live_agent_budget_before_unsigned_preview(
    database: Database,
    override: dict[str, object],
    expected_code: str | None,
) -> None:
    service = TradingService(database)
    now = datetime.now(UTC)
    case_name = expected_code or "SAFE"
    admin = service.bootstrap_admin(f"release-admin-{case_name}", now=now)
    treasury = service.create_user(f"release-treasury-{case_name}", admin, now=now)
    service.assign_role(treasury, Role.TREASURY_ADMIN, admin, now=now)
    calls: list[str] = []

    def executor(payload: dict[str, object]) -> dict[str, object]:
        operation = str(payload["operation"])
        calls.append(operation)
        if operation == "read-vault":
            block_offset = int(override.get("blockTimestampOffset", 0))
            budget = {
                "blockNumber": "123",
                "blockTimestamp": str(int((now + timedelta(seconds=block_offset)).timestamp())),
                "vault": "0x1111111111111111111111111111111111111111",
                "agent": "0x5555555555555555555555555555555555555555",
                "owner": "0x7777777777777777777777777777777777777777",
                "asset": {
                    "address": "0x6666666666666666666666666666666666666666",
                    "symbol": "USDC",
                    "decimals": 6,
                    "native": False,
                },
                "isOfficialVault": True,
                "isActiveWhitelist": True,
                "assignedWhitelistVault": "0x1111111111111111111111111111111111111111",
                "balance": "100000000",
                "maxReleaseNet": "100000000",
                "pendingNet": "0",
                "panicLocked": False,
                "dailyReleaseRate": "0",
                "dailyFeeRate": "0",
            }
            budget.update(
                {key: value for key, value in override.items() if key != "blockTimestampOffset"}
            )
            return {
                "chain": "arbitrum",
                "vault": "0x1111111111111111111111111111111111111111",
                "agent": "0x5555555555555555555555555555555555555555",
                "budgets": [budget],
            }
        if operation == "prepare-release-request" and expected_code is None:
            return {
                "transaction": {
                    "chainId": 42161,
                    "to": "0x1111111111111111111111111111111111111111",
                    "data": "0x1234",
                    "value": "0",
                    "contract": "vault",
                    "functionName": "requestWhitelistRelease",
                    "summary": "Request the reviewed NoTilt whitelist release",
                }
            }
        raise AssertionError("unsafe release preview reached transaction construction")

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(
                app=_app(database, notilt_gateway=NoTiltGateway(executor=executor))
            ),
            base_url="http://test",
        ) as client:
            await _login(client, f"release-treasury-{case_name}")
            created = await client.post(
                "/api/capital/direct-operations",
                json={
                    "path": "VAULT_TO_BINANCE",
                    "amount": "100",
                    "final_confirmed": True,
                    "idempotency_key": f"release-{case_name}",
                },
            )
            assert created.status_code == 200, created.text
            preview = await client.post(
                f"/api/capital/direct-operations/{created.json()['operation_id']}/notilt-unsigned-preview",
                json={
                    "expected_version": 1,
                    "final_confirmed": True,
                    "idempotency_key": f"release-preview-{case_name}",
                },
            )
            if expected_code is None:
                assert preview.status_code == 200, preview.text
                assert preview.json()["signing"] is False
                assert preview.json()["broadcast"] is False
                assert preview.json()["execution_blocked"] is True
            else:
                assert preview.status_code == 422, preview.text
                assert preview.json()["error"]["code"] == expected_code

    asyncio.run(scenario())
    assert calls == (
        ["read-vault", "prepare-release-request"] if expected_code is None else ["read-vault"]
    )
    with database.session_factory() as session:
        operation = session.scalar(select(DirectCapitalOperation))
        assert operation is not None
        assert operation.version == (2 if expected_code is None else 1)
        assert (operation.stages[-1]["code"] == "NOTILT_UNSIGNED_RELEASE_REQUEST_PREVIEW") is (
            expected_code is None
        )
