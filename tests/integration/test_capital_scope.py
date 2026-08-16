from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from conftest import add_exchange_account_fixture

from trading_control_plane.domain import (
    CapitalDirection,
    ExecutionEnvironment,
    Role,
)
from trading_control_plane.queries import TradingQueries
from trading_control_plane.request_context import (
    ApiClientRequestContext,
    bind_api_client_context,
    reset_api_client_context,
)
from trading_control_plane.service import TradingService


def _create_transfer(
    service: TradingService,
    actor_id: UUID,
    *,
    key: str,
    now: datetime,
) -> UUID:
    return service.create_transfer_proposal(
        actor_id=actor_id,
        environment=ExecutionEnvironment.TESTNET,
        direction=CapitalDirection.VAULT_TO_VENUE,
        account_id="shared-capital-account",
        venue="BINANCE",
        vault_id="shared-vault",
        asset="USDT",
        network="TESTNET",
        destination_reference="team-scoped-destination",
        amount=Decimal("100"),
        max_fee=Decimal("1"),
        min_received=Decimal("99"),
        reason="verify isolated capital roots",
        expires_at=now + timedelta(hours=1),
        idempotency_key=key,
        now=now,
    )


def _set_direct_configuration(
    service: TradingService,
    actor_id: UUID,
    *,
    key: str,
    now: datetime,
) -> UUID:
    return service.set_direct_capital_configuration(
        actor_id,
        key,
        environment="TESTNET",
        network="ARBITRUM",
        asset="USDC",
        treasury_provider="NOTILT_VAULT",
        vault_id="team-vault",
        vault_address=None,
        owned_arbitrum_address=None,
        binance_account_id="shared-capital-account",
        binance_deposit_address=None,
        binance_withdrawal_address=None,
        hyperliquid_account_id=None,
        hyperliquid_bridge_address=None,
        max_amount=Decimal("1000"),
        max_fee=Decimal("10"),
        now=now,
    )


def _set_automation_policy(
    service: TradingService,
    actor_id: UUID,
    *,
    key: str,
    now: datetime,
) -> UUID:
    return service.set_capital_automation_policy(
        actor_id=actor_id,
        environment=ExecutionEnvironment.TESTNET,
        account_id="shared-capital-account",
        venue="BINANCE",
        vault_id="shared-vault",
        asset="USDT",
        network="TESTNET",
        vault_destination_reference="team-vault-destination",
        venue_destination_reference="team-venue-destination",
        operating_low=Decimal("100"),
        operating_target=Decimal("200"),
        operating_high=Decimal("300"),
        vault_minimum_reserve=Decimal("500"),
        minimum_transfer=Decimal("10"),
        maximum_transfer=Decimal("100"),
        max_fee=Decimal("1"),
        idempotency_key=key,
        now=now,
    )


def test_api_key_capital_history_is_filtered_by_exact_user_rbac_resource(
    service: TradingService,
) -> None:
    now = datetime.now(UTC)
    admin = service.bootstrap_admin("capital-history-admin", now=now)
    context = TradingQueries(service.database).user_context(admin)
    workspace_id = UUID(str(context["active_workspace"]["workspace_id"]))
    team_id = UUID(str(context["active_team"]["team_id"]))
    for venue in ("BINANCE", "BYBIT"):
        add_exchange_account_fixture(
            service.database,
            admin,
            "shared-history-account",
            venue,
        )
        service.record_account_equity(
            account_id="shared-history-account",
            venue=venue,
            equity=Decimal("1000"),
            available_balance=Decimal("900"),
            currency="USDT",
            known=True,
            actor_id=admin,
            environment=ExecutionEnvironment.LIVE,
            observed_at=now,
            now=now,
        )
    owner_id = service.create_managed_user(
        "capital-history-owner",
        [Role.TREASURY_ADMIN],
        admin,
        "shared-history-account",
        "BINANCE",
        "ordinary-user-password",
        now=now,
    )
    token = bind_api_client_context(
        ApiClientRequestContext(
            owner_user_id=owner_id,
            api_client_id=UUID("00000000-0000-0000-0000-000000000123"),
            workspace_id=workspace_id,
            team_id=team_id,
        )
    )
    try:
        center = TradingQueries(service.database).capital_center(owner_id)
    finally:
        reset_api_client_context(token)

    assert {(item["location_id"], item["venue"]) for item in center["history"]} == {
        ("shared-history-account", "BINANCE")
    }
