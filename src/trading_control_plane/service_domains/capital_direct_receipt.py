from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from trading_control_plane import domain, models, rejections
from trading_control_plane.service_component import ServiceComponent


class DirectCapitalReceiptService(ServiceComponent):
    def claim_due_direct_capital_binance_deposit(
        self,
        *,
        now: datetime,
    ) -> dict[str, Any] | None:
        """Lease one durable, system-originated Binance deposit continuation."""

        with self.database.session_factory.begin() as session:
            candidates = session.scalars(
                select(models.DirectCapitalOperation)
                .where(
                    models.DirectCapitalOperation.path
                    == domain.DirectCapitalPath.VAULT_TO_BINANCE.value,
                    models.DirectCapitalOperation.status == "AWAITING_RECEIPT",
                    models.DirectCapitalOperation.receipt_status == "PENDING",
                    models.DirectCapitalOperation.receipt_next_due_at.is_not(None),
                    models.DirectCapitalOperation.receipt_next_due_at <= now,
                    models.DirectCapitalOperation.receipt_attempt_count < 10,
                )
                .order_by(
                    models.DirectCapitalOperation.receipt_next_due_at,
                    models.DirectCapitalOperation.created_at,
                )
                .limit(20)
                .with_for_update(skip_locked=True)
            ).all()
            for item in candidates:
                lease_until = (
                    None
                    if item.receipt_poll_started_at is None
                    else item.receipt_poll_started_at + timedelta(seconds=90)
                )
                if lease_until is not None and lease_until > now:
                    continue
                expected_submission_code = (
                    "NOTILT_DESTINATION_TRANSFER_SUBMITTED_BY_HUMAN_WALLET"
                    if item.treasury_provider == "NOTILT_VAULT"
                    else "TREASURY_WITHDRAWAL_SUBMITTED_BY_HUMAN_WALLET"
                )
                submission = next(
                    (
                        stage
                        for stage in reversed(item.stages)
                        if stage.get("code") == expected_submission_code
                    ),
                    None,
                )
                preflight = next(
                    (
                        stage.get("artifact")
                        for stage in reversed(item.stages)
                        if stage.get("code") == "BINANCE_DEPOSIT_PREFLIGHT_READY"
                    ),
                    None,
                )
                transaction_hash = (
                    None if submission is None else submission.get("transaction_hash")
                )
                account = session.scalar(
                    select(models.ExchangeAccount).where(
                        models.ExchangeAccount.team_id == item.team_id,
                        models.ExchangeAccount.environment == "LIVE",
                        models.ExchangeAccount.account_id == item.account_id,
                        models.ExchangeAccount.venue == "BINANCE",
                        models.ExchangeAccount.active.is_(True),
                        models.ExchangeAccount.deleted_at.is_(None),
                    )
                )
                team = session.get(models.Team, item.team_id)
                if (
                    not isinstance(transaction_hash, str)
                    or not transaction_hash.startswith("0x")
                    or len(transaction_hash) != 66
                    or not isinstance(preflight, dict)
                    or account is None
                    or team is None
                    or item.account_id is None
                ):
                    item.status = "UNKNOWN"
                    item.receipt_status = "UNKNOWN"
                    item.receipt_next_due_at = None
                    item.receipt_last_error_code = "BINANCE_DEPOSIT_CONTINUATION_SCOPE_INVALID"
                    item.blockers = list(
                        dict.fromkeys(
                            [*item.blockers, "BINANCE_DEPOSIT_CONTINUATION_SCOPE_INVALID"]
                        )
                    )
                    item.version += 1
                    item.updated_at = now
                    continue
                destination = str(preflight.get("destination", ""))
                if (
                    destination.lower() != str(item.destination_reference or "").lower()
                    or preflight.get("asset") != "USDC"
                    or preflight.get("network") != "ARBITRUM"
                ):
                    item.status = "UNKNOWN"
                    item.receipt_status = "UNKNOWN"
                    item.receipt_next_due_at = None
                    item.receipt_last_error_code = "BINANCE_DEPOSIT_CONTINUATION_SCOPE_INVALID"
                    item.blockers = list(
                        dict.fromkeys(
                            [*item.blockers, "BINANCE_DEPOSIT_CONTINUATION_SCOPE_INVALID"]
                        )
                    )
                    item.version += 1
                    item.updated_at = now
                    continue
                token = str(uuid4())
                item.receipt_poll_stage = "BINANCE_DEPOSIT_AUTO"
                item.receipt_poll_started_at = now
                item.receipt_poll_token = token
                item.receipt_attempt_count += 1
                item.receipt_next_due_at = now + timedelta(minutes=5)
                item.receipt_last_error_code = None
                item.version += 1
                item.updated_at = now
                self.transactions.audit(
                    session,
                    actor_id=str(item.actor_id),
                    event_type="CAPITAL_BINANCE_DEPOSIT_CONTINUATION_CHECK_STARTED",
                    object_type="DirectCapitalOperation",
                    object_id=item.operation_id,
                    reason=(
                        f"attempt={item.receipt_attempt_count}/10; exact-system-tx-hash; "
                        "read-only-deposit-history"
                    ),
                    correlation_id=item.correlation_id,
                    object_version=item.version,
                    idempotency_key=(
                        f"binance-deposit-auto:{item.operation_id}:"
                        f"{item.receipt_attempt_count}"
                    ),
                    workspace_id=team.workspace_id,
                    team_id=item.team_id,
                    account_id=item.account_id,
                    environment="LIVE",
                    now=now,
                )
                return {
                    "operation_id": str(item.operation_id),
                    "workspace_id": str(team.workspace_id),
                    "team_id": str(item.team_id),
                    "actor_id": str(item.actor_id),
                    "account_id": item.account_id,
                    "account_mode": str(
                        (account.credential_metadata or {}).get("account_mode", "STANDARD")
                    ),
                    "transaction_hash": transaction_hash.lower(),
                    "destination": destination.lower(),
                    "minimum_amount": str(item.min_received or item.amount),
                    "poll_token": token,
                    "attempt_count": item.receipt_attempt_count,
                }
        return None

    def finish_direct_capital_binance_deposit_check(
        self,
        operation_id: UUID,
        *,
        poll_token: str,
        error_code: str,
        now: datetime,
    ) -> None:
        with self.database.session_factory.begin() as session:
            item = session.get(models.DirectCapitalOperation, operation_id, with_for_update=True)
            if (
                item is None
                or item.receipt_poll_stage != "BINANCE_DEPOSIT_AUTO"
                or item.receipt_poll_token != poll_token
            ):
                return
            item.receipt_poll_stage = None
            item.receipt_poll_started_at = None
            item.receipt_poll_token = None
            item.receipt_last_error_code = error_code
            if item.receipt_attempt_count >= 10:
                item.status = "UNKNOWN"
                item.receipt_status = "UNKNOWN"
                item.receipt_next_due_at = None
                item.blockers = list(
                    dict.fromkeys(
                        [
                            *item.blockers,
                            "BINANCE_DEPOSIT_CONTINUATION_EXHAUSTED",
                            error_code,
                        ]
                    )
                )
                item.stages = [
                    *item.stages,
                    {
                        "code": "BINANCE_DEPOSIT_CONTINUATION_EXHAUSTED",
                        "status": "UNKNOWN",
                        "attempt_count": item.receipt_attempt_count,
                        "last_error_code": error_code,
                        "recorded_at": now.isoformat(),
                    },
                ]
            item.version += 1
            item.updated_at = now
            self.transactions.audit(
                session,
                actor_id=str(item.actor_id),
                event_type=(
                    "CAPITAL_BINANCE_DEPOSIT_CONTINUATION_EXHAUSTED"
                    if item.receipt_attempt_count >= 10
                    else "CAPITAL_BINANCE_DEPOSIT_CONTINUATION_RETRY_SCHEDULED"
                ),
                object_type="DirectCapitalOperation",
                object_id=operation_id,
                reason=f"attempt={item.receipt_attempt_count}/10; error={error_code}",
                correlation_id=item.correlation_id,
                object_version=item.version,
                idempotency_key=(
                    f"binance-deposit-auto-finish:{operation_id}:"
                    f"{item.receipt_attempt_count}"
                ),
                team_id=item.team_id,
                account_id=item.account_id,
                environment="LIVE",
                now=now,
            )

    def claim_direct_capital_binance_deposit_internal_transfer(
        self,
        operation_id: UUID,
        *,
        poll_token: str,
        deposit_evidence: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        with self.database.session_factory.begin() as session:
            item = session.get(models.DirectCapitalOperation, operation_id, with_for_update=True)
            if item is None:
                rejections.reject(
                    "CAPITAL_DIRECT_OPERATION_NOT_FOUND", "direct capital operation is missing"
                )
            if (
                item.receipt_poll_stage != "BINANCE_DEPOSIT_AUTO"
                or item.receipt_poll_token != poll_token
            ):
                rejections.reject(
                    "BINANCE_RECEIPT_CHECK_LEASE_LOST",
                    "the Binance continuation lease is no longer owned by this worker",
                )
            gate = session.get(models.CapabilityGate, "CAPITAL_TRANSFER")
            if gate is None or gate.status != "ENABLED":
                rejections.reject(
                    "CAPITAL_TRANSFER_GATE_DISABLED",
                    "CAPITAL_TRANSFER must remain enabled for the automatic internal transfer",
                )
            expected_hash = next(
                (
                    str(stage.get("transaction_hash", "")).lower()
                    for stage in reversed(item.stages)
                    if stage.get("code")
                    in {
                        "NOTILT_DESTINATION_TRANSFER_SUBMITTED_BY_HUMAN_WALLET",
                        "TREASURY_WITHDRAWAL_SUBMITTED_BY_HUMAN_WALLET",
                    }
                    and stage.get("transaction_hash")
                ),
                "",
            )
            try:
                amount = Decimal(str(deposit_evidence["amount"]))
            except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
                raise domain.DomainRejected(
                    "BINANCE_CAPITAL_RECEIPT_MISMATCH",
                    "Binance deposit receipt amount is invalid",
                ) from exc
            if (
                deposit_evidence.get("status") != "CONFIRMED"
                or str(deposit_evidence.get("transactionHash", "")).lower() != expected_hash
                or amount <= 0
            ):
                rejections.reject(
                    "BINANCE_CAPITAL_RECEIPT_MISMATCH",
                    "Binance deposit evidence does not match the system-originated transfer",
                )
            amount_text = format(amount.normalize(), "f")
            fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "operation_id": str(operation_id),
                        "transaction_hash": expected_hash,
                        "asset": "USDC",
                        "amount": amount_text,
                        "direction": "MAIN_UMFUTURE",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            outbox = session.scalar(
                select(models.BinanceCapitalOutbox)
                .where(
                    models.BinanceCapitalOutbox.operation_id == operation_id,
                    models.BinanceCapitalOutbox.stage == "DEPOSIT_SPOT_TO_USDM",
                )
                .with_for_update()
            )
            if outbox is None:
                outbox = models.BinanceCapitalOutbox(
                    operation_id=operation_id,
                    team_id=item.team_id,
                    stage="DEPOSIT_SPOT_TO_USDM",
                    status="NEVER_ATTEMPTED",
                    attempt_count=0,
                    request_fingerprint=fingerprint,
                    external_reference=None,
                    last_error_code=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(outbox)
                session.flush()
            elif outbox.request_fingerprint != fingerprint:
                rejections.reject(
                    "BINANCE_CAPITAL_OUTBOX_CONFLICT",
                    "the durable Binance internal-transfer scope changed",
                )
            if outbox.status == "CONFIRMED":
                return {
                    "mode": "CONFIRMED",
                    "amount": amount_text,
                    "prepared_at": outbox.created_at,
                    "external_reference": outbox.external_reference,
                }
            if outbox.status == "NEVER_ATTEMPTED":
                outbox.status = "ATTEMPTING"
                outbox.attempt_count += 1
                mode = "SUBMIT"
            else:
                if outbox.status == "ATTEMPTING":
                    outbox.status = "UNKNOWN"
                mode = "RECONCILE"
            outbox.updated_at = now
            return {
                "mode": mode,
                "amount": amount_text,
                "prepared_at": outbox.created_at,
                "external_reference": outbox.external_reference,
            }

    def mark_direct_capital_binance_deposit_transfer_unknown(
        self,
        operation_id: UUID,
        *,
        error_code: str,
        now: datetime,
    ) -> None:
        with self.database.session_factory.begin() as session:
            outbox = session.scalar(
                select(models.BinanceCapitalOutbox)
                .where(
                    models.BinanceCapitalOutbox.operation_id == operation_id,
                    models.BinanceCapitalOutbox.stage == "DEPOSIT_SPOT_TO_USDM",
                )
                .with_for_update()
            )
            if outbox is None or outbox.status == "CONFIRMED":
                return
            outbox.status = "UNKNOWN"
            outbox.last_error_code = error_code
            outbox.updated_at = now

    def confirm_direct_capital_binance_deposit_continuation(
        self,
        operation_id: UUID,
        *,
        poll_token: str,
        deposit_evidence: dict[str, Any],
        internal_transfer: dict[str, Any],
        now: datetime,
    ) -> None:
        with self.database.session_factory.begin() as session:
            item = session.get(models.DirectCapitalOperation, operation_id, with_for_update=True)
            outbox = session.scalar(
                select(models.BinanceCapitalOutbox)
                .where(
                    models.BinanceCapitalOutbox.operation_id == operation_id,
                    models.BinanceCapitalOutbox.stage == "DEPOSIT_SPOT_TO_USDM",
                )
                .with_for_update()
            )
            if item is None or outbox is None:
                rejections.reject(
                    "BINANCE_CAPITAL_OUTBOX_MISSING",
                    "the durable Binance internal-transfer fence is missing",
                )
            if (
                item.receipt_poll_stage != "BINANCE_DEPOSIT_AUTO"
                or item.receipt_poll_token != poll_token
            ):
                rejections.reject(
                    "BINANCE_RECEIPT_CHECK_LEASE_LOST",
                    "the Binance continuation lease is no longer owned by this worker",
                )
            reference = internal_transfer.get("tranId")
            if internal_transfer.get("status") != "CONFIRMED" or reference is None:
                rejections.reject(
                    "BINANCE_INTERNAL_TRANSFER_PENDING",
                    "the exact Binance Spot-to-USD-M transfer is not confirmed",
                )
            outbox.status = "CONFIRMED"
            outbox.external_reference = str(reference)
            outbox.last_error_code = None
            outbox.updated_at = now
            if not any(
                stage.get("code") == "BINANCE_DEPOSIT_RECEIPT_CONFIRMED"
                for stage in item.stages
            ):
                item.stages = [
                    *item.stages,
                    {
                        "code": "BINANCE_DEPOSIT_RECEIPT_CONFIRMED",
                        "status": "CONFIRMED",
                        "evidence": {
                            "deposit": deposit_evidence,
                            "internalTransfer": internal_transfer,
                        },
                        "verified_at": now.isoformat(),
                        "automatic_continuation": True,
                    },
                ]
            item.status = "SETTLED"
            item.receipt_status = "CONFIRMED"
            item.blockers = []
            item.receipt_poll_stage = None
            item.receipt_poll_started_at = None
            item.receipt_poll_token = None
            item.receipt_next_due_at = None
            item.receipt_last_error_code = None
            item.version += 1
            item.updated_at = now
            self.transactions.audit(
                session,
                actor_id=str(item.actor_id),
                event_type="CAPITAL_BINANCE_DEPOSIT_CONTINUATION_CONFIRMED",
                object_type="DirectCapitalOperation",
                object_id=operation_id,
                reason=(
                    f"attempt={item.receipt_attempt_count}/10; exact-deposit-tx; "
                    f"exact-credited-amount={deposit_evidence['amount']}; "
                    "spot-to-usdm-confirmed"
                ),
                correlation_id=item.correlation_id,
                object_version=item.version,
                idempotency_key=f"binance-deposit-auto-confirm:{operation_id}",
                team_id=item.team_id,
                account_id=item.account_id,
                environment="LIVE",
                now=now,
            )

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
                # A chain receipt proves only the Safe sent to the frozen Binance
                # address. Settlement also requires the exact Binance deposit and
                # confirmed Spot-to-USD-M continuation.
                item.status = "AWAITING_RECEIPT"
                item.receipt_status = "PENDING"
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
            irreversible_submission_recorded = any(
                existing.get("code") == "TREASURY_DEPOSIT_SUBMITTED_BY_HUMAN_WALLET"
                for existing in item.stages
            )
            if item.expires_at <= now and (
                item.status != "AWAITING_RECEIPT" or not irreversible_submission_recorded
            ):
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
            irreversible_submission_codes = {
                domain.DirectCapitalPath.VAULT_TO_HYPERLIQUID.value: {
                    "HYPERLIQUID_DEPOSIT_SUBMITTED_BY_HUMAN_WALLET"
                },
                domain.DirectCapitalPath.HYPERLIQUID_TO_VAULT.value: {
                    "HYPERLIQUID_WITHDRAWAL_SUBMITTED_BY_HUMAN_WALLET",
                    "HYPERLIQUID_CLASS_TRANSFER_SUBMITTED_BY_HUMAN_WALLET",
                },
            }.get(item.path, set())
            irreversible_submission_recorded = any(
                existing.get("code") in irreversible_submission_codes
                for existing in item.stages
            )
            if item.expires_at <= now and (
                item.status != "AWAITING_RECEIPT" or not irreversible_submission_recorded
            ):
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
            if stage == "BINANCE_DEPOSIT" and item.receipt_next_due_at is not None:
                rejections.reject(
                    "BINANCE_DEPOSIT_CONTINUATION_WORKER_OWNED",
                    "scheduled Binance deposits are reconciled only by the durable worker",
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
            irreversible_submission_recorded = (
                required_previous_stage == "BINANCE_RESTRICTED_WITHDRAWAL_SUBMITTED"
                or any(
                    existing.get("code")
                    in {
                        "NOTILT_DESTINATION_TRANSFER_SUBMITTED_BY_HUMAN_WALLET",
                        "TREASURY_WITHDRAWAL_SUBMITTED_BY_HUMAN_WALLET",
                    }
                    for existing in item.stages
                )
            )
            if item.expires_at <= now and (
                item.status != "AWAITING_RECEIPT" or not irreversible_submission_recorded
            ):
                rejections.reject(
                    "CAPITAL_DIRECT_OPERATION_EXPIRED", "direct capital operation expired"
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
            item.receipt_next_due_at = None
            item.receipt_last_error_code = None
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
            if stage == "BINANCE_DEPOSIT" and item.receipt_next_due_at is not None:
                rejections.reject(
                    "BINANCE_DEPOSIT_CONTINUATION_WORKER_OWNED",
                    "scheduled Binance deposits are reconciled only by the durable worker",
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
