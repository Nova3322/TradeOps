from __future__ import annotations

from trading_control_plane.api_core import (
    CONTENT_TYPE_LATEST,
    DATABASE_READY,
    Any,
    HTTPException,
    Response,
    __version__,
    generate_latest,
    logger,
    status,
)
from trading_control_plane.api_routes.context import ApiRouteContext


def register_system_routes(context: ApiRouteContext) -> None:
    """Register system routes against one application dependency context."""

    app = context.app
    resolved_database = context.require("resolved_database")

    @app.get("/health/live", tags=["health"])
    def liveness() -> dict[str, str]:
        return {"status": "live", "version": __version__}

    @app.get("/health/ready", tags=["health"])
    def readiness() -> dict[str, Any]:
        ready, error_code = resolved_database.is_ready()
        DATABASE_READY.set(1 if ready else 0)
        if not ready:
            logger.warning(
                "durable trading core is not ready",
                extra={
                    "event": "readiness_failed",
                    "error_code": error_code or "READINESS_FAILED",
                    "component": "database",
                },
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "status": "not_ready",
                    "error_code": error_code or "READINESS_FAILED",
                },
            )
        return {"status": "ready", "durable_store": "postgresql"}

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
