from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from conftest import add_exchange_account_fixture
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from trading_control_plane.api import create_app
from trading_control_plane.binance_capital import BinanceCapitalGateway
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.domain import (
    CapabilityStatus,
    DirectCapitalPath,
    ExecutionEnvironment,
    Role,
)
from trading_control_plane.hyperliquid_capital import (
    ARBITRUM_NATIVE_USDC_ADDRESS,
    ERC20_TRANSFER_TOPIC,
    HYPERLIQUID_BRIDGE2_ADDRESS,
    HyperliquidCapitalGateway,
)
from trading_control_plane.models import (
    AccountEquityObservation,
    Approval,
    AuditEvent,
    DirectCapitalOperation,
    ExchangeAccount,
    OrderIntent,
    TradingAuthorization,
)
from trading_control_plane.notilt import NoTiltGateway
from trading_control_plane.perptape import PerptapeClient, PerptapeFeedSnapshot
from trading_control_plane.safe_spending import SafeSpendingGateway
from trading_control_plane.service import TradingService


def _add_live_accounts(database: Database, admin: UUID) -> None:
    add_exchange_account_fixture(database, admin, "binance-main", "BINANCE")
    add_exchange_account_fixture(database, admin, "hyperliquid-main", "HYPERLIQUID")
    add_exchange_account_fixture(database, admin, "acct-live", "BINANCE")


async def _login(client: AsyncClient, username: str) -> None:
    response = await client.post("/api/auth/mock/login", json={"username": username})
    assert response.status_code == 200, response.text


def test_live_balance_history_is_persisted_no_faster_than_once_per_minute(
    database: Database,
) -> None:
    service = TradingService(database)
    start = datetime.now(UTC).replace(microsecond=0)
    actor = service.bootstrap_admin("capital-history-admin", now=start)
    _add_live_accounts(database, actor)
    for seconds, equity in ((0, "10"), (30, "11"), (60, "12")):
        observed_at = start + timedelta(seconds=seconds)
        service.record_account_equity(
            "binance-main",
            "BINANCE",
            Decimal(equity),
            Decimal(equity),
            "USDT",
            True,
            actor,
            environment=ExecutionEnvironment.LIVE,
            observed_at=observed_at,
            now=observed_at,
        )
    with database.session_factory() as session:
        observations = session.scalars(
            select(AccountEquityObservation).order_by(AccountEquityObservation.observed_at)
        ).all()
    assert [item.equity for item in observations] == [Decimal("10"), Decimal("12")]
    assert observations[1].observed_at - observations[0].observed_at == timedelta(minutes=1)


def _app(
    database: Database,
    *,
    notilt_gateway: NoTiltGateway | None = None,
    safe_spending_gateway: SafeSpendingGateway | None = None,
    hyperliquid_capital_gateway: HyperliquidCapitalGateway | None = None,
    binance_capital_gateway: BinanceCapitalGateway | None = None,
    binance_capital_withdraw_enabled: bool = False,
    runtime_binance_account_id: str | None = None,
    runtime_hyperliquid_account_id: str | None = None,
):
    settings = Settings(
        environment="test",
        database_url=str(database.engine.url),
        allow_mock_identity=True,
        session_signing_secret="product-flows-test-signing-secret",  # noqa: S106
        runtime_sync_enabled=True,
        runtime_binance_account_id=runtime_binance_account_id,
        runtime_hyperliquid_account_id=runtime_hyperliquid_account_id,
        capital_direct_vault_id="vault-1",
        capital_direct_vault_address="0x1111111111111111111111111111111111111111",
        capital_direct_owned_arbitrum_address="0x2222222222222222222222222222222222222222",
        capital_direct_binance_account_id="binance-main",
        capital_direct_binance_deposit_address=("0x3333333333333333333333333333333333333333"),
        capital_direct_binance_withdrawal_address=("0x1111111111111111111111111111111111111111"),
        binance_capital_api_key=(
            "test-binance-capital-key" if binance_capital_withdraw_enabled else None
        ),
        binance_capital_api_secret=(
            "test-binance-capital-secret" if binance_capital_withdraw_enabled else None
        ),
        binance_capital_withdraw_enabled=binance_capital_withdraw_enabled,
        capital_direct_hyperliquid_account_id="hyperliquid-main",
        capital_direct_hyperliquid_bridge_address=HYPERLIQUID_BRIDGE2_ADDRESS,
        hyperliquid_account_address="0x2222222222222222222222222222222222222222",
        hyperliquid_api_wallet_address="0x6666666666666666666666666666666666666666",
        capital_arbitrum_rpc_url="https://rpc.example.invalid",
        capital_direct_max_amount=1000,
        capital_direct_max_fee=1,
        notilt_enabled=True,
        notilt_agent_address="0x5555555555555555555555555555555555555555",
        notilt_arbitrum_vault_address="0x1111111111111111111111111111111111111111",
        safe_spending_enabled=True,
        safe_spending_arbitrum_rpc_url="https://example.invalid",
        capital_direct_safe_address="0x7777777777777777777777777777777777777777",
        capital_direct_safe_delegate_address="0x8888888888888888888888888888888888888888",
        _env_file=None,
    )
    perptape = PerptapeClient(
        base_url="https://perptape.com",
        api_key=None,
        contract_version="breakouts-v1",
        cache_ttl=timedelta(minutes=1),
    )
    return create_app(
        settings,
        database,
        perptape,
        notilt_gateway=notilt_gateway,
        safe_spending_gateway=safe_spending_gateway,
        hyperliquid_capital_gateway=hyperliquid_capital_gateway,
        binance_capital_gateway=binance_capital_gateway,
    )


