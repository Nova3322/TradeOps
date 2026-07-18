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


@dataclass(frozen=True)
class CampaignNotification:
    notification_id: str
    recipient_id: UUID
    campaign_id: UUID
    event_type: str
    environment: str
    summary: str
    campaign_version: int
    action_references: tuple[tuple[str, str], ...]
    created_at: datetime


class MockTelegramGateway:
    """Deterministic nonproduction sink. It never contacts Telegram's network."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._notifications: list[ProposalNotification] = []
        self._campaign_notifications: list[CampaignNotification] = []

    def send(self, notification: ProposalNotification) -> None:
        with self._lock:
            if all(
                item.notification_id != notification.notification_id for item in self._notifications
            ):
                self._notifications.append(notification)

    def notifications(self) -> list[ProposalNotification]:
        with self._lock:
            return list(self._notifications)

    def send_campaign(self, notification: CampaignNotification) -> None:
        with self._lock:
            if all(
                item.notification_id != notification.notification_id
                for item in self._campaign_notifications
            ):
                self._campaign_notifications.append(notification)

    def campaign_notifications(self) -> list[CampaignNotification]:
        with self._lock:
            return list(self._campaign_notifications)
