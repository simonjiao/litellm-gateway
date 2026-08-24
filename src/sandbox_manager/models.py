from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SandboxStatus = Literal["starting", "running", "failed", "terminated"]


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
