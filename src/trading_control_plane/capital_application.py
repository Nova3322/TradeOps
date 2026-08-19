from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TypedDict
from uuid import UUID

from trading_control_plane.adapters.capital import (
    CapitalAdapter,
    CapitalScope,
    CapitalVenue,
)
from trading_control_plane.adapters.capital import (
    CapitalOperation as CapitalOperation,
)
from trading_control_plane.auth import SignedTokenService
from trading_control_plane.capital import MockCapitalTransferAdapter
from trading_control_plane.config import Settings
from trading_control_plane.domain import DomainRejected
from trading_control_plane.notilt import (
    SUPPORTED_NOTILT_CHAINS,
    NoTiltGateway,
    NoTiltUsdValuator,
)
from trading_control_plane.queries import TradingQueries
from trading_control_plane.safe_spending import SafeSpendingGateway
from trading_control_plane.service import TradingService

JsonObject = dict[str, object]
QueryFactory = Callable[[], TradingQueries]
ServiceFactory = Callable[[], TradingService]
CapitalAdapterResolver = Callable[[CapitalScope], CapitalAdapter]


class DirectCapitalContext(TypedDict):
    version: int
    path: str
    treasury_provider: str
    account_id: str | None
    source_reference: str | None
    destination_reference: str | None
    asset: str
    network: str
    amount: object
    max_fee: object | None
    min_received: object
    status: str
    stages: list[dict[str, object]]
    blockers: list[str]


