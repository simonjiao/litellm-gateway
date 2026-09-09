"""Keep the upstream response reachable when a saved chat is explicitly stopped."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import aiohttp

from .settings import SETTINGS


@dataclass
class _ResponseRequest:
    session: Any
    kwargs: dict[str, Any]
    opening: asyncio.Task[Any] | None = None
    response: Any = None
    response_id: str | None = None
    initial_lines: list[bytes] = field(default_factory=list)

    async def open(self) -> Any:
        self.response = await self.session.request(**self.kwargs)
        if self.response.status >= 400:
            return self.response
        if "text/event-stream" in self.response.headers.get("Content-Type", ""):
            # Preserve the first event for the native consumer. The request stays
            # alive until we know which Response to cancel, even during startup.
            async for line in self.response.content:
                self.initial_lines.append(line)
                if line.startswith(b"data:"):
                    try:
                        event = json.loads(line[5:])
                    except (ValueError, UnicodeError):
                        continue
                    self.response_id = event.get("response", {}).get("id")
                    if self.response_id:
                        break
        else:
            self.response_id = (await self.response.json()).get("id")
        return self.response

    async def cancel(self) -> None:
        try:
            if self.opening is not None:
                await self.opening
            if self.response_id:
                # LiteLLM encodes Response IDs; send that ID back through the
                # same gateway so it selects and decodes the correct backend.
                url = self.kwargs["url"].rstrip("/") + "/" + quote(self.response_id, safe="")
                response = await self.session.request(
                    method="POST",
                    url=url + "/cancel",
                    headers=self.kwargs.get("headers"),
                    cookies=self.kwargs.get("cookies"),
                    ssl=self.kwargs.get("ssl", True),
                    timeout=aiohttp.ClientTimeout(total=30),
                )
                try:
                    response.raise_for_status()
                finally:
                    response.close()
        finally:
            if self.response is not None:
                self.response.close()


_requests: dict[tuple[str, str], _ResponseRequest] = {}


def _key(metadata: Any) -> tuple[str, str] | None:
    if not SETTINGS.enabled or not isinstance(metadata, dict):
        return None
    if metadata.get("task") or metadata.get("internal"):
        return None
    chat_id = metadata.get("chat_id")
    message_id = metadata.get("assistant_message_id") or metadata.get("message_id")
    if not isinstance(chat_id, str) or not isinstance(message_id, str):
        return None
    if not chat_id or not message_id or chat_id.startswith(("local:", "channel:")):
        return None
    return chat_id, message_id


async def request_response(session: Any, metadata: Any, is_responses: bool, **kwargs: Any) -> Any:
    key = _key(metadata) if is_responses else None
    if key is None:
        return await session.request(**kwargs)
    pending = _ResponseRequest(session, kwargs)
    _requests[key] = pending
    pending.opening = asyncio.create_task(pending.open())
    return await asyncio.shield(pending.opening)


async def stream_response(source: Any, metadata: Any):
    key = _key(metadata)
    pending = _requests.get(key) if key else None
    try:
        if pending is not None:
            for line in pending.initial_lines:
                yield line
            pending.initial_lines.clear()
        async for line in source:
            yield line
    finally:
        await source.aclose()


async def cancel_response(metadata: Any) -> None:
    key = _key(metadata)
    pending = _requests.get(key) if key else None
    if pending is not None:
        await pending.cancel()


def finish_response(metadata: Any) -> None:
    key = _key(metadata)
    if key is not None:
        pending = _requests.pop(key, None)
        if pending is not None and pending.response is not None:
            pending.response.close()
