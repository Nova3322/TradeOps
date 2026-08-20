from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trading_control_plane import domain, metrics, models, notilt, rejections
from trading_control_plane import execution_scope as scope_rules
from trading_control_plane.service_component import ServiceComponent
from trading_control_plane.service_domains.notifications import enqueue_notification_event
from trading_control_plane.service_transactions import TransactionService


def occupied_risk(
    session: Session,
    team_id: UUID,
    *,
    account_id: str | None = None,
    venue: str | None = None,
) -> Decimal:
    query = (
        select(models.RiskReservation)
        .join(models.Campaign, models.Campaign.campaign_id == models.RiskReservation.campaign_id)
        .where(
            models.RiskReservation.team_id == team_id,
            models.Campaign.team_id == team_id,
            models.RiskReservation.status.in_(scope_rules.OCCUPIED_RESERVATION_STATUSES),
        )
    )
    if account_id is not None:
        query = query.where(models.Campaign.account_id == account_id)
    if venue is not None:
        query = query.where(models.Campaign.venue == venue)
    reservations = session.scalars(query.with_for_update(of=models.RiskReservation)).all()
    return sum((reservation.amount for reservation in reservations), Decimal(0))


def active_risk_policy(session: Session, team_id: UUID) -> models.RiskPolicy:
    policy = session.scalar(
        select(models.RiskPolicy)
        .where(models.RiskPolicy.team_id == team_id, models.RiskPolicy.active)
        .with_for_update()
    )
    if policy is None:
        rejections.reject("RISK_POLICY_MISSING", "no active risk policy exists")
    return policy


