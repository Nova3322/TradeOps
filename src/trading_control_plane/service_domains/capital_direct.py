from __future__ import annotations

from decimal import InvalidOperation

from trading_control_plane.service_component import ServiceComponent

# The domain implementation intentionally consumes the explicit service_core export surface.
# ruff: noqa: F403, F405
from trading_control_plane.service_core import *


class DirectOperationCapitalService(ServiceComponent):
    @staticmethod
    def _direct_capital_configuration_payload(
        config: DirectCapitalConfiguration,
        updater: User | None,
        *,
        include_sensitive_addresses: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "config_id": str(config.config_id),
            "version": config.version,
            "environment": config.environment,
            "network": config.network,
            "asset": config.asset,
            "treasury_provider": config.treasury_provider,
            "vault_id": config.vault_id,
            "vault_address_configured": config.vault_address is not None,
            "owned_arbitrum_address_configured": (config.owned_arbitrum_address is not None),
            "binance_account_id": config.binance_account_id,
            "binance_deposit_address_configured": (config.binance_deposit_address is not None),
            "binance_withdrawal_address_configured": (
                config.binance_withdrawal_address is not None
            ),
            "hyperliquid_account_id": config.hyperliquid_account_id,
            "hyperliquid_bridge_address_configured": (
                config.hyperliquid_bridge_address is not None
            ),
            "safe_address_configured": config.safe_address is not None,
            "safe_delegate_address_configured": config.safe_delegate_address is not None,
            "vault_withdrawal_private_key_configured": (config.vault_withdrawal_key_version > 0),
            "safe_withdrawal_private_key_configured": (config.safe_withdrawal_key_version > 0),
            "max_amount": None if config.max_amount is None else str(config.max_amount),
            "max_fee": None if config.max_fee is None else str(config.max_fee),
            "updated_by": str(config.updated_by),
            "updated_by_username": None if updater is None else updater.username,
            "effective_at": config.effective_at.isoformat(),
        }
        if include_sensitive_addresses:
            payload.update(
                {
                    "vault_address": config.vault_address,
                    "owned_arbitrum_address": config.owned_arbitrum_address,
                    "binance_deposit_address": config.binance_deposit_address,
                    "binance_withdrawal_address": config.binance_withdrawal_address,
                    "hyperliquid_bridge_address": config.hyperliquid_bridge_address,
                    "safe_address": config.safe_address,
                    "safe_delegate_address": config.safe_delegate_address,
                }
            )
        return payload

    def direct_capital_configuration(
        self,
        actor_id: UUID,
        environment: str = "LIVE",
        *,
        include_sensitive_addresses: bool = False,
    ) -> dict[str, Any] | None:
        normalized_environment = environment.strip().upper()
        if normalized_environment != "LIVE":
            _reject(
                "CAPITAL_CONFIGURATION_INVALID",
                "direct capital configuration is available only in LIVE",
            )
        with self.database.session_factory() as session:
            team = self.transactions._require_role(session, actor_id, "capital.view")
            config = session.scalar(
                select(DirectCapitalConfiguration).where(
                    DirectCapitalConfiguration.team_id == team.team_id,
                    DirectCapitalConfiguration.environment == normalized_environment,
                    DirectCapitalConfiguration.active,
                )
            )
            if config is None:
                return None
            return self._direct_capital_configuration_payload(
                config,
                session.get(User, config.updated_by),
                include_sensitive_addresses=include_sensitive_addresses,
            )

    def set_direct_capital_configuration(
        self,
        actor_id: UUID,
        idempotency_key: str,
        *,
        environment: str = "LIVE",
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
        vault_withdrawal_private_key: str | None = None,
        safe_withdrawal_private_key: str | None = None,
        max_amount: Decimal | None,
        max_fee: Decimal | None,
        now: datetime,
    ) -> UUID:
        normalized_environment = environment.strip().upper()
        if normalized_environment != "LIVE":
            _reject(
                "CAPITAL_CONFIGURATION_INVALID",
                "direct capital configuration is available only in LIVE",
            )
        operation = f"capital.configuration.manage:{normalized_environment}"
        payload = {
            "environment": normalized_environment,
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
            "vault_withdrawal_key_semantics": (
                None
                if vault_withdrawal_private_key is None
                else self.credential_cipher.secret_fingerprint(
                    vault_withdrawal_private_key,
                    purpose=f"capital-vault-withdrawal:{normalized_environment}",
                )
            ),
            "safe_withdrawal_key_semantics": (
                None
                if safe_withdrawal_private_key is None
                else self.credential_cipher.secret_fingerprint(
                    safe_withdrawal_private_key,
                    purpose=f"capital-safe-withdrawal:{normalized_environment}",
                )
            ),
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
        if max_amount is not None and max_amount <= 0:
            _reject("CAPITAL_CONFIGURATION_INVALID", "maximum amount must be positive")
        if max_fee is not None and max_fee < 0:
            _reject("CAPITAL_CONFIGURATION_INVALID", "maximum fee cannot be negative")
        if max_amount is not None and max_fee is not None and max_fee >= max_amount:
            _reject(
                "CAPITAL_CONFIGURATION_FEE_LIMIT_INVALID",
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
            team = self.transactions._require_role(session, actor_id, operation)
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
                    environment=normalized_environment,
                    now=now,
                )
            digest, response = self.transactions._idempotency(
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
                    DirectCapitalConfiguration.environment == normalized_environment,
                    DirectCapitalConfiguration.active,
                )
                .with_for_update()
            )
            next_version = 1 if current is None else current.version + 1
            if current is not None:
                current.active = False
            vault_key_version = (0 if current is None else current.vault_withdrawal_key_version) + (
                1 if vault_withdrawal_private_key is not None else 0
            )
            safe_key_version = (0 if current is None else current.safe_withdrawal_key_version) + (
                1 if safe_withdrawal_private_key is not None else 0
            )
            vault_key_ciphertext = (
                None if current is None else current.vault_withdrawal_key_ciphertext
            )
            vault_key_metadata = (
                {} if current is None else dict(current.vault_withdrawal_key_metadata or {})
            )
            safe_key_ciphertext = (
                None if current is None else current.safe_withdrawal_key_ciphertext
            )
            safe_key_metadata = (
                {} if current is None else dict(current.safe_withdrawal_key_metadata or {})
            )
            if vault_withdrawal_private_key is not None:
                encrypted_vault_key = self.credential_cipher.encrypt_secret(
                    vault_withdrawal_private_key,
                    team_id=team.team_id,
                    object_id=team.team_id,
                    purpose=f"capital-vault-withdrawal:{normalized_environment}",
                    credential_version=vault_key_version,
                )
                vault_key_ciphertext = encrypted_vault_key.ciphertext
                vault_key_metadata = encrypted_vault_key.metadata
            if safe_withdrawal_private_key is not None:
                encrypted_safe_key = self.credential_cipher.encrypt_secret(
                    safe_withdrawal_private_key,
                    team_id=team.team_id,
                    object_id=team.team_id,
                    purpose=f"capital-safe-withdrawal:{normalized_environment}",
                    credential_version=safe_key_version,
                )
                safe_key_ciphertext = encrypted_safe_key.ciphertext
                safe_key_metadata = encrypted_safe_key.metadata
            config = DirectCapitalConfiguration(
                team_id=team.team_id,
                version=next_version,
                environment=normalized_environment,
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
                vault_withdrawal_key_ciphertext=vault_key_ciphertext,
                vault_withdrawal_key_metadata=vault_key_metadata,
                vault_withdrawal_key_version=vault_key_version,
                safe_withdrawal_key_ciphertext=safe_key_ciphertext,
                safe_withdrawal_key_metadata=safe_key_metadata,
                safe_withdrawal_key_version=safe_key_version,
                max_amount=max_amount,
                max_fee=max_fee,
                updated_by=actor_id,
                effective_at=now,
            )
            session.add(config)
            session.flush()
            result = {"config_id": str(config.config_id), "version": config.version}
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAPITAL_DIRECT_CONFIGURATION_UPDATED",
                object_type="DirectCapitalConfiguration",
                object_id=config.config_id,
                reason=(
                    f"environment={normalized_environment};version={config.version}; "
                    "network=ARBITRUM; asset=USDC; "
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
            team = self.transactions._require_role(
                session,
                actor_id,
                "capital.execute",
                plan.account_id,
                plan.venue,
            )
            if plan.account_id is not None:
                account_environment = (
                    team.execution_mode if team.execution_mode in {"TESTNET", "LIVE"} else "LIVE"
                )
                self._ensure_exchange_account_reference(
                    session,
                    team=team,
                    actor_id=actor_id,
                    account_id=plan.account_id,
                    venue=plan.venue,
                    environment=account_environment,
                    now=now,
                )
            digest, response = self.transactions._idempotency(
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
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
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
            self.transactions._require_role(
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
        wallet_address: str,
        idempotency_key: str,
        now: datetime,
        preview_kind: str = "INITIAL",
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
            self.transactions._require_role(
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
            digest, response = self.transactions._idempotency(
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
            outbound = item.path in {
                DirectCapitalPath.VAULT_TO_BINANCE.value,
                DirectCapitalPath.VAULT_TO_HYPERLIQUID.value,
            }
            if preview_kind == "RELEASE_EXECUTION":
                if not outbound or not any(
                    stage.get("code") == "NOTILT_RELEASE_REQUEST_RECEIPT_CONFIRMED"
                    for stage in item.stages
                ):
                    _reject(
                        "NOTILT_RELEASE_NOT_EXECUTABLE",
                        "verified NoTilt release request is required before execution",
                    )
                allowed_functions = {"executeWhitelistRelease"}
                stage_code = "NOTILT_UNSIGNED_RELEASE_EXECUTION_PREVIEW"
            elif preview_kind == "INITIAL":
                allowed_functions = {"requestWhitelistRelease"} if outbound else {
                    "approve",
                    "deposit",
                }
                stage_code = (
                    "NOTILT_UNSIGNED_RELEASE_REQUEST_PREVIEW"
                    if outbound
                    else "NOTILT_UNSIGNED_DEPOSIT_PREVIEW"
                )
            else:
                _reject("NOTILT_PLAN_INVALID", "NoTilt preview kind is unsupported")
            if any(
                transaction.function_name not in allowed_functions for transaction in transactions
            ):
                _reject(
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
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
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
            _reject("NOTILT_PLAN_INVALID", "NoTilt destination transfer is invalid")
        operation = "capital.direct.notilt_destination_preview"
        with self.database.session_factory.begin() as session:
            item = session.get(DirectCapitalOperation, operation_id, with_for_update=True)
            if item is None:
                _reject("CAPITAL_DIRECT_OPERATION_NOT_FOUND", "direct capital operation is missing")
            self.transactions._require_role(
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
            digest, response = self.transactions._idempotency(
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
            if (
                item.treasury_provider != "NOTILT_VAULT"
                or item.path != DirectCapitalPath.VAULT_TO_BINANCE.value
                or not any(
                    stage.get("code") == "NOTILT_RELEASE_EXECUTION_RECEIPT_CONFIRMED"
                    for stage in item.stages
                )
            ):
                _reject(
                    "NOTILT_RELEASE_NOT_EXECUTABLE",
                    "verified NoTilt release execution is required before destination transfer",
                )
            if (
                str(artifact.get("recipient", "")).lower()
                != str(item.destination_reference or "").lower()
                or str(artifact.get("amount")) != str(item.min_received or item.amount)
            ):
                _reject(
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
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
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
            self.transactions._require_role(
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
            required_transaction_fields = {"from", "to", "data", "value"}
            if outbound and (
                signature_request.get("calldataReady") is not True
                or any(not signature_request.get(field) for field in required_transaction_fields)
            ):
                _reject(
                    "SAFE_PLAN_INVALID",
                    "Safe allowance preflight must contain an exact wallet transaction",
                )
            payload = {
                "operation_id": str(operation_id),
                "expected_version": expected_version,
                "signature_request": signature_request,
                "final_confirmed": True,
            }
            digest, response = self.transactions._idempotency(
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
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
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
            "HYPERLIQUID_CCTP_WITHDRAWAL_TYPED_REQUEST",
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
            self.transactions._require_role(
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
                    "HYPERLIQUID_CCTP_WITHDRAWAL_TYPED_REQUEST",
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
            digest, response = self.transactions._idempotency(
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
            if kind in {
                "HYPERLIQUID_WITHDRAW3_TYPED_REQUEST",
                "HYPERLIQUID_CCTP_WITHDRAWAL_TYPED_REQUEST",
            }:
                try:
                    expected_fee = Decimal(str(artifact["expectedFee"]))
                    min_received = Decimal(str(artifact["minReceived"]))
                except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
                    raise DomainRejected(
                        "HYPERLIQUID_CAPITAL_PLAN_INVALID",
                        "Hyperliquid withdrawal fee or net amount is invalid",
                    ) from exc
                if (
                    expected_fee < 0
                    or expected_fee > item.max_fee
                    or min_received <= 0
                    or min_received > item.amount
                    or min_received != item.amount - expected_fee
                ):
                    _reject(
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
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
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
            self.transactions._require_role(
                session,
                actor_id,
                "capital.execute",
                item.account_id,
                item.venue,
                team_id=item.team_id,
            )
            digest, response = self.transactions._idempotency(
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
                DirectCapitalPath.VAULT_TO_BINANCE.value: {
                    "TREASURY_WITHDRAWAL",
                    "NOTILT_RELEASE_EXECUTION",
                    "NOTILT_DESTINATION_TRANSFER",
                },
                DirectCapitalPath.VAULT_TO_HYPERLIQUID.value: {
                    "TREASURY_WITHDRAWAL",
                    "NOTILT_RELEASE_EXECUTION",
                    "HYPERLIQUID_DEPOSIT",
                },
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
                        and str(existing.get("code", ""))
                        in treasury_preview_codes
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
                _reject(
                    "HYPERLIQUID_CAPITAL_PREFLIGHT_REQUIRED",
                    "prepare a current unsigned wallet request before recording a wallet result",
                )
            if outcome == "SUBMITTED" and stage.startswith("HYPERLIQUID_"):
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
            if outcome == "SUBMITTED" and stage == "NOTILT_RELEASE_EXECUTION":
                release_receipt = next(
                    (
                        existing.get("evidence")
                        for existing in reversed(item.stages)
                        if existing.get("code")
                        == "NOTILT_RELEASE_REQUEST_RECEIPT_CONFIRMED"
                    ),
                    None,
                )
                try:
                    release_expires_at = datetime.fromisoformat(
                        str(release_receipt["expires_at"])
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise DomainRejected(
                        "NOTILT_RECEIPT_INVALID",
                        "stored NoTilt release window is invalid",
                    ) from exc
                if release_expires_at <= now:
                    _reject("NOTILT_RELEASE_EXPIRED", "NoTilt release request expired")
            if outcome == "SUBMITTED" and stage == "NOTILT_DESTINATION_TRANSFER":
                try:
                    preview_expires_at = datetime.fromisoformat(
                        str(preview["artifact"]["expiresAt"])
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise DomainRejected(
                        "NOTILT_PLAN_INVALID",
                        "stored NoTilt destination transfer is invalid",
                    ) from exc
                if preview_expires_at <= now:
                    _reject(
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
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
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
            item = session.get(DirectCapitalOperation, operation_id, with_for_update=True)
            if item is None:
                _reject("CAPITAL_DIRECT_OPERATION_NOT_FOUND", "direct capital operation is missing")
            self.transactions._require_role(
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
            digest, response = self.transactions._idempotency(
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
            if (
                item.path
                not in {
                    DirectCapitalPath.VAULT_TO_HYPERLIQUID.value,
                    DirectCapitalPath.VAULT_TO_BINANCE.value,
                }
                or item.treasury_provider != "SAFE_SPENDING_LIMIT"
            ):
                _reject(
                    "TREASURY_RECEIPT_STAGE_INVALID",
                    "Safe source receipt does not match this capital path",
                )
            if item.path == DirectCapitalPath.VAULT_TO_BINANCE.value and not any(
                stage.get("code") == "BINANCE_DEPOSIT_PREFLIGHT_READY"
                for stage in item.stages
            ):
                _reject(
                    "BINANCE_DEPOSIT_PREFLIGHT_REQUIRED",
                    "confirm the current exact Binance deposit address before settlement",
                )
            submitted = next(
                (
                    stage
                    for stage in reversed(item.stages)
                    if stage.get("code")
                    == "TREASURY_WITHDRAWAL_SUBMITTED_BY_HUMAN_WALLET"
                ),
                None,
            )
            evidence_hash = str(
                evidence.get("transactionHash") or evidence.get("transaction_hash") or ""
            ).lower()
            if submitted is None or evidence_hash != str(
                submitted.get("transaction_hash", "")
            ).lower():
                _reject(
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
            if item.path == DirectCapitalPath.VAULT_TO_BINANCE.value:
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
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
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
            _reject("NOTILT_RECEIPT_STATE_INVALID", "NoTilt receipt kind is unsupported")
        operation = "capital.direct.notilt_receipt"
        with self.database.session_factory.begin() as session:
            item = session.get(DirectCapitalOperation, operation_id, with_for_update=True)
            if item is None:
                _reject("CAPITAL_DIRECT_OPERATION_NOT_FOUND", "direct capital operation is missing")
            self.transactions._require_role(
                session,
                actor_id,
                "capital.execute",
                item.account_id,
                item.venue,
                team_id=item.team_id,
            )
            if item.treasury_provider != "NOTILT_VAULT" or item.path not in {
                DirectCapitalPath.VAULT_TO_BINANCE.value,
                DirectCapitalPath.VAULT_TO_HYPERLIQUID.value,
            }:
                _reject(
                    "NOTILT_RECEIPT_SCOPE_MISMATCH",
                    "NoTilt release receipt does not match the frozen capital path",
                )
            payload = {
                "operation_id": str(operation_id),
                "expected_version": expected_version,
                "receipt_kind": receipt_kind,
                "evidence": evidence,
            }
            digest, response = self.transactions._idempotency(
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
            submission_code = (
                "TREASURY_WITHDRAWAL_SUBMITTED_BY_HUMAN_WALLET"
                if receipt_kind == "RELEASE_REQUEST"
                else "NOTILT_RELEASE_EXECUTION_SUBMITTED_BY_HUMAN_WALLET"
            )
            submitted = next(
                (
                    stage
                    for stage in reversed(item.stages)
                    if stage.get("code") == submission_code
                ),
                None,
            )
            evidence_hash = str(evidence.get("transaction_hash", "")).lower()
            if submitted is None or evidence_hash != str(
                submitted.get("transaction_hash", "")
            ).lower():
                _reject(
                    "NOTILT_RECEIPT_REFERENCE_MISMATCH",
                    "NoTilt receipt does not match the recorded wallet transaction",
                )
            if receipt_kind == "RELEASE_EXECUTION" and not any(
                stage.get("code") == "NOTILT_RELEASE_REQUEST_RECEIPT_CONFIRMED"
                for stage in item.stages
            ):
                _reject(
                    "NOTILT_RELEASE_NOT_EXECUTABLE",
                    "verified NoTilt release request is required before execution",
                )
            if receipt_kind == "RELEASE_REQUEST":
                request_id = str(evidence.get("request_id", ""))
                try:
                    execute_after = datetime.fromisoformat(str(evidence["execute_after"]))
                    expires_at = datetime.fromisoformat(str(evidence["expires_at"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise DomainRejected(
                        "NOTILT_RECEIPT_INVALID",
                        "NoTilt release receipt has invalid protocol timing",
                    ) from exc
                if not request_id.startswith("0x") or len(request_id) != 66:
                    _reject("NOTILT_RECEIPT_INVALID", "NoTilt request id is invalid")
                if execute_after >= expires_at or expires_at <= now:
                    _reject("NOTILT_RECEIPT_INVALID", "NoTilt release window is invalid")
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
                    _reject(
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
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
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
            item = session.get(DirectCapitalOperation, operation_id, with_for_update=True)
            if item is None:
                _reject("CAPITAL_DIRECT_OPERATION_NOT_FOUND", "direct capital operation is missing")
            self.transactions._require_role(
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
            digest, response = self.transactions._idempotency(
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
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
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
            self.transactions._require_role(
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
            digest, response = self.transactions._idempotency(
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
                    and (
                        item.treasury_provider == "SAFE_SPENDING_LIMIT"
                        or "TREASURY_DESTINATION_RECEIPT_CONFIRMED" in confirmed
                    )
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
                    item.path == DirectCapitalPath.VAULT_TO_HYPERLIQUID.value
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
                                    if item.path == DirectCapitalPath.VAULT_TO_HYPERLIQUID.value
                                    else "TREASURY_DESTINATION_RECEIPT_REQUIRED"
                                ),
                            ]
                        )
                    )
            item.updated_at = now
            result = {"operation_id": str(operation_id), "version": item.version}
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
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
            self.transactions._require_role(
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
            digest, response = self.transactions._idempotency(
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
                    "CAPITAL_MIN_RECEIVED_INVALID",
                    "BINANCE_CAPITAL_CREDENTIALS_MISSING",
                    "BINANCE_DEPOSIT_PREFLIGHT_REQUIRED",
                    "BINANCE_RESTRICTED_WITHDRAWAL_PREFLIGHT_REQUIRED",
                }
            ]
            if item.path == DirectCapitalPath.BINANCE_TO_VAULT.value:
                try:
                    min_received = Decimal(str(artifact["minReceived"]))
                except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
                    raise DomainRejected(
                        "BINANCE_CAPITAL_PREFLIGHT_INVALID",
                        "Binance withdrawal preflight did not return a valid net amount",
                    ) from exc
                if min_received <= 0 or min_received > item.amount:
                    _reject(
                        "BINANCE_CAPITAL_PREFLIGHT_INVALID",
                        "Binance withdrawal preflight returned an invalid net amount",
                    )
                item.min_received = min_received
            if not item.blockers:
                item.status = "UNSIGNED_PLAN_READY"
            item.version += 1
            item.updated_at = now
            result = {"operation_id": str(operation_id), "version": item.version}
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
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
            self.transactions._require_role(
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
            digest, response = self.transactions._idempotency(
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
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
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
            self.transactions._require_role(
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
            digest, response = self.transactions._idempotency(
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
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{item.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
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
