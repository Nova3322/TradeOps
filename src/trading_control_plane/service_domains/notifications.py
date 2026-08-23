from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from trading_control_plane import execution_scope as scope_rules
from trading_control_plane import idempotency, models, notification, rejections
from trading_control_plane.query_domains.execution import proposal_summary
from trading_control_plane.service_component import ServiceComponent
from trading_control_plane.service_transactions import TransactionService


def enqueue_notification_event(
    transactions: TransactionService,
    session: Session,
    *,
    actor_id: str,
    team: models.Team,
    event_type: str,
    payload: dict[str, Any],
    object_type: str,
    object_id: UUID | str,
    object_version: int,
    idempotency_key: str,
    correlation_id: UUID,
    environment: str | None,
    account_id: str | None,
    venue: str | None,
    now: datetime,
    target_route_id: UUID | None = None,
) -> dict[str, Any]:
    template = notification.notification_template(event_type)
    normalized_payload = notification.validate_notification_payload(payload)
    event_identity = {
        "event_type": template.event_type,
        "template_key": template.key,
        "template_version": template.version,
        "payload": normalized_payload,
        "object_type": object_type,
        "object_id": str(object_id),
        "object_version": object_version,
        "environment": environment,
        "account_id": account_id,
        "venue": venue,
        "target_route_id": None if target_route_id is None else str(target_route_id),
    }
    caller = f"notification:{team.team_id}"
    operation = f"notification-event:{template.event_type}:{object_type}:{object_id}"
    digest, replay = transactions.idempotency(
        session,
        caller_id=caller,
        operation=operation,
        idempotency_key=idempotency_key,
        payload=event_identity,
    )
    if replay is not None:
        return replay
    event_id = uuid5(
        NAMESPACE_URL,
        f"tradingops:{team.team_id}:{operation}:{idempotency_key}",
    )
    route_query = select(models.NotificationRoute).where(
        models.NotificationRoute.team_id == team.team_id,
        models.NotificationRoute.enabled,
        models.NotificationRoute.deleted_at.is_(None),
    )
    if environment is not None:
        route_query = route_query.where(models.NotificationRoute.environment == environment)
    if target_route_id is not None:
        route_query = route_query.where(
            models.NotificationRoute.notification_route_id == target_route_id
        )
    routes = session.scalars(route_query.order_by(models.NotificationRoute.name)).all()
    if target_route_id is not None and not routes:
        rejections.reject(
            "NOTIFICATION_ROUTE_UNAVAILABLE",
            "notification test route is missing or disabled",
        )
    delivery_ids: list[str] = []
    queued_delivery_count = 0
    deduplicated_delivery_count = 0
    for route in routes:
        if target_route_id is None and template.event_type not in route.event_types:
            continue
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {
                "key": scope_rules.advisory_lock_key(
                    f"notification-route:{route.notification_route_id}",
                    f"notification-delivery:{template.event_type}",
                    f"v{route.version}:{object_type}:{object_id}:v{object_version}:"
                    f"reviewer={route.recipient_user_id or 'none'}",
                )
            },
        )
        existing_delivery_id = session.scalar(
            select(models.NotificationDelivery.notification_delivery_id)
            .where(
                models.NotificationDelivery.notification_route_id
                == route.notification_route_id,
                models.NotificationDelivery.route_version == route.version,
                models.NotificationDelivery.event_type == template.event_type,
                models.NotificationDelivery.object_type == object_type,
                models.NotificationDelivery.object_id == str(object_id),
                models.NotificationDelivery.object_version == object_version,
                models.NotificationDelivery.recipient_user_id == route.recipient_user_id,
            )
            .limit(1)
        )
        if existing_delivery_id is not None:
            delivery_ids.append(str(existing_delivery_id))
            deduplicated_delivery_count += 1
            continue
        delivery = models.NotificationDelivery(
            notification_event_id=event_id,
            team_id=team.team_id,
            notification_route_id=route.notification_route_id,
            route_version=route.version,
            channel=route.channel,
            event_type=template.event_type,
            template_key=template.key,
            template_version=template.version,
            payload=normalized_payload,
            semantic_hash=digest,
            object_type=object_type,
            object_id=str(object_id),
            object_version=object_version,
            environment=environment,
            account_id=account_id,
            venue=venue,
            recipient_user_id=route.recipient_user_id,
            status="PENDING",
            attempt_count=0,
            max_attempts=5,
            next_attempt_at=now,
            last_attempt_at=None,
            sent_at=None,
            external_delivery_id=None,
            last_error_code=None,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
        )
        session.add(delivery)
        session.flush()
        delivery_ids.append(str(delivery.notification_delivery_id))
        queued_delivery_count += 1
    response = {
        "notification_event_id": str(event_id),
        "notification_delivery_ids": delivery_ids,
        "route_count": len(delivery_ids),
    }
    transactions.save_receipt(
        session,
        caller_id=caller,
        operation=operation,
        idempotency_key=idempotency_key,
        semantic_hash=digest,
        response=response,
        now=now,
    )
    transactions.audit(
        session,
        actor_id=actor_id,
        event_type=(
            "NOTIFICATION_EVENT_QUEUED"
            if queued_delivery_count
            else "NOTIFICATION_EVENT_DEDUPLICATED"
            if deduplicated_delivery_count
            else "NOTIFICATION_EVENT_UNROUTED"
        ),
        object_type="NotificationEvent",
        object_id=event_id,
        reason=(
            f"event_type={template.event_type};template={template.key}:v{template.version};"
            f"source={object_type}:{object_id}:v{object_version};routes={len(delivery_ids)};"
            f"queued={queued_delivery_count};deduplicated={deduplicated_delivery_count}"
        ),
        correlation_id=correlation_id,
        object_version=template.version,
        idempotency_key=idempotency_key,
        workspace_id=team.workspace_id,
        team_id=team.team_id,
        account_id=account_id,
        now=now,
    )
    return response


