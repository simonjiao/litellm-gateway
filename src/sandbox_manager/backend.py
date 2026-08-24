"""Sandbox Manager lifecycle backend contract."""

from __future__ import annotations

from typing import Protocol

from .models import SandboxInfo


class SandboxNotFoundError(Exception):
    def __init__(self, sandbox_id: str) -> None:
        super().__init__(f"Sandbox not found: {sandbox_id}")
        self.sandbox_id = sandbox_id


class SandboxBackendError(Exception):
    pass


class SandboxBackend(Protocol):
    async def startup(self) -> None: ...

    async def shutdown(self) -> None: ...

    async def create(self) -> SandboxInfo: ...

    async def inspect(self, sandbox_id: str) -> SandboxInfo: ...

    async def renew(self, sandbox_id: str) -> SandboxInfo: ...

    async def terminate(self, sandbox_id: str) -> SandboxInfo: ...