def test_capital_configuration_requires_canonical_runtime_account_ids_not_display_names(
    database: Database,
) -> None:
    service = TradingService(database)
    now = datetime.now(UTC)
    admin = service.bootstrap_admin("capital-account-selector-admin", now=now)
    _add_live_accounts(database, admin)
    with database.session_factory.begin() as session:
        binance = session.scalar(
            select(ExchangeAccount).where(
                ExchangeAccount.account_id == "binance-main",
                ExchangeAccount.venue == "BINANCE",
            )
        )
        assert binance is not None
        binance.label = "acct-1-xpd"

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(
                app=_app(
                    database,
                    runtime_binance_account_id="binance-main",
                    runtime_hyperliquid_account_id="hyperliquid-main",
                )
            ),
            base_url="http://test",
        ) as client:
            await _login(client, "capital-account-selector-admin")
            display_name = await client.put(
                "/api/capital/direct-configuration",
                json={
                    "binance_account_id": "acct-1-xpd",
                    "idempotency_key": "reject-display-name-as-account-id",
                },
            )
            assert display_name.status_code == 422, display_name.text
            assert display_name.json()["error"]["code"] == "DEFAULT_ACCOUNT_REQUIRED"

            canonical_id = await client.put(
                "/api/capital/direct-configuration",
                json={
                    "binance_account_id": "binance-main",
                    "idempotency_key": "accept-canonical-runtime-account-id",
                },
            )
            assert canonical_id.status_code == 200, canonical_id.text
            assert canonical_id.json()["data"]["direct_configuration"][
                "binance_account_configured"
            ] is True

    asyncio.run(scenario())


