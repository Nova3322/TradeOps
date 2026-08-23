from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from trading_control_plane import domain, models, notilt, rejections
from trading_control_plane.service_component import ServiceComponent


class DirectCapitalPreviewService(ServiceComponent):
    def record_direct_capital_unsigned_preview(
        self,
        operation_id: UUID,
        actor_id: UUID,
        *,
        expected_version: int,
        final_confirmed: bool,
        transactions: tuple[notilt.NoTiltUnsignedTransaction, ...],
        wallet_address: str,
        idempotency_key: str,
        now: datetime,
        preview_kind: str = "INITIAL",
    ) -> int:
        if not final_confirmed:
            rejections.reject(
                "CAPITAL_FINAL_CONFIRMATION_REQUIRED",
                "unsigned SDK preview requires explicit final confirmation",
            )
        if not transactions:
            rejections.reject("NOTILT_PLAN_EMPTY", "NoTilt SDK returned no unsigned transactions")
        serialized = [item.to_dict() for item in transactions]
        operation = "capital.direct.notilt_unsigned_preview"
        with self.database.session_factory.begin() as session:
            item = session.get(models.DirectCapitalOperation, operation_id, with_for_update=True)
            if item is None:
                rejections.reject(
                    "CAPITAL_DIRECT_OPERATION_NOT_FOUND", "direct capital operation is missing"
                )
            self.transactions.require_role(
                session,
                actor_id,
                "capital.execute",
                item.account_id,
                item.venue,
                team_id=item.team_id,
            )
            payload = {
                "operation_id": str(operation_id),
                "expected_version": expected_version,
                "transactions": serialized,
                "wallet_address": wallet_address,
                "final_confirmed": True,
                "preview_kind": preview_kind,
            }
            digest, response = self.transactions.idempotency(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return int(response["version"])
            if item.version != expected_version:
                rejections.reject(
                    "VERSION_CONFLICT", "direct capital operation changed; refresh first"
                )
            if item.expires_at <= now:
                rejections.reject(
                    "CAPITAL_DIRECT_OPERATION_EXPIRED", "direct capital operation expired"
                )
            if item.path not in {
                domain.DirectCapitalPath.VAULT_TO_BINANCE.value,
                domain.DirectCapitalPath.VAULT_TO_HYPERLIQUID.value,
                domain.DirectCapitalPath.BINANCE_TO_VAULT.value,
                domain.DirectCapitalPath.HYPERLIQUID_TO_VAULT.value,
            }:
                rejections.reject(
                    "CAPITAL_DIRECT_PATH_INVALID", "direct capital path is unsupported"
                )
            outbound = item.path in {
                domain.DirectCapitalPath.VAULT_TO_BINANCE.value,
                domain.DirectCapitalPath.VAULT_TO_HYPERLIQUID.value,
            }
            if preview_kind == "RELEASE_EXECUTION":
                if not outbound or not any(
                    stage.get("code") == "NOTILT_RELEASE_REQUEST_RECEIPT_CONFIRMED"
                    for stage in item.stages
                ):
                    rejections.reject(
                        "NOTILT_RELEASE_NOT_EXECUTABLE",
                        "verified NoTilt release request is required before execution",
                    )
                allowed_functions = {"executeWhitelistRelease"}
                stage_code = "NOTILT_UNSIGNED_RELEASE_EXECUTION_PREVIEW"
            elif preview_kind == "INITIAL":
                allowed_functions = (
                    {"requestWhitelistRelease"}
                    if outbound
                    else {
                        "approve",
                        "deposit",
                    }
                )
                stage_code = (
                    "NOTILT_UNSIGNED_RELEASE_REQUEST_PREVIEW"
                    if outbound
                    else "NOTILT_UNSIGNED_DEPOSIT_PREVIEW"
                )
            else:
                rejections.reject("NOTILT_PLAN_INVALID", "NoTilt preview kind is unsupported")
            if any(
                transaction.function_name not in allowed_functions for transaction in transactions
            ):
                rejections.reject(
                    "NOTILT_PLAN_INVALID",
                    "NoTilt SDK preview contains a function outside the fixed path",
                )
            item.stages = [
                *item.stages,
                {
                    "code": stage_code,
                    "status": "READY_FOR_HUMAN_REVIEW",
                    "transactions": serialized,
                    "wallet_address": wallet_address,
                    "prepared_at": now.isoformat(),
                    "broadcast": False,
                },
            ]
            item.version += 1
            item.updated_at = now
            result = {"operation_id": str(operation_id), "version": item.version}
            self.transactions.save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions.audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_DIRECT_UNSIGNED_PREVIEW_PREPARED",
                object_type="DirectCapitalOperation",
                object_id=operation_id,
                reason=f"{stage_code}; signing=false; broadcast=false",
                correlation_id=item.correlation_id,
                object_version=item.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return item.version

    def record_direct_capital_notilt_destination_preview(
        self,
        operation_id: UUID,
        actor_id: UUID,
        *,
        expected_version: int,
        artifact: dict[str, Any],
        idempotency_key: str,
        now: datetime,
    ) -> int:
        if (
            artifact.get("kind") != "ARBITRUM_USDC_UNSIGNED_TRANSACTION"
            or artifact.get("signing") is not False
            or artifact.get("broadcast") is not False
        ):
            rejections.reject("NOTILT_PLAN_INVALID", "NoTilt destination transfer is invalid")
        operation = "capital.direct.notilt_destination_preview"
        with self.database.session_factory.begin() as session:
            item = session.get(models.DirectCapitalOperation, operation_id, with_for_update=True)
            if item is None:
                rejections.reject(
                    "CAPITAL_DIRECT_OPERATION_NOT_FOUND", "direct capital operation is missing"
                )
            self.transactions.require_role(
                session,
                actor_id,
                "capital.execute",
                item.account_id,
                item.venue,
                team_id=item.team_id,
            )
            payload = {
                "operation_id": str(operation_id),
                "expected_version": expected_version,
                "artifact": artifact,
            }
            digest, response = self.transactions.idempotency(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return int(response["version"])
            if item.version != expected_version:
                rejections.reject(
                    "VERSION_CONFLICT", "direct capital operation changed; refresh first"
                )
            if item.expires_at <= now:
                rejections.reject(
                    "CAPITAL_DIRECT_OPERATION_EXPIRED", "direct capital operation expired"
                )
            if (
                item.treasury_provider != "NOTILT_VAULT"
                or item.path != domain.DirectCapitalPath.VAULT_TO_BINANCE.value
                or not any(
                    stage.get("code") == "NOTILT_RELEASE_EXECUTION_RECEIPT_CONFIRMED"
                    for stage in item.stages
                )
            ):
                rejections.reject(
                    "NOTILT_RELEASE_NOT_EXECUTABLE",
                    "verified NoTilt release execution is required before destination transfer",
                )
            if str(artifact.get("recipient", "")).lower() != str(
                item.destination_reference or ""
            ).lower() or str(artifact.get("amount")) != str(item.min_received or item.amount):
                rejections.reject(
                    "NOTILT_PLAN_INVALID",
                    "NoTilt destination transfer changed the frozen destination or amount",
                )
            item.stages = [
                *item.stages,
                {
                    "code": "NOTILT_DESTINATION_TRANSFER_PREVIEW",
                    "status": "READY_FOR_HUMAN_REVIEW",
                    "artifact": artifact,
                    "prepared_at": now.isoformat(),
                    "signing": False,
                    "broadcast": False,
                },
            ]
            item.status = "UNSIGNED_PLAN_READY"
            item.version += 1
            item.updated_at = now
            result = {"operation_id": str(operation_id), "version": item.version}
            self.transactions.save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions.audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_NOTILT_DESTINATION_PREVIEW_PREPARED",
                object_type="DirectCapitalOperation",
                object_id=operation_id,
                reason="exact-native-usdc-transfer; signing=false; broadcast=false",
                correlation_id=item.correlation_id,
                object_version=item.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return item.version

    def record_direct_capital_safe_preview(
        self,
        operation_id: UUID,
        actor_id: UUID,
        *,
        expected_version: int,
        final_confirmed: bool,
        signature_request: dict[str, Any],
        idempotency_key: str,
        now: datetime,
    ) -> int:
        if not final_confirmed:
            rejections.reject(
                "CAPITAL_FINAL_CONFIRMATION_REQUIRED", "Safe preflight requires confirmation"
            )
        artifact_kind = signature_request.get("kind")
        if artifact_kind not in {
            "SAFE_ALLOWANCE_SIGNATURE_REQUEST",
            "SAFE_ERC20_DEPOSIT_UNSIGNED_TRANSACTION",
        }:
            rejections.reject(
                "SAFE_PLAN_INVALID", "Safe preflight artifact is not a supported fixed request"
            )
        if (
            signature_request.get("signing") is not False
            or signature_request.get("broadcast") is not False
        ):
            rejections.reject(
                "SAFE_PLAN_INVALID", "Safe preflight must remain signing-free and non-broadcasting"
            )
        operation = "capital.direct.safe_spending_preview"
        with self.database.session_factory.begin() as session:
            item = session.get(models.DirectCapitalOperation, operation_id, with_for_update=True)
            if item is None:
                rejections.reject(
                    "CAPITAL_DIRECT_OPERATION_NOT_FOUND", "direct capital operation is missing"
                )
            self.transactions.require_role(
                session,
                actor_id,
                "capital.execute",
                item.account_id,
                item.venue,
                team_id=item.team_id,
            )
            if item.treasury_provider != "SAFE_SPENDING_LIMIT":
                rejections.reject(
                    "SAFE_PLAN_SCOPE_MISMATCH", "operation did not select Safe Spending Limits"
                )
            outbound = item.path in {
                domain.DirectCapitalPath.VAULT_TO_BINANCE.value,
                domain.DirectCapitalPath.VAULT_TO_HYPERLIQUID.value,
            }
            expected_kind = (
                "SAFE_ALLOWANCE_SIGNATURE_REQUEST"
                if outbound
                else "SAFE_ERC20_DEPOSIT_UNSIGNED_TRANSACTION"
            )
            if artifact_kind != expected_kind:
                rejections.reject(
                    "SAFE_PLAN_DIRECTION_INVALID", "Safe artifact does not match path direction"
                )
            required_transaction_fields = {"from", "to", "data", "value"}
            if outbound and (
                signature_request.get("calldataReady") is not True
                or any(not signature_request.get(field) for field in required_transaction_fields)
            ):
                rejections.reject(
                    "SAFE_PLAN_INVALID",
                    "Safe allowance preflight must contain an exact wallet transaction",
                )
            payload = {
                "operation_id": str(operation_id),
                "expected_version": expected_version,
                "signature_request": signature_request,
                "final_confirmed": True,
            }
            digest, response = self.transactions.idempotency(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return int(response["version"])
            if item.version != expected_version:
                rejections.reject(
                    "VERSION_CONFLICT", "direct capital operation changed; refresh first"
                )
            if item.expires_at <= now:
                rejections.reject(
                    "CAPITAL_DIRECT_OPERATION_EXPIRED", "direct capital operation expired"
                )
            item.stages = [
                *item.stages,
                {
                    "code": (
                        "SAFE_ALLOWANCE_SIGNATURE_REQUEST_READY"
                        if outbound
                        else "SAFE_DEPOSIT_UNSIGNED_TRANSACTION_READY"
                    ),
                    "status": "READY_FOR_HUMAN_REVIEW",
                    "signature_request": signature_request,
                    "prepared_at": now.isoformat(),
                    "signing": False,
                    "broadcast": False,
                },
            ]
            item.blockers = [
                blocker
                for blocker in item.blockers
                if blocker
                not in {
                    "SAFE_ALLOWANCE_PREFLIGHT_REQUIRED",
                    "SAFE_SPENDING_LIMIT_NOT_CONFIGURED",
                }
            ]
            if not item.blockers:
                item.status = "UNSIGNED_PLAN_READY"
            item.version += 1
            item.updated_at = now
            result = {"operation_id": str(operation_id), "version": item.version}
            self.transactions.save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions.audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_SAFE_SPENDING_PREVIEW_PREPARED",
                object_type="DirectCapitalOperation",
                object_id=operation_id,
                reason=f"{artifact_kind}; signing=false; broadcast=false",
                correlation_id=item.correlation_id,
                object_version=item.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return item.version

    def record_direct_capital_hyperliquid_preview(
        self,
        operation_id: UUID,
        actor_id: UUID,
        *,
        expected_version: int,
        final_confirmed: bool,
        artifact: dict[str, Any],
        idempotency_key: str,
        now: datetime,
    ) -> int:
        if not final_confirmed:
            rejections.reject(
                "CAPITAL_FINAL_CONFIRMATION_REQUIRED",
                "Hyperliquid wallet handoff requires explicit confirmation",
            )
        kind = artifact.get("kind")
        if kind not in {
            "HYPERLIQUID_ARBITRUM_DEPOSIT_UNSIGNED_TRANSACTION",
            "HYPERLIQUID_WITHDRAW3_TYPED_REQUEST",
            "HYPERLIQUID_CCTP_WITHDRAWAL_TYPED_REQUEST",
            "HYPERLIQUID_USD_CLASS_TRANSFER_TYPED_REQUEST",
        }:
            rejections.reject(
                "HYPERLIQUID_CAPITAL_PLAN_INVALID",
                "Hyperliquid preflight returned an unsupported wallet request",
            )
        if artifact.get("signing") is not False or artifact.get("broadcast") is not False:
            rejections.reject(
                "HYPERLIQUID_CAPITAL_PLAN_INVALID",
                "Hyperliquid capital preflight must remain unsigned and unbroadcast",
            )
        operation = "capital.direct.hyperliquid_preview"
        with self.database.session_factory.begin() as session:
            item = session.get(models.DirectCapitalOperation, operation_id, with_for_update=True)
            if item is None:
                rejections.reject(
                    "CAPITAL_DIRECT_OPERATION_NOT_FOUND", "direct capital operation is missing"
                )
            self.transactions.require_role(
                session,
                actor_id,
                "capital.execute",
                item.account_id,
                item.venue,
                team_id=item.team_id,
            )
            expected_kinds = {
                domain.DirectCapitalPath.VAULT_TO_HYPERLIQUID.value: (
                    {"HYPERLIQUID_ARBITRUM_DEPOSIT_UNSIGNED_TRANSACTION"}
                ),
                domain.DirectCapitalPath.HYPERLIQUID_TO_VAULT.value: {
                    "HYPERLIQUID_WITHDRAW3_TYPED_REQUEST",
                    "HYPERLIQUID_CCTP_WITHDRAWAL_TYPED_REQUEST",
                    "HYPERLIQUID_USD_CLASS_TRANSFER_TYPED_REQUEST",
                },
            }.get(item.path, set())
            if kind not in expected_kinds:
                rejections.reject(
                    "HYPERLIQUID_CAPITAL_DIRECTION_INVALID",
                    "Hyperliquid wallet request does not match the frozen capital path",
                )
            payload = {
                "operation_id": str(operation_id),
                "expected_version": expected_version,
                "artifact": artifact,
                "final_confirmed": True,
            }
            digest, response = self.transactions.idempotency(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return int(response["version"])
            if item.version != expected_version:
                rejections.reject(
                    "VERSION_CONFLICT", "direct capital operation changed; refresh first"
                )
            if item.expires_at <= now:
                rejections.reject(
                    "CAPITAL_DIRECT_OPERATION_EXPIRED", "direct capital operation expired"
                )
            if kind in {
                "HYPERLIQUID_WITHDRAW3_TYPED_REQUEST",
                "HYPERLIQUID_CCTP_WITHDRAWAL_TYPED_REQUEST",
            }:
                try:
                    expected_fee = Decimal(str(artifact["expectedFee"]))
                    min_received = Decimal(str(artifact["minReceived"]))
                except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
                    raise domain.DomainRejected(
                        "HYPERLIQUID_CAPITAL_PLAN_INVALID",
                        "Hyperliquid withdrawal fee or net amount is invalid",
                    ) from exc
                if item.max_fee is None:
                    rejections.reject(
                        "HYPERLIQUID_CAPITAL_PLAN_INVALID",
                        "Hyperliquid withdrawal is missing the frozen fee limit",
                    )
                if (
                    expected_fee < 0
                    or expected_fee > item.max_fee
                    or min_received <= 0
                    or min_received > item.amount
                    or min_received != item.amount - expected_fee
                ):
                    rejections.reject(
                        "HYPERLIQUID_CAPITAL_PLAN_INVALID",
                        "Hyperliquid withdrawal changed the frozen amount or exceeded "
                        "the fee limit",
                    )
                item.min_received = min_received
            stage_code = (
                "HYPERLIQUID_DEPOSIT_WALLET_REQUEST_READY"
                if kind == "HYPERLIQUID_ARBITRUM_DEPOSIT_UNSIGNED_TRANSACTION"
                else "HYPERLIQUID_CLASS_TRANSFER_WALLET_REQUEST_READY"
                if kind == "HYPERLIQUID_USD_CLASS_TRANSFER_TYPED_REQUEST"
                else "HYPERLIQUID_CCTP_WITHDRAWAL_WALLET_REQUEST_READY"
                if kind == "HYPERLIQUID_CCTP_WITHDRAWAL_TYPED_REQUEST"
                else "HYPERLIQUID_WITHDRAW3_WALLET_REQUEST_READY"
            )
            item.stages = [
                *item.stages,
                {
                    "code": stage_code,
                    "status": "READY_FOR_HUMAN_REVIEW",
                    "artifact": artifact,
                    "prepared_at": now.isoformat(),
                    "signing": False,
                    "broadcast": False,
                    "agent_fallback": artifact.get("fallbackReason"),
                },
            ]
            item.blockers = [
                blocker
                for blocker in item.blockers
                if blocker
                not in {
                    "HYPERLIQUID_DEPOSIT_ADAPTER_UNAVAILABLE",
                    "HYPERLIQUID_WITHDRAWAL_ADAPTER_UNAVAILABLE",
                    *(
                        {"HYPERLIQUID_WITHDRAWAL_REVALIDATION_REQUIRED"}
                        if kind
                        in {
                            "HYPERLIQUID_WITHDRAW3_TYPED_REQUEST",
                            "HYPERLIQUID_CCTP_WITHDRAWAL_TYPED_REQUEST",
                        }
                        else set()
                    ),
                }
            ]
            item.status = "UNSIGNED_PLAN_READY"
            item.version += 1
            item.updated_at = now
            result = {"operation_id": str(operation_id), "version": item.version}
            self.transactions.save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions.audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_HYPERLIQUID_WALLET_REQUEST_PREPARED",
                object_type="DirectCapitalOperation",
                object_id=operation_id,
                reason=f"{kind}; agent-capability-checked; signing=false; broadcast=false",
                correlation_id=item.correlation_id,
                object_version=item.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return item.version

    def record_direct_capital_binance_preview(
        self,
        operation_id: UUID,
        actor_id: UUID,
        *,
        expected_version: int,
        artifact: dict[str, Any],
        idempotency_key: str,
        now: datetime,
    ) -> int:
        kind = artifact.get("kind")
        expected_path = {
            "BINANCE_ARBITRUM_DEPOSIT_PREFLIGHT": domain.DirectCapitalPath.VAULT_TO_BINANCE.value,
            "BINANCE_RESTRICTED_WITHDRAWAL_PREFLIGHT": (
                domain.DirectCapitalPath.BINANCE_TO_VAULT.value
            ),
        }.get(str(kind))
        if expected_path is None:
            rejections.reject(
                "BINANCE_CAPITAL_PREFLIGHT_INVALID", "unsupported Binance preflight artifact"
            )
        operation = "capital.direct.binance_preview"
        with self.database.session_factory.begin() as session:
            item = session.get(models.DirectCapitalOperation, operation_id, with_for_update=True)
            if item is None:
                rejections.reject(
                    "CAPITAL_DIRECT_OPERATION_NOT_FOUND", "direct capital operation is missing"
                )
            self.transactions.require_role(
                session,
                actor_id,
                "capital.execute",
                item.account_id,
                item.venue,
                team_id=item.team_id,
            )
            payload = {
                "operation_id": str(operation_id),
                "expected_version": expected_version,
                "artifact": artifact,
            }
            digest, response = self.transactions.idempotency(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return int(response["version"])
            if item.version != expected_version:
                rejections.reject(
                    "VERSION_CONFLICT", "direct capital operation changed; refresh first"
                )
            if item.path != expected_path:
                rejections.reject(
                    "BINANCE_CAPITAL_DIRECTION_INVALID",
                    "Binance preflight does not match path",
                )
            if item.expires_at <= now:
                rejections.reject(
                    "CAPITAL_DIRECT_OPERATION_EXPIRED", "direct capital operation expired"
                )
            item.stages = [
                *item.stages,
                {
                    "code": (
                        "BINANCE_DEPOSIT_PREFLIGHT_READY"
                        if item.path == domain.DirectCapitalPath.VAULT_TO_BINANCE.value
                        else "BINANCE_RESTRICTED_WITHDRAWAL_PREFLIGHT_READY"
                    ),
                    "status": "READY_FOR_FINAL_CONFIRMATION",
                    "artifact": artifact,
                    "prepared_at": now.isoformat(),
                },
            ]
            item.blockers = [
                blocker
                for blocker in item.blockers
                if blocker
                not in {
                    "CAPITAL_MIN_RECEIVED_INVALID",
                    "BINANCE_CAPITAL_CREDENTIALS_MISSING",
                    "BINANCE_DEPOSIT_PREFLIGHT_REQUIRED",
                    "BINANCE_RESTRICTED_WITHDRAWAL_PREFLIGHT_REQUIRED",
                }
            ]
            if item.path == domain.DirectCapitalPath.BINANCE_TO_VAULT.value:
                try:
                    min_received = Decimal(str(artifact["minReceived"]))
                except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
                    raise domain.DomainRejected(
                        "BINANCE_CAPITAL_PREFLIGHT_INVALID",
                        "Binance withdrawal preflight did not return a valid net amount",
                    ) from exc
                if min_received <= 0 or min_received > item.amount:
                    rejections.reject(
                        "BINANCE_CAPITAL_PREFLIGHT_INVALID",
                        "Binance withdrawal preflight returned an invalid net amount",
                    )
                item.min_received = min_received
            if not item.blockers:
                item.status = "UNSIGNED_PLAN_READY"
            item.version += 1
            item.updated_at = now
            result = {"operation_id": str(operation_id), "version": item.version}
            self.transactions.save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions.audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_BINANCE_PREFLIGHT_VERIFIED",
                object_type="DirectCapitalOperation",
                object_id=operation_id,
                reason=f"kind={kind}; credentials-redacted; no-transfer-submitted",
                correlation_id=item.correlation_id,
                object_version=item.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return item.version


__all__ = ["DirectCapitalPreviewService"]
