from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from trading_control_plane.commands import hash_json
from trading_control_plane.models import InboxReceipt

InboxHandler = Callable[[Session], None]


class InboxPayloadConflict(RuntimeError):
    """A message identity was reused with different payload semantics."""


class IdempotentInboxProcessor:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def process_once(
        self,
        *,
        consumer_name: str,
        message_id: UUID,
        payload: object,
        handler: InboxHandler,
    ) -> bool:
        """Return True only when this transaction executed the handler."""

        with self._session_factory.begin() as session:
            payload_hash = hash_json(payload)
            claim = (
                insert(InboxReceipt)
                .values(
                    receipt_id=uuid4(),
                    consumer_name=consumer_name,
                    message_id=message_id,
                    payload_hash=payload_hash,
                    processed_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing(index_elements=("consumer_name", "message_id"))
                .returning(InboxReceipt.receipt_id)
            )
            claimed = session.execute(claim).scalar_one_or_none()
            if claimed is None:
                existing_hash = session.execute(
                    select(InboxReceipt.payload_hash).where(
                        InboxReceipt.consumer_name == consumer_name,
                        InboxReceipt.message_id == message_id,
                    )
                ).scalar_one()
                if existing_hash != payload_hash:
                    raise InboxPayloadConflict(
                        "message identity was reused with different payload semantics"
                    )
                return False
            handler(session)
            return True
