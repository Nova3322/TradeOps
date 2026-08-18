from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast

from httpx import ASGITransport, AsyncClient

from trading_control_plane.api import create_app
from trading_control_plane.config import Settings
from trading_control_plane.database import Database
from trading_control_plane.queries import TradingQueries
from trading_control_plane.service import TradingService

EXPECTED_OPERATIONS = Path(__file__).with_name("openapi_operations.json")
HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "options", "head", "trace"})


class _ReadyDatabase:
    def is_ready(self) -> tuple[bool, None]:
        return True, None

    def dispose(self) -> None:
        pass


def _app():
    return create_app(
        Settings(
            environment="test",
            database_url="postgresql+psycopg://test:test@localhost/test",
            _env_file=None,
        ),
        _ReadyDatabase(),
    )


def test_openapi_operation_set_matches_the_pre_refactor_contract() -> None:
    schema = _app().openapi()
    actual = sorted(
        f"{method.upper()} {path}"
        for path, item in schema["paths"].items()
        for method in item
        if method.lower() in HTTP_METHODS
    )

    assert actual == json.loads(EXPECTED_OPERATIONS.read_text())


def test_legacy_direct_execution_routes_remain_absent() -> None:
    async def exercise() -> None:
        app = _app()
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                for path in (
                    "/api/venues/binance/sync",
                    "/api/venues/binance/execute",
                    "/api/venues/hyperliquid/sync",
                    "/api/venues/hyperliquid/execute",
                ):
                    assert (await client.post(path)).status_code == 404

    asyncio.run(exercise())


def test_service_and_query_facades_retain_critical_public_entries() -> None:
    database = cast(Database, object())
    service = TradingService(database)
    queries = TradingQueries(database)

    for name in (
        "bootstrap_admin",
        "create_proposal",
        "submit_proposal",
        "review_proposal",
        "decide_risk",
        "issue_authorization",
        "create_order_intent",
        "acquire_sender",
        "record_position",
        "record_account_equity",
        "create_transfer_proposal",
        "review_transfer_proposal",
        "issue_transfer_authorization",
    ):
        assert callable(getattr(service, name))
    for name in (
        "user_context",
        "exchange_accounts",
        "list_proposals",
        "campaign_detail",
        "runtime_snapshot",
        "capital_center",
        "audit_timeline",
    ):
        assert callable(getattr(queries, name))
