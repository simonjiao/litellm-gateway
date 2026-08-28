from __future__ import annotations

import sys
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from codex_responses_adapter.settings import Settings
from sandbox_manager.models import OperationInfo, SandboxInfo, WorkerConnection, WorkspaceInfo
from sandbox_worker.app import WorkerRuntime
from sandbox_worker.models import AgentEvent
from sandbox_worker.settings import WorkerSettings


class InProcessSandbox:
    """Test implementation of the Adapter's Sandbox client contract."""

    def __init__(self, worker_settings: WorkerSettings) -> None:
        self._settings = worker_settings
        self._runtimes: dict[str, WorkerRuntime] = {}
        self.started: list[str] = []
        self.renewed: list[str] = []
        self.terminated: list[str] = []
        self.rpc_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.workspace_grants: list[str] = []
        self.operation_grants: list[str] = []
        self._recoverable: set[str] = set()

    async def create_sandbox(self, workspace_grant: str | None = None) -> SandboxInfo:
        sandbox_id = f"sandbox_{uuid.uuid4().hex}"
        runtime = WorkerRuntime(self._settings)
        await runtime.start()
        self._runtimes[sandbox_id] = runtime
        self.started.append(sandbox_id)
        if workspace_grant is not None:
            self.workspace_grants.append(workspace_grant)
            self._recoverable.add(sandbox_id)
        return self._info(sandbox_id)

    async def inspect_sandbox(self, sandbox_id: str) -> SandboxInfo:
        if sandbox_id not in self._runtimes:
            raise KeyError(sandbox_id)
        return self._info(sandbox_id)

    async def renew_sandbox(self, sandbox_id: str) -> SandboxInfo:
        if sandbox_id not in self._runtimes:
            raise KeyError(sandbox_id)
        self.renewed.append(sandbox_id)
        return self._info(sandbox_id)

    async def rpc(self, sandbox_id: str, method: str, params: dict[str, Any]) -> Any:
        self.rpc_calls.append((sandbox_id, method, params))
        return await self._runtimes[sandbox_id].rpc(method, params)

    async def event_cursor(self, sandbox_id: str) -> int:
        return self._runtimes[sandbox_id].events.last_event_id

    async def events(self, sandbox_id: str, *, after: int) -> AsyncIterator[AgentEvent]:
        async for event in self._runtimes[sandbox_id].event_stream(after=after, follow=True):
            if event is not None:
                yield event

    async def resolve_server_request(
        self,
        sandbox_id: str,
        request_id: int | str,
        *,
        result: Any | None,
        error: dict[str, Any] | None,
    ) -> None:
        await self._runtimes[sandbox_id].resolve_server_request(
            str(request_id), result=result, error=error
        )

    async def terminate_sandbox(self, sandbox_id: str) -> SandboxInfo:
        runtime = self._runtimes.pop(sandbox_id)
        await runtime.close()
        self.terminated.append(sandbox_id)
        now = int(time.time())
        return SandboxInfo(
            id=sandbox_id,
            status="terminated",
            created_at=now,
            expires_at=None,
            worker=None,
        )

    async def authorize_workspace(self, workspace_id: str, grant: str) -> WorkspaceInfo:
        self.workspace_grants.append(grant)
        now = int(time.time())
        return WorkspaceInfo(
            id=workspace_id,
            kind="recoverable",
            status="running",
            generation=1,
            head_revision=None,
            active_sandbox_id=None,
            created_at=now,
            updated_at=now,
            delete_after=None,
        )

    async def create_workspace(self, workspace_id: str, grant: str) -> WorkspaceInfo:
        return await self.authorize_workspace(workspace_id, grant)

    async def release_workspace(self, workspace_id: str, grant: str) -> WorkspaceInfo:
        workspace = await self.authorize_workspace(workspace_id, grant)
        return workspace.model_copy(
            update={"status": "detached_clean", "delete_after": int(time.time()) + 3600}
        )

    async def create_operation(self, grant: str) -> OperationInfo:
        self.operation_grants.append(grant)
        sandbox_id = self.started[-1]
        now = int(time.time())
        operation = "publish" if "publish" in grant else "checkout"
        result = (
            {"file_id": "file_test", "download_url": "/api/v1/files/file_test/content"}
            if operation == "publish"
            else {"path": "uploads/input.txt"}
        )
        return OperationInfo(
            id=f"operation_{uuid.uuid4().hex}",
            operation=operation,
            status="succeeded",
            workspace_id=f"workspace_{sandbox_id.removeprefix('sandbox_')}",
            sandbox_id=sandbox_id,
            result=result,
            error=None,
            created_at=now,
            updated_at=now,
        )

    async def inspect_operation(self, operation_id: str) -> OperationInfo:
        raise AssertionError(f"completed operation should not be polled: {operation_id}")

    async def aclose(self) -> None:
        for sandbox_id, runtime in list(self._runtimes.items()):
            await runtime.close()
            self._runtimes.pop(sandbox_id, None)

    def _info(self, sandbox_id: str) -> SandboxInfo:
        now = int(time.time())
        return SandboxInfo(
            id=sandbox_id,
            status="running",
            created_at=now,
            expires_at=now + 3600,
            worker=WorkerConnection(
                base_url=f"http://sandbox-worker-{sandbox_id}:8091",
                api_key="worker-specific-secret",
            ),
            workspace_id=f"workspace_{sandbox_id.removeprefix('sandbox_')}",
            recoverable=sandbox_id in self._recoverable,
        )


def sandbox_for(settings: Settings) -> InProcessSandbox:
    fake = Path(__file__).with_name("fake_app_server.py")
    return InProcessSandbox(
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