def test_hyperliquid_withdrawal_auto_falls_back_to_wallet_and_settles_only_after_receipts(
    database: Database,
) -> None:
    service = TradingService(database)
    now = datetime.now(UTC)
    admin = service.bootstrap_admin("hl-capital-admin", now=now)
    _add_live_accounts(database, admin)
    state: dict[str, object] = {}
    withdrawal_hash = "0x" + "ab" * 32
    withdrawal_arbitrum_hash = "0x" + "cd" * 32
    treasury_hash = "0x" + "ef" * 32
    main = "0x2222222222222222222222222222222222222222"
    agent = "0x6666666666666666666666666666666666666666"
    safe = "0x7777777777777777777777777777777777777777"

    def info_fetcher(_url: str, payload: dict[str, object], _timeout: float) -> object:
        if payload["type"] == "clearinghouseState":
            return {"withdrawable": "1000"}
        if payload["type"] == "userRole":
            assert payload["user"] == agent
            return {"role": "agent", "data": {"user": main}}
        assert payload["type"] == "userNonFundingLedgerUpdates"
        return [
            {
                "time": int(now.timestamp() * 1000),
                "hash": withdrawal_hash,
                "delta": {
                    "type": "withdraw",
                    "usdc": "100.000000000000000000",
                    "nonce": state["nonce"],
                    "fee": "1",
                },
            }
        ]

    bridge_topic = f"0x{HYPERLIQUID_BRIDGE2_ADDRESS[2:].rjust(64, '0')}"
    main_topic = f"0x{main[2:].rjust(64, '0')}"
    safe_transfer_data = "0xa9059cbb" + safe[2:].rjust(64, "0") + hex(99_000_000)[2:].rjust(64, "0")

    def rpc_fetcher(_url: str, method: str, params: list[object], _timeout: float) -> object:
        tx_hash = str(params[0]) if params else ""
        if method == "eth_blockNumber":
            return "0x78"
        if method == "eth_getTransactionReceipt":
            if tx_hash == withdrawal_arbitrum_hash:
                return {
                    "status": "0x1",
                    "blockNumber": "0x64",
                    "logs": [
                        {
                            "address": ARBITRUM_NATIVE_USDC_ADDRESS,
                            "topics": [ERC20_TRANSFER_TOPIC, bridge_topic, main_topic],
                            "data": hex(100_000_000),
                        }
                    ],
                }
            assert tx_hash == treasury_hash
            return {"status": "0x1", "blockNumber": "0x64", "logs": []}
        assert method == "eth_getTransactionByHash" and tx_hash == treasury_hash
        return {"from": main, "to": ARBITRUM_NATIVE_USDC_ADDRESS, "input": safe_transfer_data}

    def safe_executor(payload: dict[str, object]) -> dict[str, object]:
        if payload["operation"] == "read-limit":
            return {
                "chainId": 42161,
                "moduleEnabled": True,
                "balance": "90000000",
                "available": "80000000",
                "blockTimestamp": str(int(now.timestamp())),
            }
        assert payload["operation"] == "prepare-deposit"
        return {
            "kind": "SAFE_ERC20_DEPOSIT_UNSIGNED_TRANSACTION",
            "chainId": 42161,
            "safe": safe,
            "sender": main,
            "token": ARBITRUM_NATIVE_USDC_ADDRESS,
            "amount": "99000000",
            "to": ARBITRUM_NATIVE_USDC_ADDRESS,
            "value": "0",
            "data": safe_transfer_data,
            "signing": False,
            "broadcast": False,
        }

    async def scenario() -> None:
        app = _app(
            database,
            safe_spending_gateway=SafeSpendingGateway(executor=safe_executor),
            hyperliquid_capital_gateway=HyperliquidCapitalGateway(
                info_fetcher=info_fetcher,
                rpc_fetcher=rpc_fetcher,
            ),
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _login(client, "hl-capital-admin")
            configured = await client.put(
                "/api/capital/direct-configuration",
                json={
                    "treasury_provider": "SAFE_SPENDING_LIMIT",
                    "safe_address": safe,
                    "binance_withdrawal_address": safe,
                    "safe_delegate_address": ("0x8888888888888888888888888888888888888888"),
                    "hyperliquid_bridge_address": HYPERLIQUID_BRIDGE2_ADDRESS,
                    "idempotency_key": "hl-capital-config",
                },
            )
            assert configured.status_code == 200, configured.text
            created = await client.post(
                "/api/capital/direct-operations",
                json={
                    "path": "HYPERLIQUID_TO_VAULT",
                    "amount": "100",
                    "final_confirmed": True,
                    "idempotency_key": "hl-capital-create",
                },
            )
            assert created.status_code == 200, created.text
            operation_id = created.json()["operation_id"]
            preview = await client.post(
                f"/api/capital/direct-operations/{operation_id}/hyperliquid-preview",
                json={
                    "expected_version": 1,
                    "final_confirmed": True,
                    "idempotency_key": "hl-capital-preview",
                },
            )
            assert preview.status_code == 200, preview.text
            artifact = preview.json()["artifact"]
            state["nonce"] = artifact["nonce"]
            assert preview.json()["automatic_fallback"] is True
            assert preview.json()["agent_wallet"]["authorized"] is True
            assert artifact["signing"] is False and artifact["broadcast"] is False
            assert preview.json()["data"]["real_transfer_gate"] == "DISABLED"

            submitted = await client.post(
                f"/api/capital/direct-operations/{operation_id}/wallet-submission",
                json={
                    "expected_version": 2,
                    "stage": "HYPERLIQUID_WITHDRAWAL",
                    "outcome": "SUBMITTED",
                    "action_hash": withdrawal_hash,
                    "nonce": artifact["nonce"],
                    "final_confirmed": True,
                    "idempotency_key": "hl-capital-wallet-submitted",
                },
            )
            assert submitted.status_code == 200, submitted.text
            ledger = await client.post(
                f"/api/capital/direct-operations/{operation_id}/hyperliquid-receipt",
                json={
                    "expected_version": 3,
                    "stage": "HYPERLIQUID_WITHDRAWAL_LEDGER",
                    "action_hash": withdrawal_hash,
                    "nonce": artifact["nonce"],
                    "idempotency_key": "hl-capital-ledger-receipt",
                },
            )
            assert ledger.status_code == 200, ledger.text
            arbitrum = await client.post(
                f"/api/capital/direct-operations/{operation_id}/hyperliquid-receipt",
                json={
                    "expected_version": 4,
                    "stage": "HYPERLIQUID_WITHDRAWAL_ARBITRUM",
                    "transaction_hash": withdrawal_arbitrum_hash,
                    "idempotency_key": "hl-capital-arbitrum-receipt",
                },
            )
            assert arbitrum.status_code == 200, arbitrum.text
            assert arbitrum.json()["settlement"].endswith("TREASURY_RECEIPT_STILL_REQUIRED")

            safe_preview = await client.post(
                f"/api/capital/direct-operations/{operation_id}/safe-spending-preview",
                json={
                    "expected_version": 5,
                    "final_confirmed": True,
                    "idempotency_key": "hl-capital-safe-preview",
                },
            )
            assert safe_preview.status_code == 200, safe_preview.text
            treasury_submitted = await client.post(
                f"/api/capital/direct-operations/{operation_id}/wallet-submission",
                json={
                    "expected_version": 6,
                    "stage": "TREASURY_DEPOSIT",
                    "outcome": "SUBMITTED",
                    "transaction_hash": treasury_hash,
                    "final_confirmed": True,
                    "idempotency_key": "hl-capital-treasury-submitted",
                },
            )
            assert treasury_submitted.status_code == 200, treasury_submitted.text
            treasury = await client.post(
                f"/api/capital/direct-operations/{operation_id}/treasury-receipt",
                json={
                    "expected_version": 7,
                    "transaction_hash": treasury_hash,
                    "idempotency_key": "hl-capital-treasury-receipt",
                },
            )
            assert treasury.status_code == 200, treasury.text
            operation = next(
                item
                for item in treasury.json()["data"]["direct_operations"]
                if item["operation_id"] == operation_id
            )
            assert operation["status"] == "SETTLED"
            assert operation["receipt_status"] == "CONFIRMED"
            assert treasury.json()["data"]["real_transfer_gate"] == "DISABLED"

    asyncio.run(scenario())
    with database.session_factory() as session:
        events = {item.event_type for item in session.scalars(select(AuditEvent)).all()}
    assert "CAPITAL_HYPERLIQUID_WALLET_REQUEST_PREPARED" in events
    assert "CAPITAL_HUMAN_WALLET_SUBMISSION_RECORDED" in events
    assert "CAPITAL_HYPERLIQUID_RECEIPT_VERIFIED" in events
    assert "CAPITAL_TREASURY_DESTINATION_RECEIPT_VERIFIED" in events


def test_safe_spending_limit_provider_is_selected_audited_and_never_signed(
    database: Database,
) -> None:
    service = TradingService(database)
    now = datetime.now(UTC)
    admin = service.bootstrap_admin("safe-provider-admin", now=now)
    _add_live_accounts(database, admin)
    treasury = service.create_user("safe-provider-treasury", admin, now=now)
    service.assign_role(treasury, Role.TREASURY_ADMIN, admin, now=now)

    def executor(payload: dict[str, object]) -> dict[str, object]:
        assert payload["asset"] == "USDC"
        if payload["operation"] == "read-limit":
            return {
                "chainId": 42161,
                "moduleEnabled": True,
                "balance": "90000000",
                "available": "80000000",
                "blockTimestamp": str(int(now.timestamp())),
            }
        if payload["operation"] == "prepare-deposit":
            return {
                "kind": "SAFE_ERC20_DEPOSIT_UNSIGNED_TRANSACTION",
                "chainId": 42161,
                "safe": payload["safe"],
                "sender": payload["sender"],
                "token": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
                "amount": "99000000",
                "to": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
                "value": "0",
                "data": "0xa9059cbb" + "00" * 64,
                "signing": False,
                "broadcast": False,
            }
        assert payload["operation"] == "prepare-spend"
        return {
            "kind": "SAFE_ALLOWANCE_SIGNATURE_REQUEST",
            "chainId": 42161,
            "module": "0x9999999999999999999999999999999999999999",
            "safe": payload["safe"],
            "delegate": payload["delegate"],
            "token": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
            "recipient": payload["recipient"],
            "amount": "99000000",
            "nonce": "7",
            "transferHash": "0x" + "ab" * 32,
            "signing": False,
            "broadcast": False,
            "calldataReady": False,
        }

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(
                app=_app(database, safe_spending_gateway=SafeSpendingGateway(executor=executor))
            ),
            base_url="http://test",
        ) as client:
            await _login(client, "safe-provider-admin")
            configured = await client.put(
                "/api/capital/direct-configuration",
                json={
                    "treasury_provider": "SAFE_SPENDING_LIMIT",
                    "safe_address": "0x7777777777777777777777777777777777777777",
                    "binance_withdrawal_address": ("0x7777777777777777777777777777777777777777"),
                    "safe_delegate_address": "0x8888888888888888888888888888888888888888",
                    "idempotency_key": "safe-provider-configure",
                },
            )
            assert configured.status_code == 200, configured.text
            direct_config = configured.json()["data"]["direct_configuration"]
            assert direct_config["treasury_provider"] == "SAFE_SPENDING_LIMIT"
            net_worth = configured.json()["data"]["net_worth"]
            assert net_worth["onchain_provider"] == "SAFE_SPENDING_LIMIT"
            assert net_worth["onchain_probe"] == {
                "provider": "SAFE_SPENDING_LIMIT",
                "status": "SUCCESS",
                "error_code": None,
            }
            assert net_worth["vault"] == "90.000000000000000000"
            assert {
                item["location_id"]
                for item in configured.json()["data"]["balances"]
                if item["location_type"] == "VAULT"
            } == {"selected-onchain-treasury"}
            assert "0x7777777777777777777777777777777777777777" not in configured.text

            await _login(client, "safe-provider-treasury")
            created = await client.post(
                "/api/capital/direct-operations",
                json={
                    "path": "VAULT_TO_BINANCE",
                    "amount": "100",
                    "final_confirmed": True,
                    "idempotency_key": "safe-provider-create",
                },
            )
            assert created.status_code == 200, created.text
            assert created.json()["treasury_provider"] == "SAFE_SPENDING_LIMIT"
            operation_id = created.json()["operation_id"]
            preview = await client.post(
                f"/api/capital/direct-operations/{operation_id}/safe-spending-preview",
                json={
                    "expected_version": 1,
                    "final_confirmed": True,
                    "idempotency_key": "safe-provider-preview",
                },
            )
            assert preview.status_code == 200, preview.text
            body = preview.json()
            assert body["signing"] is False and body["broadcast"] is False
            assert body["signature_request"]["calldataReady"] is False
            assert body["data"]["real_transfer_gate"] == "DISABLED"

            inbound = await client.post(
                "/api/capital/direct-operations",
                json={
                    "path": "BINANCE_TO_VAULT",
                    "amount": "100",
                    "final_confirmed": True,
                    "idempotency_key": "safe-provider-inbound-create",
                },
            )
            assert inbound.status_code == 200, inbound.text
            inbound_preview = await client.post(
                f"/api/capital/direct-operations/{inbound.json()['operation_id']}/safe-spending-preview",
                json={
                    "expected_version": 1,
                    "final_confirmed": True,
                    "idempotency_key": "safe-provider-inbound-preview",
                },
            )
            assert inbound_preview.status_code == 422, inbound_preview.text
            assert (
                inbound_preview.json()["error"]["code"]
                == "BINANCE_DIRECT_TREASURY_WITHDRAWAL_REQUIRED"
            )

    asyncio.run(scenario())
    with database.session_factory() as session:
        operation = session.scalar(
            select(DirectCapitalOperation).where(
                DirectCapitalOperation.path == DirectCapitalPath.VAULT_TO_BINANCE.value
            )
        )
        assert operation is not None
        assert operation.treasury_provider == "SAFE_SPENDING_LIMIT"
        assert operation.version == 2
        assert session.query(OrderIntent).count() == 0
        events = set(session.scalars(select(AuditEvent.event_type)).all())
    assert "CAPITAL_SAFE_SPENDING_PREVIEW_PREPARED" in events


