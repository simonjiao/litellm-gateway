from __future__ import annotations

# FastAPI registers decorated local handlers dynamically.
# pyright: reportUnusedFunction=false
import asyncio
import json
import logging
import secrets
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError

from .agent_host import AgentExecutionClient, HttpAgentExecutionClient
from .csp import render_mcp_resource
from .errors import AdapterError
from .mcp_apps import McpAppsState, McpAppToolCallRequest, ResolveInteractionRequest
from .models import CreateResponseRequest
from .service import CodexResponsesService
from .settings import Settings
from .store import ResponseStore

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    agent_host: AgentExecutionClient | None = None,
) -> FastAPI:
    runtime_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        app.state.settings = runtime_settings
        app.state.store = ResponseStore()
        app.state.mcp_apps = McpAppsState(
            max_event_history=runtime_settings.mcp_apps_max_event_history
        )
        app.state.agent_host = agent_host or HttpAgentExecutionClient(
            runtime_settings.agent_host_base_url,
            runtime_settings.agent_host_api_key,
            timeout_seconds=runtime_settings.request_timeout_seconds,
        )
        app.state.service = CodexResponsesService(
            runtime_settings,
            app.state.store,
            app.state.mcp_apps,
            app.state.agent_host,
        )
        try:
            yield
        finally:
            await app.state.service.shutdown()

    app = FastAPI(
        title="Codex app-server Responses Adapter",
        version="0.3.0",
        lifespan=lifespan,
    )
    if runtime_settings.mcp_apps_enabled:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=runtime_settings.allowed_origins(),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
        )

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Any) -> Response:
        if request.url.path == "/healthz":
            return await call_next(request)
        authorization = request.headers.get("authorization")
        if authorization is None or not authorization.startswith("Bearer "):
            return _unauthorized()
        if not secrets.compare_digest(
            authorization.removeprefix("Bearer ").encode(),
            runtime_settings.api_key.encode(),
        ):
            return _unauthorized()
        return await call_next(request)

    @app.exception_handler(AdapterError)
    async def adapter_error_handler(_: Request, exc: AdapterError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.envelope())

    @app.exception_handler(ValidationError)
    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _: Request, exc: ValidationError | RequestValidationError
    ) -> JSONResponse:
        error = {
            "error": {
                "message": "Invalid request",
                "type": "invalid_request_error",
                "param": None,
                "code": "validation_error",
                "details": exc.errors(),
            }
        }
        return JSONResponse(status_code=400, content=error)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled adapter error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": "Internal adapter error",
                    "type": "server_error",
                    "param": None,
                    "code": "internal_error",
                }
            },
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "mcp_apps": runtime_settings.mcp_apps_enabled,
        }

    @app.post("/v1/responses")
    async def create_response(request: Request) -> Any:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AdapterError(
                "Request body must be valid JSON",
                status_code=400,
                error_type="invalid_request_error",
                code="invalid_json",
            ) from exc
        parsed = CreateResponseRequest.model_validate(body)
        service: CodexResponsesService = request.app.state.service
        if parsed.stream:
            events = await service.create_streaming(parsed)
            return StreamingResponse(
                _sse_stream(
                    events,
                    keepalive_seconds=runtime_settings.mcp_apps_event_keepalive_seconds,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        return await service.create_non_streaming(parsed)

    @app.get("/v1/responses/{response_id}")
    async def retrieve_response(response_id: str, request: Request) -> dict[str, Any]:
        service: CodexResponsesService = request.app.state.service
        return await service.retrieve(response_id)

    @app.post("/v1/responses/{response_id}/cancel")
    async def cancel_response(response_id: str, request: Request) -> dict[str, Any]:
        service: CodexResponsesService = request.app.state.service
        return await service.cancel(response_id)

    @app.delete("/v1/responses/{response_id}")
    async def delete_response(response_id: str, request: Request) -> dict[str, Any]:
        service: CodexResponsesService = request.app.state.service
        return await service.delete(response_id)

    @app.get("/v1/responses/{response_id}/input_items")
    async def list_input_items(response_id: str, request: Request) -> dict[str, Any]:
        service: CodexResponsesService = request.app.state.service
        return await service.list_input_items(response_id)

    @app.get("/v1/mcp-apps/responses/{response_id}/state")
    async def mcp_app_state(response_id: str, request: Request) -> dict[str, Any]:
        service: CodexResponsesService = request.app.state.service
        return await service.app_state(response_id)

    @app.get("/v1/mcp-apps/responses/{response_id}/events")
    async def mcp_app_events(
        response_id: str,
        request: Request,
        after: int = Query(default=-1, ge=-1),
    ) -> StreamingResponse:
        last_event_id = request.headers.get("last-event-id")
        if after < 0 and last_event_id is not None:
            with suppress(ValueError):
                after = int(last_event_id)
        service: CodexResponsesService = request.app.state.service
        events = service.app_events(response_id, after=after)
        return StreamingResponse(
            _mcp_app_sse_stream(events),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/v1/mcp-apps/interactions/{interaction_id}")
    async def get_mcp_app_interaction(
        interaction_id: str,
        request: Request,
    ) -> dict[str, Any]:
        service: CodexResponsesService = request.app.state.service
        return await service.get_interaction(interaction_id)

    @app.post("/v1/mcp-apps/interactions/{interaction_id}/resolve")
    async def resolve_mcp_app_interaction(
        interaction_id: str,
        body: ResolveInteractionRequest,
        request: Request,
    ) -> dict[str, Any]:
        service: CodexResponsesService = request.app.state.service
        return await service.resolve_interaction(interaction_id, body)

    @app.get("/v1/mcp-apps/responses/{response_id}/resources")
    async def read_mcp_app_resource(
        response_id: str,
        request: Request,
        server: str,
        uri: str,
        origin_call_id: str | None = None,
        connector_id: str | None = None,
        response_format: Literal["html", "json"] = Query(default="html", alias="format"),
    ) -> Response:
        service: CodexResponsesService = request.app.state.service
        result = await service.read_mcp_resource(
            response_id,
            server=server,
            uri=uri,
            origin_call_id=origin_call_id,
            connector_id=connector_id,
        )
        if response_format == "json":
            return JSONResponse(content=result)
        return render_mcp_resource(
            result,
            requested_uri=uri,
            max_bytes=runtime_settings.mcp_apps_resource_max_bytes,
        )

    @app.post("/v1/mcp-apps/responses/{response_id}/tools/call")
    async def call_mcp_app_tool(
        response_id: str,
        body: McpAppToolCallRequest,
        request: Request,
    ) -> dict[str, Any]:
        service: CodexResponsesService = request.app.state.service
        return await service.call_mcp_tool(response_id, body)

    return app


async def _sse_stream(
    events: AsyncIterator[dict[str, Any]],
    *,
    keepalive_seconds: float,
) -> AsyncIterator[bytes]:
    iterator = events.__aiter__()
    next_event = asyncio.create_task(_next_response_event(iterator))
    try:
        while True:
            done, _ = await asyncio.wait({next_event}, timeout=keepalive_seconds)
            if not done:
                yield b": keep-alive\n\n"
                continue
            try:
                event = next_event.result()
            except StopAsyncIteration:
                return
            event_type = str(event.get("type") or "message")
            data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            yield f"event: {event_type}\ndata: {data}\n\n".encode()
            next_event = asyncio.create_task(_next_response_event(iterator))
    finally:
        if not next_event.done():
            next_event.cancel()
            with suppress(asyncio.CancelledError):
                await next_event


async def _next_response_event(
    iterator: AsyncIterator[dict[str, Any]],
) -> dict[str, Any]:
    return await anext(iterator)


async def _mcp_app_sse_stream(
    events: AsyncIterator[dict[str, Any] | None],
) -> AsyncIterator[bytes]:
    async for event in events:
        if event is None:
            yield b": keep-alive\n\n"
            continue
        event_type = str(event.get("type") or "mcp_app.event")
        event_id = str(event.get("sequence_number", ""))
        data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        yield f"id: {event_id}\nevent: {event_type}\ndata: {data}\n\n".encode()


app = create_app()


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "error": {
                "message": "Unauthorized",
                "type": "authentication_error",
                "param": None,
                "code": "invalid_api_key",
            }
        },
        headers={"WWW-Authenticate": "Bearer"},
    )
