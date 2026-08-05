from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from trading_control_plane.capital import (
    CapitalTransferCommand,
    MockCapitalTransferAdapter,
    build_direct_capital_plan,
)
from trading_control_plane.config import Settings
from trading_control_plane.domain import (
    CapitalDirection,
    CapitalTreasuryProvider,
    DirectCapitalPath,
    DomainRejected,
    ExecutionEnvironment,
)


def command(environment: ExecutionEnvironment) -> CapitalTransferCommand:
    return CapitalTransferCommand(
        capital_transfer_id=UUID("00000000-0000-0000-0000-000000000701"),
        environment=environment,
        direction=CapitalDirection.VAULT_TO_VENUE,
        source_id="vault-1",
        destination_id="acct-1",
        asset="USDT",
        network="TESTNET",
        destination_reference="approved-test-destination",
        gross_amount=Decimal("100"),
        max_fee=Decimal("1"),
        min_received=Decimal("99"),
    )


def test_mock_capital_adapter_is_deterministic_and_never_live() -> None:
    adapter = MockCapitalTransferAdapter()
    now = datetime(2026, 7, 19, tzinfo=UTC)

    first = adapter.submit(command(ExecutionEnvironment.TESTNET), now=now)
    second = adapter.submit(command(ExecutionEnvironment.TESTNET), now=now)
    assert first == second
    assert first.status == "SUBMITTED"
    assert first.external_transfer_id.startswith("mock-capital-")

    with pytest.raises(DomainRejected, match="CAPITAL_TRANSFER_LIVE_DISABLED"):
        adapter.submit(command(ExecutionEnvironment.LIVE), now=now)


@pytest.mark.parametrize(
    ("path", "expected_stages"),
    [
        (
            DirectCapitalPath.VAULT_TO_BINANCE,
            [
                "VAULT_RELEASE_REQUEST",
                "WAIT_10_MINUTES",
                "REVALIDATE_RELEASE",
                "TRANSFER_TO_AUTHORIZED_BINANCE_ADDRESS",
            ],
        ),
        (
            DirectCapitalPath.VAULT_TO_HYPERLIQUID,
            [
                "VAULT_RELEASE_TO_AUTHORIZED_OWNED_ADDRESS",
                "WAIT_10_MINUTES",
                "REVALIDATE_RELEASE",
                "DEPOSIT_TO_HYPERLIQUID_CONTRACT",
            ],
        ),
        (
            DirectCapitalPath.HYPERLIQUID_TO_VAULT,
            [
                "WITHDRAW_FROM_HYPERLIQUID_CONTRACT",
                "RECEIVE_AT_AUTHORIZED_OWNED_ADDRESS",
                "PREPARE_NOTILT_SDK_DEPOSIT",
                "HUMAN_WALLET_CONFIRMATION",
                "VERIFY_NOTILT_DEPOSIT_RECEIPT",
            ],
        ),
        (
            DirectCapitalPath.BINANCE_TO_VAULT,
            [
                "RESTRICTED_BINANCE_WITHDRAWAL_TO_AUTHORIZED_OWNED_ADDRESS",
                "RECEIVE_AT_AUTHORIZED_OWNED_ADDRESS",
                "PREPARE_NOTILT_SDK_DEPOSIT",
                "HUMAN_WALLET_CONFIRMATION",
                "VERIFY_NOTILT_DEPOSIT_RECEIPT",
            ],
        ),
    ],
)
def test_direct_capital_paths_are_explicit_and_never_broadcast(
    path: DirectCapitalPath,
    expected_stages: list[str],
) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://user:pass@localhost/trading",
        capital_direct_vault_id="vault-1",
        capital_direct_vault_address="0x1111111111111111111111111111111111111111",
        capital_direct_owned_arbitrum_address="0x2222222222222222222222222222222222222222",
        capital_direct_binance_account_id="binance-main",
        capital_direct_binance_deposit_address=("0x3333333333333333333333333333333333333333"),
        capital_direct_binance_withdrawal_address=("0x2222222222222222222222222222222222222222"),
        capital_direct_hyperliquid_account_id="hyperliquid-main",
        capital_direct_hyperliquid_bridge_address=("0x4444444444444444444444444444444444444444"),
        capital_direct_max_amount=Decimal(1000),
        capital_direct_max_fee=Decimal(1),
        _env_file=None,
    )
    now = datetime(2026, 8, 2, tzinfo=UTC)

    plan = build_direct_capital_plan(
        path=path,
        amount=Decimal(100),
        settings=settings,
        capital_transfer_gate="ENABLED",
        now=now,
    )

    assert plan.status == "BLOCKED"
    assert plan.receipt_status == "NOT_SUBMITTED"
    assert [stage["code"] for stage in plan.stages] == expected_stages
    assert all(stage["status"] == "BLOCKED" for stage in plan.stages)
    assert any(code.endswith("ADAPTER_UNAVAILABLE") for code in plan.blockers)
    if path in {
        DirectCapitalPath.VAULT_TO_BINANCE,
        DirectCapitalPath.VAULT_TO_HYPERLIQUID,
    }:
        assert plan.execute_after == now + timedelta(minutes=10)


def test_safe_spending_limits_are_a_parallel_fail_closed_provider() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://user:pass@localhost/trading",
        safe_spending_enabled=True,
        safe_spending_arbitrum_rpc_url="https://example.invalid",
        capital_direct_safe_address="0x1111111111111111111111111111111111111111",
        capital_direct_safe_delegate_address="0x2222222222222222222222222222222222222222",
        capital_direct_binance_account_id="binance-main",
        capital_direct_binance_deposit_address="0x3333333333333333333333333333333333333333",
        capital_direct_max_amount=Decimal(1000),
        capital_direct_max_fee=Decimal(1),
        _env_file=None,
    )
    plan = build_direct_capital_plan(
        path=DirectCapitalPath.VAULT_TO_BINANCE,
        treasury_provider=CapitalTreasuryProvider.SAFE_SPENDING_LIMIT,
        amount=Decimal(100),
        settings=settings,
        capital_transfer_gate="DISABLED",
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )
    assert plan.treasury_provider is CapitalTreasuryProvider.SAFE_SPENDING_LIMIT
    assert plan.vault_id == "SAFE_SPENDING_LIMIT"
    assert "SAFE_ALLOWANCE_PREFLIGHT_REQUIRED" in plan.blockers
    assert "CAPITAL_TRANSFER_GATE_DISABLED" in plan.blockers
    assert plan.execute_after is None
    assert plan.stages[0]["code"] == "READ_SAFE_SPENDING_LIMIT"
