from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import and_, false, func, or_, select

from trading_control_plane import domain, models, notilt, service
from trading_control_plane import execution_scope as scope_rules
from trading_control_plane.query_component import QueryComponent, iso_datetime

DIRECT_CAPITAL_EXPIRED_BLOCKER = "CAPITAL_DIRECT_OPERATION_EXPIRED"


def direct_capital_operation_summary(
    item: models.DirectCapitalOperation,
    now: datetime,
) -> dict[str, Any]:
    blockers = list(item.blockers)
    status = item.status
    if item.receipt_status == "NOT_SUBMITTED" and item.expires_at <= now:
        status = "BLOCKED"
        if DIRECT_CAPITAL_EXPIRED_BLOCKER not in blockers:
            blockers.append(DIRECT_CAPITAL_EXPIRED_BLOCKER)
    return {
        "operation_id": str(item.operation_id),
        "path": item.path,
        "treasury_provider": item.treasury_provider,
        "status": status,
        "receipt_status": item.receipt_status,
        "account_id": item.account_id,
        "venue": item.venue,
        "vault_id": item.vault_id,
        "asset": item.asset,
        "network": item.network,
        "amount": str(item.amount),
        "max_fee": None if item.max_fee is None else str(item.max_fee),
        "min_received": None if item.min_received is None else str(item.min_received),
        "source_reference_configured": item.source_reference is not None,
        "destination_reference_configured": item.destination_reference is not None,
        "stages": item.stages,
        "blockers": blockers,
        "execute_after": iso_datetime(item.execute_after),
        "expires_at": iso_datetime(item.expires_at),
        "final_confirmed_at": iso_datetime(item.final_confirmed_at),
        "version": item.version,
        "updated_at": iso_datetime(item.updated_at),
    }


