from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from sandbox_worker.app import create_worker_app
from sandbox_worker.settings import WorkerSettings


def _settings() -> WorkerSettings:
    fake = Path(__file__).with_name("fake_app_server.py")
    return WorkerSettings(
        api_key="worker-secret",
        codex_command=f"{sys.executable} {fake}",
        codex_workdir=fake.parent,
        request_timeout_seconds=5,
    )


def _sse_events(payload: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in payload.strip().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                value = json.loads(line.removeprefix("data: "))
                assert isinstance(value, dict)
                events.append(value)
    return events


@pytest.mark.asyncio
async def test_worker_owns_codex_session_and_exposes_rpc_and_events() -> None:
    app = create_worker_app(_settings())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://worker"
        ) as client:
            headers = {"Authorization": "Bearer worker-secret"}
            started = await client.post(
                "/v1/rpc",
                headers=headers,
                json={
                    "method": "thread/start",
                    "params": {"cwd": str(Path(__file__).parent), "ephemeral": False},
                },
            )
            assert started.status_code == 200, started.text
            thread_id = started.json()["result"]["thread"]["id"]

            turn = await client.post(
                "/v1/rpc",
                headers=headers,
                json={
                    "method": "turn/start",
                    "params": {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": "say hello"}],
                    },
                },
            )
            assert turn.status_code == 200, turn.text

            event_response = await client.get(
                "/v1/events",
                headers=headers,
                params={"after": -1, "follow": "false"},
            )
            events = _sse_events(event_response.text)
            methods = [event["data"]["method"] for event in events]
            assert methods[0] == "turn/started"
            assert "item/agentMessage/delta" in methods
            assert methods[-1] == "turn/completed"


@pytest.mark.asyncio
async def test_worker_bridges_server_requests_without_auto_approving() -> None:
    app = create_worker_app(_settings())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://worker"
        ) as client:
            headers = {"Authorization": "Bearer worker-secret"}
            started = await client.post(
                "/v1/rpc",
                headers=headers,
                json={"method": "thread/start", "params": {"ephemeral": False}},
            )
            thread_id = started.json()["result"]["thread"]["id"]
            await client.post(
                "/v1/rpc",
                headers=headers,
                json={
                    "method": "turn/start",
                    "params": {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": "Open the MCP App image editor"}],
                    },
                },
            )

            before = _sse_events(
                (
                    await client.get(
                        "/v1/events",
                        headers=headers,
                        params={"after": -1, "follow": "false"},
                    )
                ).text
            )
            request_event = next(event for event in before if event["type"] == "server_request")
            assert request_event["data"]["method"] == "mcpServer/elicitation/request"

            resolved = await client.post(
                f"/v1/server-requests/{request_event['data']['request_id']}/resolve",
                headers=headers,
                json={"result": {"action": "cancel", "content": None, "_meta": None}},
            )
            assert resolved.status_code == 204

            after = _sse_events(
                (
                    await client.get(
                        "/v1/events",
                        headers=headers,
                        params={"after": int(request_event["id"]), "follow": "false"},
                    )
                ).text
            )
            assert any(event["data"].get("method") == "turn/completed" for event in after)


@pytest.mark.asyncio
async def test_worker_rejects_non_mcp_app_interactive_requests_at_boundary() -> None:
    app = create_worker_app(_settings())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://worker"
        ) as client:
            headers = {"Authorization": "Bearer worker-secret"}
            started = await client.post(
                "/v1/rpc",
                headers=headers,
                json={"method": "thread/start", "params": {"ephemeral": False}},
            )
            thread_id = started.json()["result"]["thread"]["id"]
            await client.post(
                "/v1/rpc",
                headers=headers,
                json={
                    "method": "turn/start",
                    "params": {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": "request shell approval"}],
                    },
                },
            )

            events = _sse_events(
                (
                    await client.get(
                        "/v1/events",
                        headers=headers,
                        params={"after": -1, "follow": "false"},
                    )
                ).text
            )

            methods = [event["data"].get("method") for event in events]
            assert "test/approval-rejected" in methods
            assert "turn/completed" in methods
            assert all(event["type"] != "server_request" for event in events)