def test_vault_and_safe_configurations_persist_while_current_provider_switches(
    database: Database,
) -> None:
    service = TradingService(database)
    now = datetime.now(UTC)
    admin = service.bootstrap_admin("dual-treasury-admin", now=now)
    _add_live_accounts(database, admin)

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app(database)),
            base_url="http://test",
        ) as client:
            await _login(client, "dual-treasury-admin")
            safe_selected = await client.put(
                "/api/capital/direct-configuration",
                json={
                    "treasury_provider": "SAFE_SPENDING_LIMIT",
                    "vault_id": "vault-1",
                    "vault_address": "0x1111111111111111111111111111111111111111",
                    "safe_address": "0x7777777777777777777777777777777777777777",
                    "safe_delegate_address": "0x8888888888888888888888888888888888888888",
                    "binance_withdrawal_address": (
                        "0x7777777777777777777777777777777777777777"
                    ),
                    "idempotency_key": "dual-treasury-safe-selected",
                },
            )
            assert safe_selected.status_code == 200, safe_selected.text
            direct = safe_selected.json()["data"]["direct_configuration"]
            assert direct["treasury_provider"] == "SAFE_SPENDING_LIMIT"
            assert direct["configured_providers"] == [
                "NOTILT_VAULT",
                "SAFE_SPENDING_LIMIT",
            ]
            assert direct["vault_id_configured"] is True
            assert direct["vault_address_configured"] is True
            assert direct["safe_address_configured"] is True
            assert direct["safe_delegate_configured"] is True

            vault_selected = await client.put(
                "/api/capital/direct-configuration",
                json={
                    "treasury_provider": "NOTILT_VAULT",
                    "binance_withdrawal_address": (
                        "0x1111111111111111111111111111111111111111"
                    ),
                    "idempotency_key": "dual-treasury-vault-selected",
                },
            )
            assert vault_selected.status_code == 200, vault_selected.text
            direct = vault_selected.json()["data"]["direct_configuration"]
            assert direct["treasury_provider"] == "NOTILT_VAULT"
            assert direct["configured_providers"] == [
                "NOTILT_VAULT",
                "SAFE_SPENDING_LIMIT",
            ]
            assert direct["vault_address_configured"] is True
            assert direct["safe_address_configured"] is True
            assert direct["safe_delegate_configured"] is True
            assert "0x1111111111111111111111111111111111111111" not in vault_selected.text
            assert "0x7777777777777777777777777777777777777777" not in vault_selected.text

            vault_operation = await client.post(
                "/api/capital/direct-operations",
                json={
                    "path": "VAULT_TO_BINANCE",
                    "amount": "10",
                    "final_confirmed": True,
                    "idempotency_key": "dual-treasury-vault-operation",
                },
            )
            assert vault_operation.status_code == 200, vault_operation.text
            assert vault_operation.json()["treasury_provider"] == "NOTILT_VAULT"

            safe_reselected = await client.put(
                "/api/capital/direct-configuration",
                json={
                    "treasury_provider": "SAFE_SPENDING_LIMIT",
                    "binance_withdrawal_address": (
                        "0x7777777777777777777777777777777777777777"
                    ),
                    "idempotency_key": "dual-treasury-safe-reselected",
                },
            )
            assert safe_reselected.status_code == 200, safe_reselected.text
            direct = safe_reselected.json()["data"]["direct_configuration"]
            assert direct["configured_providers"] == [
                "NOTILT_VAULT",
                "SAFE_SPENDING_LIMIT",
            ]

            safe_operation = await client.post(
                "/api/capital/direct-operations",
                json={
                    "path": "VAULT_TO_BINANCE",
                    "amount": "10",
                    "final_confirmed": True,
                    "idempotency_key": "dual-treasury-safe-operation",
                },
            )
            assert safe_operation.status_code == 200, safe_operation.text
            assert safe_operation.json()["treasury_provider"] == "SAFE_SPENDING_LIMIT"

    asyncio.run(scenario())


