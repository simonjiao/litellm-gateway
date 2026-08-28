from __future__ import annotations

# FastAPI registers decorated local handlers dynamically.
# pyright: reportUnusedFunction=false
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response
from test_sandbox_manager_api import StubBackend

from codex_responses_adapter.sandbox import HttpSandboxClient
from sandbox_manager.app import create_app as create_manager_app
from sandbox_manager.settings import ManagerSettings


@pytest.mark.asyncio
async def test_adapter_uses_manager_for_lifecycle_and_worker_for_rpc_and_events() -> None:
    backend = StubBackend()
    manager_app = create_manager_app(
        ManagerSettings(
            api_key="manager-secret",
            worker_token_secret="worker-token-secret-at-least-32-bytes",
        ),
        backend=backend,
    )
    worker_app = FastAPI()
    worker_calls: list[str] = []

    @worker_app.post("/v1/rpc")
    async def rpc(request: Request) -> dict[str, Any]:
        assert request.headers["authorization"] == "Bearer worker-specific-secret"
        body = await request.json()
        method = body["method"]
        worker_calls.append(method)
        if method == "thread/start":
            return {"result": {"thread": {"id": "thread_test"}}}
        return {"result": {"turn": {"id": "turn_test"}}}

    @worker_app.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        assert request.headers["authorization"] == "Bearer worker-specific-secret"
        return {"status": "ok", "last_event_id": -1}

    @worker_app.get("/v1/events")
    async def worker_events(request: Request) -> Response:
        assert request.headers["authorization"] == "Bearer worker-specific-secret"
        payload = {
            "id": 0,
            "type": "notification",
            "data": {
                "method": "turn/completed",
                "params": {"threadId": "thread_test", "turnId": "turn_test"},
            },
        }
        return Response(
            f"id: 0\nevent: notification\ndata: {json.dumps(payload)}\n\n",
            media_type="text/event-stream",
        )

    async with manager_app.router.lifespan_context(manager_app):
        async with worker_app.router.lifespan_context(worker_app):
            manager_http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=manager_app),
                base_url="http://manager",
            )
            worker_http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=worker_app),
                base_url="http://sandbox-worker-test:8091",
            )
            client = HttpSandboxClient(
                "http://manager",
                "manager-secret",
                manager_client=manager_http,
                worker_client=worker_http,
            )

            sandbox = await client.create_sandbox()
            assert sandbox.id == "sandbox_test"
            assert (await client.inspect_sandbox(sandbox.id)).status == "running"
            assert (await client.renew_sandbox(sandbox.id)).expires_at == 130
            assert await client.event_cursor(sandbox.id) == -1

            started = await client.rpc(
                sandbox.id,
                "thread/start",
                {"cwd": "/workspace", "ephemeral": False},
            )
            thread_id = started["thread"]["id"]
            await client.rpc(
                sandbox.id,
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": "say hello"}],
                },
            )
            received_events = []
            async for event in client.events(sandbox.id, after=-1):
                received_events.append(event)
                if event.data.get("method") == "turn/completed":
                    break
            assert received_events[-1].data["method"] == "turn/completed"
            assert worker_calls == ["thread/start", "turn/start"]

            terminated = await client.terminate_sandbox(sandbox.id)
            assert terminated.status == "terminated"
            assert backend.renewed == ["sandbox_test"]
            assert backend.terminated == ["sandbox_test"]

            recoverable = await client.create_sandbox("signed-sandbox-create-grant")
            assert recoverable.recoverable is True
            assert backend.sandbox_workspace_grants == ["signed-sandbox-create-grant"]
            workspace = await client.authorize_workspace(
                "workspace_api_test01", "signed-workspace-inspect-grant"
            )
            assert workspace.id == "workspace_api_test01"
            created_workspace = await client.create_workspace(
                "workspace_client_test01", "signed-workspace-create-grant"
            )
            assert created_workspace.id == "workspace_client_test01"
            released_workspace = await client.release_workspace(
                "workspace_client_test01", "signed-workspace-release-grant"
            )
            assert released_workspace.delete_after == 1000
            operation = await client.create_operation("signed-operation-grant")
            assert operation.id == "operation_test"
            completed = await client.inspect_operation(operation.id)
            assert completed.status == "succeeded"
            await client.aclose()
