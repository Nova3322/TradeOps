import asyncio

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from trading_control_plane.api import create_app
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


def test_web_shell_is_served_without_claiming_business_readiness() -> None:
    response = get(create_app(settings(), FakeDatabase(ready=False)), "/")

    assert response.status_code == 200
    assert "Trading Console" in response.text


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
