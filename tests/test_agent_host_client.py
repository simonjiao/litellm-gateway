from __future__ import annotations

import httpx
import pytest
from test_agent_host_api import StubBackend

from codex_responses_adapter.agent_host import HttpAgentExecutionClient
from sandbox_agent_host.app import create_app
from sandbox_agent_host.settings import HostSettings


@pytest.mark.asyncio
async def test_adapter_agent_host_client_uses_http_and_sse_contract() -> None:
    backend = StubBackend()
    host_app = create_app(
        HostSettings(api_key="host-secret", worker_api_key="worker-secret"),
        backend=backend,
    )
    async with host_app.router.lifespan_context(host_app):
        http_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host_app), base_url="http://host"
        )
        client = HttpAgentExecutionClient("http://host", "host-secret", client=http_client)

        execution = await client.start_execution()
        assert execution.id == "exec_test"
        assert (await client.inspect_execution(execution.id)).status == "running"
        assert await client.rpc(execution.id, "thread/start", {"ephemeral": False}) == {
            "method": "thread/start",
            "params": {"ephemeral": False},
        }
        events = [event async for event in client.events(execution.id, after=-1)]
        assert [event.id for event in events] == [0, 1]
        assert events[-1].data["method"] == "turn/completed"
        assert (await client.terminate_execution(execution.id)).status == "terminated"
        await client.aclose()
