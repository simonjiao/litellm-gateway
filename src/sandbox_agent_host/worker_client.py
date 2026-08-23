from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import httpx

from .backend import SandboxBackendError
from .models import AgentEvent


class WorkerClient:
    def __init__(self, api_key: str, *, timeout_seconds: float) -> None:
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, read=None))

    async def health(self, base_url: str) -> dict[str, Any]:
        response = await self._client.get(f"{base_url}/healthz")
        return _json_response(response)

    async def rpc(self, base_url: str, method: str, params: dict[str, Any]) -> Any:
        response = await self._client.post(
            f"{base_url}/v1/rpc",
            headers=self._headers,
            json={"method": method, "params": params},
        )
        body = _json_response(response)
        return body.get("result")

    async def resolve_server_request(
        self,
        base_url: str,
        request_id: int | str,
        *,
        result: Any | None,
        error: dict[str, Any] | None,
    ) -> None:
        token = quote(str(request_id), safe="")
        response = await self._client.post(
            f"{base_url}/v1/server-requests/{token}/resolve",
            headers=self._headers,
            json={"result": result, "error": error},
        )
        if response.status_code != 204:
            _raise_worker_error(response)

    async def events(
        self,
        base_url: str,
        *,
        after: int,
        follow: bool,
    ) -> AsyncIterator[AgentEvent]:
        async with self._client.stream(
            "GET",
            f"{base_url}/v1/events",
            headers=self._headers,
            params={"after": after, "follow": str(follow).lower()},
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                _raise_worker_error(response)
            async for payload in _sse_json(response):
                yield AgentEvent.model_validate(payload)

    async def aclose(self) -> None:
        await self._client.aclose()


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


def _json_response(response: httpx.Response) -> dict[str, Any]:
    if response.status_code >= 400:
        _raise_worker_error(response)
    value = response.json()
    if not isinstance(value, dict):
        raise SandboxBackendError("Sandbox worker returned a non-object response")
    return value


def _raise_worker_error(response: httpx.Response) -> None:
    try:
        body = response.json()
    except ValueError:
        body = response.text[:1000]
    raise SandboxBackendError(
        f"Sandbox worker request failed with HTTP {response.status_code}: {body}"
    )
