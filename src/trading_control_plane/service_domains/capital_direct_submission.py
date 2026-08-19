from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from trading_control_plane import domain, models, rejections
from trading_control_plane.service_component import ServiceComponent


class DirectCapitalSubmissionService(ServiceComponent):
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
                try:
                    preview_expires_at = datetime.fromisoformat(
                        str(preview["artifact"]["expiresAt"])
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
                blocker for blocker in item.blockers if blocker != "CAPITAL_TRANSFER_GATE_DISABLED"
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
