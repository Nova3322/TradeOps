from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select

from trading_control_plane.domain import (
    CapitalDirection,
    DomainRejected,
    ExecutionEnvironment,
    Role,
)
from trading_control_plane.models import (
    CapitalAutomationPolicy,
    DirectCapitalConfiguration,
    NotificationDelivery,
    SenderLease,
    Team,
    TransferProposal,
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
        environment=ExecutionEnvironment.SHADOW,
        direction=CapitalDirection.VAULT_TO_VENUE,
        account_id="shared-capital-account",
        venue="BINANCE",
        vault_id="shared-vault",
        asset="USDT",
        network="SHADOW",
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
        environment=ExecutionEnvironment.SHADOW,
        account_id="shared-capital-account",
        venue="BINANCE",
        vault_id="shared-vault",
        asset="USDT",
        network="SHADOW",
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


def test_capital_roots_idempotency_and_sender_leases_are_isolated_by_team(
    service: TradingService,
) -> None:
    now = datetime.now(UTC)
    service = TradingService(
        service.database,
        credential_encryption_key=base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef")
        .decode()
        .rstrip("="),
    )
    admin = service.bootstrap_admin("capital-scope-admin", now=now)
    context = TradingQueries(service.database).user_context(admin)
    workspace_id = UUID(str(context["active_workspace"]["workspace_id"]))
    default_team_id = UUID(str(context["active_team"]["team_id"]))
    service.configure_notification_route(
        actor_id=admin,
        notification_route_id=None,
        name="Capital scope alerts",
        channel="SLACK",
        event_types=["CAPITAL_STATUS_CHANGED"],
        enabled=True,
        configuration={"webhook_url": "https://hooks.slack.com/services/T/B/capital-scope-fixture"},
        expected_version=0,
        idempotency_key="capital-route-create",
        now=now,
    )

    proposal_a = _create_transfer(service, admin, key="shared-proposal-key", now=now)
    service.enqueue_capital_status_notification(
        actor_id=admin,
        team_id=default_team_id,
        object_id=proposal_a,
        object_type="TransferProposal",
        status="DRAFT",
        environment="SHADOW",
        account_id="shared-capital-account",
        venue="BINANCE",
        object_version=1,
        summary="Team A capital proposal created.",
        now=now,
    )
    config_a = _set_direct_configuration(service, admin, key="shared-config-key", now=now)
    policy_a = _set_automation_policy(service, admin, key="shared-policy-key", now=now)
    sender_a = service.acquire_sender(
        "shared-capital-account:BINANCE",
        "team-a-worker",
        admin,
        now,
    )

    team_b_id = service.create_team(
        actor_id=admin,
        name="Capital Team B",
        slug="capital-team-b",
        idempotency_key="capital-team-b-create",
        now=now,
    )
    with service.database.session_factory.begin() as session:
        team_b = session.get(Team, team_b_id, with_for_update=True)
        assert team_b is not None
        team_b.trading_enabled = True

    proposal_b = _create_transfer(service, admin, key="shared-proposal-key", now=now)
    service.enqueue_capital_status_notification(
        actor_id=admin,
        team_id=team_b_id,
        object_id=proposal_b,
        object_type="TransferProposal",
        status="DRAFT",
        environment="SHADOW",
        account_id="shared-capital-account",
        venue="BINANCE",
        object_version=1,
        summary="Team B capital proposal created.",
        now=now,
    )
    config_b = _set_direct_configuration(service, admin, key="shared-config-key", now=now)
    policy_b = _set_automation_policy(service, admin, key="shared-policy-key", now=now)
    sender_b = service.acquire_sender(
        "shared-capital-account:BINANCE",
        "team-b-worker",
        admin,
        now,
    )

    assert proposal_b != proposal_a
    assert config_b != config_a
    assert policy_b != policy_a
    assert sender_a == sender_b == 1
    assert _create_transfer(service, admin, key="shared-proposal-key", now=now) == proposal_b

    center_b = TradingQueries(service.database).capital_center(admin)
    assert [item["transfer_proposal_id"] for item in center_b["proposals"]] == [str(proposal_b)]
    with pytest.raises(DomainRejected, match="TEAM_SCOPE_DENIED"):
        TradingQueries(service.database).transfer_proposal_detail(admin, proposal_a)
    with pytest.raises(DomainRejected, match="TEAM_SCOPE_DENIED"):
        service.submit_transfer_proposal(proposal_a, admin, now=now)

    with service.database.session_factory() as session:
        proposals = session.scalars(select(TransferProposal)).all()
        configs = session.scalars(select(DirectCapitalConfiguration)).all()
        policies = session.scalars(select(CapitalAutomationPolicy)).all()
        leases = session.scalars(select(SenderLease)).all()
        deliveries = session.scalars(select(NotificationDelivery)).all()
        assert {item.team_id for item in proposals} == {default_team_id, team_b_id}
        assert {item.team_id for item in configs} == {default_team_id, team_b_id}
        assert {item.team_id for item in policies} == {default_team_id, team_b_id}
        assert {(item.team_id, item.execution_scope) for item in leases} == {
            (default_team_id, "shared-capital-account:BINANCE"),
            (team_b_id, "shared-capital-account:BINANCE"),
        }
        assert len(deliveries) == 1
        assert deliveries[0].team_id == default_team_id
        assert deliveries[0].event_type == "CAPITAL_STATUS_CHANGED"

    service.select_scope(
        actor_id=admin,
        workspace_id=workspace_id,
        team_id=default_team_id,
        idempotency_key="capital-return-team-a",
        now=now,
    )
    center_a = TradingQueries(service.database).capital_center(admin)
    assert [item["transfer_proposal_id"] for item in center_a["proposals"]] == [str(proposal_a)]


def test_api_key_capital_history_is_filtered_by_exact_user_rbac_resource(
    service: TradingService,
) -> None:
    now = datetime.now(UTC)
    admin = service.bootstrap_admin("capital-history-admin", now=now)
    context = TradingQueries(service.database).user_context(admin)
    workspace_id = UUID(str(context["active_workspace"]["workspace_id"]))
    team_id = UUID(str(context["active_team"]["team_id"]))
    for venue in ("BINANCE", "BYBIT"):
        service.create_exchange_account(
            actor_id=admin,
            account_id="shared-history-account",
            venue=venue,
            label=f"{venue} history",
            credentials=None,
            idempotency_key=f"capital-history-{venue.lower()}",
            now=now,
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
