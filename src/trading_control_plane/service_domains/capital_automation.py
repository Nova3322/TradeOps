from __future__ import annotations

from trading_control_plane.service_component import ServiceComponent

# The domain implementation intentionally consumes the explicit service_core export surface.
# ruff: noqa: F403, F405
from trading_control_plane.service_core import *


class AutomationCapitalService(ServiceComponent):
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
            team = self.transactions._require_role(session, actor_id, operation, account_id, venue)
            self.facade._ensure_exchange_account_reference(
                session,
                team=team,
                actor_id=actor_id,
                account_id=account_id,
                venue=venue,
                environment=environment.value,
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
                event_type="CAPITAL_AUTOMATION_POLICY_SET",
                object_type="CapitalAutomationPolicy",
                object_id=policy.policy_id,
                reason="TESTNET thresholds frozen; both automation gates remain independent",
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
            team = self.transactions._require_role(
                session,
                actor_id,
                operation,
                policy.account_id,
                policy.venue,
                team_id=policy.team_id,
            )
            digest, response = self.transactions._idempotency(
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
            self.facade._assert_capital_scope_flat(
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
            venue_fact = self.facade._capital_balance(
                session,
                team_id=team.team_id,
                environment=policy.environment,
                endpoint_type="VENUE",
                endpoint_id=policy.account_id,
                venue=policy.venue,
                asset=policy.asset,
                lock=True,
            )
            vault_fact = self.facade._capital_balance(
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
            self.transactions._require_role(session, actor_id, "capability.manage")
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
            self.transactions._audit(
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
