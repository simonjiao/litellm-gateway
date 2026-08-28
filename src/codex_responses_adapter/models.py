from __future__ import annotations

import copy
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ResponseStatus = Literal["in_progress", "completed", "failed", "incomplete"]


class CreateResponseRequest(BaseModel):
    """Responses parameters supported by the Codex adapter."""

    model_config = ConfigDict(extra="allow")

    model: str
    input: Any
    instructions: str | None = None
    previous_response_id: str | None = None
    stream: bool = False
    background: bool = False
    metadata: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    store: bool | None = True
    max_output_tokens: int | None = Field(default=None, ge=1)
    reasoning: dict[str, Any] | None = None
    service_tier: str | None = None


class WorkspaceRelayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(pattern=r"^workspace_[a-zA-Z0-9_-]{8,64}$")
    grant: str = Field(min_length=16, max_length=64 * 1024)


class WorkspaceGrantRelayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant: str = Field(min_length=16, max_length=64 * 1024)


class ArtifactPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_id: str = Field(pattern=r"^resp_[a-f0-9]{32}$")
    grant: str = Field(min_length=16, max_length=64 * 1024)


@dataclass(slots=True)
class ResponseRecord:
    id: str
    model: str
    created_at: int
    status: ResponseStatus
    instructions: str | None
    previous_response_id: str | None
    metadata: dict[str, Any]
    input_items: list[dict[str, Any]]
    store: bool
    stream: bool
    reasoning: dict[str, Any] | None
    service_tier: str | None
    mcp_apps_base_url: str
    sandbox_id: str | None = None
    workspace_id: str | None = None
    workspace_recoverable: bool = False
    thread_id: str | None = None
    turn_id: str | None = None
    message_id: str = field(default_factory=lambda: f"msg_{uuid.uuid4().hex}")
    message_output_index: int | None = None
    output_text: str = ""
    message_started: bool = False
    message_completed: bool = False
    output_items: list[dict[str, Any]] = field(default_factory=list)
    output_indexes: dict[str, int] = field(default_factory=dict)
    mcp_app_targets: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    incomplete_details: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None

    @classmethod
    def create(
        cls,
        request: CreateResponseRequest,
        input_items: list[dict[str, Any]],
        *,
        mcp_apps_base_url: str = "",
    ) -> ResponseRecord:
        return cls(
            id=f"resp_{uuid.uuid4().hex}",
            model=request.model,
            created_at=int(time.time()),
            status="in_progress",
            instructions=request.instructions,
            previous_response_id=request.previous_response_id,
            metadata=dict(request.metadata or {}),
            input_items=input_items,
            store=request.store is not False,
            stream=request.stream,
            reasoning=dict(request.reasoning) if isinstance(request.reasoning, dict) else None,
            service_tier=request.service_tier,
            mcp_apps_base_url=mcp_apps_base_url,
        )

    def add_output_item(self, item: dict[str, Any]) -> int:
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("Responses output items require a non-empty id")
        existing = self.output_indexes.get(item_id)
        if existing is not None:
            self.output_items[existing] = copy.deepcopy(item)
            return existing
        index = len(self.output_items)
        self.output_indexes[item_id] = index
        self.output_items.append(copy.deepcopy(item))
        return index

    def update_output_item(self, item: dict[str, Any]) -> int:
        return self.add_output_item(item)

    def get_output_item(self, item_id: str) -> dict[str, Any] | None:
        index = self.output_indexes.get(item_id)
        if index is None:
            return None
        return copy.deepcopy(self.output_items[index])

    def output_index(self, item_id: str) -> int | None:
        return self.output_indexes.get(item_id)

    def ensure_message(self, item_id: str | None = None) -> int:
        if self.message_started and self.message_output_index is not None:
            return self.message_output_index
        if item_id:
            self.message_id = item_id
        self.message_started = True
        self.message_output_index = self.add_output_item(self.message_item())
        return self.message_output_index

    def sync_message_item(self) -> None:
        if self.message_started:
            self.update_output_item(self.message_item())

    def message_item(self) -> dict[str, Any]:
        item_status = "completed" if self.message_completed else "in_progress"
        return {
            "id": self.message_id,
            "type": "message",
            "status": item_status,
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": self.output_text,
                    "annotations": [],
                }
            ],
        }

    def register_mcp_app_target(
        self,
        call_id: str,
        target: dict[str, Any],
    ) -> None:
        self.mcp_app_targets[call_id] = copy.deepcopy(target)

    def allowed_mcp_server(self, server: str) -> bool:
        return any(target.get("server") == server for target in self.mcp_app_targets.values())

    def to_response(self) -> dict[str, Any]:
        self.sync_message_item()
        return {
            "id": self.id,
            "object": "response",
            "created_at": self.created_at,
            "status": self.status,
            "background": False,
            "error": self.error,
            "incomplete_details": self.incomplete_details,
            "instructions": self.instructions,
            "max_output_tokens": None,
            "model": self.model,
            "output": copy.deepcopy(self.output_items),
            "parallel_tool_calls": True,
            "previous_response_id": self.previous_response_id,
            "reasoning": self.reasoning,
            "store": self.store,
            "service_tier": self.service_tier,
            "temperature": None,
            "text": {"format": {"type": "text"}},
            "tool_choice": "auto",
            "tools": [],
            "top_p": None,
            "truncation": "disabled",
            "usage": self.usage,
            "metadata": self.metadata,
        }
