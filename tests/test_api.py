from __future__ import annotations

import json

import httpx
import pytest
from support import sandbox_for

from codex_responses_adapter.app import create_app
from codex_responses_adapter.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        request_timeout_seconds=5,
    )


@pytest.mark.asyncio
async def test_non_streaming_and_previous_response(settings: Settings) -> None:
    app = create_app(settings, sandbox_client=sandbox_for(settings))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {settings.api_key}"},
        ) as client:
            first = await client.post(
                "/v1/responses",
                json={"model": "gpt-5.6-terra", "input": "say hello"},
            )
            assert first.status_code == 200, first.text
            first_body = first.json()
            assert first_body["status"] == "completed"
            assert first_body["output"][0]["content"][0]["text"] == "hello world"

            second = await client.post(
                "/v1/responses",
                json={
                    "model": "gpt-5.6-terra",
                    "input": "continue",
                    "previous_response_id": first_body["id"],
                },
            )
            assert second.status_code == 200, second.text
            second_body = second.json()
            assert second_body["previous_response_id"] == first_body["id"]

            first_record = await app.state.store.get(first_body["id"])
            second_record = await app.state.store.get(second_body["id"])
            assert first_record.thread_id != second_record.thread_id

            retrieved = await client.get(f"/v1/responses/{first_body['id']}")
            assert retrieved.status_code == 200
            assert retrieved.json()["id"] == first_body["id"]

            items = await client.get(f"/v1/responses/{first_body['id']}/input_items")
            assert items.status_code == 200
            assert items.json()["first_id"].startswith("msg_")


@pytest.mark.asyncio
async def test_streaming_response(settings: Settings) -> None:
    app = create_app(settings, sandbox_client=sandbox_for(settings))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {settings.api_key}"},
        ) as client:
            response = await client.post(
                "/v1/responses",
                json={"model": "gpt-5.6-terra", "input": "say hello", "stream": True},
            )
            assert response.status_code == 200
            events = []
            for block in response.text.strip().split("\n\n"):
                data_line = next(line for line in block.splitlines() if line.startswith("data: "))
                events.append(json.loads(data_line.removeprefix("data: ")))
            event_types = [event["type"] for event in events]
            assert event_types[0] == "response.created"
            assert "response.output_text.delta" in event_types
            assert event_types[-1] == "response.completed"


@pytest.mark.asyncio
async def test_workspace_and_artifact_control_routes_relay_signed_grants(
    settings: Settings,
) -> None:
    sandbox = sandbox_for(settings)
    app = create_app(settings, sandbox_client=sandbox)
    headers = {"Authorization": f"Bearer {settings.api_key}"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=headers,
        ) as client:
            workspace = await client.post(
                "/v1/workspaces",
                json={
                    "workspace_id": "workspace_adapter_test01",
                    "grant": "signed-workspace-create-grant",
                },
            )
            assert workspace.status_code == 201
            assert workspace.json()["id"] == "workspace_adapter_test01"

            response = await client.post(
                "/v1/responses",
                json={
                    "model": "gpt-5.6-terra",
                    "input": "produce a file",
                    "metadata": {"agent_workspace_grant": "signed-sandbox-create-grant"},
                },
            )
            assert response.status_code == 200, response.text
            published = await client.post(
                "/v1/artifacts/publish",
                json={
                    "response_id": response.json()["id"],
                    "grant": "signed-artifact-publish-grant",
                },
            )
            assert published.status_code == 200
            assert published.json()["file_id"] == "file_test"


@pytest.mark.asyncio
async def test_rejects_unmapped_tools(settings: Settings) -> None:
    app = create_app(settings, sandbox_client=sandbox_for(settings))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {settings.api_key}"},
        ) as client:
            response = await client.post(
                "/v1/responses",
                json={
                    "model": "gpt-5.6-terra",
                    "input": "hello",
                    "tools": [{"type": "function", "name": "x", "parameters": {}}],
                },
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "unsupported_tools"


@pytest.mark.asyncio
async def test_rejects_unenforced_parameters(settings: Settings) -> None:
    app = create_app(settings, sandbox_client=sandbox_for(settings))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {settings.api_key}"},
        ) as client:
            max_tokens = await client.post(
                "/v1/responses",
                json={
                    "model": "gpt-5.6-terra",
                    "input": "hello",
                    "max_output_tokens": 128,
                },
            )
            assert max_tokens.status_code == 400
            assert max_tokens.json()["error"]["code"] == "unsupported_max_output_tokens"

            unknown = await client.post(
                "/v1/responses",
                json={
                    "model": "gpt-5.6-terra",
                    "input": "hello",
                    "temperature": 0.2,
                },
            )
            assert unknown.status_code == 400
            assert unknown.json()["error"]["code"] == "unsupported_parameter"


@pytest.mark.asyncio
async def test_streaming_validation_fails_before_sse_start(settings: Settings) -> None:
    app = create_app(settings, sandbox_client=sandbox_for(settings))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {settings.api_key}"},
        ) as client:
            response = await client.post(
                "/v1/responses",
                json={
                    "model": "gpt-5.6-terra",
                    "input": "hello",
                    "stream": True,
                    "tools": [{"type": "function", "name": "x", "parameters": {}}],
                },
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "unsupported_tools"


@pytest.mark.asyncio
async def test_invalid_json_returns_400(settings: Settings) -> None:
    app = create_app(settings, sandbox_client=sandbox_for(settings))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {settings.api_key}"},
        ) as client:
            response = await client.post(
                "/v1/responses",
                content=b"{not-json",
                headers={"Content-Type": "application/json"},
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_json"


@pytest.mark.asyncio
async def test_adapter_requires_deployment_bearer_credential(settings: Settings) -> None:
    app = create_app(settings, sandbox_client=sandbox_for(settings))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/responses",
                json={"model": "gpt-5.6-terra", "input": "hello"},
            )
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "invalid_api_key"