def enqueue_proposal_review_notification(
    transactions: TransactionService,
    session: Session,
    *,
    actor_id: UUID,
    team: models.Team,
    proposal: models.Proposal,
    idempotency_key: str,
    now: datetime,
) -> None:
    instrument = session.get(models.Instrument, proposal.instrument_id)
    summary = proposal_summary(proposal, instrument)
    enqueue_notification_event(
        transactions,
        session,
        actor_id=str(actor_id),
        team=team,
        event_type="PROPOSAL_REVIEW_REQUIRED",
        payload={
            "summary": "冻结提案已提交, 等待团队成员独立审核。",
            "proposal_id": str(proposal.proposal_id),
            "proposal_version": proposal.version,
            "status": proposal.status,
            "environment": proposal.environment,
            "account_id": proposal.account_id,
            "venue": proposal.venue,
            "symbol": summary["symbol"],
            "direction": proposal.direction,
            "risk_tier": proposal.risk_tier,
            "quantity": str(proposal.quantity),
            "estimated_notional": summary["estimated_notional"],
            "quote_currency": summary["quote_currency"],
            "collateral_currency": summary["collateral_currency"],
            "leverage": None if proposal.leverage is None else str(proposal.leverage),
            "max_risk": str(proposal.max_risk),
            "expires_at": proposal.expires_at.isoformat(),
        },
        object_type="Proposal",
        object_id=proposal.proposal_id,
        object_version=proposal.version,
        idempotency_key=idempotency_key,
        correlation_id=proposal.correlation_id,
        environment=proposal.environment,
        account_id=proposal.account_id,
        venue=proposal.venue,
        now=now,
    )


def enqueue_campaign_status_notification(
    transactions: TransactionService,
    session: Session,
    *,
    actor_id: str,
    campaign: models.Campaign,
    summary: str,
    idempotency_key: str,
    correlation_id: UUID,
    now: datetime,
) -> None:
    team = session.get(models.Team, campaign.team_id)
    assert team is not None
    enqueue_notification_event(
        transactions,
        session,
        actor_id=actor_id,
        team=team,
        event_type="CAMPAIGN_STATUS_CHANGED",
        payload={
            "summary": summary,
            "status": campaign.status,
            "environment": campaign.environment,
            "account_id": campaign.account_id,
            "venue": campaign.venue,
            "direction": campaign.direction,
            "target_quantity": str(campaign.current_target_quantity),
        },
        object_type="Campaign",
        object_id=campaign.campaign_id,
        object_version=campaign.target_version,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
        environment=campaign.environment,
        account_id=campaign.account_id,
        venue=campaign.venue,
        now=now,
    )


