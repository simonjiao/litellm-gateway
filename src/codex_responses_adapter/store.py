from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .errors import ResponseNotFoundError
from .models import ResponseRecord


@dataclass(slots=True)
class ActiveExecution:
    response_id: str
    agent_execution_id: str
    thread_id: str
    turn_id: str
    event_cursor: int
    cancel_requested: bool = False
    driver_error: Exception | None = None
    task: asyncio.Task[None] | None = None
    _terminal: asyncio.Event = field(init=False, repr=False)
    _subscribers: set[asyncio.Queue[dict[str, Any] | None]] = field(init=False, repr=False)
    _subscriber_lock: asyncio.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._terminal = asyncio.Event()
        self._subscribers = set()
        self._subscriber_lock = asyncio.Lock()

    @property
    def terminal(self) -> asyncio.Event:
        return self._terminal

    async def subscribe(self) -> asyncio.Queue[dict[str, Any] | None]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        async with self._subscriber_lock:
            if self._terminal.is_set():
                queue.put_nowait(None)
            else:
                self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        async with self._subscriber_lock:
            self._subscribers.discard(queue)

    async def publish(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        async with self._subscriber_lock:
            subscribers = tuple(self._subscribers)
        for event in events:
            for queue in subscribers:
                queue.put_nowait(event)

    async def finish(self) -> None:
        async with self._subscriber_lock:
            subscribers = tuple(self._subscribers)
            self._subscribers.clear()
            self._terminal.set()
        for queue in subscribers:
            queue.put_nowait(None)


class ResponseStore:
    """Process-local response and active-execution registry.

    The interface is deliberately small so a durable implementation can replace it
    without changing the HTTP or Codex protocol layers.
    """

    def __init__(self) -> None:
        self._records: dict[str, ResponseRecord] = {}
        self._active: dict[str, ActiveExecution] = {}
        self._lock = asyncio.Lock()

    async def put(self, record: ResponseRecord) -> None:
        async with self._lock:
            self._records[record.id] = record

    async def get(self, response_id: str) -> ResponseRecord:
        async with self._lock:
            record = self._records.get(response_id)
        if record is None:
            raise ResponseNotFoundError(response_id)
        return record

    async def delete(self, response_id: str) -> ResponseRecord:
        async with self._lock:
            record = self._records.pop(response_id, None)
        if record is None:
            raise ResponseNotFoundError(response_id)
        return record

    async def register_active(self, execution: ActiveExecution) -> None:
        async with self._lock:
            self._active[execution.response_id] = execution

    async def get_active(self, response_id: str) -> ActiveExecution | None:
        async with self._lock:
            return self._active.get(response_id)

    async def unregister_active(self, response_id: str) -> None:
        async with self._lock:
            self._active.pop(response_id, None)

    async def active_executions(self) -> list[ActiveExecution]:
        async with self._lock:
            return list(self._active.values())
