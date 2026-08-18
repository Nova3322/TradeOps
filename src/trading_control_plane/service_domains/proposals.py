from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from trading_control_plane import domain, models, rejections, request_context, runtime_contracts
from trading_control_plane import execution_scope as scope_rules
from trading_control_plane.service_component import ServiceComponent
from trading_control_plane.service_domains.accounts import (
    ensure_exchange_account_reference,
    lock_perptape_runtime_binding,
)
from trading_control_plane.service_domains.notifications import enqueue_proposal_review_notification


def manual_execution_key(
    *,
    environment: str,
    account_id: str,
    venue: str,
    instrument_id: UUID,
    direction: str,
    risk_tier: str,
    quantity: Decimal,
    max_risk: Decimal,
    expires_in_minutes: int,
    details: dict[str, Any],
) -> tuple[Any, ...]:
    """Compare frozen trade instructions while leaving human commentary out of the key."""

    def decimal_detail(name: str, fallback: Decimal | None = None) -> Decimal | None:
        value = details.get(name)
        return fallback if value is None else Decimal(str(value))

    return (
        environment,
        account_id,
        venue,
        instrument_id,
        direction,
        risk_tier,
        quantity,
        max_risk,
        expires_in_minutes,
        decimal_detail("trigger_price"),
        decimal_detail("limit_price"),
        decimal_detail("invalidation_price"),
        decimal_detail("initial_quantity", quantity),
        bool(details.get("allow_auto_add", False)),
        int(details.get("requested_adds", 0)),
        decimal_detail("add_trigger_price"),
    )


def proposal_manual_execution_key(proposal: models.Proposal) -> tuple[Any, ...]:
    return manual_execution_key(
        environment=proposal.environment,
        account_id=proposal.account_id,
        venue=proposal.venue,
        instrument_id=proposal.instrument_id,
        direction=proposal.direction,
        risk_tier=proposal.risk_tier,
        quantity=proposal.quantity,
        max_risk=proposal.max_risk,
        expires_in_minutes=round((proposal.expires_at - proposal.created_at).total_seconds() / 60),
        details=dict(proposal.frozen_payload.get("details") or {}),
    )


def system_proposal_strategy_family(strategy_id: str) -> tuple[str, tuple[str, ...]]:
    """Treat one-click and automatic Perptape proposal entry points as one signal family."""

    if strategy_id in {"perptape", "perptape-resonance"}:
        return "perptape", ("perptape", "perptape-resonance")
    return strategy_id, (strategy_id,)


def is_manual_proposal_originator(
    session: Session,
    proposal: models.Proposal,
    user_id: UUID,
) -> bool:
    if proposal.proposer_id == user_id:
        return True
    return (
        session.scalar(
            select(models.AuditEvent.audit_event_id)
            .where(
                models.AuditEvent.event_type == "PROPOSAL_DUPLICATE_REUSED",
                models.AuditEvent.object_type == "Proposal",
                models.AuditEvent.object_id == str(proposal.proposal_id),
                models.AuditEvent.actor_id == str(user_id),
            )
            .limit(1)
        )
        is not None
    )