class CapitalQueries(QueryComponent):
    def treasury_users(self, team_id: UUID, account_id: str, venue: str) -> list[models.User]:
        with self.database.session_factory() as session:
            assignments = session.scalars(
                select(models.RoleAssignment)
                .join(
                    models.TeamMembership,
                    and_(
                        models.TeamMembership.team_id == models.RoleAssignment.team_id,
                        models.TeamMembership.user_id == models.RoleAssignment.user_id,
                    ),
                )
                .where(
                    models.RoleAssignment.team_id == team_id,
                    models.RoleAssignment.role == domain.Role.TREASURY_ADMIN.value,
                    models.TeamMembership.active,
                )
            ).all()
            user_ids = {
                item.user_id
                for item in assignments
                if (item.account_scope is None or item.account_scope == account_id)
                and (item.venue_scope is None or item.venue_scope == venue)
            }
            users = session.scalars(
                select(models.User).where(models.User.user_id.in_(user_ids), models.User.active)
            ).all()
            for user in users:
                session.expunge(user)
            return list(users)

    def transfer_proposal_version(self, user_id: UUID, transfer_proposal_id: UUID) -> int:
        _workspace_id, team_id = self.active_scope_ids(user_id)
        with self.database.session_factory() as session:
            proposal = session.get(models.TransferProposal, transfer_proposal_id)
            if proposal is None:
                raise domain.DomainRejected(
                    "TRANSFER_PROPOSAL_NOT_FOUND", "transfer proposal does not exist"
                )
            if proposal.team_id != team_id:
                raise domain.DomainRejected(
                    "TEAM_SCOPE_DENIED", "transfer proposal is outside scope"
                )
            return proposal.version

    @staticmethod
    def _transfer_proposal_summary(item: models.TransferProposal) -> dict[str, Any]:
        return {
            "transfer_proposal_id": str(item.transfer_proposal_id),
            "team_id": str(item.team_id),
            "proposer_id": str(item.proposer_id),
            "environment": item.environment,
            "direction": item.direction,
            "purpose": item.purpose,
            "status": item.status,
            "version": item.version,
            "account_id": item.account_id,
            "venue": item.venue,
            "source_type": item.source_type,
            "source_id": item.source_id,
            "destination_type": item.destination_type,
            "destination_id": item.destination_id,
            "asset": item.asset,
            "network": item.network,
            "destination_reference": item.destination_reference,
            "amount": str(item.amount),
            "max_fee": str(item.max_fee),
            "min_received": str(item.min_received),
            "reason": item.reason,
            "frozen_at": iso_datetime(item.frozen_at),
            "expires_at": iso_datetime(item.expires_at),
            "created_at": iso_datetime(item.created_at),
            "updated_at": iso_datetime(item.updated_at),
        }

    def transfer_proposal_detail(self, user_id: UUID, transfer_proposal_id: UUID) -> dict[str, Any]:
        workspace_id, team_id = self.active_scope_ids(user_id)
        with self.database.session_factory() as session:
            proposal = session.get(models.TransferProposal, transfer_proposal_id)
            if proposal is None:
                raise domain.DomainRejected(
                    "TRANSFER_PROPOSAL_NOT_FOUND", "transfer proposal does not exist"
                )
            if proposal.team_id != team_id:
                raise domain.DomainRejected(
                    "TEAM_SCOPE_DENIED", "transfer proposal is outside scope"
                )
            if not self.can_user(user_id, "capital.view", proposal.account_id, proposal.venue):
                raise domain.DomainRejected("RBAC_DENIED", "transfer proposal is outside scope")
            approvals = session.scalars(
                select(models.Approval)
                .where(models.Approval.transfer_proposal_id == transfer_proposal_id)
                .order_by(models.Approval.created_at)
            ).all()
            authorization = session.scalar(
                select(models.TransferAuthorization).where(
                    models.TransferAuthorization.team_id == team_id,
                    models.TransferAuthorization.transfer_proposal_id == transfer_proposal_id,
                )
            )
            result = self._transfer_proposal_summary(proposal)
            result["workspace_id"] = str(workspace_id)
            result.update(
                {
                    "approvals": [
                        {
                            "approval_id": str(item.approval_id),
                            "reviewer_id": str(item.reviewer_id),
                            "decision": item.decision,
                            "reason": item.reason,
                            "created_at": iso_datetime(item.created_at),
                        }
                        for item in approvals
                    ],
                    "authorization": None
                    if authorization is None
                    else {
                        "transfer_authorization_id": str(authorization.transfer_authorization_id),
                        "active": authorization.active,
                        "version": authorization.version,
                        "expires_at": iso_datetime(authorization.expires_at),
                        "amount_limit": str(authorization.amount_limit),
                    },
                }
            )
            return result

    @staticmethod
    def _capital_transfer_summary(item: models.CapitalTransfer) -> dict[str, Any]:
        return {
            "capital_transfer_id": str(item.capital_transfer_id),
            "team_id": str(item.team_id),
            "transfer_authorization_id": str(item.transfer_authorization_id),
            "environment": item.environment,
            "account_id": item.account_id,
            "venue": item.venue,
            "direction": item.direction,
            "source_id": item.source_id,
            "destination_id": item.destination_id,
            "asset": item.asset,
            "network": item.network,
            "status": item.status,
            "gross_amount": str(item.gross_amount),
            "reserved_amount": str(item.reserved_amount),
            "fee_amount": None if item.fee_amount is None else str(item.fee_amount),
            "net_received": None if item.net_received is None else str(item.net_received),
            "external_transfer_id": item.external_transfer_id,
            "transaction_reference": item.transaction_reference,
            "transport": item.transport,
            "chain_id": item.chain_id,
            "transport_state": item.transport_state,
            "planned_transactions": item.planned_transactions,
            "confirmed_transaction_hashes": item.confirmed_transaction_hashes,
            "protocol_request_id": item.protocol_request_id,
            "protocol_execute_after": iso_datetime(item.protocol_execute_after),
            "protocol_expires_at": iso_datetime(item.protocol_expires_at),
            "reconciliation_status": item.reconciliation_status,
            "reconciliation_details": item.reconciliation_details,
            "version": item.version,
            "observed_at": iso_datetime(item.observed_at),
            "reconciled_at": iso_datetime(item.reconciled_at),
            "updated_at": iso_datetime(item.updated_at),
        }

    def capital_transfer_detail(self, user_id: UUID, capital_transfer_id: UUID) -> dict[str, Any]:
        workspace_id, team_id = self.active_scope_ids(user_id)
        with self.database.session_factory() as session:
            transfer = session.get(models.CapitalTransfer, capital_transfer_id)
            if transfer is None:
                raise domain.DomainRejected(
                    "CAPITAL_TRANSFER_NOT_FOUND", "capital transfer is missing"
                )
            if transfer.team_id != team_id:
                raise domain.DomainRejected(
                    "TEAM_SCOPE_DENIED", "capital transfer is outside scope"
                )
            if not self.can_user(user_id, "capital.view", transfer.account_id, transfer.venue):
                raise domain.DomainRejected("RBAC_DENIED", "capital transfer is outside scope")
            result = self._capital_transfer_summary(transfer)
            result["workspace_id"] = str(workspace_id)
            return result

    def capital_display(
        self,
        user_id: UUID,
        environment: str,
        selected_account_keys: set[str] | None = None,
    ) -> dict[str, Any]:
        """Return environment-scoped, multi-account read-only capital facts for charts."""

        normalized_environment = environment.strip().upper()
        if normalized_environment not in {"TESTNET", "LIVE"}:
            raise domain.DomainRejected(
                "CAPITAL_ENVIRONMENT_INVALID",
                "capital display requires TESTNET or LIVE",
            )
        workspace_id, team_id = self.active_scope_ids(user_id)
        with self.database.session_factory() as session:
            assignments = session.scalars(
                select(models.RoleAssignment).where(
                    models.RoleAssignment.user_id == user_id,
                    models.RoleAssignment.team_id == team_id,
                )
            ).all()
            if not any(
                "capital.view" in service.ROLE_ACTIONS[domain.Role(item.role)]
                or "*" in service.ROLE_ACTIONS[domain.Role(item.role)]
                for item in assignments
            ):
                raise domain.DomainRejected("RBAC_DENIED", "capital center access is not assigned")

            scope_access: dict[tuple[str | None, str | None], bool] = {}

            def can_view_scope(account_id: str | None = None, venue: str | None = None) -> bool:
                scope = (account_id, venue)
                if scope not in scope_access:
                    scope_access[scope] = self.can_user(
                        user_id,
                        "capital.view",
                        account_id,
                        venue,
                    )
                return scope_access[scope]

            def key_for(location_type: str, venue: str, account_id: str) -> str:
                prefix = "VAULT" if location_type == "VAULT" else venue.upper()
                return f"{prefix}|{account_id}"

            accounts = session.scalars(
                select(models.ExchangeAccount)
                .where(
                    models.ExchangeAccount.team_id == team_id,
                    models.ExchangeAccount.environment == normalized_environment,
                    models.ExchangeAccount.active,
                )
                .order_by(models.ExchangeAccount.venue, models.ExchangeAccount.label)
            ).all()
            options: dict[str, dict[str, Any]] = {
                key_for("VENUE", item.venue, item.account_id): {
                    "key": key_for("VENUE", item.venue, item.account_id),
                    "account_id": item.account_id,
                    "venue": item.venue,
                    "location_type": "VENUE",
                    "label": item.label,
                    "environment": normalized_environment,
                    "connection_status": item.connection_status,
                    "last_sync_at": iso_datetime(item.last_connection_check_at),
                    "selectable": False,
                    "disabled_reason": (
                        item.connection_error_code or "尚无可采信的资金数据"
                        if item.connection_status == "VERIFIED"
                        else (item.connection_error_code or "账户连接尚未验证")
                    ),
                }
                for item in accounts
                if can_view_scope(item.account_id, item.venue)
            }
            config = (
                session.scalar(
                    select(models.DirectCapitalConfiguration).where(
                        models.DirectCapitalConfiguration.team_id == team_id,
                        models.DirectCapitalConfiguration.environment == "LIVE",
                        models.DirectCapitalConfiguration.active,
                    )
                )
                if normalized_environment == "LIVE"
                else None
            )
            if config is not None:
                treasury_account = (
                    config.safe_address
                    if config.treasury_provider == "SAFE_SPENDING_LIMIT"
                    else (config.vault_address or config.vault_id)
                )
                if treasury_account:
                    key = key_for("VAULT", "VAULT", treasury_account)
                    options[key] = {
                        "key": key,
                        "account_id": treasury_account,
                        "venue": "VAULT",
                        "location_type": "VAULT",
                        "label": (
                            "Safe Spending Limits"
                            if config.treasury_provider == "SAFE_SPENDING_LIMIT"
                            else "NoTilt Vault"
                        ),
                        "environment": normalized_environment,
                        "connection_status": "VERIFIED",
                        "last_sync_at": None,
                        "selectable": False,
                        "disabled_reason": "尚无可采信的资金数据",
                    }
            balances = session.scalars(
                select(models.AccountEquity)
                .where(
                    models.AccountEquity.team_id == team_id,
                    models.AccountEquity.environment == normalized_environment,
                )
                .order_by(
                    models.AccountEquity.location_type,
                    models.AccountEquity.venue,
                    models.AccountEquity.account_id,
                )
            ).all()
            for item in balances:
                can_view = (
                    can_view_scope(item.account_id, item.venue)
                    if item.location_type == "VENUE"
                    else can_view_scope()
                )
                if not can_view:
                    continue
                key = key_for(item.location_type, item.venue, item.account_id)
                if key not in options:
                    continue
                if item.fact_status == "KNOWN":
                    options[key]["selectable"] = True
                    options[key]["disabled_reason"] = None
                    options[key]["last_sync_at"] = iso_datetime(item.observed_at)
            requested = set(selected_account_keys or ())
            selectable = {key for key, option in options.items() if option.get("selectable", False)}
            selected = requested.intersection(selectable) if requested else set()
            if not requested and selectable:
                selected = {next(key for key in options if key in selectable)}
            now = datetime.now(UTC)
            risk_policy = session.scalar(
                select(models.RiskPolicy).where(
                    models.RiskPolicy.team_id == team_id, models.RiskPolicy.active
                )
            )
            max_age = timedelta(
                seconds=(risk_policy.max_fact_age_seconds if risk_policy is not None else 300)
            )
            balance_data: list[dict[str, Any]] = []
            current_by_key: dict[str, Decimal] = {}
            source_as_of: dict[str, str | None] = {key: None for key in selected}
            issues: set[str] = set()
            for item in balances:
                key = key_for(item.location_type, item.venue, item.account_id)
                if key not in options or key not in selected:
                    continue
                valuation_time = item.observed_at
                usd_equity: Decimal | None
                valuation_price: Decimal | None
                if item.currency.upper() in notilt.USD_STABLE_ASSETS:
                    usd_equity = item.equity
                    valuation_price = Decimal(1)
                else:
                    usd_equity = item.valuation_equity
                    valuation_price = item.valuation_price
                    if item.valuation_observed_at is not None:
                        valuation_time = min(valuation_time, item.valuation_observed_at)
                current = bool(
                    item.fact_status == "KNOWN"
                    and usd_equity is not None
                    and valuation_price is not None
                    and valuation_price > 0
                    and not scope_rules.fact_is_stale(valuation_time, now, max_age)
                )
                if current:
                    assert usd_equity is not None
                    current_by_key[key] = current_by_key.get(key, Decimal(0)) + usd_equity
                    source_as_of[key] = iso_datetime(valuation_time)
                else:
                    issues.add(f"CURRENT_VALUE_MISSING:{key}")
                balance_data.append(
                    {
                        "account_equity_id": str(item.account_equity_id),
                        "environment": item.environment,
                        "location_type": item.location_type,
                        "location_id": item.account_id,
                        "account_key": key,
                        "source_label": options.get(key, {}).get("label", item.account_id),
                        "venue": item.venue,
                        "asset": item.currency,
                        "equity": str(item.equity),
                        "confirmed_available": str(
                            item.available_balance
                            if item.withdrawable_balance is None
                            else item.withdrawable_balance
                        ),
                        "source_reserved": "0",
                        "effective_available": str(
                            item.available_balance
                            if item.withdrawable_balance is None
                            else item.withdrawable_balance
                        ),
                        "control_status": item.control_status,
                        "deposit_status": item.deposit_status,
                        "usd_equity": str(usd_equity) if current else None,
                        "valuation_current": current,
                        "fact_status": item.fact_status,
                        "observed_at": iso_datetime(item.observed_at),
                    }
                )
            for key in selected - set(item["account_key"] for item in balance_data):
                issues.add(f"MISSING_ACCOUNT_SOURCE:{key}")
            observations = session.scalars(
                select(models.AccountEquityObservation)
                .where(
                    models.AccountEquityObservation.team_id == team_id,
                    models.AccountEquityObservation.environment == normalized_environment,
                )
                .order_by(models.AccountEquityObservation.observed_at)
            ).all()
            history = []
            for observation in observations:
                key = key_for(
                    observation.location_type,
                    observation.venue,
                    observation.account_id,
                )
                if key not in selected or not can_view_scope(
                    observation.account_id, observation.venue
                ):
                    continue
                history.append(
                    {
                        "source": key,
                        "source_label": options.get(key, {}).get("label", observation.account_id),
                        "location_type": observation.location_type,
                        "location_id": observation.account_id,
                        "account_key": key,
                        "venue": observation.venue,
                        "asset": observation.currency,
                        "equity": str(observation.equity),
                        "available_balance": str(observation.available_balance),
                        "usd_equity": (
                            None if observation.usd_equity is None else str(observation.usd_equity)
                        ),
                        "observed_at": iso_datetime(observation.observed_at),
                    }
                )
            return {
                "workspace_id": str(workspace_id),
                "team_id": str(team_id),
                "environment": normalized_environment,
                "account_options": list(options.values()),
                "selected_account_keys": sorted(selected),
                "balances": balance_data,
                "history": history,
                "net_worth": {
                    "environment": normalized_environment,
                    "currency": "USD",
                    "max_fact_age_seconds": int(max_age.total_seconds()),
                    "alignment_tolerance_seconds": 60,
                    "history_gap_tolerance_seconds": 300,
                    "source_as_of": source_as_of,
                    "accounts": {
                        key: (str(current_by_key[key]) if key in current_by_key else None)
                        for key in selected
                    },
                    "venues": {},
                    "vault": None,
                    "total": (
                        str(sum(current_by_key.values(), Decimal(0)))
                        if selected and selected.issubset(current_by_key) and not issues
                        else None
                    ),
                    "complete": bool(selected) and selected.issubset(current_by_key) and not issues,
                    "issues": sorted(issues),
                    "as_of": now.isoformat(),
                },
            }

    def capital_center(
        self,
        user_id: UUID,
        *,
        authoritative_live_treasury_account_id: str | None = None,
        require_authoritative_live_treasury: bool = False,
    ) -> dict[str, Any]:
        workspace_id, team_id = self.active_scope_ids(user_id)
        with self.database.session_factory() as session:
            now = datetime.now(UTC)
            active_live_accounts = set(
                session.execute(
                    select(models.ExchangeAccount.account_id, models.ExchangeAccount.venue).where(
                        models.ExchangeAccount.team_id == team_id,
                        models.ExchangeAccount.environment == "LIVE",
                        models.ExchangeAccount.active.is_(True),
                        models.ExchangeAccount.deleted_at.is_(None),
                    )
                ).all()
            )

            def is_active_live_venue(
                environment: str,
                location_type: str,
                venue: str,
                account_id: str,
            ) -> bool:
                if environment != "LIVE" or location_type != "VENUE":
                    return True
                return (account_id, venue) in active_live_accounts

            def is_authoritative_live_treasury(
                environment: str,
                location_type: str,
                account_id: str,
            ) -> bool:
                if environment != "LIVE" or location_type != "VAULT":
                    return True
                if authoritative_live_treasury_account_id is None:
                    return not require_authoritative_live_treasury
                return account_id.lower() == authoritative_live_treasury_account_id.lower()

            assignments = session.scalars(
                select(models.RoleAssignment).where(
                    models.RoleAssignment.user_id == user_id,
                    models.RoleAssignment.team_id == team_id,
                )
            ).all()
            if not any(
                "capital.view" in service.ROLE_ACTIONS[domain.Role(item.role)]
                or "*" in service.ROLE_ACTIONS[domain.Role(item.role)]
                for item in assignments
            ):
                raise domain.DomainRejected("RBAC_DENIED", "capital center access is not assigned")

            scope_access: dict[tuple[str | None, str | None], bool] = {}

            def can_view_scope(account_id: str | None = None, venue: str | None = None) -> bool:
                scope = (account_id, venue)
                if scope not in scope_access:
                    scope_access[scope] = self.can_user(
                        user_id,
                        "capital.view",
                        account_id,
                        venue,
                    )
                return scope_access[scope]

            def can_view_history(item: models.AccountEquityObservation) -> bool:
                if not is_active_live_venue(
                    item.environment,
                    item.location_type,
                    item.venue,
                    item.account_id,
                ):
                    return False
                if not is_authoritative_live_treasury(
                    item.environment,
                    item.location_type,
                    item.account_id,
                ):
                    return False
                return can_view_scope(item.account_id, item.venue)

            balances = session.scalars(
                select(models.AccountEquity)
                .where(models.AccountEquity.team_id == team_id)
                .order_by(
                    models.AccountEquity.location_type,
                    models.AccountEquity.venue,
                    models.AccountEquity.account_id,
                )
            ).all()
            proposals = session.scalars(
                select(models.TransferProposal)
                .where(models.TransferProposal.team_id == team_id)
                .order_by(models.TransferProposal.updated_at.desc())
            ).all()
            authorizations = session.scalars(
                select(models.TransferAuthorization).where(
                    models.TransferAuthorization.team_id == team_id
                )
            ).all()
            authorization_by_proposal = {item.transfer_proposal_id: item for item in authorizations}
            transfers = session.scalars(
                select(models.CapitalTransfer)
                .where(models.CapitalTransfer.team_id == team_id)
                .order_by(models.CapitalTransfer.updated_at.desc())
            ).all()
            direct_operations = session.scalars(
                select(models.DirectCapitalOperation)
                .where(models.DirectCapitalOperation.team_id == team_id)
                .order_by(models.DirectCapitalOperation.updated_at.desc())
            ).all()
            policies = session.scalars(
                select(models.CapitalAutomationPolicy)
                .where(models.CapitalAutomationPolicy.team_id == team_id)
                .order_by(
                    models.CapitalAutomationPolicy.environment,
                    models.CapitalAutomationPolicy.venue,
                    models.CapitalAutomationPolicy.account_id,
                )
            ).all()
            observation_query = select(models.AccountEquityObservation).where(
                models.AccountEquityObservation.team_id == team_id,
                models.AccountEquityObservation.environment == "LIVE",
            )
            if require_authoritative_live_treasury:
                observation_query = observation_query.where(
                    or_(
                        models.AccountEquityObservation.location_type != "VAULT",
                        (
                            false()
                            if authoritative_live_treasury_account_id is None
                            else func.lower(models.AccountEquityObservation.account_id)
                            == authoritative_live_treasury_account_id.lower()
                        ),
                    )
                )
            observations = list(
                reversed(
                    session.scalars(
                        observation_query.order_by(
                            models.AccountEquityObservation.observed_at.desc()
                        )
                    ).all()
                )
            )
            visible_observations = [item for item in observations if can_view_history(item)]
            risk_policy = session.scalar(
                select(models.RiskPolicy).where(
                    models.RiskPolicy.team_id == team_id,
                    models.RiskPolicy.active,
                )
            )
            max_fact_age = timedelta(
                seconds=(risk_policy.max_fact_age_seconds if risk_policy is not None else 300)
            )
            visible_transfers = [
                item for item in transfers if can_view_scope(item.account_id, item.venue)
            ]
            occupied_statuses = {
                "SOURCE_RESERVED",
                "SUBMITTED",
                "IN_FLIGHT",
                "DESTINATION_CONFIRMED",
                "UNKNOWN",
                "MANUAL_REQUIRED",
            }
            balance_data: list[dict[str, Any]] = []
            valuation_issues: set[str] = set()
            venue_net_worth: dict[str, Decimal] = {}
            vault_net_worth = Decimal(0)
            total_net_worth = Decimal(0)
            live_balance_count = 0
            live_sources: set[str] = set()
            current_live_sources: set[str] = set()
            latest_live_source_times: dict[str, datetime] = {}
            current_live_source_times: dict[str, datetime] = {}
            for item in balances:
                if not is_active_live_venue(
                    item.environment,
                    item.location_type,
                    item.venue,
                    item.account_id,
                ):
                    continue
                if not is_authoritative_live_treasury(
                    item.environment,
                    item.location_type,
                    item.account_id,
                ):
                    continue
                can_view = (
                    can_view_scope(item.account_id, item.venue)
                    if item.location_type == "VENUE"
                    else can_view_scope()
                )
                if not can_view:
                    continue
                occupied = sum(
                    (
                        transfer.reserved_amount
                        for transfer in visible_transfers
                        if transfer.environment == item.environment
                        and transfer.source_id == item.account_id
                        and transfer.asset == item.currency
                        and transfer.status in occupied_statuses
                    ),
                    Decimal(0),
                )
                confirmed_available = (
                    item.available_balance
                    if item.withdrawable_balance is None
                    else item.withdrawable_balance
                )
                valuation_time = item.observed_at
                usd_equity: Decimal | None
                valuation_price: Decimal | None
                if item.currency.upper() in notilt.USD_STABLE_ASSETS:
                    usd_equity = item.equity
                    valuation_price = Decimal(1)
                else:
                    usd_equity = item.valuation_equity
                    valuation_price = item.valuation_price
                    if item.valuation_observed_at is not None:
                        valuation_time = min(valuation_time, item.valuation_observed_at)
                valuation_current = (
                    item.fact_status == "KNOWN"
                    and usd_equity is not None
                    and valuation_price is not None
                    and valuation_price > 0
                    and not scope_rules.fact_is_stale(valuation_time, now, max_fact_age)
                )
                source = "VAULT" if item.location_type == "VAULT" else item.venue
                if item.environment == "LIVE":
                    previous_time = latest_live_source_times.get(source)
                    if previous_time is None or valuation_time > previous_time:
                        latest_live_source_times[source] = valuation_time
                if item.environment == "LIVE":
                    live_balance_count += 1
                    live_sources.add("VAULT" if item.location_type == "VAULT" else item.venue)
                if valuation_current and item.environment == "LIVE":
                    assert usd_equity is not None
                    current_live_sources.add(source)
                    previous_current_time = current_live_source_times.get(source)
                    if previous_current_time is None or valuation_time < previous_current_time:
                        current_live_source_times[source] = valuation_time
                    total_net_worth += usd_equity
                    if item.location_type == "VAULT":
                        vault_net_worth += usd_equity
                    else:
                        venue_net_worth[item.venue] = (
                            venue_net_worth.get(item.venue, Decimal(0)) + usd_equity
                        )
                elif item.environment == "LIVE":
                    source = "VAULT" if item.location_type == "VAULT" else item.venue
                    if item.fact_status != "KNOWN":
                        valuation_issues.add(f"CURRENT_VALUE_MISSING:{source}")
                    elif usd_equity is None or valuation_price is None or valuation_price <= 0:
                        valuation_issues.add(f"UNKNOWN_USD_VALUE:{source}")
                    elif scope_rules.fact_is_stale(valuation_time, now, max_fact_age):
                        valuation_issues.add(f"STALE_LIVE_SOURCE:{source}")
                    else:
                        valuation_issues.add(f"CURRENT_VALUE_MISSING:{source}")
                balance_data.append(
                    {
                        "account_equity_id": str(item.account_equity_id),
                        "environment": item.environment,
                        "location_type": item.location_type,
                        "location_id": (
                            "selected-onchain-treasury"
                            if item.environment == "LIVE" and item.location_type == "VAULT"
                            else item.account_id
                        ),
                        "venue": item.venue,
                        "asset": item.currency,
                        "equity": str(item.equity),
                        "confirmed_available": str(confirmed_available),
                        "source_reserved": str(occupied),
                        "effective_available": str(max(Decimal(0), confirmed_available - occupied)),
                        "control_status": item.control_status,
                        "deposit_status": item.deposit_status,
                        "network": item.network,
                        "address_reference": (
                            None
                            if item.environment == "LIVE" and item.location_type == "VAULT"
                            else item.address_reference
                        ),
                        "valuation_currency": (
                            "USD"
                            if item.currency.upper() in notilt.USD_STABLE_ASSETS
                            else item.valuation_currency
                        ),
                        "valuation_price": (
                            None if valuation_price is None else str(valuation_price)
                        ),
                        "usd_equity": (None if not valuation_current else str(usd_equity)),
                        "valuation_observed_at": iso_datetime(item.valuation_observed_at),
                        "valuation_current": valuation_current,
                        "fact_status": item.fact_status,
                        "observed_at": iso_datetime(item.observed_at),
                    }
                )
            for required_source in ("BINANCE", "HYPERLIQUID", "VAULT"):
                if required_source not in live_sources:
                    valuation_issues.add(f"MISSING_LIVE_SOURCE:{required_source}")
                elif required_source not in current_live_sources and not any(
                    issue.endswith(f":{required_source}") for issue in valuation_issues
                ):
                    valuation_issues.add(f"CURRENT_VALUE_MISSING:{required_source}")
            alignment_tolerance = timedelta(seconds=60)
            required_sources = {"BINANCE", "HYPERLIQUID", "VAULT"}
            if required_sources.issubset(current_live_source_times):
                newest_source_time = max(current_live_source_times.values())
                for source, source_time in current_live_source_times.items():
                    if newest_source_time - source_time > alignment_tolerance:
                        valuation_issues.add(f"TIME_MISALIGNED_SOURCE:{source}")
            gate = session.get(models.CapabilityGate, "CAPITAL_TRANSFER")
            automation_gates = {
                key: (
                    None
                    if (value := session.get(models.CapabilityGate, key)) is None
                    else value.status
                )
                for key in ("AUTO_PROFIT_SWEEP", "AUTO_OPERATING_REFILL")
            }
            return {
                "workspace_id": str(workspace_id),
                "team_id": str(team_id),
                "real_transfer_gate": None if gate is None else gate.status,
                "real_transfer_reason": None if gate is None else gate.reason,
                "balances": balance_data,
                "history": [
                    {
                        "source": ("VAULT" if item.location_type == "VAULT" else item.venue),
                        "location_type": item.location_type,
                        "location_id": (
                            "selected-onchain-treasury"
                            if item.location_type == "VAULT"
                            else item.account_id
                        ),
                        "venue": item.venue,
                        "asset": item.currency,
                        "equity": str(item.equity),
                        "available_balance": str(item.available_balance),
                        "usd_equity": (None if item.usd_equity is None else str(item.usd_equity)),
                        "observed_at": iso_datetime(item.observed_at),
                    }
                    for item in visible_observations
                ],
                "history_retention": {
                    "complete": True,
                    "minimum_interval_seconds": 60,
                    "first_observed_at": iso_datetime(visible_observations[0].observed_at)
                    if visible_observations
                    else None,
                    "last_observed_at": iso_datetime(visible_observations[-1].observed_at)
                    if visible_observations
                    else None,
                    "stored_observations": len(visible_observations),
                },
                "net_worth": {
                    "environment": "LIVE",
                    "currency": "USD",
                    "max_fact_age_seconds": int(max_fact_age.total_seconds()),
                    "alignment_tolerance_seconds": int(alignment_tolerance.total_seconds()),
                    "source_as_of": {
                        source: iso_datetime(latest_live_source_times.get(source))
                        for source in ("BINANCE", "HYPERLIQUID", "VAULT")
                    },
                    "venues": {
                        venue: (
                            str(venue_net_worth[venue]) if venue in current_live_sources else None
                        )
                        for venue in ("BINANCE", "HYPERLIQUID")
                    },
                    "vault": str(vault_net_worth) if "VAULT" in current_live_sources else None,
                    "total": (
                        str(total_net_worth)
                        if {"BINANCE", "HYPERLIQUID", "VAULT"}.issubset(current_live_sources)
                        and not valuation_issues
                        else None
                    ),
                    "complete": live_balance_count > 0 and not valuation_issues,
                    "issues": sorted(valuation_issues),
                    "as_of": now.isoformat(),
                },
                "in_transit": str(
                    sum(
                        (
                            item.reserved_amount
                            for item in visible_transfers
                            if item.status in occupied_statuses
                        ),
                        Decimal(0),
                    )
                ),
                "proposals": [
                    {
                        **self._transfer_proposal_summary(item),
                        "authorization": (
                            None
                            if (
                                authorization := authorization_by_proposal.get(
                                    item.transfer_proposal_id
                                )
                            )
                            is None
                            else {
                                "transfer_authorization_id": str(
                                    authorization.transfer_authorization_id
                                ),
                                "active": authorization.active,
                                "expires_at": iso_datetime(authorization.expires_at),
                            }
                        ),
                    }
                    for item in proposals
                    if self.can_user(user_id, "capital.view", item.account_id, item.venue)
                ],
                "transfers": [self._capital_transfer_summary(item) for item in visible_transfers],
                "direct_operations": [
                    direct_capital_operation_summary(item, now)
                    for item in direct_operations
                    if self.can_user(user_id, "capital.view", item.account_id, item.venue)
                ],
                "automation": {
                    "gates": automation_gates,
                    "policies": [
                        {
                            "policy_id": str(item.policy_id),
                            "environment": item.environment,
                            "account_id": item.account_id,
                            "venue": item.venue,
                            "vault_id": item.vault_id,
                            "asset": item.asset,
                            "network": item.network,
                            "operating_low": str(item.operating_low),
                            "operating_target": str(item.operating_target),
                            "operating_high": str(item.operating_high),
                            "vault_minimum_reserve": str(item.vault_minimum_reserve),
                            "minimum_transfer": str(item.minimum_transfer),
                            "maximum_transfer": str(item.maximum_transfer),
                            "max_fee": str(item.max_fee),
                            "active": item.active,
                            "version": item.version,
                            "updated_at": iso_datetime(item.updated_at),
                        }
                        for item in policies
                        if self.can_user(user_id, "capital.view", item.account_id, item.venue)
                    ],
                },
            }
