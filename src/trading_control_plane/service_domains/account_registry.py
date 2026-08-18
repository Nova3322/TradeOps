from __future__ import annotations

from trading_control_plane.service_component import ServiceComponent

# ruff: noqa: F403, F405
from trading_control_plane.service_core import *


def exchange_account_definition(
    account_id: str,
    venue: str,
    label: str | None,
) -> tuple[str, str, str]:
    normalized_account_id = account_id.strip()
    normalized_venue = venue.strip().upper()
    normalized_label = " ".join((label or normalized_account_id).strip().split())
    if (
        not normalized_account_id
        or normalized_account_id != account_id
        or len(normalized_account_id) > 120
        or ":" in normalized_account_id
    ):
        _reject(
            "EXCHANGE_ACCOUNT_INVALID",
            "account ID must be exact, non-empty, at most 120 characters, and contain no colon",
        )
    if normalized_venue not in SUPPORTED_EXCHANGE_VENUES:
        _reject("EXCHANGE_VENUE_UNSUPPORTED", "exchange venue is unsupported")
    if not normalized_label or len(normalized_label) > 120:
        _reject("EXCHANGE_ACCOUNT_INVALID", "account label must contain 1-120 characters")
    return normalized_account_id, normalized_venue, normalized_label


def delete_exchange_account(
    self: ServiceComponent,
    exchange_account_id: UUID,
    *,
    actor_id: UUID,
    confirmation: str,
    expected_version: int,
    idempotency_key: str,
    now: datetime,
) -> dict[str, str | int]:
    """Archive an account config while preserving immutable trading and audit history."""

    with self.database.session_factory.begin() as session:
        _actor, workspace, team = self.transactions._active_scope(session, actor_id)
        assert workspace is not None and team is not None
        account = session.scalar(
            select(ExchangeAccount)
            .where(
                ExchangeAccount.exchange_account_id == exchange_account_id,
                ExchangeAccount.team_id == team.team_id,
            )
            .with_for_update()
        )
        if account is None:
            _reject(
                "EXCHANGE_ACCOUNT_NOT_FOUND",
                "active exchange account is outside the active team or does not exist",
            )
        self.transactions._require_role(
            session,
            actor_id,
            "account.manage",
            account.account_id,
            account.venue,
            team_id=team.team_id,
        )
        caller = f"{actor_id}:{team.team_id}"
        payload = {
            "exchange_account_id": str(exchange_account_id),
            "confirmation": confirmation,
            "expected_version": expected_version,
        }
        digest, replay = self.transactions._idempotency(
            session,
            caller_id=caller,
            operation="exchange-account.delete",
            idempotency_key=idempotency_key,
            payload=payload,
        )
        if replay is not None:
            return replay
        if not account.active:
            _reject(
                "EXCHANGE_ACCOUNT_NOT_FOUND",
                "active exchange account is outside the active team or does not exist",
            )
        if account.version != expected_version:
            _reject("VERSION_CONFLICT", "exchange account version changed")
        expected_confirmation = f"DELETE:{account.environment}:{account.account_id}:{account.venue}"
        if confirmation != expected_confirmation:
            _reject(
                "SECOND_CONFIRMATION_REQUIRED",
                f"confirmation must exactly equal {expected_confirmation}",
            )
        scope = (
            account.team_id,
            account.environment,
            account.account_id,
            account.venue,
        )
        blockers: list[dict[str, int | str]] = []

        def add_blocker(code: str, count: int | None) -> None:
            if count:
                blockers.append({"code": code, "count": int(count)})

        add_blocker(
            "UNFINISHED_PROPOSALS",
            session.scalar(
                select(func.count(Proposal.proposal_id)).where(
                    Proposal.team_id == scope[0],
                    Proposal.environment == scope[1],
                    Proposal.account_id == scope[2],
                    Proposal.venue == scope[3],
                    Proposal.status.in_(("DRAFT", "PENDING_REVIEW", "APPROVED")),
                )
            ),
        )
        add_blocker(
            "ACTIVE_TRADING_AUTHORIZATIONS",
            session.scalar(
                select(func.count(TradingAuthorization.authorization_id)).where(
                    TradingAuthorization.team_id == scope[0],
                    TradingAuthorization.environment == scope[1],
                    TradingAuthorization.account_id == scope[2],
                    TradingAuthorization.venue == scope[3],
                    TradingAuthorization.active,
                )
            ),
        )
        add_blocker(
            "UNFINISHED_ORDER_INTENTS",
            session.scalar(
                select(func.count(OrderIntent.intent_id))
                .join(Campaign, Campaign.campaign_id == OrderIntent.campaign_id)
                .where(
                    Campaign.team_id == scope[0],
                    Campaign.environment == scope[1],
                    Campaign.account_id == scope[2],
                    Campaign.venue == scope[3],
                    OrderIntent.status.in_(ACTIVE_INTENT_STATUSES),
                )
            ),
        )
        add_blocker(
            "UNFINISHED_VENUE_ORDERS",
            session.scalar(
                select(func.count(VenueOrder.venue_order_fact_id)).where(
                    VenueOrder.team_id == scope[0],
                    VenueOrder.environment == scope[1],
                    VenueOrder.account_id == scope[2],
                    VenueOrder.venue == scope[3],
                    VenueOrder.status.in_(("SENT", "PARTIALLY_FILLED", "UNKNOWN")),
                )
            ),
        )
        add_blocker(
            "OPEN_OR_UNKNOWN_POSITIONS",
            session.scalar(
                select(func.count(Position.position_id)).where(
                    Position.team_id == scope[0],
                    Position.environment == scope[1],
                    Position.account_id == scope[2],
                    Position.venue == scope[3],
                    (Position.quantity != 0) | (Position.fact_status == FactStatus.UNKNOWN.value),
                )
            ),
        )
        add_blocker(
            "RUNNING_CAPITAL_TRANSFERS",
            session.scalar(
                select(func.count(CapitalTransfer.capital_transfer_id)).where(
                    CapitalTransfer.team_id == scope[0],
                    CapitalTransfer.environment == scope[1],
                    CapitalTransfer.account_id == scope[2],
                    CapitalTransfer.venue == scope[3],
                    CapitalTransfer.status.not_in(
                        {
                            CapitalTransferStatus.SETTLED.value,
                            CapitalTransferStatus.FAILED_SOURCE_RESTORED.value,
                        }
                    ),
                )
            ),
        )
        add_blocker(
            "RUNNING_DIRECT_CAPITAL_OPERATIONS",
            session.scalar(
                select(func.count(DirectCapitalOperation.operation_id)).where(
                    DirectCapitalOperation.team_id == scope[0],
                    DirectCapitalOperation.environment == scope[1],
                    DirectCapitalOperation.account_id == scope[2],
                    DirectCapitalOperation.venue == scope[3],
                    DirectCapitalOperation.status.not_in({"BLOCKED", "SETTLED"}),
                )
            ),
        )
        if blockers:
            _reject(
                "EXCHANGE_ACCOUNT_DELETE_BLOCKED",
                ";".join(f"{item['code']}={item['count']}" for item in blockers),
            )
        self._set_internal_principal_active(
            session,
            account.runtime_service_principal_id,
            False,
        )
        account.active = False
        account.deleted_at = now
        account.deleted_by = actor_id
        account.runtime_sync_enabled = False
        account.trading_status = "DISABLED"
        account.connection_status = "UNCONFIGURED"
        account.credentials_ciphertext = None
        account.credential_metadata = {}
        account.credential_version = 0
        account.connection_error_code = None
        account.last_connection_check_at = None
        account.last_verified_at = None
        account.freqtrade_worker_name = None
        account.freqtrade_worker_url = None
        account.freqtrade_worker_mode = "UNCONFIGURED"
        account.freqtrade_worker_status = "UNCONFIGURED"
        account.freqtrade_auth_ciphertext = None
        account.freqtrade_auth_metadata = {}
        account.freqtrade_auth_version = 0
        account.freqtrade_hip3_dexes = []
        account.freqtrade_error_code = None
        account.freqtrade_last_check_at = None
        account.freqtrade_last_verified_at = None
        account.version += 1
        account.updated_by = actor_id
        account.updated_at = now
        response: dict[str, str | int] = {
            "exchange_account_id": str(exchange_account_id),
            "status": "DELETED",
            "version": account.version,
        }
        self.transactions._audit(
            session,
            actor_id=str(actor_id),
            event_type="EXCHANGE_ACCOUNT_DELETED",
            object_type="ExchangeAccount",
            object_id=exchange_account_id,
            reason=(
                f"venue={account.venue};credentials=cleared;runtime=disabled;"
                "trading_history_retained=true"
            ),
            correlation_id=uuid4(),
            object_version=account.version,
            idempotency_key=idempotency_key,
            workspace_id=workspace.workspace_id,
            team_id=team.team_id,
            account_id=account.account_id,
            now=now,
        )
        self.transactions._save_receipt(
            session,
            caller_id=caller,
            operation="exchange-account.delete",
            idempotency_key=idempotency_key,
            semantic_hash=digest,
            response=response,
            now=now,
        )
        return response


