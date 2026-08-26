from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select

from trading_control_plane import domain, models, rejections
from trading_control_plane.service_component import ServiceComponent


class DirectCapitalSubmissionService(ServiceComponent):
    def claim_direct_capital_binance_withdrawal_submission(
        self,
        operation_id: UUID,
        actor_id: UUID,
        *,
        expected_version: int,
        artifact: dict[str, Any],
        idempotency_key: str,
        now: datetime,
    ) -> int:
        """Commit a one-way write fence before the first Binance withdrawal side effect."""

        fingerprint = hashlib.sha256(
            json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
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
            if item.path != domain.DirectCapitalPath.BINANCE_TO_VAULT.value:
                rejections.reject(
                    "BINANCE_CAPITAL_DIRECTION_INVALID",
                    "submission is not a Binance withdrawal",
                )
            frozen = next(
                (
                    stage.get("artifact")
                    for stage in reversed(item.stages)
                    if stage.get("code") == "BINANCE_RESTRICTED_WITHDRAWAL_PREFLIGHT_READY"
                ),
                None,
            )
            if not isinstance(frozen, dict) or frozen != artifact:
                rejections.reject(
                    "BINANCE_CAPITAL_PREFLIGHT_REQUIRED", "current frozen preflight is required"
                )
            gate = session.get(models.CapabilityGate, "CAPITAL_TRANSFER")
            if gate is None or gate.status != "ENABLED":
                rejections.reject(
                    "CAPITAL_TRANSFER_GATE_DISABLED",
                    "CAPITAL_TRANSFER must remain enabled for withdrawal submission",
                )
            outbox = session.scalar(
                select(models.BinanceCapitalOutbox)
                .where(
                    models.BinanceCapitalOutbox.operation_id == operation_id,
                    models.BinanceCapitalOutbox.stage == "WITHDRAWAL",
                )
                .with_for_update()
            )
            if outbox is not None:
                if outbox.request_fingerprint != fingerprint:
                    rejections.reject(
                        "BINANCE_CAPITAL_OUTBOX_CONFLICT",
                        "the durable Binance withdrawal scope changed",
                    )
                rejections.reject(
                    "BINANCE_CAPITAL_SUBMISSION_UNKNOWN",
                    "a Binance withdrawal attempt already exists; reconcile the fixed order id",
                )
            outbox = models.BinanceCapitalOutbox(
                operation_id=operation_id,
                team_id=item.team_id,
                stage="WITHDRAWAL",
                status="ATTEMPTING",
                attempt_count=1,
                request_fingerprint=fingerprint,
                external_reference=None,
                last_error_code=None,
                created_at=now,
                updated_at=now,
            )
            session.add(outbox)
            item.stages = [
                *item.stages,
                {
                    "code": "BINANCE_RESTRICTED_WITHDRAWAL_ATTEMPTING",
                    "status": "UNKNOWN_UNTIL_EXCHANGE_REFERENCE",
                    "recorded_at": now.isoformat(),
                },
            ]
            item.status = "UNKNOWN"
            item.receipt_status = "UNKNOWN"
            item.blockers = list(
                dict.fromkeys([*item.blockers, "BINANCE_WITHDRAWAL_SUBMISSION_IN_PROGRESS"])
            )
            item.version += 1
            item.updated_at = now
            self.transactions.audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_BINANCE_WITHDRAWAL_ATTEMPTING",
                object_type="DirectCapitalOperation",
                object_id=operation_id,
                reason="durable-write-fence-committed-before-binance-call",
                correlation_id=item.correlation_id,
                object_version=item.version,
                idempotency_key=idempotency_key,
                team_id=item.team_id,
                account_id=item.account_id,
                environment="LIVE",
                now=now,
            )
            return item.version

    def record_direct_capital_binance_submission_failure(
        self,
        operation_id: UUID,
        actor_id: UUID,
        *,
        claimed_version: int,
        error_code: str,
        idempotency_key: str,
        now: datetime,
    ) -> int:
        with self.database.session_factory.begin() as session:
            item = session.get(models.DirectCapitalOperation, operation_id, with_for_update=True)
            outbox = session.scalar(
                select(models.BinanceCapitalOutbox)
                .where(
                    models.BinanceCapitalOutbox.operation_id == operation_id,
                    models.BinanceCapitalOutbox.stage == "WITHDRAWAL",
                )
                .with_for_update()
            )
            if item is None or outbox is None:
                rejections.reject(
                    "BINANCE_CAPITAL_OUTBOX_MISSING",
                    "the durable Binance withdrawal write fence is missing",
                )
            self.transactions.require_role(
                session,
                actor_id,
                "capital.execute",
                item.account_id,
                item.venue,
                team_id=item.team_id,
            )
            if item.version != claimed_version or outbox.status != "ATTEMPTING":
                rejections.reject(
                    "BINANCE_CAPITAL_SUBMISSION_UNKNOWN",
                    "Binance withdrawal state changed; reconcile before any retry",
                )
            outbox.status = "UNKNOWN"
            outbox.last_error_code = error_code
            outbox.updated_at = now
            item.stages = [
                *item.stages,
                {
                    "code": "BINANCE_RESTRICTED_WITHDRAWAL_SUBMISSION_UNKNOWN",
                    "status": "UNKNOWN",
                    "error_code": error_code,
                    "recorded_at": now.isoformat(),
                },
            ]
            item.status = "UNKNOWN"
            item.receipt_status = "UNKNOWN"
            item.blockers = list(
                dict.fromkeys(
                    [
                        blocker
                        for blocker in item.blockers
                        if blocker != "BINANCE_WITHDRAWAL_SUBMISSION_IN_PROGRESS"
                    ]
                    + [error_code]
                )
            )
            item.version += 1
            item.updated_at = now
            self.transactions.audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_BINANCE_WITHDRAWAL_UNKNOWN",
                object_type="DirectCapitalOperation",
                object_id=operation_id,
                reason=f"fail-closed; error={error_code}; no-blind-retry",
                correlation_id=item.correlation_id,
                object_version=item.version,
                idempotency_key=idempotency_key,
                team_id=item.team_id,
                account_id=item.account_id,
                environment="LIVE",
                now=now,
            )
            return item.version

    def record_direct_capital_wallet_submission(
        self,
        operation_id: UUID,
        actor_id: UUID,
        *,
        expected_version: int,
        stage: str,
        outcome: str,
        transaction_hash: str | None,
        action_hash: str | None,
        nonce: int | None,
        final_confirmed: bool,
        idempotency_key: str,
        now: datetime,
    ) -> int:
        if not final_confirmed:
            rejections.reject(
                "CAPITAL_FINAL_CONFIRMATION_REQUIRED",
                "wallet result recording requires explicit confirmation",
            )
        operation = "capital.direct.wallet_submission"
        payload = {
            "operation_id": str(operation_id),
            "expected_version": expected_version,
            "stage": stage,
            "outcome": outcome,
            "transaction_hash": transaction_hash,
            "action_hash": action_hash,
            "nonce": nonce,
            "final_confirmed": True,
        }
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
            if outcome not in {"SUBMITTED", "CANCELLED"}:
                rejections.reject(
                    "CAPITAL_WALLET_OUTCOME_INVALID",
                    "wallet outcome must be SUBMITTED or CANCELLED",
                )
            if outcome == "SUBMITTED":
                gate = session.get(models.CapabilityGate, "CAPITAL_TRANSFER")
                if gate is None or gate.status != domain.CapabilityStatus.ENABLED.value:
                    rejections.reject(
                        "CAPITAL_TRANSFER_GATE_DISABLED",
                        "CAPITAL_TRANSFER must remain enabled for wallet submission",
                    )
            allowed_stages = {
                domain.DirectCapitalPath.VAULT_TO_BINANCE.value: {
                    "TREASURY_WITHDRAWAL",
                    "NOTILT_RELEASE_EXECUTION",
                    "NOTILT_DESTINATION_TRANSFER",
                },
                domain.DirectCapitalPath.VAULT_TO_HYPERLIQUID.value: {
                    "TREASURY_WITHDRAWAL",
                    "NOTILT_RELEASE_EXECUTION",
                    "HYPERLIQUID_DEPOSIT",
                },
                domain.DirectCapitalPath.HYPERLIQUID_TO_VAULT.value: {
                    "HYPERLIQUID_WITHDRAWAL",
                    "HYPERLIQUID_CLASS_TRANSFER",
                    "TREASURY_DEPOSIT",
                },
            }.get(item.path, set())
            if stage not in allowed_stages:
                rejections.reject(
                    "CAPITAL_WALLET_STAGE_INVALID",
                    "wallet result does not match the frozen capital path",
                )
            submission_code = f"{stage}_SUBMITTED_BY_HUMAN_WALLET"
            if outcome == "SUBMITTED" and any(
                existing.get("code") == submission_code
                for existing in item.stages
                if isinstance(existing, dict)
            ):
                rejections.reject(
                    "CAPITAL_WALLET_SUBMISSION_ALREADY_RECORDED",
                    "this irreversible wallet submission is already recorded",
                )
            if stage == "TREASURY_WITHDRAWAL":
                treasury_preview_codes = {
                    "NOTILT_UNSIGNED_RELEASE_REQUEST_PREVIEW",
                    "SAFE_ALLOWANCE_SIGNATURE_REQUEST_READY",
                }
            elif stage == "TREASURY_DEPOSIT":
                treasury_preview_codes = {
                    "NOTILT_UNSIGNED_DEPOSIT_PREVIEW",
                    "SAFE_DEPOSIT_UNSIGNED_TRANSACTION_READY",
                }
            elif stage == "NOTILT_RELEASE_EXECUTION":
                treasury_preview_codes = {"NOTILT_UNSIGNED_RELEASE_EXECUTION_PREVIEW"}
            elif stage == "NOTILT_DESTINATION_TRANSFER":
                treasury_preview_codes = {"NOTILT_DESTINATION_TRANSFER_PREVIEW"}
            else:
                treasury_preview_codes = set()
            preview = (
                next(
                    (
                        existing
                        for existing in reversed(item.stages)
                        if isinstance(existing, dict)
                        and str(existing.get("code", "")) in treasury_preview_codes
                    ),
                    None,
                )
                if stage
                in {
                    "TREASURY_DEPOSIT",
                    "TREASURY_WITHDRAWAL",
                    "NOTILT_RELEASE_EXECUTION",
                    "NOTILT_DESTINATION_TRANSFER",
                }
                else next(
                    (
                        existing
                        for existing in reversed(item.stages)
                        if isinstance(existing, dict)
                        and isinstance(existing.get("artifact"), dict)
                        and str(existing["artifact"].get("kind", "")).startswith("HYPERLIQUID_")
                    ),
                    None,
                )
            )
            if preview is None:
                rejections.reject(
                    "HYPERLIQUID_CAPITAL_PREFLIGHT_REQUIRED",
                    "prepare a current unsigned wallet request before recording a wallet result",
                )
            if outcome == "SUBMITTED" and stage.startswith("HYPERLIQUID_"):
                expected_kind = {
                    "HYPERLIQUID_DEPOSIT": (
                        "HYPERLIQUID_ARBITRUM_DEPOSIT_UNSIGNED_TRANSACTION"
                    ),
                    "HYPERLIQUID_WITHDRAWAL": (
                        "HYPERLIQUID_CCTP_WITHDRAWAL_TYPED_REQUEST"
                    ),
                    "HYPERLIQUID_CLASS_TRANSFER": (
                        "HYPERLIQUID_USD_CLASS_TRANSFER_TYPED_REQUEST"
                    ),
                }.get(stage)
                artifact = preview["artifact"]
                if not isinstance(artifact, dict) or artifact.get("kind") != expected_kind:
                    rejections.reject(
                        "HYPERLIQUID_CAPITAL_PREFLIGHT_REQUIRED",
                        "wallet submission does not match the latest frozen Hyperliquid plan",
                    )
                if stage in {"HYPERLIQUID_WITHDRAWAL", "HYPERLIQUID_CLASS_TRANSFER"} and (
                    nonce is None or artifact.get("nonce") != nonce
                ):
                    rejections.reject(
                        "HYPERLIQUID_WALLET_NONCE_MISMATCH",
                        "wallet submission nonce does not match the latest frozen plan",
                    )
                try:
                    preview_expires_at = datetime.fromisoformat(
                        str(artifact["expiresAt"])
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise domain.DomainRejected(
                        "HYPERLIQUID_CAPITAL_PLAN_INVALID",
                        "stored Hyperliquid preflight is invalid",
                    ) from exc
                if preview_expires_at <= now:
                    rejections.reject(
                        "HYPERLIQUID_CAPITAL_PREFLIGHT_EXPIRED",
                        "wallet request expired; rebuild it from current facts before signing",
                    )
            if outcome == "SUBMITTED" and stage == "NOTILT_RELEASE_EXECUTION":
                release_receipt = next(
                    (
                        existing.get("evidence")
                        for existing in reversed(item.stages)
                        if existing.get("code") == "NOTILT_RELEASE_REQUEST_RECEIPT_CONFIRMED"
                    ),
                    None,
                )
                if release_receipt is None:
                    rejections.reject(
                        "NOTILT_RECEIPT_INVALID",
                        "stored NoTilt release receipt is missing",
                    )
                try:
                    release_expires_at = datetime.fromisoformat(str(release_receipt["expires_at"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise domain.DomainRejected(
                        "NOTILT_RECEIPT_INVALID",
                        "stored NoTilt release window is invalid",
                    ) from exc
                if release_expires_at <= now:
                    rejections.reject("NOTILT_RELEASE_EXPIRED", "NoTilt release request expired")
            if outcome == "SUBMITTED" and stage == "NOTILT_DESTINATION_TRANSFER":
                try:
                    preview_expires_at = datetime.fromisoformat(
                        str(preview["artifact"]["expiresAt"])
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise domain.DomainRejected(
                        "NOTILT_PLAN_INVALID",
                        "stored NoTilt destination transfer is invalid",
                    ) from exc
                if preview_expires_at <= now:
                    rejections.reject(
                        "NOTILT_DESTINATION_PREFLIGHT_EXPIRED",
                        "destination transfer expired; rebuild it before signing",
                    )
            if outcome == "CANCELLED":
                item.stages = [
                    *item.stages,
                    {
                        "code": f"{stage}_WALLET_CANCELLED",
                        "status": "CANCELLED_BY_USER",
                        "recorded_at": now.isoformat(),
                    },
                ]
                item.status = "BLOCKED"
                item.receipt_status = "NOT_SUBMITTED"
                item.blockers = list(
                    dict.fromkeys([*item.blockers, "HUMAN_WALLET_CONFIRMATION_CANCELLED"])
                )
                event_type = "CAPITAL_HUMAN_WALLET_CANCELLED"
            else:
                item.stages = [
                    *item.stages,
                    {
                        "code": f"{stage}_SUBMITTED_BY_HUMAN_WALLET",
                        "status": "AWAITING_RECEIPT",
                        "transaction_hash": transaction_hash,
                        "action_hash": action_hash,
                        "nonce": nonce,
                        "recorded_at": now.isoformat(),
                    },
                ]
                item.status = "AWAITING_RECEIPT"
                item.receipt_status = "PENDING"
                resolved_submission_blockers = {"HUMAN_WALLET_CONFIRMATION_CANCELLED"}
                if stage.startswith("HYPERLIQUID_"):
                    resolved_submission_blockers.add(
                        "HYPERLIQUID_HUMAN_WALLET_CONFIRMATION_REQUIRED"
                    )
                item.blockers = [
                    blocker
                    for blocker in item.blockers
                    if blocker not in resolved_submission_blockers
                ]
                schedules_binance_deposit = (
                    item.path == domain.DirectCapitalPath.VAULT_TO_BINANCE.value
                    and (
                        (
                            item.treasury_provider == "SAFE_SPENDING_LIMIT"
                            and stage == "TREASURY_WITHDRAWAL"
                        )
                        or (
                            item.treasury_provider == "NOTILT_VAULT"
                            and stage == "NOTILT_DESTINATION_TRANSFER"
                        )
                    )
                )
                if schedules_binance_deposit:
                    item.receipt_next_due_at = now + timedelta(minutes=5)
                    item.receipt_attempt_count = 0
                    item.receipt_last_error_code = None
                event_type = "CAPITAL_HUMAN_WALLET_SUBMISSION_RECORDED"
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
                event_type=event_type,
                object_type="DirectCapitalOperation",
                object_id=operation_id,
                reason=f"stage={stage}; outcome={outcome}; no-signature-material-stored",
                correlation_id=item.correlation_id,
                object_version=item.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return item.version

    def record_direct_capital_binance_submission(
        self,
        operation_id: UUID,
        actor_id: UUID,
        *,
        expected_version: int,
        submission: dict[str, Any],
        idempotency_key: str,
        now: datetime,
    ) -> int:
        operation = "capital.direct.binance_submission"
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
                "submission": submission,
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
            if item.path != domain.DirectCapitalPath.BINANCE_TO_VAULT.value:
                rejections.reject(
                    "BINANCE_CAPITAL_DIRECTION_INVALID",
                    "submission is not a Binance withdrawal",
                )
            if not any(
                stage.get("code") == "BINANCE_RESTRICTED_WITHDRAWAL_PREFLIGHT_READY"
                for stage in item.stages
            ):
                rejections.reject(
                    "BINANCE_CAPITAL_PREFLIGHT_REQUIRED", "current preflight is required"
                )
            outbox = session.scalar(
                select(models.BinanceCapitalOutbox)
                .where(
                    models.BinanceCapitalOutbox.operation_id == operation_id,
                    models.BinanceCapitalOutbox.stage == "WITHDRAWAL",
                )
                .with_for_update()
            )
            if outbox is None or outbox.status != "ATTEMPTING":
                rejections.reject(
                    "BINANCE_CAPITAL_OUTBOX_MISSING",
                    "the durable Binance withdrawal write fence is not active",
                )
            external_reference = next(
                (
                    submission.get(key)
                    for key in ("id", "withdrawalId", "txid", "transactionId")
                    if submission.get(key)
                ),
                None,
            )
            if external_reference is None:
                rejections.reject(
                    "CAPITAL_RESULT_UNKNOWN",
                    "Binance withdrawal returned no durable operation identity",
                )
            outbox.status = "CONFIRMED"
            outbox.external_reference = str(external_reference)
            outbox.last_error_code = None
            outbox.updated_at = now
            item.stages = [
                *item.stages,
                {
                    "code": "BINANCE_RESTRICTED_WITHDRAWAL_SUBMITTED",
                    "status": "AWAITING_RECEIPT",
                    "submission": submission,
                    "recorded_at": now.isoformat(),
                },
            ]
            item.status = "AWAITING_RECEIPT"
            item.receipt_status = "PENDING"
            item.blockers = [
                blocker
                for blocker in item.blockers
                if blocker
                not in {
                    "CAPITAL_TRANSFER_GATE_DISABLED",
                    "BINANCE_WITHDRAWAL_SUBMISSION_IN_PROGRESS",
                }
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
                event_type="CAPITAL_BINANCE_WITHDRAWAL_SUBMITTED",
                object_type="DirectCapitalOperation",
                object_id=operation_id,
                reason="restricted-api; final-confirmed; credentials-redacted",
                correlation_id=item.correlation_id,
                object_version=item.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return item.version


__all__ = ["DirectCapitalSubmissionService"]
