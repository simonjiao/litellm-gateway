from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Protocol

import httpx

from sandbox_manager.models import OperationInfo, SandboxInfo, WorkerConnection, WorkspaceInfo
from sandbox_worker.models import AgentEvent

from .errors import UpstreamProtocolError


class SandboxUnavailableError(UpstreamProtocolError):
    pass


class SandboxClient(Protocol):
    async def create_sandbox(self, workspace_grant: str | None = None) -> SandboxInfo: ...

    async def inspect_sandbox(self, sandbox_id: str) -> SandboxInfo: ...

    async def renew_sandbox(self, sandbox_id: str) -> SandboxInfo: ...

    async def rpc(self, sandbox_id: str, method: str, params: dict[str, Any]) -> Any: ...

    async def event_cursor(self, sandbox_id: str) -> int: ...

    def events(self, sandbox_id: str, *, after: int) -> AsyncIterator[AgentEvent]: ...

    async def resolve_server_request(
        self,
        sandbox_id: str,
        request_id: int | str,
        *,
        result: Any | None,
        error: dict[str, Any] | None,
    ) -> None: ...

    async def terminate_sandbox(self, sandbox_id: str) -> SandboxInfo: ...

    async def authorize_workspace(self, workspace_id: str, grant: str) -> WorkspaceInfo: ...

    async def create_workspace(self, workspace_id: str, grant: str) -> WorkspaceInfo: ...

    async def release_workspace(self, workspace_id: str, grant: str) -> WorkspaceInfo: ...

    async def create_operation(self, grant: str) -> OperationInfo: ...

    async def inspect_operation(self, operation_id: str) -> OperationInfo: ...

    async def aclose(self) -> None: ...


