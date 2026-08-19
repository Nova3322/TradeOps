from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from trading_control_plane.adapters.capital import CapitalOperation
from trading_control_plane.capital_application import CapitalApplicationRuntime
from trading_control_plane.domain import DirectCapitalPath, DomainRejected


class DirectCapitalBinanceReceiptInput(Protocol):
    expected_version: int
    stage: Literal["BINANCE_DEPOSIT", "BINANCE_WITHDRAWAL"]
    transaction_hash: str | None
    idempotency_key: str


class DirectCapitalHyperliquidReceiptInput(Protocol):
    expected_version: int
    stage: Literal[
        "HYPERLIQUID_DEPOSIT_ARBITRUM",
        "HYPERLIQUID_DEPOSIT_LEDGER",
        "HYPERLIQUID_WITHDRAWAL_LEDGER",
        "HYPERLIQUID_WITHDRAWAL_ARBITRUM",
        "HYPERLIQUID_CLASS_TRANSFER_LEDGER",
    ]
    transaction_hash: str | None
    action_hash: str | None
    nonce: int | None
    idempotency_key: str


class DirectCapitalTreasuryReceiptInput(Protocol):
    expected_version: int
    transaction_hash: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CapitalReceiptUseCases:
    runtime: CapitalApplicationRuntime

    def verify_direct_binance_receipt(
        self,
        operation_id: UUID,
        request: DirectCapitalBinanceReceiptInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        now = self.runtime.clock()
        context = self.runtime.direct_operation_context(
            operation_id,
            actor_id,
            request.expected_version,
            now,
            allow_expired=True,
        )
        direct_settings, _ = self.runtime.direct_settings(actor_id)
        capital_account_id = None if context["account_id"] is None else str(context["account_id"])
        poll_token = request.idempotency_key
        self.runtime.service().acquire_direct_capital_binance_receipt_poll(
            operation_id,
            actor_id,
            expected_version=request.expected_version,
            stage=request.stage,
            token=poll_token,
            now=now,
        )
        try:
            if request.stage == "BINANCE_DEPOSIT":
                if context["path"] != DirectCapitalPath.VAULT_TO_BINANCE.value:
                    raise DomainRejected(
                        "BINANCE_CAPITAL_RECEIPT_STAGE_INVALID",
                        "deposit receipt does not match path",
                    )
                assert request.transaction_hash is not None
                submission_code = (
                    "NOTILT_DESTINATION_TRANSFER_SUBMITTED_BY_HUMAN_WALLET"
                    if context["treasury_provider"] == "NOTILT_VAULT"
                    else "TREASURY_WITHDRAWAL_SUBMITTED_BY_HUMAN_WALLET"
                )
                submitted = next(
                    (
                        stage
                        for stage in reversed(context["stages"])
                        if stage.get("code") == submission_code
                    ),
                    None,
                )
                if (
                    submitted is None
                    or str(submitted.get("transaction_hash", "")).lower()
                    != request.transaction_hash.lower()
                ):
                    raise DomainRejected(
                        "BINANCE_CAPITAL_RECEIPT_REFERENCE_MISMATCH",
                        "Binance deposit receipt does not match the wallet-submitted transfer",
                    )
                destination = direct_settings.capital_direct_binance_deposit_address
                if destination is None:
                    raise DomainRejected(
                        "BINANCE_CAPITAL_SCOPE_MISSING", "Binance deposit address is missing"
                    )
                deposit_evidence = self.runtime.execute_mapping(
                    actor_id=actor_id,
                    account_id=capital_account_id,
                    venue="BINANCE",
                    operation=CapitalOperation.BINANCE_VERIFY_DEPOSIT,
                    parameters={
                        "transaction_hash": request.transaction_hash,
                        "destination": destination,
                        "amount": Decimal(str(context["min_received"])),
                    },
                )
                preflight = next(
                    (
                        stage.get("artifact")
                        for stage in reversed(context["stages"])
                        if stage.get("code") == "BINANCE_DEPOSIT_PREFLIGHT_READY"
                    ),
                    None,
                )
                if not isinstance(preflight, dict):
                    raise DomainRejected(
                        "BINANCE_CAPITAL_PREFLIGHT_REQUIRED",
                        "the frozen Binance deposit preflight is missing",
                    )
                try:
                    prepared_at = datetime.fromisoformat(str(preflight["preparedAt"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise DomainRejected(
                        "BINANCE_CAPITAL_PREFLIGHT_INVALID",
                        "the frozen Binance deposit preflight timestamp is invalid",
                    ) from exc
                internal_transfer = self.runtime.execute_mapping(
                    actor_id=actor_id,
                    account_id=capital_account_id,
                    venue="BINANCE",
                    operation=CapitalOperation.BINANCE_COMPLETE_DEPOSIT,
                    parameters={
                        "amount": Decimal(str(context["min_received"])),
                        "prepared_at": prepared_at,
                        "now": now,
                        "operation_id": str(operation_id),
                    },
                )
                evidence = {
                    "deposit": deposit_evidence,
                    "internalTransfer": internal_transfer,
                }
            else:
                if context["path"] != DirectCapitalPath.BINANCE_TO_VAULT.value:
                    raise DomainRejected(
                        "BINANCE_CAPITAL_RECEIPT_STAGE_INVALID",
                        "withdrawal receipt does not match path",
                    )
                destination = direct_settings.capital_direct_binance_withdrawal_address
                rpc_url = (
                    direct_settings.capital_arbitrum_rpc_url
                    or direct_settings.safe_spending_arbitrum_rpc_url
                )
                if destination is None or rpc_url is None:
                    raise DomainRejected(
                        "BINANCE_CAPITAL_SCOPE_MISSING",
                        "frozen treasury destination and trusted Arbitrum RPC are required",
                    )
                withdrawal = self.runtime.execute_mapping(
                    actor_id=actor_id,
                    account_id=capital_account_id,
                    venue="BINANCE",
                    operation=CapitalOperation.BINANCE_VERIFY_WITHDRAWAL,
                    parameters={
                        "order_id": str(operation_id),
                        "destination": destination,
                        "amount": Decimal(str(context["amount"])),
                    },
                )
                try:
                    received_amount = Decimal(str(withdrawal["amount"]))
                    fee = Decimal(str(withdrawal["fee"]))
                    min_received = Decimal(str(context["min_received"]))
                    max_fee = Decimal(str(context["max_fee"]))
                except (KeyError, ArithmeticError, ValueError) as exc:
                    raise DomainRejected(
                        "BINANCE_CAPITAL_RECEIPT_MISMATCH",
                        "Binance withdrawal receipt amounts are invalid",
                    ) from exc
                if (
                    received_amount < min_received
                    or fee > max_fee
                    or received_amount + fee != Decimal(str(context["amount"]))
                ):
                    raise DomainRejected(
                        "BINANCE_CAPITAL_RECEIPT_MISMATCH",
                        "Binance withdrawal receipt is outside the frozen amount limits",
                    )
                transaction_hash = str(withdrawal["transactionHash"])
                chain = self.runtime.execute_mapping(
                    actor_id=actor_id,
                    account_id=capital_account_id,
                    venue="BINANCE",
                    operation=CapitalOperation.HYPERLIQUID_VERIFY_ARBITRUM_CREDIT_ANY,
                    parameters={
                        "rpc_url": rpc_url,
                        "transaction_hash": transaction_hash,
                        "recipient": destination,
                        "amount": str(received_amount),
                        "min_confirmations": (direct_settings.notilt_arbitrum_min_confirmations),
                    },
                )
                evidence = {"binance": withdrawal, "arbitrum": chain}
            version = self.runtime.service().record_direct_capital_binance_receipt(
                operation_id,
                actor_id,
                expected_version=request.expected_version,
                stage=request.stage,
                evidence=evidence,
                idempotency_key=request.idempotency_key,
                now=now,
            )
            return {
                "operation_id": str(operation_id),
                "version": version,
                "receipt": evidence,
                "settlement": "CONFIRMED",
                "data": self.runtime.direct_action_snapshot(actor_id),
            }
        finally:
            self.runtime.service().release_direct_capital_binance_receipt_poll(
                operation_id,
                stage=request.stage,
                token=poll_token,
                now=self.runtime.clock(),
            )

    def verify_direct_notilt_release_receipt(
        self,
        operation_id: UUID,
        request: DirectCapitalTreasuryReceiptInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        now = self.runtime.clock()
        context = self.runtime.direct_operation_context(
            operation_id,
            actor_id,
            request.expected_version,
            now,
            allow_expired=True,
        )
        execution_submission = next(
            (
                stage
                for stage in reversed(context["stages"])
                if stage.get("code") == "NOTILT_RELEASE_EXECUTION_SUBMITTED_BY_HUMAN_WALLET"
            ),
            None,
        )
        receipt_kind = "RELEASE_EXECUTION" if execution_submission else "RELEASE_REQUEST"
        submission = execution_submission or next(
            (
                stage
                for stage in reversed(context["stages"])
                if stage.get("code") == "TREASURY_WITHDRAWAL_SUBMITTED_BY_HUMAN_WALLET"
            ),
            None,
        )
        if submission is None or str(submission.get("transaction_hash", "")).lower() != (
            request.transaction_hash.lower()
        ):
            raise DomainRejected(
                "NOTILT_RECEIPT_REFERENCE_MISMATCH",
                "receipt does not match the latest NoTilt wallet transaction",
            )
        chain_id = self.runtime.notilt_chain_id_for_network(str(context["network"]))
        agent, vault = self.runtime.configured_notilt_scope(chain_id)
        request_receipt = next(
            (
                stage.get("evidence")
                for stage in reversed(context["stages"])
                if stage.get("code") == "NOTILT_RELEASE_REQUEST_RECEIPT_CONFIRMED"
            ),
            None,
        )
        request_id = (
            str(request_receipt.get("request_id")) if isinstance(request_receipt, dict) else None
        )
        receipt = self.runtime.notilt.verify_receipt(
            chain_id=chain_id,
            vault=vault,
            agent=agent,
            receipt_kind=receipt_kind,
            transaction_hash=request.transaction_hash,
            min_confirmations=self.runtime.direct_settings(actor_id)[0].notilt_min_confirmations[
                chain_id
            ],
            asset=str(context["asset"]) if receipt_kind == "RELEASE_REQUEST" else None,
            amount=(str(context["min_received"]) if receipt_kind == "RELEASE_REQUEST" else None),
            request_id=request_id if receipt_kind == "RELEASE_EXECUTION" else None,
        )
        evidence = {
            "kind": f"NOTILT_{receipt_kind}_RECEIPT",
            "transaction_hash": receipt.transaction_hash,
            "request_id": receipt.request_id or request_id,
            "block_number": receipt.block_number,
            "confirmations": receipt.confirmations,
            "execute_after": (
                None if receipt.execute_after is None else receipt.execute_after.isoformat()
            ),
            "expires_at": (None if receipt.expires_at is None else receipt.expires_at.isoformat()),
            "net_amount": (None if receipt.net_amount is None else str(receipt.net_amount)),
            "fee": None if receipt.fee is None else str(receipt.fee),
        }
        version = self.runtime.service().record_direct_capital_notilt_receipt(
            operation_id,
            actor_id,
            expected_version=request.expected_version,
            receipt_kind=receipt_kind,
            evidence=evidence,
            idempotency_key=request.idempotency_key,
            now=now,
        )
        return {
            "operation_id": str(operation_id),
            "version": version,
            "receipt_kind": receipt_kind,
            "receipt": evidence,
            "data": self.runtime.direct_action_snapshot(actor_id),
        }

    def verify_direct_treasury_withdrawal_receipt(
        self,
        operation_id: UUID,
        request: DirectCapitalTreasuryReceiptInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        now = self.runtime.clock()
        context = self.runtime.direct_operation_context(
            operation_id,
            actor_id,
            request.expected_version,
            now,
            allow_expired=True,
        )
        if (
            context["path"]
            not in {
                DirectCapitalPath.VAULT_TO_HYPERLIQUID.value,
                DirectCapitalPath.VAULT_TO_BINANCE.value,
            }
            or context["treasury_provider"] != "SAFE_SPENDING_LIMIT"
        ):
            raise DomainRejected(
                "TREASURY_RECEIPT_STAGE_INVALID",
                "source receipt is only valid for outbound Safe capital paths",
            )
        direct_settings, _ = self.runtime.direct_settings(actor_id)
        safe = direct_settings.capital_direct_safe_address
        destination = context["destination_reference"]
        rpc_url = (
            direct_settings.capital_arbitrum_rpc_url
            or direct_settings.safe_spending_arbitrum_rpc_url
        )
        if safe is None or destination is None or rpc_url is None:
            raise DomainRejected(
                "SAFE_SPENDING_LIMIT_NOT_CONFIGURED",
                "Safe, frozen destination and trusted Arbitrum RPC are required",
            )
        evidence = self.runtime.execute_mapping(
            actor_id=actor_id,
            account_id=(None if context["account_id"] is None else str(context["account_id"])),
            venue=self.runtime.venue_for_context(context),
            operation=CapitalOperation.HYPERLIQUID_VERIFY_ARBITRUM_CREDIT,
            parameters={
                "rpc_url": rpc_url,
                "transaction_hash": request.transaction_hash,
                "sender": safe,
                "recipient": str(destination),
                "amount": str(context["min_received"]),
                "min_confirmations": direct_settings.notilt_arbitrum_min_confirmations,
            },
        )
        version = self.runtime.service().record_direct_capital_treasury_withdrawal_receipt(
            operation_id,
            actor_id,
            expected_version=request.expected_version,
            evidence=evidence,
            idempotency_key=request.idempotency_key,
            now=now,
        )
        return {
            "operation_id": str(operation_id),
            "version": version,
            "receipt": evidence,
            "data": self.runtime.direct_action_snapshot(actor_id),
        }

    def verify_direct_hyperliquid_receipt(
        self,
        operation_id: UUID,
        request: DirectCapitalHyperliquidReceiptInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        now = self.runtime.clock()
        context = self.runtime.direct_operation_context(
            operation_id,
            actor_id,
            request.expected_version,
            now,
            allow_expired=True,
        )
        direct_settings, _ = self.runtime.direct_settings(actor_id)
        direct_settings = self.runtime.hyperliquid_settings(
            actor_id=actor_id,
            account_id=(None if context["account_id"] is None else str(context["account_id"])),
            direct_settings=direct_settings,
        )
        capital_account_id = None if context["account_id"] is None else str(context["account_id"])
        main_account = self.runtime.execute_string(
            actor_id=actor_id,
            account_id=capital_account_id,
            venue="HYPERLIQUID",
            operation=CapitalOperation.HYPERLIQUID_RESOLVE_MAIN,
            parameters={
                "base_url": direct_settings.hyperliquid_base_url,
                "account_address": direct_settings.hyperliquid_account_address,
                "api_wallet_address": direct_settings.hyperliquid_api_wallet_address,
            },
        )
        owned = direct_settings.capital_direct_owned_arbitrum_address
        bridge = direct_settings.capital_direct_hyperliquid_bridge_address
        if main_account is None or owned is None or bridge is None:
            raise DomainRejected(
                "HYPERLIQUID_CAPITAL_SCOPE_MISSING",
                "receipt verification requires the frozen main account, owned wallet and Bridge2",
            )
        artifact = next(
            (
                item
                for stage in reversed(context["stages"])
                if isinstance((item := stage.get("artifact")), dict)
                and str(item.get("kind", "")).startswith("HYPERLIQUID_")
            ),
            None,
        )
        if artifact is None:
            raise DomainRejected(
                "HYPERLIQUID_CAPITAL_PREFLIGHT_REQUIRED",
                "prepare a current unsigned Hyperliquid wallet request before verifying receipts",
            )
        try:
            prepared_at = datetime.fromisoformat(str(artifact["preparedAt"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainRejected(
                "HYPERLIQUID_CAPITAL_PLAN_INVALID", "stored Hyperliquid preflight is invalid"
            ) from exc
        expected_submission_code = (
            "HYPERLIQUID_DEPOSIT_SUBMITTED_BY_HUMAN_WALLET"
            if request.stage.startswith("HYPERLIQUID_DEPOSIT")
            else "HYPERLIQUID_CLASS_TRANSFER_SUBMITTED_BY_HUMAN_WALLET"
            if request.stage == "HYPERLIQUID_CLASS_TRANSFER_LEDGER"
            else "HYPERLIQUID_WITHDRAWAL_SUBMITTED_BY_HUMAN_WALLET"
        )
        submission = next(
            (
                stage
                for stage in reversed(context["stages"])
                if isinstance(stage, dict) and stage.get("code") == expected_submission_code
            ),
            None,
        )
        if submission is None:
            raise DomainRejected(
                "HYPERLIQUID_WALLET_SUBMISSION_REQUIRED",
                "record the human wallet submission before verifying public receipts",
            )
        if request.stage.startswith("HYPERLIQUID_DEPOSIT"):
            submitted_hash = submission.get("transaction_hash")
            supplied_hash = request.transaction_hash or request.action_hash
            if submitted_hash is None or str(submitted_hash).lower() != str(supplied_hash).lower():
                raise DomainRejected(
                    "HYPERLIQUID_RECEIPT_REFERENCE_MISMATCH",
                    "deposit receipt reference does not match the recorded wallet submission",
                )
        elif request.stage in {
            "HYPERLIQUID_WITHDRAWAL_LEDGER",
            "HYPERLIQUID_CLASS_TRANSFER_LEDGER",
        } and (
            str(submission.get("action_hash", "")).lower() != str(request.action_hash).lower()
            or submission.get("nonce") != request.nonce
        ):
            raise DomainRejected(
                "HYPERLIQUID_RECEIPT_REFERENCE_MISMATCH",
                "withdrawal ledger evidence does not match the recorded signed action",
            )
        if request.stage.endswith("LEDGER"):
            receipt_amount = artifact["amount"]
            evidence = self.runtime.execute_mapping(
                actor_id=actor_id,
                account_id=capital_account_id,
                venue="HYPERLIQUID",
                operation=CapitalOperation.HYPERLIQUID_VERIFY_LEDGER,
                parameters={
                    "base_url": direct_settings.hyperliquid_base_url,
                    "main_account": main_account,
                    "receipt_kind": (
                        "DEPOSIT"
                        if "DEPOSIT" in request.stage
                        else "CLASS_TRANSFER"
                        if "CLASS_TRANSFER" in request.stage
                        else "CCTP_WITHDRAWAL"
                        if artifact.get("kind") == "HYPERLIQUID_CCTP_WITHDRAWAL_TYPED_REQUEST"
                        else "WITHDRAWAL"
                    ),
                    "amount": str(receipt_amount),
                    "prepared_at": prepared_at,
                    "nonce": request.nonce,
                    "action_hash": request.action_hash,
                    "now": now,
                },
            )
        else:
            rpc_url = (
                direct_settings.capital_arbitrum_rpc_url
                or direct_settings.safe_spending_arbitrum_rpc_url
            )
            if rpc_url is None:
                raise DomainRejected(
                    "ARBITRUM_RPC_NOT_CONFIGURED",
                    "a trusted Arbitrum RPC is required for public receipt verification",
                )
            if request.stage == "HYPERLIQUID_DEPOSIT_ARBITRUM":
                evidence = self.runtime.execute_mapping(
                    actor_id=actor_id,
                    account_id=capital_account_id,
                    venue="HYPERLIQUID",
                    operation=CapitalOperation.HYPERLIQUID_VERIFY_ARBITRUM_TRANSFER,
                    parameters={
                        "rpc_url": rpc_url,
                        "transaction_hash": str(request.transaction_hash),
                        "sender": str(artifact["from"]),
                        "recipient": bridge,
                        "amount": str(artifact["amount"]),
                        "min_confirmations": (direct_settings.notilt_arbitrum_min_confirmations),
                    },
                )
            elif artifact.get("kind") == "HYPERLIQUID_CCTP_WITHDRAWAL_TYPED_REQUEST":
                evidence = self.runtime.execute_mapping(
                    actor_id=actor_id,
                    account_id=capital_account_id,
                    venue="HYPERLIQUID",
                    operation=CapitalOperation.HYPERLIQUID_FIND_CCTP_CREDIT,
                    parameters={
                        "rpc_url": rpc_url,
                        "main_account": main_account,
                        "nonce": int(artifact["nonce"]),
                        "recipient": str(artifact["destination"]),
                        "amount": str(artifact["minReceived"]),
                        "min_confirmations": (direct_settings.notilt_arbitrum_min_confirmations),
                    },
                )
            else:
                receipt_kwargs = {
                    "rpc_url": rpc_url,
                    "sender": bridge,
                    "recipient": str(artifact["destination"]),
                    # withdraw3 deducts the fixed protocol fee before the
                    # Bridge2 credit. Match the frozen net amount that can
                    # actually arrive at the Safe/Vault, not the gross
                    # signed amount.
                    "amount": str(artifact.get("minReceived", context["min_received"])),
                    "min_confirmations": direct_settings.notilt_arbitrum_min_confirmations,
                }
                operation = (
                    CapitalOperation.HYPERLIQUID_VERIFY_ARBITRUM_CREDIT
                    if request.transaction_hash is not None
                    else CapitalOperation.HYPERLIQUID_FIND_ARBITRUM_CREDIT
                )
                evidence = self.runtime.execute_mapping(
                    actor_id=actor_id,
                    account_id=capital_account_id,
                    venue="HYPERLIQUID",
                    operation=operation,
                    parameters={
                        **receipt_kwargs,
                        **(
                            {"transaction_hash": str(request.transaction_hash)}
                            if request.transaction_hash is not None
                            else {"prepared_at": prepared_at}
                        ),
                    },
                )
        version = self.runtime.service().record_direct_capital_hyperliquid_receipt(
            operation_id,
            actor_id,
            expected_version=request.expected_version,
            stage=request.stage,
            evidence=evidence,
            idempotency_key=request.idempotency_key,
            now=now,
        )
        updated_context = self.runtime.direct_operation_context(
            operation_id,
            actor_id,
            version,
            now,
            allow_expired=True,
        )
        return {
            "operation_id": str(operation_id),
            "version": version,
            "receipt": evidence,
            "settlement": (
                "CONFIRMED"
                if updated_context["status"] == "SETTLED"
                else "HYPERLIQUID_LEG_CONFIRMED_TREASURY_RECEIPT_STILL_REQUIRED"
            ),
            "data": self.runtime.direct_action_snapshot(actor_id),
        }

    def verify_direct_treasury_receipt(
        self,
        operation_id: UUID,
        request: DirectCapitalTreasuryReceiptInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        now = self.runtime.clock()
        context = self.runtime.direct_operation_context(
            operation_id,
            actor_id,
            request.expected_version,
            now,
            allow_expired=True,
        )
        if context["path"] != DirectCapitalPath.HYPERLIQUID_TO_VAULT.value:
            raise DomainRejected(
                "TREASURY_RECEIPT_STAGE_INVALID",
                "treasury receipt is only valid after a Hyperliquid withdrawal",
            )
        submitted = next(
            (
                stage
                for stage in reversed(context["stages"])
                if stage.get("code") == "TREASURY_DEPOSIT_SUBMITTED_BY_HUMAN_WALLET"
            ),
            None,
        )
        if submitted is None or str(submitted.get("transaction_hash", "")).lower() != (
            request.transaction_hash.lower()
        ):
            raise DomainRejected(
                "TREASURY_RECEIPT_REFERENCE_MISMATCH",
                "treasury receipt does not match a recorded human wallet submission",
            )
        direct_settings, _ = self.runtime.direct_settings(actor_id)
        owned = direct_settings.capital_direct_owned_arbitrum_address
        if owned is None:
            raise DomainRejected(
                "CAPITAL_OWNED_ARBITRUM_ADDRESS_MISSING",
                "treasury receipt verification requires the authorized owned wallet",
            )
        if context["treasury_provider"] == "NOTILT_VAULT":
            chain_id = self.runtime.notilt_chain_id_for_network(str(context["network"]))
            _, vault = self.runtime.configured_notilt_scope(chain_id)
            receipt = self.runtime.notilt.verify_receipt(
                chain_id=chain_id,
                vault=vault,
                agent=owned,
                receipt_kind="DEPOSIT",
                transaction_hash=request.transaction_hash,
                min_confirmations=direct_settings.notilt_min_confirmations[chain_id],
                asset=str(context["asset"]),
                amount=str(context["min_received"]),
            )
            evidence = {
                "kind": "NOTILT_DEPOSIT_RECEIPT",
                "transaction_hash": receipt.transaction_hash,
                "block_number": receipt.block_number,
                "confirmations": receipt.confirmations,
                "amount": (
                    None if receipt.credited_amount is None else str(receipt.credited_amount)
                ),
            }
        else:
            safe = direct_settings.capital_direct_safe_address
            rpc_url = (
                direct_settings.capital_arbitrum_rpc_url
                or direct_settings.safe_spending_arbitrum_rpc_url
            )
            if safe is None or rpc_url is None:
                raise DomainRejected(
                    "SAFE_SPENDING_LIMIT_NOT_CONFIGURED",
                    "Safe address and trusted Arbitrum RPC are required for receipt verification",
                )
            evidence = self.runtime.execute_mapping(
                actor_id=actor_id,
                account_id=(None if context["account_id"] is None else str(context["account_id"])),
                venue=self.runtime.venue_for_context(context),
                operation=CapitalOperation.HYPERLIQUID_VERIFY_ARBITRUM_TRANSFER,
                parameters={
                    "rpc_url": rpc_url,
                    "transaction_hash": request.transaction_hash,
                    "sender": owned,
                    "recipient": safe,
                    "amount": str(context["min_received"]),
                    "min_confirmations": (direct_settings.notilt_arbitrum_min_confirmations),
                },
            )
        version = self.runtime.service().record_direct_capital_treasury_receipt(
            operation_id,
            actor_id,
            expected_version=request.expected_version,
            evidence=evidence,
            idempotency_key=request.idempotency_key,
            now=now,
        )
        return {
            "operation_id": str(operation_id),
            "version": version,
            "receipt": evidence,
            "settlement": "SETTLED_IF_ALL_HYPERLIQUID_AND_TREASURY_RECEIPTS_CONFIRMED",
            "data": self.runtime.direct_action_snapshot(actor_id),
        }
