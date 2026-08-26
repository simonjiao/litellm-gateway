from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from support import sandbox_for

from codex_responses_adapter.app import create_app
from codex_responses_adapter.mcp_apps import ResolveInteractionRequest
from codex_responses_adapter.models import CreateResponseRequest
from codex_responses_adapter.settings import Settings


@pytest.fixture
def mcp_settings() -> Settings:
    return Settings(
        request_timeout_seconds=5,
        mcp_apps_interaction_timeout_seconds=5,
        mcp_apps_event_keepalive_seconds=0.05,
        mcp_apps_public_base_url="https://adapter.example",
        mcp_apps_enabled=True,
    )


async def _wait_for_interaction(app: Any, response_id: str) -> dict[str, Any]:
    for _ in range(100):
        state = await app.state.mcp_apps.snapshot(response_id)
        pending = [item for item in state["interactions"] if item["status"] == "pending"]
        if pending:
            return pending[0]
        await asyncio.sleep(0.01)
    raise AssertionError("MCP App elicitation was not created")


@pytest.mark.asyncio
async def test_mcp_app_interaction_resource_and_tool_bridge(
    mcp_settings: Settings,
) -> None:
    app = create_app(mcp_settings, sandbox_client=sandbox_for(mcp_settings))
    async with app.router.lifespan_context(app):
        service = app.state.service
        request = CreateResponseRequest(
            model="gpt-5.6-terra",
            input="Open the MCP App image editor",
            stream=True,
        )
        stream = await service.create_streaming(request)
        first_event = await anext(stream)
        second_event = await anext(stream)
        response_id = first_event["response"]["id"]
        events_task = asyncio.create_task(
            _collect_events(stream, initial=[first_event, second_event])
        )

        interaction = await _wait_for_interaction(app, response_id)
        assert interaction["method"] == "mcpServer/elicitation/request"
        assert interaction["params"]["serverName"] == "image_apps"

        await service.resolve_interaction(
            interaction["id"],
            ResolveInteractionRequest(
                action="accept",
                content={
                    "selection": {"x": 10, "y": 20, "width": 120, "height": 80},
                    "method": "blur",
                },
            ),
        )
        events = await asyncio.wait_for(events_task, timeout=5)

        event_types = [event["type"] for event in events]
        assert "response.mcp_call.in_progress" in event_types
        assert "response.mcp_call.completed" in event_types
        assert event_types[-1] == "response.completed"

        added = next(
            event
            for event in events
            if event["type"] == "response.output_item.added" and event["item"]["type"] == "mcp_call"
        )
        item = added["item"]
        descriptor = item["_meta"]["mcp_app"]
        assert descriptor["resource_uri"] == "ui://image/editor"
        assert descriptor["resource_url"].startswith("https://adapter.example/")
        assert descriptor["call_id"] == "mcp_call_image_editor"
        assert descriptor["app_id"] == "image-editor"
        assert descriptor["allowed_tools"] == ["apply_edit"]

        completed = next(
            event
            for event in events
            if event["type"] == "response.output_item.done" and event["item"]["type"] == "mcp_call"
        )
        assert "edited.png" in completed["item"]["output"]

        state = await service.app_state(response_id)
        assert state["closed"] is True
        assert any(event["type"] == "mcp_app.available" for event in state["events"])
        assert any(event["type"] == "mcp_app.elicitation.resolved" for event in state["events"])
        assert state["events"][-1]["type"] == "mcp_app.response.closed"
        assert state["app_sessions"] == [
            {
                "response_id": response_id,
                "origin_call_id": descriptor["call_id"],
                "app_id": "image-editor",
                "server_id": "image_apps",
                "resource_uri": "ui://image/editor",
                "allowed_tools": ["apply_edit"],
            }
        ]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {mcp_settings.api_key}"},
        ) as client:
            resource = await client.get(
                f"/v1/mcp-apps/responses/{response_id}/resources",
                params={
                    "server": "image_apps",
                    "uri": "ui://image/editor",
                    "origin_call_id": descriptor["call_id"],
                },
            )
            assert resource.status_code == 200, resource.text
            assert resource.headers["content-type"].startswith("text/html;profile=mcp-app")
            assert "Content-Security-Policy" in resource.headers
            assert "<canvas" in resource.text

            tool_call = await client.post(
                f"/v1/mcp-apps/responses/{response_id}/tools/call",
                json={
                    "server": "image_apps",
                    "tool": "apply_edit",
                    "origin_call_id": descriptor["call_id"],
                    "arguments": {
                        "selection": {"x": 1, "y": 2, "width": 3, "height": 4},
                        "method": "remove",
                    },
                },
            )
            assert tool_call.status_code == 200, tool_call.text
            assert (
                tool_call.json()["structuredContent"]["image_url"]
                == "https://example.test/edited-from-app.png"
            )

            wrong_resource = await client.get(
                f"/v1/mcp-apps/responses/{response_id}/resources",
                params={
                    "server": "image_apps",
                    "uri": "ui://other/app",
                    "origin_call_id": descriptor["call_id"],
                },
            )
            assert wrong_resource.status_code == 400
            assert wrong_resource.json()["error"]["code"] == "invalid_mcp_app_resource"

            wrong_tool = await client.post(
                f"/v1/mcp-apps/responses/{response_id}/tools/call",
                json={
                    "server": "image_apps",
                    "tool": "admin_tool",
                    "origin_call_id": descriptor["call_id"],
                    "arguments": {},
                },
            )
            assert wrong_tool.status_code == 400
            assert wrong_tool.json()["error"]["code"] == "mcp_app_tool_not_allowed"


@pytest.mark.asyncio
async def test_non_streaming_mcp_app_elicitation_fails_closed(
    mcp_settings: Settings,
) -> None:
    app = create_app(mcp_settings, sandbox_client=sandbox_for(mcp_settings))
    async with app.router.lifespan_context(app):
        response = await asyncio.wait_for(
            app.state.service.create_non_streaming(
                CreateResponseRequest(
                    model="gpt-5.6-terra",
                    input="Open the MCP App image editor",
                    stream=False,
                )
            ),
            timeout=5,
        )
        assert response["status"] == "completed"
        mcp_item = next(item for item in response["output"] if item["type"] == "mcp_call")
        assert mcp_item["status"] == "failed"


async def _collect_events(
    stream: Any, *, initial: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    events = list(initial or [])
    events.extend([event async for event in stream])
    return events
