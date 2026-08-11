from __future__ import annotations

from trading_control_plane.query_component import QueryComponent

# ruff: noqa: F403, F405
from trading_control_plane.query_core import *


class CapitalQueries(QueryComponent):
    def treasury_reviewers_for_transfer(self, transfer_proposal_id: UUID) -> list[User]:
        with self.database.session_factory() as session:
            proposal = session.get(TransferProposal, transfer_proposal_id)
            if proposal is None:
                raise DomainRejected(
                    "TRANSFER_PROPOSAL_NOT_FOUND", "transfer proposal does not exist"
                )
            assignments = session.scalars(
                select(RoleAssignment)
                .join(
                    TeamMembership,
                    and_(
                        TeamMembership.team_id == RoleAssignment.team_id,
                        TeamMembership.user_id == RoleAssignment.user_id,
                    ),
                )
                .where(
                    RoleAssignment.team_id == proposal.team_id,
                    RoleAssignment.role == Role.TREASURY_ADMIN.value,
                    TeamMembership.active,
                )
            ).all()
            reviewer_ids = {
                item.user_id
                for item in assignments
                if (item.account_scope is None or item.account_scope == proposal.account_id)
                and (item.venue_scope is None or item.venue_scope == proposal.venue)
                and item.user_id != proposal.proposer_id
            }
            users = session.scalars(
                select(User).where(User.user_id.in_(reviewer_ids), User.active)
            ).all()
            for user in users:
                session.expunge(user)
            return list(users)

    def treasury_users(self, team_id: UUID, account_id: str, venue: str) -> list[User]:
        with self.database.session_factory() as session:
            assignments = session.scalars(
                select(RoleAssignment)
                .join(
                    TeamMembership,
                    and_(
                        TeamMembership.team_id == RoleAssignment.team_id,
                        TeamMembership.user_id == RoleAssignment.user_id,
                    ),
                )
                .where(
                    RoleAssignment.team_id == team_id,
                    RoleAssignment.role == Role.TREASURY_ADMIN.value,
                    TeamMembership.active,
                )
            ).all()
            user_ids = {
                item.user_id
                for item in assignments
                if (item.account_scope is None or item.account_scope == account_id)
                and (item.venue_scope is None or item.venue_scope == venue)
            }
            users = session.scalars(
                select(User).where(User.user_id.in_(user_ids), User.active)
            ).all()
            for user in users:
                session.expunge(user)
            return list(users)

    def transfer_proposal_version(self, user_id: UUID, transfer_proposal_id: UUID) -> int:
        _workspace_id, team_id = self.facade._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            proposal = session.get(TransferProposal, transfer_proposal_id)
            if proposal is None:
                raise DomainRejected(
                    "TRANSFER_PROPOSAL_NOT_FOUND", "transfer proposal does not exist"
                )
            if proposal.team_id != team_id:
                raise DomainRejected("TEAM_SCOPE_DENIED", "transfer proposal is outside scope")
            return proposal.version

    @staticmethod
    def _transfer_proposal_summary(item: TransferProposal) -> dict[str, Any]:
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
            "frozen_at": _iso(item.frozen_at),
            "expires_at": _iso(item.expires_at),
            "created_at": _iso(item.created_at),
            "updated_at": _iso(item.updated_at),
        }

    def transfer_proposal_detail(self, user_id: UUID, transfer_proposal_id: UUID) -> dict[str, Any]:
        workspace_id, team_id = self.facade._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            proposal = session.get(TransferProposal, transfer_proposal_id)
            if proposal is None:
                raise DomainRejected(
                    "TRANSFER_PROPOSAL_NOT_FOUND", "transfer proposal does not exist"
                )
            if proposal.team_id != team_id:
                raise DomainRejected("TEAM_SCOPE_DENIED", "transfer proposal is outside scope")
            if not self.service.can_user(
                user_id, "capital.view", proposal.account_id, proposal.venue
            ):
                raise DomainRejected("RBAC_DENIED", "transfer proposal is outside scope")
            approvals = session.scalars(
                select(Approval)
                .where(Approval.transfer_proposal_id == transfer_proposal_id)
                .order_by(Approval.created_at)
            ).all()
            authorization = session.scalar(
                select(TransferAuthorization).where(
                    TransferAuthorization.team_id == team_id,
                    TransferAuthorization.transfer_proposal_id == transfer_proposal_id,
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
                            "created_at": _iso(item.created_at),
                        }
                        for item in approvals
                    ],
                    "authorization": None
                    if authorization is None
                    else {
                        "transfer_authorization_id": str(authorization.transfer_authorization_id),
                        "active": authorization.active,
                        "version": authorization.version,
                        "expires_at": _iso(authorization.expires_at),
                        "amount_limit": str(authorization.amount_limit),
                    },
                }
            )
            return result

    @staticmethod
    def _capital_transfer_summary(item: CapitalTransfer) -> dict[str, Any]:
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
            "protocol_execute_after": _iso(item.protocol_execute_after),
            "protocol_expires_at": _iso(item.protocol_expires_at),
            "reconciliation_status": item.reconciliation_status,
            "reconciliation_details": item.reconciliation_details,
            "version": item.version,
            "observed_at": _iso(item.observed_at),
            "reconciled_at": _iso(item.reconciled_at),
            "updated_at": _iso(item.updated_at),
        }

    def capital_transfer_detail(self, user_id: UUID, capital_transfer_id: UUID) -> dict[str, Any]:
        workspace_id, team_id = self.facade._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            transfer = session.get(CapitalTransfer, capital_transfer_id)
            if transfer is None:
                raise DomainRejected("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer is missing")
            if transfer.team_id != team_id:
                raise DomainRejected("TEAM_SCOPE_DENIED", "capital transfer is outside scope")
            if not self.service.can_user(
                user_id, "capital.view", transfer.account_id, transfer.venue
            ):
                raise DomainRejected("RBAC_DENIED", "capital transfer is outside scope")
            result = self._capital_transfer_summary(transfer)
            result["workspace_id"] = str(workspace_id)
            return result

    def capital_center(
        self,
        user_id: UUID,
        *,
        authoritative_live_accounts: dict[str, str] | None = None,
        authoritative_live_treasury_account_id: str | None = None,
        require_authoritative_live_treasury: bool = False,
    ) -> dict[str, Any]:
        workspace_id, team_id = self.facade._active_scope_ids(user_id)
        with self.database.session_factory() as session:
            now = datetime.now(UTC)
            authoritative_accounts = {
                venue.upper(): account_id
                for venue, account_id in (authoritative_live_accounts or {}).items()
                if account_id
            }

            def is_authoritative_live_venue(
                environment: str,
                location_type: str,
                venue: str,
                account_id: str,
            ) -> bool:
                if environment != "LIVE" or location_type != "VENUE":
                    return True
                expected = authoritative_accounts.get(venue.upper())
                return expected is None or expected == account_id

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
                select(RoleAssignment).where(
                    RoleAssignment.user_id == user_id,
                    RoleAssignment.team_id == team_id,
                )
            ).all()
            if not self.service.can_user(user_id, "capital.view"):
                raise DomainRejected("RBAC_DENIED", "capital center access is not assigned")
            treasury_assignments = [
                item
                for item in assignments
                if item.role in {Role.TREASURY_ADMIN.value, Role.SYSTEM_ADMIN.value}
            ]

            def can_view_history(item: AccountEquityObservation) -> bool:
                if not is_authoritative_live_venue(
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
                if item.location_type == "VAULT":
                    return any(
                        assignment.account_scope is None and assignment.venue_scope is None
                        for assignment in treasury_assignments
                    )
                return any(
                    (
                        assignment.account_scope is None
                        or assignment.account_scope == item.account_id
                    )
                    and (assignment.venue_scope is None or assignment.venue_scope == item.venue)
                    for assignment in treasury_assignments
                )

            balances = session.scalars(
                select(AccountEquity)
                .where(AccountEquity.team_id == team_id)
                .order_by(
                    AccountEquity.location_type,
                    AccountEquity.venue,
                    AccountEquity.account_id,
                )
            ).all()
            proposals = session.scalars(
                select(TransferProposal)
                .where(TransferProposal.team_id == team_id)
                .order_by(TransferProposal.updated_at.desc())
            ).all()
            authorizations = session.scalars(
                select(TransferAuthorization).where(TransferAuthorization.team_id == team_id)
            ).all()
            authorization_by_proposal = {item.transfer_proposal_id: item for item in authorizations}
            transfers = session.scalars(
                select(CapitalTransfer)
                .where(CapitalTransfer.team_id == team_id)
                .order_by(CapitalTransfer.updated_at.desc())
            ).all()
            direct_operations = session.scalars(
                select(DirectCapitalOperation)
                .where(DirectCapitalOperation.team_id == team_id)
                .order_by(DirectCapitalOperation.updated_at.desc())
            ).all()
            policies = session.scalars(
                select(CapitalAutomationPolicy)
                .where(CapitalAutomationPolicy.team_id == team_id)
                .order_by(
                    CapitalAutomationPolicy.environment,
                    CapitalAutomationPolicy.venue,
                    CapitalAutomationPolicy.account_id,
                )
            ).all()
            observation_query = select(AccountEquityObservation).where(
                AccountEquityObservation.team_id == team_id,
                AccountEquityObservation.environment == "LIVE",
            )
            if authoritative_accounts:
                authoritative_history_scopes = [
                    AccountEquityObservation.location_type == "VAULT",
                    *[
                        and_(
                            AccountEquityObservation.location_type == "VENUE",
                            func.upper(AccountEquityObservation.venue) == venue,
                            AccountEquityObservation.account_id == account_id,
                        )
                        for venue, account_id in authoritative_accounts.items()
                    ],
                ]
                observation_query = observation_query.where(or_(*authoritative_history_scopes))
            if require_authoritative_live_treasury:
                observation_query = observation_query.where(
                    or_(
                        AccountEquityObservation.location_type != "VAULT",
                        (
                            false()
                            if authoritative_live_treasury_account_id is None
                            else func.lower(AccountEquityObservation.account_id)
                            == authoritative_live_treasury_account_id.lower()
                        ),
                    )
                )
            observations = list(
                reversed(
                    session.scalars(
                        observation_query.order_by(AccountEquityObservation.observed_at.desc())
                    ).all()
                )
            )
            visible_observations = [item for item in observations if can_view_history(item)]
            risk_policy = session.scalar(
                select(RiskPolicy).where(
                    RiskPolicy.team_id == team_id,
                    RiskPolicy.active,
                )
            )
            max_fact_age = timedelta(
                seconds=(risk_policy.max_fact_age_seconds if risk_policy is not None else 300)
            )
            visible_transfers = [
                item
                for item in transfers
                if self.service.can_user(user_id, "capital.view", item.account_id, item.venue)
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
                if not is_authoritative_live_venue(
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
                    self.service.can_user(user_id, "capital.view", item.account_id, item.venue)
                    if item.location_type == "VENUE"
                    else self.service.can_user(user_id, "capital.view")
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
                if item.currency.upper() in USD_STABLE_ASSETS:
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
                    and not fact_is_stale(valuation_time, now, max_fact_age)
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
                    elif fact_is_stale(valuation_time, now, max_fact_age):
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
                            if item.currency.upper() in USD_STABLE_ASSETS
                            else item.valuation_currency
                        ),
                        "valuation_price": (
                            None if valuation_price is None else str(valuation_price)
                        ),
                        "usd_equity": (None if not valuation_current else str(usd_equity)),
                        "valuation_observed_at": _iso(item.valuation_observed_at),
                        "valuation_current": valuation_current,
                        "fact_status": item.fact_status,
                        "observed_at": _iso(item.observed_at),
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
            gate = session.get(CapabilityGate, "CAPITAL_TRANSFER")
            automation_gates = {
                key: (None if (value := session.get(CapabilityGate, key)) is None else value.status)
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
                        "observed_at": _iso(item.observed_at),
                    }
                    for item in visible_observations
                ],
                "history_retention": {
                    "complete": True,
                    "minimum_interval_seconds": 60,
                    "first_observed_at": _iso(visible_observations[0].observed_at)
                    if visible_observations
                    else None,
                    "last_observed_at": _iso(visible_observations[-1].observed_at)
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
                        source: _iso(latest_live_source_times.get(source))
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
                                "expires_at": _iso(authorization.expires_at),
                            }
                        ),
                    }
                    for item in proposals
                    if self.service.can_user(user_id, "capital.view", item.account_id, item.venue)
                ],
                "transfers": [self._capital_transfer_summary(item) for item in visible_transfers],
                "direct_operations": [
                    {
                        "operation_id": str(item.operation_id),
                        "path": item.path,
                        "treasury_provider": item.treasury_provider,
                        "status": item.status,
                        "receipt_status": item.receipt_status,
                        "account_id": item.account_id,
                        "venue": item.venue,
                        "vault_id": item.vault_id,
                        "asset": item.asset,
                        "network": item.network,
                        "amount": str(item.amount),
                        "max_fee": None if item.max_fee is None else str(item.max_fee),
                        "min_received": (
                            None if item.min_received is None else str(item.min_received)
                        ),
                        "source_reference_configured": item.source_reference is not None,
                        "destination_reference_configured": (
                            item.destination_reference is not None
                        ),
                        "stages": item.stages,
                        "blockers": item.blockers,
                        "execute_after": _iso(item.execute_after),
                        "expires_at": _iso(item.expires_at),
                        "final_confirmed_at": _iso(item.final_confirmed_at),
                        "version": item.version,
                        "updated_at": _iso(item.updated_at),
                    }
                    for item in direct_operations
                    if self.service.can_user(user_id, "capital.view", item.account_id, item.venue)
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
                            "updated_at": _iso(item.updated_at),
                        }
                        for item in policies
                        if self.service.can_user(
                            user_id, "capital.view", item.account_id, item.venue
                        )
                    ],
                },
            }
