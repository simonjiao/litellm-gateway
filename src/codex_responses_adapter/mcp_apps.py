from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .errors import InteractionNotFoundError, InvalidRequestError, ResponseConflictError

InteractionAction = Literal["accept", "decline", "cancel"]
InteractionStatus = Literal["pending", "resolved", "expired", "cancelled"]


class ResolveInteractionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    action: InteractionAction
    content: Any | None = None
    meta: Any | None = Field(default=None, alias="_meta")


class McpAppToolCallRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    server: str
    tool: str
    origin_call_id: str | None = None
    arguments: Any | None = None
    meta: Any | None = Field(default=None, alias="_meta")


@dataclass(frozen=True, slots=True)
class AppSession:
    response_id: str
    origin_call_id: str
    app_id: str
    server_id: str
    resource_uri: str
    allowed_tools: tuple[str, ...]

    def public(self) -> dict[str, Any]:
        return {
            "response_id": self.response_id,
            "origin_call_id": self.origin_call_id,
            "app_id": self.app_id,
            "server_id": self.server_id,
            "resource_uri": self.resource_uri,
            "allowed_tools": list(self.allowed_tools),
        }


@dataclass(slots=True)
class AppInteraction:
    id: str
    response_id: str
    method: str
    params: dict[str, Any]
    created_at: int
    status: InteractionStatus
    future: asyncio.Future[dict[str, Any]]
    result: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "mcp_app.interaction",
            "response_id": self.response_id,
            "method": self.method,
            "params": self.params,
            "created_at": self.created_at,
            "status": self.status,
            "result": self.result,
        }


