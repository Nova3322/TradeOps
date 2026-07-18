from __future__ import annotations

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

REQUIRED_SCHEMA_REVISION = "20260718_0032"


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
            autoflush=False,
            autobegin=False,
        )

    def is_ready(self) -> tuple[bool, str | None]:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                count = connection.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM capability_gates
                        WHERE capability_key IN (
                            'LIVE_ORDER_SEND', 'CAPITAL_TRANSFER', 'AUTO_ADD'
                        )
                        """
                    )
                ).scalar_one()
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()
            if count != 3:
                return False, "CONTROL_GATES_MISSING"
            if revision != REQUIRED_SCHEMA_REVISION:
                return False, "SCHEMA_REVISION_MISMATCH"
            return True, None
        except Exception:
            return False, "DATABASE_UNAVAILABLE"

    def dispose(self) -> None:
        self.engine.dispose()
