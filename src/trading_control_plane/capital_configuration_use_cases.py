from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from trading_control_plane.adapters.capital import CapitalOperation
from trading_control_plane.capital import build_direct_capital_plan
from trading_control_plane.capital_application import CapitalApplicationRuntime
from trading_control_plane.domain import (
    CapitalTreasuryProvider,
    DirectCapitalPath,
    DomainRejected,
)
from trading_control_plane.notilt import SUPPORTED_NOTILT_CHAINS


@dataclass(frozen=True, slots=True)
class DirectCapitalConfigurationInput:
    environment: Literal["LIVE"]
    network: Literal["ARBITRUM"]
    asset: Literal["USDC"]
    treasury_provider: CapitalTreasuryProvider | None
    vault_id: str | None
    vault_address: str | None
    owned_arbitrum_address: str | None
    binance_account_id: str | None
    binance_deposit_address: str | None
    binance_withdrawal_address: str | None
    hyperliquid_account_id: str | None
    hyperliquid_bridge_address: str | None
    safe_address: str | None
    safe_delegate_address: str | None
    clear_notilt_configuration: bool
    clear_safe_configuration: bool
    vault_withdrawal_private_key: str | None
    safe_withdrawal_private_key: str | None
    max_amount: Decimal | None
    max_fee: Decimal | None
    idempotency_key: str


