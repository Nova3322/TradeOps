from __future__ import annotations

from trading_control_plane.service_component import ServiceComponent

# The domain implementation intentionally consumes the explicit service_core export surface.
# ruff: noqa: F403, F405
from trading_control_plane.service_core import *


class TransferCapitalService(ServiceComponent):
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
            team = self.transactions._require_role(session, actor_id, operation, account_id, venue)
            self._ensure_exchange_account_reference(
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
            team = self.transactions._require_role(
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
            self.transactions._audit(
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
            team = self.transactions._require_role(
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
            self.transactions._audit(
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
            team = self.transactions._require_role(
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
            digest, response = self.transactions._idempotency(
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
            team = self.transactions._require_role(
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
            digest, response = self.transactions._idempotency(
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
            self.transactions._require_role(
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
            self.transactions._require_role(
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
            self.transactions._audit(
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
            self.transactions._require_role(
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
            self.transactions._audit(
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
