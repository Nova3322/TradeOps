from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from trading_control_plane.capital_application import CapitalApplicationRuntime
from trading_control_plane.domain import (
    CapitalDirection,
    CapitalTransferStatus,
    DomainRejected,
    ExecutionEnvironment,
    ReviewDecision,
)
from trading_control_plane.notilt import NoTiltUnsignedTransaction


class CapitalBalanceFactInput(Protocol):
    environment: Literal["TESTNET", "LIVE"]
    location_type: Literal["VAULT", "VENUE"]
    location_id: str
    venue: str
    equity: Decimal
    available_balance: Decimal
    withdrawable_balance: Decimal
    asset: str
    control_status: Literal["CONTROLLED", "READ_ONLY", "UNKNOWN"]
    deposit_status: Literal["READY", "PENDING", "UNKNOWN"]
    network: str | None
    address_reference: str | None
    known: bool


class CapitalScopeReconciliationInput(Protocol):
    environment: Literal["TESTNET", "LIVE"]
    account_id: str
    venue: str


class CapitalAutomationPolicyInput(Protocol):
    environment: Literal["TESTNET", "LIVE"]
    account_id: str
    venue: str
    vault_id: str
    asset: str
    network: str
    vault_destination_reference: str
    venue_destination_reference: str
    operating_low: Decimal
    operating_target: Decimal
    operating_high: Decimal
    vault_minimum_reserve: Decimal
    minimum_transfer: Decimal
    maximum_transfer: Decimal
    max_fee: Decimal
    idempotency_key: str


class CapitalAutomationEvaluateInput(Protocol):
    purpose: Literal["AUTO_PROFIT_SWEEP", "AUTO_OPERATING_REFILL"]
    idempotency_key: str


class TransferProposalInput(Protocol):
    environment: Literal["TESTNET", "LIVE"]
    direction: CapitalDirection
    account_id: str
    venue: str
    vault_id: str
    asset: str
    network: str
    destination_reference: str
    amount: Decimal
    max_fee: Decimal
    min_received: Decimal
    reason: str
    expires_in_minutes: int
    idempotency_key: str


class TransferReviewInput(Protocol):
    decision: Literal["APPROVE", "REJECT"]
    reason: str
    expected_version: int
    action_grant: str | None


class TransferAuthorizationInput(Protocol):
    idempotency_key: str
    expires_in_minutes: int


class CapitalTransferCreateInput(Protocol):
    idempotency_key: str


class NoTiltReceiptInput(Protocol):
    transaction_hash: str


class CapitalTransferObservationInput(Protocol):
    status: Literal[
        "IN_FLIGHT",
        "DESTINATION_CONFIRMED",
        "UNKNOWN",
        "FAILED_SOURCE_RESTORED",
        "MANUAL_REQUIRED",
    ]
    transaction_reference: str | None
    fee_amount: Decimal | None
    net_received: Decimal | None


