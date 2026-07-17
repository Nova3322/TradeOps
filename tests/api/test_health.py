from fastapi.testclient import TestClient

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


def test_liveness_does_not_claim_database_readiness() -> None:
    database = FakeDatabase(ready=False, error_code="DATABASE_UNAVAILABLE")

    with TestClient(create_app(settings(), database)) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "live"
    assert database.disposed is True


def test_readiness_requires_durable_store_and_control_gates() -> None:
    with TestClient(create_app(settings(), FakeDatabase())) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "durable_store": "postgresql"}


def test_readiness_fails_closed_with_stable_error_code() -> None:
    database = FakeDatabase(ready=False, error_code="CONTROL_GATES_MISSING")

    with TestClient(create_app(settings(), database)) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "status": "not_ready",
        "error_code": "CONTROL_GATES_MISSING",
    }


def test_metrics_endpoint_exposes_control_plane_metrics() -> None:
    with TestClient(create_app(settings(), FakeDatabase())) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    assert "trading_database_ready" in response.text
