from __future__ import annotations

import asyncio
from collections import deque

from .models import AgentEvent, AgentEventType


class EventLog:
    """Bounded, process-local worker event history with async subscribers."""

    def __init__(self, max_history: int) -> None:
        self._events: deque[AgentEvent] = deque(maxlen=max_history)
        self._next_id = 0
        self._closed = False
        self._condition = asyncio.Condition()

    @property
    def last_event_id(self) -> int:
        return self._next_id - 1

    async def publish(self, event_type: AgentEventType, data: dict[str, object]) -> AgentEvent:
        async with self._condition:
            event = AgentEvent(id=self._next_id, type=event_type, data=data)
            self._next_id += 1
            self._events.append(event)
            self._condition.notify_all()
            return event

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()

    async def read(
        self,
        *,
        after: int,
        wait_seconds: float | None,
    ) -> tuple[list[AgentEvent], bool]:
        def ready() -> bool:
            return self._closed or any(event.id > after for event in self._events)

        async with self._condition:
            if not ready() and wait_seconds is not None:
                try:
                    await asyncio.wait_for(self._condition.wait_for(ready), timeout=wait_seconds)
                except TimeoutError:
                    pass
            events = [event.model_copy(deep=True) for event in self._events if event.id > after]
            return events, self._closed
