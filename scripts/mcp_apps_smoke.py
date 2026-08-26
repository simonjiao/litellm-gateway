from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from typing import Any

import httpx


def main() -> None:
    base_url = os.getenv("LITELLM_BASE_URL", "http://127.0.0.1:4000/v1").rstrip("/")
    request = {
        "model": "codex-terra",
        "input": "Open the configured image-editing MCP App and modify the selected area.",
        "stream": True,
    }

    resolved = False
    final_response: dict[str, Any] | None = None
    with httpx.stream(
        "POST",
        f"{base_url}/responses",
        headers={"Authorization": f"Bearer {os.environ['LITELLM_MASTER_KEY']}"},
        json=request,
        timeout=3600,
    ) as response:
        response.raise_for_status()
        for event in _sse_events(response.iter_lines()):
            event_type = event.get("type")
            print(event_type)

            item = event.get("item")
            descriptor = _mcp_app_descriptor(item)
            if descriptor is not None and not resolved:
                _show_resource(descriptor)
                interaction = _wait_for_interaction(descriptor["state_url"])
                _resolve_interaction(interaction)
                resolved = True

            if event_type in {
                "response.completed",
                "response.incomplete",
                "response.failed",
            }:
                response_object = event.get("response")
                if isinstance(response_object, dict):
                    final_response = response_object

    if final_response is None:
        raise RuntimeError("Responses stream ended without a terminal response event")
    print(json.dumps(final_response, indent=2, ensure_ascii=False))


def _sse_events(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    data_lines: list[str] = []
    for line in lines:
        if line == "":
            if data_lines:
                payload = "\n".join(data_lines)
                if payload != "[DONE]":
                    value = json.loads(payload)
                    if isinstance(value, dict):
                        yield value
                data_lines.clear()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        value = json.loads("\n".join(data_lines))
        if isinstance(value, dict):
            yield value


def _mcp_app_descriptor(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("type") != "mcp_call":
        return None
    meta = value.get("_meta")
    if not isinstance(meta, dict):
        return None
    descriptor = meta.get("mcp_app")
    return descriptor if isinstance(descriptor, dict) else None


def _show_resource(descriptor: dict[str, Any]) -> None:
    url = descriptor.get("resource_url")
    if not isinstance(url, str):
        return
    response = httpx.get(url, headers=_mcp_apps_headers(), timeout=30)
    response.raise_for_status()
    content_type = response.headers.get("content-type")
    print(f"MCP App resource: {content_type} ({len(response.content)} bytes)")


def _wait_for_interaction(state_url: Any) -> dict[str, Any]:
    if not isinstance(state_url, str):
        raise RuntimeError("MCP App descriptor is missing state_url")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        response = httpx.get(state_url, headers=_mcp_apps_headers(), timeout=30)
        response.raise_for_status()
        state = response.json()
        interactions = state.get("interactions") if isinstance(state, dict) else None
        if isinstance(interactions, list):
            for interaction in interactions:
                if isinstance(interaction, dict) and interaction.get("status") == "pending":
                    return interaction
        time.sleep(0.1)
    raise TimeoutError("No pending MCP App interaction appeared")


def _resolve_interaction(interaction: dict[str, Any]) -> None:
    resolve_url = interaction.get("resolve_url")
    if not isinstance(resolve_url, str):
        raise RuntimeError("MCP App interaction is missing resolve_url")
    response = httpx.post(
        resolve_url,
        headers=_mcp_apps_headers(),
        json={
            "action": "accept",
            "content": {
                "selection": {"x": 10, "y": 20, "width": 240, "height": 160},
                "method": "blur",
            },
        },
        timeout=30,
    )
    response.raise_for_status()


def _mcp_apps_headers() -> dict[str, str]:
    token = os.getenv("MCP_APPS_BFF_TOKEN") or os.getenv("CODEX_ADAPTER_API_KEY")
    return {"Authorization": f"Bearer {token}"} if token else {}


if __name__ == "__main__":
    main()
