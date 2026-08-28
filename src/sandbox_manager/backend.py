"""Sandbox Manager lifecycle backend contract."""

from __future__ import annotations

from typing import Protocol

from .models import SandboxInfo, WorkspaceInfo


class SandboxNotFoundError(Exception):
    def __init__(self, sandbox_id: str) -> None:
        super().__init__(f"Sandbox not found: {sandbox_id}")
        self.sandbox_id = sandbox_id


class SandboxBackendError(Exception):
    pass


class SandboxAuthorizationError(Exception):
    pass


class SandboxConflictError(Exception):
    pass


class WorkspaceNotFoundError(Exception):
    pass


class SandboxBackend(Protocol):
    async def startup(self) -> None: ...

    async def shutdown(self) -> None: ...

    async def create(self, workspace_grant: str | None = None) -> SandboxInfo: ...

    async def inspect(self, sandbox_id: str) -> SandboxInfo: ...

    async def renew(self, sandbox_id: str) -> SandboxInfo: ...

    async def terminate(self, sandbox_id: str) -> SandboxInfo: ...

    async def create_workspace(self, workspace_id: str, grant: str) -> WorkspaceInfo: ...

    async def inspect_workspace(self, workspace_id: str, grant: str) -> WorkspaceInfo: ...

    async def release_workspace(self, workspace_id: str, grant: str) -> WorkspaceInfo: ...
