from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from trading_control_plane.adapters.capital import CapitalOperation
from trading_control_plane.adapters.hyperliquid_capital import HYPERLIQUID_BRIDGE2_ADDRESS
from trading_control_plane.capital_application import CapitalApplicationRuntime
from trading_control_plane.domain import DirectCapitalPath, DomainRejected
from trading_control_plane.notilt import NoTiltUnsignedTransaction


class DirectCapitalUnsignedPlanInput(Protocol):
    expected_version: int
    final_confirmed: Literal[True]
    idempotency_key: str


class DirectCapitalBinanceSubmissionInput(Protocol):
    expected_version: int
    idempotency_key: str


class DirectCapitalWalletSubmissionInput(Protocol):
    expected_version: int
    stage: Literal[
        "HYPERLIQUID_DEPOSIT",
        "HYPERLIQUID_WITHDRAWAL",
        "HYPERLIQUID_CLASS_TRANSFER",
        "TREASURY_DEPOSIT",
        "TREASURY_WITHDRAWAL",
        "NOTILT_RELEASE_EXECUTION",
        "NOTILT_DESTINATION_TRANSFER",
    ]
    outcome: Literal["SUBMITTED", "CANCELLED"]
    transaction_hash: str | None
    action_hash: str | None
    nonce: int | None
    final_confirmed: Literal[True]
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CapitalDirectUseCases:
    runtime: CapitalApplicationRuntime

    def prepare_direct_binance_preview(
        self,
        operation_id: UUID,
        request: DirectCapitalUnsignedPlanInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        now = self.runtime.clock()
        context = self.runtime.direct_operation_context(
            operation_id, actor_id, request.expected_version, now
        )
        path = DirectCapitalPath(str(context["path"]))
        if path not in {
            DirectCapitalPath.VAULT_TO_BINANCE,
            DirectCapitalPath.BINANCE_TO_VAULT,
        }:
            raise DomainRejected(
                "BINANCE_CAPITAL_PATH_INVALID", "this operation does not contain a Binance leg"
            )
        direct_settings, _ = self.runtime.direct_settings(actor_id)
        capital_account_id = None if context["account_id"] is None else str(context["account_id"])
        if path is DirectCapitalPath.VAULT_TO_BINANCE:
            destination = direct_settings.capital_direct_binance_deposit_address
            source = context["source_reference"]
            if destination is None or source is None:
                raise DomainRejected(
                    "BINANCE_CAPITAL_SCOPE_MISSING",
                    "frozen treasury source and Binance deposit address are required",
                )
            artifact = self.runtime.execute_mapping(
                actor_id=actor_id,
                account_id=capital_account_id,
                venue="BINANCE",
                operation=CapitalOperation.BINANCE_PREPARE_DEPOSIT,
                parameters={
                    "expected_address": destination,
                    "amount": Decimal(str(context["min_received"])),
                    "source_address": str(source),
                    "now": now,
                },
            )
        else:
            destination = direct_settings.capital_direct_binance_withdrawal_address
            max_fee = context["max_fee"]
            if destination is None or max_fee is None:
                raise DomainRejected(
                    "BINANCE_CAPITAL_SCOPE_MISSING",
                    "allowlisted treasury destination and fee limit are required",
                )
            if str(context["destination_reference"]).lower() != destination.lower():
                raise DomainRejected(
                    "BINANCE_CAPITAL_DESTINATION_MISMATCH",
                    "frozen treasury destination does not match Binance configuration",
                )
            artifact = self.runtime.execute_mapping(
                actor_id=actor_id,
                account_id=capital_account_id,
                venue="BINANCE",
                operation=CapitalOperation.BINANCE_PREPARE_WITHDRAWAL,
                parameters={
                    "destination": destination,
                    "amount": Decimal(str(context["amount"])),
                    "max_fee": Decimal(str(max_fee)),
                    "operation_id": str(operation_id),
                    "now": now,
                },
            )
        version = self.runtime.service().record_direct_capital_binance_preview(
            operation_id,
            actor_id,
            expected_version=request.expected_version,
            artifact=artifact,
            idempotency_key=request.idempotency_key,
            now=now,
        )
        return {
            "operation_id": str(operation_id),
            "version": version,
            "artifact": artifact,
            "credentials_configured": True,
            "submission_enabled": direct_settings.binance_capital_withdraw_enabled,
            "signing_material_returned": False,
            "transfer_submitted": False,
            "data": self.runtime.direct_action_snapshot(actor_id),
        }

    def submit_direct_binance_withdrawal(
        self,
        operation_id: UUID,
        request: DirectCapitalBinanceSubmissionInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        now = self.runtime.clock()
        context = self.runtime.direct_operation_context(
            operation_id, actor_id, request.expected_version, now
        )
        if context["path"] != DirectCapitalPath.BINANCE_TO_VAULT.value:
            raise DomainRejected(
                "BINANCE_CAPITAL_DIRECTION_INVALID",
                "only Binance withdrawals use this endpoint",
            )
        direct_settings, _ = self.runtime.direct_settings(actor_id)
        if not direct_settings.binance_capital_withdraw_enabled:
            raise DomainRejected(
                "BINANCE_CAPITAL_SUBMISSION_DISABLED",
                "Binance capital withdrawal transport is explicitly disabled",
            )
        if self.runtime.direct_action_snapshot(actor_id)["real_transfer_gate"] != "ENABLED":
            raise DomainRejected(
                "CAPITAL_TRANSFER_GATE_DISABLED",
                "durable CAPITAL_TRANSFER gate is disabled",
            )
        preflight = next(
            (
                stage.get("artifact")
                for stage in reversed(context["stages"])
                if stage.get("code") == "BINANCE_RESTRICTED_WITHDRAWAL_PREFLIGHT_READY"
            ),
            None,
        )
        if not isinstance(preflight, dict):
            raise DomainRejected(
                "BINANCE_CAPITAL_PREFLIGHT_REQUIRED", "current live preflight is required"
            )
        submission = self.runtime.execute_mapping(
            actor_id=actor_id,
            account_id=(None if context["account_id"] is None else str(context["account_id"])),
            venue="BINANCE",
            operation=CapitalOperation.BINANCE_SUBMIT_WITHDRAWAL,
            parameters={
                "artifact": preflight,
                "now": now,
                "operation_id": str(operation_id),
            },
        )
        version = self.runtime.service().record_direct_capital_binance_submission(
            operation_id,
            actor_id,
            expected_version=request.expected_version,
            submission=submission,
            idempotency_key=request.idempotency_key,
            now=now,
        )
        return {
            "operation_id": str(operation_id),
            "version": version,
            "submission": submission,
            "credentials_returned": False,
            "data": self.runtime.direct_action_snapshot(actor_id),
        }

    def prepare_direct_notilt_unsigned_preview(
        self,
        operation_id: UUID,
        request: DirectCapitalUnsignedPlanInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        now = self.runtime.clock()
        context = self.runtime.direct_operation_context(
            operation_id,
            actor_id,
            request.expected_version,
            now,
            conflict_message="direct capital operation changed; refresh before SDK preflight",
        )
        path = DirectCapitalPath(str(context["path"]))
        if path is DirectCapitalPath.BINANCE_TO_VAULT:
            raise DomainRejected(
                "BINANCE_DIRECT_TREASURY_WITHDRAWAL_REQUIRED",
                "Binance return uses the restricted withdrawal API directly to the selected "
                "NoTilt Vault; no second wallet deposit may be built",
            )
        if context["treasury_provider"] != "NOTILT_VAULT":
            raise DomainRejected(
                "NOTILT_PLAN_SCOPE_MISMATCH",
                "operation selected Safe Spending Limits instead of NoTilt Vault",
            )
        chain_id = self.runtime.notilt_chain_id_for_network(str(context["network"]))
        agent, vault = self.runtime.configured_notilt_scope(chain_id)
        direct_vault = (
            context["source_reference"]
            if path
            in {
                DirectCapitalPath.VAULT_TO_BINANCE,
                DirectCapitalPath.VAULT_TO_HYPERLIQUID,
            }
            else context["destination_reference"]
        )
        if direct_vault is None or direct_vault.lower() != vault.lower():
            raise DomainRejected(
                "NOTILT_VAULT_SCOPE_MISMATCH",
                "direct capital path and official NoTilt scope do not match",
            )
        amount = str(context["min_received"])
        transactions: tuple[NoTiltUnsignedTransaction, ...]
        if path in {
            DirectCapitalPath.VAULT_TO_BINANCE,
            DirectCapitalPath.VAULT_TO_HYPERLIQUID,
        }:
            if (
                path is DirectCapitalPath.VAULT_TO_HYPERLIQUID
                and str(context["destination_reference"]).lower() != agent.lower()
            ):
                raise DomainRejected(
                    "NOTILT_AGENT_DESTINATION_MISMATCH",
                    "NoTilt agent must equal the authorized Hyperliquid funding wallet",
                )
            max_fact_age_seconds = self.runtime.max_fact_age_seconds(actor_id)
            self.runtime.verify_live_notilt_release_budget(
                chain_id=chain_id,
                vault=vault,
                agent=agent,
                asset=str(context["asset"]),
                amount=Decimal(amount),
                max_fact_age_seconds=max_fact_age_seconds,
                now=now,
            )
            transactions = (
                self.runtime.notilt.prepare_release_request(
                    chain_id=chain_id,
                    vault=vault,
                    agent=agent,
                    asset=str(context["asset"]),
                    amount=amount,
                ),
            )
            preview_kind = "AGENT_RELEASE_REQUEST"
        else:
            depositor = context["source_reference"]
            if depositor is None:
                raise DomainRejected(
                    "CAPITAL_OWNED_ARBITRUM_ADDRESS_MISSING",
                    "NoTilt deposit preview requires the authorized owned wallet",
                )
            vault_snapshot = self.runtime.notilt.read_vault(chain_id, vault, depositor)
            asset_budget = next(
                (
                    item
                    for item in vault_snapshot.budgets
                    if item.asset == str(context["asset"]).upper()
                ),
                None,
            )
            if asset_budget is None or not asset_budget.is_official_vault:
                raise DomainRejected(
                    "NOTILT_VAULT_UNTRUSTED",
                    "NoTilt deposit requires a live official Vault fact",
                )
            if asset_budget.panic_locked:
                raise DomainRejected(
                    "NOTILT_PANIC_LOCKED",
                    "NoTilt Vault is panic locked",
                )
            transactions = self.runtime.notilt.prepare_deposit(
                chain_id=chain_id,
                vault=vault,
                agent=depositor,
                asset=str(context["asset"]),
                amount=amount,
            )
            preview_kind = "SDK_DEPOSIT_SEQUENCE"
        version = self.runtime.service().record_direct_capital_unsigned_preview(
            operation_id,
            actor_id,
            expected_version=request.expected_version,
            final_confirmed=request.final_confirmed,
            transactions=transactions,
            wallet_address=(
                agent
                if path
                in {
                    DirectCapitalPath.VAULT_TO_BINANCE,
                    DirectCapitalPath.VAULT_TO_HYPERLIQUID,
                }
                else str(context["source_reference"])
            ),
            idempotency_key=request.idempotency_key,
            now=now,
        )
        blockers = list(context["blockers"])
        return {
            "operation_id": str(operation_id),
            "version": version,
            "preview_kind": preview_kind,
            "transport": "NOTILT_OFFICIAL_SDK_UNSIGNED_PREVIEW",
            "signing": False,
            "broadcast": False,
            "execution_blocked": bool(blockers),
            "blockers": blockers,
            "transactions": [item.to_dict() for item in transactions],
            "wallet_address": agent
            if path
            in {
                DirectCapitalPath.VAULT_TO_BINANCE,
                DirectCapitalPath.VAULT_TO_HYPERLIQUID,
            }
            else context["source_reference"],
            "next_step": (
                "Resolve every blocker and re-read live source receipts before a human wallet "
                "may confirm any transaction."
            ),
            "data": self.runtime.direct_action_snapshot(actor_id),
        }

    def prepare_direct_notilt_release_execution(
        self,
        operation_id: UUID,
        request: DirectCapitalUnsignedPlanInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        now = self.runtime.clock()
        context = self.runtime.direct_operation_context(
            operation_id, actor_id, request.expected_version, now
        )
        if context["treasury_provider"] != "NOTILT_VAULT" or context["path"] not in {
            DirectCapitalPath.VAULT_TO_BINANCE.value,
            DirectCapitalPath.VAULT_TO_HYPERLIQUID.value,
        }:
            raise DomainRejected(
                "NOTILT_RELEASE_NOT_EXECUTABLE",
                "NoTilt release execution does not match this capital path",
            )
        request_receipt = next(
            (
                stage.get("evidence")
                for stage in reversed(context["stages"])
                if stage.get("code") == "NOTILT_RELEASE_REQUEST_RECEIPT_CONFIRMED"
            ),
            None,
        )
        if not isinstance(request_receipt, dict):
            raise DomainRejected(
                "NOTILT_RELEASE_NOT_EXECUTABLE",
                "verify the NoTilt release request receipt before execution",
            )
        try:
            execute_after = datetime.fromisoformat(str(request_receipt["execute_after"]))
            expires_at = datetime.fromisoformat(str(request_receipt["expires_at"]))
            request_id = str(request_receipt["request_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainRejected(
                "NOTILT_RECEIPT_INVALID", "stored NoTilt release receipt is invalid"
            ) from exc
        if now < execute_after:
            raise DomainRejected(
                "NOTILT_RELEASE_NOT_UNLOCKED",
                f"NoTilt release unlocks at {execute_after.isoformat()}",
            )
        if now >= expires_at:
            raise DomainRejected("NOTILT_RELEASE_EXPIRED", "NoTilt release request expired")
        chain_id = self.runtime.notilt_chain_id_for_network(str(context["network"]))
        agent, vault = self.runtime.configured_notilt_scope(chain_id)
        transaction = self.runtime.notilt.prepare_release_execution(
            chain_id=chain_id,
            vault=vault,
            agent=agent,
            request_id=request_id,
        )
        version = self.runtime.service().record_direct_capital_unsigned_preview(
            operation_id,
            actor_id,
            expected_version=request.expected_version,
            final_confirmed=request.final_confirmed,
            transactions=(transaction,),
            wallet_address=agent,
            idempotency_key=request.idempotency_key,
            now=now,
            preview_kind="RELEASE_EXECUTION",
        )
        return {
            "operation_id": str(operation_id),
            "version": version,
            "preview_kind": "AGENT_RELEASE_EXECUTION",
            "signing": False,
            "broadcast": False,
            "transactions": [transaction.to_dict()],
            "wallet_address": agent,
            "data": self.runtime.direct_action_snapshot(actor_id),
        }

    def prepare_direct_notilt_destination_transfer(
        self,
        operation_id: UUID,
        request: DirectCapitalUnsignedPlanInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        now = self.runtime.clock()
        context = self.runtime.direct_operation_context(
            operation_id, actor_id, request.expected_version, now
        )
        if (
            context["treasury_provider"] != "NOTILT_VAULT"
            or context["path"] != DirectCapitalPath.VAULT_TO_BINANCE.value
            or not any(
                stage.get("code") == "NOTILT_RELEASE_EXECUTION_RECEIPT_CONFIRMED"
                for stage in context["stages"]
            )
        ):
            raise DomainRejected(
                "NOTILT_RELEASE_NOT_EXECUTABLE",
                "verify the NoTilt release execution before transferring to Binance",
            )
        direct_settings, _ = self.runtime.direct_settings(actor_id)
        agent, _ = self.runtime.configured_notilt_scope(
            self.runtime.notilt_chain_id_for_network(str(context["network"]))
        )
        destination = direct_settings.capital_direct_binance_deposit_address
        if destination is None or str(context["destination_reference"]).lower() != (
            destination.lower()
        ):
            raise DomainRejected(
                "BINANCE_CAPITAL_DESTINATION_MISMATCH",
                "frozen Binance deposit address no longer matches configuration",
            )
        artifact = self.runtime.execute_mapping(
            actor_id=actor_id,
            account_id=(None if context["account_id"] is None else str(context["account_id"])),
            venue=self.runtime.venue_for_context(context),
            operation=CapitalOperation.HYPERLIQUID_PREPARE_ARBITRUM_TRANSFER,
            parameters={
                "sender": agent,
                "destination": destination,
                "amount": str(context["min_received"]),
                "now": now,
            },
        )
        version = self.runtime.service().record_direct_capital_notilt_destination_preview(
            operation_id,
            actor_id,
            expected_version=request.expected_version,
            artifact=artifact,
            idempotency_key=request.idempotency_key,
            now=now,
        )
        return {
            "operation_id": str(operation_id),
            "version": version,
            "artifact": artifact,
            "signing": False,
            "broadcast": False,
            "data": self.runtime.direct_action_snapshot(actor_id),
        }

    def prepare_direct_safe_spending_preview(
        self,
        operation_id: UUID,
        request: DirectCapitalUnsignedPlanInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        now = self.runtime.clock()
        context = self.runtime.direct_operation_context(
            operation_id, actor_id, request.expected_version, now
        )
        if context["treasury_provider"] != "SAFE_SPENDING_LIMIT":
            raise DomainRejected(
                "SAFE_PLAN_SCOPE_MISMATCH", "operation did not select Safe Spending Limits"
            )
        path = DirectCapitalPath(str(context["path"]))
        if path is DirectCapitalPath.BINANCE_TO_VAULT:
            raise DomainRejected(
                "BINANCE_DIRECT_TREASURY_WITHDRAWAL_REQUIRED",
                "Binance return uses the restricted withdrawal API directly to the selected "
                "Safe; no second wallet deposit may be built",
            )
        outbound = path in {
            DirectCapitalPath.VAULT_TO_BINANCE,
            DirectCapitalPath.VAULT_TO_HYPERLIQUID,
        }
        direct_settings, _ = self.runtime.direct_settings(actor_id)
        rpc_url = direct_settings.safe_spending_arbitrum_rpc_url
        safe = direct_settings.capital_direct_safe_address
        delegate = direct_settings.capital_direct_safe_delegate_address
        counterparty = context["destination_reference"] if outbound else context["source_reference"]
        required_scope = (
            (rpc_url, safe, delegate, counterparty) if outbound else (rpc_url, safe, counterparty)
        )
        if not direct_settings.safe_spending_enabled or not all(required_scope):
            raise DomainRejected(
                "SAFE_SPENDING_LIMIT_NOT_CONFIGURED",
                "Safe RPC, account, delegate and destination scope are required",
            )
        if outbound:
            artifact = self.runtime.safe_spending.prepare_spend(
                rpc_url=str(rpc_url),
                safe=str(safe),
                delegate=str(delegate),
                recipient=str(counterparty),
                amount=str(context["min_received"]),
            )
        else:
            artifact = self.runtime.safe_spending.prepare_deposit(
                rpc_url=str(rpc_url),
                safe=str(safe),
                sender=str(counterparty),
                amount=str(context["min_received"]),
            )
        version = self.runtime.service().record_direct_capital_safe_preview(
            operation_id,
            actor_id,
            expected_version=request.expected_version,
            final_confirmed=request.final_confirmed,
            signature_request=artifact,
            idempotency_key=request.idempotency_key,
            now=now,
        )
        updated_context = self.runtime.direct_operation_context(
            operation_id, actor_id, version, now
        )
        blockers = list(updated_context["blockers"])
        return {
            "operation_id": str(operation_id),
            "version": version,
            "preview_kind": artifact["kind"],
            "transport": (
                "SAFE_OFFICIAL_ALLOWANCE_MODULE_HUMAN_HANDOFF"
                if outbound
                else "SAFE_EXACT_USDC_TRANSFER_HUMAN_HANDOFF"
            ),
            "signing": False,
            "broadcast": False,
            "execution_blocked": bool(blockers),
            "blockers": blockers,
            "signature_request": artifact,
            "next_step": (
                "The connected wallet must review and submit this exact transaction; "
                "the control plane stores no private key or signature material."
            ),
            "data": self.runtime.direct_action_snapshot(actor_id),
        }

    def prepare_direct_hyperliquid_preview(
        self,
        operation_id: UUID,
        request: DirectCapitalUnsignedPlanInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        now = self.runtime.clock()
        context = self.runtime.direct_operation_context(
            operation_id, actor_id, request.expected_version, now
        )
        path = DirectCapitalPath(str(context["path"]))
        if path not in {
            DirectCapitalPath.VAULT_TO_HYPERLIQUID,
            DirectCapitalPath.HYPERLIQUID_TO_VAULT,
        }:
            raise DomainRejected(
                "HYPERLIQUID_CAPITAL_PATH_INVALID",
                "this capital operation does not contain a Hyperliquid leg",
            )
        direct_settings, _ = self.runtime.direct_settings(actor_id)
        direct_settings = self.runtime.hyperliquid_settings(
            actor_id=actor_id,
            account_id=(None if context["account_id"] is None else str(context["account_id"])),
            direct_settings=direct_settings,
        )
        bridge = direct_settings.capital_direct_hyperliquid_bridge_address
        owned = direct_settings.capital_direct_owned_arbitrum_address
        if bridge is None or owned is None:
            raise DomainRejected(
                "HYPERLIQUID_CAPITAL_SCOPE_MISSING",
                "official Bridge2 and the authorized Arbitrum wallet must be configured",
            )
        if bridge.lower() != HYPERLIQUID_BRIDGE2_ADDRESS:
            raise DomainRejected(
                "HYPERLIQUID_BRIDGE_UNTRUSTED",
                "configured bridge does not match the official Arbitrum Bridge2 deployment",
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
        if main_account is None:
            raise DomainRejected(
                "HYPERLIQUID_MAIN_ACCOUNT_MISSING",
                "a Hyperliquid main account or resolvable authorized API wallet is required",
            )
        if path is DirectCapitalPath.VAULT_TO_HYPERLIQUID:
            if not any(
                isinstance(stage, dict)
                and stage.get("code") == "TREASURY_WITHDRAWAL_RECEIPT_CONFIRMED"
                for stage in context["stages"]
            ):
                raise DomainRejected(
                    "TREASURY_SOURCE_RECEIPT_REQUIRED",
                    "confirm the exact Safe source transfer before preparing the bridge deposit",
                )
            frozen_wallet = str(context["destination_reference"] or "")
            if frozen_wallet.lower() != main_account.lower():
                raise DomainRejected(
                    "HYPERLIQUID_DEPOSIT_ACCOUNT_MISMATCH",
                    "the frozen Safe destination is not the selected Hyperliquid main account",
                )
            rpc_url = (
                direct_settings.capital_arbitrum_rpc_url
                or direct_settings.safe_spending_arbitrum_rpc_url
            )
            if rpc_url is None:
                raise DomainRejected(
                    "HYPERLIQUID_CAPITAL_SCOPE_MISSING",
                    "a trusted Arbitrum RPC is required before preparing the bridge deposit",
                )
            required = Decimal(str(context["min_received"]))
            balance = self.runtime.execute_decimal(
                actor_id=actor_id,
                account_id=capital_account_id,
                venue="HYPERLIQUID",
                operation=CapitalOperation.HYPERLIQUID_ARBITRUM_BALANCE,
                parameters={"rpc_url": rpc_url, "address": frozen_wallet},
            )
            if balance < required:
                raise DomainRejected(
                    "HYPERLIQUID_DEPOSIT_BALANCE_INSUFFICIENT",
                    "the frozen Hyperliquid main wallet no longer holds the confirmed "
                    "deposit amount",
                )
            artifact = self.runtime.execute_mapping(
                actor_id=actor_id,
                account_id=capital_account_id,
                venue="HYPERLIQUID",
                operation=CapitalOperation.HYPERLIQUID_PREPARE_DEPOSIT,
                parameters={
                    "base_url": direct_settings.hyperliquid_base_url,
                    "main_account": main_account,
                    "api_wallet_address": direct_settings.hyperliquid_api_wallet_address,
                    "owned_arbitrum_address": frozen_wallet,
                    "bridge_address": bridge,
                    "amount": str(context["min_received"]),
                    "now": now,
                },
            )
        else:
            artifact = self.runtime.execute_mapping(
                actor_id=actor_id,
                account_id=capital_account_id,
                venue="HYPERLIQUID",
                operation=CapitalOperation.HYPERLIQUID_PREPARE_WITHDRAWAL,
                parameters={
                    "base_url": direct_settings.hyperliquid_base_url,
                    "main_account": main_account,
                    "api_wallet_address": direct_settings.hyperliquid_api_wallet_address,
                    "destination": (
                        str(context["destination_reference"])
                        if context["treasury_provider"] == "SAFE_SPENDING_LIMIT"
                        else owned
                    ),
                    "amount": str(context["amount"]),
                    "max_fee": context["max_fee"],
                    "now": now,
                },
            )
        version = self.runtime.service().record_direct_capital_hyperliquid_preview(
            operation_id,
            actor_id,
            expected_version=request.expected_version,
            final_confirmed=request.final_confirmed,
            artifact=artifact,
            idempotency_key=request.idempotency_key,
            now=now,
        )
        return {
            "operation_id": str(operation_id),
            "version": version,
            "preview_kind": artifact["kind"],
            "transport": "HYPERLIQUID_OFFICIAL_PROTOCOL_HUMAN_WALLET_HANDOFF",
            "agent_wallet": artifact["agentWallet"],
            "automatic_fallback": True,
            "fallback_reason": artifact["fallbackReason"],
            "signing": False,
            "broadcast": False,
            "artifact": artifact,
            "next_step": (
                "The main account or valid multisig wallet must re-check chain, destination, "
                "amount, fee and method before signing. This service stores no signature "
                "material."
            ),
            "data": self.runtime.direct_action_snapshot(actor_id),
        }

    def record_direct_wallet_submission(
        self,
        operation_id: UUID,
        request: DirectCapitalWalletSubmissionInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        version = self.runtime.service().record_direct_capital_wallet_submission(
            operation_id,
            actor_id,
            expected_version=request.expected_version,
            stage=request.stage,
            outcome=request.outcome,
            transaction_hash=request.transaction_hash,
            action_hash=request.action_hash,
            nonce=request.nonce,
            final_confirmed=request.final_confirmed,
            idempotency_key=request.idempotency_key,
            now=self.runtime.clock(),
        )
        return {
            "operation_id": str(operation_id),
            "version": version,
            "outcome": request.outcome,
            "signing_material_stored": False,
            "data": self.runtime.direct_action_snapshot(actor_id),
        }
