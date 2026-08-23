from __future__ import annotations

# FastAPI registers decorated local handlers dynamically.
# pyright: reportUnusedFunction=false
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .auth import valid_bearer
from .backend import ExecutionBackend, ExecutionNotFoundError, SandboxBackendError
from .docker_backend import DockerSandboxBackend
from .models import (
    AgentEvent,
    ExecutionCommand,
    ExecutionInfo,
    ResolveServerRequestCommand,
    RpcCommand,
)
from .settings import HostSettings

logger = logging.getLogger(__name__)


def create_app(
    settings: HostSettings | None = None,
    *,
    backend: ExecutionBackend | None = None,
) -> FastAPI:
    runtime_settings = settings or HostSettings()
    execution_backend = backend or DockerSandboxBackend(runtime_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        app.state.backend = execution_backend
        await execution_backend.startup()
        try:
            yield
        finally:
            await execution_backend.shutdown()

    app = FastAPI(title="Sandbox Agent Host", version="0.3.0", lifespan=lifespan)

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Any) -> Response:
        if request.url.path != "/healthz" and not valid_bearer(
            request.headers.get("authorization"), runtime_settings.api_key
        ):
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "Unauthorized", "code": "unauthorized"}},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    @app.exception_handler(ExecutionNotFoundError)
    async def not_found(_: Request, exc: ExecutionNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "message": str(exc),
                    "code": "agent_execution_not_found",
                }
            },
        )

    @app.exception_handler(SandboxBackendError)
    async def backend_error(_: Request, exc: SandboxBackendError) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(exc), "code": "sandbox_backend_error"}},
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/executions", response_model=ExecutionInfo, status_code=201)
    async def create_execution() -> ExecutionInfo:
        return await execution_backend.create()

    @app.get("/v1/executions/{execution_id}", response_model=ExecutionInfo)
    async def inspect_execution(execution_id: str) -> ExecutionInfo:
        return await execution_backend.inspect(execution_id)

    @app.post("/v1/executions/{execution_id}/commands")
    async def command(
        execution_id: str,
        body: ExecutionCommand,
    ) -> dict[str, Any] | ExecutionInfo:
        if isinstance(body, RpcCommand):
            result = await execution_backend.rpc(execution_id, body.method, body.params)
            return {"result": result}
        if isinstance(body, ResolveServerRequestCommand):
            await execution_backend.resolve_server_request(
                execution_id,
                body.request_id,
                result=body.result,
                error=body.error,
            )
            return {"accepted": True}
        return await execution_backend.terminate(execution_id)

    @app.get("/v1/executions/{execution_id}/events")
    async def events(
        execution_id: str,
        after: int = Query(default=-1, ge=-1),
        follow: bool = True,
    ) -> StreamingResponse:
        # Resolve 404/configuration failures before committing SSE headers.
        await execution_backend.inspect(execution_id)
        return StreamingResponse(
            _event_sse(execution_backend.events(execution_id, after=after, follow=follow)),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.delete("/v1/executions/{execution_id}", response_model=ExecutionInfo)
    async def terminate_execution(execution_id: str) -> ExecutionInfo:
        return await execution_backend.terminate(execution_id)

    return app


async def _event_sse(events: AsyncIterator[AgentEvent]) -> AsyncIterator[bytes]:
    async for event in events:
        data = json.dumps(event.model_dump(), ensure_ascii=False, separators=(",", ":"))
        yield f"id: {event.id}\nevent: {event.type}\ndata: {data}\n\n".encode()