@dataclass(frozen=True, slots=True)
class CapitalTransferUseCases:
    runtime: CapitalApplicationRuntime

    def record_mock_capital_balance(
        self,
        request: CapitalBalanceFactInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        fact_id = self.runtime.service().record_capital_balance(
            actor_id=actor_id,
            environment=ExecutionEnvironment(request.environment),
            location_type=request.location_type,
            location_id=request.location_id,
            venue=request.venue,
            equity=request.equity,
            available_balance=request.available_balance,
            withdrawable_balance=request.withdrawable_balance,
            asset=request.asset,
            control_status=request.control_status,
            deposit_status=request.deposit_status,
            network=request.network,
            address_reference=request.address_reference,
            known=request.known,
            observed_at=self.runtime.clock(),
            now=self.runtime.clock(),
        )
        return {
            "transport": "MOCK_READ_ONLY_FACT",
            "account_equity_id": str(fact_id),
            "data": self.runtime.snapshot(actor_id),
        }

    def reconcile_capital_scope(
        self,
        request: CapitalScopeReconciliationInput,
        *,
        actor_id: UUID,
    ) -> dict[str, str]:
        reconciliation_id = self.runtime.service().record_capital_scope_reconciliation(
            actor_id=actor_id,
            environment=ExecutionEnvironment(request.environment),
            account_id=request.account_id,
            venue=request.venue,
            now=self.runtime.clock(),
        )
        return {"reconciliation_id": str(reconciliation_id)}

    def set_capital_automation_policy(
        self,
        request: CapitalAutomationPolicyInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        policy_id = self.runtime.service().set_capital_automation_policy(
            actor_id=actor_id,
            environment=ExecutionEnvironment(request.environment),
            account_id=request.account_id,
            venue=request.venue,
            vault_id=request.vault_id,
            asset=request.asset,
            network=request.network,
            vault_destination_reference=request.vault_destination_reference,
            venue_destination_reference=request.venue_destination_reference,
            operating_low=request.operating_low,
            operating_target=request.operating_target,
            operating_high=request.operating_high,
            vault_minimum_reserve=request.vault_minimum_reserve,
            minimum_transfer=request.minimum_transfer,
            maximum_transfer=request.maximum_transfer,
            max_fee=request.max_fee,
            idempotency_key=request.idempotency_key,
            now=self.runtime.clock(),
        )
        return {
            "policy_id": str(policy_id),
            "data": self.runtime.snapshot(actor_id),
        }

    def evaluate_capital_automation_policy(
        self,
        policy_id: UUID,
        request: CapitalAutomationEvaluateInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        proposal_id, reason = self.runtime.service().create_capital_automation_candidate(
            policy_id,
            request.purpose,
            actor_id,
            request.idempotency_key,
            now=self.runtime.clock(),
        )
        if proposal_id is not None:
            detail = self.runtime.queries().transfer_proposal_detail(actor_id, proposal_id)
            self.runtime.notify_capital(
                object_id=proposal_id,
                object_type="TransferProposal",
                event_type="PENDING_REVIEW",
                actor_id=actor_id,
                team_id=UUID(str(detail["team_id"])),
                environment=str(detail["environment"]),
                account_id=str(detail["account_id"]),
                venue=str(detail["venue"]),
                object_version=int(detail["version"]),
                summary="资金候选需要两名独立 Treasury Reviewer 审核。",
            )
        return {
            "transfer_proposal_id": None if proposal_id is None else str(proposal_id),
            "reason": reason,
            "data": self.runtime.snapshot(actor_id),
        }

    def create_transfer_proposal(
        self,
        request: TransferProposalInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        now = self.runtime.clock()
        environment = ExecutionEnvironment(request.environment)
        allow_live_unsigned = False
        if environment is ExecutionEnvironment.LIVE:
            chain_id = self.runtime.notilt_chain_id_for_network(request.network)
            _, configured_vault = self.runtime.configured_notilt_scope(chain_id)
            if request.vault_id.lower() != configured_vault.lower():
                raise DomainRejected(
                    "NOTILT_VAULT_SCOPE_MISMATCH",
                    "LIVE transfer proposal must use the configured Vault for its chain",
                )
            allow_live_unsigned = True
        proposal_id = self.runtime.service().create_transfer_proposal(
            actor_id=actor_id,
            environment=environment,
            direction=CapitalDirection(request.direction),
            account_id=request.account_id,
            venue=request.venue,
            vault_id=request.vault_id,
            asset=request.asset,
            network=request.network,
            destination_reference=request.destination_reference,
            amount=request.amount,
            max_fee=request.max_fee,
            min_received=request.min_received,
            reason=request.reason,
            expires_at=now + timedelta(minutes=request.expires_in_minutes),
            idempotency_key=request.idempotency_key,
            now=now,
            allow_live_unsigned=allow_live_unsigned,
        )
        return self.runtime.queries().transfer_proposal_detail(actor_id, proposal_id)

    def submit_transfer_proposal(
        self,
        transfer_proposal_id: UUID,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        self.runtime.service().submit_transfer_proposal(
            transfer_proposal_id, actor_id, now=self.runtime.clock()
        )
        detail = self.runtime.queries().transfer_proposal_detail(actor_id, transfer_proposal_id)
        self.runtime.notify_capital(
            object_id=transfer_proposal_id,
            object_type="TransferProposal",
            event_type="PENDING_REVIEW",
            actor_id=actor_id,
            team_id=UUID(str(detail["team_id"])),
            environment=str(detail["environment"]),
            account_id=str(detail["account_id"]),
            venue=str(detail["venue"]),
            object_version=int(detail["version"]),
            summary="资金划转提案需要两名独立 Treasury Reviewer 审核。",
        )
        return detail

    def review_transfer_proposal(
        self,
        transfer_proposal_id: UUID,
        request: TransferReviewInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        now = self.runtime.clock()
        if request.decision == "APPROVE":
            if request.action_grant is None:
                raise DomainRejected(
                    "ACTION_GRANT_REQUIRED", "capital approval requires action-level step-up"
                )
            self.runtime.token_service.verify_action_grant(
                request.action_grant,
                user_id=actor_id,
                action="capital.approve",
                object_id=transfer_proposal_id,
                object_version=request.expected_version,
                now=now,
            )
        self.runtime.service().review_transfer_proposal(
            transfer_proposal_id,
            actor_id,
            ReviewDecision(request.decision),
            request.reason,
            request.expected_version,
            now=now,
        )
        detail = self.runtime.queries().transfer_proposal_detail(actor_id, transfer_proposal_id)
        self.runtime.notify_capital(
            object_id=transfer_proposal_id,
            object_type="TransferProposal",
            event_type=f"REVIEW_{request.decision}",
            actor_id=actor_id,
            team_id=UUID(str(detail["team_id"])),
            environment=str(detail["environment"]),
            account_id=str(detail["account_id"]),
            venue=str(detail["venue"]),
            object_version=int(detail["version"]),
            summary=f"资金划转审核结果已记录：{request.decision}。",  # noqa: RUF001
        )
        return detail

    def issue_transfer_authorization(
        self,
        transfer_proposal_id: UUID,
        request: TransferAuthorizationInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        now = self.runtime.clock()
        proposal = self.runtime.queries().transfer_proposal_detail(actor_id, transfer_proposal_id)
        expires_at = min(
            datetime.fromisoformat(str(proposal["expires_at"])),
            now + timedelta(minutes=request.expires_in_minutes),
        )
        authorization_id = self.runtime.service().issue_transfer_authorization(
            transfer_proposal_id,
            actor_id,
            expires_at,
            request.idempotency_key,
            now=now,
        )
        return {
            "transfer_authorization_id": str(authorization_id),
            "detail": self.runtime.queries().transfer_proposal_detail(
                actor_id, transfer_proposal_id
            ),
        }

    def submit_mock_capital_transfer(
        self,
        transfer_authorization_id: UUID,
        request: CapitalTransferCreateInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        now = self.runtime.clock()
        transfer_id = self.runtime.service().reserve_capital_transfer(
            transfer_authorization_id,
            actor_id,
            request.idempotency_key,
            now=now,
        )
        detail = self.runtime.queries().capital_transfer_detail(actor_id, transfer_id)
        if detail["status"] == CapitalTransferStatus.SOURCE_RESERVED.value:
            command = self.runtime.service().capital_transfer_command(
                transfer_id, actor_id, now=now
            )
            submission = self.runtime.transfer_adapter.submit(command, now=now)
            self.runtime.service().record_capital_submission(
                transfer_id, actor_id, submission, now=now
            )
            detail = self.runtime.queries().capital_transfer_detail(actor_id, transfer_id)
        self.runtime.notify_capital(
            object_id=transfer_id,
            object_type="CapitalTransfer",
            event_type=str(detail["status"]),
            actor_id=actor_id,
            team_id=UUID(str(detail["team_id"])),
            environment=str(detail["environment"]),
            account_id=str(detail["account_id"]),
            venue=str(detail["venue"]),
            object_version=int(detail["version"]),
            summary="Mock 资金划转已提交；没有移动真实资金。",  # noqa: RUF001
        )
        return {"transport": "MOCK_ONLY", "detail": detail}

    def prepare_notilt_capital_transfer(
        self,
        transfer_authorization_id: UUID,
        request: CapitalTransferCreateInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        now = self.runtime.clock()
        transfer_id = self.runtime.service().reserve_capital_transfer(
            transfer_authorization_id,
            actor_id,
            request.idempotency_key,
            now=now,
            allow_live_unsigned=True,
        )
        existing = self.runtime.queries().capital_transfer_detail(actor_id, transfer_id)
        if (
            existing["transport_state"]
            in {
                "DEPOSIT_PLAN_READY",
                "RELEASE_REQUEST_PLAN_READY",
            }
            and existing["planned_transactions"]
        ):
            return {
                "transport": "NOTILT_UNSIGNED_TRANSACTION_HANDOFF",
                "broadcast": False,
                "signing": "EXTERNAL_WALLET_REQUIRED",
                "capital_transfer_id": str(transfer_id),
                "reserved_gross_amount": existing["gross_amount"],
                "planned_net_amount": str(
                    self.runtime.service()
                    .notilt_transfer_command(transfer_id, actor_id)
                    .min_received
                ),
                "transactions": existing["planned_transactions"],
                "next_step": (
                    "Confirm the exact persisted transaction plan in the independent wallet."
                ),
                "detail": existing,
            }
        command = self.runtime.service().capital_transfer_command(
            transfer_id,
            actor_id,
            now=now,
        )
        if command.environment is not ExecutionEnvironment.LIVE:
            raise DomainRejected(
                "NOTILT_TRANSFER_ENVIRONMENT_INVALID",
                "NoTilt transaction plans are only available for LIVE authorizations",
            )
        chain_id = self.runtime.notilt_chain_id_for_network(command.network)
        agent, vault = self.runtime.configured_notilt_scope(chain_id)
        vault_endpoint = (
            command.source_id
            if command.direction is CapitalDirection.VAULT_TO_VENUE
            else command.destination_id
        )
        if vault_endpoint.lower() != vault.lower():
            raise DomainRejected(
                "NOTILT_VAULT_SCOPE_MISMATCH",
                "capital authorization does not reference the configured Vault",
            )
        transactions: tuple[NoTiltUnsignedTransaction, ...]
        if command.direction is CapitalDirection.VAULT_TO_VENUE:
            transactions = (
                self.runtime.notilt.prepare_release_request(
                    chain_id=chain_id,
                    vault=vault,
                    agent=agent,
                    asset=command.asset,
                    amount=str(command.min_received),
                ),
            )
            plan_state = "RELEASE_REQUEST_PLAN_READY"
            next_step = (
                "Confirm the release request in the independent wallet, wait for the "
                "protocol release window, then prepare and confirm release execution."
            )
        else:
            transactions = self.runtime.notilt.prepare_deposit(
                chain_id=chain_id,
                vault=vault,
                agent=agent,
                asset=command.asset,
                amount=str(command.min_received),
            )
            plan_state = "DEPOSIT_PLAN_READY"
            next_step = (
                "Funds must already be present in the independent wallet after the "
                "venue withdrawal; confirm each unsigned deposit transaction there."
            )
        self.runtime.service().record_notilt_plan(
            transfer_id,
            actor_id,
            chain_id=chain_id,
            transport_state=plan_state,
            transactions=transactions,
            now=now,
        )
        detail = self.runtime.queries().capital_transfer_detail(actor_id, transfer_id)
        return {
            "transport": "NOTILT_UNSIGNED_TRANSACTION_HANDOFF",
            "broadcast": False,
            "signing": "EXTERNAL_WALLET_REQUIRED",
            "capital_transfer_id": str(transfer_id),
            "reserved_gross_amount": str(command.gross_amount),
            "planned_net_amount": str(command.min_received),
            "transactions": detail["planned_transactions"],
            "next_step": next_step,
            "detail": detail,
        }

    def prepare_notilt_release_execution(
        self,
        capital_transfer_id: UUID,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        now = self.runtime.clock()
        detail = self.runtime.queries().capital_transfer_detail(actor_id, capital_transfer_id)
        if detail["transport_state"] == "RELEASE_EXECUTION_PLAN_READY":
            return {
                "transport": "NOTILT_UNSIGNED_TRANSACTION_HANDOFF",
                "broadcast": False,
                "signing": "EXTERNAL_WALLET_REQUIRED",
                "transactions": detail["planned_transactions"],
                "detail": detail,
            }
        if (
            detail["transport_state"] != "RELEASE_REQUEST_CONFIRMED"
            or detail["status"] != CapitalTransferStatus.IN_FLIGHT.value
            or detail["protocol_request_id"] is None
        ):
            raise DomainRejected(
                "NOTILT_RELEASE_NOT_EXECUTABLE",
                "verified release request is not ready for execution",
            )
        execute_after = datetime.fromisoformat(str(detail["protocol_execute_after"]))
        expires_at = datetime.fromisoformat(str(detail["protocol_expires_at"]))
        if now < execute_after:
            raise DomainRejected(
                "NOTILT_RELEASE_NOT_UNLOCKED",
                f"NoTilt release unlocks at {execute_after.isoformat()}",
            )
        if now >= expires_at:
            raise DomainRejected("NOTILT_RELEASE_EXPIRED", "NoTilt release request expired")
        command = self.runtime.service().notilt_transfer_command(capital_transfer_id, actor_id)
        chain_id = self.runtime.notilt_chain_id_for_network(command.network)
        agent, vault = self.runtime.configured_notilt_scope(chain_id)
        transaction = self.runtime.notilt.prepare_release_execution(
            chain_id=chain_id,
            vault=vault,
            agent=agent,
            request_id=str(detail["protocol_request_id"]),
        )
        self.runtime.service().record_notilt_plan(
            capital_transfer_id,
            actor_id,
            chain_id=chain_id,
            transport_state="RELEASE_EXECUTION_PLAN_READY",
            transactions=(transaction,),
            now=now,
        )
        updated = self.runtime.queries().capital_transfer_detail(actor_id, capital_transfer_id)
        return {
            "transport": "NOTILT_UNSIGNED_TRANSACTION_HANDOFF",
            "broadcast": False,
            "signing": "EXTERNAL_WALLET_REQUIRED",
            "transactions": updated["planned_transactions"],
            "detail": updated,
        }

    def prepare_notilt_release_cancellation(
        self,
        capital_transfer_id: UUID,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        now = self.runtime.clock()
        detail = self.runtime.queries().capital_transfer_detail(actor_id, capital_transfer_id)
        if detail["transport_state"] == "RELEASE_CANCELLATION_PLAN_READY":
            return {
                "transport": "NOTILT_UNSIGNED_TRANSACTION_HANDOFF",
                "broadcast": False,
                "signing": "EXTERNAL_WALLET_REQUIRED",
                "transactions": detail["planned_transactions"],
                "detail": detail,
            }
        if (
            detail["transport_state"] != "RELEASE_REQUEST_CONFIRMED"
            or detail["protocol_request_id"] is None
            or detail["status"]
            not in {
                CapitalTransferStatus.IN_FLIGHT.value,
                CapitalTransferStatus.MANUAL_REQUIRED.value,
            }
        ):
            raise DomainRejected(
                "NOTILT_RELEASE_NOT_CANCELLABLE",
                "verified release request is not available for cancellation",
            )
        command = self.runtime.service().notilt_transfer_command(capital_transfer_id, actor_id)
        chain_id = self.runtime.notilt_chain_id_for_network(command.network)
        agent, vault = self.runtime.configured_notilt_scope(chain_id)
        transaction = self.runtime.notilt.prepare_release_cancellation(
            chain_id=chain_id,
            vault=vault,
            agent=agent,
            request_id=str(detail["protocol_request_id"]),
        )
        self.runtime.service().record_notilt_plan(
            capital_transfer_id,
            actor_id,
            chain_id=chain_id,
            transport_state="RELEASE_CANCELLATION_PLAN_READY",
            transactions=(transaction,),
            now=now,
        )
        updated = self.runtime.queries().capital_transfer_detail(actor_id, capital_transfer_id)
        return {
            "transport": "NOTILT_UNSIGNED_TRANSACTION_HANDOFF",
            "broadcast": False,
            "signing": "EXTERNAL_WALLET_REQUIRED",
            "transactions": updated["planned_transactions"],
            "detail": updated,
        }

    def verify_notilt_capital_receipt(
        self,
        capital_transfer_id: UUID,
        request: NoTiltReceiptInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        now = self.runtime.clock()
        detail = self.runtime.queries().capital_transfer_detail(actor_id, capital_transfer_id)
        receipt_kind = {
            "DEPOSIT_PLAN_READY": "DEPOSIT",
            "RELEASE_REQUEST_PLAN_READY": "RELEASE_REQUEST",
            "RELEASE_EXECUTION_PLAN_READY": "RELEASE_EXECUTION",
            "RELEASE_CANCELLATION_PLAN_READY": "RELEASE_CANCELLATION",
        }.get(str(detail["transport_state"]))
        if receipt_kind is None:
            if request.transaction_hash in detail["confirmed_transaction_hashes"]:
                return {
                    "transport": "NOTILT_VERIFIED_RECEIPT",
                    "idempotent": True,
                    "detail": detail,
                }
            raise DomainRejected(
                "NOTILT_RECEIPT_STATE_INVALID",
                "capital transfer is not waiting for a NoTilt receipt",
            )
        command = self.runtime.service().notilt_transfer_command(capital_transfer_id, actor_id)
        chain_id = self.runtime.notilt_chain_id_for_network(command.network)
        agent, vault = self.runtime.configured_notilt_scope(chain_id)
        receipt = self.runtime.notilt.verify_receipt(
            chain_id=chain_id,
            vault=vault,
            agent=agent,
            receipt_kind=receipt_kind,
            transaction_hash=request.transaction_hash,
            min_confirmations=self.runtime.settings.notilt_min_confirmations[chain_id],
            asset=command.asset if receipt_kind in {"DEPOSIT", "RELEASE_REQUEST"} else None,
            amount=(
                str(command.min_received)
                if receipt_kind in {"DEPOSIT", "RELEASE_REQUEST"}
                else None
            ),
            request_id=(
                str(detail["protocol_request_id"])
                if receipt_kind in {"RELEASE_EXECUTION", "RELEASE_CANCELLATION"}
                else None
            ),
        )
        transport_state = self.runtime.service().record_notilt_receipt(
            capital_transfer_id,
            actor_id,
            receipt,
            now=now,
        )
        vault_sync: dict[str, object] = {"attempted": False}
        if receipt_kind in {"DEPOSIT", "RELEASE_EXECUTION"}:
            vault_sync = {"attempted": True}
            try:
                fact_count, _ = self.runtime.sync_configured_notilt_vault(
                    chain_id,
                    actor_id,
                    now=now,
                )
                vault_sync.update({"status": "SYNCED", "facts_recorded": fact_count})
            except DomainRejected as exc:
                vault_sync.update({"status": "FAILED", "error_code": exc.code})
        updated = self.runtime.queries().capital_transfer_detail(actor_id, capital_transfer_id)
        self.runtime.notify_capital(
            object_id=capital_transfer_id,
            object_type="CapitalTransfer",
            event_type=f"NOTILT_{receipt_kind}_CONFIRMED",
            actor_id=actor_id,
            team_id=UUID(str(updated["team_id"])),
            environment=str(updated["environment"]),
            account_id=str(updated["account_id"]),
            venue=str(updated["venue"]),
            object_version=int(updated["version"]),
            summary=f"NoTilt 回执已验证；协议状态为 {transport_state}。",  # noqa: RUF001
        )
        return {
            "transport": "NOTILT_VERIFIED_RECEIPT",
            "idempotent": False,
            "receipt": {
                "kind": receipt.receipt_kind,
                "chain_id": receipt.chain_id,
                "transaction_hash": receipt.transaction_hash,
                "block_number": receipt.block_number,
                "block_timestamp": receipt.block_timestamp.isoformat(),
                "confirmations": receipt.confirmations,
            },
            "vault_sync": vault_sync,
            "detail": updated,
        }

    def capital_transfer_detail(
        self,
        capital_transfer_id: UUID,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        return self.runtime.queries().capital_transfer_detail(actor_id, capital_transfer_id)

    def observe_mock_capital_transfer(
        self,
        capital_transfer_id: UUID,
        request: CapitalTransferObservationInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        self.runtime.service().record_capital_observation(
            capital_transfer_id,
            actor_id,
            CapitalTransferStatus(request.status),
            transaction_reference=request.transaction_reference,
            fee_amount=request.fee_amount,
            net_received=request.net_received,
            now=self.runtime.clock(),
        )
        detail = self.runtime.queries().capital_transfer_detail(actor_id, capital_transfer_id)
        self.runtime.notify_capital(
            object_id=capital_transfer_id,
            object_type="CapitalTransfer",
            event_type=str(detail["status"]),
            actor_id=actor_id,
            team_id=UUID(str(detail["team_id"])),
            environment=str(detail["environment"]),
            account_id=str(detail["account_id"]),
            venue=str(detail["venue"]),
            object_version=int(detail["version"]),
            summary=f"资金划转状态已变更为 {detail['status']}。",
        )
        return {"transport": "MOCK_ONLY", "detail": detail}

    def reconcile_capital_transfer(
        self,
        capital_transfer_id: UUID,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        result = self.runtime.service().reconcile_capital_transfer(
            capital_transfer_id, actor_id, now=self.runtime.clock()
        )
        detail = self.runtime.queries().capital_transfer_detail(actor_id, capital_transfer_id)
        self.runtime.notify_capital(
            object_id=capital_transfer_id,
            object_type="CapitalTransfer",
            event_type=f"RECONCILIATION_{result}",
            actor_id=actor_id,
            team_id=UUID(str(detail["team_id"])),
            environment=str(detail["environment"]),
            account_id=str(detail["account_id"]),
            venue=str(detail["venue"]),
            object_version=int(detail["version"]),
            summary=f"资金对账结果为 {result}。",
        )
        return {"reconciliation_status": result, "detail": detail}
