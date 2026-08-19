from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from trading_control_plane import domain, models, rejections
from trading_control_plane.service_component import ServiceComponent


class DirectCapitalReceiptService(ServiceComponent):
    def record_direct_capital_treasury_withdrawal_receipt(
        self,
        operation_id: UUID,
        actor_id: UUID,
        *,
        expected_version: int,
        evidence: dict[str, Any],
        idempotency_key: str,
        now: datetime,
    ) -> int:
        operation = "capital.direct.treasury_withdrawal_receipt"
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
                "evidence": evidence,
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
            if (
                item.path
                not in {
                    domain.DirectCapitalPath.VAULT_TO_HYPERLIQUID.value,
                    domain.DirectCapitalPath.VAULT_TO_BINANCE.value,
                }
                or item.treasury_provider != "SAFE_SPENDING_LIMIT"
            ):
                rejections.reject(
                    "TREASURY_RECEIPT_STAGE_INVALID",
                    "Safe source receipt does not match this capital path",
                )
            if item.path == domain.DirectCapitalPath.VAULT_TO_BINANCE.value and not any(
                stage.get("code") == "BINANCE_DEPOSIT_PREFLIGHT_READY" for stage in item.stages
            ):
                rejections.reject(
                    "BINANCE_DEPOSIT_PREFLIGHT_REQUIRED",
                    "confirm the current exact Binance deposit address before settlement",
                )
            submitted = next(
                (
                    stage
                    for stage in reversed(item.stages)
                    if stage.get("code") == "TREASURY_WITHDRAWAL_SUBMITTED_BY_HUMAN_WALLET"
                ),
                None,
            )
            evidence_hash = str(
                evidence.get("transactionHash") or evidence.get("transaction_hash") or ""
            ).lower()
            if (
                submitted is None
                or evidence_hash != str(submitted.get("transaction_hash", "")).lower()
            ):
                rejections.reject(
                    "TREASURY_RECEIPT_REFERENCE_MISMATCH",
                    "Safe receipt does not match the wallet-submitted transfer",
                )
            code = "TREASURY_WITHDRAWAL_RECEIPT_CONFIRMED"
            if not any(stage.get("code") == code for stage in item.stages):
                item.stages = [
                    *item.stages,
                    {
                        "code": code,
                        "status": "CONFIRMED",
                        "evidence": evidence,
                        "verified_at": now.isoformat(),
                    },
                ]
                item.version += 1
            if item.path == domain.DirectCapitalPath.VAULT_TO_BINANCE.value:
                item.status = "SETTLED"
                item.receipt_status = "CONFIRMED"
                item.blockers = [
                    blocker
                    for blocker in item.blockers
                    if blocker
                    not in {
                        "BINANCE_DEPOSIT_RECEIPT_REQUIRED",
                        "TREASURY_SOURCE_RECEIPT_REQUIRED",
                        "HUMAN_WALLET_CONFIRMATION_CANCELLED",
                    }
                ]
            else:
                item.status = "AWAITING_RECEIPT"
                item.receipt_status = "PENDING"
                item.blockers = [
                    blocker
                    for blocker in item.blockers
                    if blocker != "TREASURY_SOURCE_RECEIPT_REQUIRED"
                ]
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
                event_type="CAPITAL_TREASURY_WITHDRAWAL_RECEIPT_VERIFIED",
                object_type="DirectCapitalOperation",
                object_id=operation_id,
                reason=(
                    f"safe-to-frozen-destination; path={item.path}; "
                    f"public-receipt-verified; settled={item.status == 'SETTLED'}"
                ),
                correlation_id=item.correlation_id,
                object_version=item.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return item.version

    def record_direct_capital_notilt_receipt(
        self,
        operation_id: UUID,
        actor_id: UUID,
        *,
        expected_version: int,
        receipt_kind: str,
        evidence: dict[str, Any],
        idempotency_key: str,
        now: datetime,
    ) -> int:
        if receipt_kind not in {"RELEASE_REQUEST", "RELEASE_EXECUTION"}:
            rejections.reject("NOTILT_RECEIPT_STATE_INVALID", "NoTilt receipt kind is unsupported")
        operation = "capital.direct.notilt_receipt"
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
            if item.treasury_provider != "NOTILT_VAULT" or item.path not in {
                domain.DirectCapitalPath.VAULT_TO_BINANCE.value,
                domain.DirectCapitalPath.VAULT_TO_HYPERLIQUID.value,
            }:
                rejections.reject(
                    "NOTILT_RECEIPT_SCOPE_MISMATCH",
                    "NoTilt release receipt does not match the frozen capital path",
                )
            payload = {
                "operation_id": str(operation_id),
                "expected_version": expected_version,
                "receipt_kind": receipt_kind,
                "evidence": evidence,
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
            submission_code = (
                "TREASURY_WITHDRAWAL_SUBMITTED_BY_HUMAN_WALLET"
                if receipt_kind == "RELEASE_REQUEST"
                else "NOTILT_RELEASE_EXECUTION_SUBMITTED_BY_HUMAN_WALLET"
            )
            submitted = next(
                (stage for stage in reversed(item.stages) if stage.get("code") == submission_code),
                None,
            )
            evidence_hash = str(evidence.get("transaction_hash", "")).lower()
            if (
                submitted is None
                or evidence_hash != str(submitted.get("transaction_hash", "")).lower()
            ):
                rejections.reject(
                    "NOTILT_RECEIPT_REFERENCE_MISMATCH",
                    "NoTilt receipt does not match the recorded wallet transaction",
                )
            if receipt_kind == "RELEASE_EXECUTION" and not any(
                stage.get("code") == "NOTILT_RELEASE_REQUEST_RECEIPT_CONFIRMED"
                for stage in item.stages
            ):
                rejections.reject(
                    "NOTILT_RELEASE_NOT_EXECUTABLE",
                    "verified NoTilt release request is required before execution",
                )
            if receipt_kind == "RELEASE_REQUEST":
                request_id = str(evidence.get("request_id", ""))
                try:
                    execute_after = datetime.fromisoformat(str(evidence["execute_after"]))
                    expires_at = datetime.fromisoformat(str(evidence["expires_at"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise domain.DomainRejected(
                        "NOTILT_RECEIPT_INVALID",
                        "NoTilt release receipt has invalid protocol timing",
                    ) from exc
                if not request_id.startswith("0x") or len(request_id) != 66:
                    rejections.reject("NOTILT_RECEIPT_INVALID", "NoTilt request id is invalid")
                if execute_after >= expires_at or expires_at <= now:
                    rejections.reject("NOTILT_RECEIPT_INVALID", "NoTilt release window is invalid")
                item.execute_after = execute_after
                stage_code = "NOTILT_RELEASE_REQUEST_RECEIPT_CONFIRMED"
                item.status = "AWAITING_RECEIPT"
            else:
                request_receipt = next(
                    stage
                    for stage in reversed(item.stages)
                    if stage.get("code") == "NOTILT_RELEASE_REQUEST_RECEIPT_CONFIRMED"
                )
                if evidence.get("request_id") != request_receipt.get("evidence", {}).get(
                    "request_id"
                ):
                    rejections.reject(
                        "NOTILT_RECEIPT_REFERENCE_MISMATCH",
                        "NoTilt execution receipt request id changed",
                    )
                stage_code = "NOTILT_RELEASE_EXECUTION_RECEIPT_CONFIRMED"
                item.status = "AWAITING_RECEIPT"
            if not any(stage.get("code") == stage_code for stage in item.stages):
                item.stages = [
                    *item.stages,
                    {
                        "code": stage_code,
                        "status": "CONFIRMED",
                        "evidence": evidence,
                        "verified_at": now.isoformat(),
                    },
                ]
                item.version += 1
            item.receipt_status = "PENDING"
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
                event_type=f"CAPITAL_NOTILT_{receipt_kind}_RECEIPT_VERIFIED",
                object_type="DirectCapitalOperation",
                object_id=operation_id,
                reason="public-receipt-verified; no-signature-material-stored",
                correlation_id=item.correlation_id,
                object_version=item.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return item.version

    def record_direct_capital_treasury_receipt(
        self,
        operation_id: UUID,
        actor_id: UUID,
        *,
        expected_version: int,
        evidence: dict[str, Any],
        idempotency_key: str,
        now: datetime,
    ) -> int:
        operation = "capital.direct.treasury_receipt"
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
            if item.path != domain.DirectCapitalPath.HYPERLIQUID_TO_VAULT.value:
                rejections.reject(
                    "TREASURY_RECEIPT_STAGE_INVALID",
                    "treasury deposit receipt is only valid for Hyperliquid withdrawal paths",
                )
            payload = {
                "operation_id": str(operation_id),
                "expected_version": expected_version,
                "evidence": evidence,
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
            submitted = next(
                (
                    stage
                    for stage in reversed(item.stages)
                    if stage.get("code") == "TREASURY_DEPOSIT_SUBMITTED_BY_HUMAN_WALLET"
                ),
                None,
            )
            if submitted is None:
                rejections.reject(
                    "TREASURY_WALLET_SUBMISSION_REQUIRED",
                    "record the human wallet deposit transaction before receipt verification",
                )
            evidence_hash = str(
                evidence.get("transactionHash") or evidence.get("transaction_hash") or ""
            ).lower()
            if evidence_hash != str(submitted.get("transaction_hash", "")).lower():
                rejections.reject(
                    "TREASURY_RECEIPT_REFERENCE_MISMATCH",
                    "treasury receipt does not match the recorded wallet submission",
                )
            code = "TREASURY_DESTINATION_RECEIPT_CONFIRMED"
            if not any(stage.get("code") == code for stage in item.stages):
                item.stages = [
                    *item.stages,
                    {
                        "code": code,
                        "status": "CONFIRMED",
                        "evidence": evidence,
                        "verified_at": now.isoformat(),
                    },
                ]
                item.version += 1
            confirmed = {str(stage.get("code")) for stage in item.stages}
            required = {
                "HYPERLIQUID_WITHDRAWAL_LEDGER_CONFIRMED",
                "HYPERLIQUID_WITHDRAWAL_ARBITRUM_CONFIRMED",
                code,
            }
            if required.issubset(confirmed):
                item.status = "SETTLED"
                item.receipt_status = "CONFIRMED"
                item.blockers = [
                    blocker
                    for blocker in item.blockers
                    if blocker
                    not in {
                        "TREASURY_DESTINATION_RECEIPT_REQUIRED",
                        "HYPERLIQUID_HUMAN_WALLET_CONFIRMATION_REQUIRED",
                    }
                ]
            else:
                item.status = "AWAITING_RECEIPT"
                item.receipt_status = "PENDING"
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
                event_type="CAPITAL_TREASURY_DESTINATION_RECEIPT_VERIFIED",
                object_type="DirectCapitalOperation",
                object_id=operation_id,
                reason=(
                    f"provider={item.treasury_provider}; public-receipt-verified; "
                    f"settled={item.status == 'SETTLED'}"
                ),
                correlation_id=item.correlation_id,
                object_version=item.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return item.version

    def record_direct_capital_hyperliquid_receipt(
        self,
        operation_id: UUID,
        actor_id: UUID,
        *,
        expected_version: int,
        stage: str,
        evidence: dict[str, Any],
        idempotency_key: str,
        now: datetime,
    ) -> int:
        operation = "capital.direct.hyperliquid_receipt"
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
            allowed = {
                domain.DirectCapitalPath.VAULT_TO_HYPERLIQUID.value: {
                    "HYPERLIQUID_DEPOSIT_ARBITRUM",
                    "HYPERLIQUID_DEPOSIT_LEDGER",
                },
                domain.DirectCapitalPath.HYPERLIQUID_TO_VAULT.value: {
                    "HYPERLIQUID_WITHDRAWAL_LEDGER",
                    "HYPERLIQUID_WITHDRAWAL_ARBITRUM",
                    "HYPERLIQUID_CLASS_TRANSFER_LEDGER",
                },
            }.get(item.path, set())
            if stage not in allowed:
                rejections.reject(
                    "HYPERLIQUID_RECEIPT_STAGE_INVALID",
                    "receipt stage does not match the frozen capital path",
                )
            payload = {
                "operation_id": str(operation_id),
                "expected_version": expected_version,
                "stage": stage,
                "evidence": evidence,
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
            code = f"{stage}_CONFIRMED"
            if not any(existing.get("code") == code for existing in item.stages):
                item.stages = [
                    *item.stages,
                    {
                        "code": code,
                        "status": "CONFIRMED",
                        "evidence": evidence,
                        "verified_at": now.isoformat(),
                    },
                ]
                item.version += 1
            confirmed = {str(existing.get("code")) for existing in item.stages}
            required = (
                {"HYPERLIQUID_CLASS_TRANSFER_LEDGER_CONFIRMED"}
                if stage == "HYPERLIQUID_CLASS_TRANSFER_LEDGER"
                else {
                    f"{candidate}_CONFIRMED"
                    for candidate in allowed
                    if candidate != "HYPERLIQUID_CLASS_TRANSFER_LEDGER"
                }
            )
            item.status = "AWAITING_RECEIPT"
            item.receipt_status = "PENDING"
            if required.issubset(confirmed):
                if stage == "HYPERLIQUID_CLASS_TRANSFER_LEDGER":
                    item.status = "BLOCKED"
                    item.receipt_status = "CONFIRMED"
                    item.blockers = list(
                        dict.fromkeys(
                            [
                                *item.blockers,
                                "HYPERLIQUID_WITHDRAWAL_REVALIDATION_REQUIRED",
                            ]
                        )
                    )
                elif item.path == domain.DirectCapitalPath.HYPERLIQUID_TO_VAULT.value and (
                    item.treasury_provider == "SAFE_SPENDING_LIMIT"
                    or "TREASURY_DESTINATION_RECEIPT_CONFIRMED" in confirmed
                ):
                    item.status = "SETTLED"
                    item.receipt_status = "CONFIRMED"
                    item.blockers = [
                        blocker
                        for blocker in item.blockers
                        if blocker
                        not in {
                            "TREASURY_DESTINATION_RECEIPT_REQUIRED",
                            "HYPERLIQUID_HUMAN_WALLET_CONFIRMATION_REQUIRED",
                        }
                    ]
                elif (
                    item.path == domain.DirectCapitalPath.VAULT_TO_HYPERLIQUID.value
                    and "TREASURY_WITHDRAWAL_RECEIPT_CONFIRMED" in confirmed
                ):
                    item.status = "SETTLED"
                    item.receipt_status = "CONFIRMED"
                    item.blockers = [
                        blocker
                        for blocker in item.blockers
                        if blocker
                        not in {
                            "TREASURY_SOURCE_RECEIPT_REQUIRED",
                            "HYPERLIQUID_HUMAN_WALLET_CONFIRMATION_REQUIRED",
                        }
                    ]
                else:
                    item.blockers = list(
                        dict.fromkeys(
                            [
                                *item.blockers,
                                (
                                    "TREASURY_SOURCE_RECEIPT_REQUIRED"
                                    if item.path
                                    == domain.DirectCapitalPath.VAULT_TO_HYPERLIQUID.value
                                    else "TREASURY_DESTINATION_RECEIPT_REQUIRED"
                                ),
                            ]
                        )
                    )
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
                event_type="CAPITAL_HYPERLIQUID_RECEIPT_VERIFIED",
                object_type="DirectCapitalOperation",
                object_id=operation_id,
                reason=f"stage={stage}; public-receipt-verified; destination-still-fail-closed",
                correlation_id=item.correlation_id,
                object_version=item.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return item.version

    def record_direct_capital_binance_receipt(
        self,
        operation_id: UUID,
        actor_id: UUID,
        *,
        expected_version: int,
        stage: str,
        evidence: dict[str, Any],
        idempotency_key: str,
        now: datetime,
    ) -> int:
        expected_path = {
            "BINANCE_DEPOSIT": domain.DirectCapitalPath.VAULT_TO_BINANCE.value,
            "BINANCE_WITHDRAWAL": domain.DirectCapitalPath.BINANCE_TO_VAULT.value,
        }.get(stage)
        if expected_path is None:
            rejections.reject(
                "BINANCE_CAPITAL_RECEIPT_STAGE_INVALID", "unknown Binance receipt stage"
            )
        operation = "capital.direct.binance_receipt"
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
                "stage": stage,
                "evidence": evidence,
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
                    "BINANCE_CAPITAL_RECEIPT_STAGE_INVALID", "receipt does not match path"
                )
            if item.expires_at <= now:
                rejections.reject(
                    "CAPITAL_DIRECT_OPERATION_EXPIRED", "direct capital operation expired"
                )
            required_previous_stage = (
                "BINANCE_DEPOSIT_PREFLIGHT_READY"
                if stage == "BINANCE_DEPOSIT"
                else "BINANCE_RESTRICTED_WITHDRAWAL_SUBMITTED"
            )
            if not any(existing.get("code") == required_previous_stage for existing in item.stages):
                rejections.reject(
                    "BINANCE_CAPITAL_PREVIOUS_STAGE_REQUIRED",
                    "Binance receipt cannot be accepted before the frozen prior stage",
                )
            code = f"{stage}_RECEIPT_CONFIRMED"
            if not any(existing.get("code") == code for existing in item.stages):
                item.stages = [
                    *item.stages,
                    {
                        "code": code,
                        "status": "CONFIRMED",
                        "evidence": evidence,
                        "verified_at": now.isoformat(),
                    },
                ]
                item.version += 1
            item.status = "SETTLED"
            item.receipt_status = "CONFIRMED"
            item.blockers = []
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
                event_type="CAPITAL_BINANCE_RECEIPT_VERIFIED",
                object_type="DirectCapitalOperation",
                object_id=operation_id,
                reason=f"stage={stage}; exact-receipt-and-chain-scope-verified",
                correlation_id=item.correlation_id,
                object_version=item.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return item.version

    def acquire_direct_capital_binance_receipt_poll(
        self,
        operation_id: UUID,
        actor_id: UUID,
        *,
        expected_version: int,
        stage: str,
        token: str,
        now: datetime,
    ) -> None:
        """Lease one Binance receipt stage across tabs and server requests."""

        expected_path = {
            "BINANCE_DEPOSIT": domain.DirectCapitalPath.VAULT_TO_BINANCE.value,
            "BINANCE_WITHDRAWAL": domain.DirectCapitalPath.BINANCE_TO_VAULT.value,
        }.get(stage)
        if expected_path is None:
            rejections.reject(
                "BINANCE_CAPITAL_RECEIPT_STAGE_INVALID", "unknown Binance receipt stage"
            )
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
            if item.version != expected_version:
                rejections.reject(
                    "VERSION_CONFLICT", "direct capital operation changed; refresh first"
                )
            if item.path != expected_path:
                rejections.reject(
                    "BINANCE_CAPITAL_RECEIPT_STAGE_INVALID", "receipt does not match path"
                )
            lease_until = (
                None
                if item.receipt_poll_started_at is None
                else item.receipt_poll_started_at + timedelta(seconds=90)
            )
            if (
                item.receipt_poll_token is not None
                and item.receipt_poll_token != token
                and lease_until is not None
                and lease_until > now
            ):
                raise domain.DomainRejected(
                    "BINANCE_RECEIPT_CHECK_IN_PROGRESS",
                    "this Binance receipt stage already has an active server-side check",
                    metadata={
                        "operation_id": str(operation_id),
                        "receipt_stage": item.receipt_poll_stage,
                        "next_retry_at": lease_until.astimezone(UTC).isoformat(),
                    },
                )
            item.receipt_poll_stage = stage
            item.receipt_poll_started_at = now
            item.receipt_poll_token = token
            item.updated_at = now

    def release_direct_capital_binance_receipt_poll(
        self,
        operation_id: UUID,
        *,
        stage: str,
        token: str,
        now: datetime,
    ) -> None:
        with self.database.session_factory.begin() as session:
            item = session.get(models.DirectCapitalOperation, operation_id, with_for_update=True)
            if item is None:
                return
            if item.receipt_poll_stage != stage or item.receipt_poll_token != token:
                return
            item.receipt_poll_stage = None
            item.receipt_poll_started_at = None
            item.receipt_poll_token = None
            item.updated_at = now


__all__ = ["DirectCapitalReceiptService"]
