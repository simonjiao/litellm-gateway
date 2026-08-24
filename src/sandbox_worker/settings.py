from __future__ import annotations

import shlex
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    """Settings used only inside one Sandbox Worker."""

    model_config = SettingsConfigDict(
        env_prefix="SANDBOX_WORKER_",
        env_file=".env",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = Field(default=8091, ge=1, le=65535)
    api_key: str = Field(default="local-sandbox-worker-key", min_length=8)

    codex_command: str = "codex app-server --stdio"
    codex_workdir: Path = Path("/workspace")
    codex_model: str | None = None

    request_timeout_seconds: float = Field(default=3600.0, gt=0)
    process_shutdown_seconds: float = Field(default=3.0, gt=0)
    max_event_history: int = Field(default=2048, ge=64)
    event_keepalive_seconds: float = Field(default=15.0, gt=0)

    mcp_apps_enabled: bool = True
    client_name: str = "sandbox_worker"
    client_title: str = "Sandbox Worker"
    client_version: str = "0.3.0"

    @field_validator("codex_workdir")
    @classmethod
    def normalize_workdir(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("codex_model", mode="before")
    @classmethod
    def empty_model_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def command_argv(self) -> list[str]:
        argv = shlex.split(self.codex_command)
        if not argv:
            raise ValueError("SANDBOX_WORKER_CODEX_COMMAND must not be empty")
        return argv