@dataclass(frozen=True, slots=True)
class CapitalApplicationRuntime:
    """Explicit immutable dependencies shared by capital application use cases."""

    settings: Settings
    queries: QueryFactory
    service: ServiceFactory
    clock: Callable[[], datetime]
    notify_capital: Callable[..., None]
    token_service: SignedTokenService
    adapter_resolver: CapitalAdapterResolver
    transfer_adapter: MockCapitalTransferAdapter
    notilt: NoTiltGateway
    notilt_valuator: NoTiltUsdValuator
    safe_spending: SafeSpendingGateway

    def direct_action_snapshot(self, actor_id: UUID) -> JsonObject:
        """Return DB state without repeating slow external capital probes."""

        return self.queries().capital_center(actor_id)

    def direct_plan_gate(self, actor_id: UUID) -> tuple[str | None, bool]:
        snapshot = self.snapshot(actor_id)
        raw_gate = snapshot.get("real_transfer_gate")
        raw_configuration = snapshot.get("direct_configuration")
        credentials_configured = (
            bool(raw_configuration.get("binance_capital_credentials_configured"))
            if isinstance(raw_configuration, Mapping)
            else False
        )
        return (
            raw_gate if isinstance(raw_gate, str) else None,
            credentials_configured,
        )

    def max_fact_age_seconds(self, actor_id: UUID) -> int:
        snapshot = self.snapshot(actor_id)
        net_worth = snapshot.get("net_worth")
        value = net_worth.get("max_fact_age_seconds") if isinstance(net_worth, Mapping) else None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DomainRejected(
                "CAPITAL_FACT_AGE_INVALID",
                "capital freshness policy is unavailable",
            )
        return value

    def direct_operation_context(
        self,
        operation_id: UUID,
        actor_id: UUID,
        expected_version: int,
        now: datetime,
        *,
        conflict_message: str = "direct capital operation changed; refresh",
        allow_expired: bool = False,
    ) -> DirectCapitalContext:
        raw = self.service().direct_capital_operation_context(
            operation_id,
            actor_id,
            now=now,
            allow_expired=allow_expired,
        )
        version = raw.get("version")
        stages = raw.get("stages")
        blockers = raw.get("blockers")
        if isinstance(version, bool) or not isinstance(version, int):
            raise DomainRejected("CAPITAL_CONTEXT_INVALID", "capital version is invalid")
        if version != expected_version:
            raise DomainRejected("VERSION_CONFLICT", conflict_message)
        if not isinstance(stages, list) or not all(isinstance(stage, Mapping) for stage in stages):
            raise DomainRejected("CAPITAL_CONTEXT_INVALID", "capital stages are invalid")
        if not isinstance(blockers, list) or not all(
            isinstance(blocker, str) for blocker in blockers
        ):
            raise DomainRejected("CAPITAL_CONTEXT_INVALID", "capital blockers are invalid")
        required_text = ("path", "treasury_provider", "asset", "network", "status")
        if any(not isinstance(raw.get(field), str) for field in required_text):
            raise DomainRejected("CAPITAL_CONTEXT_INVALID", "capital scope is invalid")
        return DirectCapitalContext(
            version=version,
            path=raw["path"],
            treasury_provider=raw["treasury_provider"],
            account_id=None if raw.get("account_id") is None else str(raw["account_id"]),
            source_reference=(
                None if raw.get("source_reference") is None else str(raw["source_reference"])
            ),
            destination_reference=(
                None
                if raw.get("destination_reference") is None
                else str(raw["destination_reference"])
            ),
            asset=raw["asset"],
            network=raw["network"],
            amount=raw.get("amount"),
            max_fee=raw.get("max_fee"),
            min_received=raw.get("min_received"),
            status=raw["status"],
            stages=[{str(key): value for key, value in stage.items()} for stage in stages],
            blockers=list(blockers),
        )

    def scope(
        self,
        *,
        actor_id: UUID,
        account_id: str | None,
        venue: CapitalVenue,
    ) -> CapitalScope:
        if not account_id:
            raise DomainRejected(
                "CAPITAL_ACCOUNT_SCOPE_MISSING",
                "capital operations require an exact runtime account ID",
            )
        account = self.queries().capital_account_scope(
            actor_id,
            account_id,
            venue,
            "LIVE",
        )
        return CapitalScope(
            workspace_id=account["workspace_id"],
            team_id=account["team_id"],
            account_id=account_id,
            venue=venue,
            environment="LIVE",
            account_mode=account["account_mode"],
        )

    def _execute(
        self,
        *,
        actor_id: UUID,
        account_id: str | None,
        venue: CapitalVenue,
        operation: CapitalOperation,
        parameters: Mapping[str, object],
    ) -> object:
        scope = self.scope(actor_id=actor_id, account_id=account_id, venue=venue)
        return self.adapter_resolver(scope).execute(operation, parameters).value

    def execute_mapping(
        self,
        *,
        actor_id: UUID,
        account_id: str | None,
        venue: CapitalVenue,
        operation: CapitalOperation,
        parameters: Mapping[str, object],
    ) -> dict[str, object]:
        value = self._execute(
            actor_id=actor_id,
            account_id=account_id,
            venue=venue,
            operation=operation,
            parameters=parameters,
        )
        if not isinstance(value, Mapping):
            raise DomainRejected("CAPITAL_RESULT_INVALID", "capital result must be an object")
        return {str(key): item for key, item in value.items()}

    def execute_string(
        self,
        *,
        actor_id: UUID,
        account_id: str | None,
        venue: CapitalVenue,
        operation: CapitalOperation,
        parameters: Mapping[str, object],
    ) -> str:
        value = self._execute(
            actor_id=actor_id,
            account_id=account_id,
            venue=venue,
            operation=operation,
            parameters=parameters,
        )
        if not isinstance(value, str) or not value:
            raise DomainRejected("CAPITAL_RESULT_INVALID", "capital result must be text")
        return value

    def execute_decimal(
        self,
        *,
        actor_id: UUID,
        account_id: str | None,
        venue: CapitalVenue,
        operation: CapitalOperation,
        parameters: Mapping[str, object],
    ) -> Decimal:
        value = self._execute(
            actor_id=actor_id,
            account_id=account_id,
            venue=venue,
            operation=operation,
            parameters=parameters,
        )
        if isinstance(value, bool):
            raise DomainRejected("CAPITAL_RESULT_INVALID", "capital amount is invalid")
        try:
            return Decimal(str(value))
        except (ArithmeticError, ValueError) as exc:
            raise DomainRejected("CAPITAL_RESULT_INVALID", "capital amount is invalid") from exc

    @staticmethod
    def venue_for_context(context: Mapping[str, object]) -> CapitalVenue:
        path = str(context["path"])
        if "BINANCE" in path:
            return "BINANCE"
        if "HYPERLIQUID" in path:
            return "HYPERLIQUID"
        raise DomainRejected(
            "CAPITAL_ACCOUNT_SCOPE_MISSING",
            "capital path has no exact exchange-account scope",
        )

    def hyperliquid_settings(
        self,
        *,
        actor_id: UUID,
        account_id: str | None,
        direct_settings: Settings,
    ) -> Settings:
        if account_id != direct_settings.capital_direct_hyperliquid_account_id:
            raise DomainRejected(
                "HYPERLIQUID_CAPITAL_ACCOUNT_MISMATCH",
                "the selected account does not match the dedicated capital configuration",
            )
        self.scope(
            actor_id=actor_id,
            account_id=account_id,
            venue="HYPERLIQUID",
        )
        return direct_settings

    def direct_settings(
        self,
        user_id: UUID,
        environment: str = "LIVE",
    ) -> tuple[Settings, dict[str, object] | None]:
        config = self.service().direct_capital_configuration(
            user_id,
            environment,
            include_sensitive_addresses=True,
        )
        if config is None:
            return self.settings, None
        return (
            self.settings.model_copy(
                update={
                    "capital_direct_network": config["network"],
                    "capital_direct_asset": config["asset"],
                    "capital_direct_treasury_provider": config["treasury_provider"],
                    "capital_direct_vault_id": config["vault_id"],
                    "capital_direct_vault_address": config["vault_address"],
                    "capital_direct_owned_arbitrum_address": config["owned_arbitrum_address"],
                    "capital_direct_binance_account_id": config["binance_account_id"],
                    "capital_direct_binance_deposit_address": config["binance_deposit_address"],
                    "capital_direct_binance_withdrawal_address": config[
                        "binance_withdrawal_address"
                    ],
                    "capital_direct_hyperliquid_account_id": config["hyperliquid_account_id"],
                    "capital_direct_hyperliquid_bridge_address": config[
                        "hyperliquid_bridge_address"
                    ],
                    "capital_direct_safe_address": config["safe_address"],
                    "capital_direct_safe_delegate_address": config["safe_delegate_address"],
                    "capital_direct_max_amount": (
                        None if config["max_amount"] is None else Decimal(str(config["max_amount"]))
                    ),
                    "capital_direct_max_fee": (
                        None if config["max_fee"] is None else Decimal(str(config["max_fee"]))
                    ),
                }
            ),
            config,
        )

    def snapshot(self, user_id: UUID) -> dict[str, object]:
        direct_settings, saved_config = self.direct_settings(user_id)
        binance_account_id = direct_settings.capital_direct_binance_account_id
        binance_capital_credentials_configured = bool(
            self.settings.binance_capital_api_key
            and self.settings.binance_capital_api_secret
            and binance_account_id is not None
            and binance_account_id == self.settings.binance_capital_account_id
        )
        configured_chain_id: int | None
        try:
            configured_chain_id = self.notilt_chain_id_for_network(
                direct_settings.capital_direct_network
            )
        except DomainRejected:
            configured_chain_id = None
        configured_vault = (
            None
            if configured_chain_id is None
            else self.settings.notilt_vaults.get(configured_chain_id)
        )
        selected_provider = direct_settings.capital_direct_treasury_provider
        configured_notilt_address = direct_settings.capital_direct_vault_address or configured_vault
        configured_safe_address = direct_settings.capital_direct_safe_address
        selected_treasury_account_id = (
            configured_safe_address
            if selected_provider == "SAFE_SPENDING_LIMIT"
            else configured_notilt_address
        )
        safe_scope_ready = (
            direct_settings.safe_spending_enabled
            and direct_settings.safe_spending_arbitrum_rpc_url is not None
            and direct_settings.capital_direct_safe_address is not None
            and direct_settings.capital_direct_safe_delegate_address is not None
        )
        notilt_scope_ready = (
            direct_settings.notilt_enabled
            and direct_settings.notilt_agent_address is not None
            and configured_notilt_address is not None
        )
        selected_scope_ready = (
            safe_scope_ready if selected_provider == "SAFE_SPENDING_LIMIT" else notilt_scope_ready
        )
        onchain_probe: dict[str, object] = {
            "provider": selected_provider,
            "status": "NOT_ATTEMPTED" if selected_scope_ready else "BLOCKED",
            "error_code": (
                "SAFE_SPENDING_LIMIT_NOT_CONFIGURED"
                if selected_provider == "SAFE_SPENDING_LIMIT" and not safe_scope_ready
                else "NOTILT_VAULT_NOT_CONFIGURED"
                if selected_provider == "NOTILT_VAULT" and not notilt_scope_ready
                else None
            ),
        }
        if selected_provider == "SAFE_SPENDING_LIMIT" and safe_scope_ready:
            safe_rpc_url = direct_settings.safe_spending_arbitrum_rpc_url
            safe_address = direct_settings.capital_direct_safe_address
            safe_delegate = direct_settings.capital_direct_safe_delegate_address
            assert safe_rpc_url is not None
            assert safe_address is not None
            assert safe_delegate is not None
            try:
                safe_fact = self.safe_spending.read_limit(
                    rpc_url=safe_rpc_url,
                    safe=safe_address,
                    delegate=safe_delegate,
                )
                scale = Decimal(10) ** 6
                observed_at = datetime.fromtimestamp(int(str(safe_fact["blockTimestamp"])), UTC)
                safe_balance = Decimal(str(safe_fact["balance"])) / scale
                safe_available_limit = Decimal(str(safe_fact["available"])) / scale
                self.service().record_safe_spending_snapshot(
                    actor_id=user_id,
                    safe_address=safe_address,
                    asset="USDC",
                    balance=safe_balance,
                    available_limit=safe_available_limit,
                    module_enabled=bool(safe_fact["moduleEnabled"]),
                    observed_at=observed_at,
                    now=self.clock(),
                )
            except DomainRejected as exc:
                onchain_probe.update(status="FAILED", error_code=exc.code)
            except (KeyError, TypeError, ValueError, ArithmeticError):
                onchain_probe.update(status="FAILED", error_code="SAFE_RESPONSE_INVALID")
            else:
                onchain_probe.update(
                    status="SUCCESS",
                    error_code=None,
                    module_enabled=bool(safe_fact["moduleEnabled"]),
                    available_limit=str(safe_available_limit),
                    balance=str(safe_balance),
                    reset_time_minutes=int(str(safe_fact["resetTimeMinutes"])),
                    nonce=str(safe_fact["nonce"]),
                    observed_at=observed_at.isoformat(),
                )
        snapshot = self.queries().capital_center(
            user_id,
            authoritative_live_treasury_account_id=selected_treasury_account_id,
            require_authoritative_live_treasury=True,
        )
        expected_interval = self.settings.runtime_sync_interval_seconds
        snapshot["net_worth"]["history_expected_interval_seconds"] = expected_interval
        snapshot["net_worth"]["history_gap_tolerance_seconds"] = max(
            180,
            expected_interval * 3,
        )
        snapshot["net_worth"]["onchain_provider"] = selected_provider
        snapshot["net_worth"]["onchain_probe"] = onchain_probe
        can_manage_direct_configuration = self.service().can_user(user_id, "access.manage")
        notilt_provider_configured = bool(
            direct_settings.capital_direct_vault_id and direct_settings.capital_direct_vault_address
        )
        safe_provider_configured = bool(
            direct_settings.capital_direct_safe_address
            and direct_settings.capital_direct_safe_delegate_address
        )
        public_configuration_values = (
            {
                "vault_id": direct_settings.capital_direct_vault_id,
                "vault_address": direct_settings.capital_direct_vault_address,
                "owned_arbitrum_address": (direct_settings.capital_direct_owned_arbitrum_address),
                "binance_account_id": direct_settings.capital_direct_binance_account_id,
                "binance_deposit_address": (direct_settings.capital_direct_binance_deposit_address),
                "binance_withdrawal_address": (
                    direct_settings.capital_direct_binance_withdrawal_address
                ),
                "hyperliquid_account_id": (direct_settings.capital_direct_hyperliquid_account_id),
                "hyperliquid_bridge_address": (
                    direct_settings.capital_direct_hyperliquid_bridge_address
                ),
                "safe_address": direct_settings.capital_direct_safe_address,
                "safe_delegate_address": (direct_settings.capital_direct_safe_delegate_address),
                "max_amount": (
                    None
                    if direct_settings.capital_direct_max_amount is None
                    else str(direct_settings.capital_direct_max_amount)
                ),
                "max_fee": (
                    None
                    if direct_settings.capital_direct_max_fee is None
                    else str(direct_settings.capital_direct_max_fee)
                ),
            }
            if can_manage_direct_configuration
            else {}
        )
        snapshot["direct_configuration"] = {
            "single_account_mode": False,
            "source": "VERSIONED_DATABASE" if saved_config is not None else "ENVIRONMENT",
            "version": None if saved_config is None else saved_config["version"],
            "effective_at": None if saved_config is None else saved_config["effective_at"],
            "updated_by_username": (
                None if saved_config is None else saved_config["updated_by_username"]
            ),
            "can_manage": can_manage_direct_configuration,
            **public_configuration_values,
            "asset": direct_settings.capital_direct_asset,
            "network": direct_settings.capital_direct_network,
            "treasury_provider": direct_settings.capital_direct_treasury_provider,
            "configured_providers": [
                provider
                for provider, configured in (
                    ("NOTILT_VAULT", notilt_provider_configured),
                    ("SAFE_SPENDING_LIMIT", safe_provider_configured),
                )
                if configured
            ],
            "selected_onchain_account_configured": selected_treasury_account_id is not None,
            "vault_id_configured": direct_settings.capital_direct_vault_id is not None,
            "vault_address_configured": (direct_settings.capital_direct_vault_address is not None),
            "notilt_provider_configured": notilt_provider_configured,
            "owned_arbitrum_address_configured": (
                direct_settings.capital_direct_owned_arbitrum_address is not None
            ),
            "binance_account_configured": (
                direct_settings.capital_direct_binance_account_id is not None
            ),
            "binance_whitelist_destination_configured": (
                direct_settings.capital_direct_binance_deposit_address is not None
            ),
            "binance_withdrawal_destination_configured": (
                direct_settings.capital_direct_binance_withdrawal_address is not None
            ),
            "binance_capital_credentials_configured": (binance_capital_credentials_configured),
            "binance_capital_credentials_source": (
                "DEDICATED_ENVIRONMENT" if binance_capital_credentials_configured else None
            ),
            "binance_capital_submission_enabled": (
                direct_settings.binance_capital_withdraw_enabled
            ),
            "hyperliquid_account_configured": (
                direct_settings.capital_direct_hyperliquid_account_id is not None
            ),
            "hyperliquid_contract_configured": (
                direct_settings.capital_direct_hyperliquid_bridge_address is not None
            ),
            "limits_configured": (
                direct_settings.capital_direct_max_amount is not None
                and direct_settings.capital_direct_max_fee is not None
            ),
            "notilt_sdk_available": self.notilt.available,
            "notilt_scope_configured": (
                self.settings.notilt_enabled
                and configured_notilt_address is not None
                and self.settings.notilt_agent_address is not None
            ),
            "safe_spending_enabled": direct_settings.safe_spending_enabled,
            "safe_gateway_available": self.safe_spending.available,
            "safe_spending_scope_configured": (
                direct_settings.safe_spending_enabled
                and direct_settings.safe_spending_arbitrum_rpc_url is not None
                and direct_settings.capital_direct_safe_address is not None
                and direct_settings.capital_direct_safe_delegate_address is not None
            ),
            "safe_address_configured": direct_settings.capital_direct_safe_address is not None,
            "safe_delegate_configured": (
                direct_settings.capital_direct_safe_delegate_address is not None
            ),
            "safe_provider_configured": safe_provider_configured,
            "signing": False,
            "broadcast": False,
        }
        return snapshot

    def configured_notilt_scope(self, chain_id: int) -> tuple[str, str]:
        if chain_id not in SUPPORTED_NOTILT_CHAINS:
            raise DomainRejected(
                "NOTILT_CHAIN_UNSUPPORTED",
                "NoTilt only supports Ethereum, BNB Smart Chain, and Arbitrum One",
            )
        if not self.settings.notilt_enabled:
            raise DomainRejected("NOTILT_DISABLED", "NoTilt read-only integration is disabled")
        agent = self.settings.notilt_agent_address
        vault = self.settings.notilt_vaults.get(chain_id)
        if agent is None:
            raise DomainRejected(
                "NOTILT_NOT_CONFIGURED",
                "NoTilt public whitelist agent address is not configured",
            )
        if vault is None:
            raise DomainRejected(
                "NOTILT_VAULT_NOT_CONFIGURED",
                f"NoTilt {SUPPORTED_NOTILT_CHAINS[chain_id]} Vault is not configured",
            )
        return agent, vault

    def notilt_chain_id_for_network(self, network: str) -> int:
        normalized = network.upper().replace("-", "_").replace(" ", "_")
        chain_id = {
            "ETH": 1,
            "ETHEREUM": 1,
            "BNB": 56,
            "BSC": 56,
            "BNB_SMART_CHAIN": 56,
            "ARB": 42161,
            "ARBITRUM": 42161,
            "ARBITRUM_ONE": 42161,
        }.get(normalized)
        if chain_id is None:
            raise DomainRejected(
                "NOTILT_CHAIN_UNSUPPORTED",
                "NoTilt network must be Ethereum, BNB Smart Chain, or Arbitrum One",
            )
        return chain_id

    def verify_live_notilt_release_budget(
        self,
        *,
        chain_id: int,
        vault: str,
        agent: str,
        asset: str,
        amount: Decimal,
        max_fact_age_seconds: int,
        now: datetime,
    ) -> None:
        snapshot = self.notilt.read_vault(chain_id, vault, agent)
        budget = next(
            (item for item in snapshot.budgets if item.asset == asset.upper()),
            None,
        )
        if budget is None:
            raise DomainRejected(
                "NOTILT_RELEASE_BUDGET_MISSING",
                "NoTilt release requires a live budget for the configured asset",
            )
        if (
            snapshot.vault.lower() != vault.lower()
            or snapshot.agent.lower() != agent.lower()
            or budget.vault.lower() != vault.lower()
            or budget.agent.lower() != agent.lower()
        ):
            raise DomainRejected(
                "NOTILT_RELEASE_SCOPE_MISMATCH",
                "NoTilt live budget does not match the configured Vault and Agent scope",
            )
        if not budget.is_official_vault:
            raise DomainRejected(
                "NOTILT_VAULT_UNTRUSTED",
                "NoTilt release requires an official Vault from the trusted deployment catalog",
            )
        if (
            not budget.is_active_whitelist
            or budget.assigned_whitelist_vault.lower() != vault.lower()
        ):
            raise DomainRejected(
                "NOTILT_WHITELIST_INACTIVE",
                "NoTilt release requires an active whitelist assigned to the configured Vault",
            )
        if budget.owner.lower() == agent.lower():
            raise DomainRejected(
                "NOTILT_AGENT_OWNER_FORBIDDEN",
                "NoTilt Agent budget cannot use the Vault owner identity",
            )
        if budget.panic_locked:
            raise DomainRejected("NOTILT_PANIC_LOCKED", "NoTilt Vault is panic locked")
        fact_age = now - budget.block_timestamp
        if fact_age < timedelta(0) or fact_age > timedelta(seconds=max_fact_age_seconds):
            raise DomainRejected(
                "NOTILT_FACT_STALE",
                "NoTilt live budget is outside the active production freshness window",
            )
        if amount > budget.max_release_net:
            raise DomainRejected(
                "NOTILT_RELEASE_LIMIT_EXCEEDED",
                "NoTilt release amount exceeds the current live maxReleaseNet allowance",
            )

    def sync_configured_notilt_vault(
        self,
        chain_id: int,
        actor_id: UUID,
        *,
        now: datetime,
    ) -> tuple[int, dict[str, object]]:
        agent, vault = self.configured_notilt_scope(chain_id)
        snapshot = self.notilt.read_vault(chain_id, vault, agent)
        valuations = {
            budget.asset: (
                self.notilt_valuator.value(
                    budget.asset,
                    budget.balance,
                    now=now,
                    mark_price=None if mark is None else mark[0],
                    mark_observed_at=None if mark is None else mark[1],
                )
                if (mark := self.queries().native_asset_mark(actor_id, budget.asset)) is not None
                or budget.asset.upper() in {"USD", "USDC", "USDT", "USDT0"}
                else self.notilt_valuator.value(
                    budget.asset,
                    budget.balance,
                    now=now,
                )
            )
            for budget in snapshot.budgets
        }
        fact_ids = self.service().record_notilt_vault_snapshot(
            actor_id=actor_id,
            snapshot=snapshot,
            valuations=valuations,
            now=now,
        )
        return len(fact_ids), self.snapshot(actor_id)


__all__ = ["CapitalApplicationRuntime"]
