from __future__ import annotations

from trading_control_plane.query_component import QueryComponent

# ruff: noqa: F403, F405
from trading_control_plane.query_core import *


class AccountQueries(QueryComponent):
    def exchange_accounts(self, actor_id: UUID) -> dict[str, Any]:
        workspace_id, team_id = self._active_scope_ids(actor_id)
        with self.database.session_factory() as session:
            assignments = session.scalars(
                select(RoleAssignment).where(
                    RoleAssignment.user_id == actor_id,
                    RoleAssignment.team_id == team_id,
                )
            ).all()
            listing_actions = {"venue.view", "proposal.create"}

            def can_list_account(assignment: RoleAssignment) -> bool:
                actions = ROLE_ACTIONS[Role(assignment.role)]
                return "*" in actions or not listing_actions.isdisjoint(actions)

            if not any(can_list_account(item) for item in assignments):
                raise DomainRejected("RBAC_DENIED", "exchange account visibility is not assigned")
            accounts = session.scalars(
                select(ExchangeAccount)
                .where(
                    ExchangeAccount.team_id == team_id,
                    ExchangeAccount.deleted_at.is_(None),
                )
                .order_by(ExchangeAccount.venue, ExchangeAccount.label, ExchangeAccount.account_id)
            ).all()
            visible = [
                item
                for item in accounts
                if any(
                    self.service.can_user(actor_id, action, item.account_id, item.venue)
                    for action in listing_actions
                )
            ]

            def granted(account: ExchangeAccount, action: str) -> bool:
                if current_api_client_context() is not None and action in {
                    "account.manage",
                    "account.credentials.manage",
                }:
                    return False
                return self.service.can_user(
                    actor_id,
                    action,
                    account.account_id,
                    account.venue,
                )

            projected: list[dict[str, Any]] = []
            for item in visible:
                projection = self._exchange_account_projection(item)
                can_manage_credentials = granted(item, "account.credentials.manage")
                projection["permissions"] = {
                    "can_manage": granted(item, "account.manage"),
                    "can_delete": granted(item, "account.manage"),
                    "can_manage_trading": granted(item, "account.manage"),
                    "can_manage_credentials": can_manage_credentials,
                    "can_verify_connection": can_manage_credentials,
                    "can_manage_worker": can_manage_credentials,
                }
                if not can_manage_credentials:
                    projection["execution_worker"]["endpoint"] = None
                    projection["execution_worker"]["auth"]["username_hint"] = None
                projected.append(projection)
            return {
                "workspace_id": str(workspace_id),
                "team_id": str(team_id),
                "can_manage": any(
                    current_api_client_context() is None
                    and (
                        "account.manage" in ROLE_ACTIONS[Role(item.role)]
                        or "*" in ROLE_ACTIONS[Role(item.role)]
                    )
                    for item in assignments
                ),
                "supported_venues": ["BINANCE", "HYPERLIQUID", "OKX", "BYBIT"],
                "data": projected,
            }

    @staticmethod
    def _exchange_account_projection(item: ExchangeAccount) -> dict[str, Any]:
        metadata = dict(item.credential_metadata or {})
        credential_state = "UNCONFIGURED" if item.credential_version == 0 else "CONFIGURED"
        if item.trading_status == "ELIGIBLE":
            trading_reason = "account policy is eligible; global and task gates still apply"
        elif item.trading_status == "BLOCKED":
            trading_reason = (
                "account eligibility is blocked because a required connection "
                "or runtime fact was lost"
            )
        else:
            trading_reason = (
                "trading capability is disabled; connection status never enables order sending"
            )
        if credential_state == "UNCONFIGURED":
            next_action = "add encrypted credentials"
        elif item.connection_status != "VERIFIED":
            next_action = "run a supported no-side-effect connection verification"
        elif not item.runtime_sync_enabled:
            next_action = "enable the database-bound continuous read-only sync"
        elif item.freqtrade_worker_mode == "UNCONFIGURED":
            next_action = "bind one encrypted Freqtrade worker to this exact account"
        elif item.freqtrade_worker_mode != "LIVE" or item.freqtrade_worker_status != "VERIFIED":
            next_action = "verify an account-bound LIVE Freqtrade worker"
        elif item.trading_status == "ELIGIBLE":
            next_action = "verify global, sender, risk, and task gates before LIVE execution"
        else:
            next_action = "explicitly enable exact-account trading eligibility when approved"
        return {
            "exchange_account_id": str(item.exchange_account_id),
            "team_id": str(item.team_id),
            "account_id": item.account_id,
            "venue": item.venue,
            "environment": item.environment,
            "label": item.label,
            "registration_source": item.registration_source,
            "active": item.active,
            "version": item.version,
            "connection": {
                "status": item.connection_status,
                "error_code": item.connection_error_code,
                "checked_at": _iso(item.last_connection_check_at),
                "last_verified_at": _iso(item.last_verified_at),
                "read_only_capability": item.connection_status == "VERIFIED",
            },
            "trading": {
                "status": item.trading_status,
                "enabled": item.trading_status == "ELIGIBLE",
                "reason": trading_reason,
            },
            "credentials": {
                "state": credential_state,
                "version": item.credential_version,
                "configured_fields": list(metadata.get("configured_fields") or []),
                "key_hint": metadata.get("key_hint"),
                "signing_material_configured": bool(
                    metadata.get("signing_material_configured", False)
                ),
            },
            "runtime_binding": {
                "bound": item.runtime_sync_enabled,
                "source": "DATABASE_ENVELOPE",
                "read_only_connector": "IMPLEMENTED",
                "read_only_scope": (
                    "USDT_LINEAR_PERPETUALS"
                    if item.venue in {"OKX", "BYBIT"}
                    else "USD_M_PERPETUALS"
                    if item.venue == "BINANCE"
                    else "CORE_AND_CONFIGURED_HIP3"
                ),
                "connection_verification_connector": "IMPLEMENTED",
                "connection_verification_source": "DATABASE_ENVELOPE",
                "service_principal_configured": (item.runtime_service_principal_id is not None),
                "trading_connector": "FREQTRADE_EXTERNAL",
            },
            "execution_worker": {
                "supported": True,
                "configured": item.freqtrade_worker_mode != "UNCONFIGURED",
                "scope": {
                    "team_id": str(item.team_id),
                    "account_id": item.account_id,
                    "venue": item.venue,
                },
                "name": item.freqtrade_worker_name,
                "endpoint": item.freqtrade_worker_url,
                "mode": item.freqtrade_worker_mode,
                "status": item.freqtrade_worker_status,
                "error_code": item.freqtrade_error_code,
                "checked_at": _iso(item.freqtrade_last_check_at),
                "last_verified_at": _iso(item.freqtrade_last_verified_at),
                "hip3_dexes": list(item.freqtrade_hip3_dexes or []),
                "auth": {
                    "state": ("CONFIGURED" if item.freqtrade_auth_version > 0 else "UNCONFIGURED"),
                    "version": item.freqtrade_auth_version,
                    "username_hint": (item.freqtrade_auth_metadata or {}).get("username_hint"),
                },
                "live_ready": (
                    item.freqtrade_worker_mode == "LIVE"
                    and item.freqtrade_worker_status == "VERIFIED"
                ),
                "order_send": False,
            },
            "next_action": next_action,
            "created_at": _iso(item.created_at),
            "updated_at": _iso(item.updated_at),
        }

    def venue_facts(
        self,
        user_id: UUID,
        account_id: str,
        venue: str,
        environment: str,
    ) -> dict[str, Any]:
        if not self.service.can_user(user_id, "view", account_id, venue):
            raise DomainRejected("RBAC_DENIED", "venue facts are outside the current scope")
        workspace_id, team_id = self._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            account = session.scalar(
                select(ExchangeAccount.exchange_account_id).where(
                    ExchangeAccount.team_id == team_id,
                    ExchangeAccount.environment == environment,
                    ExchangeAccount.account_id == account_id,
                    ExchangeAccount.venue == venue,
                )
            )
            if account is None:
                raise DomainRejected(
                    "EXCHANGE_ACCOUNT_NOT_FOUND",
                    "venue facts require a registered account in the active team",
                )
            instruments = session.scalars(
                select(Instrument).where(Instrument.venue == venue).order_by(Instrument.symbol)
            ).all()
            instrument_by_id = {item.instrument_id: item for item in instruments}
            positions = session.scalars(
                select(Position).where(
                    Position.team_id == team_id,
                    Position.account_id == account_id,
                    Position.venue == venue,
                    Position.environment == environment,
                )
            ).all()
            protections = (
                session.scalars(
                    select(ProtectionOrder).where(
                        ProtectionOrder.position_id.in_([item.position_id for item in positions])
                    )
                ).all()
                if positions
                else []
            )
            protection_by_position = {item.position_id: item for item in protections}
            orders = session.scalars(
                select(VenueOrder)
                .where(
                    VenueOrder.team_id == team_id,
                    VenueOrder.account_id == account_id,
                    VenueOrder.venue == venue,
                    VenueOrder.environment == environment,
                )
                .order_by(VenueOrder.observed_at.desc())
            ).all()
            fills = session.scalars(
                select(VenueFill)
                .where(
                    VenueFill.team_id == team_id,
                    VenueFill.account_id == account_id,
                    VenueFill.venue == venue,
                    VenueFill.environment == environment,
                )
                .order_by(VenueFill.executed_at.desc())
            ).all()
            funding = session.scalars(
                select(FundingPayment)
                .where(
                    FundingPayment.team_id == team_id,
                    FundingPayment.account_id == account_id,
                    FundingPayment.venue == venue,
                    FundingPayment.environment == environment,
                )
                .order_by(FundingPayment.paid_at.desc())
            ).all()
            equity = session.scalar(
                select(AccountEquity).where(
                    AccountEquity.team_id == team_id,
                    AccountEquity.account_id == account_id,
                    AccountEquity.venue == venue,
                    AccountEquity.environment == environment,
                )
            )
            execution_scope = f"{environment}:{account_id}:{venue}"
            reconciliation = session.scalar(
                select(ReconciliationRun)
                .where(
                    ReconciliationRun.team_id == team_id,
                    ReconciliationRun.execution_scope == execution_scope,
                )
                .order_by(ReconciliationRun.completed_at.desc())
            )
            return {
                "workspace_id": str(workspace_id),
                "team_id": str(team_id),
                "account_id": account_id,
                "venue": venue,
                "environment": environment,
                "instruments": [
                    {
                        "instrument_id": str(item.instrument_id),
                        "symbol": item.symbol,
                        "tick_size": str(item.tick_size),
                        "lot_size": str(item.lot_size),
                        "minimum_notional": str(item.minimum_notional),
                        "active": item.active,
                        "updated_at": _iso(item.updated_at),
                    }
                    for item in instruments
                ],
                "positions": [
                    {
                        "position_id": str(item.position_id),
                        "instrument_id": str(item.instrument_id),
                        "symbol": instrument_by_id[item.instrument_id].symbol,
                        "quantity": str(item.quantity),
                        "average_entry_price": str(item.average_entry_price),
                        "mark_price": str(item.mark_price),
                        "fact_status": item.fact_status,
                        "observed_at": _iso(item.observed_at),
                        "protection": (
                            None
                            if item.position_id not in protection_by_position
                            else {
                                "venue_order_id": protection_by_position[
                                    item.position_id
                                ].venue_order_id,
                                "quantity": str(protection_by_position[item.position_id].quantity),
                                "trigger_price": str(
                                    protection_by_position[item.position_id].trigger_price
                                ),
                                "status": protection_by_position[item.position_id].status,
                                "fully_covered": protection_by_position[
                                    item.position_id
                                ].fully_covered,
                                "observed_at": _iso(
                                    protection_by_position[item.position_id].observed_at
                                ),
                            }
                        ),
                    }
                    for item in positions
                ],
                "orders": [
                    {
                        "venue_order_id": item.venue_order_id,
                        "client_order_id": item.client_order_id,
                        "instrument_id": str(item.instrument_id),
                        "symbol": instrument_by_id[item.instrument_id].symbol,
                        "intent_id": (
                            None if item.order_intent_id is None else str(item.order_intent_id)
                        ),
                        "status": item.status,
                        "side": item.side,
                        "order_type": item.order_type,
                        "reduce_only": item.reduce_only,
                        "ordered_quantity": str(item.ordered_quantity),
                        "filled_quantity": str(item.filled_quantity),
                        "observed_at": _iso(item.observed_at),
                    }
                    for item in orders
                ],
                "fills": [
                    {
                        "venue_fill_id": item.venue_fill_id,
                        "instrument_id": str(item.instrument_id),
                        "symbol": instrument_by_id[item.instrument_id].symbol,
                        "intent_id": (
                            None if item.order_intent_id is None else str(item.order_intent_id)
                        ),
                        "side": item.side,
                        "quantity": str(item.quantity),
                        "price": str(item.price),
                        "fee": str(item.fee),
                        "fee_currency": item.fee_currency,
                        "executed_at": _iso(item.executed_at),
                    }
                    for item in fills
                ],
                "funding": [
                    {
                        "venue_payment_id": item.venue_payment_id,
                        "instrument_id": str(item.instrument_id),
                        "symbol": instrument_by_id[item.instrument_id].symbol,
                        "amount": str(item.amount),
                        "currency": item.currency,
                        "paid_at": _iso(item.paid_at),
                    }
                    for item in funding
                ],
                "equity": None
                if equity is None
                else {
                    "equity": str(equity.equity),
                    "available_balance": str(equity.available_balance),
                    "currency": equity.currency,
                    "fact_status": equity.fact_status,
                    "observed_at": _iso(equity.observed_at),
                },
                "reconciliation": None
                if reconciliation is None
                else {
                    "reconciliation_id": str(reconciliation.reconciliation_id),
                    "status": reconciliation.status,
                    "is_computed": reconciliation.is_computed,
                    "differences": reconciliation.differences,
                    "completed_at": _iso(reconciliation.completed_at),
                },
            }
