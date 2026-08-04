from __future__ import annotations

from datetime import UTC, datetime

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from trading_control_plane.config import get_settings
from trading_control_plane.database import Database
from trading_control_plane.domain import CapabilityStatus, PrincipalType, Role
from trading_control_plane.models import CapabilityGate, RoleAssignment, User
from trading_control_plane.service import TradingService

OWNER_USERNAME = "kelly_oooo"
PROPOSER_USERNAME = "local-proposer"
SECOND_REVIEWER_USERNAME = "local-reviewer-two"
PERPTAPE_USERNAME = "perptape"
RUNTIME_SYNC_USERNAME = "runtime-sync"


def _user_id(database: Database, username: str):
    with database.session_factory() as session:
        user = session.scalar(select(User).where(User.username == username))
        return None if user is None else user.user_id


def _ensure_user(
    database: Database,
    service: TradingService,
    username: str,
    owner_id,
    *,
    now: datetime,
):
    user_id = _user_id(database, username)
    return service.create_user(username, owner_id, now=now) if user_id is None else user_id


def _ensure_service_principal(
    database: Database,
    service: TradingService,
    username: str,
    owner_id,
    *,
    now: datetime,
):
    with database.session_factory() as session:
        user = session.scalar(select(User).where(User.username == username))
    if user is not None:
        if user.principal_type != PrincipalType.SERVICE.value or not user.active:
            raise RuntimeError(f"{username} must be an active service principal")
        return user.user_id
    return service.create_service_principal(username, owner_id, now=now)


def _ensure_role(
    database: Database,
    service: TradingService,
    user_id,
    role: Role,
    owner_id,
    *,
    now: datetime,
) -> None:
    with database.session_factory() as session:
        exists = session.scalar(
            select(RoleAssignment.assignment_id).where(
                RoleAssignment.user_id == user_id,
                RoleAssignment.role == role.value,
                RoleAssignment.account_scope.is_(None),
                RoleAssignment.venue_scope.is_(None),
            )
        )
    if exists is None:
        service.assign_role(user_id, role, owner_id, now=now)


def main() -> None:
    settings = get_settings()
    command.upgrade(Config("alembic.ini"), "head")
    database = Database(settings.database_url)
    service = TradingService(database)
    now = datetime.now(UTC)
    try:
        owner_id = _user_id(database, OWNER_USERNAME)
        if owner_id is None:
            owner_id = service.bootstrap_admin(OWNER_USERNAME, now=now)
        proposer_id = _ensure_user(
            database,
            service,
            PROPOSER_USERNAME,
            owner_id,
            now=now,
        )
        second_reviewer_id = _ensure_user(
            database,
            service,
            SECOND_REVIEWER_USERNAME,
            owner_id,
            now=now,
        )
        perptape_id = _ensure_service_principal(
            database,
            service,
            PERPTAPE_USERNAME,
            owner_id,
            now=now,
        )
        runtime_sync_id = _ensure_service_principal(
            database,
            service,
            RUNTIME_SYNC_USERNAME,
            owner_id,
            now=now,
        )
        for role in (Role.OBSERVER, Role.REVIEWER, Role.OPERATOR, Role.TREASURY_ADMIN):
            _ensure_role(database, service, owner_id, role, owner_id, now=now)
        _ensure_role(database, service, proposer_id, Role.PROPOSER, owner_id, now=now)
        _ensure_role(
            database,
            service,
            second_reviewer_id,
            Role.REVIEWER,
            owner_id,
            now=now,
        )
        _ensure_role(database, service, perptape_id, Role.PROPOSER, owner_id, now=now)
        for role in (Role.OPERATOR, Role.TREASURY_ADMIN):
            _ensure_role(database, service, runtime_sync_id, role, owner_id, now=now)
        with database.session_factory() as session:
            enabled_gates = session.scalars(
                select(CapabilityGate).where(
                    CapabilityGate.status != CapabilityStatus.DISABLED.value
                )
            ).all()
        for gate in enabled_gates:
            service.set_capability_gate(
                gate.capability_key,
                CapabilityStatus.DISABLED,
                "local console startup forces every dangerous gate closed",
                owner_id,
                now=now,
            )
    finally:
        database.dispose()
    print("Local database and internal human/service principals are ready.")


if __name__ == "__main__":
    main()