class McpAppsState:
    """Process-local MCP Apps interaction and event state.

    The state is intentionally separate from ResponseStore so it can later move to a
    shared implementation without changing the Responses or Codex protocol layers.
    """

    def __init__(self, *, max_event_history: int = 512) -> None:
        self._max_event_history = max_event_history
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._next_sequence: dict[str, int] = {}
        self._closed_responses: set[str] = set()
        self._app_sessions: dict[tuple[str, str], AppSession] = {}
        self._interactions: dict[str, AppInteraction] = {}
        self._interaction_ids_by_response: dict[str, set[str]] = {}
        self._condition = asyncio.Condition()

    async def publish(
        self,
        response_id: str,
        event_type: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        async with self._condition:
            sequence = self._next_sequence.get(response_id, 0)
            self._next_sequence[response_id] = sequence + 1
            event = {
                "type": event_type,
                "sequence_number": sequence,
                "response_id": response_id,
                "created_at": int(time.time()),
                "data": data,
            }
            history = self._events.setdefault(response_id, [])
            history.append(event)
            if len(history) > self._max_event_history:
                del history[: len(history) - self._max_event_history]
            self._condition.notify_all()
            return event

    async def wait_for_events(
        self,
        response_id: str,
        *,
        after: int,
        timeout_seconds: float,
    ) -> tuple[list[dict[str, Any]], bool]:
        def ready() -> bool:
            return (
                any(
                    int(event.get("sequence_number", -1)) > after
                    for event in self._events.get(response_id, [])
                )
                or response_id in self._closed_responses
            )

        async with self._condition:
            if not ready():
                try:
                    await asyncio.wait_for(
                        self._condition.wait_for(ready),
                        timeout=timeout_seconds,
                    )
                except TimeoutError:
                    return [], response_id in self._closed_responses
            events = [
                dict(event)
                for event in self._events.get(response_id, [])
                if int(event.get("sequence_number", -1)) > after
            ]
            return events, response_id in self._closed_responses

    async def snapshot(self, response_id: str) -> dict[str, Any]:
        async with self._condition:
            interactions = [
                self._interactions[interaction_id].public()
                for interaction_id in sorted(
                    self._interaction_ids_by_response.get(response_id, set())
                )
                if interaction_id in self._interactions
            ]
            app_sessions = [
                session.public()
                for (bound_response_id, _), session in sorted(
                    self._app_sessions.items(), key=lambda item: item[0]
                )
                if bound_response_id == response_id
            ]
            return {
                "object": "mcp_app.response_state",
                "response_id": response_id,
                "closed": response_id in self._closed_responses,
                "events": [dict(event) for event in self._events.get(response_id, [])],
                "interactions": interactions,
                "app_sessions": app_sessions,
            }

    async def register_app_session(
        self, response_id: str, descriptor: dict[str, Any]
    ) -> AppSession:
        origin_call_id = descriptor.get("call_id")
        app_id = descriptor.get("app_id")
        server_id = descriptor.get("server")
        resource_uri = descriptor.get("resource_uri")
        if (
            not isinstance(origin_call_id, str)
            or not origin_call_id
            or not isinstance(app_id, str)
            or not app_id
            or not isinstance(server_id, str)
            or not server_id
            or not isinstance(resource_uri, str)
            or not resource_uri
        ):
            raise InvalidRequestError(
                "Codex emitted an invalid MCP App descriptor.",
                code="invalid_mcp_app_descriptor",
            )
        if descriptor.get("response_id") != response_id:
            raise InvalidRequestError(
                "MCP App descriptor response binding is invalid.",
                code="invalid_mcp_app_descriptor",
            )
        raw_tools = descriptor.get("allowed_tools")
        allowed_tools = (
            tuple(dict.fromkeys(value for value in raw_tools if isinstance(value, str) and value))
            if isinstance(raw_tools, list)
            else ()
        )
        session = AppSession(
            response_id=response_id,
            origin_call_id=origin_call_id,
            app_id=app_id,
            server_id=server_id,
            resource_uri=resource_uri,
            allowed_tools=allowed_tools,
        )
        async with self._condition:
            self._app_sessions[(response_id, origin_call_id)] = session
            self._condition.notify_all()
        return session

    async def get_app_session(self, response_id: str, origin_call_id: str) -> AppSession:
        async with self._condition:
            session = self._app_sessions.get((response_id, origin_call_id))
        if session is None:
            raise InvalidRequestError(
                "MCP App call does not belong to this response.",
                param="origin_call_id",
                code="invalid_mcp_app_origin",
            )
        return session

    async def find_app_session(self, response_id: str, origin_call_id: str) -> AppSession | None:
        async with self._condition:
            return self._app_sessions.get((response_id, origin_call_id))

    async def close_response(self, response_id: str) -> None:
        async with self._condition:
            self._closed_responses.add(response_id)
            self._condition.notify_all()

    async def delete_response(self, response_id: str) -> None:
        async with self._condition:
            interaction_ids = self._interaction_ids_by_response.pop(response_id, set())
            for interaction_id in interaction_ids:
                interaction = self._interactions.pop(interaction_id, None)
                if interaction is not None and not interaction.future.done():
                    interaction.status = "cancelled"
                    result = {"action": "cancel", "content": None, "_meta": None}
                    interaction.result = result
                    interaction.future.set_result(result)
            self._events.pop(response_id, None)
            self._next_sequence.pop(response_id, None)
            self._closed_responses.discard(response_id)
            for key in [key for key in self._app_sessions if key[0] == response_id]:
                self._app_sessions.pop(key, None)
            self._condition.notify_all()

    async def create_interaction(
        self,
        *,
        response_id: str,
        method: str,
        params: dict[str, Any],
    ) -> AppInteraction:
        interaction_id = f"mcpint_{uuid.uuid4().hex}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        interaction = AppInteraction(
            id=interaction_id,
            response_id=response_id,
            method=method,
            params=dict(params),
            created_at=int(time.time()),
            status="pending",
            future=future,
        )
        async with self._condition:
            self._interactions[interaction_id] = interaction
            self._interaction_ids_by_response.setdefault(response_id, set()).add(interaction_id)
            self._condition.notify_all()
        return interaction

    async def get_interaction(self, interaction_id: str) -> AppInteraction:
        async with self._condition:
            interaction = self._interactions.get(interaction_id)
        if interaction is None:
            raise InteractionNotFoundError(interaction_id)
        return interaction

    async def resolve_interaction(
        self,
        interaction_id: str,
        request: ResolveInteractionRequest,
    ) -> AppInteraction:
        async with self._condition:
            interaction = self._interactions.get(interaction_id)
            if interaction is None:
                raise InteractionNotFoundError(interaction_id)
            if interaction.status != "pending":
                raise ResponseConflictError(
                    "The MCP App interaction is no longer pending.",
                    code="mcp_app_interaction_already_resolved",
                )
            result = {
                "action": request.action,
                "content": request.content if request.action == "accept" else None,
                "_meta": request.meta,
            }
            interaction.status = "resolved"
            interaction.result = result
            if not interaction.future.done():
                interaction.future.set_result(result)
            self._condition.notify_all()
            return interaction

    async def expire_interaction(self, interaction_id: str) -> AppInteraction:
        async with self._condition:
            interaction = self._interactions.get(interaction_id)
            if interaction is None:
                raise InteractionNotFoundError(interaction_id)
            if interaction.status == "pending":
                result = {"action": "cancel", "content": None, "_meta": None}
                interaction.status = "expired"
                interaction.result = result
                if not interaction.future.done():
                    interaction.future.set_result(result)
                self._condition.notify_all()
            return interaction

    async def cancel_response_interactions(self, response_id: str) -> None:
        async with self._condition:
            for interaction_id in self._interaction_ids_by_response.get(response_id, set()):
                interaction = self._interactions.get(interaction_id)
                if interaction is None or interaction.status != "pending":
                    continue
                result = {"action": "cancel", "content": None, "_meta": None}
                interaction.status = "cancelled"
                interaction.result = result
                if not interaction.future.done():
                    interaction.future.set_result(result)
            self._condition.notify_all()