def test_safe_configuration_validation_returns_actionable_error_codes(
    database: Database,
) -> None:
    service = TradingService(database)
    now = datetime.now(UTC)
    admin = service.bootstrap_admin("safe-validation-admin", now=now)
    _add_live_accounts(database, admin)

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app(database)),
            base_url="http://test",
        ) as client:
            await _login(client, "safe-validation-admin")
            invalid_fee = await client.put(
                "/api/capital/direct-configuration",
                json={
                    "treasury_provider": "SAFE_SPENDING_LIMIT",
                    "safe_address": "0x7777777777777777777777777777777777777777",
                    "safe_delegate_address": "0x8888888888888888888888888888888888888888",
                    "binance_withdrawal_address": (
                        "0x7777777777777777777777777777777777777777"
                    ),
                    "max_amount": "1",
                    "max_fee": "1",
                    "idempotency_key": "safe-invalid-fee",
                },
            )
            assert invalid_fee.status_code == 422, invalid_fee.text
            assert (
                invalid_fee.json()["error"]["code"]
                == "CAPITAL_CONFIGURATION_FEE_LIMIT_INVALID"
            )

            mismatched_withdrawal = await client.put(
                "/api/capital/direct-configuration",
                json={
                    "treasury_provider": "SAFE_SPENDING_LIMIT",
                    "safe_address": "0x7777777777777777777777777777777777777777",
                    "safe_delegate_address": "0x8888888888888888888888888888888888888888",
                    "binance_withdrawal_address": (
                        "0x9999999999999999999999999999999999999999"
                    ),
                    "max_amount": "10",
                    "max_fee": "1",
                    "idempotency_key": "safe-mismatched-withdrawal",
                },
            )
            assert mismatched_withdrawal.status_code == 422, mismatched_withdrawal.text
            assert (
                mismatched_withdrawal.json()["error"]["code"]
                == "CAPITAL_BINANCE_WITHDRAWAL_ADDRESS_SCOPE_MISMATCH"
            )

    asyncio.run(scenario())


