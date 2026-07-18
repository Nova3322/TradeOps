from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from uuid import UUID


@dataclass(frozen=True)
class ProposalNotification:
    notification_id: str
    reviewer_id: UUID
    proposal_id: UUID
    proposal_version: int
    environment: str
    summary: str
    review_code: str
    review_url: str
    created_at: datetime


class MockTelegramGateway:
    """Deterministic M1 sink. It never contacts Telegram's network."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._notifications: list[ProposalNotification] = []

    def send(self, notification: ProposalNotification) -> None:
        with self._lock:
            self._notifications.append(notification)

    def notifications(self) -> list[ProposalNotification]:
        with self._lock:
            return list(self._notifications)