class NotificationService(ServiceComponent):
    def configure_notification_route(
        self,
        *,
        actor_id: UUID,
        notification_route_id: UUID | None,
        environment: str = "LIVE",
        name: str,
        channel: str,
        event_types: list[str],
        enabled: bool,
        configuration: dict[str, str] | None,
        expected_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        normalized_name = " ".join(name.strip().split())
        normalized_environment = environment.strip().upper()
        if normalized_environment not in {"TESTNET", "LIVE"}:
            rejections.reject("NOTIFICATION_ROUTE_INVALID", "environment must be TESTNET or LIVE")
        normalized_channel = channel.strip().upper()
        normalized_events = notification.normalize_notification_event_types(event_types)
        if not normalized_name or len(normalized_name) > 120:
            rejections.reject(
                "NOTIFICATION_ROUTE_NAME_INVALID",
                "notification route name must contain 1-120 characters",
            )
        with self.database.session_factory.begin() as session:
            team = self.transactions.require_role(session, actor_id, "notification.manage")
            route = (
                None
                if notification_route_id is None
                else session.scalar(
                    select(models.NotificationRoute)
                    .where(
                        models.NotificationRoute.notification_route_id == notification_route_id,
                        models.NotificationRoute.team_id == team.team_id,
                        models.NotificationRoute.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
            )
            if notification_route_id is not None and route is None:
                rejections.reject(
                    "NOTIFICATION_ROUTE_NOT_FOUND",
                    "notification route does not exist in the active team",
                )
            if route is not None and route.channel != normalized_channel:
                rejections.reject(
                    "NOTIFICATION_ROUTE_CHANNEL_IMMUTABLE",
                    "create a new route to change notification channel",
                )
            if route is not None and route.environment != normalized_environment:
                rejections.reject(
                    "NOTIFICATION_ROUTE_ENVIRONMENT_IMMUTABLE",
                    "create a new route to change notification environment",
                )
            route_id = (
                uuid5(
                    NAMESPACE_URL,
                    f"tradingops:notification-route:{team.team_id}:{actor_id}:{idempotency_key}",
                )
                if route is None
                else route.notification_route_id
            )
            if configuration is None:
                if route is None:
                    rejections.reject(
                        "NOTIFICATION_CONFIGURATION_REQUIRED",
                        "a new notification route requires channel configuration",
                    )
                normalized_configuration = None
                configuration_metadata = route.configuration_metadata
                recipient_user_id = route.recipient_user_id
                configuration_semantics = f"unchanged:{route.credential_version}"
            else:
                normalized_configuration, configuration_metadata = (
                    notification.validate_notification_configuration(
                        normalized_channel, configuration
                    )
                )
                configuration_semantics = self.credential_cipher.secret_fingerprint(
                    idempotency.canonical_json(normalized_configuration),
                    purpose=(
                        f"notification-route:{team.team_id}:{route_id}:{normalized_channel.lower()}"
                    ),
                )
                recipient_user_id = None
                if normalized_channel == "TELEGRAM":
                    recipient_user_id = session.scalar(
                        select(models.User.user_id).where(
                            models.User.telegram_chat_id
                            == normalized_configuration["chat_id"],
                            models.User.active,
                            models.User.principal_type == "HUMAN",
                        )
                    )
            payload = {
                "notification_route_id": str(route_id),
                "environment": normalized_environment,
                "name": normalized_name,
                "channel": normalized_channel,
                "event_types": normalized_events,
                "enabled": enabled,
                "configuration_semantics": configuration_semantics,
                "expected_version": expected_version,
            }
            caller = f"{actor_id}:{team.team_id}"
            operation = (
                "notification-route.create"
                if route is None
                else f"notification-route.update:{route_id}"
            )
            digest, replay = self.transactions.idempotency(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return replay
            current_version = 0 if route is None else route.version
            if current_version != expected_version:
                rejections.reject(
                    "VERSION_CONFLICT", "notification route changed before configuration"
                )
            name_conflict = session.scalar(
                select(models.NotificationRoute.notification_route_id).where(
                    models.NotificationRoute.team_id == team.team_id,
                    models.NotificationRoute.environment == normalized_environment,
                    models.NotificationRoute.name == normalized_name,
                    models.NotificationRoute.notification_route_id != route_id,
                    models.NotificationRoute.deleted_at.is_(None),
                )
            )
            if name_conflict is not None:
                rejections.reject(
                    "NOTIFICATION_ROUTE_NAME_CONFLICT",
                    "notification route name already exists in the active team",
                )
            credential_version = 1 if route is None else route.credential_version
            ciphertext = None if route is None else route.configuration_ciphertext
            if normalized_configuration is not None:
                credential_version = 1 if route is None else route.credential_version + 1
                encrypted = self.credential_cipher.encrypt_secret(
                    idempotency.canonical_json(normalized_configuration),
                    team_id=team.team_id,
                    object_id=route_id,
                    purpose=(
                        f"notification-route:{normalized_channel.lower()}"
                        if normalized_environment == "LIVE"
                        else f"notification-route:testnet:{normalized_channel.lower()}"
                    ),
                    credential_version=credential_version,
                )
                ciphertext = encrypted.ciphertext
            assert ciphertext is not None
            if route is None:
                route = models.NotificationRoute(
                    notification_route_id=route_id,
                    team_id=team.team_id,
                    environment=normalized_environment,
                    name=normalized_name,
                    channel=normalized_channel,
                    event_types=normalized_events,
                    enabled=enabled,
                    configuration_ciphertext=ciphertext,
                    configuration_metadata=configuration_metadata,
                    recipient_user_id=recipient_user_id,
                    credential_version=credential_version,
                    version=1,
                    created_by=actor_id,
                    updated_by=actor_id,
                    created_at=now,
                    updated_at=now,
                )
                session.add(route)
            else:
                route.name = normalized_name
                route.environment = normalized_environment
                route.event_types = normalized_events
                route.enabled = enabled
                route.configuration_ciphertext = ciphertext
                route.configuration_metadata = configuration_metadata
                route.recipient_user_id = recipient_user_id
                route.credential_version = credential_version
                route.version += 1
                route.updated_by = actor_id
                route.updated_at = now
            session.flush()
            response = {
                "notification_route_id": str(route.notification_route_id),
                "version": route.version,
            }
            self.transactions.save_receipt(
                session,
                caller_id=caller,
                operation=operation,
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=response,
                now=now,
            )
            correlation_id = uuid4()
            self.transactions.audit(
                session,
                actor_id=str(actor_id),
                event_type="NOTIFICATION_ROUTE_CONFIGURED",
                object_type="NotificationRoute",
                object_id=route.notification_route_id,
                reason=(
                    f"channel={route.channel};enabled={str(route.enabled).lower()};"
                    f"events={','.join(route.event_types)};"
                    f"credential_version={route.credential_version}"
                ),
                correlation_id=correlation_id,
                object_version=route.version,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                now=now,
            )
            return response

    def delete_notification_route(
        self,
        *,
        actor_id: UUID,
        notification_route_id: UUID,
        expected_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        """Archive a route, clear its secret, and retain immutable delivery history."""

        with self.database.session_factory.begin() as session:
            team = self.transactions.require_role(session, actor_id, "notification.manage")
            route = session.scalar(
                select(models.NotificationRoute)
                .where(
                    models.NotificationRoute.notification_route_id == notification_route_id,
                    models.NotificationRoute.team_id == team.team_id,
                )
                .with_for_update()
            )
            if route is None:
                rejections.reject(
                    "NOTIFICATION_ROUTE_NOT_FOUND",
                    "notification route does not exist in the active team",
                )
            caller = f"{actor_id}:{team.team_id}"
            payload = {
                "notification_route_id": str(notification_route_id),
                "expected_version": expected_version,
            }
            digest, replay = self.transactions.idempotency(
                session,
                caller_id=caller,
                operation=f"notification-route.delete:{notification_route_id}",
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return replay
            if route.deleted_at is not None:
                rejections.reject(
                    "NOTIFICATION_ROUTE_NOT_FOUND",
                    "notification route does not exist in the active team",
                )
            if route.version != expected_version:
                rejections.reject("VERSION_CONFLICT", "notification route changed before deletion")
            sending = session.scalar(
                select(models.NotificationDelivery.notification_delivery_id)
                .where(
                    models.NotificationDelivery.team_id == team.team_id,
                    models.NotificationDelivery.notification_route_id == notification_route_id,
                    models.NotificationDelivery.status == "SENDING",
                )
                .limit(1)
            )
            if sending is not None:
                rejections.reject(
                    "NOTIFICATION_ROUTE_DELETE_BLOCKED",
                    "wait for the in-flight notification attempt to reach a known outcome",
                )
            session.execute(
                update(models.NotificationDelivery)
                .where(
                    models.NotificationDelivery.team_id == team.team_id,
                    models.NotificationDelivery.notification_route_id == notification_route_id,
                    models.NotificationDelivery.status.in_(["PENDING", "RETRY_WAIT"]),
                )
                .values(
                    status="CANCELLED",
                    last_error_code="NOTIFICATION_ROUTE_DELETED",
                    updated_at=now,
                )
            )
            route.enabled = False
            route.configuration_ciphertext = "deleted"
            route.configuration_metadata = {"deleted": True}
            route.deleted_at = now
            route.deleted_by = actor_id
            route.version += 1
            route.updated_by = actor_id
            route.updated_at = now
            response = {
                "notification_route_id": str(notification_route_id),
                "status": "DELETED",
                "version": route.version,
            }
            self.transactions.save_receipt(
                session,
                caller_id=caller,
                operation=f"notification-route.delete:{notification_route_id}",
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=response,
                now=now,
            )
            self.transactions.audit(
                session,
                actor_id=str(actor_id),
                event_type="NOTIFICATION_ROUTE_DELETED",
                object_type="NotificationRoute",
                object_id=notification_route_id,
                reason="enabled=false;credential=cleared;delivery_history_retained=true",
                correlation_id=uuid4(),
                object_version=route.version,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                now=now,
            )
            return response

    def enqueue_notification_event(
        self,
        *,
        actor_id: str,
        team_id: UUID,
        event_type: str,
        payload: dict[str, Any],
        object_type: str,
        object_id: UUID | str,
        object_version: int,
        idempotency_key: str,
        correlation_id: UUID,
        environment: str | None,
        account_id: str | None,
        venue: str | None,
        now: datetime,
    ) -> dict[str, Any]:
        with self.database.session_factory.begin() as session:
            team = session.get(models.Team, team_id)
            if team is None or not team.active:
                rejections.reject(
                    "TEAM_SCOPE_DENIED", "notification event team is missing or inactive"
                )
            return enqueue_notification_event(
                self.transactions,
                session,
                actor_id=actor_id,
                team=team,
                event_type=event_type,
                payload=payload,
                object_type=object_type,
                object_id=object_id,
                object_version=object_version,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                environment=environment,
                account_id=account_id,
                venue=venue,
                now=now,
            )

    def enqueue_test_notification(
        self,
        *,
        actor_id: UUID,
        notification_route_id: UUID,
        idempotency_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        with self.database.session_factory.begin() as session:
            team = self.transactions.require_role(session, actor_id, "notification.manage")
            route = session.scalar(
                select(models.NotificationRoute).where(
                    models.NotificationRoute.team_id == team.team_id,
                    models.NotificationRoute.notification_route_id == notification_route_id,
                    models.NotificationRoute.deleted_at.is_(None),
                )
            )
            if route is None:
                rejections.reject(
                    "NOTIFICATION_ROUTE_NOT_FOUND",
                    "notification route does not exist in the active team",
                )
            return enqueue_notification_event(
                self.transactions,
                session,
                actor_id=str(actor_id),
                team=team,
                event_type="TEST_NOTIFICATION",
                payload={
                    "summary": "这是一条团队通知路由测试, 不包含交易或资金操作。",
                    "route_name": route.name,
                    "channel": route.channel,
                },
                object_type="NotificationRoute",
                object_id=route.notification_route_id,
                object_version=route.version,
                idempotency_key=idempotency_key,
                correlation_id=uuid4(),
                environment=None,
                account_id=None,
                venue=None,
                now=now,
                target_route_id=route.notification_route_id,
            )

    def enqueue_capital_status_notification(
        self,
        *,
        actor_id: UUID,
        team_id: UUID,
        object_id: UUID,
        object_type: str,
        status: str,
        environment: str,
        account_id: str,
        venue: str,
        object_version: int,
        summary: str,
        now: datetime,
    ) -> dict[str, Any]:
        with self.database.session_factory.begin() as session:
            team = self.transactions.require_role(
                session,
                actor_id,
                "capital.view",
                account_id,
                venue,
                team_id=team_id,
            )
            notification_key = f"{object_type}:{object_id}:{status}:v{object_version}"
            return enqueue_notification_event(
                self.transactions,
                session,
                actor_id=str(actor_id),
                team=team,
                event_type="CAPITAL_STATUS_CHANGED",
                payload={
                    "summary": summary,
                    "status": status,
                    "environment": environment,
                    "account_id": account_id,
                    "venue": venue,
                },
                object_type=object_type,
                object_id=object_id,
                object_version=object_version,
                idempotency_key=notification_key,
                correlation_id=uuid5(
                    NAMESPACE_URL,
                    f"tradingops:{team.team_id}:capital-notification:{notification_key}",
                ),
                environment=environment,
                account_id=account_id,
                venue=venue,
                now=now,
            )