def update_exchange_account(
    self: ServiceComponent,
    exchange_account_id: UUID,
    *,
    actor_id: UUID,
    label: str,
    expected_version: int,
    idempotency_key: str,
    now: datetime,
) -> dict[str, str | int]:
    normalized_label = " ".join(label.strip().split())
    if not normalized_label or len(normalized_label) > 120:
        _reject("EXCHANGE_ACCOUNT_INVALID", "account label must contain 1-120 characters")
    with self.database.session_factory.begin() as session:
        _actor, workspace, team = self.transactions._active_scope(session, actor_id)
        assert workspace is not None and team is not None
        account = session.scalar(
            select(ExchangeAccount)
            .where(
                ExchangeAccount.exchange_account_id == exchange_account_id,
                ExchangeAccount.team_id == team.team_id,
            )
            .with_for_update()
        )
        if account is None or not account.active:
            _reject("EXCHANGE_ACCOUNT_NOT_FOUND", "active exchange account does not exist")
        self.transactions._require_role(
            session, actor_id, "account.manage", account.account_id, account.venue
        )
        caller = f"{actor_id}:{team.team_id}"
        payload = {
            "exchange_account_id": str(exchange_account_id),
            "label": normalized_label,
            "expected_version": expected_version,
        }
        digest, replay = self.transactions._idempotency(
            session,
            caller_id=caller,
            operation="exchange-account.update",
            idempotency_key=idempotency_key,
            payload=payload,
        )
        if replay is not None:
            return replay
        if account.version != expected_version:
            _reject("VERSION_CONFLICT", "exchange account version changed")
        account.label = normalized_label
        account.version += 1
        account.updated_by = actor_id
        account.updated_at = now
        response: dict[str, str | int] = {
            "exchange_account_id": str(exchange_account_id),
            "version": account.version,
        }
        self.transactions._save_receipt(
            session,
            caller_id=caller,
            operation="exchange-account.update",
            idempotency_key=idempotency_key,
            semantic_hash=digest,
            response=response,
            now=now,
        )
        self.transactions._audit(
            session,
            actor_id=str(actor_id),
            event_type="EXCHANGE_ACCOUNT_UPDATED",
            object_type="ExchangeAccount",
            object_id=exchange_account_id,
            reason=f"environment={account.environment};label-updated=true",
            correlation_id=uuid4(),
            object_version=account.version,
            idempotency_key=idempotency_key,
            workspace_id=workspace.workspace_id,
            team_id=team.team_id,
            account_id=account.account_id,
            environment=account.environment,
            now=now,
        )
        return response


