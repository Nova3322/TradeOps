import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from trading_control_plane.api import (
    _perptape_runtime_status,
    _perptape_transport_status,
    create_app,
)
from trading_control_plane.config import Settings


class FakeDatabase:
    def __init__(self, ready: bool = True, error_code: str | None = None) -> None:
        self.ready = ready
        self.error_code = error_code
        self.disposed = False

    def is_ready(self) -> tuple[bool, str | None]:
        return self.ready, self.error_code

    def dispose(self) -> None:
        self.disposed = True


def settings() -> Settings:
    return Settings(
        environment="test",
        database_url="postgresql+psycopg://test:test@localhost/test",
        _env_file=None,
    )


async def async_get(app: FastAPI, path: str) -> Response:
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get(path)


def get(app: FastAPI, path: str) -> Response:
    return asyncio.run(async_get(app, path))


def test_liveness_does_not_claim_database_readiness() -> None:
    database = FakeDatabase(ready=False, error_code="DATABASE_UNAVAILABLE")

    response = get(create_app(settings(), database), "/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "live"
    assert database.disposed is True


def test_readiness_requires_durable_store_and_control_gates() -> None:
    response = get(create_app(settings(), FakeDatabase()), "/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "durable_store": "postgresql"}


def test_readiness_fails_closed_with_stable_error_code() -> None:
    database = FakeDatabase(ready=False, error_code="CONTROL_GATES_MISSING")

    response = get(create_app(settings(), database), "/health/ready")

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "status": "not_ready",
        "error_code": "CONTROL_GATES_MISSING",
    }


def test_metrics_endpoint_exposes_control_plane_metrics() -> None:
    response = get(create_app(settings(), FakeDatabase()), "/metrics")

    assert response.status_code == 200
    assert "trading_database_ready" in response.text


def test_perptape_runtime_status_distinguishes_configuration_and_freshness() -> None:
    now = datetime.now(UTC)
    empty_feed = {
        "available": False,
        "contract_version": None,
        "fetched_at": None,
    }
    assert _perptape_runtime_status(settings(), empty_feed, now=now) == "NOT_CONFIGURED"

    on_demand = settings().model_copy(update={"perptape_api_key": "configured"})
    assert _perptape_runtime_status(on_demand, empty_feed, now=now) == "ON_DEMAND"
    continuous = on_demand.model_copy(update={"runtime_sync_enabled": True})
    assert _perptape_runtime_status(continuous, empty_feed, now=now) == "WAITING"

    fresh_feed = {
        "available": True,
        "contract_version": "breakouts-v1",
        "fetched_at": (now - timedelta(seconds=10)).isoformat(),
    }
    assert _perptape_runtime_status(continuous, fresh_feed, now=now) == "SUCCESS"
    stale_feed = {**fresh_feed, "fetched_at": (now - timedelta(minutes=3)).isoformat()}
    assert _perptape_runtime_status(continuous, stale_feed, now=now) == "STALE"
    mismatched_feed = {**fresh_feed, "contract_version": "breakouts-v0"}
    assert _perptape_runtime_status(continuous, mismatched_feed, now=now) == "STALE"


def test_perptape_transport_status_distinguishes_live_stream_and_polling_fallback() -> None:
    now = datetime.now(UTC)
    configured = settings().model_copy(update={"runtime_sync_enabled": True})
    polling = {
        "status": "SUCCESS",
        "checked_at": (now - timedelta(seconds=10)).isoformat(),
        "error_code": None,
    }
    live = _perptape_transport_status(
        configured,
        {
            "PERPTAPE": polling,
            "PERPTAPE_WEBSOCKET": {
                "status": "SUCCESS",
                "checked_at": (now - timedelta(seconds=5)).isoformat(),
                "error_code": None,
            },
        },
        now=now,
    )
    assert live["state"] == "WEBSOCKET_LIVE"
    assert live["primary_channel"] == "WEBSOCKET"
    assert live["fallback_active"] is False

    fallback = _perptape_transport_status(
        configured,
        {
            "PERPTAPE": polling,
            "PERPTAPE_WEBSOCKET": {
                "status": "FAILED",
                "checked_at": now.isoformat(),
                "error_code": "PERPTAPE_AUTH_FAILED",
            },
        },
        now=now,
    )
    assert fallback["state"] == "POLLING_FALLBACK"
    assert fallback["primary_channel"] == "HTTPS_POLLING"
    assert fallback["fallback_active"] is True
    assert fallback["error_code"] == "PERPTAPE_AUTH_FAILED"

    stale = _perptape_transport_status(
        configured,
        {
            "PERPTAPE_WEBSOCKET": {
                "status": "SUCCESS",
                "checked_at": (now - timedelta(hours=1)).isoformat(),
                "error_code": None,
            }
        },
        now=now,
    )
    assert stale["state"] == "WEBSOCKET_FAILED"
    assert stale["error_code"] == "PERPTAPE_WEBSOCKET_HEALTH_STALE"


def test_venue_account_detail_route_serves_the_spa_shell() -> None:
    app = create_app(settings(), FakeDatabase(ready=False))

    response = get(app, "/venues/binance-main")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_account_assets_use_explicit_account_id_inputs() -> None:
    app = create_app(settings(), FakeDatabase(ready=False))

    capital = get(app, "/assets/capital.js")
    accounts = get(app, "/assets/execution.js")

    assert capital.status_code == 200
    assert "const exchangeAccountInput" in capital.text
    assert '<input name="${name}"' in capital.text
    assert "exchangeAccountSelect" not in capital.text
    assert accounts.status_code == 200
    assert "账户 ID（创建后不可修改）" in accounts.text  # noqa: RUF001
    assert "同时填写精确账户 ID 和账户名称" in accounts.text


def test_team_mode_switch_is_in_the_header_and_legacy_page_is_removed() -> None:
    app = create_app(settings(), FakeDatabase(ready=False))

    shell = get(app, "/")
    execution = get(app, "/assets/execution.js")
    router = get(app, "/assets/router.js")

    assert shell.status_code == 200
    assert 'id="environment-badge"' in shell.text
    assert 'aria-haspopup="listbox"' in shell.text
    assert 'id="team-mode-menu"' in shell.text
    assert 'id="team-mode-dialog"' not in shell.text
    assert 'href="/team-settings"' not in shell.text
    assert execution.status_code == 200
    assert "async function openTeamModeDropdown" in execution.text
    assert "function initializeTeamModeDropdown()" in execution.text
    assert "I_CONFIRM_LIVE_PRODUCTION_MONEY" in execution.text
    assert "openTeamModeDialog" not in execution.text
    assert "team-mode-switch-form" not in execution.text
    assert "renderTeamSettings" not in execution.text
    assert router.status_code == 200
    assert "path === '/team-settings' || path === '/trading-mode'" in router.text
    assert "history.replaceState({}, '', '/home')" in router.text


def test_mock_login_is_not_available_unless_explicitly_enabled() -> None:
    async def post() -> Response:
        app = create_app(settings(), FakeDatabase())
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                return await client.post("/api/auth/mock/login", json={"username": "admin"})

    response = asyncio.run(post())

    assert response.status_code == 404
