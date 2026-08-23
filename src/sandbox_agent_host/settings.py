from __future__ import annotations

import shlex
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    """Settings used only inside an Agent Session sandbox."""

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
    codex_sandbox: str = "workspace-write"
    codex_approval_policy: str = "never"
    codex_ephemeral_threads: bool = False

    request_timeout_seconds: float = Field(default=3600.0, gt=0)
    process_shutdown_seconds: float = Field(default=3.0, gt=0)
    max_event_history: int = Field(default=2048, ge=64)
    event_keepalive_seconds: float = Field(default=15.0, gt=0)

    mcp_apps_enabled: bool = True
    client_name: str = "sandbox_agent_worker"
    client_title: str = "Sandbox Agent Worker"
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


class HostSettings(BaseSettings):
    """Node-side Sandbox Agent Host settings."""

    model_config = SettingsConfigDict(
        env_prefix="SANDBOX_HOST_",
        env_file=".env",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = Field(default=8092, ge=1, le=65535)
    api_key: str = Field(default="local-sandbox-host-key", min_length=8)
    worker_api_key: str = Field(default="local-sandbox-worker-key", min_length=8)

    image: str = "litellm-codex-sandbox-worker:0.3.0"
    docker_runtime: str = "runsc"
    docker_network: str = "codex-agent-egress"
    egress_proxy_url: str = "http://egress-proxy:3128"
    worker_port: int = Field(default=8091, ge=1, le=65535)
    worker_start_timeout_seconds: float = Field(default=30.0, gt=0)

    execution_ttl_seconds: int = Field(default=1800, ge=30)
    reaper_interval_seconds: float = Field(default=15.0, gt=0, le=60)
    memory_limit: str = "4g"
    nano_cpus: int = Field(default=2_000_000_000, ge=100_000_000)
    pids_limit: int = Field(default=512, ge=32)
    tmpfs_limit: str = "256m"
    container_user: str = "10001:10001"

    codex_command: str = "codex app-server --stdio"
    codex_model: str | None = None
    codex_auth_file: Path | None = None
    codex_config_file: Path | None = None
    mcp_apps_enabled: bool = True

    @field_validator("docker_runtime", "docker_network", "image")
    @classmethod
    def non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("egress_proxy_url")
    @classmethod
    def valid_proxy_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("must be an absolute HTTP(S) proxy URL")
        return value

    @field_validator("codex_auth_file", "codex_config_file", mode="before")
    @classmethod
    def normalize_optional_path(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return Path(str(value)).expanduser().resolve()
