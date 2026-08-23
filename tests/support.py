from __future__ import annotations

import sys
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from codex_responses_adapter.settings import Settings
from sandbox_agent_host.models import AgentEvent, ExecutionInfo
from sandbox_agent_host.settings import WorkerSettings
from sandbox_agent_host.worker import WorkerRuntime


class InProcessAgentHost:
    """Test implementation of the AgentExecution client contract."""

    def __init__(self, worker_settings: WorkerSettings) -> None:
        self._settings = worker_settings
        self._runtimes: dict[str, WorkerRuntime] = {}
        self.started: list[str] = []
        self.terminated: list[str] = []
        self.rpc_calls: list[tuple[str, str, dict[str, Any]]] = []

    async def start_execution(self) -> ExecutionInfo:
        execution_id = f"exec_{uuid.uuid4().hex}"
        runtime = WorkerRuntime(self._settings)
        await runtime.start()
        self._runtimes[execution_id] = runtime
        self.started.append(execution_id)
        now = int(time.time())
        return ExecutionInfo(
            id=execution_id,
            status="running",
            created_at=now,
            expires_at=now + 3600,
            last_event_id=runtime.events.last_event_id,
        )

    async def inspect_execution(self, execution_id: str) -> ExecutionInfo:
        runtime = self._runtimes[execution_id]
        now = int(time.time())
        return ExecutionInfo(
            id=execution_id,
            status="running",
            created_at=now,
            expires_at=now + 3600,
            last_event_id=runtime.events.last_event_id,
        )

    async def rpc(self, execution_id: str, method: str, params: dict[str, Any]) -> Any:
        self.rpc_calls.append((execution_id, method, params))
        return await self._runtimes[execution_id].rpc(method, params)

    async def events(self, execution_id: str, *, after: int) -> AsyncIterator[AgentEvent]:
        async for event in self._runtimes[execution_id].event_stream(after=after, follow=True):
            if event is not None:
                yield event

    async def resolve_server_request(
        self,
        execution_id: str,
        request_id: int | str,
        *,
        result: Any | None,
        error: dict[str, Any] | None,
    ) -> None:
        await self._runtimes[execution_id].resolve_server_request(
            str(request_id), result=result, error=error
        )

    async def terminate_execution(self, execution_id: str) -> ExecutionInfo:
        runtime = self._runtimes.pop(execution_id)
        await runtime.close()
        self.terminated.append(execution_id)
        now = int(time.time())
        return ExecutionInfo(
            id=execution_id,
            status="terminated",
            created_at=now,
            expires_at=None,
            last_event_id=runtime.events.last_event_id,
        )

    async def aclose(self) -> None:
        for execution_id, runtime in list(self._runtimes.items()):
            await runtime.close()
            self._runtimes.pop(execution_id, None)


def agent_host_for(settings: Settings) -> InProcessAgentHost:
    fake = Path(__file__).with_name("fake_app_server.py")
    return InProcessAgentHost(
        WorkerSettings(
            api_key="worker-secret",
            codex_command=f"{sys.executable} {fake}",
            codex_workdir=fake.parent,
            codex_model=settings.codex_model,
            request_timeout_seconds=settings.request_timeout_seconds,
            event_keepalive_seconds=settings.mcp_apps_event_keepalive_seconds,
            mcp_apps_enabled=settings.mcp_apps_enabled,
        )
    )
