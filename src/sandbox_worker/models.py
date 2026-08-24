from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

AgentEventType = Literal["notification", "server_request", "session_failed"]


class AgentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=0)
    type: AgentEventType
    data: dict[str, Any]


class RpcRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class ResolveServerRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: Any | None = None
    error: dict[str, Any] | None = None

    @model_validator(mode="after")
    def exactly_one_outcome(self) -> ResolveServerRequestBody:
        if self.result is not None and self.error is not None:
            raise ValueError("result and error are mutually exclusive")
        if self.result is None and self.error is None:
            raise ValueError("result or error is required")
        return self
