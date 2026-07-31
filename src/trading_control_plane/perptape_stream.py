from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, cast

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from trading_control_plane.domain import DomainRejected
from trading_control_plane.perptape import (
    PERPTAPE_CANDIDATE_WINDOW,
    PerptapeCandidate,
    PerptapeClient,
    PerptapeEventKey,
    PerptapeFeedSnapshot,
    PerptapeRateLimited,
    bound_perptape_feed_snapshot,
    merge_incomplete_perptape_candidates,
    normalize_perptape_datetime,
    normalize_perptape_operational_datetime,
    perptape_event_key,
    validate_perptape_feed_contract,
    validate_perptape_websocket_url,
)

logger = logging.getLogger(__name__)
PERPTAPE_STREAM_WINDOW = PERPTAPE_CANDIDATE_WINDOW
PERPTAPE_FATAL_INPUT_ERROR_CODES = {
    "PERPTAPE_DATETIME_INVALID",
    "PERPTAPE_DECIMAL_INVALID",
    "PERPTAPE_FIELD_TOO_LARGE",
    "PERPTAPE_PAYLOAD_TOO_LARGE",
}


class PerptapeSocket(Protocol):
    def send(self, message: str) -> None: ...

    def recv(self, timeout: float | None = None) -> str | bytes: ...


class StopEvent(Protocol):
    def is_set(self) -> bool: ...

    def set(self) -> None: ...

    def wait(self, timeout: float) -> bool: ...


SocketConnector = Callable[[str, dict[str, str], float], AbstractContextManager[PerptapeSocket]]
SnapshotLoader = Callable[[], PerptapeFeedSnapshot | None]
SnapshotRecorder = Callable[
    [PerptapeFeedSnapshot, datetime, PerptapeFeedSnapshot | None],
    object,
]
MessageOrder = Literal["ACCEPT", "DUPLICATE", "OUT_OF_ORDER", "GAP"]


@contextmanager
def _default_connector(
    url: str,
    headers: dict[str, str],
    timeout_seconds: float,
) -> Iterator[PerptapeSocket]:
    try:
        with connect(
            url,
            additional_headers=headers,
            user_agent_header="trading-control-plane/1.0",
            open_timeout=timeout_seconds,
            close_timeout=min(timeout_seconds, 5),
            ping_interval=15,
            ping_timeout=timeout_seconds,
            max_size=256 * 1024,
            max_queue=32,
        ) as connection:
            yield cast(PerptapeSocket, connection)
    except (OSError, TimeoutError, WebSocketException) as exc:
        raise DomainRejected(
            "PERPTAPE_STREAM_UNAVAILABLE",
            "Perptape WebSocket could not be reached",
        ) from exc


@dataclass(frozen=True, slots=True)
class StreamEnvelope:
    event_type: Literal["hello", "heartbeat", "alert"]
    sequence: int | None
    server_time: datetime
    payload: dict[str, Any]
    event_id: str | None


@dataclass(slots=True)
class PerptapeStreamStats:
    connections: int = 0
    messages_received: int = 0
    alerts_applied: int = 0
    duplicates_dropped: int = 0
    gaps_detected: int = 0
    https_reconciliations: int = 0
    https_backfills: int = 0
    degraded_writes: int = 0


