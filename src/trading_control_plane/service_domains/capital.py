from __future__ import annotations

from trading_control_plane.service_core import (
    ACTIVE_INTENT_STATUSES,
    MAX_FACT_CLOCK_SKEW,
    OCCUPIED_CAPITAL_STATUSES,
    USD_STABLE_ASSETS,
    UUID,
    AccountEquity,
    AccountEquityObservation,
    Any,
    Approval,
    Campaign,
    CampaignStatus,
    CapabilityGate,
    CapabilityStatus,
    CapitalAutomationPolicy,
    CapitalDirection,
    CapitalTransfer,
    CapitalTransferCommand,
    CapitalTransferStatus,
    CapitalTransferSubmission,
    Decimal,
    DirectCapitalConfiguration,
    DirectCapitalOperation,
    DirectCapitalPath,
    DirectCapitalPlan,
    DomainRejected,
    ExecutionEnvironment,
    FactStatus,
    NoTiltReceipt,
    NoTiltUnsignedTransaction,
    NoTiltVaultSnapshot,
    OrderIntent,
    Position,
    PrincipalType,
    ProposalStatus,
    ReconciliationRun,
    ReconciliationStatus,
    ReviewDecision,
    RiskPolicy,
    Role,
    RoleAssignment,
    ServiceMixinBase,
    Session,
    TransferAuthorization,
    TransferProposal,
    UsdValuation,
    User,
    VenueOrder,
    VenueOrderStatus,
    _advisory_lock_key,
    _as_uuid,
    _reject,
    _scope_key,
    _semantic_hash,
    datetime,
    evaluate_capital_automation,
    func,
    select,
    text,
    timedelta,
    uuid4,
)


