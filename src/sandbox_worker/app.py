from __future__ import annotations

# FastAPI registers decorated local handlers dynamically.
# pyright: reportUnusedFunction=false
import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .auth import valid_bearer
from .codex_protocol import CodexAppServerSession, CodexProtocolError, RequestId
from .event_log import EventLog
from .models import AgentEvent, ResolveServerRequestBody, RpcRequest
from .settings import WorkerSettings

logger = logging.getLogger(__name__)

_MCP_APP_ELICITATION_METHOD = "mcpServer/elicitation/request"


class WorkerRuntime:
    """Own one initialized app-server process for the sandbox lifetime."""

    def __init__(self, settings: WorkerSettings) -> None:
        self.settings = settings
        self.events = EventLog(settings.max_event_history)
        self._session = CodexAppServerSession(
            settings, server_request_handler=self._handle_server_request
        )
        self._pump_task: asyncio.Task[None] | None = None
        self._pending_server_requests: dict[str, asyncio.Future[Any]] = {}
        self._closed = False

    async def start(self) -> None:
        await self._session.start()
        self._pump_task = asyncio.create_task(
            self._pump_notifications(), name="sandbox-worker-notifications"
        )

    async def rpc(self, method: str, params: dict[str, Any]) -> Any:
        if self._closed:
            raise CodexProtocolError("Sandbox worker is closed")
        result = await self._session.request(method, params)
        # Let the stdout pump consume notifications already written beside the RPC result.
        await asyncio.sleep(0)
        return result

    async def resolve_server_request(
        self,
        request_id: str,
        *,
        result: Any | None,
        error: dict[str, Any] | None,
    ) -> None:
        future = self._pending_server_requests.get(request_id)
        if future is None or future.done():
            raise CodexProtocolError(f"Unknown or completed app-server request: {request_id}")
        if error is not None:
            message = error.get("message")
            future.set_exception(
                CodexProtocolError(str(message or "App-server request was rejected"), details=error)
            )
        else:
            future.set_result(result)

    async def event_stream(self, *, after: int, follow: bool) -> AsyncIterator[AgentEvent | None]:
        cursor = after
        if not follow:
            # An RPC response and its adjacent notifications can be scheduled on
            # consecutive event-loop ticks. Capture that already-buffered burst.
            await asyncio.sleep(0.01)
        while True:
            events, closed = await self.events.read(
                after=cursor,
                wait_seconds=(self.settings.event_keepalive_seconds if follow else None),
            )
            for event in events:
                cursor = event.id
                yield event
            if closed or not follow:
                return
            if not events:
                yield None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for future in self._pending_server_requests.values():
            if not future.done():
                future.set_exception(CodexProtocolError("Sandbox worker is closing"))
        self._pending_server_requests.clear()
        if self._pump_task is not None and not self._pump_task.done():
            self._pump_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._pump_task
        await self._session.close()
        await self.events.close()

    async def _pump_notifications(self) -> None:
        try:
            while True:
                notification = await self._session.next_notification()
                await self.events.publish("notification", notification)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Sandbox worker app-server notification pump failed")
            await self.events.publish(
                "session_failed",
                {"message": _safe_message(exc)},
            )
            await self.events.close()

    async def _handle_server_request(
        self,
        request_id: RequestId,
        method: str,
        params: dict[str, Any],
    ) -> Any:
        if method != _MCP_APP_ELICITATION_METHOD:
            raise CodexProtocolError(f"Interactive app-server request is disabled: {method}")
        token = str(request_id)
        if token in self._pending_server_requests:
            raise CodexProtocolError(f"Duplicate app-server request id: {token}")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending_server_requests[token] = future
        await self.events.publish(
            "server_request",
            {
                "request_id": token,
                "method": method,
                "params": params,
            },
        )
        try:
            return await asyncio.wait_for(
                asyncio.shield(future), timeout=self.settings.request_timeout_seconds
            )
        except TimeoutError as exc:
            raise CodexProtocolError(f"App-server request timed out: {method}") from exc
        finally:
            self._pending_server_requests.pop(token, None)


def create_worker_app(
    settings: WorkerSettings | None = None,
    *,
    runtime: WorkerRuntime | None = None,
) -> FastAPI:
    runtime_settings = settings or WorkerSettings()
    worker_runtime = runtime or WorkerRuntime(runtime_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        app.state.runtime = worker_runtime
        await worker_runtime.start()
        try:
            yield
        finally:
            await worker_runtime.close()

    app = FastAPI(
        title="Sandbox Worker",
        version="0.3.0",
        lifespan=lifespan,
    )

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

    @app.exception_handler(CodexProtocolError)
    async def protocol_error_handler(_: Request, exc: CodexProtocolError) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "message": exc.message,
                    "code": "codex_app_server_error",
                }
            },
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"status": "ok", "last_event_id": worker_runtime.events.last_event_id}

    @app.post("/v1/rpc")
    async def rpc(body: RpcRequest) -> dict[str, Any]:
        return {"result": await worker_runtime.rpc(body.method, body.params)}

    @app.get("/v1/events")
    async def events(
        after: int = Query(default=-1, ge=-1),
        follow: bool = True,
    ) -> StreamingResponse:
        return StreamingResponse(
            _event_sse(worker_runtime.event_stream(after=after, follow=follow)),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/v1/server-requests/{request_id}/resolve", status_code=204)
    async def resolve_server_request(request_id: str, body: ResolveServerRequestBody) -> Response:
        await worker_runtime.resolve_server_request(
            request_id, result=body.result, error=body.error
        )
        return Response(status_code=204)

    return app


async def _event_sse(events: AsyncIterator[AgentEvent | None]) -> AsyncIterator[bytes]:
    async for event in events:
        if event is None:
            yield b": keep-alive\n\n"
            continue
        data = json.dumps(event.model_dump(), ensure_ascii=False, separators=(",", ":"))
        yield f"id: {event.id}\nevent: {event.type}\ndata: {data}\n\n".encode()


def _safe_message(exc: Exception) -> str:
    if isinstance(exc, CodexProtocolError):
        return exc.message
    return "Codex app-server session failed"
