from __future__ import annotations

from trading_control_plane.service_core import (
    ACTIVE_INTENT_STATUSES,
    MAX_ADD_UNITS,
    MAX_FACT_CLOCK_SKEW,
    OCCUPIED_CAPITAL_STATUSES,
    OCCUPIED_RESERVATION_STATUSES,
    RECONCILIATION_RESULTS,
    RISK_CAPACITY_LOCK_KEY,
    RISK_RESTORE_COOLDOWN,
    RISK_RESTORE_TTL,
    RISK_RESULTS,
    ROLE_ACTIONS,
    USD_STABLE_ASSETS,
    UUID,
    AccountEquity,
    AddCandidateFacts,
    Any,
    Approval,
    Campaign,
    CampaignStatus,
    CapabilityGate,
    CapabilityStatus,
    CapitalDirection,
    CapitalTransfer,
    Decimal,
    Direction,
    ExecutionEnvironment,
    FactStatus,
    Instrument,
    IntentCreation,
    IntentKind,
    OrderIntent,
    OrderIntentStatus,
    Position,
    PrincipalType,
    Proposal,
    ProposalSource,
    ProposalStatus,
    ProtectionOrder,
    ProtectionStatus,
    ReconciliationRun,
    ReconciliationStatus,
    ReservationStatus,
    ReviewDecision,
    RiskControlChangeRequest,
    RiskDecision,
    RiskEvaluationInput,
    RiskPolicy,
    RiskPolicyChangeStatus,
    RiskPolicyInput,
    RiskReservation,
    RiskResult,
    RiskTier,
    Role,
    RoleAssignment,
    RuntimeSourceHealth,
    ServiceMixinBase,
    Session,
    SystemRiskState,
    Team,
    TradingAuthorization,
    User,
    VenueFill,
    VenueOrder,
    VenueOrderStatus,
    _advisory_lock_key,
    _as_uuid,
    _reject,
    _scope_key,
    _scope_parts,
    datetime,
    evaluate_risk,
    fact_is_stale,
    func,
    select,
    text,
    timedelta,
    uuid4,
)


