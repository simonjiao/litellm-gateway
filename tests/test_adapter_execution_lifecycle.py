from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from support import InProcessAgentHost

from codex_responses_adapter.app import create_app
from codex_responses_adapter.errors import UpstreamProtocolError
from codex_responses_adapter.models import CreateResponseRequest
from codex_responses_adapter.settings import Settings
from sandbox_agent_host.models import AgentEvent
from sandbox_agent_host.settings import WorkerSettings


class FlakyEventSubscriptionHost(InProcessAgentHost):
    def __init__(self, worker_settings: WorkerSettings) -> None:
        super().__init__(worker_settings)
        self.subscriptions = 0

    async def events(self, execution_id: str, *, after: int) -> AsyncIterator[AgentEvent]:
        self.subscriptions += 1
        async for event in super().events(execution_id, after=after):
            yield event
            if self.subscriptions == 1:
                raise UpstreamProtocolError("temporary Agent Host SSE disconnect")


def _settings() -> tuple[Settings, WorkerSettings]:
    fake = Path(__file__).with_name("fake_app_server.py")
    adapter = Settings(
        api_key="adapter-secret",
        request_timeout_seconds=5,
        mcp_apps_event_keepalive_seconds=0.05,
    )
    worker = WorkerSettings(
        api_key="worker-secret",
        codex_command=f"{sys.executable} {fake}",
        codex_workdir=fake.parent,
        request_timeout_seconds=5,
        event_keepalive_seconds=0.05,
    )
    return adapter, worker


@pytest.mark.asyncio
async def test_stream_disconnect_only_unsubscribes_execution_continues() -> None:
    settings, worker_settings = _settings()
    host = InProcessAgentHost(worker_settings)
    app = create_app(settings, agent_host=host)

    async with app.router.lifespan_context(app):
        stream = await app.state.service.create_streaming(
            CreateResponseRequest(model="codex-app-server", input="say hello", stream=True)
        )
        created = await anext(stream)
        response_id = created["response"]["id"]
        await stream.aclose()

        response: dict[str, Any] = {}
        for _ in range(100):
            response = await app.state.service.retrieve(response_id)
            if response["status"] != "in_progress":
                break
            await asyncio.sleep(0.01)

        assert response["status"] == "completed"
        assert response["output"][0]["content"][0]["text"] == "hello world"
        assert host.terminated == []


@pytest.mark.asyncio
async def test_previous_response_reuses_agent_session_sandbox() -> None:
    settings, worker_settings = _settings()
    host = InProcessAgentHost(worker_settings)
    app = create_app(settings, agent_host=host)

    async with app.router.lifespan_context(app):
        first = await app.state.service.create_non_streaming(
            CreateResponseRequest(model="codex-app-server", input="first")
        )
        second = await app.state.service.create_non_streaming(
            CreateResponseRequest(
                model="codex-app-server",
                input="second",
                previous_response_id=first["id"],
            )
        )
        first_record = await app.state.store.get(first["id"])
        second_record = await app.state.store.get(second["id"])

        assert len(host.started) == 1
        assert first_record.agent_execution_id == host.started[0]
        assert second_record.agent_execution_id == first_record.agent_execution_id


@pytest.mark.asyncio
async def test_cancel_interrupts_turn_without_destroying_reusable_sandbox() -> None:
    settings, worker_settings = _settings()
    host = InProcessAgentHost(worker_settings)
    app = create_app(settings, agent_host=host)

    async with app.router.lifespan_context(app):
        stream = await app.state.service.create_streaming(
            CreateResponseRequest(
                model="codex-app-server",
                input="wait until cancelled",
                stream=True,
            )
        )
        created = await anext(stream)
        response_id = created["response"]["id"]
        await app.state.service.cancel(response_id)
        events = [event async for event in stream]

        assert events[-1]["type"] == "response.incomplete"
        assert (await app.state.service.retrieve(response_id))["status"] == "incomplete"
        assert any(method == "turn/interrupt" for _, method, _ in host.rpc_calls)
        assert host.terminated == []


@pytest.mark.asyncio
async def test_adapter_reconnects_internal_host_events_without_public_stream_replay() -> None:
    settings, worker_settings = _settings()
    host = FlakyEventSubscriptionHost(worker_settings)
    app = create_app(settings, agent_host=host)

    async with app.router.lifespan_context(app):
        response = await app.state.service.create_non_streaming(
            CreateResponseRequest(model="codex-app-server", input="say hello")
        )
        assert response["status"] == "completed"
        assert response["output"][0]["content"][0]["text"] == "hello world"
        assert host.subscriptions == 2
