from __future__ import annotations

import hmac
from collections.abc import Mapping
from datetime import timedelta
from typing import Literal

import anyio
from fastapi import FastAPI, Header, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from trading_control_plane.adapters.facts import (
    Environment,
    FactAdapterRegistry,
    FactAdapterScope,
    Venue,
)
from trading_control_plane.domain import DomainRejected


def _bearer_token(value: str | None) -> str:
    if value is None:
        return ""
    scheme, separator, token = value.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return ""
    return token


def _scope_key(
    workspace_id: str,
    team_id: str,
    account_id: str,
    venue: Venue,
    environment: Environment,
) -> str:
    return FactAdapterScope(
        workspace_id=workspace_id,
        team_id=team_id,
        account_id=account_id,
        venue=venue,
        environment=environment,
        symbols=("SCOPE_LOOKUP",),
    ).key


def create_fact_adapter_app(
    *,
    registry: FactAdapterRegistry,
    bearer_token: str,
    stale_after_seconds: int = 90,
) -> FastAPI:
    """Expose the isolated fact contract without exposing exchange credentials."""

    if len(bearer_token) < 32:
        raise ValueError("fact adapter bearer token must contain at least 32 characters")
    if not 15 <= stale_after_seconds <= 900:
        raise ValueError("fact adapter stale threshold is outside its bounded range")

    app = FastAPI(title="TradingOPS Fact Adapter", docs_url=None, redoc_url=None)

    def authorize(value: str | None) -> None:
        if not hmac.compare_digest(_bearer_token(value), bearer_token):
            raise DomainRejected(
                "FACT_ADAPTER_UNAUTHORIZED",
                "the fact adapter request is not authorized",
            )

    @app.exception_handler(DomainRejected)
    async def domain_error(_request: object, exc: DomainRejected) -> JSONResponse:
        status = 401 if exc.code == "FACT_ADAPTER_UNAUTHORIZED" else 409
        return JSONResponse(
            status_code=status,
            content={"error": {"code": exc.code, "detail": exc.detail}},
        )

    @app.get("/health/ready")
    async def health_ready() -> JSONResponse:
        health = await registry.health(
            stale_after=timedelta(seconds=stale_after_seconds)
        )
        return JSONResponse(
            status_code=200 if health["status"] == "ready" else 503,
            content=health,
        )

    @app.get("/facts/snapshot")
    async def fact_snapshot(
        workspace_id: str = Query(min_length=1, max_length=160),
        team_id: str = Query(min_length=1, max_length=160),
        account_id: str = Query(min_length=1, max_length=160),
        venue: Literal["BINANCE", "HYPERLIQUID", "OKX", "BYBIT"] = Query(),
        environment: Literal["TESTNET", "LIVE"] = Query(),
        authorization: str | None = Header(default=None),
    ) -> Mapping[str, object]:
        authorize(authorization)
        key = _scope_key(
            workspace_id,
            team_id,
            account_id,
            venue,
            environment,
        )
        snapshot = await registry.latest(
            key,
            stale_after=timedelta(seconds=stale_after_seconds),
        )
        return snapshot.to_dict()

    @app.websocket("/facts/ws")
    async def fact_stream(
        websocket: WebSocket,
        workspace_id: str = Query(min_length=1, max_length=160),
        team_id: str = Query(min_length=1, max_length=160),
        account_id: str = Query(min_length=1, max_length=160),
        venue: Literal["BINANCE", "HYPERLIQUID", "OKX", "BYBIT"] = Query(),
        environment: Literal["TESTNET", "LIVE"] = Query(),
        after_sequence: int | None = Query(default=None, ge=0),
        after_stream_id: str | None = Query(default=None, min_length=1, max_length=160),
    ) -> None:
        try:
            authorize(websocket.headers.get("authorization"))
        except DomainRejected:
            await websocket.close(code=4401, reason="fact adapter authorization failed")
            return
        key = _scope_key(
            workspace_id,
            team_id,
            account_id,
            venue,
            environment,
        )
        try:
            await registry.scope(key)
        except DomainRejected:
            await websocket.close(code=4404, reason="fact adapter scope not found")
            return
        await websocket.accept()

        async def send_events() -> None:
            first = True
            async for event in registry.subscribe(key):
                message = event.to_dict()
                if first:
                    first = False
                    status = "CURRENT_SNAPSHOT"
                    if after_stream_id is not None and after_stream_id != event.stream_id:
                        status = "STREAM_RESET_COMPENSATED"
                    elif after_sequence is not None and after_sequence > event.sequence:
                        status = "STREAM_RESET_COMPENSATED"
                    elif (
                        after_sequence is not None
                        and after_sequence + 1 < event.sequence
                    ):
                        status = "SEQUENCE_GAP_COMPENSATED"
                    message["resume"] = {
                        "status": status,
                        "requested_after_sequence": after_sequence,
                        "requested_stream_id": after_stream_id,
                    }
                await websocket.send_json(message)

        async def wait_for_disconnect(task_group: anyio.abc.TaskGroup) -> None:
            while True:
                frame = await websocket.receive()
                if frame["type"] == "websocket.disconnect":
                    task_group.cancel_scope.cancel()
                    return

        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(send_events)
                task_group.start_soon(wait_for_disconnect, task_group)
        except WebSocketDisconnect:
            return

    return app


__all__ = ["create_fact_adapter_app"]
