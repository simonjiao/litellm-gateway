from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SandboxStatus = Literal["starting", "running", "failed", "terminated"]
WorkspaceKind = Literal["ephemeral", "recoverable"]
WorkspaceStatus = Literal[
    "running",
    "detached_dirty",
    "checkpointing",
    "detached_clean",
    "remote_only",
    "restoring",
    "deleting",
]
OperationStatus = Literal["pending", "running", "succeeded", "failed"]


class SandboxCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_grant: str | None = Field(default=None, max_length=64 * 1024)


class WorkspaceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(pattern=r"^workspace_[a-zA-Z0-9_-]{8,64}$")
    grant: str = Field(min_length=16, max_length=64 * 1024)


class WorkspaceGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant: str = Field(min_length=16, max_length=64 * 1024)


class WorkspaceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: WorkspaceKind
    status: WorkspaceStatus
    generation: int
    head_revision: str | None
    active_sandbox_id: str | None
    created_at: int
    updated_at: int
    delete_after: int | None


class OperationInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    operation: str
    status: OperationStatus
    workspace_id: str
    sandbox_id: str | None
    result: dict[str, Any] | None
    error: str | None
    created_at: int
    updated_at: int


class WorkerConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    api_key: str = Field(min_length=8)

    @field_validator("base_url")
    @classmethod
    def absolute_http_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("must be an absolute HTTP(S) URL")
        return value


class SandboxInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: SandboxStatus
    created_at: int
    expires_at: int | None
    worker: WorkerConnection | None
    workspace_id: str | None = None
    recoverable: bool = False