class DirectCapitalOperationInput(Protocol):
    path: DirectCapitalPath
    treasury_provider: CapitalTreasuryProvider | None
    amount: Decimal
    final_confirmed: Literal[True]
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CapitalConfigurationUseCases:
    runtime: CapitalApplicationRuntime

    def direct_capital_configurations(
        self,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        return {
            "data": {
                "TESTNET": None,
                "LIVE": self.runtime.service().direct_capital_configuration(actor_id, "LIVE"),
            },
            "can_manage": self.runtime.service().can_user(actor_id, "access.manage"),
        }

    def notilt_status(
        self,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        self.runtime.snapshot(actor_id)
        return {
            "enabled": self.runtime.settings.notilt_enabled,
            "gateway_available": self.runtime.notilt.available,
            "signing_mode": "EXTERNAL_WALLET_ONLY",
            "credential_custody": "EXTERNAL_WALLET",
            "chains": [
                {
                    "chain_id": chain_id,
                    "chain": chain,
                    "vault_configured": chain_id in self.runtime.settings.notilt_vaults,
                }
                for chain_id, chain in SUPPORTED_NOTILT_CHAINS.items()
            ],
        }

    def notilt_assignment(
        self,
        chain_id: int,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        self.runtime.snapshot(actor_id)
        if (
            not self.runtime.settings.notilt_enabled
            or self.runtime.settings.notilt_agent_address is None
        ):
            raise DomainRejected(
                "NOTILT_NOT_CONFIGURED",
                "NoTilt public whitelist agent address is not configured",
            )
        assigned_vault, active = self.runtime.notilt.resolve_assignment(
            chain_id, self.runtime.settings.notilt_agent_address
        )
        configured_vault = self.runtime.settings.notilt_vaults.get(chain_id)
        return {
            "chain_id": chain_id,
            "chain": SUPPORTED_NOTILT_CHAINS.get(chain_id),
            "active": active,
            "matches_configured_vault": (
                configured_vault is not None and assigned_vault.lower() == configured_vault.lower()
            ),
            "configured_vault": configured_vault is not None,
        }

    def sync_notilt_vault(
        self,
        chain_id: int,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        now = self.runtime.clock()
        fact_count, capital = self.runtime.sync_configured_notilt_vault(
            chain_id,
            actor_id,
            now=now,
        )
        return {
            "transport": "NOTILT_OFFICIAL_SDK_READ_ONLY",
            "chain_id": chain_id,
            "facts_recorded": fact_count,
            "data": capital,
        }

    def capital_center(
        self,
        environment: str | None = None,
        accounts: str | None = None,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        context = self.runtime.queries().user_context(actor_id)
        current_environment = str(context["active_team"]["execution_mode"]).upper()
        if current_environment not in {"TESTNET", "LIVE"}:
            raise DomainRejected(
                "TEAM_SETUP_INCOMPLETE",
                "team must select TESTNET or LIVE before viewing capital",
            )
        if environment is not None and environment.strip().upper() != current_environment:
            raise DomainRejected(
                "CAPITAL_ENVIRONMENT_MISMATCH",
                "capital environment is owned by the team current mode",
            )
        selected = {value for value in (accounts or "").split(",") if value}
        display = self.runtime.queries().capital_display(actor_id, current_environment, selected)
        if current_environment == "LIVE":
            data = self.runtime.snapshot(actor_id)
        else:
            data = {
                "real_transfer_gate": "DISABLED",
                "real_transfer_reason": "test mode never enables real capital transfer",
                "in_transit": None,
                "proposals": [],
                "transfers": [],
                "direct_operations": [],
                "automation": {"gates": {}, "policies": []},
                "direct_configuration": {
                    "can_manage": False,
                    "treasury_provider": None,
                    "network": None,
                    "asset": None,
                },
            }
        raw_net_worth = data.get("net_worth")
        net_worth = dict(raw_net_worth) if isinstance(raw_net_worth, dict) else {}
        authoritative_net_worth_metadata = {
            key: net_worth[key]
            for key in (
                "history_expected_interval_seconds",
                "onchain_provider",
                "onchain_probe",
            )
            if key in net_worth
        }
        data.update(display)
        projected_net_worth = data.get("net_worth")
        if isinstance(projected_net_worth, dict):
            projected_net_worth.update(authoritative_net_worth_metadata)
        else:
            data["net_worth"] = authoritative_net_worth_metadata
        return {"data": data, "as_of": self.runtime.clock().isoformat()}

    def update_direct_capital_configuration(
        self,
        request: DirectCapitalConfigurationInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        configured, _ = self.runtime.direct_settings(actor_id, request.environment)
        provider = request.treasury_provider or CapitalTreasuryProvider(
            configured.capital_direct_treasury_provider
        )
        vault_id = request.vault_id or configured.capital_direct_vault_id
        vault_address = request.vault_address or configured.capital_direct_vault_address
        safe_address = request.safe_address or configured.capital_direct_safe_address
        safe_delegate = (
            request.safe_delegate_address or configured.capital_direct_safe_delegate_address
        )
        if request.clear_notilt_configuration:
            vault_id = vault_address = None
        if request.clear_safe_configuration:
            safe_address = safe_delegate = None
        if provider is CapitalTreasuryProvider.NOTILT_VAULT and request.clear_notilt_configuration:
            raise DomainRejected(
                "CAPITAL_SELECTED_PROVIDER_CLEAR_FORBIDDEN",
                "switch to Safe before clearing the NoTilt configuration",
            )
        if (
            provider is CapitalTreasuryProvider.SAFE_SPENDING_LIMIT
            and request.clear_safe_configuration
        ):
            raise DomainRejected(
                "CAPITAL_SELECTED_PROVIDER_CLEAR_FORBIDDEN",
                "switch to NoTilt before clearing the Safe configuration",
            )
        trusted_vault = self.runtime.settings.notilt_vaults.get(42161)
        if (
            provider is CapitalTreasuryProvider.NOTILT_VAULT
            and trusted_vault is not None
            and vault_address is not None
            and vault_address.lower() != trusted_vault.lower()
        ):
            raise DomainRejected(
                "NOTILT_VAULT_SCOPE_MISMATCH",
                "direct capital Vault must match the configured trusted NoTilt scope",
            )
        config_id = self.runtime.service().set_direct_capital_configuration(
            actor_id,
            request.idempotency_key,
            environment=request.environment,
            network=request.network,
            asset=request.asset,
            treasury_provider=provider.value,
            vault_id=vault_id,
            vault_address=vault_address,
            owned_arbitrum_address=(
                request.owned_arbitrum_address or configured.capital_direct_owned_arbitrum_address
            ),
            binance_account_id=(
                request.binance_account_id or configured.capital_direct_binance_account_id
            ),
            binance_deposit_address=(
                request.binance_deposit_address or configured.capital_direct_binance_deposit_address
            ),
            binance_withdrawal_address=(
                request.binance_withdrawal_address
                or configured.capital_direct_binance_withdrawal_address
            ),
            hyperliquid_account_id=(
                request.hyperliquid_account_id or configured.capital_direct_hyperliquid_account_id
            ),
            hyperliquid_bridge_address=(
                request.hyperliquid_bridge_address
                or configured.capital_direct_hyperliquid_bridge_address
            ),
            safe_address=safe_address,
            safe_delegate_address=safe_delegate,
            vault_withdrawal_private_key=request.vault_withdrawal_private_key,
            safe_withdrawal_private_key=request.safe_withdrawal_private_key,
            max_amount=request.max_amount or configured.capital_direct_max_amount,
            max_fee=(
                request.max_fee
                if request.max_fee is not None
                else configured.capital_direct_max_fee
            ),
            now=self.runtime.clock(),
        )
        configuration = self.runtime.service().direct_capital_configuration(
            actor_id,
            request.environment,
        )
        return {
            "config_id": str(config_id),
            "configuration": configuration,
            "data": self.runtime.snapshot(actor_id),
        }

    def create_direct_capital_operation(
        self,
        request: DirectCapitalOperationInput,
        *,
        actor_id: UUID,
    ) -> dict[str, object]:
        now = self.runtime.clock()
        capital_transfer_gate, binance_credentials_configured = self.runtime.direct_plan_gate(
            actor_id
        )
        direct_settings, _ = self.runtime.direct_settings(actor_id)
        selected_provider = CapitalTreasuryProvider(
            direct_settings.capital_direct_treasury_provider
        )
        if (
            request.treasury_provider is not None
            and request.treasury_provider is not selected_provider
        ):
            raise DomainRejected(
                "CAPITAL_TREASURY_PROVIDER_MISMATCH",
                "capital operation must use the administrator-selected funding provider",
            )
        requested_path = DirectCapitalPath(request.path)
        if requested_path in {
            DirectCapitalPath.VAULT_TO_HYPERLIQUID,
            DirectCapitalPath.HYPERLIQUID_TO_VAULT,
        }:
            direct_settings = self.runtime.hyperliquid_settings(
                actor_id=actor_id,
                account_id=direct_settings.capital_direct_hyperliquid_account_id,
                direct_settings=direct_settings,
            )
            hyperliquid_main = self.runtime.execute_string(
                actor_id=actor_id,
                account_id=direct_settings.capital_direct_hyperliquid_account_id,
                venue="HYPERLIQUID",
                operation=CapitalOperation.HYPERLIQUID_RESOLVE_MAIN,
                parameters={
                    "base_url": direct_settings.hyperliquid_base_url,
                    "account_address": direct_settings.hyperliquid_account_address,
                    "api_wallet_address": direct_settings.hyperliquid_api_wallet_address,
                },
            )
            if hyperliquid_main is None:
                raise DomainRejected(
                    "HYPERLIQUID_MAIN_ACCOUNT_MISSING",
                    "the selected Hyperliquid account does not expose a main account address",
                )
            # Freeze the actual Hyperliquid main wallet as the path handoff address.
            # Safe's delegate remains an independent signer and may send directly to it.
            direct_settings = direct_settings.model_copy(
                update={"capital_direct_owned_arbitrum_address": hyperliquid_main}
            )
        plan = build_direct_capital_plan(
            path=requested_path,
            treasury_provider=selected_provider,
            amount=request.amount,
            settings=direct_settings,
            capital_transfer_gate=capital_transfer_gate,
            binance_capital_credentials_configured=binance_credentials_configured,
            now=now,
        )
        operation_id = self.runtime.service().create_direct_capital_operation(
            actor_id=actor_id,
            plan=plan,
            final_confirmed=request.final_confirmed,
            idempotency_key=request.idempotency_key,
            now=now,
        )
        return {
            "operation_id": str(operation_id),
            "status": plan.status,
            "treasury_provider": plan.treasury_provider.value,
            "blockers": list(plan.blockers),
            "data": self.runtime.direct_action_snapshot(actor_id),
        }
