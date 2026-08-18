from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select, text

from trading_control_plane import capital, domain, models, notilt, rejections
from trading_control_plane import execution_scope as scope_rules
from trading_control_plane.service_component import ServiceComponent
from trading_control_plane.service_domains.execution_facts import record_account_equity_observation


class NoTiltCapitalService(ServiceComponent):
    def record_notilt_vault_snapshot(
        self,
        *,
        actor_id: UUID,
        snapshot: notilt.NoTiltVaultSnapshot,
        valuations: dict[str, notilt.UsdValuation],
        now: datetime,
    ) -> tuple[UUID, ...]:
        if not snapshot.budgets:
            rejections.reject("NOTILT_FACT_INVALID", "NoTilt snapshot must contain catalog assets")
        with self.database.session_factory.begin() as session:
            team = self.transactions.require_role(session, actor_id, "capital.fact.record")
            fact_ids: list[UUID] = []
            for budget in snapshot.budgets:
                if (
                    not budget.is_official_vault
                    or budget.chain_id != snapshot.chain_id
                    or budget.vault.lower() != snapshot.vault.lower()
                    or budget.agent.lower() != snapshot.agent.lower()
                ):
                    rejections.reject(
                        "NOTILT_VAULT_UNVERIFIED",
                        "NoTilt facts must belong to one official configured Vault",
                    )
                if budget.block_timestamp > now + scope_rules.MAX_FACT_CLOCK_SKEW:
                    rejections.reject(
                        "FACT_TIME_INVALID", "NoTilt block time cannot be in the future"
                    )
                valuation = valuations.get(budget.asset)
                if (
                    valuation is None
                    or valuation.observed_at > now + scope_rules.MAX_FACT_CLOCK_SKEW
                ):
                    rejections.reject(
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
                    select(models.AccountEquity)
                    .where(
                        models.AccountEquity.team_id == team.team_id,
                        models.AccountEquity.environment == domain.ExecutionEnvironment.LIVE.value,
                        models.AccountEquity.account_id == snapshot.vault,
                        models.AccountEquity.venue == "VAULT",
                        models.AccountEquity.currency == budget.asset,
                    )
                    .with_for_update()
                )
                if fact is None:
                    fact = models.AccountEquity(
                        team_id=team.team_id,
                        account_id=snapshot.vault,
                        venue="VAULT",
                        environment=domain.ExecutionEnvironment.LIVE.value,
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
                        fact_status=domain.FactStatus.KNOWN.value,
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
                    fact.fact_status = domain.FactStatus.KNOWN.value
                    fact.observed_at = budget.block_timestamp
                    fact.updated_at = now
                record_account_equity_observation(session, fact, recorded_at=now)
                self.transactions.audit(
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
            rejections.reject(
                "SAFE_FACT_INVALID", "Safe balance and spending limit cannot be negative"
            )
        if observed_at > now + scope_rules.MAX_FACT_CLOCK_SKEW:
            rejections.reject("FACT_TIME_INVALID", "Safe block time cannot be in the future")
        normalized_asset = asset.upper()
        if normalized_asset not in notilt.USD_STABLE_ASSETS:
            rejections.reject(
                "SAFE_ASSET_UNSUPPORTED", "Safe treasury snapshot requires a USD asset"
            )
        with self.database.session_factory.begin() as session:
            team = self.transactions.require_role(session, actor_id, "capital.fact.record")
            fact = session.scalar(
                select(models.AccountEquity)
                .where(
                    models.AccountEquity.team_id == team.team_id,
                    models.AccountEquity.environment == domain.ExecutionEnvironment.LIVE.value,
                    models.AccountEquity.account_id == safe_address,
                    models.AccountEquity.venue == "VAULT",
                    models.AccountEquity.currency == normalized_asset,
                )
                .with_for_update()
            )
            withdrawable = min(balance, available_limit) if module_enabled else Decimal(0)
            if fact is None:
                fact = models.AccountEquity(
                    team_id=team.team_id,
                    account_id=safe_address,
                    venue="VAULT",
                    environment=domain.ExecutionEnvironment.LIVE.value,
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
                    fact_status=domain.FactStatus.KNOWN.value,
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
                fact.fact_status = domain.FactStatus.KNOWN.value
                fact.observed_at = observed_at
                fact.updated_at = now
            observation_exists = session.scalar(
                select(models.AccountEquityObservation.observation_id).where(
                    models.AccountEquityObservation.team_id == team.team_id,
                    models.AccountEquityObservation.account_equity_id == fact.account_equity_id,
                    models.AccountEquityObservation.observed_at == observed_at,
                )
            )
            record_account_equity_observation(session, fact, recorded_at=now)
            if observation_exists is None:
                self.transactions.audit(
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

    def notilt_transfer_command(
        self, capital_transfer_id: UUID, actor_id: UUID
    ) -> capital.CapitalTransferCommand:
        with self.database.session_factory() as session:
            transfer = session.get(models.CapitalTransfer, capital_transfer_id)
            if transfer is None:
                rejections.reject("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer does not exist")
            authorization = session.get(
                models.TransferAuthorization, transfer.transfer_authorization_id
            )
            if authorization is None:
                rejections.reject(
                    "TRANSFER_AUTHORIZATION_NOT_FOUND", "transfer authorization is missing"
                )
            self.transactions.require_role(
                session,
                actor_id,
                "capital.execute",
                transfer.account_id,
                transfer.venue,
                team_id=transfer.team_id,
            )
            if (
                transfer.transport != "NOTILT"
                or transfer.environment != domain.ExecutionEnvironment.LIVE.value
            ):
                rejections.reject(
                    "NOTILT_TRANSFER_STATE_INVALID", "capital transfer is not a NoTilt flow"
                )
            return capital.CapitalTransferCommand(
                capital_transfer_id=transfer.capital_transfer_id,
                environment=domain.ExecutionEnvironment(transfer.environment),
                direction=domain.CapitalDirection(transfer.direction),
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
        transactions: tuple[notilt.NoTiltUnsignedTransaction, ...],
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
            rejections.reject("NOTILT_PLAN_INVALID", "NoTilt transaction plan is invalid")
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
            rejections.reject(
                "NOTILT_PLAN_INVALID", "NoTilt plan contains an unexpected transaction"
            )
        planned = [item.to_dict() for item in transactions]
        with self.database.session_factory.begin() as session:
            transfer = session.get(
                models.CapitalTransfer, capital_transfer_id, with_for_update=True
            )
            if transfer is None:
                rejections.reject("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer does not exist")
            self.transactions.require_role(
                session,
                actor_id,
                "capital.execute",
                transfer.account_id,
                transfer.venue,
                team_id=transfer.team_id,
            )
            if transfer.environment != domain.ExecutionEnvironment.LIVE.value:
                rejections.reject(
                    "NOTILT_TRANSFER_ENVIRONMENT_INVALID",
                    "NoTilt plans require a LIVE capital transfer",
                )
            expected_direction = (
                domain.CapitalDirection.VENUE_TO_VAULT.value
                if transport_state == "DEPOSIT_PLAN_READY"
                else domain.CapitalDirection.VAULT_TO_VENUE.value
            )
            if transfer.direction != expected_direction:
                rejections.reject(
                    "NOTILT_PLAN_DIRECTION_INVALID", "NoTilt plan direction does not match"
                )
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
                rejections.reject(
                    "NOTILT_PLAN_STATE_INVALID", "NoTilt plan is not valid in this state"
                )
            if (
                transport_state
                in {
                    "DEPOSIT_PLAN_READY",
                    "RELEASE_REQUEST_PLAN_READY",
                }
                and transfer.status != domain.CapitalTransferStatus.SOURCE_RESERVED.value
            ):
                rejections.reject(
                    "NOTILT_PLAN_STATE_INVALID", "initial NoTilt plan is no longer available"
                )
            if transport_state in {
                "RELEASE_EXECUTION_PLAN_READY",
                "RELEASE_CANCELLATION_PLAN_READY",
            } and transfer.status not in {
                domain.CapitalTransferStatus.IN_FLIGHT.value,
                domain.CapitalTransferStatus.MANUAL_REQUIRED.value,
            }:
                rejections.reject(
                    "NOTILT_PLAN_STATE_INVALID", "release request is not awaiting resolution"
                )
            if transfer.transport_state == transport_state:
                if (
                    transfer.transport == "NOTILT"
                    and transfer.chain_id == chain_id
                    and transfer.planned_transactions == planned
                ):
                    return
                rejections.reject(
                    "NOTILT_PLAN_IDENTITY_CONFLICT", "NoTilt plan changed for the same stage"
                )
            transfer.transport = "NOTILT"
            transfer.chain_id = chain_id
            transfer.transport_state = transport_state
            transfer.planned_transactions = planned
            transfer.updated_at = now
            transfer.version += 1
            self.transactions.audit(
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
        receipt: notilt.NoTiltReceipt,
        *,
        now: datetime,
    ) -> str:
        with self.database.session_factory.begin() as session:
            transfer = session.get(
                models.CapitalTransfer, capital_transfer_id, with_for_update=True
            )
            if transfer is None:
                rejections.reject("CAPITAL_TRANSFER_NOT_FOUND", "capital transfer does not exist")
            self.transactions.require_role(
                session, actor_id, "capital.reconcile", transfer.account_id, transfer.venue
            )
            authorization = session.get(
                models.TransferAuthorization, transfer.transfer_authorization_id
            )
            if authorization is None:
                rejections.reject(
                    "TRANSFER_AUTHORIZATION_NOT_FOUND", "transfer authorization is missing"
                )
            if (
                transfer.transport != "NOTILT"
                or transfer.chain_id != receipt.chain_id
                or transfer.environment != domain.ExecutionEnvironment.LIVE.value
            ):
                rejections.reject(
                    "NOTILT_RECEIPT_SCOPE_MISMATCH", "receipt is outside the NoTilt transfer"
                )
            vault = (
                transfer.source_id
                if transfer.direction == domain.CapitalDirection.VAULT_TO_VENUE.value
                else transfer.destination_id
            )
            if receipt.vault.lower() != vault.lower():
                rejections.reject("NOTILT_RECEIPT_SCOPE_MISMATCH", "receipt Vault does not match")
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {
                    "key": scope_rules.advisory_lock_key(
                        str(receipt.chain_id),
                        "notilt-receipt",
                        receipt.transaction_hash,
                    )
                },
            )
            replay = session.scalar(
                select(models.CapitalTransfer.capital_transfer_id)
                .where(
                    models.CapitalTransfer.capital_transfer_id != capital_transfer_id,
                    models.CapitalTransfer.chain_id == receipt.chain_id,
                    models.CapitalTransfer.confirmed_transaction_hashes.contains(
                        [receipt.transaction_hash]
                    ),
                )
                .limit(1)
            )
            if replay is not None:
                rejections.reject(
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
                rejections.reject(
                    "NOTILT_RECEIPT_STATE_INVALID", "receipt is unexpected for this transfer"
                )
            if receipt.block_timestamp > now + scope_rules.MAX_FACT_CLOCK_SKEW:
                rejections.reject(
                    "FACT_TIME_INVALID", "NoTilt receipt time cannot be in the future"
                )

            if receipt.receipt_kind == "DEPOSIT":
                if (
                    transfer.direction != domain.CapitalDirection.VENUE_TO_VAULT.value
                    or receipt.asset != transfer.asset
                    or receipt.requested_amount != authorization.min_received
                    or receipt.credited_amount != authorization.min_received
                ):
                    rejections.reject(
                        "NOTILT_RECEIPT_AMOUNT_INVALID",
                        "NoTilt deposit receipt is outside the authorization",
                    )
                fee = transfer.gross_amount - receipt.credited_amount
                if fee < 0 or fee > authorization.max_fee:
                    rejections.reject(
                        "CAPITAL_DESTINATION_AMOUNT_INVALID",
                        "NoTilt credited amount exceeds the authorized fee budget",
                    )
                transfer.fee_amount = fee
                transfer.net_received = receipt.credited_amount
                transfer.status = domain.CapitalTransferStatus.DESTINATION_CONFIRMED.value
                transfer.transport_state = "DEPOSIT_CONFIRMED"
                transfer.external_transfer_id = receipt.transaction_hash
            elif receipt.receipt_kind == "RELEASE_REQUEST":
                if (
                    transfer.direction != domain.CapitalDirection.VAULT_TO_VENUE.value
                    or receipt.asset != transfer.asset
                    or receipt.request_id is None
                    or receipt.net_amount != authorization.min_received
                    or receipt.fee is None
                    or receipt.execute_after is None
                    or receipt.expires_at is None
                    or receipt.execute_after >= receipt.expires_at
                ):
                    rejections.reject(
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
                    domain.CapitalTransferStatus.MANUAL_REQUIRED.value
                    if (
                        receipt.fee > authorization.max_fee
                        or receipt.net_amount + receipt.fee > transfer.gross_amount
                    )
                    else domain.CapitalTransferStatus.IN_FLIGHT.value
                )
            elif receipt.receipt_kind == "RELEASE_EXECUTION":
                if (
                    transfer.direction != domain.CapitalDirection.VAULT_TO_VENUE.value
                    or receipt.request_id != transfer.protocol_request_id
                    or transfer.protocol_execute_after is None
                    or transfer.protocol_expires_at is None
                    or receipt.block_timestamp < transfer.protocol_execute_after
                    or receipt.block_timestamp >= transfer.protocol_expires_at
                    or transfer.fee_amount is None
                    or transfer.fee_amount > authorization.max_fee
                ):
                    rejections.reject(
                        "NOTILT_RECEIPT_REQUEST_INVALID",
                        "NoTilt release execution is outside the authorized request",
                    )
                transfer.transport_state = "RELEASE_EXECUTION_CONFIRMED"
                transfer.status = domain.CapitalTransferStatus.IN_FLIGHT.value
            else:
                if receipt.request_id != transfer.protocol_request_id:
                    rejections.reject(
                        "NOTILT_RECEIPT_REQUEST_INVALID",
                        "NoTilt cancellation request identity does not match",
                    )
                transfer.transport_state = "RELEASE_CANCELLED"
                transfer.status = domain.CapitalTransferStatus.FAILED_SOURCE_RESTORED.value

            confirmed.append(receipt.transaction_hash)
            transfer.confirmed_transaction_hashes = confirmed
            transfer.transaction_reference = receipt.transaction_hash
            transfer.observed_at = receipt.block_timestamp
            transfer.updated_at = now
            transfer.version += 1
            self.transactions.audit(
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
