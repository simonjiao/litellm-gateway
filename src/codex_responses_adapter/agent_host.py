from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Protocol

import httpx

from sandbox_agent_host.models import AgentEvent, ExecutionInfo

from .errors import UpstreamProtocolError


class AgentExecutionClient(Protocol):
    async def start_execution(self) -> ExecutionInfo: ...

    async def inspect_execution(self, execution_id: str) -> ExecutionInfo: ...

    async def rpc(self, execution_id: str, method: str, params: dict[str, Any]) -> Any: ...

    def events(self, execution_id: str, *, after: int) -> AsyncIterator[AgentEvent]: ...

    async def resolve_server_request(
        self,
        execution_id: str,
        request_id: int | str,
        *,
        result: Any | None,
        error: dict[str, Any] | None,
    ) -> None: ...

    async def terminate_execution(self, execution_id: str) -> ExecutionInfo: ...

    async def aclose(self) -> None: ...


class HttpAgentExecutionClient:
    """Adapter-side client for the narrow AgentExecution HTTP/SSE seam."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: float = 3600.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, read=None)
        )

    async def start_execution(self) -> ExecutionInfo:
        response = await self._request("POST", "/v1/executions")
        return ExecutionInfo.model_validate(_json_object(response))

    async def inspect_execution(self, execution_id: str) -> ExecutionInfo:
        response = await self._request("GET", f"/v1/executions/{execution_id}")
        return ExecutionInfo.model_validate(_json_object(response))

    async def rpc(self, execution_id: str, method: str, params: dict[str, Any]) -> Any:
        response = await self._request(
            "POST",
            f"/v1/executions/{execution_id}/commands",
            json_body={"type": "rpc", "method": method, "params": params},
        )
        return _json_object(response).get("result")

    async def events(self, execution_id: str, *, after: int) -> AsyncIterator[AgentEvent]:
        try:
            async with self._client.stream(
                "GET",
                f"{self._base_url}/v1/executions/{execution_id}/events",
                headers=self._headers,
                params={"after": after, "follow": "true"},
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    _raise_agent_host_error(response)
                async for payload in _sse_json(response):
                    yield AgentEvent.model_validate(payload)
        except httpx.HTTPError as exc:
            raise UpstreamProtocolError(f"Sandbox Agent Host event stream failed: {exc}") from exc

    async def resolve_server_request(
        self,
        execution_id: str,
        request_id: int | str,
        *,
        result: Any | None,
        error: dict[str, Any] | None,
    ) -> None:
        await self._request(
            "POST",
            f"/v1/executions/{execution_id}/commands",
            json_body={
                "type": "resolve_server_request",
                "request_id": request_id,
                "result": result,
                "error": error,
            },
        )

    async def terminate_execution(self, execution_id: str) -> ExecutionInfo:
        response = await self._request("DELETE", f"/v1/executions/{execution_id}")
        return ExecutionInfo.model_validate(_json_object(response))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method,
                f"{self._base_url}{path}",
                headers=self._headers,
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise UpstreamProtocolError(f"Sandbox Agent Host request failed: {exc}") from exc
        if response.status_code >= 400:
            _raise_agent_host_error(response)
        return response


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


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError as exc:
        raise UpstreamProtocolError("Sandbox Agent Host returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise UpstreamProtocolError("Sandbox Agent Host returned a non-object response")
    return value


def _raise_agent_host_error(response: httpx.Response) -> None:
    try:
        body = response.json()
    except ValueError:
        body = None
    message: str | None = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            message = error["message"]
    raise UpstreamProtocolError(
        message or f"Sandbox Agent Host returned HTTP {response.status_code}"
    )