def test_binance_restricted_withdrawal_runs_frozen_preflight_submission_and_dual_receipt(
    database: Database,
) -> None:
    service = TradingService(database)
    now = datetime.now(UTC)
    admin_id = service.bootstrap_admin("binance-capital-admin", now=now)
    _add_live_accounts(database, admin_id)
    service.set_capability_gate(
        "CAPITAL_TRANSFER",
        CapabilityStatus.ENABLED,
        "explicit mock integration test only",
        admin_id,
        now=now,
    )
    destination = "0x1111111111111111111111111111111111111111"
    tx_hash = "0x" + "ab" * 32
    calls: list[tuple[str, str, dict[str, str]]] = []
    submitted = False

    def transport(method: str, path: str, params: dict[str, str], _timeout: float):
        nonlocal submitted
        calls.append((method, path, params))
        if path == "/sapi/v1/account/apiRestrictions":
            return {"ipRestrict": True, "enableReading": True, "enableWithdrawals": True}
        if path == "/sapi/v1/capital/config/getall":
            return [
                {
                    "coin": "USDC",
                    "free": "100",
                    "networkList": [
                        {
                            "network": "ARBITRUM",
                            "depositEnable": True,
                            "withdrawEnable": True,
                            "busy": False,
                            "withdrawTag": False,
                            "withdrawFee": "1",
                            "withdrawMin": "5",
                            "withdrawMax": "1000",
                        }
                    ],
                }
            ]
        if path == "/sapi/v1/capital/withdraw/address/list":
            return [
                {
                    "coin": "USDC",
                    "network": "ARBITRUM",
                    "address": destination,
                    "whiteStatus": True,
                }
            ]
        if path == "/sapi/v1/localentity/questionnaire-requirements":
            return "NIL"
        if path == "/sapi/v1/capital/withdraw/quota":
            return {"wdQuota": "1000", "usedWdQuota": "0"}
        if path == "/sapi/v1/capital/withdraw/apply":
            assert method == "POST"
            assert params["address"] == destination
            submitted = True
            return {"id": "withdrawal-1"}
        if path == "/sapi/v1/capital/withdraw/history":
            if not submitted:
                return []
            return [
                {
                    "id": "withdrawal-1",
                    "withdrawOrderId": params["withdrawOrderId"],
                    "coin": "USDC",
                    "network": "ARBITRUM",
                    "address": destination,
                    "txId": tx_hash,
                    "amount": "25",
                    "transactionFee": "1",
                    "status": 6,
                }
            ]
        raise AssertionError(path)

    def rpc_fetcher(_rpc_url: str, method: str, _params: list[object], _timeout: float):
        if method == "eth_blockNumber":
            return "0x77"
        assert method == "eth_getTransactionReceipt"
        return {
            "status": "0x1",
            "blockNumber": "0x64",
            "logs": [
                {
                    "address": ARBITRUM_NATIVE_USDC_ADDRESS,
                    "topics": [
                        ERC20_TRANSFER_TOPIC,
                        "0x" + "0" * 24 + "9" * 40,
                        "0x" + "0" * 24 + destination[2:],
                    ],
                    "data": hex(25_000_000),
                }
            ],
        }

    binance_gateway = BinanceCapitalGateway(
        api_key="fixture-capital-key",
        api_secret="fixture-capital-secret",  # noqa: S106
        transport=transport,
    )
    hyperliquid_gateway = HyperliquidCapitalGateway(rpc_fetcher=rpc_fetcher)

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(
                app=_app(
                    database,
                    binance_capital_gateway=binance_gateway,
                    hyperliquid_capital_gateway=hyperliquid_gateway,
                    binance_capital_withdraw_enabled=True,
                )
            ),
            base_url="http://test",
        ) as client:
            await _login(client, "binance-capital-admin")
            created = await client.post(
                "/api/capital/direct-operations",
                json={
                    "path": "BINANCE_TO_VAULT",
                    "amount": "25",
                    "final_confirmed": True,
                    "idempotency_key": "binance-create-1",
                },
            )
            assert created.status_code == 200, created.text
            operation_id = created.json()["operation_id"]
            preview = await client.post(
                f"/api/capital/direct-operations/{operation_id}/binance-preview",
                json={
                    "expected_version": 1,
                    "final_confirmed": True,
                    "idempotency_key": "binance-preview-1",
                },
            )
            assert preview.status_code == 200, preview.text
            assert preview.json()["transfer_submitted"] is False
            assert preview.json()["artifact"]["allowlisted"] is True
            submitted_response = await client.post(
                f"/api/capital/direct-operations/{operation_id}/binance-submit",
                json={
                    "expected_version": 2,
                    "final_confirmed": True,
                    "confirmation_phrase": "CONFIRM_BINANCE_WITHDRAWAL",
                    "idempotency_key": "binance-submit-1",
                },
            )
            assert submitted_response.status_code == 200, submitted_response.text
            receipt = await client.post(
                f"/api/capital/direct-operations/{operation_id}/binance-receipt",
                json={
                    "expected_version": 3,
                    "stage": "BINANCE_WITHDRAWAL",
                    "idempotency_key": "binance-receipt-1",
                },
            )
            assert receipt.status_code == 200, receipt.text
            assert receipt.json()["settlement"] == "CONFIRMED"
            assert "fixture-capital" not in receipt.text

    asyncio.run(scenario())
    assert submitted is True
    assert all("signature" not in params for _, _, params in calls)
    with database.session_factory() as session:
        operation = session.scalar(
            select(DirectCapitalOperation).where(
                DirectCapitalOperation.path == DirectCapitalPath.BINANCE_TO_VAULT.value
            )
        )
        assert operation is not None
        assert operation.status == "SETTLED"
        assert operation.receipt_status == "CONFIRMED"
        assert session.query(OrderIntent).count() == 0
        events = set(session.scalars(select(AuditEvent.event_type)).all())
    assert {
        "CAPITAL_BINANCE_PREFLIGHT_VERIFIED",
        "CAPITAL_BINANCE_WITHDRAWAL_SUBMITTED",
        "CAPITAL_BINANCE_RECEIPT_VERIFIED",
    }.issubset(events)


