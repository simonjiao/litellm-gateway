from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ManagerSettings(BaseSettings):
    """Sandbox Manager settings."""

    model_config = SettingsConfigDict(
        env_prefix="SANDBOX_MANAGER_",
        env_file=".env",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = Field(default=8092, ge=1, le=65535)
    api_key: str = Field(default="local-sandbox-manager-key", min_length=8)
    worker_token_secret: str = Field(
        default="local-worker-token-secret-change-me", min_length=32
    )

    sandbox_image: str = "codex-sandbox-worker:0.3.0"
    docker_runtime: str = "runsc"
    rpc_network: str = "agent-rpc"
    egress_network: str = "agent-egress"
    resolv_conf_file: Path | None = None
    egress_proxy_url: str = "http://egress-proxy:3128"
    internal_no_proxy: str = ""
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

    @field_validator("docker_runtime", "rpc_network", "egress_network", "sandbox_image")
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

    @field_validator(
        "resolv_conf_file", "codex_auth_file", "codex_config_file", mode="before"
    )
    @classmethod
    def normalize_optional_path(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return Path(str(value)).expanduser().resolve()

    def internal_no_proxy_names(self) -> list[str]:
        return [name.strip() for name in self.internal_no_proxy.split(",") if name.strip()]