class CapitalServiceMixin(ServiceMixinBase):
    """Capital configuration, treasury workflow, transfer, and balance transactions."""

    @staticmethod
    def _direct_capital_configuration_payload(
        config: DirectCapitalConfiguration,
        updater: User | None,
    ) -> dict[str, Any]:
        return {
            "config_id": str(config.config_id),
            "version": config.version,
            "network": config.network,
            "asset": config.asset,
            "treasury_provider": config.treasury_provider,
            "vault_id": config.vault_id,
            "vault_address": config.vault_address,
            "owned_arbitrum_address": config.owned_arbitrum_address,
            "binance_account_id": config.binance_account_id,
            "binance_deposit_address": config.binance_deposit_address,
            "binance_withdrawal_address": config.binance_withdrawal_address,
            "hyperliquid_account_id": config.hyperliquid_account_id,
            "hyperliquid_bridge_address": config.hyperliquid_bridge_address,
            "safe_address": config.safe_address,
            "safe_delegate_address": config.safe_delegate_address,
            "max_amount": None if config.max_amount is None else str(config.max_amount),
            "max_fee": None if config.max_fee is None else str(config.max_fee),
            "updated_by": str(config.updated_by),
            "updated_by_username": None if updater is None else updater.username,
            "effective_at": config.effective_at.isoformat(),
        }

    def direct_capital_configuration(self, actor_id: UUID) -> dict[str, Any] | None:
        with self.database.session_factory() as session:
            team = self._require_role(session, actor_id, "capital.view")
            config = session.scalar(
                select(DirectCapitalConfiguration).where(
                    DirectCapitalConfiguration.team_id == team.team_id,
                    DirectCapitalConfiguration.active,
                )
            )
            if config is None:
                return None
            return self._direct_capital_configuration_payload(
                config,
                session.get(User, config.updated_by),
            )

    def set_direct_capital_configuration(
        self,
        actor_id: UUID,
        idempotency_key: str,
        *,
        network: str,
        asset: str,
        treasury_provider: str,
        vault_id: str | None,
        vault_address: str | None,
        owned_arbitrum_address: str | None,
        binance_account_id: str | None,
        binance_deposit_address: str | None,
        binance_withdrawal_address: str | None,
        hyperliquid_account_id: str | None,
        hyperliquid_bridge_address: str | None,
        safe_address: str | None = None,
        safe_delegate_address: str | None = None,
        max_amount: Decimal | None,
        max_fee: Decimal | None,
        now: datetime,
    ) -> UUID:
        operation = "capital.configuration.manage"
        payload = {
            "network": network,
            "asset": asset,
            "treasury_provider": treasury_provider,
            "vault_id": vault_id,
            "vault_address": vault_address,
            "owned_arbitrum_address": owned_arbitrum_address,
            "binance_account_id": binance_account_id,
            "binance_deposit_address": binance_deposit_address,
            "binance_withdrawal_address": binance_withdrawal_address,
            "hyperliquid_account_id": hyperliquid_account_id,
            "hyperliquid_bridge_address": hyperliquid_bridge_address,
            "safe_address": safe_address,
            "safe_delegate_address": safe_delegate_address,
            "max_amount": None if max_amount is None else str(max_amount),
            "max_fee": None if max_fee is None else str(max_fee),
        }
        if network != "ARBITRUM" or asset != "USDC":
            _reject(
                "CAPITAL_CONFIGURATION_UNTRUSTED",
                "direct capital paths only support the trusted Arbitrum USDC catalog",
            )
        if treasury_provider not in {"NOTILT_VAULT", "SAFE_SPENDING_LIMIT"}:
            _reject("CAPITAL_CONFIGURATION_INVALID", "funding provider is unsupported")
        if treasury_provider == "NOTILT_VAULT":
            safe_address = None
            safe_delegate_address = None
        else:
            vault_id = None
            vault_address = None
        payload.update(
            {
                "vault_id": vault_id,
                "vault_address": vault_address,
                "safe_address": safe_address,
                "safe_delegate_address": safe_delegate_address,
            }
        )
        if max_amount is not None and max_amount <= 0:
            _reject("CAPITAL_CONFIGURATION_INVALID", "maximum amount must be positive")
        if max_fee is not None and max_fee < 0:
            _reject("CAPITAL_CONFIGURATION_INVALID", "maximum fee cannot be negative")
        if max_amount is not None and max_fee is not None and max_fee >= max_amount:
            _reject(
                "CAPITAL_CONFIGURATION_INVALID",
                "maximum fee must be lower than maximum amount",
            )
        selected_treasury_address = (
            vault_address if treasury_provider == "NOTILT_VAULT" else safe_address
        )
        if (
            selected_treasury_address is not None
            and binance_withdrawal_address is not None
            and selected_treasury_address.lower() != binance_withdrawal_address.lower()
        ):
            _reject(
                "CAPITAL_BINANCE_WITHDRAWAL_ADDRESS_SCOPE_MISMATCH",
                "Binance withdrawal must target the selected on-chain treasury",
            )
        with self.database.session_factory.begin() as session:
            team = self._require_role(session, actor_id, operation)
            assignments = session.scalars(
                select(RoleAssignment).where(
                    RoleAssignment.user_id == actor_id,
                    RoleAssignment.team_id == team.team_id,
                )
            ).all()
            if not any(item.role == Role.SYSTEM_ADMIN.value for item in assignments):
                _reject(
                    "CAPITAL_CONFIGURATION_ADMIN_REQUIRED",
                    "direct capital configuration requires SYSTEM_ADMIN",
                )
            for configured_account_id, configured_venue in (
                (binance_account_id, "BINANCE"),
                (hyperliquid_account_id, "HYPERLIQUID"),
            ):
                if configured_account_id is None:
                    continue
                self._ensure_exchange_account_reference(
                    session,
                    team=team,
                    actor_id=actor_id,
                    account_id=configured_account_id,
                    venue=configured_venue,
                    now=now,
                )
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return _as_uuid(str(response["config_id"]))
            current = session.scalar(
                select(DirectCapitalConfiguration)
                .where(
                    DirectCapitalConfiguration.team_id == team.team_id,
                    DirectCapitalConfiguration.active,
                )
                .with_for_update()
            )
            next_version = 1 if current is None else current.version + 1
            if current is not None:
                current.active = False
            config = DirectCapitalConfiguration(
                team_id=team.team_id,
                version=next_version,
                active=True,
                network=network,
                asset=asset,
                treasury_provider=treasury_provider,
                vault_id=vault_id,
                vault_address=vault_address,
                owned_arbitrum_address=owned_arbitrum_address,
                binance_account_id=binance_account_id,
                binance_deposit_address=binance_deposit_address,
                binance_withdrawal_address=binance_withdrawal_address,
                hyperliquid_account_id=hyperliquid_account_id,
                hyperliquid_bridge_address=hyperliquid_bridge_address,
                safe_address=safe_address,
                safe_delegate_address=safe_delegate_address,
                max_amount=max_amount,
                max_fee=max_fee,
                updated_by=actor_id,
                effective_at=now,
            )
            session.add(config)
            session.flush()
            result = {"config_id": str(config.config_id), "version": config.version}
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_DIRECT_CONFIGURATION_UPDATED",
                object_type="DirectCapitalConfiguration",
                object_id=config.config_id,
                reason=(
                    f"version={config.version}; network=ARBITRUM; asset=USDC; "
                    f"treasury_provider={treasury_provider}"
                ),
                correlation_id=uuid4(),
                object_version=config.version,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                now=now,
            )
            return config.config_id

    def create_direct_capital_operation(
        self,
        *,
        actor_id: UUID,
        plan: DirectCapitalPlan,
        final_confirmed: bool,
        idempotency_key: str,
        now: datetime,
    ) -> UUID:
        if not final_confirmed:
            _reject(
                "CAPITAL_FINAL_CONFIRMATION_REQUIRED",
                "direct capital operations require explicit final confirmation",
            )
        payload = {
            "path": plan.path.value,
            "treasury_provider": plan.treasury_provider.value,
            "venue": plan.venue,
            "account_id": plan.account_id,
            "vault_id": plan.vault_id,
            "asset": plan.asset,
            "network": plan.network,
            "amount": str(plan.amount),
            "max_fee": None if plan.max_fee is None else str(plan.max_fee),
            "min_received": None if plan.min_received is None else str(plan.min_received),
            "source_reference": plan.source_reference,
            "destination_reference": plan.destination_reference,
            "stages": list(plan.stages),
            "blockers": list(plan.blockers),
            "execute_after": (
                None if plan.execute_after is None else plan.execute_after.isoformat()
            ),
            "expires_at": plan.expires_at.isoformat(),
            "final_confirmed": True,
        }
        operation = "capital.direct.create"
        with self.database.session_factory.begin() as session:
            team = self._require_role(
                session,
                actor_id,
                "capital.execute",
                plan.account_id,
                plan.venue,
            )
            if plan.account_id is not None:
                self._ensure_exchange_account_reference(
                    session,
                    team=team,
                    actor_id=actor_id,
                    account_id=plan.account_id,
                    venue=plan.venue,
                    now=now,
                )
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return _as_uuid(str(response["operation_id"]))
            correlation_id = uuid4()
            direct_operation = DirectCapitalOperation(
                team_id=team.team_id,
                path=plan.path.value,
                treasury_provider=plan.treasury_provider.value,
                status=plan.status,
                receipt_status=plan.receipt_status,
                account_id=plan.account_id,
                venue=plan.venue,
                vault_id=plan.vault_id,
                asset=plan.asset,
                network=plan.network,
                amount=plan.amount,
                max_fee=plan.max_fee,
                min_received=plan.min_received,
                source_reference=plan.source_reference,
                destination_reference=plan.destination_reference,
                stages=list(plan.stages),
                blockers=list(plan.blockers),
                execute_after=plan.execute_after,
                expires_at=plan.expires_at,
                final_confirmed_at=now,
                actor_id=actor_id,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(direct_operation)
            session.flush()
            result = {"operation_id": str(direct_operation.operation_id)}
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_DIRECT_OPERATION_BLOCKED",
                object_type="DirectCapitalOperation",
                object_id=direct_operation.operation_id,
                reason=",".join(plan.blockers),
                correlation_id=correlation_id,
                object_version=1,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                account_id=plan.account_id,
                now=now,
            )
            return direct_operation.operation_id

    def direct_capital_operation_context(
        self,
        operation_id: UUID,
        actor_id: UUID,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        with self.database.session_factory() as session:
            item = session.get(DirectCapitalOperation, operation_id)
            if item is None:
                _reject("CAPITAL_DIRECT_OPERATION_NOT_FOUND", "direct capital operation is missing")
            self._require_role(
                session,
                actor_id,
                "capital.execute",
                item.account_id,
                item.venue,
                team_id=item.team_id,
            )
            if item.expires_at <= now:
                _reject("CAPITAL_DIRECT_OPERATION_EXPIRED", "direct capital operation expired")
            return {
                "operation_id": str(item.operation_id),
                "path": item.path,
                "treasury_provider": item.treasury_provider,
                "version": item.version,
                "status": item.status,
                "blockers": list(item.blockers),
                "account_id": item.account_id,
                "venue": item.venue,
                "vault_id": item.vault_id,
                "asset": item.asset,
                "network": item.network,
                "amount": str(item.amount),
                "max_fee": None if item.max_fee is None else str(item.max_fee),
                "min_received": (
                    str(item.amount) if item.min_received is None else str(item.min_received)
                ),
                "source_reference": item.source_reference,
                "destination_reference": item.destination_reference,
                "stages": list(item.stages),
                "created_at": item.created_at.isoformat(),
            }

    def record_direct_capital_unsigned_preview(
        self,
        operation_id: UUID,
        actor_id: UUID,
        *,
        expected_version: int,
        final_confirmed: bool,
        transactions: tuple[NoTiltUnsignedTransaction, ...],
        idempotency_key: str,
        now: datetime,
    ) -> int:
        if not final_confirmed:
            _reject(
                "CAPITAL_FINAL_CONFIRMATION_REQUIRED",
                "unsigned SDK preview requires explicit final confirmation",
            )
        if not transactions:
            _reject("NOTILT_PLAN_EMPTY", "NoTilt SDK returned no unsigned transactions")
        serialized = [item.to_dict() for item in transactions]
        operation = "capital.direct.notilt_unsigned_preview"
        with self.database.session_factory.begin() as session:
            item = session.get(DirectCapitalOperation, operation_id, with_for_update=True)
            if item is None:
                _reject("CAPITAL_DIRECT_OPERATION_NOT_FOUND", "direct capital operation is missing")
            self._require_role(
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
                "final_confirmed": True,
            }
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return int(response["version"])
            if item.version != expected_version:
                _reject("VERSION_CONFLICT", "direct capital operation changed; refresh first")
            if item.expires_at <= now:
                _reject("CAPITAL_DIRECT_OPERATION_EXPIRED", "direct capital operation expired")
            if item.path not in {
                DirectCapitalPath.VAULT_TO_BINANCE.value,
                DirectCapitalPath.VAULT_TO_HYPERLIQUID.value,
                DirectCapitalPath.BINANCE_TO_VAULT.value,
                DirectCapitalPath.HYPERLIQUID_TO_VAULT.value,
            }:
                _reject("CAPITAL_DIRECT_PATH_INVALID", "direct capital path is unsupported")
            allowed_functions = (
                {"requestWhitelistRelease"}
                if item.path
                in {
                    DirectCapitalPath.VAULT_TO_BINANCE.value,
                    DirectCapitalPath.VAULT_TO_HYPERLIQUID.value,
                }
                else {"approve", "deposit"}
            )
            if any(
                transaction.function_name not in allowed_functions for transaction in transactions
            ):
                _reject(
                    "NOTILT_PLAN_INVALID",
                    "NoTilt SDK preview contains a function outside the fixed path",
                )
            stage_code = (
                "NOTILT_UNSIGNED_RELEASE_REQUEST_PREVIEW"
                if "requestWhitelistRelease" in allowed_functions
                else "NOTILT_UNSIGNED_DEPOSIT_PREVIEW"
            )
            item.stages = [
                *item.stages,
                {
                    "code": stage_code,
                    "status": "READY_FOR_HUMAN_REVIEW",
                    "transactions": serialized,
                    "prepared_at": now.isoformat(),
                    "broadcast": False,
                },
            ]
            item.version += 1
            item.updated_at = now
            result = {"operation_id": str(operation_id), "version": item.version}
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
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
            _reject("CAPITAL_FINAL_CONFIRMATION_REQUIRED", "Safe preflight requires confirmation")
        artifact_kind = signature_request.get("kind")
        if artifact_kind not in {
            "SAFE_ALLOWANCE_SIGNATURE_REQUEST",
            "SAFE_ERC20_DEPOSIT_UNSIGNED_TRANSACTION",
        }:
            _reject("SAFE_PLAN_INVALID", "Safe preflight artifact is not a supported fixed request")
        if (
            signature_request.get("signing") is not False
            or signature_request.get("broadcast") is not False
        ):
            _reject(
                "SAFE_PLAN_INVALID", "Safe preflight must remain signing-free and non-broadcasting"
            )
        operation = "capital.direct.safe_spending_preview"
        with self.database.session_factory.begin() as session:
            item = session.get(DirectCapitalOperation, operation_id, with_for_update=True)
            if item is None:
                _reject("CAPITAL_DIRECT_OPERATION_NOT_FOUND", "direct capital operation is missing")
            self._require_role(
                session,
                actor_id,
                "capital.execute",
                item.account_id,
                item.venue,
                team_id=item.team_id,
            )
            if item.treasury_provider != "SAFE_SPENDING_LIMIT":
                _reject("SAFE_PLAN_SCOPE_MISMATCH", "operation did not select Safe Spending Limits")
            outbound = item.path in {
                DirectCapitalPath.VAULT_TO_BINANCE.value,
                DirectCapitalPath.VAULT_TO_HYPERLIQUID.value,
            }
            expected_kind = (
                "SAFE_ALLOWANCE_SIGNATURE_REQUEST"
                if outbound
                else "SAFE_ERC20_DEPOSIT_UNSIGNED_TRANSACTION"
            )
            if artifact_kind != expected_kind:
                _reject(
                    "SAFE_PLAN_DIRECTION_INVALID", "Safe artifact does not match path direction"
                )
            payload = {
                "operation_id": str(operation_id),
                "expected_version": expected_version,
                "signature_request": signature_request,
                "final_confirmed": True,
            }
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return int(response["version"])
            if item.version != expected_version:
                _reject("VERSION_CONFLICT", "direct capital operation changed; refresh first")
            if item.expires_at <= now:
                _reject("CAPITAL_DIRECT_OPERATION_EXPIRED", "direct capital operation expired")
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
            item.version += 1
            item.updated_at = now
            result = {"operation_id": str(operation_id), "version": item.version}
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
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
            _reject(
                "CAPITAL_FINAL_CONFIRMATION_REQUIRED",
                "Hyperliquid wallet handoff requires explicit confirmation",
            )
        kind = artifact.get("kind")
        if kind not in {
            "HYPERLIQUID_ARBITRUM_DEPOSIT_UNSIGNED_TRANSACTION",
            "HYPERLIQUID_WITHDRAW3_TYPED_REQUEST",
            "HYPERLIQUID_USD_CLASS_TRANSFER_TYPED_REQUEST",
        }:
            _reject(
                "HYPERLIQUID_CAPITAL_PLAN_INVALID",
                "Hyperliquid preflight returned an unsupported wallet request",
            )
        if artifact.get("signing") is not False or artifact.get("broadcast") is not False:
            _reject(
                "HYPERLIQUID_CAPITAL_PLAN_INVALID",
                "Hyperliquid capital preflight must remain unsigned and unbroadcast",
            )
        operation = "capital.direct.hyperliquid_preview"
        with self.database.session_factory.begin() as session:
            item = session.get(DirectCapitalOperation, operation_id, with_for_update=True)
            if item is None:
                _reject("CAPITAL_DIRECT_OPERATION_NOT_FOUND", "direct capital operation is missing")
            self._require_role(
                session,
                actor_id,
                "capital.execute",
                item.account_id,
                item.venue,
                team_id=item.team_id,
            )
            expected_kinds = {
                DirectCapitalPath.VAULT_TO_HYPERLIQUID.value: (
                    {"HYPERLIQUID_ARBITRUM_DEPOSIT_UNSIGNED_TRANSACTION"}
                ),
                DirectCapitalPath.HYPERLIQUID_TO_VAULT.value: {
                    "HYPERLIQUID_WITHDRAW3_TYPED_REQUEST",
                    "HYPERLIQUID_USD_CLASS_TRANSFER_TYPED_REQUEST",
                },
            }.get(item.path, set())
            if kind not in expected_kinds:
                _reject(
                    "HYPERLIQUID_CAPITAL_DIRECTION_INVALID",
                    "Hyperliquid wallet request does not match the frozen capital path",
                )
            payload = {
                "operation_id": str(operation_id),
                "expected_version": expected_version,
                "artifact": artifact,
                "final_confirmed": True,
            }
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return int(response["version"])
            if item.version != expected_version:
                _reject("VERSION_CONFLICT", "direct capital operation changed; refresh first")
            if item.expires_at <= now:
                _reject("CAPITAL_DIRECT_OPERATION_EXPIRED", "direct capital operation expired")
            stage_code = (
                "HYPERLIQUID_DEPOSIT_WALLET_REQUEST_READY"
                if kind == "HYPERLIQUID_ARBITRUM_DEPOSIT_UNSIGNED_TRANSACTION"
                else "HYPERLIQUID_CLASS_TRANSFER_WALLET_REQUEST_READY"
                if kind == "HYPERLIQUID_USD_CLASS_TRANSFER_TYPED_REQUEST"
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
                        if kind == "HYPERLIQUID_WITHDRAW3_TYPED_REQUEST"
                        else set()
                    ),
                }
            ]
            item.status = "UNSIGNED_PLAN_READY"
            item.version += 1
            item.updated_at = now
            result = {"operation_id": str(operation_id), "version": item.version}
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
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
            _reject(
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
            item = session.get(DirectCapitalOperation, operation_id, with_for_update=True)
            if item is None:
                _reject("CAPITAL_DIRECT_OPERATION_NOT_FOUND", "direct capital operation is missing")
            self._require_role(
                session,
                actor_id,
                "capital.execute",
                item.account_id,
                item.venue,
                team_id=item.team_id,
            )
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return int(response["version"])
            if item.version != expected_version:
                _reject("VERSION_CONFLICT", "direct capital operation changed; refresh first")
            if item.expires_at <= now:
                _reject("CAPITAL_DIRECT_OPERATION_EXPIRED", "direct capital operation expired")
            allowed_stages = {
                DirectCapitalPath.VAULT_TO_HYPERLIQUID.value: {"HYPERLIQUID_DEPOSIT"},
                DirectCapitalPath.HYPERLIQUID_TO_VAULT.value: {
                    "HYPERLIQUID_WITHDRAWAL",
                    "HYPERLIQUID_CLASS_TRANSFER",
                    "TREASURY_DEPOSIT",
                },
            }.get(item.path, set())
            if stage not in allowed_stages:
                _reject(
                    "CAPITAL_WALLET_STAGE_INVALID",
                    "wallet result does not match the frozen capital path",
                )
            preview = (
                next(
                    (
                        existing
                        for existing in reversed(item.stages)
                        if isinstance(existing, dict)
                        and str(existing.get("code", ""))
                        in {
                            "NOTILT_UNSIGNED_DEPOSIT_PREVIEW",
                            "SAFE_DEPOSIT_UNSIGNED_TRANSACTION_READY",
                        }
                    ),
                    None,
                )
                if stage == "TREASURY_DEPOSIT"
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
                _reject(
                    "HYPERLIQUID_CAPITAL_PREFLIGHT_REQUIRED",
                    "prepare a current unsigned wallet request before recording a wallet result",
                )
            if outcome == "SUBMITTED" and stage != "TREASURY_DEPOSIT":
                try:
                    preview_expires_at = datetime.fromisoformat(
                        str(preview["artifact"]["expiresAt"])
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise DomainRejected(
                        "HYPERLIQUID_CAPITAL_PLAN_INVALID",
                        "stored Hyperliquid preflight is invalid",
                    ) from exc
                if preview_expires_at <= now:
                    _reject(
                        "HYPERLIQUID_CAPITAL_PREFLIGHT_EXPIRED",
                        "wallet request expired; rebuild it from current facts before signing",
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
                item.blockers = [
                    blocker
                    for blocker in item.blockers
                    if blocker != "HUMAN_WALLET_CONFIRMATION_CANCELLED"
                ]
                event_type = "CAPITAL_HUMAN_WALLET_SUBMISSION_RECORDED"
            item.version += 1
            item.updated_at = now
            result = {"operation_id": str(operation_id), "version": item.version}
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
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
            item = session.get(DirectCapitalOperation, operation_id, with_for_update=True)
            if item is None:
                _reject("CAPITAL_DIRECT_OPERATION_NOT_FOUND", "direct capital operation is missing")
            self._require_role(
                session,
                actor_id,
                "capital.execute",
                item.account_id,
                item.venue,
                team_id=item.team_id,
            )
            if item.path != DirectCapitalPath.HYPERLIQUID_TO_VAULT.value:
                _reject(
                    "TREASURY_RECEIPT_STAGE_INVALID",
                    "treasury deposit receipt is only valid for Hyperliquid withdrawal paths",
                )
            payload = {
                "operation_id": str(operation_id),
                "expected_version": expected_version,
                "evidence": evidence,
            }
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return int(response["version"])
            if item.version != expected_version:
                _reject("VERSION_CONFLICT", "direct capital operation changed; refresh first")
            if item.expires_at <= now:
                _reject("CAPITAL_DIRECT_OPERATION_EXPIRED", "direct capital operation expired")
            submitted = next(
                (
                    stage
                    for stage in reversed(item.stages)
                    if stage.get("code") == "TREASURY_DEPOSIT_SUBMITTED_BY_HUMAN_WALLET"
                ),
                None,
            )
            if submitted is None:
                _reject(
                    "TREASURY_WALLET_SUBMISSION_REQUIRED",
                    "record the human wallet deposit transaction before receipt verification",
                )
            evidence_hash = str(
                evidence.get("transactionHash") or evidence.get("transaction_hash") or ""
            ).lower()
            if evidence_hash != str(submitted.get("transaction_hash", "")).lower():
                _reject(
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
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
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
            item = session.get(DirectCapitalOperation, operation_id, with_for_update=True)
            if item is None:
                _reject("CAPITAL_DIRECT_OPERATION_NOT_FOUND", "direct capital operation is missing")
            self._require_role(
                session,
                actor_id,
                "capital.execute",
                item.account_id,
                item.venue,
                team_id=item.team_id,
            )
            allowed = {
                DirectCapitalPath.VAULT_TO_HYPERLIQUID.value: {
                    "HYPERLIQUID_DEPOSIT_ARBITRUM",
                    "HYPERLIQUID_DEPOSIT_LEDGER",
                },
                DirectCapitalPath.HYPERLIQUID_TO_VAULT.value: {
                    "HYPERLIQUID_WITHDRAWAL_LEDGER",
                    "HYPERLIQUID_WITHDRAWAL_ARBITRUM",
                    "HYPERLIQUID_CLASS_TRANSFER_LEDGER",
                },
            }.get(item.path, set())
            if stage not in allowed:
                _reject(
                    "HYPERLIQUID_RECEIPT_STAGE_INVALID",
                    "receipt stage does not match the frozen capital path",
                )
            payload = {
                "operation_id": str(operation_id),
                "expected_version": expected_version,
                "stage": stage,
                "evidence": evidence,
            }
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return int(response["version"])
            if item.version != expected_version:
                _reject("VERSION_CONFLICT", "direct capital operation changed; refresh first")
            if item.expires_at <= now:
                _reject("CAPITAL_DIRECT_OPERATION_EXPIRED", "direct capital operation expired")
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
                elif (
                    item.path == DirectCapitalPath.HYPERLIQUID_TO_VAULT.value
                    and "TREASURY_DESTINATION_RECEIPT_CONFIRMED" in confirmed
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
                else:
                    item.blockers = list(
                        dict.fromkeys(
                            [
                                *item.blockers,
                                (
                                    "TREASURY_SOURCE_RECEIPT_REQUIRED"
                                    if item.path == DirectCapitalPath.VAULT_TO_HYPERLIQUID.value
                                    else "TREASURY_DESTINATION_RECEIPT_REQUIRED"
                                ),
                            ]
                        )
                    )
            item.updated_at = now
            result = {"operation_id": str(operation_id), "version": item.version}
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
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
            "BINANCE_ARBITRUM_DEPOSIT_PREFLIGHT": DirectCapitalPath.VAULT_TO_BINANCE.value,
            "BINANCE_RESTRICTED_WITHDRAWAL_PREFLIGHT": DirectCapitalPath.BINANCE_TO_VAULT.value,
        }.get(str(kind))
        if expected_path is None:
            _reject("BINANCE_CAPITAL_PREFLIGHT_INVALID", "unsupported Binance preflight artifact")
        operation = "capital.direct.binance_preview"
        with self.database.session_factory.begin() as session:
            item = session.get(DirectCapitalOperation, operation_id, with_for_update=True)
            if item is None:
                _reject("CAPITAL_DIRECT_OPERATION_NOT_FOUND", "direct capital operation is missing")
            self._require_role(
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
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return int(response["version"])
            if item.version != expected_version:
                _reject("VERSION_CONFLICT", "direct capital operation changed; refresh first")
            if item.path != expected_path:
                _reject(
                    "BINANCE_CAPITAL_DIRECTION_INVALID",
                    "Binance preflight does not match path",
                )
            if item.expires_at <= now:
                _reject("CAPITAL_DIRECT_OPERATION_EXPIRED", "direct capital operation expired")
            item.stages = [
                *item.stages,
                {
                    "code": (
                        "BINANCE_DEPOSIT_PREFLIGHT_READY"
                        if item.path == DirectCapitalPath.VAULT_TO_BINANCE.value
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
                    "BINANCE_CAPITAL_CREDENTIALS_MISSING",
                    "BINANCE_DEPOSIT_PREFLIGHT_REQUIRED",
                    "BINANCE_RESTRICTED_WITHDRAWAL_PREFLIGHT_REQUIRED",
                }
            ]
            item.version += 1
            item.updated_at = now
            result = {"operation_id": str(operation_id), "version": item.version}
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
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
            item = session.get(DirectCapitalOperation, operation_id, with_for_update=True)
            if item is None:
                _reject("CAPITAL_DIRECT_OPERATION_NOT_FOUND", "direct capital operation is missing")
            self._require_role(
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
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return int(response["version"])
            if item.version != expected_version:
                _reject("VERSION_CONFLICT", "direct capital operation changed; refresh first")
            if item.path != DirectCapitalPath.BINANCE_TO_VAULT.value:
                _reject(
                    "BINANCE_CAPITAL_DIRECTION_INVALID",
                    "submission is not a Binance withdrawal",
                )
            if not any(
                stage.get("code") == "BINANCE_RESTRICTED_WITHDRAWAL_PREFLIGHT_READY"
                for stage in item.stages
            ):
                _reject("BINANCE_CAPITAL_PREFLIGHT_REQUIRED", "current preflight is required")
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
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
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
            "BINANCE_DEPOSIT": DirectCapitalPath.VAULT_TO_BINANCE.value,
            "BINANCE_WITHDRAWAL": DirectCapitalPath.BINANCE_TO_VAULT.value,
        }.get(stage)
        if expected_path is None:
            _reject("BINANCE_CAPITAL_RECEIPT_STAGE_INVALID", "unknown Binance receipt stage")
        operation = "capital.direct.binance_receipt"
        with self.database.session_factory.begin() as session:
            item = session.get(DirectCapitalOperation, operation_id, with_for_update=True)
            if item is None:
                _reject("CAPITAL_DIRECT_OPERATION_NOT_FOUND", "direct capital operation is missing")
            self._require_role(
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
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return int(response["version"])
            if item.version != expected_version:
                _reject("VERSION_CONFLICT", "direct capital operation changed; refresh first")
            if item.path != expected_path:
                _reject("BINANCE_CAPITAL_RECEIPT_STAGE_INVALID", "receipt does not match path")
            if item.expires_at <= now:
                _reject("CAPITAL_DIRECT_OPERATION_EXPIRED", "direct capital operation expired")
            required_previous_stage = (
                "BINANCE_DEPOSIT_PREFLIGHT_READY"
                if stage == "BINANCE_DEPOSIT"
                else "BINANCE_RESTRICTED_WITHDRAWAL_SUBMITTED"
            )
            if not any(existing.get("code") == required_previous_stage for existing in item.stages):
                _reject(
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
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
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

    def record_capital_balance(
        self,
        *,
        actor_id: UUID,
        environment: ExecutionEnvironment,
        location_type: str,
        location_id: str,
        venue: str,
        equity: Decimal,
        available_balance: Decimal,
        withdrawable_balance: Decimal,
        asset: str,
        control_status: str,
        deposit_status: str,
        network: str | None,
        address_reference: str | None,
        known: bool,
        observed_at: datetime,
        now: datetime,
        valuation_currency: str | None = None,
        valuation_price: Decimal | None = None,
        valuation_equity: Decimal | None = None,
        valuation_observed_at: datetime | None = None,
    ) -> UUID:
        if location_type not in {"VAULT", "VENUE"}:
            _reject("CAPITAL_LOCATION_INVALID", "capital location must be VAULT or VENUE")
        if environment is ExecutionEnvironment.LIVE:
            _reject(
                "CAPITAL_LIVE_FACT_DISABLED",
                "LIVE capital facts require a configured read-only adapter",
            )
        if observed_at > now:
            _reject("FACT_TIME_INVALID", "capital observation cannot be in the future")
        if withdrawable_balance > available_balance or available_balance > equity:
            _reject(
                "CAPITAL_BALANCE_INVALID",
                "withdrawable, available, and equity balances are inconsistent",
            )
        if valuation_price is not None and valuation_price <= 0:
            _reject("CAPITAL_VALUATION_INVALID", "capital valuation price must be positive")
        if valuation_equity is not None and valuation_equity < 0:
            _reject("CAPITAL_VALUATION_INVALID", "capital valuation cannot be negative")
        if valuation_observed_at is not None and valuation_observed_at > now + MAX_FACT_CLOCK_SKEW:
            _reject("FACT_TIME_INVALID", "capital valuation cannot be in the future")
        if asset.upper() in USD_STABLE_ASSETS and valuation_equity is None:
            valuation_currency = "USD"
            valuation_price = Decimal(1)
            valuation_equity = equity
            valuation_observed_at = observed_at
        fact_venue = venue if location_type == "VENUE" else "VAULT"
        with self.database.session_factory.begin() as session:
            team = self._require_role(
                session,
                actor_id,
                "capital.fact.record",
                location_id if location_type == "VENUE" else None,
                venue if location_type == "VENUE" else None,
            )
            if location_type == "VENUE":
                self._ensure_exchange_account_reference(
                    session,
                    team=team,
                    actor_id=actor_id,
                    account_id=location_id,
                    venue=venue,
                    now=now,
                )
            fact = session.scalar(
                select(AccountEquity)
                .where(
                    AccountEquity.team_id == team.team_id,
                    AccountEquity.environment == environment.value,
                    AccountEquity.account_id == location_id,
                    AccountEquity.venue == fact_venue,
                    AccountEquity.currency == asset,
                )
                .with_for_update()
            )
            if fact is None:
                fact = AccountEquity(
                    team_id=team.team_id,
                    account_id=location_id,
                    venue=fact_venue,
                    environment=environment.value,
                    equity=equity,
                    available_balance=available_balance,
                    withdrawable_balance=withdrawable_balance,
                    currency=asset,
                    location_type=location_type,
                    control_status=control_status,
                    deposit_status=deposit_status,
                    network=network,
                    address_reference=address_reference,
                    valuation_currency=valuation_currency,
                    valuation_price=valuation_price,
                    valuation_equity=valuation_equity,
                    valuation_observed_at=valuation_observed_at,
                    fact_status=FactStatus.KNOWN.value if known else FactStatus.UNKNOWN.value,
                    observed_at=observed_at,
                    updated_at=now,
                )
                session.add(fact)
                session.flush()
            else:
                fact.equity = equity
                fact.available_balance = available_balance
                fact.withdrawable_balance = withdrawable_balance
                fact.currency = asset
                fact.location_type = location_type
                fact.control_status = control_status
                fact.deposit_status = deposit_status
                fact.network = network
                fact.address_reference = address_reference
                fact.valuation_currency = valuation_currency
                fact.valuation_price = valuation_price
                fact.valuation_equity = valuation_equity
                fact.valuation_observed_at = valuation_observed_at
                fact.fact_status = FactStatus.KNOWN.value if known else FactStatus.UNKNOWN.value
                fact.observed_at = observed_at
                fact.updated_at = now
            self._record_account_equity_observation(session, fact, recorded_at=now)
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_BALANCE_RECORDED",
                object_type="AccountEquity",
                object_id=fact.account_equity_id,
                reason=f"{location_type}:{fact.fact_status}",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return fact.account_equity_id

    def record_notilt_vault_snapshot(
        self,
        *,
        actor_id: UUID,
        snapshot: NoTiltVaultSnapshot,
        valuations: dict[str, UsdValuation],
        now: datetime,
    ) -> tuple[UUID, ...]:
        if not snapshot.budgets:
            _reject("NOTILT_FACT_INVALID", "NoTilt snapshot must contain catalog assets")
        with self.database.session_factory.begin() as session:
            team = self._require_role(session, actor_id, "capital.fact.record")
            fact_ids: list[UUID] = []
            for budget in snapshot.budgets:
                if (
                    not budget.is_official_vault
                    or budget.chain_id != snapshot.chain_id
                    or budget.vault.lower() != snapshot.vault.lower()
                    or budget.agent.lower() != snapshot.agent.lower()
                ):
                    _reject(
                        "NOTILT_VAULT_UNVERIFIED",
                        "NoTilt facts must belong to one official configured Vault",
                    )
                if budget.block_timestamp > now + MAX_FACT_CLOCK_SKEW:
                    _reject("FACT_TIME_INVALID", "NoTilt block time cannot be in the future")
                valuation = valuations.get(budget.asset)
                if valuation is None or valuation.observed_at > now + MAX_FACT_CLOCK_SKEW:
                    _reject(
                        "NOTILT_VALUATION_UNKNOWN",
                        "every NoTilt asset requires a current USD valuation",
                    )
                assigned = (
                    budget.is_active_whitelist
                    and budget.assigned_whitelist_vault.lower() == snapshot.vault.lower()
                )
                controlled = assigned and not budget.panic_locked
                withdrawable = (
                    min(budget.balance, budget.max_release_net) if controlled else Decimal(0)
                )
                fact = session.scalar(
                    select(AccountEquity)
                    .where(
                        AccountEquity.team_id == team.team_id,
                        AccountEquity.environment == ExecutionEnvironment.LIVE.value,
                        AccountEquity.account_id == snapshot.vault,
                        AccountEquity.venue == "VAULT",
                        AccountEquity.currency == budget.asset,
                    )
                    .with_for_update()
                )
                if fact is None:
                    fact = AccountEquity(
                        team_id=team.team_id,
                        account_id=snapshot.vault,
                        venue="VAULT",
                        environment=ExecutionEnvironment.LIVE.value,
                        equity=budget.balance,
                        available_balance=budget.balance,
                        withdrawable_balance=withdrawable,
                        currency=budget.asset,
                        location_type="VAULT",
                        control_status="CONTROLLED" if controlled else "READ_ONLY",
                        deposit_status="READY",
                        network=snapshot.chain,
                        address_reference=snapshot.vault,
                        valuation_currency="USD",
                        valuation_price=valuation.price,
                        valuation_equity=valuation.value,
                        valuation_observed_at=valuation.observed_at,
                        fact_status=FactStatus.KNOWN.value,
                        observed_at=budget.block_timestamp,
                        updated_at=now,
                    )
                    session.add(fact)
                    session.flush()
                else:
                    fact.equity = budget.balance
                    fact.available_balance = budget.balance
                    fact.withdrawable_balance = withdrawable
                    fact.location_type = "VAULT"
                    fact.control_status = "CONTROLLED" if controlled else "READ_ONLY"
                    fact.deposit_status = "READY"
                    fact.network = snapshot.chain
                    fact.address_reference = snapshot.vault
                    fact.valuation_currency = "USD"
                    fact.valuation_price = valuation.price
                    fact.valuation_equity = valuation.value
                    fact.valuation_observed_at = valuation.observed_at
                    fact.fact_status = FactStatus.KNOWN.value
                    fact.observed_at = budget.block_timestamp
                    fact.updated_at = now
                self._record_account_equity_observation(session, fact, recorded_at=now)
                self._audit(
                    session,
                    actor_id=str(actor_id),
                    event_type="NOTILT_VAULT_FACT_RECORDED",
                    object_type="AccountEquity",
                    object_id=fact.account_equity_id,
                    reason=(
                        f"{snapshot.chain}:{budget.asset}:"
                        f"{'CONTROLLED' if controlled else 'READ_ONLY'}"
                    ),
                    correlation_id=uuid4(),
                    object_version=1,
                    now=now,
                )
                fact_ids.append(fact.account_equity_id)
        return tuple(fact_ids)

    def record_safe_spending_snapshot(
        self,
        *,
        actor_id: UUID,
        safe_address: str,
        asset: str,
        balance: Decimal,
        available_limit: Decimal,
        module_enabled: bool,
        observed_at: datetime,
        now: datetime,
    ) -> UUID:
        """Persist one read-only Safe balance as the selected on-chain treasury fact."""
        if balance < 0 or available_limit < 0:
            _reject("SAFE_FACT_INVALID", "Safe balance and spending limit cannot be negative")
        if observed_at > now + MAX_FACT_CLOCK_SKEW:
            _reject("FACT_TIME_INVALID", "Safe block time cannot be in the future")
        normalized_asset = asset.upper()
        if normalized_asset not in USD_STABLE_ASSETS:
            _reject("SAFE_ASSET_UNSUPPORTED", "Safe treasury snapshot requires a USD asset")
        with self.database.session_factory.begin() as session:
            team = self._require_role(session, actor_id, "capital.fact.record")
            fact = session.scalar(
                select(AccountEquity)
                .where(
                    AccountEquity.team_id == team.team_id,
                    AccountEquity.environment == ExecutionEnvironment.LIVE.value,
                    AccountEquity.account_id == safe_address,
                    AccountEquity.venue == "VAULT",
                    AccountEquity.currency == normalized_asset,
                )
                .with_for_update()
            )
            withdrawable = min(balance, available_limit) if module_enabled else Decimal(0)
            if fact is None:
                fact = AccountEquity(
                    team_id=team.team_id,
                    account_id=safe_address,
                    venue="VAULT",
                    environment=ExecutionEnvironment.LIVE.value,
                    equity=balance,
                    available_balance=balance,
                    withdrawable_balance=withdrawable,
                    currency=normalized_asset,
                    location_type="VAULT",
                    control_status="READ_ONLY",
                    deposit_status="READY",
                    network="ARBITRUM",
                    address_reference=safe_address,
                    valuation_currency="USD",
                    valuation_price=Decimal(1),
                    valuation_equity=balance,
                    valuation_observed_at=observed_at,
                    fact_status=FactStatus.KNOWN.value,
                    observed_at=observed_at,
                    updated_at=now,
                )
                session.add(fact)
                session.flush()
            else:
                fact.equity = balance
                fact.available_balance = balance
                fact.withdrawable_balance = withdrawable
                fact.location_type = "VAULT"
                fact.control_status = "READ_ONLY"
                fact.deposit_status = "READY"
                fact.network = "ARBITRUM"
                fact.address_reference = safe_address
                fact.valuation_currency = "USD"
                fact.valuation_price = Decimal(1)
                fact.valuation_equity = balance
                fact.valuation_observed_at = observed_at
                fact.fact_status = FactStatus.KNOWN.value
                fact.observed_at = observed_at
                fact.updated_at = now
            observation_exists = session.scalar(
                select(AccountEquityObservation.observation_id).where(
                    AccountEquityObservation.team_id == team.team_id,
                    AccountEquityObservation.account_equity_id == fact.account_equity_id,
                    AccountEquityObservation.observed_at == observed_at,
                )
            )
            self._record_account_equity_observation(session, fact, recorded_at=now)
            if observation_exists is None:
                self._audit(
                    session,
                    actor_id=str(actor_id),
                    event_type="CAPITAL_SAFE_BALANCE_RECORDED",
                    object_type="AccountEquity",
                    object_id=fact.account_equity_id,
                    reason=(
                        "read-only Safe Spending Limits treasury snapshot; "
                        f"module_enabled={str(module_enabled).lower()}; "
                        "signing=false; broadcast=false"
                    ),
                    correlation_id=uuid4(),
                    object_version=1,
                    now=now,
                )
            return fact.account_equity_id

    def set_capital_automation_policy(
        self,
        *,
        actor_id: UUID,
        environment: ExecutionEnvironment,
        account_id: str,
        venue: str,
        vault_id: str,
        asset: str,
        network: str,
        vault_destination_reference: str,
        venue_destination_reference: str,
        operating_low: Decimal,
        operating_target: Decimal,
        operating_high: Decimal,
        vault_minimum_reserve: Decimal,
        minimum_transfer: Decimal,
        maximum_transfer: Decimal,
        max_fee: Decimal,
        idempotency_key: str,
        now: datetime,
    ) -> UUID:
        if environment is ExecutionEnvironment.LIVE:
            _reject(
                "CAPITAL_AUTOMATION_LIVE_DISABLED",
                "LIVE automation requires approved external capital parameters",
            )
        evaluate_capital_automation(
            purpose="AUTO_PROFIT_SWEEP",
            venue_available=Decimal(0),
            venue_withdrawable=Decimal(0),
            vault_available=Decimal(0),
            confirmed_realized_pnl=Decimal(0),
            operating_low=operating_low,
            operating_target=operating_target,
            operating_high=operating_high,
            vault_minimum_reserve=vault_minimum_reserve,
            minimum_transfer=minimum_transfer,
            maximum_transfer=maximum_transfer,
            max_fee=max_fee,
        )
        payload = {
            "environment": environment.value,
            "account_id": account_id,
            "venue": venue,
            "vault_id": vault_id,
            "asset": asset,
            "network": network,
            "vault_destination_reference": vault_destination_reference,
            "venue_destination_reference": venue_destination_reference,
            "operating_low": str(operating_low),
            "operating_target": str(operating_target),
            "operating_high": str(operating_high),
            "vault_minimum_reserve": str(vault_minimum_reserve),
            "minimum_transfer": str(minimum_transfer),
            "maximum_transfer": str(maximum_transfer),
            "max_fee": str(max_fee),
        }
        operation = "capital.policy.manage"
        with self.database.session_factory.begin() as session:
            team = self._require_role(session, actor_id, operation, account_id, venue)
            self._ensure_exchange_account_reference(
                session,
                team=team,
                actor_id=actor_id,
                account_id=account_id,
                venue=venue,
                now=now,
            )
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return _as_uuid(str(response["policy_id"]))
            policy = session.scalar(
                select(CapitalAutomationPolicy)
                .where(
                    CapitalAutomationPolicy.team_id == team.team_id,
                    CapitalAutomationPolicy.environment == environment.value,
                    CapitalAutomationPolicy.account_id == account_id,
                    CapitalAutomationPolicy.venue == venue,
                    CapitalAutomationPolicy.asset == asset,
                )
                .with_for_update()
            )
            if policy is None:
                policy = CapitalAutomationPolicy(
                    team_id=team.team_id,
                    environment=environment.value,
                    account_id=account_id,
                    venue=venue,
                    vault_id=vault_id,
                    asset=asset,
                    network=network,
                    vault_destination_reference=vault_destination_reference,
                    venue_destination_reference=venue_destination_reference,
                    operating_low=operating_low,
                    operating_target=operating_target,
                    operating_high=operating_high,
                    vault_minimum_reserve=vault_minimum_reserve,
                    minimum_transfer=minimum_transfer,
                    maximum_transfer=maximum_transfer,
                    max_fee=max_fee,
                    active=True,
                    actor_id=str(actor_id),
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(policy)
                session.flush()
            else:
                policy.vault_id = vault_id
                policy.network = network
                policy.vault_destination_reference = vault_destination_reference
                policy.venue_destination_reference = venue_destination_reference
                policy.operating_low = operating_low
                policy.operating_target = operating_target
                policy.operating_high = operating_high
                policy.vault_minimum_reserve = vault_minimum_reserve
                policy.minimum_transfer = minimum_transfer
                policy.maximum_transfer = maximum_transfer
                policy.max_fee = max_fee
                policy.active = True
                policy.actor_id = str(actor_id)
                policy.version += 1
                policy.updated_at = now
            result = {"policy_id": str(policy.policy_id)}
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_AUTOMATION_POLICY_SET",
                object_type="CapitalAutomationPolicy",
                object_id=policy.policy_id,
                reason="SHADOW/TESTNET thresholds frozen; both automation gates remain independent",
                correlation_id=uuid4(),
                object_version=policy.version,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                account_id=policy.account_id,
                now=now,
            )
            return policy.policy_id

    def create_capital_automation_candidate(
        self,
        policy_id: UUID,
        purpose: str,
        actor_id: UUID,
        idempotency_key: str,
        *,
        now: datetime,
    ) -> tuple[UUID | None, str]:
        if purpose not in {"AUTO_PROFIT_SWEEP", "AUTO_OPERATING_REFILL"}:
            _reject("CAPITAL_AUTOMATION_PURPOSE_INVALID", "unknown capital automation")
        operation = "capital.automation.evaluate"
        payload = {"policy_id": str(policy_id), "purpose": purpose}
        with self.database.session_factory.begin() as session:
            policy = session.get(CapitalAutomationPolicy, policy_id, with_for_update=True)
            if policy is None:
                _reject("CAPITAL_AUTOMATION_POLICY_NOT_FOUND", "capital policy is missing")
            team = self._require_role(
                session,
                actor_id,
                operation,
                policy.account_id,
                policy.venue,
                team_id=policy.team_id,
            )
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                proposal_id = response.get("transfer_proposal_id")
                return (
                    None if proposal_id is None else _as_uuid(str(proposal_id)),
                    str(response["reason"]),
                )
            gate = session.get(CapabilityGate, purpose)
            if gate is None or gate.status != CapabilityStatus.ENABLED.value:
                _reject("CAPITAL_AUTOMATION_DISABLED", f"{purpose} is disabled")
            if not policy.active:
                _reject("CAPITAL_AUTOMATION_POLICY_INACTIVE", "capital policy is inactive")
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {
                    "key": _advisory_lock_key(
                        str(team.team_id),
                        "capital-automation",
                        f"{policy.environment}:{policy.account_id}:{policy.venue}:{policy.asset}",
                    )
                },
            )
            self._assert_capital_scope_flat(
                session,
                team_id=team.team_id,
                environment=policy.environment,
                account_id=policy.account_id,
                venue=policy.venue,
                now=now,
            )
            risk_policy = session.scalar(
                select(RiskPolicy).where(
                    RiskPolicy.team_id == team.team_id,
                    RiskPolicy.active,
                )
            )
            if risk_policy is None:
                _reject("RISK_POLICY_MISSING", "capital automation requires an active risk policy")
            latest_match = session.scalar(
                select(ReconciliationRun)
                .where(
                    ReconciliationRun.team_id == team.team_id,
                    ReconciliationRun.execution_scope
                    == _scope_key(policy.environment, policy.account_id, policy.venue),
                )
                .order_by(ReconciliationRun.completed_at.desc())
                .limit(1)
            )
            if (
                latest_match is None
                or latest_match.status != ReconciliationStatus.MATCH.value
                or not latest_match.is_computed
                or latest_match.completed_at
                < now - timedelta(seconds=risk_policy.max_fact_age_seconds)
            ):
                _reject(
                    "CAPITAL_AUTOMATION_RECONCILIATION_REQUIRED",
                    "fresh computed MATCH is required",
                )
            campaigns = session.scalars(
                select(Campaign).where(
                    Campaign.team_id == team.team_id,
                    Campaign.environment == policy.environment,
                    Campaign.account_id == policy.account_id,
                    Campaign.venue == policy.venue,
                )
            ).all()
            if any(item.status != CampaignStatus.CLOSED.value for item in campaigns):
                _reject(
                    "CAPITAL_AUTOMATION_ACTIVE_CYCLE",
                    "automation only prepares the next flat trading cycle",
                )
            active_transfer = session.scalar(
                select(CapitalTransfer.capital_transfer_id)
                .where(
                    CapitalTransfer.team_id == team.team_id,
                    CapitalTransfer.environment == policy.environment,
                    CapitalTransfer.account_id == policy.account_id,
                    CapitalTransfer.venue == policy.venue,
                    CapitalTransfer.status.in_(OCCUPIED_CAPITAL_STATUSES),
                )
                .limit(1)
            )
            active_proposal = session.scalar(
                select(TransferProposal.transfer_proposal_id)
                .where(
                    TransferProposal.team_id == team.team_id,
                    TransferProposal.environment == policy.environment,
                    TransferProposal.account_id == policy.account_id,
                    TransferProposal.venue == policy.venue,
                    TransferProposal.purpose.in_({"AUTO_PROFIT_SWEEP", "AUTO_OPERATING_REFILL"}),
                    TransferProposal.status.in_(
                        {
                            ProposalStatus.DRAFT.value,
                            ProposalStatus.PENDING_REVIEW.value,
                            ProposalStatus.APPROVED.value,
                        }
                    ),
                )
                .limit(1)
            )
            if active_transfer is not None or active_proposal is not None:
                _reject(
                    "CAPITAL_AUTOMATION_ALREADY_PENDING",
                    "another capital operation owns this scope",
                )
            venue_fact = self._capital_balance(
                session,
                team_id=team.team_id,
                environment=policy.environment,
                endpoint_type="VENUE",
                endpoint_id=policy.account_id,
                venue=policy.venue,
                asset=policy.asset,
                lock=True,
            )
            vault_fact = self._capital_balance(
                session,
                team_id=team.team_id,
                environment=policy.environment,
                endpoint_type="VAULT",
                endpoint_id=policy.vault_id,
                venue=policy.venue,
                asset=policy.asset,
                lock=True,
            )
            if (
                venue_fact.observed_at < now - timedelta(seconds=risk_policy.max_fact_age_seconds)
                or vault_fact.observed_at
                < now - timedelta(seconds=risk_policy.max_fact_age_seconds)
                or venue_fact.deposit_status != "READY"
                or vault_fact.control_status != "CONTROLLED"
            ):
                _reject("CAPITAL_FACT_UNKNOWN", "fresh controlled capital facts are required")
            realized_pnl = sum((item.final_pnl for item in campaigns), Decimal(0))
            already_swept = session.scalar(
                select(func.coalesce(func.sum(CapitalTransfer.gross_amount), 0))
                .join(
                    TransferAuthorization,
                    TransferAuthorization.transfer_authorization_id
                    == CapitalTransfer.transfer_authorization_id,
                )
                .where(
                    CapitalTransfer.team_id == team.team_id,
                    TransferAuthorization.team_id == team.team_id,
                    CapitalTransfer.environment == policy.environment,
                    CapitalTransfer.account_id == policy.account_id,
                    CapitalTransfer.venue == policy.venue,
                    TransferAuthorization.purpose == "AUTO_PROFIT_SWEEP",
                    CapitalTransfer.status != CapitalTransferStatus.FAILED_SOURCE_RESTORED.value,
                )
            )
            confirmed_profit = max(Decimal(0), realized_pnl - Decimal(already_swept or 0))
            decision = evaluate_capital_automation(
                purpose=purpose,
                venue_available=venue_fact.available_balance,
                venue_withdrawable=(
                    venue_fact.available_balance
                    if venue_fact.withdrawable_balance is None
                    else venue_fact.withdrawable_balance
                ),
                vault_available=(
                    vault_fact.available_balance
                    if vault_fact.withdrawable_balance is None
                    else vault_fact.withdrawable_balance
                ),
                confirmed_realized_pnl=(
                    realized_pnl if purpose == "AUTO_OPERATING_REFILL" else confirmed_profit
                ),
                operating_low=policy.operating_low,
                operating_target=policy.operating_target,
                operating_high=policy.operating_high,
                vault_minimum_reserve=policy.vault_minimum_reserve,
                minimum_transfer=policy.minimum_transfer,
                maximum_transfer=policy.maximum_transfer,
                max_fee=policy.max_fee,
            )
            if decision.amount is None:
                result: dict[str, Any] = {
                    "transfer_proposal_id": None,
                    "reason": decision.reason,
                }
                event_type = "CAPITAL_AUTOMATION_NO_ACTION"
                object_id: UUID | str = policy.policy_id
                object_version = policy.version
            else:
                direction = (
                    CapitalDirection.VENUE_TO_VAULT
                    if purpose == "AUTO_PROFIT_SWEEP"
                    else CapitalDirection.VAULT_TO_VENUE
                )
                source_type, source_id, destination_type, destination_id = (
                    ("VENUE", policy.account_id, "VAULT", policy.vault_id)
                    if direction is CapitalDirection.VENUE_TO_VAULT
                    else ("VAULT", policy.vault_id, "VENUE", policy.account_id)
                )
                destination_reference = (
                    policy.vault_destination_reference
                    if direction is CapitalDirection.VENUE_TO_VAULT
                    else policy.venue_destination_reference
                )
                frozen_payload = {
                    **payload,
                    "policy_version": policy.version,
                    "environment": policy.environment,
                    "direction": direction.value,
                    "account_id": policy.account_id,
                    "venue": policy.venue,
                    "vault_id": policy.vault_id,
                    "asset": policy.asset,
                    "network": policy.network,
                    "amount": str(decision.amount),
                    "max_fee": str(policy.max_fee),
                    "min_received": str(decision.amount - policy.max_fee),
                    "confirmed_realized_pnl": str(realized_pnl),
                    "remaining_sweepable_profit": str(confirmed_profit),
                    "venue_fact_id": str(venue_fact.account_equity_id),
                    "venue_observed_at": venue_fact.observed_at.isoformat(),
                    "vault_fact_id": str(vault_fact.account_equity_id),
                    "vault_observed_at": vault_fact.observed_at.isoformat(),
                    "reconciliation_id": str(latest_match.reconciliation_id),
                }
                proposal = TransferProposal(
                    team_id=team.team_id,
                    proposer_id=actor_id,
                    environment=policy.environment,
                    direction=direction.value,
                    purpose=purpose,
                    status=ProposalStatus.PENDING_REVIEW.value,
                    version=1,
                    account_id=policy.account_id,
                    venue=policy.venue,
                    source_type=source_type,
                    source_id=source_id,
                    destination_type=destination_type,
                    destination_id=destination_id,
                    asset=policy.asset,
                    network=policy.network,
                    destination_reference=destination_reference,
                    amount=decision.amount,
                    max_fee=policy.max_fee,
                    min_received=decision.amount - policy.max_fee,
                    reason=(
                        "confirmed realized profit above operating high"
                        if purpose == "AUTO_PROFIT_SWEEP"
                        else "flat next-cycle operating balance below low"
                    ),
                    frozen_payload=frozen_payload,
                    semantic_hash=_semantic_hash(frozen_payload),
                    frozen_at=now,
                    expires_at=now + timedelta(hours=2),
                    correlation_id=uuid4(),
                    created_at=now,
                    updated_at=now,
                )
                session.add(proposal)
                session.flush()
                result = {
                    "transfer_proposal_id": str(proposal.transfer_proposal_id),
                    "reason": decision.reason,
                }
                event_type = "CAPITAL_AUTOMATION_CANDIDATE_CREATED"
                object_id = proposal.transfer_proposal_id
                object_version = proposal.version
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type=event_type,
                object_type=(
                    "TransferProposal"
                    if result["transfer_proposal_id"] is not None
                    else "CapitalAutomationPolicy"
                ),
                object_id=object_id,
                reason=f"{purpose}:{decision.reason}; no automatic transfer submission",
                correlation_id=uuid4(),
                object_version=object_version,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                account_id=policy.account_id,
                now=now,
            )
            return (
                None
                if result["transfer_proposal_id"] is None
                else _as_uuid(str(result["transfer_proposal_id"])),
                decision.reason,
            )

    def create_transfer_proposal(
        self,
        *,
        actor_id: UUID,
        environment: ExecutionEnvironment,
        direction: CapitalDirection,
        account_id: str,
        venue: str,
        vault_id: str,
        asset: str,
        network: str,
        destination_reference: str,
        amount: Decimal,
        max_fee: Decimal,
        min_received: Decimal,
        reason: str,
        expires_at: datetime,
        idempotency_key: str,
        now: datetime,
        allow_live_unsigned: bool = False,
    ) -> UUID:
        if environment is ExecutionEnvironment.LIVE and not allow_live_unsigned:
            _reject(
                "CAPITAL_TRANSFER_LIVE_DISABLED",
                "LIVE capital proposals require the constrained unsigned transaction workflow",
            )
        if expires_at <= now:
            _reject("TRANSFER_PROPOSAL_EXPIRY_INVALID", "transfer proposal must expire later")
        source_type, source_id, destination_type, destination_id = (
            ("VAULT", vault_id, "VENUE", account_id)
            if direction is CapitalDirection.VAULT_TO_VENUE
            else ("VENUE", account_id, "VAULT", vault_id)
        )
        payload = {
            "environment": environment.value,
            "direction": direction.value,
            "purpose": "MANUAL_TRANSFER",
            "account_id": account_id,
            "venue": venue,
            "source_type": source_type,
            "source_id": source_id,
            "destination_type": destination_type,
            "destination_id": destination_id,
            "asset": asset,
            "network": network,
            "destination_reference": destination_reference,
            "amount": str(amount),
            "max_fee": str(max_fee),
            "min_received": str(min_received),
            "reason": reason,
            "expires_at": expires_at.isoformat(),
        }
        operation = "capital.propose"
        with self.database.session_factory.begin() as session:
            team = self._require_role(session, actor_id, operation, account_id, venue)
            self._ensure_exchange_account_reference(
                session,
                team=team,
                actor_id=actor_id,
                account_id=account_id,
                venue=venue,
                now=now,
            )
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return _as_uuid(str(response["transfer_proposal_id"]))
            proposal = TransferProposal(
                team_id=team.team_id,
                proposer_id=actor_id,
                environment=environment.value,
                direction=direction.value,
                purpose="MANUAL_TRANSFER",
                status=ProposalStatus.DRAFT.value,
                version=1,
                account_id=account_id,
                venue=venue,
                source_type=source_type,
                source_id=source_id,
                destination_type=destination_type,
                destination_id=destination_id,
                asset=asset,
                network=network,
                destination_reference=destination_reference,
                amount=amount,
                max_fee=max_fee,
                min_received=min_received,
                reason=reason,
                frozen_payload=payload,
                semantic_hash=digest,
                frozen_at=None,
                expires_at=expires_at,
                correlation_id=uuid4(),
                created_at=now,
                updated_at=now,
            )
            session.add(proposal)
            session.flush()
            result = {"transfer_proposal_id": str(proposal.transfer_proposal_id)}
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="TRANSFER_PROPOSAL_CREATED",
                object_type="TransferProposal",
                object_id=proposal.transfer_proposal_id,
                reason=direction.value,
                correlation_id=proposal.correlation_id,
                object_version=proposal.version,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                account_id=proposal.account_id,
                now=now,
            )
            return proposal.transfer_proposal_id

    def submit_transfer_proposal(
        self, transfer_proposal_id: UUID, actor_id: UUID, *, now: datetime
    ) -> None:
        with self.database.session_factory.begin() as session:
            proposal = session.get(TransferProposal, transfer_proposal_id, with_for_update=True)
            if proposal is None:
                _reject("TRANSFER_PROPOSAL_NOT_FOUND", "transfer proposal does not exist")
            team = self._require_role(
                session,
                actor_id,
                "capital.submit",
                proposal.account_id,
                proposal.venue,
                team_id=proposal.team_id,
            )
            if proposal.proposer_id != actor_id:
                _reject("TRANSFER_PROPOSAL_OWNER_REQUIRED", "only the proposer may submit")
            if proposal.expires_at <= now:
                proposal.status = ProposalStatus.EXPIRED.value
                proposal.version += 1
                proposal.updated_at = now
                _reject("TRANSFER_PROPOSAL_EXPIRED", "transfer proposal expired")
            if proposal.status != ProposalStatus.DRAFT.value:
                _reject("TRANSFER_PROPOSAL_NOT_DRAFT", "only a draft may be submitted")
            proposal.status = ProposalStatus.PENDING_REVIEW.value
            proposal.frozen_at = now
            proposal.version += 1
            proposal.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="TRANSFER_PROPOSAL_SUBMITTED",
                object_type="TransferProposal",
                object_id=proposal.transfer_proposal_id,
                reason="frozen for two independent Treasury reviewers",
                correlation_id=proposal.correlation_id,
                object_version=proposal.version,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                account_id=proposal.account_id,
                now=now,
            )

    def review_transfer_proposal(
        self,
        transfer_proposal_id: UUID,
        reviewer_id: UUID,
        decision: ReviewDecision,
        reason: str,
        expected_version: int,
        *,
        now: datetime,
    ) -> ProposalStatus:
        with self.database.session_factory.begin() as session:
            proposal = session.get(TransferProposal, transfer_proposal_id, with_for_update=True)
            if proposal is None:
                _reject("TRANSFER_PROPOSAL_NOT_FOUND", "transfer proposal does not exist")
            if proposal.version != expected_version:
                _reject("VERSION_CONFLICT", "transfer proposal changed before review")
            if proposal.proposer_id == reviewer_id:
                _reject("SELF_REVIEW_FORBIDDEN", "a transfer proposer cannot review it")
            team = self._require_role(
                session,
                reviewer_id,
                "capital.review",
                proposal.account_id,
                proposal.venue,
                team_id=proposal.team_id,
            )
            reviewer = session.get(User, reviewer_id)
            if reviewer is None or reviewer.principal_type != PrincipalType.HUMAN.value:
                _reject("SERVICE_REVIEW_FORBIDDEN", "capital review requires a human")
            if proposal.expires_at <= now:
                proposal.status = ProposalStatus.EXPIRED.value
                proposal.version += 1
                proposal.updated_at = now
                _reject("TRANSFER_PROPOSAL_EXPIRED", "transfer proposal expired")
            if proposal.status != ProposalStatus.PENDING_REVIEW.value:
                _reject("TRANSFER_PROPOSAL_NOT_REVIEWABLE", "transfer proposal is not pending")
            duplicate = session.scalar(
                select(Approval).where(
                    Approval.transfer_proposal_id == transfer_proposal_id,
                    Approval.reviewer_id == reviewer_id,
                )
            )
            if duplicate is not None:
                _reject("REVIEW_ALREADY_RECORDED", "reviewer already decided this transfer")
            session.add(
                Approval(
                    proposal_id=None,
                    transfer_proposal_id=transfer_proposal_id,
                    reviewer_id=reviewer_id,
                    decision=decision.value,
                    reason=reason,
                    created_at=now,
                )
            )
            session.flush()
            if decision is ReviewDecision.REJECT:
                proposal.status = ProposalStatus.REJECTED.value
            else:
                approvals = session.scalar(
                    select(func.count())
                    .select_from(Approval)
                    .where(
                        Approval.transfer_proposal_id == transfer_proposal_id,
                        Approval.decision == ReviewDecision.APPROVE.value,
                    )
                )
                if int(approvals or 0) >= 2:
                    proposal.status = ProposalStatus.APPROVED.value
            proposal.version += 1
            proposal.updated_at = now
            self._audit(
                session,
                actor_id=str(reviewer_id),
                event_type="TRANSFER_PROPOSAL_REVIEWED",
                object_type="TransferProposal",
                object_id=proposal.transfer_proposal_id,
                reason=f"{decision.value}: {reason}",
                correlation_id=proposal.correlation_id,
                object_version=proposal.version,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                account_id=proposal.account_id,
                now=now,
            )
            return ProposalStatus(proposal.status)

    def issue_transfer_authorization(
        self,
        transfer_proposal_id: UUID,
        actor_id: UUID,
        expires_at: datetime,
        idempotency_key: str,
        *,
        now: datetime,
    ) -> UUID:
        payload = {
            "transfer_proposal_id": str(transfer_proposal_id),
            "expires_at": expires_at.isoformat(),
        }
        operation = "capital.authorize"
        with self.database.session_factory.begin() as session:
            proposal = session.get(TransferProposal, transfer_proposal_id)
            if proposal is None:
                _reject("TRANSFER_PROPOSAL_NOT_FOUND", "transfer proposal does not exist")
            team = self._require_role(
                session,
                actor_id,
                operation,
                proposal.account_id,
                proposal.venue,
                team_id=proposal.team_id,
            )
            if proposal.proposer_id == actor_id:
                _reject(
                    "CAPITAL_DUTY_SEPARATION_REQUIRED",
                    "the transfer proposer cannot issue its authorization",
                )
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return _as_uuid(str(response["transfer_authorization_id"]))
            if proposal.status != ProposalStatus.APPROVED.value:
                _reject("TRANSFER_PROPOSAL_NOT_APPROVED", "two Treasury approvals are required")
            if expires_at <= now or expires_at > proposal.expires_at:
                _reject(
                    "TRANSFER_AUTHORIZATION_EXPIRY_INVALID",
                    "transfer authorization must be short-lived",
                )
            authorization = TransferAuthorization(
                team_id=proposal.team_id,
                transfer_proposal_id=proposal.transfer_proposal_id,
                environment=proposal.environment,
                direction=proposal.direction,
                purpose=proposal.purpose,
                account_id=proposal.account_id,
                venue=proposal.venue,
                source_type=proposal.source_type,
                source_id=proposal.source_id,
                destination_type=proposal.destination_type,
                destination_id=proposal.destination_id,
                asset=proposal.asset,
                network=proposal.network,
                destination_reference=proposal.destination_reference,
                amount_limit=proposal.amount,
                max_fee=proposal.max_fee,
                min_received=proposal.min_received,
                expires_at=expires_at,
                active=True,
                actor_id=str(actor_id),
                correlation_id=proposal.correlation_id,
                version=1,
                created_at=now,
            )
            session.add(authorization)
            session.flush()
            result = {"transfer_authorization_id": str(authorization.transfer_authorization_id)}
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="TRANSFER_AUTHORIZATION_ISSUED",
                object_type="TransferAuthorization",
                object_id=authorization.transfer_authorization_id,
                reason="two-reviewer frozen manual transfer",
                correlation_id=proposal.correlation_id,
                object_version=1,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                account_id=proposal.account_id,
                now=now,
            )
            return authorization.transfer_authorization_id

    @staticmethod
    def _capital_balance(
        session: Session,
        *,
        team_id: UUID,
        environment: str,
        endpoint_type: str,
        endpoint_id: str,
        venue: str,
        asset: str,
        lock: bool = False,
    ) -> AccountEquity:
        statement = select(AccountEquity).where(
            AccountEquity.team_id == team_id,
            AccountEquity.environment == environment,
            AccountEquity.account_id == endpoint_id,
            AccountEquity.venue == (venue if endpoint_type == "VENUE" else "VAULT"),
            AccountEquity.location_type == endpoint_type,
            AccountEquity.currency == asset,
        )
        if lock:
            statement = statement.with_for_update()
        fact = session.scalar(statement)
        if fact is None or fact.fact_status != FactStatus.KNOWN.value:
            _reject("CAPITAL_FACT_UNKNOWN", "source or destination capital fact is unknown")
        return fact

    def record_capital_scope_reconciliation(
        self,
        *,
        actor_id: UUID,
        environment: ExecutionEnvironment,
        account_id: str,
        venue: str,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            team = self._require_role(session, actor_id, "capital.reconcile", account_id, venue)
            positions = session.scalars(
                select(Position).where(
                    Position.team_id == team.team_id,
                    Position.environment == environment.value,
                    Position.account_id == account_id,
                    Position.venue == venue,
                )
            ).all()
            orders = session.scalars(
                select(VenueOrder).where(
                    VenueOrder.team_id == team.team_id,
                    VenueOrder.environment == environment.value,
                    VenueOrder.account_id == account_id,
                    VenueOrder.venue == venue,
                )
            ).all()
            campaigns = session.scalars(
                select(Campaign).where(
                    Campaign.team_id == team.team_id,
                    Campaign.environment == environment.value,
                    Campaign.account_id == account_id,
                    Campaign.venue == venue,
                )
            ).all()
            campaign_ids = [item.campaign_id for item in campaigns]
            intents = (
                session.scalars(
                    select(OrderIntent).where(OrderIntent.campaign_id.in_(campaign_ids))
                ).all()
                if campaign_ids
                else []
            )
            unknown = (
                not positions
                or any(item.fact_status != FactStatus.KNOWN.value for item in positions)
                or any(item.status == VenueOrderStatus.UNKNOWN.value for item in orders)
            )
            differences: list[str] = []
            if any(item.quantity != 0 for item in positions):
                differences.append("NONZERO_POSITION")
            if any(item.status in ACTIVE_INTENT_STATUSES for item in intents):
                differences.append("ACTIVE_OR_UNKNOWN_INTENT")
            if any(
                item.status
                not in {
                    VenueOrderStatus.FILLED.value,
                    VenueOrderStatus.CANCELLED.value,
                    VenueOrderStatus.REJECTED.value,
                }
                for item in orders
            ):
                differences.append("ACTIVE_OR_UNKNOWN_VENUE_ORDER")
            status = (
                ReconciliationStatus.UNKNOWN.value
                if unknown
                else (
                    ReconciliationStatus.DIFFERENCE.value
                    if differences
                    else ReconciliationStatus.MATCH.value
                )
            )
            run = ReconciliationRun(
                team_id=team.team_id,
                execution_scope=_scope_key(environment.value, account_id, venue),
                campaign_id=None,
                status=status,
                is_computed=True,
                differences=differences,
                resolution_reason=None,
                actor_id=str(actor_id),
                correlation_id=uuid4(),
                started_at=now,
                completed_at=now,
            )
            session.add(run)
            session.flush()
            return run.reconciliation_id

    @staticmethod
    def _assert_capital_scope_flat(
        session: Session,
        *,
        team_id: UUID,
        environment: str,
        account_id: str,
        venue: str,
        now: datetime,
    ) -> None:
        positions = session.scalars(
            select(Position).where(
                Position.team_id == team_id,
                Position.environment == environment,
                Position.account_id == account_id,
                Position.venue == venue,
            )
        ).all()
        if not positions:
            _reject("CAPITAL_POSITION_UNKNOWN", "flat position facts are required")
        if any(item.fact_status != FactStatus.KNOWN.value for item in positions):
            _reject("CAPITAL_POSITION_UNKNOWN", "unknown position blocks capital transfer")
        policy = session.scalar(
            select(RiskPolicy).where(
                RiskPolicy.team_id == team_id,
                RiskPolicy.active,
            )
        )
        if policy is None or any(
            item.observed_at < now - timedelta(seconds=policy.max_fact_age_seconds)
            for item in positions
        ):
            _reject("CAPITAL_POSITION_UNKNOWN", "fresh flat position facts are required")
        if any(item.quantity != 0 for item in positions):
            _reject(
                "ACTIVE_POSITION_CAPITAL_RESCUE_FORBIDDEN",
                "capital transfer cannot rescue an active position",
            )
        campaigns = session.scalars(
            select(Campaign).where(
                Campaign.team_id == team_id,
                Campaign.environment == environment,
                Campaign.account_id == account_id,
                Campaign.venue == venue,
            )
        ).all()
        campaign_ids = [item.campaign_id for item in campaigns]
        if campaign_ids:
            active_intent = session.scalar(
                select(OrderIntent.intent_id)
                .where(
                    OrderIntent.campaign_id.in_(campaign_ids),
                    OrderIntent.status.in_(ACTIVE_INTENT_STATUSES),
                )
                .limit(1)
            )
            if active_intent is not None:
                _reject(
                    "CAPITAL_ORDER_UNRESOLVED",
                    "active or unknown OrderIntent blocks capital transfer",
                )
        venue_order = session.scalar(
            select(VenueOrder.venue_order_fact_id)
            .where(
                VenueOrder.team_id == team_id,
                VenueOrder.environment == environment,
                VenueOrder.account_id == account_id,
                VenueOrder.venue == venue,
                VenueOrder.status.not_in(
                    {
                        VenueOrderStatus.FILLED.value,
                        VenueOrderStatus.CANCELLED.value,
                        VenueOrderStatus.REJECTED.value,
                    }
                ),
            )
            .limit(1)
        )
        if venue_order is not None:
            _reject(
                "CAPITAL_ORDER_UNRESOLVED",
                "active or unknown VenueOrder blocks capital transfer",
            )

    def reserve_capital_transfer(
        self,
        transfer_authorization_id: UUID,
        actor_id: UUID,
        idempotency_key: str,
        *,
        now: datetime,
        allow_live_unsigned: bool = False,
    ) -> UUID:
        operation = "capital.execute"
        payload = {"transfer_authorization_id": str(transfer_authorization_id)}
        with self.database.session_factory.begin() as session:
            authorization = session.get(
                TransferAuthorization, transfer_authorization_id, with_for_update=True
            )
            if authorization is None:
                _reject("TRANSFER_AUTHORIZATION_NOT_FOUND", "transfer authorization is missing")
            team = self._require_role(
                session,
                actor_id,
                operation,
                authorization.account_id,
                authorization.venue,
                team_id=authorization.team_id,
            )
            proposal = session.get(TransferProposal, authorization.transfer_proposal_id)
            if proposal is None:
                _reject("TRANSFER_PROPOSAL_NOT_FOUND", "authorization proposal is missing")
            if proposal.team_id != authorization.team_id:
                _reject("TEAM_SCOPE_DENIED", "authorization lineage crosses team scope")
            if proposal.proposer_id == actor_id:
                _reject(
                    "CAPITAL_DUTY_SEPARATION_REQUIRED",
                    "the transfer proposer cannot execute its transfer",
                )
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return _as_uuid(str(response["capital_transfer_id"]))
            if not authorization.active or authorization.expires_at <= now:
                _reject("TRANSFER_AUTHORIZATION_INACTIVE", "transfer authorization is inactive")
            if allow_live_unsigned and authorization.environment != ExecutionEnvironment.LIVE.value:
                _reject(
                    "NOTILT_TRANSFER_ENVIRONMENT_INVALID",
                    "NoTilt transaction plans require a LIVE authorization",
                )
            if (
                authorization.environment == ExecutionEnvironment.LIVE.value
                and not allow_live_unsigned
            ):
                _reject(
                    "CAPITAL_TRANSFER_LIVE_DISABLED",
                    "LIVE transfer requires the constrained unsigned transaction workflow",
                )
            if authorization.environment == ExecutionEnvironment.LIVE.value:
                gate = session.get(CapabilityGate, "CAPITAL_TRANSFER")
                if gate is None or gate.status != CapabilityStatus.ENABLED.value:
                    _reject(
                        "CAPABILITY_DISABLED",
                        "CAPITAL_TRANSFER must be explicitly enabled before a LIVE reservation",
                    )
            self._assert_capital_scope_flat(
                session,
                team_id=team.team_id,
                environment=authorization.environment,
                account_id=authorization.account_id,
                venue=authorization.venue,
                now=now,
            )
            if authorization.direction == CapitalDirection.VENUE_TO_VAULT.value:
                latest = session.scalar(
                    select(ReconciliationRun)
                    .where(
                        ReconciliationRun.team_id == team.team_id,
                        ReconciliationRun.execution_scope
                        == _scope_key(
                            authorization.environment,
                            authorization.account_id,
                            authorization.venue,
                        ),
                    )
                    .order_by(ReconciliationRun.completed_at.desc())
                    .limit(1)
                )
                if (
                    latest is None
                    or latest.status != ReconciliationStatus.MATCH.value
                    or not latest.is_computed
                ):
                    _reject(
                        "CAPITAL_RECONCILIATION_REQUIRED",
                        "venue to Vault transfer requires a computed MATCH",
                    )
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {
                    "key": _advisory_lock_key(
                        str(team.team_id),
                        "capital-source",
                        f"{authorization.environment}:{authorization.source_type}:"
                        f"{authorization.source_id}:"
                        f"{authorization.asset}",
                    )
                },
            )
            source = self._capital_balance(
                session,
                team_id=team.team_id,
                environment=authorization.environment,
                endpoint_type=authorization.source_type,
                endpoint_id=authorization.source_id,
                venue=authorization.venue,
                asset=authorization.asset,
                lock=True,
            )
            destination = self._capital_balance(
                session,
                team_id=team.team_id,
                environment=authorization.environment,
                endpoint_type=authorization.destination_type,
                endpoint_id=authorization.destination_id,
                venue=authorization.venue,
                asset=authorization.asset,
                lock=True,
            )
            if source.control_status == "UNKNOWN" or destination.deposit_status != "READY":
                _reject("CAPITAL_FACT_UNKNOWN", "control or destination deposit status is unsafe")
            occupied = session.scalar(
                select(func.coalesce(func.sum(CapitalTransfer.reserved_amount), 0)).where(
                    CapitalTransfer.team_id == team.team_id,
                    CapitalTransfer.environment == authorization.environment,
                    CapitalTransfer.source_id == authorization.source_id,
                    CapitalTransfer.asset == authorization.asset,
                    CapitalTransfer.status.in_(OCCUPIED_CAPITAL_STATUSES),
                )
            )
            withdrawable = (
                source.available_balance
                if source.withdrawable_balance is None
                else source.withdrawable_balance
            )
            if withdrawable - Decimal(occupied or 0) < authorization.amount_limit:
                _reject("CAPITAL_CAPACITY_EXCEEDED", "source confirmed capital is insufficient")
            transfer = CapitalTransfer(
                team_id=team.team_id,
                transfer_authorization_id=authorization.transfer_authorization_id,
                environment=authorization.environment,
                account_id=authorization.account_id,
                venue=authorization.venue,
                direction=authorization.direction,
                source_id=authorization.source_id,
                destination_id=authorization.destination_id,
                asset=authorization.asset,
                network=authorization.network,
                status=CapitalTransferStatus.SOURCE_RESERVED.value,
                gross_amount=authorization.amount_limit,
                reserved_amount=authorization.amount_limit,
                source_balance_before=source.available_balance,
                destination_balance_before=destination.available_balance,
                fee_amount=None,
                net_received=None,
                external_transfer_id=None,
                transaction_reference=None,
                reconciliation_status="NOT_STARTED",
                reconciliation_details=[],
                actor_id=str(actor_id),
                correlation_id=authorization.correlation_id,
                idempotency_key=idempotency_key,
                version=1,
                observed_at=now,
                reconciled_at=None,
                created_at=now,
                updated_at=now,
            )
            authorization.active = False
            authorization.version += 1
            session.add(transfer)
            session.flush()
            result = {"capital_transfer_id": str(transfer.capital_transfer_id)}
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_SOURCE_RESERVED",
                object_type="CapitalTransfer",
                object_id=transfer.capital_transfer_id,
                reason=(
                    "source availability reserved before independent wallet confirmation"
                    if authorization.environment == ExecutionEnvironment.LIVE.value
                    else "source availability reduced before mock submission"
                ),
                correlation_id=transfer.correlation_id,
                object_version=transfer.version,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                account_id=transfer.account_id,
                now=now,
            )
            return transfer.capital_transfer_id

    def capital_transfer_command(
        self, capital_transfer_id: UUID, actor_id: UUID, *, now: datetime
    ) -> CapitalTransferCommand:
        with self.database.session_factory() as session:
            transfer = session.get(CapitalTransfer, capital_transfer_id)
            if transfer is None:
                _reject("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer does not exist")
            authorization = session.get(TransferAuthorization, transfer.transfer_authorization_id)
            if authorization is None:
                _reject("TRANSFER_AUTHORIZATION_NOT_FOUND", "transfer authorization is missing")
            self._require_role(
                session,
                actor_id,
                "capital.execute",
                transfer.account_id,
                transfer.venue,
                team_id=transfer.team_id,
            )
            if transfer.status != CapitalTransferStatus.SOURCE_RESERVED.value:
                _reject("CAPITAL_TRANSFER_ALREADY_SUBMITTED", "capital transfer is not reserved")
            return CapitalTransferCommand(
                capital_transfer_id=transfer.capital_transfer_id,
                environment=ExecutionEnvironment(transfer.environment),
                direction=CapitalDirection(transfer.direction),
                source_id=transfer.source_id,
                destination_id=transfer.destination_id,
                asset=transfer.asset,
                network=transfer.network,
                destination_reference=authorization.destination_reference,
                gross_amount=transfer.gross_amount,
                max_fee=authorization.max_fee,
                min_received=authorization.min_received,
            )

    def notilt_transfer_command(
        self, capital_transfer_id: UUID, actor_id: UUID
    ) -> CapitalTransferCommand:
        with self.database.session_factory() as session:
            transfer = session.get(CapitalTransfer, capital_transfer_id)
            if transfer is None:
                _reject("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer does not exist")
            authorization = session.get(TransferAuthorization, transfer.transfer_authorization_id)
            if authorization is None:
                _reject("TRANSFER_AUTHORIZATION_NOT_FOUND", "transfer authorization is missing")
            self._require_role(
                session,
                actor_id,
                "capital.execute",
                transfer.account_id,
                transfer.venue,
                team_id=transfer.team_id,
            )
            if (
                transfer.transport != "NOTILT"
                or transfer.environment != ExecutionEnvironment.LIVE.value
            ):
                _reject("NOTILT_TRANSFER_STATE_INVALID", "capital transfer is not a NoTilt flow")
            return CapitalTransferCommand(
                capital_transfer_id=transfer.capital_transfer_id,
                environment=ExecutionEnvironment(transfer.environment),
                direction=CapitalDirection(transfer.direction),
                source_id=transfer.source_id,
                destination_id=transfer.destination_id,
                asset=transfer.asset,
                network=transfer.network,
                destination_reference=authorization.destination_reference,
                gross_amount=transfer.gross_amount,
                max_fee=authorization.max_fee,
                min_received=authorization.min_received,
            )

    def record_notilt_plan(
        self,
        capital_transfer_id: UUID,
        actor_id: UUID,
        *,
        chain_id: int,
        transport_state: str,
        transactions: tuple[NoTiltUnsignedTransaction, ...],
        now: datetime,
    ) -> None:
        expected_functions = {
            "DEPOSIT_PLAN_READY": {"approve", "deposit"},
            "RELEASE_REQUEST_PLAN_READY": {"requestWhitelistRelease"},
            "RELEASE_EXECUTION_PLAN_READY": {"executeWhitelistRelease"},
            "RELEASE_CANCELLATION_PLAN_READY": {"cancelWhitelistRelease"},
        }
        allowed_functions = expected_functions.get(transport_state)
        if allowed_functions is None or not transactions:
            _reject("NOTILT_PLAN_INVALID", "NoTilt transaction plan is invalid")
        function_names = {item.function_name for item in transactions}
        if (
            not function_names.issubset(allowed_functions)
            or transactions[-1].function_name
            not in {
                "deposit",
                "requestWhitelistRelease",
                "executeWhitelistRelease",
                "cancelWhitelistRelease",
            }
            or any(item.chain_id != chain_id for item in transactions)
        ):
            _reject("NOTILT_PLAN_INVALID", "NoTilt plan contains an unexpected transaction")
        planned = [item.to_dict() for item in transactions]
        with self.database.session_factory.begin() as session:
            transfer = session.get(CapitalTransfer, capital_transfer_id, with_for_update=True)
            if transfer is None:
                _reject("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer does not exist")
            self._require_role(
                session,
                actor_id,
                "capital.execute",
                transfer.account_id,
                transfer.venue,
                team_id=transfer.team_id,
            )
            if transfer.environment != ExecutionEnvironment.LIVE.value:
                _reject(
                    "NOTILT_TRANSFER_ENVIRONMENT_INVALID",
                    "NoTilt plans require a LIVE capital transfer",
                )
            expected_direction = (
                CapitalDirection.VENUE_TO_VAULT.value
                if transport_state == "DEPOSIT_PLAN_READY"
                else CapitalDirection.VAULT_TO_VENUE.value
            )
            if transfer.direction != expected_direction:
                _reject("NOTILT_PLAN_DIRECTION_INVALID", "NoTilt plan direction does not match")
            allowed_previous_by_state: dict[str, set[str | None]] = {
                "DEPOSIT_PLAN_READY": {None, "DEPOSIT_PLAN_READY"},
                "RELEASE_REQUEST_PLAN_READY": {None, "RELEASE_REQUEST_PLAN_READY"},
                "RELEASE_EXECUTION_PLAN_READY": {
                    "RELEASE_REQUEST_CONFIRMED",
                    "RELEASE_EXECUTION_PLAN_READY",
                },
                "RELEASE_CANCELLATION_PLAN_READY": {
                    "RELEASE_REQUEST_CONFIRMED",
                    "RELEASE_CANCELLATION_PLAN_READY",
                },
            }
            allowed_previous = allowed_previous_by_state[transport_state]
            if transfer.transport_state not in allowed_previous:
                _reject("NOTILT_PLAN_STATE_INVALID", "NoTilt plan is not valid in this state")
            if (
                transport_state
                in {
                    "DEPOSIT_PLAN_READY",
                    "RELEASE_REQUEST_PLAN_READY",
                }
                and transfer.status != CapitalTransferStatus.SOURCE_RESERVED.value
            ):
                _reject("NOTILT_PLAN_STATE_INVALID", "initial NoTilt plan is no longer available")
            if transport_state in {
                "RELEASE_EXECUTION_PLAN_READY",
                "RELEASE_CANCELLATION_PLAN_READY",
            } and transfer.status not in {
                CapitalTransferStatus.IN_FLIGHT.value,
                CapitalTransferStatus.MANUAL_REQUIRED.value,
            }:
                _reject("NOTILT_PLAN_STATE_INVALID", "release request is not awaiting resolution")
            if transfer.transport_state == transport_state:
                if (
                    transfer.transport == "NOTILT"
                    and transfer.chain_id == chain_id
                    and transfer.planned_transactions == planned
                ):
                    return
                _reject("NOTILT_PLAN_IDENTITY_CONFLICT", "NoTilt plan changed for the same stage")
            transfer.transport = "NOTILT"
            transfer.chain_id = chain_id
            transfer.transport_state = transport_state
            transfer.planned_transactions = planned
            transfer.updated_at = now
            transfer.version += 1
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="NOTILT_UNSIGNED_PLAN_RECORDED",
                object_type="CapitalTransfer",
                object_id=transfer.capital_transfer_id,
                reason=transport_state,
                correlation_id=transfer.correlation_id,
                object_version=transfer.version,
                now=now,
            )

    def record_notilt_receipt(
        self,
        capital_transfer_id: UUID,
        actor_id: UUID,
        receipt: NoTiltReceipt,
        *,
        now: datetime,
    ) -> str:
        with self.database.session_factory.begin() as session:
            transfer = session.get(CapitalTransfer, capital_transfer_id, with_for_update=True)
            if transfer is None:
                _reject("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer does not exist")
            self._require_role(
                session, actor_id, "capital.reconcile", transfer.account_id, transfer.venue
            )
            authorization = session.get(TransferAuthorization, transfer.transfer_authorization_id)
            if authorization is None:
                _reject("TRANSFER_AUTHORIZATION_NOT_FOUND", "transfer authorization is missing")
            if (
                transfer.transport != "NOTILT"
                or transfer.chain_id != receipt.chain_id
                or transfer.environment != ExecutionEnvironment.LIVE.value
            ):
                _reject("NOTILT_RECEIPT_SCOPE_MISMATCH", "receipt is outside the NoTilt transfer")
            vault = (
                transfer.source_id
                if transfer.direction == CapitalDirection.VAULT_TO_VENUE.value
                else transfer.destination_id
            )
            if receipt.vault.lower() != vault.lower():
                _reject("NOTILT_RECEIPT_SCOPE_MISMATCH", "receipt Vault does not match")
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {
                    "key": _advisory_lock_key(
                        str(receipt.chain_id),
                        "notilt-receipt",
                        receipt.transaction_hash,
                    )
                },
            )
            replay = session.scalar(
                select(CapitalTransfer.capital_transfer_id)
                .where(
                    CapitalTransfer.capital_transfer_id != capital_transfer_id,
                    CapitalTransfer.chain_id == receipt.chain_id,
                    CapitalTransfer.confirmed_transaction_hashes.contains(
                        [receipt.transaction_hash]
                    ),
                )
                .limit(1)
            )
            if replay is not None:
                _reject(
                    "NOTILT_RECEIPT_REPLAY",
                    "NoTilt transaction receipt is already bound to another transfer",
                )
            confirmed = list(transfer.confirmed_transaction_hashes)
            if receipt.transaction_hash in confirmed:
                return str(transfer.transport_state)
            expected_state = {
                "DEPOSIT": "DEPOSIT_PLAN_READY",
                "RELEASE_REQUEST": "RELEASE_REQUEST_PLAN_READY",
                "RELEASE_EXECUTION": "RELEASE_EXECUTION_PLAN_READY",
                "RELEASE_CANCELLATION": "RELEASE_CANCELLATION_PLAN_READY",
            }[receipt.receipt_kind]
            if transfer.transport_state != expected_state:
                _reject("NOTILT_RECEIPT_STATE_INVALID", "receipt is unexpected for this transfer")
            if receipt.block_timestamp > now + MAX_FACT_CLOCK_SKEW:
                _reject("FACT_TIME_INVALID", "NoTilt receipt time cannot be in the future")

            if receipt.receipt_kind == "DEPOSIT":
                if (
                    transfer.direction != CapitalDirection.VENUE_TO_VAULT.value
                    or receipt.asset != transfer.asset
                    or receipt.requested_amount != authorization.min_received
                    or receipt.credited_amount != authorization.min_received
                ):
                    _reject(
                        "NOTILT_RECEIPT_AMOUNT_INVALID",
                        "NoTilt deposit receipt is outside the authorization",
                    )
                fee = transfer.gross_amount - receipt.credited_amount
                if fee < 0 or fee > authorization.max_fee:
                    _reject(
                        "CAPITAL_DESTINATION_AMOUNT_INVALID",
                        "NoTilt credited amount exceeds the authorized fee budget",
                    )
                transfer.fee_amount = fee
                transfer.net_received = receipt.credited_amount
                transfer.status = CapitalTransferStatus.DESTINATION_CONFIRMED.value
                transfer.transport_state = "DEPOSIT_CONFIRMED"
                transfer.external_transfer_id = receipt.transaction_hash
            elif receipt.receipt_kind == "RELEASE_REQUEST":
                if (
                    transfer.direction != CapitalDirection.VAULT_TO_VENUE.value
                    or receipt.asset != transfer.asset
                    or receipt.request_id is None
                    or receipt.net_amount != authorization.min_received
                    or receipt.fee is None
                    or receipt.execute_after is None
                    or receipt.expires_at is None
                    or receipt.execute_after >= receipt.expires_at
                ):
                    _reject(
                        "NOTILT_RECEIPT_AMOUNT_INVALID",
                        "NoTilt release request is outside the authorization",
                    )
                transfer.fee_amount = receipt.fee
                transfer.protocol_request_id = receipt.request_id
                transfer.protocol_execute_after = receipt.execute_after
                transfer.protocol_expires_at = receipt.expires_at
                transfer.external_transfer_id = receipt.request_id
                transfer.transport_state = "RELEASE_REQUEST_CONFIRMED"
                transfer.status = (
                    CapitalTransferStatus.MANUAL_REQUIRED.value
                    if (
                        receipt.fee > authorization.max_fee
                        or receipt.net_amount + receipt.fee > transfer.gross_amount
                    )
                    else CapitalTransferStatus.IN_FLIGHT.value
                )
            elif receipt.receipt_kind == "RELEASE_EXECUTION":
                if (
                    transfer.direction != CapitalDirection.VAULT_TO_VENUE.value
                    or receipt.request_id != transfer.protocol_request_id
                    or transfer.protocol_execute_after is None
                    or transfer.protocol_expires_at is None
                    or receipt.block_timestamp < transfer.protocol_execute_after
                    or receipt.block_timestamp >= transfer.protocol_expires_at
                    or transfer.fee_amount is None
                    or transfer.fee_amount > authorization.max_fee
                ):
                    _reject(
                        "NOTILT_RECEIPT_REQUEST_INVALID",
                        "NoTilt release execution is outside the authorized request",
                    )
                transfer.transport_state = "RELEASE_EXECUTION_CONFIRMED"
                transfer.status = CapitalTransferStatus.IN_FLIGHT.value
            else:
                if receipt.request_id != transfer.protocol_request_id:
                    _reject(
                        "NOTILT_RECEIPT_REQUEST_INVALID",
                        "NoTilt cancellation request identity does not match",
                    )
                transfer.transport_state = "RELEASE_CANCELLED"
                transfer.status = CapitalTransferStatus.FAILED_SOURCE_RESTORED.value

            confirmed.append(receipt.transaction_hash)
            transfer.confirmed_transaction_hashes = confirmed
            transfer.transaction_reference = receipt.transaction_hash
            transfer.observed_at = receipt.block_timestamp
            transfer.updated_at = now
            transfer.version += 1
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="NOTILT_RECEIPT_VERIFIED",
                object_type="CapitalTransfer",
                object_id=transfer.capital_transfer_id,
                reason=f"{receipt.receipt_kind}:{transfer.transport_state}",
                correlation_id=transfer.correlation_id,
                object_version=transfer.version,
                now=now,
            )
            return str(transfer.transport_state)

    def record_capital_submission(
        self,
        capital_transfer_id: UUID,
        actor_id: UUID,
        submission: CapitalTransferSubmission,
        *,
        now: datetime,
    ) -> None:
        if submission.status != CapitalTransferStatus.SUBMITTED.value:
            _reject("CAPITAL_SUBMISSION_INVALID", "adapter submission status is invalid")
        with self.database.session_factory.begin() as session:
            transfer = session.get(CapitalTransfer, capital_transfer_id, with_for_update=True)
            if transfer is None:
                _reject("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer does not exist")
            self._require_role(
                session,
                actor_id,
                "capital.execute",
                transfer.account_id,
                transfer.venue,
                team_id=transfer.team_id,
            )
            if transfer.status == CapitalTransferStatus.SUBMITTED.value:
                if transfer.external_transfer_id == submission.external_transfer_id:
                    return
                _reject("CAPITAL_TRANSFER_IDENTITY_CONFLICT", "submission identity changed")
            if transfer.status != CapitalTransferStatus.SOURCE_RESERVED.value:
                _reject("CAPITAL_TRANSFER_NOT_SUBMITTABLE", "transfer cannot be submitted again")
            transfer.status = CapitalTransferStatus.SUBMITTED.value
            transfer.external_transfer_id = submission.external_transfer_id
            transfer.observed_at = submission.observed_at
            transfer.updated_at = now
            transfer.version += 1
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_TRANSFER_SUBMITTED_MOCK",
                object_type="CapitalTransfer",
                object_id=transfer.capital_transfer_id,
                reason=submission.external_transfer_id,
                correlation_id=transfer.correlation_id,
                object_version=transfer.version,
                now=now,
            )

    def record_capital_observation(
        self,
        capital_transfer_id: UUID,
        actor_id: UUID,
        status: CapitalTransferStatus,
        *,
        transaction_reference: str | None = None,
        fee_amount: Decimal | None = None,
        net_received: Decimal | None = None,
        now: datetime,
    ) -> CapitalTransferStatus:
        allowed = {
            CapitalTransferStatus.SUBMITTED: {
                CapitalTransferStatus.IN_FLIGHT,
                CapitalTransferStatus.UNKNOWN,
                CapitalTransferStatus.FAILED_SOURCE_RESTORED,
            },
            CapitalTransferStatus.IN_FLIGHT: {
                CapitalTransferStatus.DESTINATION_CONFIRMED,
                CapitalTransferStatus.UNKNOWN,
            },
            CapitalTransferStatus.UNKNOWN: {
                CapitalTransferStatus.IN_FLIGHT,
                CapitalTransferStatus.DESTINATION_CONFIRMED,
                CapitalTransferStatus.MANUAL_REQUIRED,
                CapitalTransferStatus.FAILED_SOURCE_RESTORED,
            },
            CapitalTransferStatus.MANUAL_REQUIRED: {
                CapitalTransferStatus.IN_FLIGHT,
                CapitalTransferStatus.DESTINATION_CONFIRMED,
                CapitalTransferStatus.FAILED_SOURCE_RESTORED,
            },
        }
        with self.database.session_factory.begin() as session:
            transfer = session.get(CapitalTransfer, capital_transfer_id, with_for_update=True)
            if transfer is None:
                _reject("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer does not exist")
            self._require_role(
                session,
                actor_id,
                "capital.reconcile",
                transfer.account_id,
                transfer.venue,
                team_id=transfer.team_id,
            )
            current = CapitalTransferStatus(transfer.status)
            if status is current:
                return current
            if status not in allowed.get(current, set()):
                _reject("CAPITAL_TRANSFER_TRANSITION_INVALID", "capital transition is invalid")
            authorization = session.get(TransferAuthorization, transfer.transfer_authorization_id)
            if authorization is None:
                _reject("TRANSFER_AUTHORIZATION_NOT_FOUND", "transfer authorization is missing")
            if status is CapitalTransferStatus.DESTINATION_CONFIRMED:
                if fee_amount is None or net_received is None:
                    _reject(
                        "CAPITAL_DESTINATION_EVIDENCE_REQUIRED",
                        "destination confirmation requires fee and net receipt",
                    )
                if (
                    fee_amount > authorization.max_fee
                    or net_received < authorization.min_received
                    or net_received + fee_amount > transfer.gross_amount
                ):
                    _reject(
                        "CAPITAL_DESTINATION_AMOUNT_INVALID",
                        "destination receipt is outside the authorization",
                    )
                transfer.fee_amount = fee_amount
                transfer.net_received = net_received
            transfer.status = status.value
            transfer.transaction_reference = transaction_reference
            transfer.observed_at = now
            transfer.updated_at = now
            transfer.version += 1
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_TRANSFER_OBSERVED",
                object_type="CapitalTransfer",
                object_id=transfer.capital_transfer_id,
                reason=status.value,
                correlation_id=transfer.correlation_id,
                object_version=transfer.version,
                now=now,
            )
            return status

    def reconcile_capital_transfer(
        self, capital_transfer_id: UUID, actor_id: UUID, *, now: datetime
    ) -> str:
        with self.database.session_factory.begin() as session:
            transfer = session.get(CapitalTransfer, capital_transfer_id, with_for_update=True)
            if transfer is None:
                _reject("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer does not exist")
            team = self._require_role(
                session,
                actor_id,
                "capital.reconcile",
                transfer.account_id,
                transfer.venue,
                team_id=transfer.team_id,
            )
            authorization = session.get(TransferAuthorization, transfer.transfer_authorization_id)
            if authorization is None:
                _reject("TRANSFER_AUTHORIZATION_NOT_FOUND", "transfer authorization is missing")
            if (
                transfer.status == CapitalTransferStatus.IN_FLIGHT.value
                and transfer.transport_state == "RELEASE_EXECUTION_CONFIRMED"
                and transfer.fee_amount is not None
            ):
                source_fact = self._capital_balance(
                    session,
                    team_id=team.team_id,
                    environment=authorization.environment,
                    endpoint_type=authorization.source_type,
                    endpoint_id=authorization.source_id,
                    venue=authorization.venue,
                    asset=authorization.asset,
                )
                destination_fact = self._capital_balance(
                    session,
                    team_id=team.team_id,
                    environment=authorization.environment,
                    endpoint_type=authorization.destination_type,
                    endpoint_id=authorization.destination_id,
                    venue=authorization.venue,
                    asset=authorization.asset,
                )
                expected_source = (
                    transfer.source_balance_before
                    - authorization.min_received
                    - transfer.fee_amount
                )
                expected_destination = (
                    transfer.destination_balance_before + authorization.min_received
                )
                if (
                    source_fact.observed_at >= transfer.observed_at
                    and destination_fact.observed_at >= transfer.observed_at
                    and source_fact.available_balance == expected_source
                    and destination_fact.available_balance == expected_destination
                ):
                    transfer.net_received = authorization.min_received
                    transfer.status = CapitalTransferStatus.DESTINATION_CONFIRMED.value
                    transfer.version += 1
            differences: list[str] = []
            if transfer.status in {
                CapitalTransferStatus.UNKNOWN.value,
                CapitalTransferStatus.MANUAL_REQUIRED.value,
            }:
                result = ReconciliationStatus.UNKNOWN.value
                differences.append("TRANSFER_OUTCOME_UNKNOWN")
            elif transfer.status in {
                CapitalTransferStatus.SOURCE_RESERVED.value,
                CapitalTransferStatus.SUBMITTED.value,
                CapitalTransferStatus.IN_FLIGHT.value,
            }:
                result = "IN_FLIGHT"
            else:
                source = self._capital_balance(
                    session,
                    team_id=team.team_id,
                    environment=authorization.environment,
                    endpoint_type=authorization.source_type,
                    endpoint_id=authorization.source_id,
                    venue=authorization.venue,
                    asset=authorization.asset,
                )
                destination = self._capital_balance(
                    session,
                    team_id=team.team_id,
                    environment=authorization.environment,
                    endpoint_type=authorization.destination_type,
                    endpoint_id=authorization.destination_id,
                    venue=authorization.venue,
                    asset=authorization.asset,
                )
                if transfer.status == CapitalTransferStatus.FAILED_SOURCE_RESTORED.value:
                    if source.available_balance < transfer.source_balance_before:
                        differences.append("SOURCE_NOT_RESTORED")
                else:
                    if transfer.net_received is None:
                        differences.append("DESTINATION_NET_UNKNOWN")
                    else:
                        expected_source_debit = transfer.net_received + (
                            transfer.fee_amount or Decimal(0)
                        )
                        if source.available_balance > (
                            transfer.source_balance_before - expected_source_debit
                        ):
                            differences.append("SOURCE_DEBIT_NOT_CONFIRMED")
                        if destination.available_balance < (
                            transfer.destination_balance_before + transfer.net_received
                        ):
                            differences.append("DESTINATION_CREDIT_NOT_CONFIRMED")
                        if source.observed_at < transfer.observed_at:
                            differences.append("SOURCE_FACT_STALE")
                        if destination.observed_at < transfer.observed_at:
                            differences.append("DESTINATION_FACT_STALE")
                result = (
                    ReconciliationStatus.DIFFERENCE.value
                    if differences
                    else ReconciliationStatus.MATCH.value
                )
                if (
                    result == ReconciliationStatus.MATCH.value
                    and transfer.status == CapitalTransferStatus.DESTINATION_CONFIRMED.value
                ):
                    transfer.status = CapitalTransferStatus.SETTLED.value
                    transfer.version += 1
            transfer.reconciliation_status = result
            transfer.reconciliation_details = differences
            transfer.reconciled_at = now
            transfer.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_TRANSFER_RECONCILED",
                object_type="CapitalTransfer",
                object_id=transfer.capital_transfer_id,
                reason=result,
                correlation_id=transfer.correlation_id,
                object_version=transfer.version,
                now=now,
            )
            return result

    def set_capability_gate(
        self,
        capability_key: str,
        status: CapabilityStatus,
        reason: str,
        actor_id: UUID,
        *,
        now: datetime,
    ) -> None:
        with self.database.session_factory.begin() as session:
            self._require_role(session, actor_id, "capability.manage")
            gate = session.get(CapabilityGate, capability_key, with_for_update=True)
            if gate is None:
                _reject("CAPABILITY_GATE_NOT_FOUND", "unknown capability")
            if (
                capability_key == "AUTO_ADD"
                and status is CapabilityStatus.ENABLED
                and gate.status != CapabilityStatus.ENABLED.value
            ):
                _reject(
                    "REVIEWED_RESTORE_REQUIRED",
                    "AUTO_ADD may only be enabled through reviewed restore",
                )
            gate.status = status.value
            gate.reason = reason
            gate.operator_id = str(actor_id)
            gate.version += 1
            gate.updated_at = now
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPABILITY_GATE_UPDATED",
                object_type="CapabilityGate",
                object_id=capability_key,
                reason=f"{status.value}:{reason}",
                correlation_id=uuid4(),
                object_version=gate.version,
                now=now,
            )