def test_proposal_defaults_and_direct_capital_are_permissioned_audited_and_blocked(
    database: Database,
) -> None:
    service = TradingService(database)
    now = datetime.now(UTC)
    admin = service.bootstrap_admin("product-admin", now=now)
    _add_live_accounts(database, admin)
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
            occupied = await client.get("/api/opportunities")
            occupied_candidate = next(
                item
                for item in occupied.json()["data"]
                if item["candidate_id"] == candidate.candidate_id
            )
            active_proposal = occupied_candidate["active_proposal"]
            assert active_proposal["workspace_id"]
            assert active_proposal["team_id"]
            assert {
                key: active_proposal[key]
                for key in (
                    "proposal_id",
                    "status",
                    "venue",
                    "symbol",
                    "direction",
                    "expires_at",
                    "source_observed_at",
                    "active_count",
                )
            } == {
                "proposal_id": proposal_id,
                "status": "PENDING_REVIEW",
                "venue": "BINANCE",
                "symbol": "BTCUSDT",
                "direction": "LONG",
                "expires_at": one_click.json()["expires_at"],
                "source_observed_at": one_click.json()["source_observed_at"],
                "active_count": 1,
            }
            refreshed_at = datetime.now(UTC)
            refreshed_candidate = replace(
                candidate,
                candidate_id="pt_refreshed_same_scope",
                reference_price=Decimal("100001"),
                triggered_at=refreshed_at,
                observed_at=refreshed_at,
            )
            service.record_perptape_feed(
                perptape_actor,
                PerptapeFeedSnapshot(
                    contract_version="breakouts-v1",
                    generated_at=refreshed_at,
                    fetched_at=refreshed_at,
                    next_allowed_at=refreshed_at,
                    candidates=(refreshed_candidate,),
                ),
                now=refreshed_at,
                base_snapshot=None,
            )
            repeated_one_click = await client.post(
                f"/api/opportunities/{refreshed_candidate.candidate_id}/proposals/default"
            )
            assert repeated_one_click.status_code == 200, repeated_one_click.text
            assert repeated_one_click.json()["proposal_id"] == proposal_id
            refreshed_opportunities = await client.get("/api/opportunities")
            refreshed_projection = next(
                item
                for item in refreshed_opportunities.json()["data"]
                if item["candidate_id"] == refreshed_candidate.candidate_id
            )
            assert refreshed_projection["active_proposal"]["proposal_id"] == proposal_id
            assert refreshed_projection["active_proposal"]["active_count"] == 1

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
            observer_opportunities = await client.get("/api/opportunities")
            assert observer_opportunities.status_code == 200
            assert (
                observer_opportunities.json()["data"][0]["active_proposal"]["proposal_id"]
                == proposal_id
            )
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
                assert HYPERLIQUID_BRIDGE2_ADDRESS not in result.text
            capital = await client.get("/api/capital")
            assert capital.status_code == 200
            assert len(capital.json()["data"]["direct_operations"]) == 4
            assert capital.json()["data"]["real_transfer_gate"] == "DISABLED"

    asyncio.run(scenario())
    with database.session_factory() as session:
        events = set(session.scalars(select(AuditEvent.event_type)).all())
    assert "PROPOSAL_DEFAULTS_UPDATED" in events
    assert "CAPITAL_DIRECT_OPERATION_BLOCKED" in events


