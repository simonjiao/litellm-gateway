from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from .models import AgentEvent, ExecutionInfo


class ExecutionNotFoundError(Exception):
    def __init__(self, execution_id: str) -> None:
        super().__init__(f"Agent execution not found: {execution_id}")
        self.execution_id = execution_id


class SandboxBackendError(Exception):
    pass


class ExecutionBackend(Protocol):
    async def startup(self) -> None: ...

    async def shutdown(self) -> None: ...

    async def create(self) -> ExecutionInfo: ...

    async def inspect(self, execution_id: str) -> ExecutionInfo: ...

    async def rpc(self, execution_id: str, method: str, params: dict[str, Any]) -> Any: ...

    async def resolve_server_request(
        self,
        execution_id: str,
        request_id: int | str,
        *,
        result: Any | None,
        error: dict[str, Any] | None,
    ) -> None: ...

    def events(
        self, execution_id: str, *, after: int, follow: bool
    ) -> AsyncIterator[AgentEvent]: ...

    async def terminate(self, execution_id: str) -> ExecutionInfo: ...
