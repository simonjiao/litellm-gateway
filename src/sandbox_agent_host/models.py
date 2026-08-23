from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ExecutionStatus = Literal["starting", "running", "failed", "terminated"]
AgentEventType = Literal["notification", "server_request", "session_failed"]


class ExecutionInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: ExecutionStatus
    created_at: int
    expires_at: int | None
    last_event_id: int = -1


class AgentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=0)
    type: AgentEventType
    data: dict[str, Any]


class RpcRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class RpcCommand(RpcRequest):
    type: Literal["rpc"]


class ResolveServerRequestCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["resolve_server_request"]
    request_id: int | str
    result: Any | None = None
    error: dict[str, Any] | None = None

    @model_validator(mode="after")
    def exactly_one_outcome(self) -> ResolveServerRequestCommand:
        if self.result is not None and self.error is not None:
            raise ValueError("result and error are mutually exclusive")
        if self.result is None and self.error is None:
            raise ValueError("result or error is required")
        return self


class TerminateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["terminate"]


ExecutionCommand = Annotated[
    RpcCommand | ResolveServerRequestCommand | TerminateCommand,
    Field(discriminator="type"),
]


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