class PerptapeStreamWorker:
    """Consume Perptape alerts while keeping PostgreSQL's feed snapshot authoritative."""

    def __init__(
        self,
        *,
        client: PerptapeClient,
        websocket_url: str,
        api_key: str | None,
        contract_version: str,
        load_snapshot: SnapshotLoader,
        record_snapshot: SnapshotRecorder,
        timeout_seconds: float,
        heartbeat_timeout_seconds: float,
        reconciliation_interval_seconds: float,
        reconnect_initial_seconds: float,
        reconnect_max_seconds: float,
        max_reconnect_attempts: int,
        connector: SocketConnector = _default_connector,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._websocket_url = validate_perptape_websocket_url(websocket_url)
        self._api_key = api_key
        self._contract_version = contract_version
        self._load_snapshot = load_snapshot
        self._record_snapshot = record_snapshot
        self._timeout_seconds = timeout_seconds
        self._heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self._reconciliation_interval_seconds = reconciliation_interval_seconds
        self._reconnect_initial_seconds = reconnect_initial_seconds
        self._reconnect_max_seconds = reconnect_max_seconds
        self._max_reconnect_attempts = max_reconnect_attempts
        self._connector = connector
        self._clock = clock
        self._monotonic = monotonic
        self._seen_event_ids: set[str] = set()
        self._event_id_order: deque[str] = deque()
        self._connection_healthy = False
        self._backfill_not_before: datetime | None = None
        self._completion_pending = False
        (
            _remote_generation,
            _remote_success_generation,
            self._observed_remote_rate_limit_count,
            self._transport_consecutive_rate_limits,
        ) = self._client.remote_request_state()
        self._target_consecutive_rate_limits = 0
        self._pending_completion_targets: OrderedDict[
            PerptapeEventKey,
            tuple[PerptapeCandidate, int],
        ] = OrderedDict()
        self.fatal_error_code: str | None = None
        self.stats = PerptapeStreamStats()

    @staticmethod
    def _safe_record_time(now: datetime, current: PerptapeFeedSnapshot | None) -> datetime:
        now = normalize_perptape_datetime(now)
        if current is None:
            return now
        current_fetched_at = normalize_perptape_datetime(current.fetched_at)
        if now > current_fetched_at:
            return now
        try:
            return current_fetched_at + timedelta(microseconds=1)
        except (OverflowError, ValueError) as exc:
            raise DomainRejected(
                "PERPTAPE_DATETIME_INVALID",
                "Perptape snapshot time cannot be advanced safely",
            ) from exc

    def _now(self) -> datetime:
        return normalize_perptape_operational_datetime(self._clock())

    @staticmethod
    def _add_time(value: datetime, delta: timedelta) -> datetime:
        try:
            return (
                normalize_perptape_operational_datetime(
                    value,
                    required_headroom=delta,
                )
                + delta
            )
        except (OverflowError, ValueError) as exc:
            raise DomainRejected(
                "PERPTAPE_DATETIME_INVALID",
                "Perptape timestamp arithmetic exceeds the supported range",
            ) from exc

    def _current_snapshot(self) -> PerptapeFeedSnapshot | None:
        current = self._load_snapshot()
        if current is not None:
            validate_perptape_feed_contract(current)
        return current

    def _record(
        self,
        feed: PerptapeFeedSnapshot,
        current: PerptapeFeedSnapshot | None,
    ) -> None:
        feed = bound_perptape_feed_snapshot(feed)
        self._record_snapshot(
            feed,
            feed.fetched_at,
            current,
        )

    def _sync_remote_request_state(self) -> int:
        (
            _remote_generation,
            success_generation,
            rate_limit_count,
            transport_consecutive_rate_limits,
        ) = self._client.remote_request_state()
        if self._pending_completion_targets:
            new_rate_limits = max(
                0,
                rate_limit_count - self._observed_remote_rate_limit_count,
            )
            self._target_consecutive_rate_limits += new_rate_limits
        self._observed_remote_rate_limit_count = rate_limit_count
        self._transport_consecutive_rate_limits = transport_consecutive_rate_limits
        if self._transport_consecutive_rate_limits >= self._max_reconnect_attempts or (
            self._pending_completion_targets
            and self._target_consecutive_rate_limits >= self._max_reconnect_attempts
        ):
            self.fatal_error_code = "PERPTAPE_RATE_LIMITED"
        return success_generation

    @staticmethod
    def _candidate_completes_target(
        candidate: PerptapeCandidate,
        target: PerptapeCandidate,
    ) -> bool:
        return (
            PerptapeStreamWorker._same_alert(candidate, target)
            and candidate.readiness == "READY"
            and candidate.data_health == "CURRENT"
            and candidate.observed_at >= target.observed_at
        )

    def _resolve_pending_targets(
        self,
        feed: PerptapeFeedSnapshot,
        *,
        success_generation: int,
    ) -> bool:
        completed = [
            event_key
            for event_key, (
                target,
                registered_success_generation,
            ) in self._pending_completion_targets.items()
            if success_generation > registered_success_generation
            and any(
                self._candidate_completes_target(candidate, target) for candidate in feed.candidates
            )
        ]
        for event_key in completed:
            del self._pending_completion_targets[event_key]
        if not self._pending_completion_targets:
            self._target_consecutive_rate_limits = 0
            self._completion_pending = False
            return True
        return False

    def _register_completion_target(
        self,
        target: PerptapeCandidate,
    ) -> None:
        event_key = perptape_event_key(target)
        existing = self._pending_completion_targets.get(event_key)
        if existing is not None:
            existing_target, registered_success_generation = existing
            if target.observed_at > existing_target.observed_at:
                success_generation = self._sync_remote_request_state()
                self._pending_completion_targets[event_key] = (
                    target,
                    max(registered_success_generation, success_generation),
                )
            self._completion_pending = True
            return
        success_generation = self._sync_remote_request_state()
        if self.fatal_error_code == "PERPTAPE_RATE_LIMITED":
            raise DomainRejected(
                "PERPTAPE_RATE_LIMITED",
                "Perptape exceeded the bounded rate-limit recovery sequence",
            )
        if len(self._pending_completion_targets) >= PERPTAPE_STREAM_WINDOW:
            self.fatal_error_code = "PERPTAPE_STREAM_PENDING_LIMIT"
            raise DomainRejected(
                "PERPTAPE_STREAM_PENDING_LIMIT",
                "Perptape unresolved alert window is full",
            )
        if not self._pending_completion_targets:
            self._target_consecutive_rate_limits = 0
        self._pending_completion_targets[event_key] = (
            target,
            success_generation,
        )
        self._completion_pending = True

    def _rebuild_pending_targets(self, feed: PerptapeFeedSnapshot | None) -> None:
        if feed is None or feed.contract_version != self._contract_version:
            return
        for candidate in feed.candidates:
            if candidate.readiness == "INCOMPLETE":
                self._register_completion_target(candidate)

    def _preserved_pending_candidates(
        self,
        current: PerptapeFeedSnapshot | None,
    ) -> tuple[PerptapeCandidate, ...]:
        current_by_key = {
            perptape_event_key(candidate): candidate
            for candidate in (
                ()
                if current is None or current.contract_version != self._contract_version
                else current.candidates
            )
        }
        return tuple(
            current_by_key.get(event_key, target)
            for event_key, (target, _generation) in self._pending_completion_targets.items()
        )

    def _reconcile(self, *, backfill: bool) -> PerptapeFeedSnapshot:
        now = self._now()
        self._sync_remote_request_state()
        if self.fatal_error_code == "PERPTAPE_RATE_LIMITED":
            raise DomainRejected(
                "PERPTAPE_RATE_LIMITED",
                "Perptape exceeded the bounded rate-limit recovery sequence",
            )
        base = self._current_snapshot()
        if base is not None and now < base.next_allowed_at:
            raise PerptapeRateLimited(base.next_allowed_at)
        try:
            feed = self._client.refresh(now=now, force=backfill)
        except PerptapeRateLimited as exc:
            self._sync_remote_request_state()
            self._mark_degraded(next_allowed_at=exc.next_allowed_at)
            raise
        success_generation = self._sync_remote_request_state()
        current = self._current_snapshot()
        fetched_at = self._safe_record_time(feed.fetched_at, current)
        if fetched_at != feed.fetched_at:
            feed = replace(feed, fetched_at=fetched_at)
        feed = merge_incomplete_perptape_candidates(
            feed,
            self._preserved_pending_candidates(current),
        )
        self._record(feed, base)
        self.stats.https_reconciliations += 1
        if backfill:
            self.stats.https_backfills += 1
        self._resolve_pending_targets(
            feed,
            success_generation=success_generation,
        )
        return feed

    def _mark_degraded(self, *, next_allowed_at: datetime | None = None) -> None:
        current = self._current_snapshot()
        if current is None:
            return
        candidates_already_degraded = all(
            candidate.data_health == "DEGRADED" and candidate.readiness != "READY"
            for candidate in current.candidates
        )
        effective_next_allowed_at = max(
            current.next_allowed_at,
            current.generated_at,
            next_allowed_at or current.generated_at,
        )
        if candidates_already_degraded and effective_next_allowed_at == current.next_allowed_at:
            return
        fetched_at = self._safe_record_time(self._now(), current)
        degraded = tuple(
            replace(
                candidate,
                data_health="DEGRADED",
                readiness=("INCOMPLETE" if candidate.readiness == "INCOMPLETE" else "DEGRADED"),
            )
            for candidate in current.candidates
        )
        self._record(
            PerptapeFeedSnapshot(
                contract_version=current.contract_version,
                generated_at=current.generated_at,
                fetched_at=fetched_at,
                next_allowed_at=effective_next_allowed_at,
                candidates=degraded,
            ),
            current,
        )
        self.stats.degraded_writes += 1

    def _try_backfill(self) -> bool:
        now = self._now()
        success_generation = self._sync_remote_request_state()
        current = self._current_snapshot()
        had_pending_targets = bool(self._pending_completion_targets)
        if current is not None and had_pending_targets:
            self._resolve_pending_targets(
                current,
                success_generation=success_generation,
            )
            if not self._pending_completion_targets:
                return True
        if self.fatal_error_code == "PERPTAPE_RATE_LIMITED":
            raise DomainRejected(
                "PERPTAPE_RATE_LIMITED",
                "Perptape exceeded the bounded rate-limit recovery sequence",
            )
        not_before = self._backfill_not_before
        if current is not None:
            not_before = max(not_before or current.next_allowed_at, current.next_allowed_at)
        if not_before is not None and now < not_before:
            self._backfill_not_before = not_before
            self._completion_pending = True
            self._mark_degraded()
            return False
        try:
            feed = self._reconcile(backfill=True)
        except DomainRejected as exc:
            if exc.code in PERPTAPE_FATAL_INPUT_ERROR_CODES:
                self.fatal_error_code = exc.code
                raise
            current = self._current_snapshot()
            retry_at = self._add_time(
                now,
                timedelta(seconds=self._reconnect_initial_seconds),
            )
            if current is not None:
                retry_at = max(retry_at, current.next_allowed_at)
            if isinstance(exc, PerptapeRateLimited) and exc.next_allowed_at is not None:
                retry_at = max(retry_at, exc.next_allowed_at)
            self._backfill_not_before = retry_at
            self._completion_pending = True
            self._mark_degraded()
            if self.fatal_error_code == "PERPTAPE_RATE_LIMITED":
                raise
            return False
        self._backfill_not_before = max(now, feed.next_allowed_at)
        if self._pending_completion_targets:
            self._completion_pending = True
            return False
        self._completion_pending = False
        return True

    def _reconciliation_delay(self) -> float:
        delay = self._reconciliation_interval_seconds
        current = self._current_snapshot()
        if current is not None:
            server_delay = (current.next_allowed_at - self._now()).total_seconds()
            delay = max(delay, server_delay)
        return max(0, delay)

    def _stop_for_fatal_error(self, stop_event: StopEvent) -> bool:
        if self.fatal_error_code is None:
            return False
        if self.fatal_error_code not in PERPTAPE_FATAL_INPUT_ERROR_CODES:
            self._mark_degraded()
        logger.error(
            "Perptape WebSocket stopped after bounded failures",
            extra={
                "event": "perptape_stream_stopped",
                "component": "perptape",
                "error_code": self.fatal_error_code,
                "attempt": max(
                    self._transport_consecutive_rate_limits,
                    self._target_consecutive_rate_limits,
                    (
                        len(self._pending_completion_targets)
                        if self.fatal_error_code == "PERPTAPE_STREAM_PENDING_LIMIT"
                        else 0
                    ),
                ),
            },
        )
        stop_event.set()
        return True

    def _parse_message(self, raw_message: str | bytes) -> StreamEnvelope:
        try:
            if isinstance(raw_message, bytes):
                raw_message = raw_message.decode("utf-8")
            value = json.loads(raw_message)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DomainRejected(
                "PERPTAPE_STREAM_MESSAGE_INVALID",
                "Perptape WebSocket returned invalid JSON",
            ) from exc
        if not isinstance(value, dict):
            raise DomainRejected(
                "PERPTAPE_STREAM_MESSAGE_INVALID",
                "Perptape WebSocket message must be an object",
            )
        event_type = value.get("e")
        if event_type == "error":
            remote_code = str(value.get("code", "")).upper()
            code = (
                "PERPTAPE_AUTH_FAILED"
                if "AUTH" in remote_code or "KEY" in remote_code
                else "PERPTAPE_STREAM_PROTOCOL_ERROR"
            )
            raise DomainRejected(code, "Perptape WebSocket rejected the connection")
        if event_type not in {"hello", "heartbeat", "alert"}:
            raise DomainRejected(
                "PERPTAPE_STREAM_MESSAGE_INVALID",
                "Perptape WebSocket event type is invalid",
            )
        sequence_value = value.get("seq")
        try:
            if sequence_value is not None and (
                isinstance(sequence_value, bool) or not isinstance(sequence_value, int)
            ):
                raise ValueError("sequence must be an integer")
            sequence = None if sequence_value is None else int(sequence_value)
            server_time = datetime.fromtimestamp(int(value["E"]) / 1_000, UTC)
        except (KeyError, OSError, OverflowError, TypeError, ValueError) as exc:
            raise DomainRejected(
                "PERPTAPE_STREAM_MESSAGE_INVALID",
                "Perptape WebSocket ordering metadata is invalid",
            ) from exc
        if (sequence is not None and sequence < 0) or server_time > self._add_time(
            self._now(), timedelta(seconds=30)
        ):
            raise DomainRejected(
                "PERPTAPE_STREAM_MESSAGE_INVALID",
                "Perptape WebSocket ordering metadata is invalid",
            )
        payload_value = value.get("d", {})
        if not isinstance(payload_value, dict):
            raise DomainRejected(
                "PERPTAPE_STREAM_MESSAGE_INVALID",
                "Perptape WebSocket payload is invalid",
            )
        event_id_value = payload_value.get("id") if event_type == "alert" else None
        event_id = None if event_id_value is None else str(event_id_value)
        if event_type == "alert" and not event_id:
            raise DomainRejected(
                "PERPTAPE_STREAM_MESSAGE_INVALID",
                "Perptape alert identity is invalid",
            )
        if event_type == "alert" and event_id is not None and len(event_id.encode("utf-8")) > 200:
            raise DomainRejected(
                "PERPTAPE_FIELD_TOO_LARGE",
                "Perptape alert identity exceeds the supported byte ceiling",
            )
        return StreamEnvelope(
            event_type=event_type,
            sequence=sequence,
            server_time=server_time,
            payload=payload_value,
            event_id=event_id,
        )

    def _message_order(
        self,
        envelope: StreamEnvelope,
        *,
        last_sequence: int | None,
        last_server_time: datetime | None,
    ) -> MessageOrder:
        if last_sequence is not None and envelope.sequence is not None:
            if envelope.sequence == last_sequence:
                return "DUPLICATE"
            if envelope.sequence < last_sequence:
                return "OUT_OF_ORDER"
            if envelope.sequence > last_sequence + 1:
                return "GAP"
        if last_server_time is not None:
            if envelope.server_time < last_server_time:
                return "OUT_OF_ORDER"
            if envelope.server_time - last_server_time > timedelta(
                seconds=self._heartbeat_timeout_seconds
            ):
                return "GAP"
        return "ACCEPT"

    @staticmethod
    def _needs_completion(payload: dict[str, Any]) -> bool:
        readiness = payload.get("kr")
        return (
            not isinstance(readiness, dict)
            or not readiness.get("status")
            or not isinstance(payload.get("cs"), str)
            or not payload.get("cs")
            or "th" not in payload
            or payload.get("u") is None
            or payload.get("vq24") is None
            or payload.get("oi") is None
        )

    @staticmethod
    def _same_alert(
        candidate: PerptapeCandidate,
        preliminary: PerptapeCandidate,
    ) -> bool:
        return candidate.candidate_id == preliminary.candidate_id or perptape_event_key(
            candidate
        ) == perptape_event_key(preliminary)

    def _remember_event(self, event_id: str) -> None:
        self._seen_event_ids.add(event_id)
        self._event_id_order.append(event_id)
        if len(self._event_id_order) > PERPTAPE_STREAM_WINDOW:
            expired = self._event_id_order.popleft()
            self._seen_event_ids.discard(expired)

    def _apply_alert(
        self,
        envelope: StreamEnvelope,
        *,
        allow_completion: bool,
        completion_confirmed: bool = False,
    ) -> None:
        assert envelope.event_id is not None
        if envelope.event_id in self._seen_event_ids:
            self.stats.duplicates_dropped += 1
            return
        current = self._current_snapshot()
        preliminary = self._client.parse_stream_alert(
            envelope.payload,
            event_time=envelope.server_time,
        )
        existing = None
        if current is not None:
            existing = next(
                (
                    candidate
                    for candidate in current.candidates
                    if self._same_alert(candidate, preliminary)
                ),
                None,
            )
        needs_completion = self._needs_completion(envelope.payload)
        if needs_completion and not completion_confirmed:
            self._register_completion_target(preliminary)
        existing_is_complete = (
            existing is not None
            and self._candidate_completes_target(existing, preliminary)
            and (not needs_completion or completion_confirmed)
        )
        if needs_completion and not existing_is_complete and allow_completion:
            if self._try_backfill():
                current = self._current_snapshot()
                if current is not None:
                    completed = next(
                        (
                            candidate
                            for candidate in current.candidates
                            if self._candidate_completes_target(
                                candidate,
                                preliminary,
                            )
                        ),
                        None,
                    )
                    if completed is not None:
                        self._remember_event(envelope.event_id)
                        self.stats.alerts_applied += 1
                        return
            current = self._current_snapshot()
            existing = None
            if current is not None:
                existing = next(
                    (
                        candidate
                        for candidate in current.candidates
                        if self._same_alert(candidate, preliminary)
                    ),
                    None,
                )
        candidate = self._client.parse_stream_alert(
            envelope.payload,
            event_time=envelope.server_time,
            existing=existing,
        )
        if needs_completion and not existing_is_complete:
            candidate = replace(candidate, data_health="DEGRADED", readiness="INCOMPLETE")
        current = self._current_snapshot()
        now = self._now()
        fetched_at = self._safe_record_time(now, current)
        candidates = {
            perptape_event_key(item): item
            for item in (() if current is None else current.candidates)
        }
        candidate_key = perptape_event_key(candidate)
        candidates.pop(candidate_key, None)
        candidates[candidate_key] = candidate
        generated_at = (
            envelope.server_time
            if current is None
            else max(current.generated_at, envelope.server_time)
        )
        self._record(
            PerptapeFeedSnapshot(
                contract_version=(
                    self._contract_version if current is None else current.contract_version
                ),
                generated_at=generated_at,
                fetched_at=fetched_at,
                next_allowed_at=max(
                    generated_at,
                    generated_at if current is None else current.next_allowed_at,
                ),
                candidates=tuple(candidates.values()),
            ),
            current,
        )
        self._remember_event(envelope.event_id)
        self.stats.alerts_applied += 1

    def _consume_connection(self, stop_event: StopEvent) -> None:
        if stop_event.is_set():
            return
        if self._api_key is None:
            raise DomainRejected(
                "PERPTAPE_NOT_CONFIGURED",
                "Perptape API key is not configured",
            )
        headers = {
            "authorization": f"Bearer {self._api_key}",
            "x-api-key": self._api_key,
        }
        config = json.dumps(
            {
                "type": "client_config",
                "payload": {
                    "priceType": "last",
                    "breakoutTimeframes": ["1h", "4h", "1d", "1w"],
                    "breakoutLookbacks": {"1h": 20, "4h": 20, "1d": 20, "1w": 20},
                },
            },
            separators=(",", ":"),
        )
        with self._connector(
            self._websocket_url,
            headers,
            self._timeout_seconds,
        ) as connection:
            if stop_event.is_set():
                return
            self.stats.connections += 1
            connection.send(config)
            opened_at = self._monotonic()
            last_received_at = opened_at
            reconcile_at = opened_at + self._reconciliation_delay()
            last_sequence: int | None = None
            last_server_time: datetime | None = None
            hello_received = False
            while not stop_event.is_set():
                success_generation = self._sync_remote_request_state()
                if self._pending_completion_targets and any(
                    success_generation > registered_success_generation
                    for (
                        _target,
                        registered_success_generation,
                    ) in self._pending_completion_targets.values()
                ):
                    shared = self._current_snapshot()
                    if shared is not None:
                        self._resolve_pending_targets(
                            shared,
                            success_generation=success_generation,
                        )
                if self.fatal_error_code == "PERPTAPE_RATE_LIMITED":
                    raise DomainRejected(
                        "PERPTAPE_RATE_LIMITED",
                        "Perptape exceeded the bounded rate-limit recovery sequence",
                    )
                current_time = self._monotonic()
                if self._completion_pending and (
                    self._backfill_not_before is None or self._now() >= self._backfill_not_before
                ):
                    self._try_backfill()
                    if stop_event.is_set():
                        return
                if current_time >= reconcile_at:
                    try:
                        self._reconcile(backfill=False)
                    except DomainRejected as exc:
                        if exc.code in PERPTAPE_FATAL_INPUT_ERROR_CODES:
                            self.fatal_error_code = exc.code
                            raise
                        self._mark_degraded()
                        if self.fatal_error_code == "PERPTAPE_RATE_LIMITED":
                            raise
                    reconcile_at = current_time + self._reconciliation_delay()
                    if stop_event.is_set():
                        return
                if current_time - last_received_at >= self._heartbeat_timeout_seconds:
                    raise DomainRejected(
                        "PERPTAPE_STREAM_STALE",
                        "Perptape WebSocket heartbeat timed out",
                    )
                try:
                    raw_message = connection.recv(timeout=1.0)
                except TimeoutError:
                    continue
                if stop_event.is_set():
                    return
                last_received_at = self._monotonic()
                envelope = self._parse_message(raw_message)
                self.stats.messages_received += 1
                if not hello_received:
                    if envelope.event_type != "hello":
                        raise DomainRejected(
                            "PERPTAPE_STREAM_PROTOCOL_ERROR",
                            "Perptape WebSocket did not begin with hello",
                        )
                    hello_received = True
                    last_sequence = envelope.sequence
                    last_server_time = envelope.server_time
                    continue
                if envelope.event_type == "hello":
                    raise DomainRejected(
                        "PERPTAPE_STREAM_PROTOCOL_ERROR",
                        "Perptape WebSocket sent a duplicate hello",
                    )
                order = self._message_order(
                    envelope,
                    last_sequence=last_sequence,
                    last_server_time=last_server_time,
                )
                if order == "DUPLICATE":
                    self.stats.duplicates_dropped += 1
                    continue
                if order == "OUT_OF_ORDER":
                    self.stats.gaps_detected += 1
                    self._try_backfill()
                    if stop_event.is_set():
                        return
                    continue
                gap_backfill_attempted = order == "GAP"
                gap_backfill_succeeded = False
                if gap_backfill_attempted:
                    self.stats.gaps_detected += 1
                    if envelope.event_type == "alert" and self._needs_completion(envelope.payload):
                        target = self._client.parse_stream_alert(
                            envelope.payload,
                            event_time=envelope.server_time,
                        )
                        self._register_completion_target(target)
                    gap_backfill_succeeded = self._try_backfill()
                    if stop_event.is_set():
                        return
                if envelope.sequence is not None:
                    last_sequence = envelope.sequence
                last_server_time = envelope.server_time
                if envelope.event_type in {"heartbeat", "alert"}:
                    self._connection_healthy = True
                if envelope.event_type == "alert":
                    self._apply_alert(
                        envelope,
                        allow_completion=not gap_backfill_attempted,
                        completion_confirmed=gap_backfill_succeeded,
                    )

    def run_forever(self, stop_event: StopEvent) -> None:
        attempts = 0
        startup_complete = False
        while not stop_event.is_set():
            self._connection_healthy = False
            error_code = "PERPTAPE_STREAM_UNAVAILABLE"
            retry_not_before: datetime | None = None
            try:
                if not startup_complete:
                    current = self._current_snapshot()
                    self._rebuild_pending_targets(current)
                    if self.fatal_error_code is not None:
                        raise DomainRejected(
                            self.fatal_error_code,
                            "Perptape unresolved alert window could not be restored",
                        )
                    if current is None or self._now() >= current.next_allowed_at:
                        self._reconcile(backfill=False)
                    else:
                        self._backfill_not_before = current.next_allowed_at
                        self._completion_pending = True
                    startup_complete = True
                    if stop_event.is_set():
                        return
                self._consume_connection(stop_event)
                return
            except DomainRejected as exc:
                error_code = exc.code
                if isinstance(exc, PerptapeRateLimited):
                    retry_not_before = exc.next_allowed_at
            except Exception:
                error_code = "PERPTAPE_STREAM_UNAVAILABLE"
            if stop_event.is_set():
                return
            if error_code in PERPTAPE_FATAL_INPUT_ERROR_CODES:
                self.fatal_error_code = error_code
                logger.error(
                    "Perptape WebSocket stopped after an invalid external payload",
                    extra={
                        "event": "perptape_stream_stopped",
                        "component": "perptape",
                        "error_code": error_code,
                    },
                )
                stop_event.set()
                return
            if self._stop_for_fatal_error(stop_event):
                return
            if error_code == "PERPTAPE_RATE_LIMITED":
                if retry_not_before is not None and self._now() < retry_not_before:
                    delay = (retry_not_before - self._now()).total_seconds()
                else:
                    rate_limit_attempts = max(
                        self._transport_consecutive_rate_limits,
                        self._target_consecutive_rate_limits,
                        1,
                    )
                    delay = min(
                        self._reconnect_initial_seconds * (2 ** (rate_limit_attempts - 1)),
                        self._reconnect_max_seconds,
                    )
                logger.warning(
                    "Perptape WebSocket reconnect scheduled",
                    extra={
                        "event": "perptape_stream_reconnect_scheduled",
                        "component": "perptape",
                        "error_code": error_code,
                        "attempt": max(
                            self._transport_consecutive_rate_limits,
                            self._target_consecutive_rate_limits,
                        ),
                        "delay_seconds": delay,
                    },
                )
                if stop_event.wait(delay):
                    return
                continue
            if startup_complete:
                try:
                    self._try_backfill()
                except DomainRejected:
                    if self._stop_for_fatal_error(stop_event):
                        return
                    raise
            else:
                self._mark_degraded()
            attempts = 1 if self._connection_healthy else attempts + 1
            non_retryable = error_code in {
                "PERPTAPE_AUTH_FAILED",
                "PERPTAPE_NOT_CONFIGURED",
            }
            if non_retryable or attempts >= self._max_reconnect_attempts:
                self._mark_degraded()
                self.fatal_error_code = error_code
                logger.error(
                    "Perptape WebSocket stopped after bounded failures",
                    extra={
                        "event": "perptape_stream_stopped",
                        "component": "perptape",
                        "error_code": error_code,
                        "attempt": attempts,
                    },
                )
                stop_event.set()
                return
            if retry_not_before is not None and self._now() < retry_not_before:
                delay = (retry_not_before - self._now()).total_seconds()
            else:
                delay = min(
                    self._reconnect_initial_seconds * (2 ** (attempts - 1)),
                    self._reconnect_max_seconds,
                )
            logger.warning(
                "Perptape WebSocket reconnect scheduled",
                extra={
                    "event": "perptape_stream_reconnect_scheduled",
                    "component": "perptape",
                    "error_code": error_code,
                    "attempt": attempts,
                    "delay_seconds": delay,
                },
            )
            if stop_event.wait(delay):
                return