class HttpSandboxClient:
    """Adapter-side client for Manager lifecycle and direct Worker data access."""

    def __init__(
        self,
        manager_base_url: str,
        manager_api_key: str,
        *,
        timeout_seconds: float = 3600.0,
        manager_client: httpx.AsyncClient | None = None,
        worker_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._manager_base_url = manager_base_url.rstrip("/")
        self._manager_headers = {"Authorization": f"Bearer {manager_api_key}"}
        timeout = httpx.Timeout(timeout_seconds, read=None)
        self._manager = manager_client or httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
        )
        self._worker = worker_client or httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
        )
        self._connections: dict[str, WorkerConnection] = {}

    async def create_sandbox(self, workspace_grant: str | None = None) -> SandboxInfo:
        body = {"workspace_grant": workspace_grant} if workspace_grant is not None else None
        response = await self._manager_request("POST", "/v1/sandboxes", json_body=body)
        return self._remember(SandboxInfo.model_validate(_json_object(response, "Manager")))

    async def inspect_sandbox(self, sandbox_id: str) -> SandboxInfo:
        response = await self._manager_request("GET", f"/v1/sandboxes/{sandbox_id}")
        return self._remember(SandboxInfo.model_validate(_json_object(response, "Manager")))

    async def renew_sandbox(self, sandbox_id: str) -> SandboxInfo:
        response = await self._manager_request("POST", f"/v1/sandboxes/{sandbox_id}/lease")
        return self._remember(SandboxInfo.model_validate(_json_object(response, "Manager")))

    async def rpc(self, sandbox_id: str, method: str, params: dict[str, Any]) -> Any:
        connection = await self._connection(sandbox_id)
        response = await self._worker_request(
            connection,
            "POST",
            "/v1/rpc",
            json_body={"method": method, "params": params},
        )
        return _json_object(response, "Worker").get("result")

    async def event_cursor(self, sandbox_id: str) -> int:
        connection = await self._connection(sandbox_id)
        response = await self._worker_request(connection, "GET", "/healthz")
        value = _json_object(response, "Worker").get("last_event_id")
        if not isinstance(value, int):
            raise UpstreamProtocolError(
                "Sandbox Worker health response did not include last_event_id"
            )
        return value

    async def events(self, sandbox_id: str, *, after: int) -> AsyncIterator[AgentEvent]:
        connection = await self._connection(sandbox_id)
        try:
            async with self._worker.stream(
                "GET",
                f"{connection.base_url}/v1/events",
                headers=_worker_headers(connection),
                params={"after": after, "follow": "true"},
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    _raise_upstream(response, "Sandbox Worker")
                async for payload in _sse_json(response):
                    yield AgentEvent.model_validate(payload)
        except httpx.HTTPError as exc:
            raise UpstreamProtocolError(f"Sandbox Worker event stream failed: {exc}") from exc

    async def resolve_server_request(
        self,
        sandbox_id: str,
        request_id: int | str,
        *,
        result: Any | None,
        error: dict[str, Any] | None,
    ) -> None:
        connection = await self._connection(sandbox_id)
        await self._worker_request(
            connection,
            "POST",
            f"/v1/server-requests/{request_id}/resolve",
            json_body={"result": result, "error": error},
        )

    async def terminate_sandbox(self, sandbox_id: str) -> SandboxInfo:
        response = await self._manager_request("DELETE", f"/v1/sandboxes/{sandbox_id}")
        self._connections.pop(sandbox_id, None)
        return SandboxInfo.model_validate(_json_object(response, "Manager"))

    async def authorize_workspace(self, workspace_id: str, grant: str) -> WorkspaceInfo:
        response = await self._manager_request(
            "POST",
            f"/v1/workspaces/{workspace_id}/inspect",
            json_body={"grant": grant},
        )
        return WorkspaceInfo.model_validate(_json_object(response, "Manager"))

    async def create_workspace(self, workspace_id: str, grant: str) -> WorkspaceInfo:
        response = await self._manager_request(
            "POST",
            "/v1/workspaces",
            json_body={"workspace_id": workspace_id, "grant": grant},
        )
        return WorkspaceInfo.model_validate(_json_object(response, "Manager"))

    async def release_workspace(self, workspace_id: str, grant: str) -> WorkspaceInfo:
        response = await self._manager_request(
            "POST",
            f"/v1/workspaces/{workspace_id}/release",
            json_body={"grant": grant},
        )
        return WorkspaceInfo.model_validate(_json_object(response, "Manager"))

    async def create_operation(self, grant: str) -> OperationInfo:
        response = await self._manager_request(
            "POST",
            "/v1/operations",
            json_body={"grant": grant},
        )
        return OperationInfo.model_validate(_json_object(response, "Manager"))

    async def inspect_operation(self, operation_id: str) -> OperationInfo:
        response = await self._manager_request("GET", f"/v1/operations/{operation_id}")
        return OperationInfo.model_validate(_json_object(response, "Manager"))

    async def aclose(self) -> None:
        await self._manager.aclose()
        if self._worker is not self._manager:
            await self._worker.aclose()

    async def _connection(self, sandbox_id: str) -> WorkerConnection:
        connection = self._connections.get(sandbox_id)
        if connection is not None:
            return connection
        sandbox = await self.inspect_sandbox(sandbox_id)
        if sandbox.status != "running" or sandbox.worker is None:
            raise SandboxUnavailableError(f"Sandbox '{sandbox_id}' is unavailable")
        return sandbox.worker

    def _remember(self, sandbox: SandboxInfo) -> SandboxInfo:
        if sandbox.status == "running" and sandbox.worker is not None:
            self._connections[sandbox.id] = sandbox.worker
        else:
            self._connections.pop(sandbox.id, None)
        return sandbox

    async def _manager_request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._manager.request(
                method,
                f"{self._manager_base_url}{path}",
                headers=self._manager_headers,
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise UpstreamProtocolError(f"Sandbox Manager request failed: {exc}") from exc
        if response.status_code >= 400:
            if response.status_code == 404:
                raise SandboxUnavailableError("Sandbox is unavailable")
            _raise_upstream(response, "Sandbox Manager")
        return response

    async def _worker_request(
        self,
        connection: WorkerConnection,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._worker.request(
                method,
                f"{connection.base_url}{path}",
                headers=_worker_headers(connection),
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise UpstreamProtocolError(f"Sandbox Worker request failed: {exc}") from exc
        if response.status_code >= 400:
            _raise_upstream(response, "Sandbox Worker")
        return response


def _worker_headers(connection: WorkerConnection) -> dict[str, str]:
    return {"Authorization": f"Bearer {connection.api_key}"}


async def _sse_json(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data_lines:
                value = json.loads("\n".join(data_lines))
                if isinstance(value, dict):
                    yield value
            data_lines.clear()
            continue
        if line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").lstrip())
    if data_lines:
        value = json.loads("\n".join(data_lines))
        if isinstance(value, dict):
            yield value


def _json_object(response: httpx.Response, upstream: str) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError as exc:
        raise UpstreamProtocolError(f"Sandbox {upstream} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise UpstreamProtocolError(f"Sandbox {upstream} returned a non-object response")
    return value


def _raise_upstream(response: httpx.Response, upstream: str) -> None:
    try:
        body = response.json()
    except ValueError:
        body = None
    message: str | None = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            message = error["message"]
    raise UpstreamProtocolError(message or f"{upstream} returned HTTP {response.status_code}")
