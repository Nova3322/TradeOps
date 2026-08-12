from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from trading_control_plane.database import Database
from trading_control_plane.domain import DomainRejected
from trading_control_plane.models import AuditEvent
from trading_control_plane.queries import TradingQueries
from trading_control_plane.service import TradingService

pytestmark = pytest.mark.integration


def test_private_chat_binding_is_idempotent_audited_and_cannot_be_reassigned(
    database: Database,
    service: TradingService,
) -> None:
    now = datetime.now(UTC)
    owner_id = service.bootstrap_admin("telegram-owner", now=now)
    other_id = service.create_user("other-user", owner_id, now=now)

    assert (
        service.bind_telegram_private_chat(
            internal_username="telegram-owner",
            telegram_username="telegram-owner",
            telegram_chat_id="789",
            now=now,
        )
        == "telegram-owner"
    )
    assert (
        service.bind_telegram_private_chat(
            internal_username="telegram-owner",
            telegram_username="telegram-owner",
            telegram_chat_id="789",
            now=now,
        )
        == "telegram-owner"
    )
    assert TradingQueries(database).telegram_chat_id(owner_id) == "789"
    assert TradingQueries(database).telegram_chat_id(other_id) is None

    with pytest.raises(DomainRejected, match="already has another"):
        service.bind_telegram_private_chat(
            internal_username="telegram-owner",
            telegram_username="telegram-owner",
            telegram_chat_id="999",
            now=now,
        )
    with pytest.raises(DomainRejected, match="already bound to another"):
        service.bind_telegram_private_chat(
            internal_username="other-user",
            telegram_username="other_user",
            telegram_chat_id="789",
            now=now,
        )

    with database.session_factory() as session:
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "TELEGRAM_PRIVATE_CHAT_BOUND")
        ).all()
    assert len(events) == 1
    assert events[0].actor_id == str(owner_id)
