from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from sandbox_agent_host.app import create_app
from sandbox_agent_host.models import AgentEvent, ExecutionInfo
from sandbox_agent_host.settings import HostSettings


class StubBackend:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str, dict[str, Any]]] = []
        self.terminated: list[str] = []
        self.info = ExecutionInfo(
            id="exec_test",
            status="running",
            created_at=10,
            expires_at=70,
            last_event_id=1,
        )

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def create(self) -> ExecutionInfo:
        return self.info

    async def inspect(self, execution_id: str) -> ExecutionInfo:
        assert execution_id == self.info.id
        return self.info

    async def rpc(self, execution_id: str, method: str, params: dict[str, Any]) -> Any:
        self.commands.append((execution_id, method, params))
        return {"method": method, "params": params}

    async def resolve_server_request(
        self,
        execution_id: str,
        request_id: int | str,
        *,
        result: Any | None,
        error: dict[str, Any] | None,
    ) -> None:
        self.commands.append(
            (
                execution_id,
                "resolve_server_request",
                {"request_id": request_id, "result": result, "error": error},
            )
        )

    async def events(
        self, execution_id: str, *, after: int, follow: bool
    ) -> AsyncIterator[AgentEvent]:
        assert execution_id == self.info.id
        assert after == -1
        yield AgentEvent(
            id=0,
            type="notification",
            data={"method": "turn/started", "params": {"turnId": "turn_1"}},
        )
        yield AgentEvent(
            id=1,
            type="notification",
            data={"method": "turn/completed", "params": {"turnId": "turn_1"}},
        )

    async def terminate(self, execution_id: str) -> ExecutionInfo:
        self.terminated.append(execution_id)
        return ExecutionInfo(
            id=execution_id,
            status="terminated",
            created_at=10,
            expires_at=None,
            last_event_id=1,
        )


@pytest.mark.asyncio
async def test_agent_execution_interface_is_authenticated_and_narrow() -> None:
    backend = StubBackend()
    settings = HostSettings(api_key="host-secret", worker_api_key="worker-secret")
    app = create_app(settings, backend=backend)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://host"
        ) as client:
            unauthenticated = await client.post("/v1/executions")
            assert unauthenticated.status_code == 401

            headers = {"Authorization": "Bearer host-secret"}
            created = await client.post("/v1/executions", headers=headers)
            assert created.status_code == 201
            assert created.json()["id"] == "exec_test"

            inspected = await client.get("/v1/executions/exec_test", headers=headers)
            assert inspected.status_code == 200
            assert inspected.json()["status"] == "running"

            rpc = await client.post(
                "/v1/executions/exec_test/commands",
                headers=headers,
                json={"type": "rpc", "method": "thread/start", "params": {"ephemeral": False}},
            )
            assert rpc.status_code == 200
            assert rpc.json()["result"]["method"] == "thread/start"

            arbitrary_shell = await client.post(
                "/v1/executions/exec_test/commands",
                headers=headers,
                json={"type": "shell", "command": "id"},
            )
            assert arbitrary_shell.status_code == 422

            events = await client.get(
                "/v1/executions/exec_test/events",
                headers=headers,
                params={"after": -1, "follow": "false"},
            )
            assert events.status_code == 200
            assert "event: notification" in events.text
            assert '"method":"turn/completed"' in events.text

            terminated = await client.delete("/v1/executions/exec_test", headers=headers)
            assert terminated.status_code == 200
            assert terminated.json()["status"] == "terminated"
            assert backend.terminated == ["exec_test"]
