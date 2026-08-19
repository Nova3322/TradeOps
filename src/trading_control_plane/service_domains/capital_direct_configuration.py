from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from trading_control_plane import capital, domain, models, rejections
from trading_control_plane.service_component import ServiceComponent
from trading_control_plane.service_domains.accounts import ensure_exchange_account_reference


class DirectCapitalConfigurationService(ServiceComponent):
    @staticmethod
    def _direct_capital_configuration_payload(
        config: models.DirectCapitalConfiguration,
        updater: models.User | None,
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
            rejections.reject(
                "CAPITAL_CONFIGURATION_INVALID",
                "direct capital configuration is available only in LIVE",
            )
        with self.database.session_factory() as session:
            team = self.transactions.require_action_assignment(session, actor_id, "capital.view")
            config = session.scalar(
                select(models.DirectCapitalConfiguration).where(
                    models.DirectCapitalConfiguration.team_id == team.team_id,
                    models.DirectCapitalConfiguration.environment == normalized_environment,
                    models.DirectCapitalConfiguration.active,
                )
            )
            if config is None:
                return None
            return self._direct_capital_configuration_payload(
                config,
                session.get(models.User, config.updated_by),
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
            rejections.reject(
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
            rejections.reject(
                "CAPITAL_CONFIGURATION_UNTRUSTED",
                "direct capital paths only support the trusted Arbitrum USDC catalog",
            )
        if treasury_provider not in {"NOTILT_VAULT", "SAFE_SPENDING_LIMIT"}:
            rejections.reject("CAPITAL_CONFIGURATION_INVALID", "funding provider is unsupported")
        if max_amount is not None and max_amount <= 0:
            rejections.reject("CAPITAL_CONFIGURATION_INVALID", "maximum amount must be positive")
        if max_fee is not None and max_fee < 0:
            rejections.reject("CAPITAL_CONFIGURATION_INVALID", "maximum fee cannot be negative")
        if max_amount is not None and max_fee is not None and max_fee >= max_amount:
            rejections.reject(
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
            rejections.reject(
                "CAPITAL_BINANCE_WITHDRAWAL_ADDRESS_SCOPE_MISMATCH",
                "Binance withdrawal must target the selected on-chain treasury",
            )
        with self.database.session_factory.begin() as session:
            team = self.transactions.require_role(session, actor_id, operation)
            assignments = session.scalars(
                select(models.RoleAssignment).where(
                    models.RoleAssignment.user_id == actor_id,
                    models.RoleAssignment.team_id == team.team_id,
                )
            ).all()
            if not any(item.role == domain.Role.SYSTEM_ADMIN.value for item in assignments):
                rejections.reject(
                    "CAPITAL_CONFIGURATION_ADMIN_REQUIRED",
                    "direct capital configuration requires SYSTEM_ADMIN",
                )
            for configured_account_id, configured_venue in (
                (binance_account_id, "BINANCE"),
                (hyperliquid_account_id, "HYPERLIQUID"),
            ):
                if configured_account_id is None:
                    continue
                ensure_exchange_account_reference(
                    session,
                    team=team,
                    actor_id=actor_id,
                    account_id=configured_account_id,
                    venue=configured_venue,
                    environment=normalized_environment,
                    now=now,
                )
            digest, response = self.transactions.idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return UUID(str(response["config_id"]))
            current = session.scalar(
                select(models.DirectCapitalConfiguration)
                .where(
                    models.DirectCapitalConfiguration.team_id == team.team_id,
                    models.DirectCapitalConfiguration.environment == normalized_environment,
                    models.DirectCapitalConfiguration.active,
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
            config = models.DirectCapitalConfiguration(
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
            self.transactions.save_receipt(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions.audit(
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
        plan: capital.DirectCapitalPlan,
        final_confirmed: bool,
        idempotency_key: str,
        now: datetime,
    ) -> UUID:
        if not final_confirmed:
            rejections.reject(
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
            team = self.transactions.require_role(
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
                ensure_exchange_account_reference(
                    session,
                    team=team,
                    actor_id=actor_id,
                    account_id=plan.account_id,
                    venue=plan.venue,
                    environment=account_environment,
                    now=now,
                )
            digest, response = self.transactions.idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return UUID(str(response["operation_id"]))
            correlation_id = uuid4()
            direct_operation = models.DirectCapitalOperation(
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
            self.transactions.save_receipt(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions.audit(
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
        allow_expired: bool = False,
    ) -> dict[str, Any]:
        with self.database.session_factory() as session:
            item = session.get(models.DirectCapitalOperation, operation_id)
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
            if item.expires_at <= now and not allow_expired:
                rejections.reject(
                    "CAPITAL_DIRECT_OPERATION_EXPIRED", "direct capital operation expired"
                )
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


__all__ = ["DirectCapitalConfigurationService"]