def risk_policy_input(
    policy: models.RiskPolicy,
    *,
    effective_max_total_risk: Decimal | None = None,
) -> domain.RiskPolicyInput:
    return domain.RiskPolicyInput(
        version=policy.version,
        system_state=domain.SystemRiskState(policy.system_state),
        max_total_risk=(
            policy.max_total_risk if effective_max_total_risk is None else effective_max_total_risk
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


def managed_capital_context(
    session: Session,
    *,
    team_id: UUID,
    environment: str,
    now: datetime,
    max_age: timedelta,
) -> tuple[bool, Decimal, list[dict[str, Any]], datetime]:
    rows = session.scalars(
        select(models.AccountEquity)
        .where(
            models.AccountEquity.team_id == team_id,
            models.AccountEquity.environment == environment,
        )
        .order_by(
            models.AccountEquity.location_type,
            models.AccountEquity.venue,
            models.AccountEquity.account_id,
            models.AccountEquity.currency,
        )
        .with_for_update()
    ).all()
    if environment == domain.ExecutionEnvironment.LIVE.value:
        active_accounts = set(
            session.execute(
                select(models.ExchangeAccount.account_id, models.ExchangeAccount.venue).where(
                    models.ExchangeAccount.team_id == team_id,
                    models.ExchangeAccount.environment == domain.ExecutionEnvironment.LIVE.value,
                    models.ExchangeAccount.active.is_(True),
                    models.ExchangeAccount.deleted_at.is_(None),
                )
            ).all()
        )
        rows = [
            row
            for row in rows
            if row.location_type != "VENUE" or (row.account_id, row.venue) in active_accounts
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
        if row.currency.upper() in notilt.USD_STABLE_ASSETS:
            unit_price = Decimal(1)
            value = row.equity
        else:
            unit_price = row.valuation_price
            value = row.valuation_equity
            if row.valuation_observed_at is not None:
                valuation_time = min(valuation_time, row.valuation_observed_at)
        control_known = row.location_type != "VAULT" or row.control_status == "CONTROLLED"
        row_known = (
            row.fact_status == domain.FactStatus.KNOWN.value
            and control_known
            and unit_price is not None
            and unit_price > 0
            and value is not None
            and value >= 0
            and not scope_rules.fact_is_stale(valuation_time, now, max_age)
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
        select(models.CapitalTransfer).where(
            models.CapitalTransfer.team_id == team_id,
            models.CapitalTransfer.environment == environment,
            models.CapitalTransfer.status.in_(scope_rules.OCCUPIED_CAPITAL_STATUSES),
        )
    ).all()
    occupied_usd = Decimal(0)
    for transfer in occupied:
        source_venue = (
            transfer.venue
            if transfer.direction == domain.CapitalDirection.VENUE_TO_VAULT
            else "VAULT"
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


def server_risk_context(
    session: Session,
    *,
    proposal: models.Proposal,
    policy: models.RiskPolicy,
    kind: domain.IntentKind,
    requested_quantity: Decimal,
    requested_risk: Decimal,
    current_risk: Decimal,
    now: datetime,
) -> tuple[domain.RiskEvaluationInput, dict[str, Any], datetime, Decimal]:
    instrument = session.get(models.Instrument, proposal.instrument_id)
    if instrument is None or not instrument.active:
        rejections.reject("INSTRUMENT_UNAVAILABLE", "proposal instrument is unavailable")
    position = session.scalar(
        select(models.Position)
        .where(
            models.Position.team_id == proposal.team_id,
            models.Position.account_id == proposal.account_id,
            models.Position.venue == proposal.venue,
            models.Position.environment == proposal.environment,
            models.Position.instrument_id == proposal.instrument_id,
        )
        .with_for_update()
    )
    equity = session.scalar(
        select(models.AccountEquity)
        .where(
            models.AccountEquity.team_id == proposal.team_id,
            models.AccountEquity.account_id == proposal.account_id,
            models.AccountEquity.venue == proposal.venue,
            models.AccountEquity.environment == proposal.environment,
            models.AccountEquity.currency == instrument.collateral_currency,
        )
        .with_for_update()
    )
    protection = None
    if position is not None:
        protection = session.scalar(
            select(models.ProtectionOrder)
            .where(models.ProtectionOrder.position_id == position.position_id)
            .with_for_update()
        )

    max_age = timedelta(seconds=policy.max_fact_age_seconds)
    source_health = (
        session.scalar(
            select(models.RuntimeSourceHealth).where(
                models.RuntimeSourceHealth.team_id == proposal.team_id,
                models.RuntimeSourceHealth.source_name == proposal.venue,
                models.RuntimeSourceHealth.account_id == proposal.account_id,
                models.RuntimeSourceHealth.venue == proposal.venue,
            )
        )
        if proposal.environment == domain.ExecutionEnvironment.LIVE.value
        else None
    )
    source_current = proposal.environment != domain.ExecutionEnvironment.LIVE.value or (
        source_health is not None
        and source_health.status == "SUCCESS"
        and not scope_rules.fact_is_stale(source_health.checked_at, now, max_age)
    )

    position_known = position is not None and position.fact_status == domain.FactStatus.KNOWN.value
    venue_equity_known = (
        equity is not None
        and equity.fact_status == domain.FactStatus.KNOWN.value
        and equity.currency == instrument.collateral_currency
    )
    capital_known, managed_capital_usd, managed_facts, capital_as_of = managed_capital_context(
        session,
        team_id=proposal.team_id,
        environment=proposal.environment,
        now=now,
        max_age=max_age,
    )
    equity_known = venue_equity_known and capital_known
    protection_required = kind is domain.IntentKind.ADD or (
        position_known and position is not None and position.quantity != 0
    )
    protection_known = not protection_required or (
        protection is not None
        and protection.status == domain.ProtectionStatus.ACTIVE.value
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
        timedelta(0)
        if -scope_rules.MAX_FACT_CLOCK_SKEW <= raw_fact_age < timedelta(0)
        else raw_fact_age
    )
    current_account_risk = occupied_risk(
        session,
        proposal.team_id,
        account_id=proposal.account_id,
        venue=proposal.venue,
    )
    team_loss_streak, account_loss_streak, loss_cooldown_remaining = loss_limit_context(
        session,
        proposal=proposal,
        policy=policy,
        now=now,
    )
    inputs = domain.RiskEvaluationInput(
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


def decide_risk_in_session(
    transactions: TransactionService,
    session: Session,
    *,
    proposal: models.Proposal,
    actor_id: UUID,
    kind: domain.IntentKind,
    idempotency_key: str,
    now: datetime,
    requested_quantity: Decimal | None = None,
) -> UUID:
    """Record one risk decision inside the caller's transaction."""

    operation = "risk.decide"
    transactions.require_role(
        session,
        actor_id,
        operation,
        proposal.account_id,
        proposal.venue,
        team_id=proposal.team_id,
    )
    quantity = proposal.quantity if requested_quantity is None else requested_quantity
    request_payload = {
        "proposal_id": str(proposal.proposal_id),
        "team_id": str(proposal.team_id),
        "kind": kind.value,
        "requested_quantity": str(quantity),
    }
    digest, response = transactions.idempotency(
        session,
        caller_id=f"{actor_id}:{proposal.team_id}",
        operation=operation,
        idempotency_key=idempotency_key,
        payload=request_payload,
    )
    if response is not None:
        return UUID(str(response["decision_id"]))
    if (
        proposal.status != domain.ProposalStatus.APPROVED.value
        or proposal.expires_at <= now
    ):
        rejections.reject(
            "PROPOSAL_NOT_APPROVED", "risk decision requires a live approved proposal"
        )
    if quantity <= 0 or quantity > proposal.quantity:
        rejections.reject(
            "PROPOSAL_QUANTITY_EXCEEDED", "requested quantity exceeds proposal cap"
        )

    transactions.lock_risk_capacity(session, proposal.team_id)
    policy = active_risk_policy(session, proposal.team_id)
    current_risk = occupied_risk(session, proposal.team_id)
    requested_risk = proposal.max_risk * quantity / proposal.quantity
    if requested_risk > proposal.max_risk:
        rejections.reject("PROPOSAL_RISK_EXCEEDED", "requested risk exceeds proposal cap")
    inputs, facts, data_as_of, effective_max_total_risk = server_risk_context(
        session,
        proposal=proposal,
        policy=policy,
        kind=kind,
        requested_quantity=quantity,
        requested_risk=requested_risk,
        current_risk=current_risk,
        now=now,
    )
    outcome = domain.evaluate_risk(
        risk_policy_input(
            policy,
            effective_max_total_risk=(
                effective_max_total_risk
                if effective_max_total_risk > 0
                else policy.max_total_risk
            ),
        ),
        inputs,
    )
    decision = models.RiskDecision(
        team_id=proposal.team_id,
        proposal_id=proposal.proposal_id,
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
    transactions.save_receipt(
        session,
        caller_id=f"{actor_id}:{proposal.team_id}",
        operation=operation,
        idempotency_key=idempotency_key,
        semantic_hash=digest,
        response=result,
        now=now,
    )
    transactions.audit(
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
    team = session.get(models.Team, proposal.team_id)
    assert team is not None
    enqueue_notification_event(
        transactions,
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
    metrics.RISK_RESULTS.labels(outcome.result.value).inc()
    return decision.decision_id


def consecutive_loss_snapshot(
    session: Session,
    *,
    team_id: UUID,
    environment: str,
    account_id: str | None = None,
    venue: str | None = None,
) -> tuple[int, datetime | None]:
    query = (
        select(models.Campaign)
        .where(
            models.Campaign.team_id == team_id,
            models.Campaign.environment == environment,
            models.Campaign.status == domain.CampaignStatus.CLOSED.value,
        )
        .order_by(models.Campaign.updated_at.desc(), models.Campaign.campaign_id.desc())
    )
    if account_id is not None:
        query = query.where(models.Campaign.account_id == account_id)
    if venue is not None:
        query = query.where(models.Campaign.venue == venue)
    streak = 0
    latest_loss_at: datetime | None = None
    for campaign in session.scalars(query):
        if campaign.final_pnl >= 0:
            break
        streak += 1
        if latest_loss_at is None:
            latest_loss_at = campaign.updated_at
    return streak, latest_loss_at


def loss_limit_context(
    session: Session,
    *,
    proposal: models.Proposal,
    policy: models.RiskPolicy,
    now: datetime,
) -> tuple[int, int, timedelta]:
    team_streak, team_latest_loss_at = consecutive_loss_snapshot(
        session,
        team_id=proposal.team_id,
        environment=proposal.environment,
    )
    account_streak, account_latest_loss_at = consecutive_loss_snapshot(
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


class PolicyRiskService(ServiceComponent):
    def set_risk_policy(
        self,
        *,
        actor_id: UUID,
        version: str,
        system_state: domain.SystemRiskState,
        max_total_risk: Decimal,
        max_account_risk: Decimal,
        max_single_loss: Decimal,
        max_consecutive_losses: int,
        loss_cooldown: timedelta,
        max_fact_age: timedelta,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            team = self.transactions.require_role(session, actor_id, "risk_policy.manage")
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
                rejections.reject("RISK_POLICY_INVALID", "all risk limits must be explicitly valid")
            self.transactions.lock_risk_capacity(session, team.team_id)
            for current in session.scalars(
                select(models.RiskPolicy).where(
                    models.RiskPolicy.team_id == team.team_id,
                    models.RiskPolicy.active,
                )
            ).all():
                if (
                    system_state is domain.SystemRiskState.NORMAL
                    and current.system_state != domain.SystemRiskState.NORMAL.value
                ):
                    rejections.reject(
                        "REVIEWED_RESTORE_REQUIRED",
                        "a tightened risk policy may only return to NORMAL through "
                        "reviewed restore",
                    )
                current.active = False
            previous_revision = (
                session.scalar(
                    select(func.max(models.RiskPolicy.revision)).where(
                        models.RiskPolicy.team_id == team.team_id
                    )
                )
                or 0
            )
            policy = models.RiskPolicy(
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
            self.transactions.audit(
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
            rejections.reject("RISK_POLICY_INVALID", "all risk limits must be explicitly valid")
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
            team = self.transactions.require_role(session, actor_id, "risk_policy.manage")
            digest, replay = self.transactions.idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return UUID(str(replay["policy_id"]))
            self.transactions.lock_risk_capacity(session, team.team_id)
            current = session.scalar(
                select(models.RiskPolicy)
                .where(models.RiskPolicy.team_id == team.team_id, models.RiskPolicy.active)
                .with_for_update()
            )
            current_revision = 0 if current is None else current.revision
            if current_revision != expected_revision:
                rejections.reject("VERSION_CONFLICT", "risk policy changed before configuration")
            if session.scalar(
                select(models.RiskPolicy.policy_id).where(
                    models.RiskPolicy.team_id == team.team_id,
                    models.RiskPolicy.version == version,
                )
            ):
                rejections.reject(
                    "RISK_POLICY_VERSION_CONFLICT", "risk policy version already exists"
                )
            if current is not None:
                current.active = False
            policy = models.RiskPolicy(
                team_id=team.team_id,
                version=version,
                revision=current_revision + 1,
                system_state=(
                    domain.SystemRiskState.NORMAL.value if current is None else current.system_state
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
                select(models.TradingAuthorization)
                .where(
                    models.TradingAuthorization.team_id == team.team_id,
                    models.TradingAuthorization.active,
                )
                .with_for_update()
            ):
                authorization.active = False
                if authorization.add_revoked_at is None:
                    authorization.add_revoked_at = now
            result = {"policy_id": str(policy.policy_id), "revision": policy.revision}
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

    def decide_risk(
        self,
        *,
        proposal_id: UUID,
        actor_id: UUID,
        kind: domain.IntentKind,
        idempotency_key: str,
        now: datetime,
        requested_quantity: Decimal | None = None,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            proposal = session.get(models.Proposal, proposal_id, with_for_update=True)
            if proposal is None:
                rejections.reject("PROPOSAL_NOT_FOUND", "proposal does not exist")
            return decide_risk_in_session(
                self.transactions,
                session,
                proposal=proposal,
                actor_id=actor_id,
                kind=kind,
                idempotency_key=idempotency_key,
                now=now,
                requested_quantity=requested_quantity,
            )