def set_exchange_account_state(
    self: ServiceComponent,
    exchange_account_id: UUID,
    *,
    actor_id: UUID,
    enabled: bool,
    confirmation: str,
    expected_version: int,
    idempotency_key: str,
    now: datetime,
) -> dict[str, str | int | bool]:
    expected_confirmation = "ENABLE_ACCOUNT" if enabled else "DISABLE_ACCOUNT"
    if confirmation != expected_confirmation:
        _reject(
            "SECOND_CONFIRMATION_REQUIRED",
            f"confirmation must exactly equal {expected_confirmation}",
        )
    with self.database.session_factory.begin() as session:
        _actor, workspace, team = self.transactions._active_scope(session, actor_id)
        assert workspace is not None and team is not None
        account = session.scalar(
            select(ExchangeAccount)
            .where(
                ExchangeAccount.exchange_account_id == exchange_account_id,
                ExchangeAccount.team_id == team.team_id,
            )
            .with_for_update()
        )
        if account is None:
            _reject("EXCHANGE_ACCOUNT_NOT_FOUND", "exchange account does not exist")
        self.transactions._require_role(
            session, actor_id, "account.manage", account.account_id, account.venue
        )
        caller = f"{actor_id}:{team.team_id}"
        payload = {
            "exchange_account_id": str(exchange_account_id),
            "enabled": enabled,
            "confirmation": confirmation,
            "expected_version": expected_version,
        }
        digest, replay = self.transactions._idempotency(
            session,
            caller_id=caller,
            operation="exchange-account.state",
            idempotency_key=idempotency_key,
            payload=payload,
        )
        if replay is not None:
            return replay
        if account.version != expected_version:
            _reject("VERSION_CONFLICT", "exchange account version changed")
        if enabled and (
            account.credentials_ciphertext is None
            or account.connection_status != "VERIFIED"
            or (account.credential_metadata or {}).get("environment") != account.environment
        ):
            _reject(
                "EXCHANGE_ACCOUNT_ENABLE_BLOCKED",
                "verified credentials bound to this environment are required",
            )
        account.active = enabled
        if not enabled:
            account.runtime_sync_enabled = False
            account.trading_status = "DISABLED"
            self._set_internal_principal_active(
                session, account.runtime_service_principal_id, False
            )
        account.version += 1
        account.updated_by = actor_id
        account.updated_at = now
        response: dict[str, str | int | bool] = {
            "exchange_account_id": str(exchange_account_id),
            "enabled": enabled,
            "version": account.version,
        }
        self.transactions._save_receipt(
            session,
            caller_id=caller,
            operation="exchange-account.state",
            idempotency_key=idempotency_key,
            semantic_hash=digest,
            response=response,
            now=now,
        )
        self.transactions._audit(
            session,
            actor_id=str(actor_id),
            event_type="EXCHANGE_ACCOUNT_ENABLED" if enabled else "EXCHANGE_ACCOUNT_DISABLED",
            object_type="ExchangeAccount",
            object_id=exchange_account_id,
            reason=f"environment={account.environment};enabled={str(enabled).lower()}",
            correlation_id=uuid4(),
            object_version=account.version,
            idempotency_key=idempotency_key,
            workspace_id=workspace.workspace_id,
            team_id=team.team_id,
            account_id=account.account_id,
            environment=account.environment,
            now=now,
        )
        return response