class RiskServiceMixin(ServiceMixinBase):
    """Server-side risk, authorization, recovery, and reconciliation transactions."""

    def set_risk_policy(
        self,
        *,
        actor_id: UUID,
        version: str,
        system_state: SystemRiskState,
        max_total_risk: Decimal,
        max_account_risk: Decimal,
        max_single_loss: Decimal,
        max_consecutive_losses: int,
        loss_cooldown: timedelta,
        max_fact_age: timedelta,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            team = self._require_role(session, actor_id, "risk_policy.manage")
            if (
                max_total_risk <= 0
                or max_account_risk <= 0
                or max_single_loss <= 0
                or max_account_risk > max_total_risk
                or max_single_loss > max_account_risk
                or max_consecutive_losses <= 0
                or loss_cooldown <= timedelta(0)
                or max_fact_age <= timedelta(0)
            ):
                _reject("RISK_POLICY_INVALID", "all risk limits must be explicitly valid")
            self._lock_risk_capacity(session, team.team_id)
            for current in session.scalars(
                select(RiskPolicy).where(
                    RiskPolicy.team_id == team.team_id,
                    RiskPolicy.active,
                )
            ).all():
                if (
                    system_state is SystemRiskState.NORMAL
                    and current.system_state != SystemRiskState.NORMAL.value
                ):
                    _reject(
                        "REVIEWED_RESTORE_REQUIRED",
                        "a tightened risk policy may only return to NORMAL through "
                        "reviewed restore",
                    )
                current.active = False
            previous_revision = (
                session.scalar(
                    select(func.max(RiskPolicy.revision)).where(RiskPolicy.team_id == team.team_id)
                )
                or 0
            )
            policy = RiskPolicy(
                team_id=team.team_id,
                version=version,
                revision=int(previous_revision) + 1,
                system_state=system_state.value,
                max_total_risk=max_total_risk,
                max_account_risk=max_account_risk,
                max_single_loss=max_single_loss,
                max_consecutive_losses=max_consecutive_losses,
                loss_cooldown_seconds=int(loss_cooldown.total_seconds()),
                max_fact_age_seconds=int(max_fact_age.total_seconds()),
                reason=system_state.value,
                active=True,
                updated_by=str(actor_id),
                updated_at=now,
            )
            session.add(policy)
            session.flush()
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="RISK_POLICY_SET",
                object_type="RiskPolicy",
                object_id=policy.policy_id,
                reason=system_state.value,
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return policy.policy_id

    def configure_risk_policy(
        self,
        *,
        actor_id: UUID,
        version: str,
        max_total_risk: Decimal,
        max_account_risk: Decimal,
        max_single_loss: Decimal,
        max_consecutive_losses: int,
        loss_cooldown: timedelta,
        max_fact_age: timedelta,
        expected_revision: int,
        reason: str,
        idempotency_key: str,
        now: datetime,
    ) -> UUID:
        operation = "risk_policy.configure"
        values = (max_total_risk, max_account_risk, max_single_loss)
        if (
            not version.strip()
            or version.strip() != version
            or len(version) > 120
            or any(not value.is_finite() or value <= 0 for value in values)
            or max_account_risk > max_total_risk
            or max_single_loss > max_account_risk
            or max_consecutive_losses <= 0
            or loss_cooldown <= timedelta(0)
            or max_fact_age <= timedelta(0)
        ):
            _reject("RISK_POLICY_INVALID", "all risk limits must be explicitly valid")
        payload = {
            "version": version,
            "max_total_risk": str(max_total_risk),
            "max_account_risk": str(max_account_risk),
            "max_single_loss": str(max_single_loss),
            "max_consecutive_losses": max_consecutive_losses,
            "loss_cooldown_seconds": int(loss_cooldown.total_seconds()),
            "max_fact_age_seconds": int(max_fact_age.total_seconds()),
            "expected_revision": expected_revision,
            "reason": reason,
        }
        with self.database.session_factory.begin() as session:
            team = self._require_role(session, actor_id, "risk_policy.manage")
            digest, replay = self._idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return _as_uuid(str(replay["policy_id"]))
            self._lock_risk_capacity(session, team.team_id)
            current = session.scalar(
                select(RiskPolicy)
                .where(RiskPolicy.team_id == team.team_id, RiskPolicy.active)
                .with_for_update()
            )
            current_revision = 0 if current is None else current.revision
            if current_revision != expected_revision:
                _reject("VERSION_CONFLICT", "risk policy changed before configuration")
            if session.scalar(
                select(RiskPolicy.policy_id).where(
                    RiskPolicy.team_id == team.team_id,
                    RiskPolicy.version == version,
                )
            ):
                _reject("RISK_POLICY_VERSION_CONFLICT", "risk policy version already exists")
            if current is not None and all(
                value is not None
                for value in (
                    current.max_account_risk,
                    current.max_single_loss,
                    current.max_consecutive_losses,
                    current.loss_cooldown_seconds,
                )
            ):
                assert current.max_account_risk is not None
                assert current.max_single_loss is not None
                assert current.max_consecutive_losses is not None
                assert current.loss_cooldown_seconds is not None
                if (
                    max_total_risk > current.max_total_risk
                    or max_account_risk > current.max_account_risk
                    or max_single_loss > current.max_single_loss
                    or max_consecutive_losses > current.max_consecutive_losses
                    or int(loss_cooldown.total_seconds()) < current.loss_cooldown_seconds
                    or int(max_fact_age.total_seconds()) > current.max_fact_age_seconds
                ):
                    _reject(
                        "REVIEWED_POLICY_CHANGE_REQUIRED",
                        "loosening configured risk limits requires an independently "
                        "reviewed change",
                    )
            if current is not None:
                current.active = False
            policy = RiskPolicy(
                team_id=team.team_id,
                version=version,
                revision=current_revision + 1,
                system_state=(
                    SystemRiskState.NORMAL.value if current is None else current.system_state
                ),
                max_total_risk=max_total_risk,
                max_account_risk=max_account_risk,
                max_single_loss=max_single_loss,
                max_consecutive_losses=max_consecutive_losses,
                loss_cooldown_seconds=int(loss_cooldown.total_seconds()),
                max_fact_age_seconds=int(max_fact_age.total_seconds()),
                reason=reason,
                active=True,
                updated_by=str(actor_id),
                updated_at=now,
            )
            session.add(policy)
            session.flush()
            for authorization in session.scalars(
                select(TradingAuthorization)
                .where(
                    TradingAuthorization.team_id == team.team_id,
                    TradingAuthorization.active,
                )
                .with_for_update()
            ):
                authorization.active = False
                if authorization.add_revoked_at is None:
                    authorization.add_revoked_at = now
            result = {"policy_id": str(policy.policy_id), "revision": policy.revision}
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
                event_type="RISK_POLICY_CONFIGURED",
                object_type="RiskPolicy",
                object_id=policy.policy_id,
                reason=reason,
                correlation_id=uuid4(),
                object_version=policy.revision,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                now=now,
            )
            return policy.policy_id

    @staticmethod
    def _lock_risk_capacity(session: Session, team_id: UUID | None = None) -> None:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {
                "key": (
                    RISK_CAPACITY_LOCK_KEY
                    if team_id is None
                    else _advisory_lock_key(str(team_id), "risk-capacity", "team")
                )
            },
        )

    @staticmethod
    def _occupied_risk(
        session: Session,
        team_id: UUID,
        *,
        account_id: str | None = None,
        venue: str | None = None,
    ) -> Decimal:
        query = (
            select(RiskReservation)
            .join(Campaign, Campaign.campaign_id == RiskReservation.campaign_id)
            .where(
                RiskReservation.team_id == team_id,
                Campaign.team_id == team_id,
                RiskReservation.status.in_(OCCUPIED_RESERVATION_STATUSES),
            )
        )
        if account_id is not None:
            query = query.where(Campaign.account_id == account_id)
        if venue is not None:
            query = query.where(Campaign.venue == venue)
        reservations = session.scalars(query.with_for_update(of=RiskReservation)).all()
        return sum((reservation.amount for reservation in reservations), Decimal(0))

    @staticmethod
    def _active_risk_policy(session: Session, team_id: UUID) -> RiskPolicy:
        policy = session.scalar(
            select(RiskPolicy)
            .where(RiskPolicy.team_id == team_id, RiskPolicy.active)
            .with_for_update()
        )
        if policy is None:
            _reject("RISK_POLICY_MISSING", "no active risk policy exists")
        return policy

    @staticmethod
    def _risk_policy_input(
        policy: RiskPolicy,
        *,
        effective_max_total_risk: Decimal | None = None,
    ) -> RiskPolicyInput:
        return RiskPolicyInput(
            version=policy.version,
            system_state=SystemRiskState(policy.system_state),
            max_total_risk=(
                policy.max_total_risk
                if effective_max_total_risk is None
                else effective_max_total_risk
            ),
            max_account_risk=policy.max_account_risk,
            max_single_loss=policy.max_single_loss,
            max_consecutive_losses=policy.max_consecutive_losses,
            loss_cooldown=(
                None
                if policy.loss_cooldown_seconds is None
                else timedelta(seconds=policy.loss_cooldown_seconds)
            ),
            max_fact_age=timedelta(seconds=policy.max_fact_age_seconds),
        )

    @staticmethod
    def _consecutive_loss_snapshot(
        session: Session,
        *,
        team_id: UUID,
        environment: str,
        account_id: str | None = None,
        venue: str | None = None,
    ) -> tuple[int, datetime | None]:
        query = (
            select(Campaign)
            .where(
                Campaign.team_id == team_id,
                Campaign.environment == environment,
                Campaign.status == CampaignStatus.CLOSED.value,
            )
            .order_by(Campaign.updated_at.desc(), Campaign.campaign_id.desc())
        )
        if account_id is not None:
            query = query.where(Campaign.account_id == account_id)
        if venue is not None:
            query = query.where(Campaign.venue == venue)
        streak = 0
        latest_loss_at: datetime | None = None
        for campaign in session.scalars(query):
            if campaign.final_pnl >= 0:
                break
            streak += 1
            if latest_loss_at is None:
                latest_loss_at = campaign.updated_at
        return streak, latest_loss_at

    def _loss_limit_context(
        self,
        session: Session,
        *,
        proposal: Proposal,
        policy: RiskPolicy,
        now: datetime,
    ) -> tuple[int, int, timedelta]:
        team_streak, team_latest_loss_at = self._consecutive_loss_snapshot(
            session,
            team_id=proposal.team_id,
            environment=proposal.environment,
        )
        account_streak, account_latest_loss_at = self._consecutive_loss_snapshot(
            session,
            team_id=proposal.team_id,
            environment=proposal.environment,
            account_id=proposal.account_id,
            venue=proposal.venue,
        )
        if policy.loss_cooldown_seconds is None:
            return team_streak, account_streak, timedelta(0)
        threshold = policy.max_consecutive_losses
        cooldown = timedelta(seconds=policy.loss_cooldown_seconds)
        remaining: list[timedelta] = []
        for streak, latest_loss_at in (
            (team_streak, team_latest_loss_at),
            (account_streak, account_latest_loss_at),
        ):
            if threshold is None or streak < threshold or latest_loss_at is None:
                continue
            remaining.append(max(timedelta(0), latest_loss_at + cooldown - now))
        return team_streak, account_streak, max(remaining, default=timedelta(0))

    def _managed_capital_context(
        self,
        session: Session,
        *,
        team_id: UUID,
        environment: str,
        now: datetime,
        max_age: timedelta,
    ) -> tuple[bool, Decimal, list[dict[str, Any]], datetime]:
        rows = session.scalars(
            select(AccountEquity)
            .where(
                AccountEquity.team_id == team_id,
                AccountEquity.environment == environment,
            )
            .order_by(
                AccountEquity.location_type,
                AccountEquity.venue,
                AccountEquity.account_id,
                AccountEquity.currency,
            )
            .with_for_update()
        ).all()
        if self.authoritative_live_accounts and environment == ExecutionEnvironment.LIVE.value:
            rows = [
                row
                for row in rows
                if row.location_type != "VENUE"
                or self.authoritative_live_accounts.get(row.venue) == row.account_id
            ]
        if not rows:
            return False, Decimal(0), [], now
        known = True
        total = Decimal(0)
        data_as_of = now
        facts: list[dict[str, Any]] = []
        unit_prices: dict[tuple[str, str, str], Decimal] = {}
        for row in rows:
            valuation_time = row.observed_at
            unit_price: Decimal | None
            value: Decimal | None
            if row.currency.upper() in USD_STABLE_ASSETS:
                unit_price = Decimal(1)
                value = row.equity
            else:
                unit_price = row.valuation_price
                value = row.valuation_equity
                if row.valuation_observed_at is not None:
                    valuation_time = min(valuation_time, row.valuation_observed_at)
            control_known = row.location_type != "VAULT" or row.control_status == "CONTROLLED"
            row_known = (
                row.fact_status == FactStatus.KNOWN.value
                and control_known
                and unit_price is not None
                and unit_price > 0
                and value is not None
                and value >= 0
                and not self._fact_is_stale(valuation_time, now, max_age)
            )
            known = known and row_known
            if row_known:
                assert unit_price is not None and value is not None
                total += value
                unit_prices[(row.account_id, row.venue, row.currency)] = unit_price
            data_as_of = min(data_as_of, valuation_time)
            facts.append(
                {
                    "account_equity_id": str(row.account_equity_id),
                    "location_type": row.location_type,
                    "location_id": row.account_id,
                    "venue": row.venue,
                    "asset": row.currency,
                    "fact_status": row.fact_status,
                    "control_status": row.control_status,
                    "usd_value": None if not row_known else str(value),
                    "observed_at": row.observed_at.isoformat(),
                    "valuation_observed_at": (
                        None
                        if row.valuation_observed_at is None
                        else row.valuation_observed_at.isoformat()
                    ),
                }
            )

        occupied = session.scalars(
            select(CapitalTransfer).where(
                CapitalTransfer.team_id == team_id,
                CapitalTransfer.environment == environment,
                CapitalTransfer.status.in_(OCCUPIED_CAPITAL_STATUSES),
            )
        ).all()
        occupied_usd = Decimal(0)
        for transfer in occupied:
            source_venue = (
                transfer.venue if transfer.direction == CapitalDirection.VENUE_TO_VAULT else "VAULT"
            )
            price = unit_prices.get((transfer.source_id, source_venue, transfer.asset))
            if price is None:
                known = False
                continue
            occupied_usd += transfer.reserved_amount * price
        total = max(Decimal(0), total - occupied_usd)
        facts.append(
            {
                "capital_transfer_reserved_usd": str(occupied_usd),
                "managed_capital_usd": str(total),
            }
        )
        return known and total > 0, total, facts, data_as_of

    def _server_risk_context(
        self,
        session: Session,
        *,
        proposal: Proposal,
        policy: RiskPolicy,
        kind: IntentKind,
        requested_quantity: Decimal,
        requested_risk: Decimal,
        current_risk: Decimal,
        now: datetime,
    ) -> tuple[RiskEvaluationInput, dict[str, Any], datetime, Decimal]:
        instrument = session.get(Instrument, proposal.instrument_id)
        if instrument is None or not instrument.active:
            _reject("INSTRUMENT_UNAVAILABLE", "proposal instrument is unavailable")
        position = session.scalar(
            select(Position)
            .where(
                Position.team_id == proposal.team_id,
                Position.account_id == proposal.account_id,
                Position.venue == proposal.venue,
                Position.environment == proposal.environment,
                Position.instrument_id == proposal.instrument_id,
            )
            .with_for_update()
        )
        equity = session.scalar(
            select(AccountEquity)
            .where(
                AccountEquity.team_id == proposal.team_id,
                AccountEquity.account_id == proposal.account_id,
                AccountEquity.venue == proposal.venue,
                AccountEquity.environment == proposal.environment,
                AccountEquity.currency == instrument.collateral_currency,
            )
            .with_for_update()
        )
        protection = None
        if position is not None:
            protection = session.scalar(
                select(ProtectionOrder)
                .where(ProtectionOrder.position_id == position.position_id)
                .with_for_update()
            )

        max_age = timedelta(seconds=policy.max_fact_age_seconds)
        source_health = (
            session.scalar(
                select(RuntimeSourceHealth).where(
                    RuntimeSourceHealth.team_id == proposal.team_id,
                    RuntimeSourceHealth.source_name == proposal.venue,
                    RuntimeSourceHealth.account_id == proposal.account_id,
                    RuntimeSourceHealth.venue == proposal.venue,
                )
            )
            if proposal.environment == ExecutionEnvironment.LIVE.value
            else None
        )
        source_current = proposal.environment != ExecutionEnvironment.LIVE.value or (
            source_health is not None
            and source_health.status == "SUCCESS"
            and not self._fact_is_stale(source_health.checked_at, now, max_age)
        )

        position_known = position is not None and position.fact_status == FactStatus.KNOWN.value
        venue_equity_known = (
            equity is not None
            and equity.fact_status == FactStatus.KNOWN.value
            and equity.currency == instrument.collateral_currency
        )
        capital_known, managed_capital_usd, managed_facts, capital_as_of = (
            self._managed_capital_context(
                session,
                team_id=proposal.team_id,
                environment=proposal.environment,
                now=now,
                max_age=max_age,
            )
        )
        equity_known = venue_equity_known and capital_known
        protection_required = kind is IntentKind.ADD or (
            position_known and position is not None and position.quantity != 0
        )
        protection_known = not protection_required or (
            protection is not None
            and protection.status == ProtectionStatus.ACTIVE.value
            and protection.fully_covered
            and position is not None
            and protection.quantity >= abs(position.quantity)
        )
        observed_times = [fact.observed_at for fact in (position, equity) if fact is not None]
        if protection_required and protection is not None:
            observed_times.append(protection.observed_at)
        observed_times.append(capital_as_of)
        data_as_of = min(observed_times, default=now)
        raw_fact_age = now - data_as_of
        fact_age = (
            timedelta(0) if -MAX_FACT_CLOCK_SKEW <= raw_fact_age < timedelta(0) else raw_fact_age
        )
        current_account_risk = self._occupied_risk(
            session,
            proposal.team_id,
            account_id=proposal.account_id,
            venue=proposal.venue,
        )
        team_loss_streak, account_loss_streak, loss_cooldown_remaining = self._loss_limit_context(
            session,
            proposal=proposal,
            policy=policy,
            now=now,
        )
        inputs = RiskEvaluationInput(
            kind=kind,
            requested_quantity=requested_quantity,
            requested_risk=requested_risk,
            current_risk=current_risk,
            current_account_risk=current_account_risk,
            team_consecutive_losses=team_loss_streak,
            account_consecutive_losses=account_loss_streak,
            loss_cooldown_remaining=loss_cooldown_remaining,
            fact_age=fact_age,
            position_known=position_known,
            equity_known=equity_known,
            protection_known=protection_known,
            source_current=source_current,
        )
        facts = {
            "proposal_id": str(proposal.proposal_id),
            "proposal_version": proposal.version,
            "kind": kind.value,
            "requested_quantity": str(requested_quantity),
            "requested_risk": str(requested_risk),
            "current_risk": str(current_risk),
            "current_account_risk": str(current_account_risk),
            "team_consecutive_losses": team_loss_streak,
            "account_consecutive_losses": account_loss_streak,
            "loss_cooldown_remaining_seconds": str(loss_cooldown_remaining.total_seconds()),
            "policy": {
                "team_id": str(policy.team_id),
                "policy_id": str(policy.policy_id),
                "version": policy.version,
                "revision": policy.revision,
                "system_state": policy.system_state,
                "max_total_risk": str(policy.max_total_risk),
                "max_account_risk": (
                    None if policy.max_account_risk is None else str(policy.max_account_risk)
                ),
                "max_single_loss": (
                    None if policy.max_single_loss is None else str(policy.max_single_loss)
                ),
                "max_consecutive_losses": policy.max_consecutive_losses,
                "loss_cooldown_seconds": policy.loss_cooldown_seconds,
                "max_fact_age_seconds": policy.max_fact_age_seconds,
            },
            "position": None
            if position is None
            else {
                "position_id": str(position.position_id),
                "quantity": str(position.quantity),
                "fact_status": position.fact_status,
                "observed_at": position.observed_at.isoformat(),
                "written_at": position.updated_at.isoformat(),
            },
            "equity": None
            if equity is None
            else {
                "account_equity_id": str(equity.account_equity_id),
                "fact_status": equity.fact_status,
                "currency": equity.currency,
                "observed_at": equity.observed_at.isoformat(),
                "written_at": equity.updated_at.isoformat(),
            },
            "managed_capital": {
                "known": capital_known,
                "total_usd": str(managed_capital_usd),
                "effective_max_total_risk": str(min(policy.max_total_risk, managed_capital_usd)),
                "facts": managed_facts,
            },
            "read_only_source": None
            if source_health is None
            else {
                "status": source_health.status,
                "error_code": source_health.error_code,
                "checked_at": source_health.checked_at.isoformat(),
            },
            "protection_required": protection_required,
            "protection": None
            if protection is None
            else {
                "protection_id": str(protection.protection_id),
                "status": protection.status,
                "quantity": str(protection.quantity),
                "fully_covered": protection.fully_covered,
                "observed_at": protection.observed_at.isoformat(),
                "written_at": protection.updated_at.isoformat(),
            },
            "data_as_of": data_as_of.isoformat(),
            "fact_age_seconds": str(fact_age.total_seconds()),
        }
        return (
            inputs,
            facts,
            data_as_of,
            min(policy.max_total_risk, managed_capital_usd),
        )

    def decide_risk(
        self,
        *,
        proposal_id: UUID,
        actor_id: UUID,
        kind: IntentKind,
        idempotency_key: str,
        now: datetime,
        requested_quantity: Decimal | None = None,
    ) -> UUID:
        operation = "risk.decide"
        with self.database.session_factory.begin() as session:
            proposal = session.get(Proposal, proposal_id, with_for_update=True)
            if proposal is None:
                _reject("PROPOSAL_NOT_FOUND", "proposal does not exist")
            self._require_role(
                session,
                actor_id,
                operation,
                proposal.account_id,
                proposal.venue,
                team_id=proposal.team_id,
            )
            quantity = proposal.quantity if requested_quantity is None else requested_quantity
            request_payload = {
                "proposal_id": str(proposal_id),
                "team_id": str(proposal.team_id),
                "kind": kind.value,
                "requested_quantity": str(quantity),
            }
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{proposal.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=request_payload,
            )
            if response is not None:
                return _as_uuid(str(response["decision_id"]))
            if proposal.status != ProposalStatus.APPROVED.value or proposal.expires_at <= now:
                _reject("PROPOSAL_NOT_APPROVED", "risk decision requires a live approved proposal")
            if quantity <= 0 or quantity > proposal.quantity:
                _reject("PROPOSAL_QUANTITY_EXCEEDED", "requested quantity exceeds proposal cap")

            self._lock_risk_capacity(session, proposal.team_id)
            policy = self._active_risk_policy(session, proposal.team_id)
            current_risk = self._occupied_risk(session, proposal.team_id)
            requested_risk = proposal.max_risk * quantity / proposal.quantity
            if requested_risk > proposal.max_risk:
                _reject("PROPOSAL_RISK_EXCEEDED", "requested risk exceeds proposal cap")
            inputs, facts, data_as_of, effective_max_total_risk = self._server_risk_context(
                session,
                proposal=proposal,
                policy=policy,
                kind=kind,
                requested_quantity=quantity,
                requested_risk=requested_risk,
                current_risk=current_risk,
                now=now,
            )
            outcome = evaluate_risk(
                self._risk_policy_input(
                    policy,
                    effective_max_total_risk=(
                        effective_max_total_risk
                        if effective_max_total_risk > 0
                        else policy.max_total_risk
                    ),
                ),
                inputs,
            )
            decision = RiskDecision(
                team_id=proposal.team_id,
                proposal_id=proposal_id,
                policy_id=policy.policy_id,
                input_data=facts,
                result=outcome.result.value,
                approved_quantity=outcome.allowed_quantity,
                risk_amount=outcome.allowed_risk,
                reasons=list(outcome.reasons),
                data_as_of=data_as_of,
                actor_id=str(actor_id),
                correlation_id=proposal.correlation_id,
                created_at=now,
            )
            session.add(decision)
            session.flush()
            result = {"decision_id": str(decision.decision_id)}
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{proposal.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="RISK_DECIDED",
                object_type="RiskDecision",
                object_id=decision.decision_id,
                reason=outcome.result.value,
                correlation_id=proposal.correlation_id,
                object_version=1,
                idempotency_key=idempotency_key,
                now=now,
            )
            team = session.get(Team, proposal.team_id)
            assert team is not None
            self._enqueue_notification_event(
                session,
                actor_id=str(actor_id),
                team=team,
                event_type="RISK_DECISION_RECORDED",
                payload={
                    "summary": "服务端风险决策已冻结记录。",
                    "result": decision.result,
                    "reasons": list(decision.reasons),
                    "policy_version": policy.version,
                    "environment": proposal.environment,
                    "account_id": proposal.account_id,
                    "venue": proposal.venue,
                    "intent_kind": kind.value,
                    "risk_amount": str(decision.risk_amount),
                    "approved_quantity": (
                        None
                        if decision.approved_quantity is None
                        else str(decision.approved_quantity)
                    ),
                },
                object_type="RiskDecision",
                object_id=decision.decision_id,
                object_version=1,
                idempotency_key=idempotency_key,
                correlation_id=proposal.correlation_id,
                environment=proposal.environment,
                account_id=proposal.account_id,
                venue=proposal.venue,
                now=now,
            )
            RISK_RESULTS.labels(outcome.result.value).inc()
            return decision.decision_id

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
            self._require_role(
                session,
                actor_id,
                operation,
                proposal.account_id,
                proposal.venue,
                team_id=proposal.team_id,
            )
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{proposal.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload={**payload, "team_id": str(proposal.team_id)},
            )
            if response is not None:
                return _as_uuid(str(response["authorization_id"]))
            self._lock_risk_capacity(session, proposal.team_id)
            policy = self._active_risk_policy(session, proposal.team_id)
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
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{proposal.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
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
        trigger_price = RiskServiceMixin._proposal_detail_decimal(proposal, "add_trigger_price")
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
            self._require_role(
                session,
                actor_id,
                "risk.tighten",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{campaign.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload={**payload, "team_id": str(campaign.team_id)},
            )
            if response is not None:
                return int(response["allowed_adds"])
            self._lock_risk_capacity(session, campaign.team_id)
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
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{campaign.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
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
            self._require_role(session, actor_id, "risk.tighten")
            digest, response = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return
            self._lock_risk_capacity(session)
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
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response={"status": gate.status},
                now=now,
            )
            self._audit(
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
            team = self._require_role(session, actor_id, "risk.tighten")
            digest, response = self._idempotency(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return SystemRiskState(str(response["system_state"]))
            self._lock_risk_capacity(session, team.team_id)
            policy = self._active_risk_policy(session, team.team_id)
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
            self._save_receipt(
                session,
                caller_id=str(actor_id),
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
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

    @staticmethod
    def _canonical_restore_scopes(
        configured_scopes: tuple[tuple[str, str, str], ...],
        campaigns: list[Campaign],
        *,
        required_environment: str | None = None,
    ) -> list[dict[str, str]]:
        scopes = {
            (environment, account_id, venue)
            for environment, account_id, venue in configured_scopes
            if required_environment is None or environment == required_environment
        }
        scopes.update(
            (campaign.environment, campaign.account_id, campaign.venue)
            for campaign in campaigns
            if campaign.status != CampaignStatus.CLOSED.value
            and (required_environment is None or campaign.environment == required_environment)
        )
        return [
            {"environment": environment, "account_id": account_id, "venue": venue}
            for environment, account_id, venue in sorted(scopes)
        ]

    def _risk_restore_blockers(
        self,
        session: Session,
        policy: RiskPolicy,
        required_scopes: list[dict[str, str]],
        *,
        require_live_scope: bool = False,
        now: datetime,
    ) -> list[str]:
        blockers: set[str] = set()
        if require_live_scope and not any(
            scope.get("environment") == ExecutionEnvironment.LIVE.value for scope in required_scopes
        ):
            blockers.add("LIVE_SCOPE_CONFIGURATION_REQUIRED")
        if policy.system_state == SystemRiskState.KILL_SWITCH.value:
            blockers.add("KILL_SWITCH_MANUAL_RECOVERY_REQUIRED")

        if (
            session.scalar(
                select(OrderIntent.intent_id).where(
                    OrderIntent.campaign_id.in_(
                        select(Campaign.campaign_id).where(Campaign.team_id == policy.team_id)
                    ),
                    OrderIntent.kind.in_({IntentKind.INITIAL.value, IntentKind.ADD.value}),
                    OrderIntent.status.in_(ACTIVE_INTENT_STATUSES),
                )
            )
            is not None
        ):
            blockers.add("ACTIVE_NEW_RISK_INTENT")
        if (
            session.scalar(
                select(OrderIntent.intent_id).where(
                    OrderIntent.campaign_id.in_(
                        select(Campaign.campaign_id).where(Campaign.team_id == policy.team_id)
                    ),
                    OrderIntent.status == OrderIntentStatus.UNKNOWN.value,
                )
            )
            is not None
        ):
            blockers.add("ORDER_INTENT_UNKNOWN")
        if (
            session.scalar(
                select(VenueOrder.venue_order_fact_id).where(
                    VenueOrder.team_id == policy.team_id,
                    VenueOrder.status == VenueOrderStatus.UNKNOWN.value,
                )
            )
            is not None
        ):
            blockers.add("VENUE_ORDER_UNKNOWN")
        if (
            session.scalar(
                select(RiskReservation.reservation_id).where(
                    RiskReservation.team_id == policy.team_id,
                    RiskReservation.status == ReservationStatus.UNKNOWN.value,
                )
            )
            is not None
        ):
            blockers.add("RISK_RESERVATION_UNKNOWN")
        if (
            session.scalar(
                select(Campaign.campaign_id).where(
                    Campaign.team_id == policy.team_id,
                    Campaign.status == CampaignStatus.UNKNOWN.value,
                )
            )
            is not None
        ):
            blockers.add("CAMPAIGN_UNKNOWN")
        if (
            session.scalar(
                select(VenueOrder.venue_order_fact_id).where(
                    VenueOrder.team_id == policy.team_id,
                    VenueOrder.order_intent_id.is_(None),
                    VenueOrder.status.in_(
                        {
                            VenueOrderStatus.SENT.value,
                            VenueOrderStatus.PARTIALLY_FILLED.value,
                            VenueOrderStatus.UNKNOWN.value,
                        }
                    ),
                )
            )
            is not None
        ):
            blockers.add("UNBOUND_OPEN_ORDER")

        max_age = timedelta(seconds=policy.max_fact_age_seconds)
        for scope in required_scopes:
            try:
                environment = ExecutionEnvironment(str(scope["environment"]))
                account_id = str(scope["account_id"])
                venue = str(scope["venue"])
            except (KeyError, ValueError):
                blockers.add("CONTROL_SCOPE_INVALID")
                continue
            prefix = f"{environment.value}:{account_id}:{venue}"
            if require_live_scope:
                source_health = session.scalar(
                    select(RuntimeSourceHealth).where(
                        RuntimeSourceHealth.team_id == policy.team_id,
                        RuntimeSourceHealth.source_name == venue,
                        RuntimeSourceHealth.account_id == account_id,
                        RuntimeSourceHealth.venue == venue,
                    )
                )
                if source_health is None:
                    blockers.add(f"READ_ONLY_SOURCE_MISSING:{prefix}")
                elif source_health.status != "SUCCESS":
                    failure_code = source_health.error_code or "READ_ONLY_PROBE_FAILED"
                    blockers.add(f"READ_ONLY_SOURCE_FAILED:{prefix}:{failure_code}")
                elif self._fact_is_stale(source_health.checked_at, now, max_age):
                    blockers.add(f"READ_ONLY_SOURCE_STALE:{prefix}")
            equity = session.scalar(
                select(AccountEquity).where(
                    AccountEquity.team_id == policy.team_id,
                    AccountEquity.environment == environment.value,
                    AccountEquity.account_id == account_id,
                    AccountEquity.venue == venue,
                )
            )
            if equity is None:
                blockers.add(f"ACCOUNT_EQUITY_MISSING:{prefix}")
            elif equity.fact_status != FactStatus.KNOWN.value:
                blockers.add(f"ACCOUNT_EQUITY_UNKNOWN:{prefix}")
            elif self._fact_is_stale(equity.observed_at, now, max_age):
                blockers.add(f"ACCOUNT_EQUITY_STALE:{prefix}")

            positions = session.scalars(
                select(Position).where(
                    Position.team_id == policy.team_id,
                    Position.environment == environment.value,
                    Position.account_id == account_id,
                    Position.venue == venue,
                )
            ).all()
            if not positions:
                blockers.add(f"POSITION_FACTS_MISSING:{prefix}")
            for position in positions:
                if position.fact_status != FactStatus.KNOWN.value:
                    blockers.add(f"POSITION_UNKNOWN:{prefix}")
                    continue
                if self._fact_is_stale(position.observed_at, now, max_age):
                    blockers.add(f"POSITION_STALE:{prefix}")
                if position.quantity == 0:
                    continue
                protection = session.scalar(
                    select(ProtectionOrder).where(
                        ProtectionOrder.position_id == position.position_id
                    )
                )
                if (
                    protection is None
                    or protection.status != ProtectionStatus.ACTIVE.value
                    or not protection.fully_covered
                    or protection.quantity < abs(position.quantity)
                ):
                    blockers.add(f"PROTECTION_INCOMPLETE:{prefix}")
                elif self._fact_is_stale(protection.observed_at, now, max_age):
                    blockers.add(f"PROTECTION_STALE:{prefix}")

            execution_scope = _scope_key(environment.value, account_id, venue)
            reconciliation = session.scalar(
                select(ReconciliationRun)
                .where(
                    ReconciliationRun.team_id == policy.team_id,
                    ReconciliationRun.execution_scope == execution_scope,
                )
                .order_by(ReconciliationRun.completed_at.desc())
                .limit(1)
            )
            latest_source_at = max(
                [
                    policy.updated_at,
                    *([equity.observed_at] if equity is not None else []),
                    *(position.observed_at for position in positions),
                ]
            )
            if (
                reconciliation is None
                or reconciliation.status != ReconciliationStatus.MATCH.value
                or not reconciliation.is_computed
            ):
                blockers.add(f"COMPUTED_RECONCILIATION_MATCH_REQUIRED:{prefix}")
            elif (
                self._fact_is_stale(reconciliation.completed_at, now, max_age)
                or reconciliation.completed_at < latest_source_at
            ):
                blockers.add(f"RECONCILIATION_STALE:{prefix}")
        return sorted(blockers)

    @staticmethod
    def _risk_restore_condition_details(
        blockers: list[str],
        required_scopes: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        blocker_set = set(blockers)

        def condition(
            code: str,
            label: str,
            matching: list[str],
            role: str,
            next_action: str,
            scope: dict[str, str] | None = None,
        ) -> dict[str, Any]:
            return {
                "code": code,
                "label": label,
                "status": "BLOCKED" if matching else "PASS",
                "reason": matching if matching else ["CURRENT"],
                "role": role,
                "next_action": next_action if matching else "无需处理",
                "scope": scope,
            }

        details = [
            condition(
                "LIVE_SCOPE_CONFIGURED",
                "生产账户范围已配置",
                [item for item in blockers if item == "LIVE_SCOPE_CONFIGURATION_REQUIRED"],
                "SYSTEM_ADMIN",
                "配置明确的 LIVE 账户与交易所范围后重新检查",
            ),
            condition(
                "SYSTEM_RECOVERABLE",
                "系统不处于需人工处置的紧急停止",
                [item for item in blockers if item == "KILL_SWITCH_MANUAL_RECOVERY_REQUIRED"],
                "SYSTEM_ADMIN",
                "先完成 KILL_SWITCH 人工处置; 不能从本页绕过",
            ),
            condition(
                "NO_ACTIVE_OR_UNKNOWN_OPERATIONS",
                "不存在新增风险在途或结果未知操作",
                [
                    item
                    for item in blockers
                    if item
                    in {
                        "ACTIVE_NEW_RISK_INTENT",
                        "ORDER_INTENT_UNKNOWN",
                        "VENUE_ORDER_UNKNOWN",
                        "RISK_RESERVATION_UNKNOWN",
                        "CAMPAIGN_UNKNOWN",
                        "UNBOUND_OPEN_ORDER",
                    }
                ],
                "OPERATOR",
                "完成订单、仓位、预留与任务对账; 消除未知结果",
            ),
        ]
        for scope in required_scopes:
            prefix = (
                f"{scope.get('environment', '')}:"
                f"{scope.get('account_id', '')}:{scope.get('venue', '')}"
            )
            details.extend(
                [
                    condition(
                        "READ_ONLY_SOURCE_CURRENT",
                        "交易所只读探针已连接且新鲜",
                        [
                            item
                            for item in blockers
                            if item.startswith(
                                (
                                    f"READ_ONLY_SOURCE_MISSING:{prefix}",
                                    f"READ_ONLY_SOURCE_FAILED:{prefix}",
                                    f"READ_ONLY_SOURCE_STALE:{prefix}",
                                )
                            )
                        ],
                        "SYSTEM_ADMIN",
                        "恢复该交易所只读同步并等待一次成功探针",
                        scope,
                    ),
                    condition(
                        "ACCOUNT_EQUITY_CURRENT",
                        "账户权益事实完整且新鲜",
                        [
                            item
                            for item in blockers
                            if item.startswith(
                                (
                                    f"ACCOUNT_EQUITY_MISSING:{prefix}",
                                    f"ACCOUNT_EQUITY_UNKNOWN:{prefix}",
                                    f"ACCOUNT_EQUITY_STALE:{prefix}",
                                )
                            )
                        ],
                        "OPERATOR",
                        "同步该账户最新权益事实",
                        scope,
                    ),
                    condition(
                        "POSITION_AND_PROTECTION_CURRENT",
                        "仓位与保护事实完整且新鲜",
                        [
                            item
                            for item in blockers
                            if item.startswith(
                                (
                                    f"POSITION_FACTS_MISSING:{prefix}",
                                    f"POSITION_UNKNOWN:{prefix}",
                                    f"POSITION_STALE:{prefix}",
                                    f"PROTECTION_INCOMPLETE:{prefix}",
                                    f"PROTECTION_STALE:{prefix}",
                                )
                            )
                        ],
                        "OPERATOR",
                        "同步仓位并补齐有效保护事实",
                        scope,
                    ),
                    condition(
                        "COMPUTED_RECONCILIATION_CURRENT",
                        "最新计算型对账一致",
                        [
                            item
                            for item in blockers
                            if item.startswith(
                                (
                                    f"COMPUTED_RECONCILIATION_MATCH_REQUIRED:{prefix}",
                                    f"RECONCILIATION_STALE:{prefix}",
                                )
                            )
                        ],
                        "OPERATOR",
                        "用最新事实重新运行计算型对账",
                        scope,
                    ),
                ]
            )
        details.append(
            condition(
                "AUTO_ADD_REMAINS_DISABLED",
                "恢复不会开启自动加仓",
                [],
                "SYSTEM",
                "无需处理",
            )
        )
        represented = {
            reason for item in details for reason in item["reason"] if reason != "CURRENT"
        }
        for blocker in sorted(blocker_set - represented):
            details.append(
                condition(
                    blocker.split(":", 1)[0],
                    "其他实时安全条件",
                    [blocker],
                    "OPERATOR",
                    "按精确阻断码处理后重新检查",
                )
            )
        return details

    @staticmethod
    def _risk_restore_request_drifted(
        request: RiskControlChangeRequest,
        policy: RiskPolicy,
        gate: CapabilityGate,
    ) -> bool:
        return bool(
            request.source_policy_id != policy.policy_id
            or request.source_policy_version != policy.version
            or request.source_policy_revision != policy.revision
            or request.source_auto_add_status != gate.status
            or request.source_auto_add_version != gate.version
        )

    def risk_control_status(
        self,
        actor_id: UUID,
        configured_scopes: tuple[tuple[str, str, str], ...],
        *,
        require_live_scope: bool = False,
        now: datetime,
    ) -> dict[str, Any]:
        with self.database.session_factory() as session:
            team = self._require_role(session, actor_id, "system.view")
            policy = session.scalar(
                select(RiskPolicy).where(
                    RiskPolicy.team_id == team.team_id,
                    RiskPolicy.active,
                )
            )
            gate = session.get(CapabilityGate, "AUTO_ADD")
            if gate is None:
                _reject("CAPABILITY_GATE_NOT_FOUND", "AUTO_ADD gate is missing")
            if policy is None:
                return {
                    "policy": None,
                    "auto_add_gate": {
                        "status": gate.status,
                        "version": gate.version,
                        "reason": gate.reason,
                        "operator_id": gate.operator_id,
                        "operator_username": None,
                        "updated_at": gate.updated_at.isoformat(),
                    },
                    "restore_conditions": {
                        "ready": False,
                        "live_scope_required": require_live_scope,
                        "blockers": ["RISK_POLICY_MISSING"],
                        "checks": [],
                        "required_scopes": [],
                        "cooldown_seconds": int(RISK_RESTORE_COOLDOWN.total_seconds()),
                    },
                    "actions": {
                        "configure_policy": {
                            "allowed": any(
                                "risk_policy.manage" in ROLE_ACTIONS[Role(item.role)]
                                or "*" in ROLE_ACTIONS[Role(item.role)]
                                for item in session.scalars(
                                    select(RoleAssignment).where(
                                        RoleAssignment.user_id == actor_id,
                                        RoleAssignment.team_id == team.team_id,
                                    )
                                )
                            ),
                            "reason": "RISK_POLICY_MISSING",
                        },
                        "direct_restore": {"allowed": False, "reason": "RISK_POLICY_MISSING"},
                        "request_restore": {"allowed": False, "reason": "RISK_POLICY_MISSING"},
                        "review_restore": {"allowed": False, "reason": "RISK_POLICY_MISSING"},
                        "execute_restore": {"allowed": False, "reason": "RISK_POLICY_MISSING"},
                    },
                    "requests": [],
                    "as_of": now.isoformat(),
                }
            campaigns = session.scalars(
                select(Campaign).where(Campaign.team_id == team.team_id)
            ).all()
            scopes = self._canonical_restore_scopes(
                configured_scopes,
                list(campaigns),
                required_environment=(
                    ExecutionEnvironment.LIVE.value if require_live_scope else None
                ),
            )
            blockers = self._risk_restore_blockers(
                session,
                policy,
                scopes,
                require_live_scope=require_live_scope,
                now=now,
            )
            if any(
                value is None
                for value in (
                    policy.max_account_risk,
                    policy.max_single_loss,
                    policy.max_consecutive_losses,
                    policy.loss_cooldown_seconds,
                )
            ):
                blockers = ["RISK_LIMITS_UNCONFIGURED", *blockers]
            requests = session.scalars(
                select(RiskControlChangeRequest)
                .where(RiskControlChangeRequest.team_id == team.team_id)
                .order_by(RiskControlChangeRequest.created_at.desc())
                .limit(20)
            ).all()
            request_ids = [item.request_id for item in requests]
            reviews = (
                []
                if not request_ids
                else session.scalars(
                    select(Approval)
                    .where(Approval.risk_control_change_request_id.in_(request_ids))
                    .order_by(Approval.created_at, Approval.approval_id)
                ).all()
            )
            identity_ids = {item.requester_id for item in requests}
            identity_ids.update(item.reviewer_id for item in reviews)
            for value in (policy.updated_by, gate.operator_id):
                try:
                    identity_ids.add(UUID(str(value)))
                except (TypeError, ValueError):
                    pass
            usernames = {
                item.user_id: item.username
                for item in session.scalars(
                    select(User).where(User.user_id.in_(identity_ids))
                ).all()
            }

            def projected_username(value: object) -> str | None:
                try:
                    user_id = UUID(str(value))
                except (TypeError, ValueError):
                    return None
                return usernames.get(user_id)

            reviews_by_request: dict[UUID, list[dict[str, Any]]] = {}
            for review in reviews:
                if review.risk_control_change_request_id is None:
                    continue
                reviews_by_request.setdefault(review.risk_control_change_request_id, []).append(
                    {
                        "reviewer_id": str(review.reviewer_id),
                        "reviewer_username": usernames.get(review.reviewer_id),
                        "decision": review.decision,
                        "reason": review.reason,
                        "created_at": review.created_at.isoformat(),
                    }
                )
            assignments = session.scalars(
                select(RoleAssignment).where(
                    RoleAssignment.user_id == actor_id,
                    RoleAssignment.team_id == team.team_id,
                )
            ).all()
            role_names = {item.role for item in assignments}
            restricted = policy.system_state != SystemRiskState.NORMAL.value

            def request_superseded(item: RiskControlChangeRequest) -> bool:
                return not restricted or self._risk_restore_request_drifted(item, policy, gate)

            def effective_request_status(item: RiskControlChangeRequest) -> str:
                if item.status in {
                    RiskPolicyChangeStatus.PENDING_REVIEW.value,
                    RiskPolicyChangeStatus.APPROVED.value,
                } and (item.expires_at <= now or request_superseded(item)):
                    return RiskPolicyChangeStatus.EXPIRED.value
                return item.status

            active_request = next(
                (
                    item
                    for item in requests
                    if effective_request_status(item)
                    in {
                        RiskPolicyChangeStatus.PENDING_REVIEW.value,
                        RiskPolicyChangeStatus.APPROVED.value,
                    }
                ),
                None,
            )
            is_admin = Role.SYSTEM_ADMIN.value in role_names
            is_operator = Role.OPERATOR.value in role_names
            is_reviewer = Role.REVIEWER.value in role_names
            direct_allowed = is_admin and restricted and not blockers
            request_allowed = is_operator and restricted and active_request is None
            review_allowed = bool(
                is_reviewer
                and active_request is not None
                and active_request.status == RiskPolicyChangeStatus.PENDING_REVIEW.value
                and active_request.requester_id != actor_id
                and not any(
                    review["reviewer_id"] == str(actor_id)
                    for review in reviews_by_request.get(active_request.request_id, [])
                )
            )
            execute_allowed = bool(
                (is_reviewer or is_admin)
                and active_request is not None
                and active_request.status == RiskPolicyChangeStatus.APPROVED.value
                and active_request.requester_id != actor_id
                and not blockers
                and active_request.execute_after <= now
            )
            return {
                "policy": {
                    "team_id": str(policy.team_id),
                    "policy_id": str(policy.policy_id),
                    "version": policy.version,
                    "revision": policy.revision,
                    "system_state": policy.system_state,
                    "reason": policy.reason,
                    "max_total_risk": str(policy.max_total_risk),
                    "max_account_risk": (
                        None if policy.max_account_risk is None else str(policy.max_account_risk)
                    ),
                    "max_single_loss": (
                        None if policy.max_single_loss is None else str(policy.max_single_loss)
                    ),
                    "max_consecutive_losses": policy.max_consecutive_losses,
                    "loss_cooldown_seconds": policy.loss_cooldown_seconds,
                    "limits_configured": all(
                        value is not None
                        for value in (
                            policy.max_account_risk,
                            policy.max_single_loss,
                            policy.max_consecutive_losses,
                            policy.loss_cooldown_seconds,
                        )
                    ),
                    "max_fact_age_seconds": policy.max_fact_age_seconds,
                    "updated_by": policy.updated_by,
                    "updated_by_username": projected_username(policy.updated_by),
                    "updated_at": policy.updated_at.isoformat(),
                },
                "auto_add_gate": {
                    "status": gate.status,
                    "version": gate.version,
                    "reason": gate.reason,
                    "operator_id": gate.operator_id,
                    "operator_username": projected_username(gate.operator_id),
                    "updated_at": gate.updated_at.isoformat(),
                },
                "restore_conditions": {
                    "ready": not blockers,
                    "live_scope_required": require_live_scope,
                    "blockers": blockers,
                    "checks": self._risk_restore_condition_details(blockers, scopes),
                    "required_scopes": scopes,
                    "cooldown_seconds": int(RISK_RESTORE_COOLDOWN.total_seconds()),
                },
                "actions": {
                    "configure_policy": {
                        "allowed": is_admin,
                        "reason": "READY" if is_admin else "SYSTEM_ADMIN_REQUIRED",
                    },
                    "direct_restore": {
                        "allowed": direct_allowed,
                        "reason": (
                            "READY"
                            if direct_allowed
                            else "SYSTEM_ADMIN_REQUIRED"
                            if not is_admin
                            else "SYSTEM_ALREADY_NORMAL"
                            if not restricted
                            else "REALTIME_CONDITIONS_BLOCKED"
                        ),
                    },
                    "request_restore": {
                        "allowed": request_allowed,
                        "reason": (
                            "READY"
                            if request_allowed
                            else "OPERATOR_REQUIRED"
                            if not is_operator
                            else "SYSTEM_ALREADY_NORMAL"
                            if not restricted
                            else "RESTORE_REQUEST_ALREADY_ACTIVE"
                        ),
                    },
                    "review_restore": {
                        "allowed": review_allowed,
                        "reason": (
                            "READY"
                            if review_allowed
                            else "INDEPENDENT_REVIEWER_REQUIRED"
                            if not is_reviewer
                            else "NO_REVIEWABLE_REQUEST"
                        ),
                    },
                    "execute_restore": {
                        "allowed": execute_allowed,
                        "reason": (
                            "READY" if execute_allowed else "EXECUTION_REQUIREMENTS_NOT_MET"
                        ),
                    },
                },
                "requests": [
                    {
                        "request_id": str(item.request_id),
                        "requester_id": str(item.requester_id),
                        "requester_username": usernames.get(item.requester_id),
                        "status": effective_request_status(item),
                        "superseded_by_control_state": request_superseded(item),
                        "version": item.version,
                        "reason": item.reason,
                        "restore_auto_add": item.restore_auto_add,
                        "require_live_scope": item.require_live_scope,
                        "source_policy_id": str(item.source_policy_id),
                        "source_policy_version": item.source_policy_version,
                        "source_policy_revision": item.source_policy_revision,
                        "source_auto_add_status": item.source_auto_add_status,
                        "source_auto_add_version": item.source_auto_add_version,
                        "required_scopes": item.required_scopes,
                        "execute_after": item.execute_after.isoformat(),
                        "expires_at": item.expires_at.isoformat(),
                        "executed_at": (
                            None if item.executed_at is None else item.executed_at.isoformat()
                        ),
                        "resulting_policy_id": (
                            None
                            if item.resulting_policy_id is None
                            else str(item.resulting_policy_id)
                        ),
                        "reviews": reviews_by_request.get(item.request_id, []),
                        "created_at": item.created_at.isoformat(),
                        "updated_at": item.updated_at.isoformat(),
                    }
                    for item in requests
                ],
                "as_of": now.isoformat(),
            }

    def risk_control_change_version(
        self,
        request_id: UUID,
        actor_id: UUID | None = None,
    ) -> int:
        with self.database.session_factory() as session:
            request = session.get(RiskControlChangeRequest, request_id)
            if request is None:
                _reject("RISK_RESTORE_NOT_FOUND", "restore request does not exist")
            if actor_id is not None:
                self._require_role(
                    session,
                    actor_id,
                    "system.view",
                    team_id=request.team_id,
                )
            return request.version

    def create_risk_control_change_request(
        self,
        actor_id: UUID,
        idempotency_key: str,
        *,
        reason: str,
        restore_auto_add: bool,
        configured_scopes: tuple[tuple[str, str, str], ...],
        require_live_scope: bool = False,
        now: datetime,
    ) -> UUID:
        operation = "risk.restore.request"
        payload = {
            "reason": reason,
            "restore_auto_add": restore_auto_add,
            "configured_scopes": configured_scopes,
            "require_live_scope": require_live_scope,
        }
        with self.database.session_factory.begin() as session:
            team = self._require_action_assignment(session, actor_id, operation)
            requester = session.get(User, actor_id)
            if requester is None or requester.principal_type != PrincipalType.HUMAN.value:
                _reject("SERVICE_REQUEST_FORBIDDEN", "risk restoration requires a human requester")
            operator_assignments = session.scalars(
                select(RoleAssignment).where(
                    RoleAssignment.user_id == actor_id,
                    RoleAssignment.team_id == team.team_id,
                    RoleAssignment.role == Role.OPERATOR.value,
                )
            ).all()
            if not operator_assignments:
                _reject(
                    "RISK_RESTORE_OPERATOR_REQUIRED",
                    "reviewed risk restoration must be requested by an operator",
                )
            if any(
                not any(
                    (assignment.account_scope is None or assignment.account_scope == account_id)
                    and (assignment.venue_scope is None or assignment.venue_scope == venue)
                    for assignment in operator_assignments
                )
                for _environment, account_id, venue in configured_scopes
            ):
                _reject(
                    "RBAC_DENIED",
                    "risk restoration scope is outside the operator assignment",
                )
            if restore_auto_add:
                _reject(
                    "AUTO_ADD_RESTORE_FORBIDDEN",
                    "risk restoration never enables the AUTO_ADD gate",
                )
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return _as_uuid(str(response["request_id"]))
            self._lock_risk_capacity(session, team.team_id)
            policy = self._active_risk_policy(session, team.team_id)
            gate = session.get(CapabilityGate, "AUTO_ADD", with_for_update=True)
            if gate is None:
                _reject("CAPABILITY_GATE_NOT_FOUND", "AUTO_ADD gate is missing")
            if policy.system_state == SystemRiskState.KILL_SWITCH.value:
                _reject(
                    "KILL_SWITCH_MANUAL_RECOVERY_REQUIRED",
                    "KILL_SWITCH cannot be resumed through the reviewed restore workflow",
                )
            if policy.system_state == SystemRiskState.NORMAL.value and (
                not restore_auto_add or gate.status == CapabilityStatus.ENABLED.value
            ):
                _reject("RISK_CONTROL_ALREADY_NORMAL", "the requested controls are already open")
            existing_requests = list(
                session.scalars(
                    select(RiskControlChangeRequest)
                    .where(
                        RiskControlChangeRequest.team_id == team.team_id,
                        RiskControlChangeRequest.status.in_(
                            {
                                RiskPolicyChangeStatus.PENDING_REVIEW.value,
                                RiskPolicyChangeStatus.APPROVED.value,
                            }
                        ),
                    )
                    .with_for_update()
                )
            )
            pending = None
            for existing_request in existing_requests:
                superseded = self._risk_restore_request_drifted(existing_request, policy, gate)
                if existing_request.expires_at <= now or superseded:
                    existing_request.status = RiskPolicyChangeStatus.EXPIRED.value
                    existing_request.version += 1
                    existing_request.updated_at = now
                    self._audit(
                        session,
                        actor_id=str(actor_id),
                        event_type=(
                            "RISK_RESTORE_SUPERSEDED" if superseded else "RISK_RESTORE_EXPIRED"
                        ),
                        object_type="RiskControlChangeRequest",
                        object_id=existing_request.request_id,
                        reason=(
                            "restore request control snapshot was superseded"
                            if superseded
                            else "restore request expired before a replacement was created"
                        ),
                        correlation_id=existing_request.correlation_id,
                        object_version=existing_request.version,
                        idempotency_key=idempotency_key,
                        now=now,
                    )
                else:
                    pending = existing_request.request_id
            if pending is not None:
                _reject("RISK_RESTORE_ALREADY_PENDING", "a reviewed restore is already active")
            campaigns = list(
                session.scalars(select(Campaign).where(Campaign.team_id == team.team_id)).all()
            )
            scopes = self._canonical_restore_scopes(
                configured_scopes,
                campaigns,
                required_environment=(
                    ExecutionEnvironment.LIVE.value if require_live_scope else None
                ),
            )
            last_tighten_at = max(
                policy.updated_at,
                gate.updated_at if restore_auto_add else policy.updated_at,
            )
            request = RiskControlChangeRequest(
                team_id=team.team_id,
                requester_id=actor_id,
                status=RiskPolicyChangeStatus.PENDING_REVIEW.value,
                version=1,
                reason=reason,
                restore_auto_add=restore_auto_add,
                require_live_scope=require_live_scope,
                source_policy_id=policy.policy_id,
                source_policy_version=policy.version,
                source_policy_revision=policy.revision,
                source_auto_add_status=gate.status,
                source_auto_add_version=gate.version,
                required_scopes=scopes,
                resulting_policy_id=None,
                correlation_id=uuid4(),
                execute_after=max(now, last_tighten_at + RISK_RESTORE_COOLDOWN),
                expires_at=now + RISK_RESTORE_TTL,
                executed_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(request)
            session.flush()
            result = {"request_id": str(request.request_id)}
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
                event_type="RISK_RESTORE_REQUESTED",
                object_type="RiskControlChangeRequest",
                object_id=request.request_id,
                reason=reason,
                correlation_id=request.correlation_id,
                object_version=request.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return request.request_id

    def review_risk_control_change_request(
        self,
        request_id: UUID,
        reviewer_id: UUID,
        decision: ReviewDecision,
        reason: str,
        expected_version: int,
        idempotency_key: str,
        *,
        now: datetime,
    ) -> RiskPolicyChangeStatus:
        operation = "risk.restore.review"
        payload = {
            "request_id": str(request_id),
            "decision": decision.value,
            "reason": reason,
            "expected_version": expected_version,
        }
        expired = False
        result_status: RiskPolicyChangeStatus | None = None
        with self.database.session_factory.begin() as session:
            request = session.get(RiskControlChangeRequest, request_id, with_for_update=True)
            if request is None:
                _reject("RISK_RESTORE_NOT_FOUND", "restore request does not exist")
            self._require_role(session, reviewer_id, operation, team_id=request.team_id)
            reviewer = session.get(User, reviewer_id)
            if reviewer is None or reviewer.principal_type != PrincipalType.HUMAN.value:
                _reject("SERVICE_REVIEW_FORBIDDEN", "risk restoration requires human reviewers")
            digest, response = self._idempotency(
                session,
                caller_id=f"{reviewer_id}:{request.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return RiskPolicyChangeStatus(str(response["status"]))
            if request.version != expected_version:
                _reject("VERSION_CONFLICT", "restore request changed before review")
            policy = self._active_risk_policy(session, request.team_id)
            gate = session.get(CapabilityGate, "AUTO_ADD")
            if gate is None:
                _reject("CAPABILITY_GATE_NOT_FOUND", "AUTO_ADD gate is missing")
            if (
                policy.system_state == SystemRiskState.NORMAL.value
                or self._risk_restore_request_drifted(request, policy, gate)
            ):
                _reject(
                    "RISK_RESTORE_CONTROL_DRIFT",
                    "restore request no longer matches current controls",
                )
            if request.requester_id == reviewer_id:
                _reject("SELF_REVIEW_FORBIDDEN", "requester cannot review their restore")
            if request.expires_at <= now:
                request.status = RiskPolicyChangeStatus.EXPIRED.value
                request.version += 1
                request.updated_at = now
                expired = True
            else:
                if request.status != RiskPolicyChangeStatus.PENDING_REVIEW.value:
                    _reject("RISK_RESTORE_NOT_REVIEWABLE", "restore request is not pending")
                duplicate = session.scalar(
                    select(Approval).where(
                        Approval.risk_control_change_request_id == request_id,
                        Approval.reviewer_id == reviewer_id,
                    )
                )
                if duplicate is not None:
                    _reject("REVIEW_ALREADY_RECORDED", "reviewer already voted")
                session.add(
                    Approval(
                        proposal_id=None,
                        transfer_proposal_id=None,
                        risk_control_change_request_id=request_id,
                        reviewer_id=reviewer_id,
                        decision=decision.value,
                        reason=reason,
                        created_at=now,
                    )
                )
                session.flush()
                if decision is ReviewDecision.REJECT:
                    request.status = RiskPolicyChangeStatus.REJECTED.value
                else:
                    approvals = session.scalar(
                        select(func.count())
                        .select_from(Approval)
                        .where(
                            Approval.risk_control_change_request_id == request_id,
                            Approval.decision == ReviewDecision.APPROVE.value,
                        )
                    )
                    if int(approvals or 0) >= 1:
                        request.status = RiskPolicyChangeStatus.APPROVED.value
                request.version += 1
                request.updated_at = now
                result_status = RiskPolicyChangeStatus(request.status)
                response_value = {"status": request.status, "version": request.version}
                self._save_receipt(
                    session,
                    caller_id=f"{reviewer_id}:{request.team_id}",
                    operation=operation,
                    idempotency_key=idempotency_key,
                    semantic_hash=digest,
                    response=response_value,
                    now=now,
                )
                self._audit(
                    session,
                    actor_id=str(reviewer_id),
                    event_type="RISK_RESTORE_REVIEWED",
                    object_type="RiskControlChangeRequest",
                    object_id=request.request_id,
                    reason=f"{decision.value}: {reason}",
                    correlation_id=request.correlation_id,
                    object_version=request.version,
                    idempotency_key=idempotency_key,
                    now=now,
                )
        if expired:
            _reject("RISK_RESTORE_EXPIRED", "restore request expired before review")
        if result_status is None:
            raise RuntimeError("risk restore review completed without a status")
        return result_status

    def execute_risk_control_change_request(
        self,
        request_id: UUID,
        actor_id: UUID,
        expected_version: int,
        idempotency_key: str,
        configured_scopes: tuple[tuple[str, str, str], ...],
        *,
        require_live_scope: bool = False,
        now: datetime,
    ) -> UUID:
        operation = "risk.restore.execute"
        payload = {
            "request_id": str(request_id),
            "expected_version": expected_version,
            "configured_scopes": configured_scopes,
            "require_live_scope": require_live_scope,
        }
        with self.database.session_factory.begin() as session:
            request = session.get(RiskControlChangeRequest, request_id, with_for_update=True)
            if request is None:
                _reject("RISK_RESTORE_NOT_FOUND", "restore request does not exist")
            self._require_role(session, actor_id, operation, team_id=request.team_id)
            executor = session.get(User, actor_id)
            if executor is None or executor.principal_type != PrincipalType.HUMAN.value:
                _reject("SERVICE_EXECUTION_FORBIDDEN", "risk restoration requires a human executor")
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{request.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return _as_uuid(str(response["policy_id"]))
            if request.version != expected_version:
                _reject("VERSION_CONFLICT", "restore request changed before execution")
            if request.status != RiskPolicyChangeStatus.APPROVED.value:
                _reject("RISK_RESTORE_NOT_APPROVED", "an independent approval is required")
            if request.expires_at <= now:
                _reject("RISK_RESTORE_EXPIRED", "restore request expired before execution")
            if request.execute_after > now:
                _reject("RISK_RESTORE_COOLDOWN", "restore cooldown has not completed")
            approvals = session.scalars(
                select(Approval).where(
                    Approval.risk_control_change_request_id == request_id,
                    Approval.decision == ReviewDecision.APPROVE.value,
                )
            ).all()
            if len({approval.reviewer_id for approval in approvals}) < 1:
                _reject("RISK_RESTORE_NOT_APPROVED", "an independent approval is required")
            if request.requester_id == actor_id:
                _reject("SELF_EXECUTION_FORBIDDEN", "requester cannot execute their restore")
            self._lock_risk_capacity(session, request.team_id)
            policy = self._active_risk_policy(session, request.team_id)
            gate = session.get(CapabilityGate, "AUTO_ADD", with_for_update=True)
            if gate is None:
                _reject("CAPABILITY_GATE_NOT_FOUND", "AUTO_ADD gate is missing")
            if self._risk_restore_request_drifted(request, policy, gate):
                _reject("RISK_RESTORE_CONTROL_DRIFT", "risk controls changed after the request")
            campaigns = list(
                session.scalars(select(Campaign).where(Campaign.team_id == request.team_id)).all()
            )
            current_scopes = self._canonical_restore_scopes(
                configured_scopes,
                campaigns,
                required_environment=(
                    ExecutionEnvironment.LIVE.value if request.require_live_scope else None
                ),
            )
            if current_scopes != request.required_scopes:
                _reject("RISK_RESTORE_SCOPE_DRIFT", "controlled scopes changed after the request")
            if request.require_live_scope != require_live_scope:
                _reject(
                    "RISK_RESTORE_SCOPE_DRIFT",
                    "LIVE scope requirement changed after the request",
                )
            blockers = self._risk_restore_blockers(
                session,
                policy,
                request.required_scopes,
                require_live_scope=request.require_live_scope,
                now=now,
            )
            if blockers:
                _reject("RISK_RESTORE_BLOCKED", ",".join(blockers))
            policy.active = False
            next_revision = policy.revision + 1
            restored = RiskPolicy(
                team_id=request.team_id,
                version=f"restore-{next_revision}-{request.request_id.hex[:12]}",
                revision=next_revision,
                system_state=SystemRiskState.NORMAL.value,
                max_total_risk=policy.max_total_risk,
                max_account_risk=policy.max_account_risk,
                max_single_loss=policy.max_single_loss,
                max_consecutive_losses=policy.max_consecutive_losses,
                loss_cooldown_seconds=policy.loss_cooldown_seconds,
                max_fact_age_seconds=policy.max_fact_age_seconds,
                reason=request.reason,
                active=True,
                updated_by=str(actor_id),
                updated_at=now,
            )
            session.add(restored)
            session.flush()
            authorizations = session.scalars(
                select(TradingAuthorization)
                .where(
                    TradingAuthorization.team_id == request.team_id,
                    TradingAuthorization.active,
                )
                .order_by(TradingAuthorization.authorization_id)
                .with_for_update()
            ).all()
            for authorization in authorizations:
                authorization.active = False
                if authorization.add_revoked_at is None:
                    authorization.add_revoked_at = now
            request.status = RiskPolicyChangeStatus.EXECUTED.value
            request.resulting_policy_id = restored.policy_id
            request.executed_at = now
            request.updated_at = now
            request.version += 1
            result = {"policy_id": str(restored.policy_id), "request_id": str(request_id)}
            self._save_receipt(
                session,
                caller_id=f"{actor_id}:{request.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self._audit(
                session,
                actor_id=str(actor_id),
                event_type="RISK_RESTORE_EXECUTED",
                object_type="RiskControlChangeRequest",
                object_id=request.request_id,
                reason=request.reason,
                correlation_id=request.correlation_id,
                object_version=request.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return restored.policy_id

    def direct_restore_risk_controls(
        self,
        actor_id: UUID,
        idempotency_key: str,
        *,
        reason: str,
        configured_scopes: tuple[tuple[str, str, str], ...],
        require_live_scope: bool = True,
        now: datetime,
    ) -> UUID:
        operation = "risk.restore.direct"
        payload = {
            "reason": reason,
            "configured_scopes": configured_scopes,
            "require_live_scope": require_live_scope,
        }
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
                    "RISK_RESTORE_ADMIN_REQUIRED",
                    "direct risk restoration requires SYSTEM_ADMIN",
                )
            actor = session.get(User, actor_id)
            if actor is None or actor.principal_type != PrincipalType.HUMAN.value:
                _reject("SERVICE_EXECUTION_FORBIDDEN", "direct restoration requires a human")
            digest, response = self._idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if response is not None:
                return _as_uuid(str(response["policy_id"]))
            self._lock_risk_capacity(session, team.team_id)
            policy = self._active_risk_policy(session, team.team_id)
            if policy.system_state == SystemRiskState.NORMAL.value:
                _reject("RISK_CONTROL_ALREADY_NORMAL", "risk policy is already normal")
            gate = session.get(CapabilityGate, "AUTO_ADD", with_for_update=True)
            if gate is None:
                _reject("CAPABILITY_GATE_NOT_FOUND", "AUTO_ADD gate is missing")
            campaigns = list(
                session.scalars(select(Campaign).where(Campaign.team_id == team.team_id)).all()
            )
            scopes = self._canonical_restore_scopes(
                configured_scopes,
                campaigns,
                required_environment=(
                    ExecutionEnvironment.LIVE.value if require_live_scope else None
                ),
            )
            blockers = self._risk_restore_blockers(
                session,
                policy,
                scopes,
                require_live_scope=require_live_scope,
                now=now,
            )
            if blockers:
                _reject("RISK_RESTORE_BLOCKED", ",".join(blockers))
            policy.active = False
            next_revision = policy.revision + 1
            restored = RiskPolicy(
                team_id=team.team_id,
                version=f"direct-restore-{next_revision}-{uuid4().hex[:12]}",
                revision=next_revision,
                system_state=SystemRiskState.NORMAL.value,
                max_total_risk=policy.max_total_risk,
                max_account_risk=policy.max_account_risk,
                max_single_loss=policy.max_single_loss,
                max_consecutive_losses=policy.max_consecutive_losses,
                loss_cooldown_seconds=policy.loss_cooldown_seconds,
                max_fact_age_seconds=policy.max_fact_age_seconds,
                reason=reason,
                active=True,
                updated_by=str(actor_id),
                updated_at=now,
            )
            session.add(restored)
            session.flush()
            pending_requests = session.scalars(
                select(RiskControlChangeRequest)
                .where(
                    RiskControlChangeRequest.team_id == team.team_id,
                    RiskControlChangeRequest.status.in_(
                        {
                            RiskPolicyChangeStatus.PENDING_REVIEW.value,
                            RiskPolicyChangeStatus.APPROVED.value,
                        }
                    ),
                )
                .with_for_update()
            ).all()
            for pending_request in pending_requests:
                pending_request.status = RiskPolicyChangeStatus.EXPIRED.value
                pending_request.version += 1
                pending_request.resulting_policy_id = restored.policy_id
                pending_request.updated_at = now
                self._audit(
                    session,
                    actor_id=str(actor_id),
                    event_type="RISK_RESTORE_SUPERSEDED",
                    object_type="RiskControlChangeRequest",
                    object_id=pending_request.request_id,
                    reason="direct administrator restoration superseded the request",
                    correlation_id=pending_request.correlation_id,
                    object_version=pending_request.version,
                    idempotency_key=idempotency_key,
                    now=now,
                )
            authorizations = session.scalars(
                select(TradingAuthorization)
                .where(
                    TradingAuthorization.team_id == team.team_id,
                    TradingAuthorization.active,
                )
                .order_by(TradingAuthorization.authorization_id)
                .with_for_update()
            ).all()
            for authorization in authorizations:
                authorization.active = False
                if authorization.add_revoked_at is None:
                    authorization.add_revoked_at = now
            result = {"policy_id": str(restored.policy_id)}
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
                event_type="RISK_RESTORE_DIRECT_EXECUTED",
                object_type="RiskPolicy",
                object_id=restored.policy_id,
                reason=reason,
                correlation_id=uuid4(),
                object_version=restored.revision,
                idempotency_key=idempotency_key,
                now=now,
            )
            return restored.policy_id

    def record_scope_reconciliation(
        self,
        execution_scope: str,
        actor_id: UUID,
        status: ReconciliationStatus,
        differences: tuple[str, ...],
        *,
        now: datetime,
        campaign_id: UUID | None = None,
    ) -> UUID:
        if status in {ReconciliationStatus.MATCH, ReconciliationStatus.RESOLVED}:
            _reject(
                "RECONCILIATION_STATUS_NOT_TRUSTED",
                "MATCH must be computed and RESOLVED requires a manual transition",
            )
        with self.database.session_factory.begin() as session:
            _environment, account_id, venue = _scope_parts(execution_scope)
            team = self._require_role(session, actor_id, "reconcile", account_id, venue)
            if campaign_id is not None:
                campaign = session.get(Campaign, campaign_id)
                if campaign is None:
                    _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
                if campaign.team_id != team.team_id:
                    _reject("TEAM_SCOPE_DENIED", "campaign is outside the active team scope")
            run = ReconciliationRun(
                team_id=team.team_id,
                execution_scope=execution_scope,
                campaign_id=campaign_id,
                status=status.value,
                is_computed=False,
                differences=list(differences),
                resolution_reason=None,
                actor_id=str(actor_id),
                correlation_id=uuid4(),
                started_at=now,
                completed_at=now,
            )
            session.add(run)
            session.flush()
            RECONCILIATION_RESULTS.labels(status.value).inc()
            return run.reconciliation_id

    def require_manual_reconciliation(
        self, reconciliation_id: UUID, actor_id: UUID, reason: str, *, now: datetime
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            run = session.get(ReconciliationRun, reconciliation_id, with_for_update=True)
            if run is None:
                _reject("RECONCILIATION_NOT_FOUND", "run does not exist")
            _environment, account_id, venue = _scope_parts(run.execution_scope)
            self._require_role(
                session,
                actor_id,
                "reconcile",
                account_id,
                venue,
                team_id=run.team_id,
            )
            if run.status not in {
                ReconciliationStatus.DIFFERENCE.value,
                ReconciliationStatus.UNKNOWN.value,
            }:
                _reject(
                    "RECONCILIATION_TRANSITION_INVALID",
                    "only DIFFERENCE or UNKNOWN may require manual handling",
                )
            run.status = ReconciliationStatus.MANUAL_REQUIRED.value
            run.resolution_reason = reason
            run.completed_at = now
            return run.reconciliation_id

    def resolve_reconciliation(
        self, reconciliation_id: UUID, actor_id: UUID, reason: str, *, now: datetime
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            run = session.get(ReconciliationRun, reconciliation_id, with_for_update=True)
            if run is None:
                _reject("RECONCILIATION_NOT_FOUND", "run does not exist")
            _environment, account_id, venue = _scope_parts(run.execution_scope)
            self._require_role(
                session,
                actor_id,
                "reconcile",
                account_id,
                venue,
                team_id=run.team_id,
            )
            if run.status != ReconciliationStatus.MANUAL_REQUIRED.value:
                _reject(
                    "RECONCILIATION_TRANSITION_INVALID",
                    "only MANUAL_REQUIRED may be resolved",
                )
            run.status = ReconciliationStatus.RESOLVED.value
            run.resolution_reason = reason
            run.completed_at = now
            return run.reconciliation_id

    def reconciliation_status(self, reconciliation_id: UUID) -> ReconciliationStatus:
        with self.database.session_factory() as session:
            run = session.get(ReconciliationRun, reconciliation_id)
            if run is None:
                _reject("RECONCILIATION_NOT_FOUND", "run does not exist")
            return ReconciliationStatus(run.status)

    @staticmethod
    def _fact_is_stale(observed_at: datetime, now: datetime, max_age: timedelta) -> bool:
        return fact_is_stale(observed_at, now, max_age)

    def reconcile_scope(
        self,
        execution_scope: str,
        actor_id: UUID,
        *,
        now: datetime,
    ) -> UUID:
        environment, account_id, venue = _scope_parts(execution_scope)
        with self.database.session_factory.begin() as session:
            team = self._require_role(session, actor_id, "reconcile", account_id, venue)
            policy = session.scalar(select(RiskPolicy).where(RiskPolicy.active))
            max_age = (
                timedelta(seconds=policy.max_fact_age_seconds)
                if policy is not None
                else timedelta(0)
            )
            campaigns = session.scalars(
                select(Campaign)
                .where(
                    Campaign.team_id == team.team_id,
                    Campaign.account_id == account_id,
                    Campaign.venue == venue,
                    Campaign.environment == environment.value,
                    Campaign.status != CampaignStatus.CLOSED.value,
                )
                .order_by(Campaign.created_at, Campaign.campaign_id)
                .with_for_update()
            ).all()
            equity = session.scalar(
                select(AccountEquity)
                .where(
                    AccountEquity.team_id == team.team_id,
                    AccountEquity.account_id == account_id,
                    AccountEquity.venue == venue,
                    AccountEquity.environment == environment.value,
                )
                .with_for_update()
            )
            differences: list[str] = []
            unknown: list[str] = []
            if policy is None:
                unknown.append("RISK_POLICY_UNKNOWN")
            if equity is None or equity.fact_status != FactStatus.KNOWN.value:
                unknown.append("ACCOUNT_EQUITY_UNKNOWN")
            elif self._fact_is_stale(equity.observed_at, now, max_age):
                unknown.append("ACCOUNT_EQUITY_STALE")

            protection_order_ids = set(
                session.scalars(
                    select(ProtectionOrder.venue_order_id)
                    .join(Position, ProtectionOrder.position_id == Position.position_id)
                    .where(
                        Position.team_id == team.team_id,
                        Position.account_id == account_id,
                        Position.venue == venue,
                        Position.environment == environment.value,
                    )
                ).all()
            )
            unbound_orders = session.scalars(
                select(VenueOrder).where(
                    VenueOrder.team_id == team.team_id,
                    VenueOrder.account_id == account_id,
                    VenueOrder.venue == venue,
                    VenueOrder.environment == environment.value,
                    VenueOrder.order_intent_id.is_(None),
                    VenueOrder.status.in_(
                        {
                            VenueOrderStatus.SENT.value,
                            VenueOrderStatus.PARTIALLY_FILLED.value,
                            VenueOrderStatus.UNKNOWN.value,
                        }
                    ),
                )
            ).all()
            for unbound_order in unbound_orders:
                if unbound_order.venue_order_id in protection_order_ids:
                    continue
                if unbound_order.status == VenueOrderStatus.UNKNOWN.value:
                    unknown.append(f"EXTERNAL_ORDER_UNKNOWN:{unbound_order.venue_order_id}")
                else:
                    differences.append(f"EXTERNAL_ORDER_UNBOUND:{unbound_order.venue_order_id}")

            active_instrument_ids = {campaign.instrument_id for campaign in campaigns}
            scope_positions = session.scalars(
                select(Position).where(
                    Position.team_id == team.team_id,
                    Position.account_id == account_id,
                    Position.venue == venue,
                    Position.environment == environment.value,
                )
            ).all()
            for scope_position in scope_positions:
                if scope_position.instrument_id not in active_instrument_ids:
                    if scope_position.fact_status != FactStatus.KNOWN.value:
                        unknown.append(f"POSITION_UNKNOWN:{scope_position.instrument_id}")
                    elif self._fact_is_stale(scope_position.observed_at, now, max_age):
                        unknown.append(f"POSITION_STALE:{scope_position.instrument_id}")
                if (
                    scope_position.quantity != 0
                    and scope_position.instrument_id not in active_instrument_ids
                ):
                    differences.append(f"EXTERNAL_POSITION_UNBOUND:{scope_position.instrument_id}")

            for campaign in campaigns:
                scope_suffix = str(campaign.campaign_id)
                instrument = session.get(Instrument, campaign.instrument_id)
                if instrument is None:
                    unknown.append(f"INSTRUMENT_UNKNOWN:{scope_suffix}")
                elif equity is not None and equity.currency != instrument.collateral_currency:
                    differences.append(f"EQUITY_CURRENCY_MISMATCH:{scope_suffix}")
                intents = session.scalars(
                    select(OrderIntent)
                    .where(OrderIntent.campaign_id == campaign.campaign_id)
                    .order_by(OrderIntent.created_at, OrderIntent.intent_id)
                    .with_for_update()
                ).all()
                fills = session.scalars(
                    select(VenueFill).where(VenueFill.campaign_id == campaign.campaign_id)
                ).all()
                reservations = session.scalars(
                    select(RiskReservation)
                    .where(RiskReservation.campaign_id == campaign.campaign_id)
                    .with_for_update()
                ).all()
                if not intents:
                    differences.append(f"ORDER_INTENT_MISSING:{scope_suffix}")
                for reservation in reservations:
                    if reservation.status == ReservationStatus.UNKNOWN.value:
                        unknown.append(f"RISK_RESERVATION_UNKNOWN:{reservation.reservation_id}")

                for intent in intents:
                    intent_fills = [
                        fill for fill in fills if fill.order_intent_id == intent.intent_id
                    ]
                    intent_fill_quantity = sum((fill.quantity for fill in intent_fills), Decimal(0))
                    intent_order = session.scalar(
                        select(VenueOrder)
                        .where(VenueOrder.order_intent_id == intent.intent_id)
                        .with_for_update()
                    )
                    order_required = intent.status in {
                        OrderIntentStatus.SENT.value,
                        OrderIntentStatus.PARTIALLY_FILLED.value,
                        OrderIntentStatus.FILLED.value,
                        OrderIntentStatus.UNKNOWN.value,
                    }
                    if intent_order is None and order_required:
                        differences.append(f"VENUE_ORDER_MISSING:{intent.intent_id}")
                    elif intent_order is not None:
                        if intent_order.venue != venue:
                            differences.append(f"VENUE_ORDER_SCOPE_MISMATCH:{intent.intent_id}")
                        if intent_order.filled_quantity != intent_fill_quantity:
                            differences.append(f"ORDER_FILL_MISMATCH:{intent.intent_id}")
                        if intent_order.status == VenueOrderStatus.UNKNOWN.value:
                            unknown.append(f"VENUE_ORDER_UNKNOWN:{intent.intent_id}")
                        elif intent_order.status not in {
                            VenueOrderStatus.FILLED.value,
                            VenueOrderStatus.CANCELLED.value,
                            VenueOrderStatus.REJECTED.value,
                        } and self._fact_is_stale(intent_order.observed_at, now, max_age):
                            unknown.append(f"VENUE_ORDER_STALE:{intent.intent_id}")
                    if intent_fill_quantity > intent.quantity:
                        differences.append(f"ORDER_INTENT_OVERFILLED:{intent.intent_id}")
                    if intent.status == OrderIntentStatus.UNKNOWN.value:
                        unknown.append(f"ORDER_INTENT_UNKNOWN:{intent.intent_id}")
                    elif intent.status == OrderIntentStatus.DISPATCHING.value:
                        unknown.append(f"ORDER_DISPATCH_UNRESOLVED:{intent.intent_id}")
                    if (
                        intent.status == OrderIntentStatus.FILLED.value
                        and intent_order is not None
                        and intent_fill_quantity != intent_order.filled_quantity
                    ):
                        differences.append(f"INTENT_FILL_STATE_MISMATCH:{intent.intent_id}")

                position = session.scalar(
                    select(Position)
                    .where(
                        Position.team_id == campaign.team_id,
                        Position.account_id == campaign.account_id,
                        Position.venue == campaign.venue,
                        Position.environment == campaign.environment,
                        Position.instrument_id == campaign.instrument_id,
                    )
                    .with_for_update()
                )
                if position is None or position.fact_status != FactStatus.KNOWN.value:
                    unknown.append(f"POSITION_UNKNOWN:{scope_suffix}")
                    continue
                if self._fact_is_stale(position.observed_at, now, max_age):
                    unknown.append(f"POSITION_STALE:{scope_suffix}")
                signed_fills = sum(
                    (fill.quantity if fill.side == "BUY" else -fill.quantity for fill in fills),
                    Decimal(0),
                )
                if signed_fills != position.quantity:
                    differences.append(f"POSITION_QUANTITY_MISMATCH:{scope_suffix}")
                if position.quantity != 0:
                    protection = session.scalar(
                        select(ProtectionOrder)
                        .where(ProtectionOrder.position_id == position.position_id)
                        .with_for_update()
                    )
                    if protection is None or protection.status == ProtectionStatus.UNKNOWN.value:
                        unknown.append(f"PROTECTION_UNKNOWN:{scope_suffix}")
                    elif self._fact_is_stale(protection.observed_at, now, max_age):
                        unknown.append(f"PROTECTION_STALE:{scope_suffix}")
                    elif (
                        protection.status != ProtectionStatus.ACTIVE.value
                        or not protection.fully_covered
                        or protection.quantity < abs(position.quantity)
                    ):
                        differences.append(f"PROTECTION_INSUFFICIENT:{scope_suffix}")

            if unknown:
                status = ReconciliationStatus.UNKNOWN
                result_differences = sorted(set(unknown + differences))
            elif differences:
                status = ReconciliationStatus.DIFFERENCE
                result_differences = sorted(set(differences))
            else:
                status = ReconciliationStatus.MATCH
                result_differences = []
            run = ReconciliationRun(
                team_id=team.team_id,
                execution_scope=execution_scope,
                campaign_id=None,
                status=status.value,
                is_computed=True,
                differences=result_differences,
                resolution_reason=None,
                actor_id=str(actor_id),
                correlation_id=uuid4(),
                started_at=now,
                completed_at=now,
            )
            session.add(run)
            session.flush()
            RECONCILIATION_RESULTS.labels(status.value).inc()
            return run.reconciliation_id

    def reconcile_campaign(
        self,
        campaign_id: UUID,
        execution_scope: str,
        actor_id: UUID,
        *,
        now: datetime,
    ) -> UUID:
        environment, account_id, venue = _scope_parts(execution_scope)
        with self.database.session_factory() as session:
            campaign = session.get(Campaign, campaign_id)
            if campaign is None:
                _reject("CAMPAIGN_NOT_FOUND", "campaign does not exist")
            self._require_role(
                session,
                actor_id,
                "reconcile",
                campaign.account_id,
                campaign.venue,
                team_id=campaign.team_id,
            )
            if (
                campaign.account_id != account_id
                or campaign.venue != venue
                or campaign.environment != environment.value
            ):
                _reject("EXECUTION_SCOPE_MISMATCH", "campaign is outside reconciliation scope")
        return self.reconcile_scope(execution_scope, actor_id, now=now)