class ProposalService(ServiceComponent):
    def proposal_default_config(self, actor_id: UUID) -> dict[str, Any] | None:
        with self.database.session_factory() as session:
            team = self.transactions.require_action_assignment(
                session, actor_id, "proposal.create"
            )
            config = session.scalar(
                select(models.ProposalDefaultConfig).where(
                    models.ProposalDefaultConfig.team_id == team.team_id,
                    models.ProposalDefaultConfig.active,
                )
            )
            if config is None:
                return None
            updater = session.get(models.User, config.updated_by)
            return self._proposal_default_payload(config, updater, team.workspace_id)

    @staticmethod
    def _proposal_default_payload(
        config: models.ProposalDefaultConfig,
        updater: models.User | None,
        workspace_id: UUID,
    ) -> dict[str, Any]:
        return {
            "config_id": str(config.config_id),
            "workspace_id": str(workspace_id),
            "team_id": str(config.team_id),
            "version": config.version,
            "environment": config.environment,
            "account_id": config.account_id,
            "risk_tier": config.risk_tier,
            "notional": str(config.notional),
            "max_risk": str(config.max_risk),
            "invalidation_bps": config.invalidation_bps,
            "expires_in_minutes": config.expires_in_minutes,
            "rationale": config.rationale,
            "auto_proposal_enabled": config.auto_proposal_enabled,
            "auto_proposal_min_timeframes": config.auto_proposal_min_timeframes,
            "updated_by": str(config.updated_by),
            "updated_by_username": None if updater is None else updater.username,
            "effective_at": config.effective_at.isoformat(),
        }

    def proposal_automation_config(self, actor_id: UUID) -> dict[str, Any] | None:
        """Return the active admin policy to the internal feed worker only."""
        with self.database.session_factory() as session:
            actor = session.get(models.User, actor_id)
            if (
                actor is None
                or not actor.active
                or actor.principal_type != domain.PrincipalType.SERVICE.value
            ):
                rejections.reject(
                    "PROPOSAL_AUTOMATION_SERVICE_REQUIRED",
                    "automatic proposal policy is restricted to an active service principal",
                )
            team = self.transactions.require_action_assignment(
                session,
                actor_id,
                "proposal.create",
                allow_setup=True,
            )
            # Feed synchronization remains read-only while setup is incomplete.
            # Never expose the automatic proposal policy until the Team has
            # explicitly entered TESTNET or LIVE and passed its operational gates.
            if (
                not team.trading_enabled
                or team.execution_mode == domain.TeamExecutionMode.SETUP.value
            ):
                return None
            source = session.scalar(
                select(models.TeamSignalSource).where(
                    models.TeamSignalSource.team_id == team.team_id,
                    models.TeamSignalSource.mode == domain.SignalSourceMode.PERPTAPE.value,
                    models.TeamSignalSource.deleted_at.is_(None),
                )
            )
            if (
                source is None
                or not source.enabled
                or source.mode != domain.SignalSourceMode.PERPTAPE.value
            ):
                return None
            config = session.scalar(
                select(models.ProposalDefaultConfig).where(
                    models.ProposalDefaultConfig.team_id == team.team_id,
                    models.ProposalDefaultConfig.active,
                )
            )
            if config is None:
                return None
            return self._proposal_default_payload(
                config,
                session.get(models.User, config.updated_by),
                team.workspace_id,
            )

    def set_proposal_default_config(
        self,
        actor_id: UUID,
        idempotency_key: str,
        *,
        account_id: str,
        risk_tier: domain.RiskTier,
        notional: Decimal,
        max_risk: Decimal,
        invalidation_bps: int,
        expires_in_minutes: int,
        rationale: str,
        auto_proposal_enabled: bool,
        auto_proposal_min_timeframes: int,
        now: datetime,
    ) -> UUID:
        operation = "proposal.defaults.manage"
        payload = {
            "environment": domain.ExecutionEnvironment.LIVE.value,
            "account_id": account_id,
            "risk_tier": risk_tier.value,
            "notional": str(notional),
            "max_risk": str(max_risk),
            "invalidation_bps": invalidation_bps,
            "expires_in_minutes": expires_in_minutes,
            "rationale": rationale,
            "auto_proposal_enabled": auto_proposal_enabled,
            "auto_proposal_min_timeframes": auto_proposal_min_timeframes,
        }
        if not account_id.strip() or account_id != account_id.strip():
            rejections.reject(
                "PROPOSAL_DEFAULT_INVALID", "default account ID must be non-empty and exact"
            )
        if notional <= 0 or max_risk <= 0:
            rejections.reject("PROPOSAL_DEFAULT_INVALID", "default amounts must be positive")
        if not 1 <= invalidation_bps <= 5_000 or not 480 <= expires_in_minutes <= 1_440:
            rejections.reject(
                "PROPOSAL_DEFAULT_INVALID", "default price distance or expiry is invalid"
            )
        if auto_proposal_min_timeframes not in {3, 4}:
            rejections.reject(
                "PROPOSAL_DEFAULT_INVALID",
                "automatic proposal threshold must be three or four timeframes",
            )
        with self.database.session_factory.begin() as session:
            team = self.transactions.require_role(session, actor_id, operation)
            self.transactions.require_team_environment(team, domain.ExecutionEnvironment.LIVE)
            assignments = session.scalars(
                select(models.RoleAssignment).where(
                    models.RoleAssignment.user_id == actor_id,
                    models.RoleAssignment.team_id == team.team_id,
                )
            ).all()
            if not any(item.role == domain.Role.SYSTEM_ADMIN.value for item in assignments):
                rejections.reject(
                    "PROPOSAL_DEFAULT_ADMIN_REQUIRED",
                    "only a team SYSTEM_ADMIN can change team proposal defaults",
                )
            source = session.scalar(
                select(models.TeamSignalSource).where(
                    models.TeamSignalSource.team_id == team.team_id,
                    models.TeamSignalSource.mode == domain.SignalSourceMode.PERPTAPE.value,
                    models.TeamSignalSource.deleted_at.is_(None),
                )
            )
            if auto_proposal_enabled and (
                source is None
                or not source.enabled
                or source.mode != domain.SignalSourceMode.PERPTAPE.value
            ):
                rejections.reject(
                    "AUTO_PROPOSAL_SOURCE_INVALID",
                    "automatic proposals require the active team Perptape source",
                )
            digest, response = self.transactions.idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload={**payload, "team_id": str(team.team_id)},
            )
            if response is not None:
                return UUID(str(response["config_id"]))
            current = session.scalar(
                select(models.ProposalDefaultConfig)
                .where(
                    models.ProposalDefaultConfig.team_id == team.team_id,
                    models.ProposalDefaultConfig.active,
                )
                .with_for_update()
            )
            next_version = 1 if current is None else current.version + 1
            if current is not None:
                current.active = False
            config = models.ProposalDefaultConfig(
                team_id=team.team_id,
                version=next_version,
                environment=domain.ExecutionEnvironment.LIVE.value,
                account_id=account_id,
                risk_tier=risk_tier.value,
                notional=notional,
                max_risk=max_risk,
                invalidation_bps=invalidation_bps,
                expires_in_minutes=expires_in_minutes,
                rationale=rationale,
                auto_proposal_enabled=auto_proposal_enabled,
                auto_proposal_min_timeframes=auto_proposal_min_timeframes,
                active=True,
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
                event_type="PROPOSAL_DEFAULTS_UPDATED",
                object_type="ProposalDefaultConfig",
                object_id=config.config_id,
                reason=f"version={config.version}",
                correlation_id=uuid4(),
                object_version=config.version,
                idempotency_key=idempotency_key,
                now=now,
            )
            return config.config_id

    def create_proposal(
        self,
        *,
        actor_id: UUID,
        source: domain.ProposalSource,
        risk_tier: domain.RiskTier,
        account_id: str,
        venue: str,
        instrument_id: UUID,
        direction: domain.Direction,
        quantity: Decimal,
        max_risk: Decimal,
        expires_at: datetime,
        idempotency_key: str,
        strategy_id: str | None = None,
        strategy_version: str | None = None,
        environment: domain.ExecutionEnvironment | None = None,
        source_candidate_id: str | None = None,
        source_link: str | None = None,
        source_observed_at: datetime | None = None,
        source_readiness: str | None = None,
        signal_event_id: UUID | None = None,
        details: dict[str, Any] | None = None,
        idempotency_payload: dict[str, Any] | None = None,
        deduplicate_active_manual_semantics: bool = False,
        deduplicate_active_system_scope: bool = False,
        submit_for_review: bool = False,
        perptape_runtime_binding: runtime_contracts.PreparedPerptapeRuntimeBinding | None = None,
        now: datetime,
    ) -> UUID:
        payload = {
            "source": source.value,
            "risk_tier": risk_tier.value,
            "account_id": account_id,
            "venue": venue,
            "instrument_id": str(instrument_id),
            "direction": direction.value,
            "quantity": str(quantity),
            "max_risk": str(max_risk),
            "expires_at": expires_at.isoformat(),
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "environment": None if environment is None else environment.value,
            "source_candidate_id": source_candidate_id,
            "source_link": source_link,
            "source_observed_at": (
                None if source_observed_at is None else source_observed_at.isoformat()
            ),
            "source_readiness": source_readiness,
            "signal_event_id": None if signal_event_id is None else str(signal_event_id),
            "details": details or {},
            "deduplicate_active_manual_semantics": deduplicate_active_manual_semantics,
            "deduplicate_active_system_scope": deduplicate_active_system_scope,
            "submit_for_review": submit_for_review,
        }
        operation = "proposal.create"
        with self.database.session_factory.begin() as session:
            if perptape_runtime_binding is not None:
                if source is not domain.ProposalSource.SYSTEM or signal_event_id is not None:
                    rejections.reject(
                        "SIGNAL_RUNTIME_BINDING_INVALID",
                        "Perptape runtime bindings only create system signal proposals",
                    )
                lock_perptape_runtime_binding(session, perptape_runtime_binding)
            team = self.transactions.require_role(session, actor_id, operation, account_id, venue)
            if team.execution_mode not in {
                domain.TeamExecutionMode.TESTNET.value,
                domain.TeamExecutionMode.LIVE.value,
            }:
                rejections.reject(
                    "TEAM_SETUP_INCOMPLETE",
                    "team must select TESTNET or LIVE before creating proposals",
                )
            actual_environment = domain.ExecutionEnvironment(team.execution_mode)
            if environment is not None and environment is not actual_environment:
                rejections.reject(
                    "PROPOSAL_ENVIRONMENT_MISMATCH",
                    "proposal environment must match the server-owned team current mode",
                )
            environment = actual_environment
            payload["environment"] = environment.value
            if submit_for_review:
                self.transactions.require_role(
                    session,
                    actor_id,
                    "proposal.submit",
                    account_id,
                    venue,
                    team_id=team.team_id,
                )
            scoped_payload = {
                **(payload if idempotency_payload is None else idempotency_payload),
                "workspace_id": str(team.workspace_id),
                "team_id": str(team.team_id),
            }
            digest, response = self.transactions.idempotency(
                session,
                caller_id=f"{actor_id}:{team.team_id}",
                operation=operation,
                idempotency_key=idempotency_key,
                payload=scoped_payload,
            )
            if response is not None:
                return UUID(str(response["proposal_id"]))
            principal = session.get(models.User, actor_id)
            if principal is None:
                rejections.reject("USER_NOT_AUTHORIZED", "proposal principal does not exist")
            signal_event: models.SignalEvent | None = None
            if source is domain.ProposalSource.MANUAL:
                if principal.principal_type != domain.PrincipalType.HUMAN.value:
                    rejections.reject("PROPOSAL_SOURCE_INVALID", "MANUAL proposals require a human")
                if strategy_id is not None or strategy_version is not None:
                    rejections.reject(
                        "PROPOSAL_STRATEGY_INVALID", "MANUAL proposals do not bind a strategy"
                    )
                if source_candidate_id is not None:
                    rejections.reject(
                        "PROPOSAL_SOURCE_INVALID",
                        "MANUAL proposals cannot bind a source candidate",
                    )
                if signal_event_id is not None:
                    signal_event = session.scalar(
                        select(models.SignalEvent)
                        .where(
                            models.SignalEvent.signal_event_id == signal_event_id,
                            models.SignalEvent.team_id == team.team_id,
                        )
                        .with_for_update()
                    )
                    if signal_event is None:
                        rejections.reject(
                            "SIGNAL_EVENT_NOT_FOUND",
                            "signal event is outside the active team or does not exist",
                        )
                    if signal_event.status != domain.SignalEventStatus.RECEIVED.value:
                        rejections.reject(
                            "SIGNAL_ALREADY_CONSUMED",
                            "signal event already created a proposal",
                        )
                    signal_source = session.scalar(
                        select(models.TeamSignalSource).where(
                            models.TeamSignalSource.signal_source_id
                            == signal_event.signal_source_id,
                            models.TeamSignalSource.team_id == team.team_id,
                            models.TeamSignalSource.deleted_at.is_(None),
                        )
                    )
                    if (
                        signal_source is None
                        or not signal_source.enabled
                        or signal_source.mode != domain.SignalSourceMode.WEBHOOK.value
                    ):
                        rejections.reject(
                            "SIGNAL_SOURCE_DISABLED",
                            "the Webhook signal source is no longer enabled",
                        )
                    if signal_event.occurred_at < now.astimezone(UTC) - timedelta(
                        seconds=signal_source.webhook_max_age_seconds
                    ):
                        rejections.reject(
                            "SIGNAL_STALE",
                            "the Webhook signal is outside its source freshness window",
                        )
            elif (
                (
                    principal.principal_type != domain.PrincipalType.SERVICE.value
                    and not (
                        (api_context := request_context.current_api_client_context()) is not None
                        and api_context.owner_user_id == actor_id
                        and principal.principal_type == domain.PrincipalType.HUMAN.value
                    )
                )
                or not strategy_id
                or not strategy_version
                or not source_candidate_id
                or source_observed_at is None
                or signal_event_id is not None
            ):
                rejections.reject(
                    "PROPOSAL_SOURCE_INVALID",
                    "SYSTEM proposals require a service or user-owned API Key, strategy "
                    "version and candidate",
                )
            instrument = session.get(models.Instrument, instrument_id)
            if instrument is None or not instrument.active or instrument.venue != venue:
                rejections.reject(
                    "INSTRUMENT_UNAVAILABLE", "instrument is inactive or outside venue scope"
                )
            if signal_event is not None and (
                signal_event.venue != venue
                or signal_event.symbol != instrument.symbol
                or signal_event.direction != direction.value
            ):
                rejections.reject(
                    "SIGNAL_PROPOSAL_MISMATCH",
                    "proposal instrument, venue and direction must match the frozen signal",
                )
            ensure_exchange_account_reference(
                session,
                team=team,
                actor_id=actor_id,
                account_id=account_id,
                venue=venue,
                environment=environment.value,
                now=now,
            )
            if expires_at <= now:
                rejections.reject(
                    "PROPOSAL_EXPIRY_INVALID", "proposal expiry must be in the future"
                )
            if deduplicate_active_manual_semantics:
                if source is not domain.ProposalSource.MANUAL:
                    rejections.reject(
                        "PROPOSAL_DEDUPLICATION_SCOPE_INVALID",
                        "manual semantic deduplication is restricted to MANUAL proposals",
                    )
                active_scope = ":".join(
                    [
                        str(team.team_id),
                        environment.value,
                        account_id,
                        venue,
                        str(instrument_id),
                        direction.value,
                    ]
                )
                session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {
                        "key": scope_rules.advisory_lock_key(
                            "proposal.active-manual-semantics",
                            "shared",
                            active_scope,
                        )
                    },
                )
                requested_key = manual_execution_key(
                    environment=environment.value,
                    account_id=account_id,
                    venue=venue,
                    instrument_id=instrument_id,
                    direction=direction.value,
                    risk_tier=risk_tier.value,
                    quantity=quantity,
                    max_risk=max_risk,
                    expires_in_minutes=round((expires_at - now).total_seconds() / 60),
                    details=details or {},
                )
                active_manual = session.scalars(
                    select(models.Proposal)
                    .where(
                        models.Proposal.source == domain.ProposalSource.MANUAL.value,
                        models.Proposal.team_id == team.team_id,
                        models.Proposal.environment == environment.value,
                        models.Proposal.account_id == account_id,
                        models.Proposal.venue == venue,
                        models.Proposal.instrument_id == instrument_id,
                        models.Proposal.direction == direction.value,
                        models.Proposal.status.in_(
                            [
                                domain.ProposalStatus.DRAFT.value,
                                domain.ProposalStatus.PENDING_REVIEW.value,
                            ]
                        ),
                        models.Proposal.expires_at > now,
                    )
                    .order_by(models.Proposal.created_at, models.Proposal.proposal_id)
                    .with_for_update()
                ).all()
                existing = next(
                    (
                        proposal
                        for proposal in active_manual
                        if proposal_manual_execution_key(proposal) == requested_key
                    ),
                    None,
                )
                if existing is not None:
                    result = {"proposal_id": str(existing.proposal_id)}
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
                        event_type="PROPOSAL_DUPLICATE_REUSED",
                        object_type="Proposal",
                        object_id=existing.proposal_id,
                        reason="matching active manual execution semantics",
                        correlation_id=existing.correlation_id,
                        object_version=existing.version,
                        idempotency_key=idempotency_key,
                        now=now,
                    )
                    return existing.proposal_id
            if deduplicate_active_system_scope:
                if source is not domain.ProposalSource.SYSTEM or not strategy_id:
                    rejections.reject(
                        "PROPOSAL_DEDUPLICATION_SCOPE_INVALID",
                        "active-scope deduplication is restricted to identified SYSTEM strategies",
                    )
                strategy_family, strategy_ids = system_proposal_strategy_family(strategy_id)
                active_scope = ":".join(
                    [
                        str(team.team_id),
                        environment.value,
                        account_id,
                        venue,
                        str(instrument_id),
                        direction.value,
                        strategy_family,
                    ]
                )
                session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {
                        "key": scope_rules.advisory_lock_key(
                            "proposal.active-system-scope",
                            strategy_family,
                            active_scope,
                        )
                    },
                )
                existing = session.scalar(
                    select(models.Proposal)
                    .where(
                        models.Proposal.source == domain.ProposalSource.SYSTEM.value,
                        models.Proposal.team_id == team.team_id,
                        models.Proposal.proposer_id == actor_id,
                        models.Proposal.strategy_id.in_(strategy_ids),
                        models.Proposal.environment == environment.value,
                        models.Proposal.account_id == account_id,
                        models.Proposal.venue == venue,
                        models.Proposal.instrument_id == instrument_id,
                        models.Proposal.direction == direction.value,
                        models.Proposal.status.in_(
                            [
                                domain.ProposalStatus.DRAFT.value,
                                domain.ProposalStatus.PENDING_REVIEW.value,
                            ]
                        ),
                        models.Proposal.expires_at > now,
                    )
                    .order_by(models.Proposal.created_at, models.Proposal.proposal_id)
                    .limit(1)
                )
                if existing is not None:
                    self.transactions.audit(
                        session,
                        actor_id=str(actor_id),
                        event_type="PROPOSAL_DUPLICATE_REUSED",
                        object_type="Proposal",
                        object_id=existing.proposal_id,
                        reason=f"matching active SYSTEM proposal family {strategy_family}",
                        correlation_id=existing.correlation_id,
                        object_version=existing.version,
                        idempotency_key=idempotency_key,
                        now=now,
                    )
                    return existing.proposal_id
            correlation_id = uuid4()
            proposal = models.Proposal(
                team_id=team.team_id,
                source=source.value,
                environment=environment.value,
                proposer_id=actor_id,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                source_candidate_id=source_candidate_id,
                source_link=source_link,
                source_observed_at=source_observed_at,
                source_readiness=source_readiness,
                signal_event_id=signal_event_id,
                status=domain.ProposalStatus.DRAFT.value,
                version=1,
                risk_tier=risk_tier.value,
                account_id=account_id,
                venue=venue,
                instrument_id=instrument_id,
                direction=direction.value,
                quantity=quantity,
                max_risk=max_risk,
                frozen_payload={
                    **payload,
                    "scope": {
                        "workspace_id": str(team.workspace_id),
                        "team_id": str(team.team_id),
                        "account_id": account_id,
                        "venue": venue,
                    },
                },
                semantic_hash=digest,
                frozen_at=None,
                expires_at=expires_at,
                correlation_id=correlation_id,
                created_at=now,
                updated_at=now,
            )
            session.add(proposal)
            session.flush()
            if signal_event is not None:
                signal_event.status = domain.SignalEventStatus.PROPOSAL_CREATED.value
                self.transactions.audit(
                    session,
                    actor_id=str(actor_id),
                    event_type="SIGNAL_PROPOSAL_CREATED",
                    object_type="SignalEvent",
                    object_id=signal_event.signal_event_id,
                    reason=f"proposal={proposal.proposal_id}",
                    correlation_id=proposal.correlation_id,
                    object_version=1,
                    idempotency_key=idempotency_key,
                    workspace_id=team.workspace_id,
                    team_id=team.team_id,
                    account_id=account_id,
                    now=now,
                )
            result = {"proposal_id": str(proposal.proposal_id)}
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
                event_type="PROPOSAL_CREATED",
                object_type="Proposal",
                object_id=proposal.proposal_id,
                reason=source.value,
                correlation_id=correlation_id,
                object_version=1,
                idempotency_key=idempotency_key,
                now=now,
            )
            if submit_for_review:
                proposal.status = domain.ProposalStatus.PENDING_REVIEW.value
                proposal.frozen_at = now
                proposal.version = 2
                self.transactions.audit(
                    session,
                    actor_id=str(actor_id),
                    event_type="PROPOSAL_SUBMITTED",
                    object_type="Proposal",
                    object_id=proposal.proposal_id,
                    reason="frozen for review in the creating transaction",
                    correlation_id=proposal.correlation_id,
                    object_version=proposal.version,
                    idempotency_key=idempotency_key,
                    workspace_id=team.workspace_id,
                    team_id=team.team_id,
                    account_id=account_id,
                    now=now,
                )
                enqueue_proposal_review_notification(
                    self.transactions,
                    session,
                    actor_id=actor_id,
                    team=team,
                    proposal=proposal,
                    idempotency_key=idempotency_key,
                    now=now,
                )
            return proposal.proposal_id

    def expire_duplicate_active_manual_proposals(
        self,
        *,
        actor_id: UUID,
        now: datetime,
    ) -> int:
        """Keep one active proposal per exact frozen manual trade and audit the surplus."""

        with self.database.session_factory.begin() as session:
            team = self.transactions.require_role(session, actor_id, "user.manage")
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {
                    "key": scope_rules.advisory_lock_key(
                        "proposal.active-manual-semantics",
                        str(team.team_id),
                        "all",
                    )
                },
            )
            proposals = session.scalars(
                select(models.Proposal)
                .where(
                    models.Proposal.source == domain.ProposalSource.MANUAL.value,
                    models.Proposal.team_id == team.team_id,
                    models.Proposal.status.in_(
                        [
                            domain.ProposalStatus.DRAFT.value,
                            domain.ProposalStatus.PENDING_REVIEW.value,
                        ]
                    ),
                    models.Proposal.expires_at > now,
                )
                .order_by(models.Proposal.created_at, models.Proposal.proposal_id)
                .with_for_update()
            ).all()
            approved_ids = set(
                session.scalars(
                    select(models.Approval.proposal_id).where(
                        models.Approval.proposal_id.in_([item.proposal_id for item in proposals])
                    )
                ).all()
            )
            grouped: dict[tuple[Any, ...], list[models.Proposal]] = {}
            for proposal in proposals:
                grouped.setdefault(proposal_manual_execution_key(proposal), []).append(proposal)

            expired = 0
            for matching in grouped.values():
                if len(matching) < 2:
                    continue
                matching.sort(
                    key=lambda item: (
                        item.proposal_id not in approved_ids,
                        item.created_at,
                        str(item.proposal_id),
                    )
                )
                canonical = matching[0]
                for duplicate in matching[1:]:
                    self.transactions.audit(
                        session,
                        actor_id=str(duplicate.proposer_id),
                        event_type="PROPOSAL_DUPLICATE_REUSED",
                        object_type="Proposal",
                        object_id=canonical.proposal_id,
                        reason="existing duplicate manual proposal consolidated at startup",
                        correlation_id=canonical.correlation_id,
                        object_version=canonical.version,
                        now=now,
                    )
                    duplicate.status = domain.ProposalStatus.EXPIRED.value
                    duplicate.updated_at = now
                    duplicate.version += 1
                    self.transactions.audit(
                        session,
                        actor_id=str(actor_id),
                        event_type="PROPOSAL_DUPLICATE_EXPIRED",
                        object_type="Proposal",
                        object_id=duplicate.proposal_id,
                        reason=(
                            "duplicate active manual execution semantics; "
                            f"canonical={canonical.proposal_id}"
                        ),
                        correlation_id=duplicate.correlation_id,
                        object_version=duplicate.version,
                        now=now,
                    )
                    expired += 1
            return expired

    def expire_duplicate_active_system_proposals(
        self,
        *,
        actor_id: UUID,
        strategy_id: str,
        now: datetime,
    ) -> int:
        """Keep one frozen active proposal per strategy-family scope and audit the surplus."""

        if not strategy_id:
            rejections.reject(
                "PROPOSAL_STRATEGY_INVALID", "duplicate cleanup requires a strategy ID"
            )
        with self.database.session_factory.begin() as session:
            actor = session.get(models.User, actor_id)
            if (
                actor is None
                or not actor.active
                or actor.principal_type != domain.PrincipalType.SERVICE.value
            ):
                rejections.reject(
                    "PROPOSAL_AUTOMATION_SERVICE_REQUIRED",
                    "duplicate cleanup is restricted to an active service principal",
                )
            _actor, _workspace, team = self.transactions.active_scope(session, actor_id)
            assert team is not None
            strategy_family, strategy_ids = system_proposal_strategy_family(strategy_id)
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {
                    "key": scope_rules.advisory_lock_key(
                        str(actor_id),
                        "proposal.active-system-deduplication",
                        f"{team.team_id}:{strategy_family}",
                    )
                },
            )
            proposals = session.scalars(
                select(models.Proposal)
                .where(
                    models.Proposal.source == domain.ProposalSource.SYSTEM.value,
                    models.Proposal.team_id == team.team_id,
                    models.Proposal.proposer_id == actor_id,
                    models.Proposal.strategy_id.in_(strategy_ids),
                    models.Proposal.status.in_(
                        [
                            domain.ProposalStatus.DRAFT.value,
                            domain.ProposalStatus.PENDING_REVIEW.value,
                        ]
                    ),
                    models.Proposal.expires_at > now,
                )
                .order_by(models.Proposal.created_at, models.Proposal.proposal_id)
                .with_for_update()
            ).all()
            approved_proposal_ids = set(
                session.scalars(
                    select(models.Approval.proposal_id).where(
                        models.Approval.proposal_id.in_([item.proposal_id for item in proposals])
                    )
                ).all()
            )
            grouped: dict[tuple[str, str, str, UUID, str], list[models.Proposal]] = {}
            for proposal in proposals:
                scope = (
                    proposal.environment,
                    proposal.account_id,
                    proposal.venue,
                    proposal.instrument_id,
                    proposal.direction,
                )
                grouped.setdefault(scope, []).append(proposal)
            expired = 0
            for scoped in grouped.values():
                if len(scoped) < 2:
                    continue
                scoped.sort(
                    key=lambda item: (
                        item.proposal_id not in approved_proposal_ids,
                        item.created_at,
                        str(item.proposal_id),
                    )
                )
                canonical = scoped[0]
                for duplicate in scoped[1:]:
                    duplicate.status = domain.ProposalStatus.EXPIRED.value
                    duplicate.updated_at = now
                    duplicate.version += 1
                    self.transactions.audit(
                        session,
                        actor_id=str(actor_id),
                        event_type="PROPOSAL_DUPLICATE_EXPIRED",
                        object_type="Proposal",
                        object_id=duplicate.proposal_id,
                        reason=(
                            f"duplicate active resonance scope; canonical={canonical.proposal_id}"
                        ),
                        correlation_id=duplicate.correlation_id,
                        object_version=duplicate.version,
                        now=now,
                    )
                    expired += 1
            return expired

    def submit_proposal(
        self,
        proposal_id: UUID,
        actor_id: UUID,
        *,
        perptape_runtime_binding: runtime_contracts.PreparedPerptapeRuntimeBinding | None = None,
        now: datetime,
    ) -> None:
        expired = False
        with self.database.session_factory.begin() as session:
            if perptape_runtime_binding is not None:
                lock_perptape_runtime_binding(session, perptape_runtime_binding)
            proposal = session.get(models.Proposal, proposal_id, with_for_update=True)
            if proposal is None:
                rejections.reject("PROPOSAL_NOT_FOUND", "proposal does not exist")
            self.transactions.require_role(
                session,
                actor_id,
                "proposal.submit",
                proposal.account_id,
                proposal.venue,
                team_id=proposal.team_id,
            )
            if proposal.proposer_id != actor_id:
                rejections.reject(
                    "PROPOSAL_OWNER_REQUIRED", "only the proposer may submit the draft"
                )
            if proposal.expires_at <= now:
                proposal.status = domain.ProposalStatus.EXPIRED.value
                proposal.updated_at = now
                proposal.version += 1
                self.transactions.audit(
                    session,
                    actor_id=str(actor_id),
                    event_type="PROPOSAL_EXPIRED",
                    object_type="Proposal",
                    object_id=proposal.proposal_id,
                    reason="expired before submission",
                    correlation_id=proposal.correlation_id,
                    object_version=proposal.version,
                    now=now,
                )
                expired = True
            elif proposal.status != domain.ProposalStatus.DRAFT.value:
                rejections.reject("PROPOSAL_NOT_DRAFT", "only a draft can be submitted")
            else:
                proposal.status = domain.ProposalStatus.PENDING_REVIEW.value
                proposal.frozen_at = now
                proposal.updated_at = now
                proposal.version += 1
                self.transactions.audit(
                    session,
                    actor_id=str(actor_id),
                    event_type="PROPOSAL_SUBMITTED",
                    object_type="Proposal",
                    object_id=proposal.proposal_id,
                    reason="frozen for review",
                    correlation_id=proposal.correlation_id,
                    object_version=proposal.version,
                    now=now,
                )
                team = session.get(models.Team, proposal.team_id)
                assert team is not None
                enqueue_proposal_review_notification(
                    self.transactions,
                    session,
                    actor_id=actor_id,
                    team=team,
                    proposal=proposal,
                    idempotency_key=f"proposal-submit-v{proposal.version}",
                    now=now,
                )
        if expired:
            rejections.reject("PROPOSAL_EXPIRED", "proposal expired before submission")

    def review_proposal(
        self,
        proposal_id: UUID,
        reviewer_id: UUID,
        decision: domain.ReviewDecision,
        reason: str,
        expected_version: int | None = None,
        *,
        idempotency_key: str | None = None,
        now: datetime,
    ) -> domain.ProposalStatus:
        expired = False
        result: domain.ProposalStatus | None = None
        with self.database.session_factory.begin() as session:
            proposal = session.get(models.Proposal, proposal_id, with_for_update=True)
            if proposal is None:
                rejections.reject("PROPOSAL_NOT_FOUND", "proposal does not exist")
            if is_manual_proposal_originator(session, proposal, reviewer_id):
                rejections.reject(
                    "SELF_REVIEW_FORBIDDEN", "a proposer cannot review the same proposal"
                )
            self.transactions.require_role(
                session,
                reviewer_id,
                "proposal.review",
                proposal.account_id,
                proposal.venue,
                team_id=proposal.team_id,
            )
            digest: str | None = None
            operation = f"proposal.review:{proposal_id}"
            if idempotency_key is not None:
                digest, replay = self.transactions.idempotency(
                    session,
                    caller_id=f"{reviewer_id}:{proposal.team_id}",
                    operation=operation,
                    idempotency_key=idempotency_key,
                    payload={
                        "proposal_id": str(proposal_id),
                        "decision": decision.value,
                        "reason": reason,
                        "expected_version": expected_version,
                    },
                )
                if replay is not None:
                    return domain.ProposalStatus(str(replay["status"]))
            if expected_version is not None and proposal.version != expected_version:
                rejections.reject(
                    "VERSION_CONFLICT", "proposal version changed; refresh before reviewing"
                )
            if proposal.expires_at <= now:
                proposal.status = domain.ProposalStatus.EXPIRED.value
                proposal.updated_at = now
                proposal.version += 1
                self.transactions.audit(
                    session,
                    actor_id=str(reviewer_id),
                    event_type="PROPOSAL_EXPIRED",
                    object_type="Proposal",
                    object_id=proposal.proposal_id,
                    reason="expired before review",
                    correlation_id=proposal.correlation_id,
                    object_version=proposal.version,
                    now=now,
                )
                expired = True
            else:
                if proposal.status != domain.ProposalStatus.PENDING_REVIEW.value:
                    rejections.reject("PROPOSAL_NOT_REVIEWABLE", "proposal is not pending review")
                duplicate = session.scalar(
                    select(models.Approval).where(
                        models.Approval.proposal_id == proposal_id,
                        models.Approval.reviewer_id == reviewer_id,
                    )
                )
                if duplicate is not None:
                    rejections.reject("REVIEW_ALREADY_RECORDED", "reviewer already voted")
                session.add(
                    models.Approval(
                        proposal_id=proposal_id,
                        reviewer_id=reviewer_id,
                        decision=decision.value,
                        reason=reason,
                        created_at=now,
                    )
                )
                session.flush()
                if decision is domain.ReviewDecision.REJECT:
                    proposal.status = domain.ProposalStatus.REJECTED.value
                else:
                    approvals = session.execute(
                        select(func.count())
                        .select_from(models.Approval)
                        .where(
                            models.Approval.proposal_id == proposal_id,
                            models.Approval.decision == domain.ReviewDecision.APPROVE.value,
                        )
                    ).scalar_one()
                    required = 2 if proposal.risk_tier == domain.RiskTier.HIGH.value else 1
                    if approvals >= required:
                        proposal.status = domain.ProposalStatus.APPROVED.value
                proposal.updated_at = now
                proposal.version += 1
                self.transactions.audit(
                    session,
                    actor_id=str(reviewer_id),
                    event_type="PROPOSAL_REVIEWED",
                    object_type="Proposal",
                    object_id=proposal_id,
                    reason=f"{decision.value}: {reason}",
                    correlation_id=proposal.correlation_id,
                    object_version=proposal.version,
                    now=now,
                )
                result = domain.ProposalStatus(proposal.status)
                if idempotency_key is not None:
                    assert digest is not None
                    self.transactions.save_receipt(
                        session,
                        caller_id=f"{reviewer_id}:{proposal.team_id}",
                        operation=operation,
                        idempotency_key=idempotency_key,
                        semantic_hash=digest,
                        response={"status": result.value},
                        now=now,
                    )
        if expired:
            rejections.reject("PROPOSAL_EXPIRED", "proposal expired before review")
        if result is None:
            raise RuntimeError("proposal review completed without a result")
        return result
