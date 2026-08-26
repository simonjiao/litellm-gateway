from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from support import InProcessSandbox

from codex_responses_adapter.app import create_app
from codex_responses_adapter.errors import UpstreamProtocolError
from codex_responses_adapter.models import CreateResponseRequest
from codex_responses_adapter.settings import Settings
from sandbox_worker.models import AgentEvent
from sandbox_worker.settings import WorkerSettings


class FlakyEventSubscriptionSandbox(InProcessSandbox):
    def __init__(self, worker_settings: WorkerSettings) -> None:
        super().__init__(worker_settings)
        self.subscriptions = 0

    async def events(self, sandbox_id: str, *, after: int) -> AsyncIterator[AgentEvent]:
        self.subscriptions += 1
        async for event in super().events(sandbox_id, after=after):
            yield event
            if self.subscriptions == 1:
                raise UpstreamProtocolError("temporary Worker SSE disconnect")


def _settings() -> tuple[Settings, WorkerSettings]:
    fake = Path(__file__).with_name("fake_app_server.py")
    adapter = Settings(
        api_key="adapter-secret",
        request_timeout_seconds=5,
        mcp_apps_event_keepalive_seconds=0.05,
        sandbox_lease_renew_interval_seconds=0.01,
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
    sandbox = InProcessSandbox(worker_settings)
    app = create_app(settings, sandbox_client=sandbox)

    async with app.router.lifespan_context(app):
        stream = await app.state.service.create_streaming(
            CreateResponseRequest(model="gpt-5.6-terra", input="say hello", stream=True)
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
        assert sandbox.terminated == []


@pytest.mark.asyncio
async def test_previous_response_reuses_agent_session_sandbox() -> None:
    settings, worker_settings = _settings()
    sandbox = InProcessSandbox(worker_settings)
    app = create_app(settings, sandbox_client=sandbox)

    async with app.router.lifespan_context(app):
        first = await app.state.service.create_non_streaming(
            CreateResponseRequest(model="gpt-5.6-sol", input="first")
        )
        second = await app.state.service.create_non_streaming(
            CreateResponseRequest(
                model="gpt-5.6-luna",
                input="second",
                previous_response_id=first["id"],
            )
        )
        first_record = await app.state.store.get(first["id"])
        second_record = await app.state.store.get(second["id"])

        assert len(sandbox.started) == 1
        assert first_record.sandbox_id == sandbox.started[0]
        assert second_record.sandbox_id == first_record.sandbox_id
        assert first_record.sandbox_id in sandbox.renewed

        thread_calls = [
            (method, params)
            for _, method, params in sandbox.rpc_calls
            if method.startswith("thread/")
        ]
        assert [(method, params["model"]) for method, params in thread_calls] == [
            ("thread/start", "gpt-5.6-sol"),
            ("thread/fork", "gpt-5.6-luna"),
        ]
        assert all("sandbox" not in params for _, params in thread_calls)
        turn_calls = [params for _, method, params in sandbox.rpc_calls if method == "turn/start"]
        assert all(params["approvalPolicy"] == "never" for params in turn_calls)
        assert all(
            params["sandboxPolicy"]
            == {"type": "externalSandbox", "networkAccess": "restricted"}
            for params in turn_calls
        )


@pytest.mark.asyncio
async def test_cancel_interrupts_turn_without_destroying_reusable_sandbox() -> None:
    settings, worker_settings = _settings()
    sandbox = InProcessSandbox(worker_settings)
    app = create_app(settings, sandbox_client=sandbox)

    async with app.router.lifespan_context(app):
        stream = await app.state.service.create_streaming(
            CreateResponseRequest(
                model="gpt-5.6-terra",
                input="wait until cancelled",
                stream=True,
            )
        )
        created = await anext(stream)
        response_id = created["response"]["id"]
        await asyncio.sleep(0.03)
        assert sandbox.started[0] in sandbox.renewed
        await app.state.service.cancel(response_id)
        events = [event async for event in stream]

        assert events[-1]["type"] == "response.incomplete"
        assert (await app.state.service.retrieve(response_id))["status"] == "incomplete"
        assert any(method == "turn/interrupt" for _, method, _ in sandbox.rpc_calls)
        assert sandbox.terminated == []


@pytest.mark.asyncio
async def test_adapter_reconnects_worker_events_without_public_stream_replay() -> None:
    settings, worker_settings = _settings()
    sandbox = FlakyEventSubscriptionSandbox(worker_settings)
    app = create_app(settings, sandbox_client=sandbox)

    async with app.router.lifespan_context(app):
        response = await app.state.service.create_non_streaming(
            CreateResponseRequest(model="gpt-5.6-terra", input="say hello")
        )
        assert response["status"] == "completed"
        assert response["output"][0]["content"][0]["text"] == "hello world"
        assert sandbox.subscriptions == 2
