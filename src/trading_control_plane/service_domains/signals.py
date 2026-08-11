from __future__ import annotations

from trading_control_plane.service_component import ServiceComponent

# The domain implementation intentionally consumes the explicit service_core export surface.
# ruff: noqa: F403, F405
from trading_control_plane.service_core import *


class SignalService(ServiceComponent):
    @staticmethod
    def _signal_source_payload(
        source: TeamSignalSource,
        *,
        workspace_id: UUID,
        updater: User | None,
        service_principal: User | None,
    ) -> dict[str, Any]:
        if source.credential_ciphertext is not None:
            credential_state = "CONFIGURED"
        elif source.credential_metadata.get("credential_source") == "RUNTIME_FALLBACK":
            credential_state = "RUNTIME_FALLBACK"
        else:
            credential_state = "UNCONFIGURED"
        return {
            "signal_source_id": str(source.signal_source_id),
            "workspace_id": str(workspace_id),
            "team_id": str(source.team_id),
            "mode": source.mode,
            "enabled": source.enabled,
            "credential": {
                "state": credential_state,
                "version": source.credential_version,
                "key_hint": source.credential_metadata.get("key_hint"),
            },
            "webhook": (
                {
                    "endpoint_path": f"/api/webhooks/signals/{source.signal_source_id}",
                    "signature_version": "v1",
                    "max_age_seconds": source.webhook_max_age_seconds,
                    "automatic_proposal_supported": False,
                }
                if source.mode == SignalSourceMode.WEBHOOK.value
                else None
            ),
            "service_principal": (
                None
                if service_principal is None
                else {
                    "user_id": str(service_principal.user_id),
                    "username": service_principal.username,
                }
            ),
            "version": source.version,
            "updated_by": str(source.updated_by),
            "updated_by_username": None if updater is None else updater.username,
            "updated_at": source.updated_at.isoformat(),
        }

    def signal_source_status(self, actor_id: UUID) -> dict[str, Any]:
        with self.database.session_factory() as session:
            team = self.transactions._require_action_assignment(session, actor_id, "signal.view")
            source = session.scalar(
                select(TeamSignalSource).where(TeamSignalSource.team_id == team.team_id)
            )
            assignments = session.scalars(
                select(RoleAssignment).where(
                    RoleAssignment.user_id == actor_id,
                    RoleAssignment.team_id == team.team_id,
                )
            ).all()
            return {
                "configured": source is not None,
                "can_manage": any(
                    item.account_scope is None
                    and item.venue_scope is None
                    and (
                        "signal.manage" in ROLE_ACTIONS[Role(item.role)]
                        or "*" in ROLE_ACTIONS[Role(item.role)]
                    )
                    for item in assignments
                ),
                "source": (
                    None
                    if source is None
                    else self._signal_source_payload(
                        source,
                        workspace_id=team.workspace_id,
                        updater=session.get(User, source.updated_by),
                        service_principal=(
                            None
                            if source.service_principal_id is None
                            else session.get(User, source.service_principal_id)
                        ),
                    )
                ),
            }

    def _ensure_signal_service_principal(
        self,
        session: Session,
        *,
        team: Team,
        actor_id: UUID,
        now: datetime,
    ) -> User:
        username = f"signal-{team.team_id.hex}"
        principal = session.scalar(select(User).where(User.username == username))
        if principal is None:
            principal = User(
                username=username,
                principal_type=PrincipalType.SERVICE.value,
                service_kind=ServicePrincipalKind.INTERNAL.value,
                active_workspace_id=team.workspace_id,
                active_team_id=team.team_id,
                active=True,
                created_at=now,
            )
            session.add(principal)
            session.flush()
            session.add_all(
                [
                    WorkspaceMembership(
                        workspace_id=team.workspace_id,
                        user_id=principal.user_id,
                        role=WorkspaceRole.MEMBER.value,
                        active=True,
                        invited_by=actor_id,
                        created_at=now,
                        updated_at=now,
                    ),
                    TeamMembership(
                        team_id=team.team_id,
                        user_id=principal.user_id,
                        active=True,
                        invited_by=actor_id,
                        created_at=now,
                        updated_at=now,
                    ),
                    RoleAssignment(
                        user_id=principal.user_id,
                        team_id=team.team_id,
                        role=Role.PROPOSER.value,
                        account_scope=None,
                        venue_scope=None,
                        created_at=now,
                    ),
                ]
            )
        principal = self.facade._require_exact_runtime_principal(
            session,
            principal_id=principal.user_id,
            team=team,
            role=Role.PROPOSER,
            account_id=None,
            venue=None,
            error_code="SIGNAL_SERVICE_PRINCIPAL_INVALID",
            error_message="the dedicated signal principal is outside the active team",
            allow_inactive=True,
        )
        self.facade._set_internal_principal_active(session, principal.user_id, True)
        return principal

    def configure_signal_source(
        self,
        *,
        actor_id: UUID,
        mode: SignalSourceMode,
        secret: str,
        enabled: bool,
        webhook_max_age_seconds: int,
        expected_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> UUID:
        normalized_secret = secret.strip()
        secret_bytes = normalized_secret.encode()
        if (
            normalized_secret != secret
            or len(secret_bytes) < (32 if mode is SignalSourceMode.WEBHOOK else 8)
            or len(secret_bytes) > 512
            or not 30 <= webhook_max_age_seconds <= 900
        ):
            _reject(
                "SIGNAL_SOURCE_CONFIG_INVALID",
                "signal credential or freshness boundary is invalid",
            )
        with self.database.session_factory.begin() as session:
            team = self.transactions._require_role(session, actor_id, "signal.manage")
            caller = f"{actor_id}:{team.team_id}"
            payload = {
                "mode": mode.value,
                "secret_semantics": self.credential_cipher.secret_fingerprint(
                    normalized_secret,
                    purpose=f"signal-source:{team.team_id}:{mode.value.lower()}",
                ),
                "enabled": enabled,
                "webhook_max_age_seconds": webhook_max_age_seconds,
                "expected_version": expected_version,
            }
            digest, replay = self.transactions._idempotency(
                session,
                caller_id=caller,
                operation="signal-source.configure",
                idempotency_key=idempotency_key,
                payload=payload,
            )
            if replay is not None:
                return UUID(str(replay["signal_source_id"]))
            source = session.scalar(
                select(TeamSignalSource)
                .where(TeamSignalSource.team_id == team.team_id)
                .with_for_update()
            )
            current_version = 0 if source is None else source.version
            if current_version != expected_version:
                _reject("VERSION_CONFLICT", "signal source changed before configuration")
            signal_source_id = uuid4() if source is None else source.signal_source_id
            credential_version = 1 if source is None else source.credential_version + 1
            encrypted = self.credential_cipher.encrypt_secret(
                normalized_secret,
                team_id=team.team_id,
                object_id=signal_source_id,
                purpose=f"signal-source:{mode.value.lower()}",
                credential_version=credential_version,
            )
            previous_principal_id = None if source is None else source.service_principal_id
            principal = (
                self._ensure_signal_service_principal(
                    session,
                    team=team,
                    actor_id=actor_id,
                    now=now,
                )
                if mode is SignalSourceMode.PERPTAPE and enabled
                else None
            )
            if previous_principal_id is not None and principal is None:
                self.facade._set_internal_principal_active(
                    session,
                    previous_principal_id,
                    False,
                )
            configured_principal_id = (
                principal.user_id
                if principal is not None
                else previous_principal_id
                if mode is SignalSourceMode.PERPTAPE
                else None
            )
            if source is None:
                source = TeamSignalSource(
                    signal_source_id=signal_source_id,
                    team_id=team.team_id,
                    mode=mode.value,
                    enabled=enabled,
                    credential_ciphertext=encrypted.ciphertext,
                    credential_metadata=encrypted.metadata,
                    credential_version=credential_version,
                    webhook_max_age_seconds=webhook_max_age_seconds,
                    service_principal_id=configured_principal_id,
                    version=1,
                    created_by=actor_id,
                    updated_by=actor_id,
                    created_at=now,
                    updated_at=now,
                )
                session.add(source)
            else:
                source.mode = mode.value
                source.enabled = enabled
                source.credential_ciphertext = encrypted.ciphertext
                source.credential_metadata = encrypted.metadata
                source.credential_version = credential_version
                source.webhook_max_age_seconds = webhook_max_age_seconds
                source.service_principal_id = configured_principal_id
                source.version += 1
                source.updated_by = actor_id
                source.updated_at = now
            if mode is SignalSourceMode.WEBHOOK:
                defaults = session.scalar(
                    select(ProposalDefaultConfig)
                    .where(
                        ProposalDefaultConfig.team_id == team.team_id,
                        ProposalDefaultConfig.active,
                    )
                    .with_for_update()
                )
                if defaults is not None and defaults.auto_proposal_enabled:
                    defaults.active = False
                    session.add(
                        ProposalDefaultConfig(
                            team_id=team.team_id,
                            version=defaults.version + 1,
                            environment=defaults.environment,
                            account_id=defaults.account_id,
                            risk_tier=defaults.risk_tier,
                            notional=defaults.notional,
                            max_risk=defaults.max_risk,
                            invalidation_bps=defaults.invalidation_bps,
                            expires_in_minutes=defaults.expires_in_minutes,
                            rationale=defaults.rationale,
                            auto_proposal_enabled=False,
                            auto_proposal_min_timeframes=defaults.auto_proposal_min_timeframes,
                            active=True,
                            updated_by=actor_id,
                            effective_at=now,
                        )
                    )
            session.flush()
            result = {"signal_source_id": str(source.signal_source_id)}
            self.transactions._save_receipt(
                session,
                caller_id=caller,
                operation="signal-source.configure",
                idempotency_key=idempotency_key,
                semantic_hash=digest,
                response=result,
                now=now,
            )
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="SIGNAL_SOURCE_CONFIGURED",
                object_type="TeamSignalSource",
                object_id=source.signal_source_id,
                reason=(
                    f"mode={mode.value};enabled={str(enabled).lower()};"
                    f"credential_version={credential_version}"
                ),
                correlation_id=uuid4(),
                object_version=source.version,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                now=now,
            )
            return source.signal_source_id

    def perptape_source_runtime(self, actor_id: UUID) -> dict[str, Any]:
        with self.database.session_factory() as session:
            team = self.transactions._require_action_assignment(session, actor_id, "signal.view")
            source = session.scalar(
                select(TeamSignalSource).where(TeamSignalSource.team_id == team.team_id)
            )
            if source is None:
                _reject("SIGNAL_SOURCE_NOT_CONFIGURED", "the active team has no signal source")
            if not source.enabled:
                _reject("SIGNAL_SOURCE_DISABLED", "the active team signal source is disabled")
            if source.mode != SignalSourceMode.PERPTAPE.value:
                _reject(
                    "SIGNAL_SOURCE_MODE_MISMATCH",
                    "the active team uses Webhook signals instead of Perptape",
                )
            api_key = (
                None
                if source.credential_ciphertext is None
                else self.credential_cipher.decrypt_secret(
                    source.credential_ciphertext,
                    team_id=team.team_id,
                    object_id=source.signal_source_id,
                    purpose="signal-source:perptape",
                    credential_version=source.credential_version,
                )
            )
            return {
                "signal_source_id": source.signal_source_id,
                "team_id": team.team_id,
                "version": source.version,
                "api_key": api_key,
                "runtime_fallback": source.credential_ciphertext is None,
                "service_principal_id": source.service_principal_id,
            }

    def signal_service_principal(self, actor_id: UUID) -> UUID | None:
        runtime = self.perptape_source_runtime(actor_id)
        value = runtime["service_principal_id"]
        return None if value is None else UUID(str(value))

    def ingest_webhook_signal(
        self,
        signal_source_id: UUID,
        *,
        raw_body: bytes,
        payload: dict[str, Any],
        request_timestamp: str,
        nonce: str,
        signature: str,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[UUID, bool]:
        if len(raw_body) > 65_536:
            _reject("SIGNAL_PAYLOAD_TOO_LARGE", "signal payload exceeds 64 KiB")
        if not SIGNAL_NONCE_PATTERN.fullmatch(nonce) or not 1 <= len(idempotency_key) <= 160:
            _reject("SIGNAL_HEADERS_INVALID", "signal nonce or idempotency key is invalid")
        try:
            request_time = datetime.fromtimestamp(int(request_timestamp), UTC)
        except (OSError, OverflowError, TypeError, ValueError):
            _reject("SIGNAL_TIMESTAMP_INVALID", "signal request timestamp is invalid")
        with self.database.session_factory.begin() as session:
            source = session.scalar(
                select(TeamSignalSource)
                .where(TeamSignalSource.signal_source_id == signal_source_id)
                .with_for_update()
            )
            if source is None:
                _reject("SIGNAL_SOURCE_NOT_FOUND", "signal endpoint does not exist")
            if not source.enabled or source.mode != SignalSourceMode.WEBHOOK.value:
                _reject("SIGNAL_SOURCE_DISABLED", "Webhook signal ingestion is disabled")
            if source.credential_ciphertext is None:
                _reject("SIGNAL_SOURCE_NOT_CONFIGURED", "Webhook signature secret is missing")
            max_age = timedelta(seconds=source.webhook_max_age_seconds)
            if request_time < now.astimezone(UTC) - max_age:
                _reject("SIGNAL_REQUEST_STALE", "signal request timestamp is stale")
            if request_time > now.astimezone(UTC) + SIGNAL_CLOCK_SKEW:
                _reject("SIGNAL_TIMESTAMP_FUTURE", "signal request timestamp is in the future")
            secret = self.credential_cipher.decrypt_secret(
                source.credential_ciphertext,
                team_id=source.team_id,
                object_id=source.signal_source_id,
                purpose="signal-source:webhook",
                credential_version=source.credential_version,
            )
            signed = request_timestamp.encode() + b"." + nonce.encode() + b"." + raw_body
            expected = "v1=" + hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                _reject("SIGNAL_SIGNATURE_INVALID", "signal signature verification failed")
            try:
                occurred_at = datetime.fromisoformat(str(payload["signal_at"]))
                if occurred_at.utcoffset() is None:
                    raise ValueError("timezone required")
                occurred_at = occurred_at.astimezone(UTC)
            except (KeyError, TypeError, ValueError):
                _reject("SIGNAL_PAYLOAD_INVALID", "signal_at is invalid")
            if occurred_at < now.astimezone(UTC) - max_age:
                _reject("SIGNAL_STALE", "signal event is older than the team freshness window")
            if occurred_at > now.astimezone(UTC) + SIGNAL_CLOCK_SKEW:
                _reject("SIGNAL_TIMESTAMP_FUTURE", "signal event timestamp is in the future")
            normalized = {
                **payload,
                "signal_at": occurred_at.isoformat(),
                "reference_price": (
                    None
                    if payload.get("reference_price") is None
                    else str(payload["reference_price"])
                ),
            }
            semantic_hash = _semantic_hash(normalized)
            existing = session.scalar(
                select(SignalEvent).where(
                    SignalEvent.team_id == source.team_id,
                    SignalEvent.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if existing.semantic_hash != semantic_hash:
                    raise IdempotencyConflict
                return existing.signal_event_id, True
            if session.scalar(
                select(SignalEvent.signal_event_id).where(
                    SignalEvent.signal_source_id == source.signal_source_id,
                    SignalEvent.nonce == nonce,
                )
            ):
                _reject("SIGNAL_REPLAY_DETECTED", "signal nonce was already accepted")
            if session.scalar(
                select(SignalEvent.signal_event_id).where(
                    SignalEvent.team_id == source.team_id,
                    SignalEvent.provider == str(payload["provider"]),
                    SignalEvent.external_id == str(payload["external_id"]),
                )
            ):
                _reject("SIGNAL_REPLAY_DETECTED", "signal external identity was already accepted")
            event = SignalEvent(
                team_id=source.team_id,
                signal_source_id=source.signal_source_id,
                provider=str(payload["provider"]),
                external_id=str(payload["external_id"]),
                idempotency_key=idempotency_key,
                nonce=nonce,
                payload_version=int(payload["payload_version"]),
                venue=str(payload["venue"]),
                symbol=str(payload["symbol"]),
                direction=str(payload["direction"]),
                strategy_id=str(payload["strategy_id"]),
                strategy_version=str(payload["strategy_version"]),
                timeframe=(None if payload.get("timeframe") is None else str(payload["timeframe"])),
                reference_price=(
                    None
                    if payload.get("reference_price") is None
                    else Decimal(str(payload["reference_price"]))
                ),
                occurred_at=occurred_at,
                received_at=now,
                status=SignalEventStatus.RECEIVED.value,
                normalized_payload=normalized,
                semantic_hash=semantic_hash,
                signature_version="v1",
            )
            session.add(event)
            session.flush()
            team = session.get(Team, source.team_id)
            assert team is not None
            self.transactions._audit(
                session,
                actor_id=f"webhook:{source.signal_source_id}",
                event_type="SIGNAL_RECEIVED",
                object_type="SignalEvent",
                object_id=event.signal_event_id,
                reason=f"provider={event.provider};strategy={event.strategy_id}",
                correlation_id=event.signal_event_id,
                object_version=1,
                idempotency_key=idempotency_key,
                workspace_id=team.workspace_id,
                team_id=team.team_id,
                now=now,
            )
            self.transactions._enqueue_notification_event(
                session,
                actor_id=f"webhook:{source.signal_source_id}",
                team=team,
                event_type="SIGNAL_EVENT_RECEIVED",
                payload={
                    "summary": "团队收到并验证了一条 Webhook 信号; 不会自动创建提案。",
                    "provider": event.provider,
                    "strategy": event.strategy_id,
                    "strategy_version": event.strategy_version,
                    "venue": event.venue,
                    "symbol": event.symbol,
                    "direction": event.direction,
                },
                object_type="SignalEvent",
                object_id=event.signal_event_id,
                object_version=1,
                idempotency_key=idempotency_key,
                correlation_id=event.signal_event_id,
                environment=None,
                account_id=None,
                venue=event.venue,
                now=now,
            )
            return event.signal_event_id, False

    @staticmethod
    def _signal_event_payload(
        event: SignalEvent,
        *,
        workspace_id: UUID,
        proposal: Proposal | None,
    ) -> dict[str, Any]:
        return {
            "signal_event_id": str(event.signal_event_id),
            "workspace_id": str(workspace_id),
            "team_id": str(event.team_id),
            "signal_source_id": str(event.signal_source_id),
            "provider": event.provider,
            "external_id": event.external_id,
            "venue": event.venue,
            "symbol": event.symbol,
            "direction": event.direction,
            "strategy_id": event.strategy_id,
            "strategy_version": event.strategy_version,
            "timeframe": event.timeframe,
            "reference_price": (
                None if event.reference_price is None else str(event.reference_price)
            ),
            "occurred_at": event.occurred_at.isoformat(),
            "received_at": event.received_at.isoformat(),
            "status": event.status,
            "payload_version": event.payload_version,
            "proposal": (
                None
                if proposal is None
                else {
                    "proposal_id": str(proposal.proposal_id),
                    "status": proposal.status,
                    "version": proposal.version,
                }
            ),
        }

    def signal_event(self, actor_id: UUID, signal_event_id: UUID) -> dict[str, Any]:
        with self.database.session_factory() as session:
            team = self.transactions._require_action_assignment(session, actor_id, "signal.view")
            event = session.scalar(
                select(SignalEvent).where(
                    SignalEvent.signal_event_id == signal_event_id,
                    SignalEvent.team_id == team.team_id,
                )
            )
            if event is None:
                _reject(
                    "SIGNAL_EVENT_NOT_FOUND",
                    "signal event is outside the active team or does not exist",
                )
            proposal = session.scalar(
                select(Proposal).where(
                    Proposal.team_id == team.team_id,
                    Proposal.signal_event_id == event.signal_event_id,
                )
            )
            return self._signal_event_payload(
                event,
                workspace_id=team.workspace_id,
                proposal=proposal,
            )

    def list_signal_events(self, actor_id: UUID, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.session_factory() as session:
            team = self.transactions._require_action_assignment(session, actor_id, "signal.view")
            events = session.scalars(
                select(SignalEvent)
                .where(SignalEvent.team_id == team.team_id)
                .order_by(SignalEvent.received_at.desc(), SignalEvent.signal_event_id.desc())
                .limit(min(max(limit, 1), 200))
            ).all()
            proposal_by_signal = {
                proposal.signal_event_id: proposal
                for proposal in session.scalars(
                    select(Proposal).where(
                        Proposal.team_id == team.team_id,
                        Proposal.signal_event_id.in_([item.signal_event_id for item in events]),
                    )
                ).all()
                if proposal.signal_event_id is not None
            }
            return [
                self._signal_event_payload(
                    event,
                    workspace_id=team.workspace_id,
                    proposal=proposal_by_signal.get(event.signal_event_id),
                )
                for event in events
            ]

    def record_runtime_source_health(
        self,
        actor_id: UUID,
        sources: dict[str, dict[str, Any]],
        *,
        scopes: dict[str, tuple[str, str] | None] | None = None,
        require_exact_account_scope: bool = True,
        runtime_account_binding: PreparedRuntimeAccountBinding | None = None,
        perptape_runtime_binding: PreparedPerptapeRuntimeBinding | None = None,
        now: datetime,
    ) -> None:
        with self.database.session_factory() as session, session.begin():
            if runtime_account_binding is not None and perptape_runtime_binding is not None:
                _reject(
                    "RUNTIME_BINDING_INVALID",
                    "runtime health cannot bind an account and signal source together",
                )
            if runtime_account_binding is not None:
                self.facade._lock_runtime_account_binding(session, runtime_account_binding)
            if perptape_runtime_binding is not None:
                self.facade._lock_perptape_runtime_binding(session, perptape_runtime_binding)
            principal = session.get(User, actor_id)
            if (
                principal is None
                or not principal.active
                or principal.principal_type != PrincipalType.SERVICE.value
            ):
                _reject(
                    "SERVICE_PRINCIPAL_REQUIRED",
                    "runtime source health requires an active service principal",
                )
            _principal, _workspace, team = self.transactions._active_scope(session, actor_id)
            assert team is not None
            for source_name, result in sources.items():
                status = str(result.get("status", ""))
                error_code = result.get("error_code")
                items_observed = int(result.get("items_observed", 0))
                if status not in {"SUCCESS", "FAILED", "SKIPPED"}:
                    _reject(
                        "RUNTIME_HEALTH_STATUS_INVALID",
                        "runtime source health status is invalid",
                    )
                if error_code is not None and (
                    not isinstance(error_code, str)
                    or len(error_code) > 120
                    or not error_code.replace("_", "").replace(":", "").isalnum()
                ):
                    _reject(
                        "RUNTIME_HEALTH_ERROR_INVALID",
                        "runtime source health error code is invalid",
                    )
                if items_observed < 0:
                    _reject(
                        "RUNTIME_HEALTH_ITEMS_INVALID",
                        "runtime source item count cannot be negative",
                    )
                requested_scope = (scopes or {}).get(source_name)
                account_id: str | None = None
                venue: str | None = None
                if requested_scope is not None:
                    account_id, venue = requested_scope
                    account = session.scalar(
                        select(ExchangeAccount).where(
                            ExchangeAccount.team_id == team.team_id,
                            ExchangeAccount.account_id == account_id,
                            ExchangeAccount.venue == venue,
                        )
                    )
                    if account is None:
                        _reject(
                            "RUNTIME_HEALTH_SCOPE_INVALID",
                            "runtime source health account is outside the principal team",
                        )
                    assignment = session.scalar(
                        select(RoleAssignment).where(
                            RoleAssignment.team_id == team.team_id,
                            RoleAssignment.user_id == actor_id,
                            RoleAssignment.role == Role.OPERATOR.value,
                            RoleAssignment.account_scope == account_id,
                            RoleAssignment.venue_scope == venue,
                        )
                    )
                    if assignment is None and not require_exact_account_scope:
                        assignment = session.scalar(
                            select(RoleAssignment).where(
                                RoleAssignment.team_id == team.team_id,
                                RoleAssignment.user_id == actor_id,
                                RoleAssignment.role == Role.OPERATOR.value,
                                RoleAssignment.account_scope.is_(None),
                                RoleAssignment.venue_scope.is_(None),
                            )
                        )
                    if assignment is None:
                        _reject(
                            "RUNTIME_HEALTH_SCOPE_DENIED",
                            "runtime source health requires the exact account operator scope",
                        )
                current = session.scalar(
                    select(RuntimeSourceHealth)
                    .where(
                        RuntimeSourceHealth.team_id == team.team_id,
                        RuntimeSourceHealth.source_name == source_name,
                        RuntimeSourceHealth.account_id == account_id,
                        RuntimeSourceHealth.venue == venue,
                    )
                    .with_for_update()
                )
                if (
                    current is not None
                    and status == "SKIPPED"
                    and error_code is not None
                    and error_code.endswith("_RATE_LIMITED_COOLDOWN")
                ):
                    continue
                consecutive_failures = (
                    (current.consecutive_failures if current is not None else 0) + 1
                    if status == "FAILED"
                    else 0
                )
                retry_at = None
                if status == "FAILED" and error_code is not None and "RATE_LIMITED" in error_code:
                    retry_seconds = min(300, 60 * (2 ** min(consecutive_failures - 1, 3)))
                    retry_at = now + timedelta(seconds=retry_seconds)
                last_success_at = (
                    now
                    if status == "SUCCESS"
                    else current.last_success_at
                    if current is not None
                    else None
                )
                if current is None:
                    session.add(
                        RuntimeSourceHealth(
                            team_id=team.team_id,
                            source_name=source_name,
                            account_id=account_id,
                            venue=venue,
                            status=status,
                            items_observed=items_observed,
                            error_code=error_code,
                            checked_at=now,
                            last_success_at=last_success_at,
                            retry_at=retry_at,
                            consecutive_failures=consecutive_failures,
                            updated_by=actor_id,
                        )
                    )
                else:
                    current.status = status
                    current.items_observed = items_observed
                    current.error_code = error_code
                    current.checked_at = now
                    current.last_success_at = last_success_at
                    current.retry_at = retry_at
                    current.consecutive_failures = consecutive_failures
                    current.updated_by = actor_id

    def record_perptape_feed(
        self,
        actor_id: UUID,
        feed: PerptapeFeedSnapshot,
        *,
        now: datetime,
        base_snapshot: PerptapeFeedSnapshot | None,
        runtime_binding: PreparedPerptapeRuntimeBinding | None = None,
    ) -> int:
        now_utc = normalize_perptape_datetime(now)
        feed = bound_perptape_feed_snapshot(feed)
        if base_snapshot is not None:
            base_snapshot = bound_perptape_feed_snapshot(base_snapshot)
        validate_perptape_feed_payload(feed)
        generated_at_utc = normalize_perptape_datetime(feed.generated_at)
        fetched_at_utc = normalize_perptape_datetime(feed.fetched_at)
        next_allowed_at_utc = normalize_perptape_datetime(feed.next_allowed_at)
        if (
            fetched_at_utc - now_utc > MAX_FACT_CLOCK_SKEW
            or generated_at_utc - fetched_at_utc > MAX_FACT_CLOCK_SKEW
            or next_allowed_at_utc < generated_at_utc
            or any(
                candidate.source_contract_version != feed.contract_version
                for candidate in feed.candidates
            )
        ):
            _reject("PERPTAPE_RESPONSE_INVALID", "Perptape feed metadata is inconsistent")
        with self.database.session_factory.begin() as session:
            if runtime_binding is not None:
                self.facade._lock_perptape_runtime_binding(session, runtime_binding)
            _actor, workspace, team = self.transactions._active_scope(session, actor_id)
            assert team is not None
            self.transactions._require_role(
                session,
                actor_id,
                "proposal.create",
                team_id=team.team_id,
                allow_setup=True,
            )
            current = session.get(
                PerptapeFeed,
                (team.team_id, "BREAKOUTS"),
                with_for_update=True,
            )
            current_feed = (
                None
                if current is None
                else PerptapeFeedSnapshot(
                    contract_version=current.contract_version,
                    generated_at=current.generated_at,
                    fetched_at=current.fetched_at,
                    next_allowed_at=current.next_allowed_at,
                    candidates=tuple(
                        PerptapeCandidate.from_dict(value) for value in current.candidates
                    ),
                )
            )
            if current is None and base_snapshot is not None:
                _reject(
                    "PERPTAPE_FEED_CONFLICT",
                    "the base Perptape snapshot no longer exists",
                )
            feed = apply_perptape_feed_delta(
                base=base_snapshot,
                current=current_feed,
                incoming=feed,
            )
            validate_perptape_feed_payload(feed)
            if current_feed is not None and perptape_snapshot_identity(
                current_feed
            ) == perptape_snapshot_identity(feed):
                assert current is not None
                return current.version
            generated_at_utc = normalize_perptape_datetime(feed.generated_at)
            fetched_at_utc = normalize_perptape_datetime(feed.fetched_at)
            next_allowed_at_utc = normalize_perptape_datetime(feed.next_allowed_at)
            if (
                fetched_at_utc - now_utc > MAX_FACT_CLOCK_SKEW
                or generated_at_utc - fetched_at_utc > MAX_FACT_CLOCK_SKEW
                or next_allowed_at_utc < generated_at_utc
            ):
                _reject(
                    "PERPTAPE_RESPONSE_INVALID",
                    "merged Perptape feed metadata is inconsistent",
                )
            candidates = [candidate.to_dict() for candidate in feed.candidates]
            if current is None:
                current = PerptapeFeed(
                    team_id=team.team_id,
                    feed_key="BREAKOUTS",
                    contract_version=feed.contract_version,
                    candidates=candidates,
                    generated_at=feed.generated_at,
                    fetched_at=feed.fetched_at,
                    next_allowed_at=feed.next_allowed_at,
                    version=1,
                    updated_at=now,
                )
                session.add(current)
            else:
                current.contract_version = feed.contract_version
                current.candidates = candidates
                current.generated_at = feed.generated_at
                current.fetched_at = feed.fetched_at
                current.next_allowed_at = feed.next_allowed_at
                current.version += 1
                current.updated_at = now
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="PERPTAPE_FEED_RECORDED",
                object_type="PerptapeFeed",
                object_id=current.feed_key,
                reason=f"{len(candidates)} candidates",
                correlation_id=uuid4(),
                object_version=current.version,
                workspace_id=workspace.workspace_id,
                team_id=team.team_id,
                now=now,
            )
            return current.version

    def register_instrument(
        self,
        *,
        actor_id: UUID,
        venue: str,
        symbol: str,
        tick_size: Decimal,
        lot_size: Decimal,
        minimum_notional: Decimal,
        contract_multiplier: Decimal,
        quote_currency: str,
        collateral_currency: str,
        protection_supported: bool,
        now: datetime,
    ) -> UUID:
        with self.database.session_factory.begin() as session:
            self.transactions._require_role(session, actor_id, "instrument.manage")
            instrument = Instrument(
                venue=venue,
                symbol=symbol,
                tick_size=tick_size,
                lot_size=lot_size,
                minimum_notional=minimum_notional,
                contract_multiplier=contract_multiplier,
                quote_currency=quote_currency,
                collateral_currency=collateral_currency,
                active=True,
                protection_supported=protection_supported,
                updated_at=now,
            )
            session.add(instrument)
            session.flush()
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type="INSTRUMENT_REGISTERED",
                object_type="Instrument",
                object_id=instrument.instrument_id,
                reason=f"{venue}:{symbol}",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return instrument.instrument_id

    def upsert_venue_instrument(
        self,
        *,
        actor_id: UUID,
        account_id: str,
        venue: str,
        symbol: str,
        tick_size: Decimal,
        lot_size: Decimal,
        minimum_notional: Decimal,
        quote_currency: str,
        collateral_currency: str,
        active: bool,
        now: datetime,
    ) -> UUID:
        """Refresh a read-only venue contract catalog entry from official venue metadata."""

        with self.database.session_factory.begin() as session:
            self.transactions._require_role(session, actor_id, "venue.record", account_id, venue)
            instrument = session.scalar(
                select(Instrument)
                .where(Instrument.venue == venue, Instrument.symbol == symbol)
                .with_for_update()
            )
            event_type = "INSTRUMENT_REGISTERED"
            if instrument is None:
                instrument = Instrument(
                    venue=venue,
                    symbol=symbol,
                    tick_size=tick_size,
                    lot_size=lot_size,
                    minimum_notional=minimum_notional,
                    contract_multiplier=Decimal(1),
                    quote_currency=quote_currency,
                    collateral_currency=collateral_currency,
                    active=active,
                    protection_supported=True,
                    updated_at=now,
                )
                session.add(instrument)
                session.flush()
            else:
                event_type = "INSTRUMENT_REFRESHED"
                instrument.tick_size = tick_size
                instrument.lot_size = lot_size
                instrument.minimum_notional = minimum_notional
                instrument.quote_currency = quote_currency
                instrument.collateral_currency = collateral_currency
                instrument.active = active
                instrument.updated_at = now
            self.transactions._audit(
                session,
                actor_id=str(actor_id),
                event_type=event_type,
                object_type="Instrument",
                object_id=instrument.instrument_id,
                reason=f"OFFICIAL_VENUE_METADATA:{venue}:{symbol}",
                correlation_id=uuid4(),
                object_version=1,
                now=now,
            )
            return instrument.instrument_id

    def synchronize_active_venue_instruments(
        self,
        *,
        actor_id: UUID,
        account_id: str,
        venue: str,
        instruments: Sequence[BinanceInstrument | HyperliquidInstrument | VenueInstrument],
        hip3_dexes: tuple[str, ...] = (),
        runtime_binding: PreparedRuntimeAccountBinding | None = None,
        now: datetime,
    ) -> dict[str, int]:
        """Replace one venue's active Catalog set from a complete official read-only snapshot."""

        if venue not in SUPPORTED_EXCHANGE_VENUES or not instruments:
            _reject("INSTRUMENT_CATALOG_INVALID", "official active instrument catalog is invalid")
        symbols = [instrument.symbol for instrument in instruments]
        for symbol in symbols:
            try:
                freqtrade_pair(venue, symbol, hip3_dexes=hip3_dexes)
            except DomainRejected as exc:
                _reject(
                    "INSTRUMENT_CATALOG_INVALID",
                    f"official catalog symbol is not executable: {exc.code}",
                )
        if len(symbols) != len(set(symbols)) or any(
            not instrument.active
            or not instrument.symbol
            or instrument.tick_size <= 0
            or instrument.lot_size <= 0
            or instrument.minimum_notional < 0
            or not instrument.quote_currency
            or not instrument.collateral_currency
            for instrument in instruments
        ):
            _reject("INSTRUMENT_CATALOG_INVALID", "official active instrument catalog is invalid")

        with self.database.session_factory.begin() as session:
            if runtime_binding is not None:
                self.facade._lock_runtime_account_binding(session, runtime_binding)
            self.transactions._require_role(session, actor_id, "venue.record", account_id, venue)
            existing = {
                instrument.symbol: instrument
                for instrument in session.scalars(
                    select(Instrument).where(Instrument.venue == venue).with_for_update()
                )
            }
            created = 0
            refreshed = 0
            unchanged = 0
            for snapshot in instruments:
                instrument = existing.get(snapshot.symbol)
                desired = (
                    snapshot.tick_size,
                    snapshot.lot_size,
                    snapshot.minimum_notional,
                    Decimal(1),
                    snapshot.quote_currency,
                    snapshot.collateral_currency,
                    True,
                    True,
                )
                if instrument is None:
                    instrument = Instrument(
                        venue=venue,
                        symbol=snapshot.symbol,
                        tick_size=desired[0],
                        lot_size=desired[1],
                        minimum_notional=desired[2],
                        contract_multiplier=desired[3],
                        quote_currency=desired[4],
                        collateral_currency=desired[5],
                        active=desired[6],
                        protection_supported=desired[7],
                        updated_at=now,
                    )
                    session.add(instrument)
                    created += 1
                    continue
                current = (
                    instrument.tick_size,
                    instrument.lot_size,
                    instrument.minimum_notional,
                    instrument.contract_multiplier,
                    instrument.quote_currency,
                    instrument.collateral_currency,
                    instrument.active,
                    instrument.protection_supported,
                )
                if current == desired:
                    unchanged += 1
                    continue
                (
                    instrument.tick_size,
                    instrument.lot_size,
                    instrument.minimum_notional,
                    instrument.contract_multiplier,
                    instrument.quote_currency,
                    instrument.collateral_currency,
                    instrument.active,
                    instrument.protection_supported,
                ) = desired
                instrument.updated_at = now
                refreshed += 1

            active_symbols = set(symbols)
            deactivated = 0
            for symbol, instrument in existing.items():
                if symbol not in active_symbols and instrument.active:
                    instrument.active = False
                    instrument.updated_at = now
                    deactivated += 1

            if created or refreshed or deactivated:
                self.transactions._audit(
                    session,
                    actor_id=str(actor_id),
                    event_type="INSTRUMENT_CATALOG_SYNCED",
                    object_type="InstrumentCatalog",
                    object_id=f"{venue}:active",
                    reason=(
                        f"official read-only catalog active={len(instruments)} created={created} "
                        f"refreshed={refreshed} deactivated={deactivated} unchanged={unchanged}"
                    ),
                    correlation_id=uuid4(),
                    object_version=1,
                    now=now,
                )
            return {
                "active": len(instruments),
                "created": created,
                "refreshed": refreshed,
                "deactivated": deactivated,
                "unchanged": unchanged,
            }
