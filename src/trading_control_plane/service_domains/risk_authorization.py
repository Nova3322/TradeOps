from __future__ import annotations

from trading_control_plane.service_component import ServiceComponent

# The domain implementation intentionally consumes the explicit service_core export surface.
# ruff: noqa: F403, F405
from trading_control_plane.service_core import *


class AuthorizationRiskService(ServiceComponent):
    def issue_authorization(
        self,
        *,
        proposal_id: UUID,
        actor_id: UUID,
        expires_at: datetime,
        allowed_adds: int,
        idempotency_key: str,
        now: datetime,
    ) -> UUID:
        payload = {
            "proposal_id": str(proposal_id),
            "expires_at": expires_at.isoformat(),
            "allowed_adds": allowed_adds,
        }
        operation = "authorization.issue"
        with self.database.session_factory.begin() as session:
            proposal = session.get(Proposal, proposal_id)
            if proposal is None:
                _reject("PROPOSAL_NOT_FOUND", "proposal does not exist")
            self.transactions._require_role(
                session,
                actor_id,
                operation,
                proposal.account_id,
                proposal.venue,
                team_id=proposal.team_id,
            )
            digest, response = self.transactions._idempotency(
                session,
                caller_id=f"{actor_id}:{proposal.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload={**payload, "team_id": str(proposal.team_id)},
            )
            if response is not None:
                return _as_uuid(str(response["authorization_id"]))
            self.transactions._lock_risk_capacity(session, proposal.team_id)
            policy = self.facade._active_risk_policy(session, proposal.team_id)
            if policy.system_state != SystemRiskState.NORMAL.value:
                _reject(
                    "AUTHORIZATION_RISK_STATE_INVALID",
                    "new authorization requires the current NORMAL risk policy",
                )
            if proposal.status != ProposalStatus.APPROVED.value:
                _reject("PROPOSAL_NOT_APPROVED", "authorization requires approved proposal")
            decision = session.scalar(
                select(RiskDecision)
                .where(RiskDecision.proposal_id == proposal_id)
                .order_by(RiskDecision.created_at.desc())
                .limit(1)
            )
            if decision is None or decision.result == RiskResult.DENY.value:
                _reject("RISK_DECISION_NOT_ALLOWING", "latest risk decision does not allow risk")
            frozen_policy = decision.input_data.get("policy")
            if not isinstance(frozen_policy, dict) or (
                frozen_policy.get("policy_id") != str(policy.policy_id)
                or frozen_policy.get("version") != policy.version
                or frozen_policy.get("revision") != policy.revision
            ):
                _reject(
                    "RISK_DECISION_CONTROL_CHANGED",
                    "risk controls changed after the latest decision; a new decision is required",
                )
            if expires_at <= now or expires_at > proposal.expires_at:
                _reject("AUTHORIZATION_EXPIRY_INVALID", "authorization must be short-lived")
            details = proposal.frozen_payload.get("details")
            management = details if isinstance(details, dict) else {}
            requested_adds_raw = management.get("requested_adds", 0)
            try:
                requested_adds = int(requested_adds_raw)
            except (TypeError, ValueError):
                _reject(
                    "PROPOSAL_ADD_CONTRACT_INVALID",
                    "frozen proposal AddUnit request is invalid",
                )
            tier_limit = MAX_ADD_UNITS[RiskTier(proposal.risk_tier)]
            proposal_limit = min(requested_adds, tier_limit)
            if (
                allowed_adds < 0
                or allowed_adds > proposal_limit
                or (allowed_adds > 0 and management.get("allow_auto_add") is not True)
            ):
                _reject(
                    "AUTHORIZATION_ADD_LIMIT_INVALID",
                    "allowed Add count exceeds the frozen proposal and risk tier",
                )
            if allowed_adds > 0:
                auto_add_gate = session.get(CapabilityGate, "AUTO_ADD", with_for_update=True)
                if auto_add_gate is None or auto_add_gate.status != CapabilityStatus.ENABLED.value:
                    _reject(
                        "AUTO_ADD_DISABLED",
                        "new AddUnit authorization requires the current AUTO_ADD gate",
                    )
            authorization = TradingAuthorization(
                team_id=proposal.team_id,
                proposal_id=proposal_id,
                risk_decision_id=decision.decision_id,
                account_id=proposal.account_id,
                venue=proposal.venue,
                environment=proposal.environment,
                instrument_id=proposal.instrument_id,
                direction=proposal.direction,
                quantity_limit=decision.approved_quantity,
                used_quantity=Decimal(0),
                risk_limit=decision.risk_amount,
                expires_at=expires_at,
                allowed_adds=allowed_adds,
                used_adds=0,
                add_revoked_at=None,
                active=True,
                actor_id=str(actor_id),
                created_at=now,
            )
            session.add(authorization)
            session.flush()
            result = {"authorization_id": str(authorization.authorization_id)}
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{proposal.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="AUTHORIZATION_ISSUED",
                object_type="TradingAuthorization",
                object_id=authorization.authorization_id,
                reason="approved proposal and risk decision",
                correlation_id=proposal.correlation_id,
                object_version=1,
                idempotency_key=idempotency_key,
                now=now,
            )
            return authorization.authorization_id

    @staticmethod
    def _intent_creation(response: dict[str, Any]) -> IntentCreation:
        return IntentCreation(
            campaign_id=_as_uuid(str(response["campaign_id"])),
            reservation_id=_as_uuid(str(response["reservation_id"])),
            intent_id=_as_uuid(str(response["intent_id"])),
        )

    @staticmethod
    def _proposal_limit_price(proposal: Proposal) -> Decimal | None:
        details = proposal.frozen_payload.get("details")
        if not isinstance(details, dict) or details.get("limit_price") is None:
            return None
        try:
            value = Decimal(str(details["limit_price"]))
        except (ArithmeticError, TypeError, ValueError):
            _reject("PROPOSAL_PRICE_INVALID", "frozen proposal limit price is invalid")
        if not value.is_finite() or value <= 0:
            _reject("PROPOSAL_PRICE_INVALID", "frozen proposal limit price is invalid")
        return value

    @staticmethod
    def _proposal_detail_decimal(proposal: Proposal, key: str) -> Decimal:
        details = proposal.frozen_payload.get("details")
        if not isinstance(details, dict) or details.get(key) is None:
            _reject(
                "PROPOSAL_ADD_CONTRACT_INVALID",
                f"frozen proposal is missing {key}",
            )
        try:
            value = Decimal(str(details[key]))
        except (ArithmeticError, TypeError, ValueError):
            _reject("PROPOSAL_ADD_CONTRACT_INVALID", f"frozen {key} is invalid")
        if not value.is_finite() or value <= 0:
            _reject("PROPOSAL_ADD_CONTRACT_INVALID", f"frozen {key} is invalid")
        return value

    @staticmethod
    def _validate_add_candidate(
        *,
        proposal: Proposal,
        instrument: Instrument,
        candidate: AddCandidateFacts | None,
        policy: RiskPolicy,
        now: datetime,
    ) -> None:
        details = proposal.frozen_payload.get("details")
        if not isinstance(details, dict) or details.get("allow_auto_add") is not True:
            _reject("PROPOSAL_AUTO_ADD_DISABLED", "frozen proposal does not allow AUTO_ADD")
        if candidate is None:
            _reject(
                "AUTO_ADD_CANDIDATE_REQUIRED",
                "ADD requires a current Perptape candidate at the Trading boundary",
            )
        if (
            candidate.readiness != "READY"
            or candidate.venue != proposal.venue
            or candidate.symbol != instrument.symbol
            or candidate.direction.value != proposal.direction
        ):
            _reject(
                "AUTO_ADD_CANDIDATE_SCOPE_INVALID",
                "Perptape candidate does not match the frozen Campaign scope",
            )
        if proposal.source == ProposalSource.SYSTEM.value and (
            candidate.contract_version != proposal.strategy_version
            or proposal.source_candidate_id
            in {candidate.candidate_id, candidate.legacy_candidate_id}
        ):
            _reject(
                "AUTO_ADD_CANDIDATE_VERSION_INVALID",
                "SYSTEM Add requires a new candidate from the frozen Perptape contract",
            )
        baseline = proposal.source_observed_at or proposal.frozen_at or proposal.created_at
        if proposal.source == ProposalSource.SYSTEM.value and candidate.observed_at <= baseline:
            _reject(
                "AUTO_ADD_CANDIDATE_NOT_SUBSEQUENT",
                "Add candidate must be newer than the frozen Proposal facts",
            )
        age = now - candidate.observed_at
        if age < timedelta(0) or age > timedelta(seconds=policy.max_fact_age_seconds):
            _reject("AUTO_ADD_CANDIDATE_STALE", "Add candidate is not a current fact")
        trigger_price = AuthorizationRiskService._proposal_detail_decimal(
            proposal, "add_trigger_price"
        )
        if proposal.direction == Direction.LONG.value:
            passed = candidate.reference_price >= trigger_price
        else:
            passed = candidate.reference_price <= trigger_price
        if not passed:
            _reject(
                "AUTO_ADD_TRIGGER_NOT_MET",
                "Perptape candidate has not reached the frozen favorable-price gate",
            )

    def disable_campaign_auto_add(
        self,
        campaign_id: UUID,
        actor_id: UUID,
        idempotency_key: str,
        *,
        reason: str,
        expected_target_version: int | None = None,
        now: datetime,
    ) -> int:
        operation = "campaign.auto_add.disable"
        payload = {
            "campaign_id": str(campaign_id),
            "reason": reason,
            "expected_target_version": expected_target_version,
        }
        with self.database.session_factory.begin() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self.transactions._require_role(
                session,
                actor_id,
                "risk.tighten",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            digest, response = self.transactions._idempotency(
                session,
                caller_id=f"{actor_id}:{campaign.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload={**payload, "team_id": str(campaign.team_id)},
            )
            if response is not None:
                return int(response["allowed_adds"])
            self.transactions._lock_risk_capacity(session, campaign.team_id)
            authorization = session.get(
                TradingAuthorization, campaign.authorization_id, with_for_update=True
            )
            campaign = session.get(Campaign, campaign_id, with_for_update=True)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            if (
                expected_target_version is not None
                and campaign.target_version != expected_target_version
            ):
                _reject("VERSION_CONFLICT", "Campaign target changed before the action")
            if authorization is None or authorization.authorization_id != campaign.authorization_id:
                _reject("AUTHORIZATION_INACTIVE", "campaign authorization is missing")
            unresolved_add = session.scalar(
                select(OrderIntent.intent_id).where(
                    OrderIntent.campaign_id == campaign_id,
                    OrderIntent.kind == IntentKind.ADD.value,
                    OrderIntent.add_unit_consumed.is_(False),
                    OrderIntent.status.in_(ACTIVE_INTENT_STATUSES),
                )
            )
            authorization.add_revoked_at = now
            authorization.active = False
            result = {
                "allowed_adds": authorization.used_adds + (1 if unresolved_add is not None else 0)
            }
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{campaign.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="CAMPAIGN_AUTO_ADD_DISABLED",
                object_type="Campaign",
                object_id=campaign.campaign_id,
                reason=reason,
                correlation_id=uuid4(),
                object_version=campaign.target_version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return int(result["allowed_adds"])

    def disable_global_auto_add(
        self,
        actor_id: UUID,
        idempotency_key: str,
        *,
        reason: str,
        now: datetime,
    ) -> None:
        operation = "auto_add.disable"
        payload = {"reason": reason}
        with self.database.session_factory.begin() as session:
            _actor, _workspace, team = self.transactions._active_scope(session, actor_id)
            assert team is not None
            if not session.scalar(
                select(RoleAssignment.assignment_id).where(
                    RoleAssignment.user_id == actor_id,
                    RoleAssignment.team_id == team.team_id,
                    RoleAssignment.role == Role.SYSTEM_ADMIN.value,
                )
            ):
                _reject(
                    "RISK_CHANGE_REVIEW_REQUIRED",
                    "non-admin risk control changes require independent review",
                )
            digest, response = self.transactions._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return
            self.transactions._lock_risk_capacity(session)
            gate = session.get(CapabilityGate, "AUTO_ADD", with_for_update=True)
            if gate is None:
                _reject("CAPABILITY_GATE_NOT_FOUND", "AUTO_ADD gate is missing")
            gate.status = CapabilityStatus.DISABLED.value
            gate.reason = reason
            gate.operator_id = str(actor_id)
            gate.version += 1
            gate.updated_at = now
            authorizations = session.scalars(
                select(TradingAuthorization)
                .where(TradingAuthorization.add_revoked_at.is_(None))
                .order_by(TradingAuthorization.authorization_id)
                .with_for_update()
            ).all()
            for authorization in authorizations:
                authorization.add_revoked_at = now
            self.transactions._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response={"status": gate.status},
                now=now,
            )
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="AUTO_ADD_DISABLED",
                object_type="CapabilityGate",
                object_id="AUTO_ADD",
                reason=reason,
                correlation_id=uuid4(),
                object_version=gate.version,
                idempotency_key=idempotency_key,
                now=now,
            )

    def pause_new_risk(
        self,
        actor_id: UUID,
        idempotency_key: str,
        *,
        reason: str,
        now: datetime,
    ) -> SystemRiskState:
        operation = "risk.pause_new"
        payload = {"reason": reason}
        with self.database.session_factory.begin() as session:
            _actor, _workspace, team = self.transactions._active_scope(session, actor_id)
            assert team is not None
            if not session.scalar(
                select(RoleAssignment.assignment_id).where(
                    RoleAssignment.user_id == actor_id,
                    RoleAssignment.team_id == team.team_id,
                    RoleAssignment.role == Role.SYSTEM_ADMIN.value,
                )
            ):
                _reject(
                    "RISK_CHANGE_REVIEW_REQUIRED",
                    "non-admin risk control changes require independent review",
                )
            digest, response = self.transactions._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return SystemRiskState(str(response["system_state"]))
            self.transactions._lock_risk_capacity(session, team.team_id)
            policy = self.facade._active_risk_policy(session, team.team_id)
            if policy.system_state != SystemRiskState.KILL_SWITCH.value:
                policy.system_state = SystemRiskState.REDUCE_ONLY.value
                policy.revision += 1
                policy.reason = reason
                policy.updated_by = str(actor_id)
                policy.updated_at = now
            authorizations = session.scalars(
                select(TradingAuthorization)
                .where(
                    TradingAuthorization.team_id == team.team_id,
                    TradingAuthorization.active,
                    TradingAuthorization.expires_at > now,
                )
                .order_by(TradingAuthorization.authorization_id)
                .with_for_update()
            ).all()
            for authorization in authorizations:
                authorization.active = False
                if authorization.add_revoked_at is None:
                    authorization.add_revoked_at = now
            result = {"system_state": policy.system_state}
            self.transactions._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="NEW_RISK_PAUSED",
                object_type="RiskPolicy",
                object_id=policy.policy_id,
                reason=reason,
                correlation_id=uuid4(),
                object_version=policy.revision,
                idempotency_key=idempotency_key,
                now=now,
            )
            return SystemRiskState(policy.system_state)

    def enable_global_auto_add(
        self,
        actor_id: UUID,
        idempotency_key: str,
        *,
        reason: str,
        now: datetime,
    ) -> None:
        operation = "auto_add.enable"
        payload = {"reason": reason}
        with self.database.session_factory.begin() as session:
            team = self.transactions._require_role(session, actor_id, "risk.restore.direct")
            if not session.scalar(
                select(RoleAssignment.assignment_id).where(
                    RoleAssignment.user_id == actor_id,
                    RoleAssignment.team_id == team.team_id,
                    RoleAssignment.role == Role.SYSTEM_ADMIN.value,
                )
            ):
                _reject("RISK_CHANGE_REVIEW_REQUIRED", "SYSTEM_ADMIN is required")
            digest, response = self.transactions._idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return
            self.transactions._lock_risk_capacity(session, team.team_id)
            policy = self.facade._active_risk_policy(session, team.team_id)
            if policy.system_state != SystemRiskState.NORMAL.value:
                _reject("RISK_RESTORE_BLOCKED", "risk policy must be NORMAL")
            gate = session.get(CapabilityGate, "AUTO_ADD", with_for_update=True)
            if gate is None:
                _reject("CAPABILITY_GATE_NOT_FOUND", "AUTO_ADD gate is missing")
            gate.status = CapabilityStatus.ENABLED.value
            gate.reason = reason
            gate.operator_id = str(actor_id)
            gate.version += 1
            gate.updated_at = now
            self.transactions._save_receipt(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response={"status": gate.status},
                now=now,
            )
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="AUTO_ADD_ENABLED",
                object_type="CapabilityGate",
                object_id="AUTO_ADD",
                reason=reason,
                correlation_id=uuid4(),
                object_version=gate.version,
                idempotency_key=idempotency_key,
                now=now,
            )