def test_system_admin_cannot_approve_own_proposal_or_use_a_direct_override(
    database: Database,
) -> None:
    service = TradingService(database)
    now = datetime.now(UTC)
    admin = service.bootstrap_admin("direct-approval-admin", now=now)
    _add_live_accounts(database, admin)
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

            grant = await client.post(
                "/api/auth/mock/step-up",
                json={
                    "action": "proposal.approve",
                    "object_id": proposal_id,
                    "object_version": version,
                },
            )
            assert grant.status_code == 200, grant.text
            denied = await client.post(
                f"/api/proposals/{proposal_id}/reviews",
                json={
                    "decision": "APPROVE",
                    "reason": "the creator must still use an independent reviewer",
                    "expected_version": version,
                    "action_grant": grant.json()["action_grant"],
                },
            )
            assert denied.status_code == 403, denied.text
            assert denied.json()["error"]["code"] == "SELF_REVIEW_FORBIDDEN"

            removed_override = await client.post(
                f"/api/proposals/{proposal_id}/admin-approve",
                json={
                    "reason": "there is no direct approval path",
                    "expected_version": version,
                    "action_grant": grant.json()["action_grant"],
                },
            )
            assert removed_override.status_code == 404

    asyncio.run(scenario())
    with database.session_factory() as session:
        assert session.query(TradingAuthorization).count() == 0
        assert session.query(OrderIntent).count() == 0
        assert session.query(Approval).count() == 0


def test_direct_binance_return_does_not_build_redundant_notilt_wallet_deposit(
    database: Database,
) -> None:
    service = TradingService(database)
    now = datetime.now(UTC)
    admin = service.bootstrap_admin("notilt-direct-admin", now=now)
    _add_live_accounts(database, admin)
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
            assert preview.status_code == 422, preview.text
            assert preview.json()["error"]["code"] == "BINANCE_DIRECT_TREASURY_WITHDRAWAL_REQUIRED"

    asyncio.run(scenario())
    with database.session_factory() as session:
        operation = session.scalar(select(DirectCapitalOperation))
        assert operation is not None
        assert operation.version == 1
        assert operation.status == "BLOCKED"
        assert operation.receipt_status == "NOT_SUBMITTED"
        assert all("broadcast" not in stage for stage in operation.stages)
        assert session.query(TradingAuthorization).count() == 0
        assert session.query(OrderIntent).count() == 0
        events = set(session.scalars(select(AuditEvent.event_type)).all())
    assert "CAPITAL_DIRECT_UNSIGNED_PREVIEW_PREPARED" not in events
    assert "CAPITAL_DIRECT_CONFIGURATION_UPDATED" in events


def test_direct_notilt_return_rejects_non_deposit_sdk_function(database: Database) -> None:
    service = TradingService(database)
    now = datetime.now(UTC)
    admin = service.bootstrap_admin("notilt-plan-admin", now=now)
    _add_live_accounts(database, admin)
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
    _add_live_accounts(database, admin)
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
