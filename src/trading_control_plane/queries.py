from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select

from trading_control_plane.database import Database
from trading_control_plane.domain import DomainRejected, PrincipalType, Role
from trading_control_plane.models import (
    AccountEquity,
    Approval,
    Campaign,
    CapabilityGate,
    CapitalAutomationPolicy,
    CapitalTransfer,
    FundingPayment,
    Instrument,
    OrderIntent,
    Position,
    Proposal,
    ProtectionOrder,
    ReconciliationRun,
    RiskDecision,
    RiskReservation,
    RoleAssignment,
    SenderLease,
    TradingAuthorization,
    TransferAuthorization,
    TransferProposal,
    User,
    VenueFill,
    VenueOrder,
)
from trading_control_plane.service import TradingService


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


class TradingQueries:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.service = TradingService(database)

    def user_by_username(self, username: str) -> User:
        with self.database.session_factory() as session:
            user = session.scalar(select(User).where(User.username == username))
            if user is None or not user.active or user.principal_type != PrincipalType.HUMAN.value:
                raise DomainRejected("LOGIN_DENIED", "internal user is missing or inactive")
            session.expunge(user)
            return user

    def service_principal_by_username(self, username: str) -> User:
        with self.database.session_factory() as session:
            user = session.scalar(select(User).where(User.username == username))
            if (
                user is None
                or not user.active
                or user.principal_type != PrincipalType.SERVICE.value
            ):
                raise DomainRejected(
                    "PERPTAPE_PRINCIPAL_MISSING",
                    "configured Perptape service principal is missing or inactive",
                )
            session.expunge(user)
            return user

    def user_context(self, user_id: UUID) -> dict[str, Any]:
        with self.database.session_factory() as session:
            user = session.get(User, user_id)
            if user is None or not user.active:
                raise DomainRejected("SESSION_REVOKED", "internal user is inactive or missing")
            roles = session.scalars(
                select(RoleAssignment)
                .where(RoleAssignment.user_id == user_id)
                .order_by(RoleAssignment.role)
            ).all()
            return {
                "user_id": str(user.user_id),
                "username": user.username,
                "roles": [
                    {
                        "role": role.role,
                        "account_scope": role.account_scope,
                        "venue_scope": role.venue_scope,
                    }
                    for role in roles
                ],
            }

    def list_instruments(self, user_id: UUID) -> list[dict[str, Any]]:
        with self.database.session_factory() as session:
            user = session.get(User, user_id)
            if user is None or not user.active:
                raise DomainRejected("SESSION_REVOKED", "internal user is inactive or missing")
            assignments = session.scalars(
                select(RoleAssignment).where(RoleAssignment.user_id == user_id)
            ).all()
            values = session.scalars(
                select(Instrument)
                .where(Instrument.active)
                .order_by(Instrument.venue, Instrument.symbol)
            ).all()
            return [
                {
                    "instrument_id": str(item.instrument_id),
                    "venue": item.venue,
                    "symbol": item.symbol,
                    "tick_size": str(item.tick_size),
                    "lot_size": str(item.lot_size),
                    "minimum_notional": str(item.minimum_notional),
                    "contract_multiplier": str(item.contract_multiplier),
                    "quote_currency": item.quote_currency,
                    "collateral_currency": item.collateral_currency,
                    "protection_supported": item.protection_supported,
                    "updated_at": _iso(item.updated_at),
                }
                for item in values
                if any(
                    assignment.venue_scope is None or assignment.venue_scope == item.venue
                    for assignment in assignments
                )
            ]

    def instrument_id_by_venue_symbol(self, venue: str, symbol: str) -> UUID:
        with self.database.session_factory() as session:
            instrument = session.scalar(
                select(Instrument).where(
                    Instrument.venue == venue,
                    Instrument.symbol == symbol,
                    Instrument.active,
                )
            )
            if instrument is None:
                raise DomainRejected(
                    "INSTRUMENT_UNAVAILABLE",
                    "candidate instrument is not active in the Trading catalog",
                )
            return instrument.instrument_id

    def list_proposals(self, user_id: UUID, *, status: str | None = None) -> list[dict[str, Any]]:
        with self.database.session_factory() as session:
            statement = select(Proposal).order_by(Proposal.created_at.desc())
            if status is not None:
                statement = statement.where(Proposal.status == status)
            values = session.scalars(statement).all()
            return [
                self._proposal_summary(item)
                for item in values
                if self.service.can_user(user_id, "view", item.account_id, item.venue)
            ]

    def proposal_detail(self, user_id: UUID, proposal_id: UUID) -> dict[str, Any]:
        with self.database.session_factory() as session:
            proposal = session.get(Proposal, proposal_id)
            if proposal is None:
                raise DomainRejected("PROPOSAL_NOT_FOUND", "proposal does not exist")
            if not self.service.can_user(user_id, "view", proposal.account_id, proposal.venue):
                raise DomainRejected("RBAC_DENIED", "proposal is outside the current scope")
            approvals = session.scalars(
                select(Approval)
                .where(Approval.proposal_id == proposal_id)
                .order_by(Approval.created_at)
            ).all()
            risk = session.scalar(
                select(RiskDecision)
                .where(RiskDecision.proposal_id == proposal_id)
                .order_by(RiskDecision.created_at.desc())
                .limit(1)
            )
            authorization = session.scalar(
                select(TradingAuthorization).where(TradingAuthorization.proposal_id == proposal_id)
            )
            result = self._proposal_summary(proposal)
            result.update(
                {
                    "frozen_payload": proposal.frozen_payload,
                    "semantic_hash": proposal.semantic_hash,
                    "frozen_at": _iso(proposal.frozen_at),
                    "correlation_id": str(proposal.correlation_id),
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
                    "risk_decision": None
                    if risk is None
                    else {
                        "decision_id": str(risk.decision_id),
                        "result": risk.result,
                        "approved_quantity": str(risk.approved_quantity),
                        "risk_amount": str(risk.risk_amount),
                        "reasons": risk.reasons,
                        "data_as_of": _iso(risk.data_as_of),
                    },
                    "authorization": None
                    if authorization is None
                    else {
                        "authorization_id": str(authorization.authorization_id),
                        "environment": authorization.environment,
                        "quantity_limit": str(authorization.quantity_limit),
                        "risk_limit": str(authorization.risk_limit),
                        "allowed_adds": authorization.allowed_adds,
                        "used_adds": authorization.used_adds,
                        "active": authorization.active,
                        "expires_at": _iso(authorization.expires_at),
                    },
                }
            )
            return result

    def proposal_version(self, proposal_id: UUID) -> int:
        with self.database.session_factory() as session:
            proposal = session.get(Proposal, proposal_id)
            if proposal is None:
                raise DomainRejected("PROPOSAL_NOT_FOUND", "proposal does not exist")
            return proposal.version

    def reviewers_for_proposal(self, proposal_id: UUID) -> list[User]:
        with self.database.session_factory() as session:
            proposal = session.get(Proposal, proposal_id)
            if proposal is None:
                raise DomainRejected("PROPOSAL_NOT_FOUND", "proposal does not exist")
            assignments = session.scalars(
                select(RoleAssignment).where(RoleAssignment.role == Role.REVIEWER.value)
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

    def treasury_reviewers_for_transfer(self, transfer_proposal_id: UUID) -> list[User]:
        with self.database.session_factory() as session:
            proposal = session.get(TransferProposal, transfer_proposal_id)
            if proposal is None:
                raise DomainRejected(
                    "TRANSFER_PROPOSAL_NOT_FOUND", "transfer proposal does not exist"
                )
            assignments = session.scalars(
                select(RoleAssignment).where(RoleAssignment.role == Role.TREASURY_ADMIN.value)
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

    def treasury_users(self, account_id: str, venue: str) -> list[User]:
        with self.database.session_factory() as session:
            assignments = session.scalars(
                select(RoleAssignment).where(RoleAssignment.role == Role.TREASURY_ADMIN.value)
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

    def transfer_proposal_version(self, transfer_proposal_id: UUID) -> int:
        with self.database.session_factory() as session:
            proposal = session.get(TransferProposal, transfer_proposal_id)
            if proposal is None:
                raise DomainRejected(
                    "TRANSFER_PROPOSAL_NOT_FOUND", "transfer proposal does not exist"
                )
            return proposal.version

    @staticmethod
    def _transfer_proposal_summary(item: TransferProposal) -> dict[str, Any]:
        return {
            "transfer_proposal_id": str(item.transfer_proposal_id),
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
        with self.database.session_factory() as session:
            proposal = session.get(TransferProposal, transfer_proposal_id)
            if proposal is None:
                raise DomainRejected(
                    "TRANSFER_PROPOSAL_NOT_FOUND", "transfer proposal does not exist"
                )
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
                    TransferAuthorization.transfer_proposal_id == transfer_proposal_id
                )
            )
            result = self._transfer_proposal_summary(proposal)
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
            "reconciliation_status": item.reconciliation_status,
            "reconciliation_details": item.reconciliation_details,
            "version": item.version,
            "observed_at": _iso(item.observed_at),
            "reconciled_at": _iso(item.reconciled_at),
            "updated_at": _iso(item.updated_at),
        }

    def capital_transfer_detail(self, user_id: UUID, capital_transfer_id: UUID) -> dict[str, Any]:
        with self.database.session_factory() as session:
            transfer = session.get(CapitalTransfer, capital_transfer_id)
            if transfer is None:
                raise DomainRejected("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer is missing")
            if not self.service.can_user(
                user_id, "capital.view", transfer.account_id, transfer.venue
            ):
                raise DomainRejected("RBAC_DENIED", "capital transfer is outside scope")
            return self._capital_transfer_summary(transfer)

    def capital_center(self, user_id: UUID) -> dict[str, Any]:
        with self.database.session_factory() as session:
            capital_roles = {
                Role.OBSERVER.value,
                Role.PROPOSER.value,
                Role.REVIEWER.value,
                Role.OPERATOR.value,
                Role.TREASURY_ADMIN.value,
            }
            assignments = session.scalars(
                select(RoleAssignment).where(RoleAssignment.user_id == user_id)
            ).all()
            if not any(item.role in capital_roles for item in assignments):
                raise DomainRejected("RBAC_DENIED", "capital center access is not assigned")
            balances = session.scalars(
                select(AccountEquity).order_by(
                    AccountEquity.location_type,
                    AccountEquity.venue,
                    AccountEquity.account_id,
                )
            ).all()
            proposals = session.scalars(
                select(TransferProposal).order_by(TransferProposal.updated_at.desc())
            ).all()
            authorizations = session.scalars(select(TransferAuthorization)).all()
            authorization_by_proposal = {item.transfer_proposal_id: item for item in authorizations}
            transfers = session.scalars(
                select(CapitalTransfer).order_by(CapitalTransfer.updated_at.desc())
            ).all()
            policies = session.scalars(
                select(CapitalAutomationPolicy).order_by(
                    CapitalAutomationPolicy.environment,
                    CapitalAutomationPolicy.venue,
                    CapitalAutomationPolicy.account_id,
                )
            ).all()
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
            for item in balances:
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
                balance_data.append(
                    {
                        "account_equity_id": str(item.account_equity_id),
                        "environment": item.environment,
                        "location_type": item.location_type,
                        "location_id": item.account_id,
                        "venue": item.venue,
                        "asset": item.currency,
                        "equity": str(item.equity),
                        "confirmed_available": str(confirmed_available),
                        "source_reserved": str(occupied),
                        "effective_available": str(max(Decimal(0), confirmed_available - occupied)),
                        "control_status": item.control_status,
                        "deposit_status": item.deposit_status,
                        "network": item.network,
                        "address_reference": item.address_reference,
                        "fact_status": item.fact_status,
                        "observed_at": _iso(item.observed_at),
                    }
                )
            gate = session.get(CapabilityGate, "CAPITAL_TRANSFER")
            automation_gates = {
                key: (None if (value := session.get(CapabilityGate, key)) is None else value.status)
                for key in ("AUTO_PROFIT_SWEEP", "AUTO_OPERATING_REFILL")
            }
            return {
                "real_transfer_gate": None if gate is None else gate.status,
                "real_transfer_reason": None if gate is None else gate.reason,
                "balances": balance_data,
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

    def list_campaigns(self, user_id: UUID) -> list[dict[str, Any]]:
        with self.database.session_factory() as session:
            values = session.scalars(
                select(Campaign).order_by(Campaign.updated_at.desc(), Campaign.campaign_id)
            ).all()
            return [
                self._campaign_summary(item)
                for item in values
                if self.service.can_user(user_id, "view", item.account_id, item.venue)
            ]

    def campaign_detail(self, user_id: UUID, campaign_id: UUID) -> dict[str, Any]:
        with self.database.session_factory() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                raise DomainRejected("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            if not self.service.can_user(user_id, "view", campaign.account_id, campaign.venue):
                raise DomainRejected("RBAC_DENIED", "campaign is outside the current scope")
            instrument = session.get(Instrument, campaign.instrument_id)
            proposal = session.get(Proposal, campaign.proposal_id)
            authorization = session.get(TradingAuthorization, campaign.authorization_id)
            auto_add_gate = session.get(CapabilityGate, "AUTO_ADD")
            reservations = session.scalars(
                select(RiskReservation)
                .where(RiskReservation.campaign_id == campaign_id)
                .order_by(RiskReservation.created_at)
            ).all()
            intents = session.scalars(
                select(OrderIntent)
                .where(OrderIntent.campaign_id == campaign_id)
                .order_by(OrderIntent.created_at, OrderIntent.intent_id)
            ).all()
            intent_ids = [item.intent_id for item in intents]
            orders = (
                session.scalars(
                    select(VenueOrder).where(VenueOrder.order_intent_id.in_(intent_ids))
                ).all()
                if intent_ids
                else []
            )
            fills = session.scalars(
                select(VenueFill)
                .where(VenueFill.campaign_id == campaign_id)
                .order_by(VenueFill.executed_at, VenueFill.venue_fill_fact_id)
            ).all()
            position = session.scalar(
                select(Position).where(
                    Position.account_id == campaign.account_id,
                    Position.venue == campaign.venue,
                    Position.environment == campaign.environment,
                    Position.instrument_id == campaign.instrument_id,
                )
            )
            protection = (
                session.scalar(
                    select(ProtectionOrder).where(
                        ProtectionOrder.position_id == position.position_id
                    )
                )
                if position is not None
                else None
            )
            funding = session.scalars(
                select(FundingPayment)
                .where(FundingPayment.campaign_id == campaign_id)
                .order_by(FundingPayment.paid_at)
            ).all()
            scope = (
                f"{campaign.account_id}:{campaign.venue}"
                if campaign.environment == "SHADOW"
                else f"{campaign.environment}:{campaign.account_id}:{campaign.venue}"
            )
            reconciliation = session.scalar(
                select(ReconciliationRun)
                .where(ReconciliationRun.execution_scope == scope)
                .order_by(ReconciliationRun.completed_at.desc())
                .limit(1)
            )
            lease = session.get(SenderLease, scope)
            orders_by_intent = {item.order_intent_id: item for item in orders}
            result = self._campaign_summary(campaign)
            result.update(
                {
                    "instrument": None
                    if instrument is None
                    else {
                        "symbol": instrument.symbol,
                        "collateral_currency": instrument.collateral_currency,
                    },
                    "authorization": None
                    if authorization is None
                    else {
                        "authorization_id": str(authorization.authorization_id),
                        "environment": authorization.environment,
                        "active": authorization.active,
                        "quantity_limit": str(authorization.quantity_limit),
                        "used_quantity": str(authorization.used_quantity),
                        "allowed_adds": authorization.allowed_adds,
                        "used_adds": authorization.used_adds,
                        "expires_at": _iso(authorization.expires_at),
                    },
                    "reservations": [
                        {
                            "reservation_id": str(item.reservation_id),
                            "status": item.status,
                            "amount": str(item.amount),
                            "version": item.version,
                        }
                        for item in reservations
                    ],
                    "intents": [
                        {
                            "intent_id": str(item.intent_id),
                            "kind": item.kind,
                            "side": item.side,
                            "quantity": str(item.quantity),
                            "limit_price": (
                                None if item.limit_price is None else str(item.limit_price)
                            ),
                            "reduce_only": item.reduce_only,
                            "trigger_source": item.trigger_source,
                            "trigger_observed_at": _iso(item.trigger_observed_at),
                            "add_unit_consumed": item.add_unit_consumed,
                            "status": item.status,
                            "version": item.version,
                            "created_at": _iso(item.created_at),
                            "updated_at": _iso(item.updated_at),
                            "order": self._order_summary(orders_by_intent.get(item.intent_id)),
                        }
                        for item in intents
                    ],
                    "fills": [
                        {
                            "fill_id": str(item.venue_fill_fact_id),
                            "venue_fill_id": item.venue_fill_id,
                            "intent_id": str(item.order_intent_id),
                            "side": item.side,
                            "quantity": str(item.quantity),
                            "price": str(item.price),
                            "fee": str(item.fee),
                            "fee_currency": item.fee_currency,
                            "slippage_cost": str(item.slippage_cost),
                            "executed_at": _iso(item.executed_at),
                        }
                        for item in fills
                    ],
                    "position": None
                    if position is None
                    else {
                        "position_id": str(position.position_id),
                        "quantity": str(position.quantity),
                        "average_entry_price": str(position.average_entry_price),
                        "mark_price": str(position.mark_price),
                        "fact_status": position.fact_status,
                        "observed_at": _iso(position.observed_at),
                    },
                    "protection": None
                    if protection is None
                    else {
                        "protection_id": str(protection.protection_id),
                        "venue_order_id": protection.venue_order_id,
                        "quantity": str(protection.quantity),
                        "trigger_price": str(protection.trigger_price),
                        "status": protection.status,
                        "fully_covered": protection.fully_covered,
                        "observed_at": _iso(protection.observed_at),
                    },
                    "funding": [
                        {
                            "venue_payment_id": item.venue_payment_id,
                            "amount": str(item.amount),
                            "currency": item.currency,
                            "paid_at": _iso(item.paid_at),
                        }
                        for item in funding
                    ],
                    "reconciliation": None
                    if reconciliation is None
                    else {
                        "reconciliation_id": str(reconciliation.reconciliation_id),
                        "status": reconciliation.status,
                        "is_computed": reconciliation.is_computed,
                        "differences": reconciliation.differences,
                        "resolution_reason": reconciliation.resolution_reason,
                        "completed_at": _iso(reconciliation.completed_at),
                    },
                    "sender_lease": None
                    if lease is None
                    else {
                        "execution_scope": lease.execution_scope,
                        "owner_id": lease.owner_id,
                        "fencing_token": lease.fencing_token,
                        "expires_at": _iso(lease.expires_at),
                    },
                    "management": {
                        "auto_add_gate": (
                            "UNKNOWN" if auto_add_gate is None else auto_add_gate.status
                        ),
                        "allow_auto_add": bool(
                            proposal is not None
                            and isinstance(proposal.frozen_payload.get("details"), dict)
                            and proposal.frozen_payload["details"].get("allow_auto_add") is True
                        ),
                        "initial_quantity": (
                            None
                            if proposal is None
                            or not isinstance(proposal.frozen_payload.get("details"), dict)
                            else proposal.frozen_payload["details"].get("initial_quantity")
                        ),
                        "add_trigger_price": (
                            None
                            if proposal is None
                            or not isinstance(proposal.frozen_payload.get("details"), dict)
                            else proposal.frozen_payload["details"].get("add_trigger_price")
                        ),
                        "requested_adds": (
                            0
                            if proposal is None
                            or not isinstance(proposal.frozen_payload.get("details"), dict)
                            else proposal.frozen_payload["details"].get("requested_adds", 0)
                        ),
                        "remaining_quantity": (
                            "0"
                            if authorization is None or not authorization.active
                            else str(authorization.quantity_limit - authorization.used_quantity)
                        ),
                        "remaining_adds": (
                            0
                            if authorization is None or not authorization.active
                            else authorization.allowed_adds - authorization.used_adds
                        ),
                    },
                }
            )
            return result

    def campaign_id_for_intent(self, user_id: UUID, intent_id: UUID) -> UUID:
        with self.database.session_factory() as session:
            intent = session.get(OrderIntent, intent_id)
            if intent is None:
                raise DomainRejected("ORDER_INTENT_NOT_FOUND", "intent does not exist")
            campaign = session.get(Campaign, intent.campaign_id)
            if campaign is None:
                raise DomainRejected("CAMPAIGN_NOT_FOUND", "intent campaign is missing")
            if not self.service.can_user(user_id, "view", campaign.account_id, campaign.venue):
                raise DomainRejected("RBAC_DENIED", "intent is outside the current scope")
            return campaign.campaign_id

    def list_exceptions(self, user_id: UUID) -> list[dict[str, Any]]:
        exceptions: list[dict[str, Any]] = []
        for campaign in self.list_campaigns(user_id):
            detail = self.campaign_detail(user_id, UUID(str(campaign["campaign_id"])))
            campaign_id = str(campaign["campaign_id"])
            if campaign["status"] == "UNKNOWN":
                exceptions.append(self._exception(campaign_id, "CAMPAIGN_UNKNOWN", "BLOCKING"))
            for reservation in detail["reservations"]:
                if reservation["status"] == "UNKNOWN":
                    exceptions.append(
                        self._exception(campaign_id, "RISK_RESERVATION_UNKNOWN", "BLOCKING")
                    )
            for intent in detail["intents"]:
                if intent["status"] == "UNKNOWN":
                    exceptions.append(
                        self._exception(
                            campaign_id,
                            "ORDER_INTENT_UNKNOWN",
                            "BLOCKING",
                            object_id=str(intent["intent_id"]),
                        )
                    )
            position = detail["position"]
            if position is None or position["fact_status"] == "UNKNOWN":
                exceptions.append(self._exception(campaign_id, "POSITION_UNKNOWN", "BLOCKING"))
            elif Decimal(str(position["quantity"])) != 0:
                protection = detail["protection"]
                if protection is None or protection["status"] == "UNKNOWN":
                    exceptions.append(
                        self._exception(campaign_id, "PROTECTION_UNKNOWN", "BLOCKING")
                    )
                elif not protection["fully_covered"]:
                    exceptions.append(
                        self._exception(campaign_id, "PROTECTION_INSUFFICIENT", "BLOCKING")
                    )
            reconciliation = detail["reconciliation"]
            if reconciliation is not None and reconciliation["status"] != "MATCH":
                exceptions.append(
                    self._exception(
                        campaign_id,
                        f"RECONCILIATION_{reconciliation['status']}",
                        "BLOCKING",
                        details=list(reconciliation["differences"]),
                    )
                )
        return exceptions

    def venue_facts(
        self,
        user_id: UUID,
        account_id: str,
        venue: str,
        environment: str,
    ) -> dict[str, Any]:
        if not self.service.can_user(user_id, "view", account_id, venue):
            raise DomainRejected("RBAC_DENIED", "venue facts are outside the current scope")
        with self.database.session_factory() as session:
            instruments = session.scalars(
                select(Instrument).where(Instrument.venue == venue).order_by(Instrument.symbol)
            ).all()
            instrument_by_id = {item.instrument_id: item for item in instruments}
            positions = session.scalars(
                select(Position).where(
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
                    VenueOrder.account_id == account_id,
                    VenueOrder.venue == venue,
                    VenueOrder.environment == environment,
                )
                .order_by(VenueOrder.observed_at.desc())
            ).all()
            fills = session.scalars(
                select(VenueFill)
                .where(
                    VenueFill.account_id == account_id,
                    VenueFill.venue == venue,
                    VenueFill.environment == environment,
                )
                .order_by(VenueFill.executed_at.desc())
            ).all()
            funding = session.scalars(
                select(FundingPayment)
                .where(
                    FundingPayment.account_id == account_id,
                    FundingPayment.venue == venue,
                    FundingPayment.environment == environment,
                )
                .order_by(FundingPayment.paid_at.desc())
            ).all()
            equity = session.scalar(
                select(AccountEquity).where(
                    AccountEquity.account_id == account_id,
                    AccountEquity.venue == venue,
                    AccountEquity.environment == environment,
                )
            )
            return {
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
            }

    @staticmethod
    def _exception(
        campaign_id: str,
        code: str,
        severity: str,
        *,
        object_id: str | None = None,
        details: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "campaign_id": campaign_id,
            "object_id": object_id or campaign_id,
            "code": code,
            "severity": severity,
            "details": details or [],
        }

    @staticmethod
    def _order_summary(order: VenueOrder | None) -> dict[str, Any] | None:
        if order is None:
            return None
        return {
            "venue_order_fact_id": str(order.venue_order_fact_id),
            "venue_order_id": order.venue_order_id,
            "client_order_id": order.client_order_id,
            "status": order.status,
            "side": order.side,
            "order_type": order.order_type,
            "reduce_only": order.reduce_only,
            "ordered_quantity": str(order.ordered_quantity),
            "filled_quantity": str(order.filled_quantity),
            "observed_at": _iso(order.observed_at),
        }

    @staticmethod
    def _campaign_summary(campaign: Campaign) -> dict[str, Any]:
        return {
            "campaign_id": str(campaign.campaign_id),
            "proposal_id": str(campaign.proposal_id),
            "authorization_id": str(campaign.authorization_id),
            "account_id": campaign.account_id,
            "venue": campaign.venue,
            "environment": campaign.environment,
            "instrument_id": str(campaign.instrument_id),
            "direction": campaign.direction,
            "status": campaign.status,
            "current_target_quantity": str(campaign.current_target_quantity),
            "target_version": campaign.target_version,
            "target_reason": campaign.target_reason,
            "target_urgency": campaign.target_urgency,
            "target_calculated_at": _iso(campaign.target_calculated_at),
            "realized_pnl": str(campaign.realized_pnl),
            "unrealized_pnl": str(campaign.unrealized_pnl),
            "final_pnl": str(campaign.final_pnl),
            "created_at": _iso(campaign.created_at),
            "updated_at": _iso(campaign.updated_at),
        }

    @staticmethod
    def _proposal_summary(proposal: Proposal) -> dict[str, Any]:
        return {
            "proposal_id": str(proposal.proposal_id),
            "source": proposal.source,
            "environment": proposal.environment,
            "proposer_id": str(proposal.proposer_id),
            "strategy_id": proposal.strategy_id,
            "strategy_version": proposal.strategy_version,
            "source_candidate_id": proposal.source_candidate_id,
            "source_link": proposal.source_link,
            "source_observed_at": _iso(proposal.source_observed_at),
            "source_readiness": proposal.source_readiness,
            "status": proposal.status,
            "version": proposal.version,
            "risk_tier": proposal.risk_tier,
            "account_id": proposal.account_id,
            "venue": proposal.venue,
            "instrument_id": str(proposal.instrument_id),
            "direction": proposal.direction,
            "quantity": str(proposal.quantity),
            "max_risk": str(proposal.max_risk),
            "expires_at": _iso(proposal.expires_at),
            "created_at": _iso(proposal.created_at),
            "updated_at": _iso(proposal.updated_at),
        }
