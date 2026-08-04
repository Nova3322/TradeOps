from __future__ import annotations

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

REQUIRED_SCHEMA_REVISION = "20260802_0011"


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, database_url: str) -> None:
        self.engine: Engine = create_engine(
            database_url,
            pool_pre_ping=True,
            future=True,
        )
        self.session_factory: sessionmaker[Session] = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
            autoflush=True,
        )

    def is_ready(self) -> tuple[bool, str | None]:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                gate_rows = {
                    row[0]: row[1]
                    for row in connection.execute(
                        text("SELECT capability_key, status FROM capability_gates")
                    )
                }
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()
            if revision != REQUIRED_SCHEMA_REVISION:
                return False, "SCHEMA_REVISION_MISMATCH"
            if set(gate_rows) != {
                "LIVE_ORDER_SEND",
                "CAPITAL_TRANSFER",
                "AUTO_ADD",
                "AUTO_PROFIT_SWEEP",
                "AUTO_OPERATING_REFILL",
            }:
                return False, "CONTROL_GATES_INVALID"
            if any(value not in {"DISABLED", "ENABLED"} for value in gate_rows.values()):
                return False, "CONTROL_GATES_INVALID"
            return True, None
        except Exception:
            return False, "DATABASE_UNAVAILABLE"

    def dispose(self) -> None:
        self.engine.dispose()
