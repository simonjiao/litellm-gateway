from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the Responses-to-Codex adapter."""

    model_config = SettingsConfigDict(
        env_prefix="CODEX_ADAPTER_",
        env_file=".env",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = Field(default=8090, ge=1, le=65535)
    api_key: str = Field(default="local-adapter-key", min_length=8)

    sandbox_manager_base_url: str = "http://sandbox-manager:8092"
    sandbox_manager_api_key: str = Field(default="local-sandbox-manager-key", min_length=8)
    agent_workspace: str = "/workspace"

    codex_model: str | None = None
    codex_ephemeral_threads: bool = False

    request_timeout_seconds: float = Field(default=3600.0, gt=0)
    max_concurrent_executions: int = Field(default=8, ge=1)
    sandbox_lease_renew_interval_seconds: float = Field(default=30.0, gt=0)

    mcp_apps_enabled: bool = True
    mcp_apps_public_base_url: str = "http://127.0.0.1:8090"
    mcp_apps_allowed_origins: str = "http://127.0.0.1:3000,http://localhost:3000"
    mcp_apps_interaction_timeout_seconds: float = Field(default=3600.0, gt=0)
    mcp_apps_event_keepalive_seconds: float = Field(default=15.0, gt=0)
    mcp_apps_max_event_history: int = Field(default=512, ge=16)
    mcp_apps_resource_max_bytes: int = Field(default=4 * 1024 * 1024, ge=1024)

    @field_validator("codex_model", mode="before")
    @classmethod
    def empty_model_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("mcp_apps_public_base_url")
    @classmethod
    def normalize_public_base_url(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @field_validator("sandbox_manager_base_url")
    @classmethod
    def normalize_sandbox_manager_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            raise ValueError("CODEX_ADAPTER_SANDBOX_MANAGER_BASE_URL must not be empty")
        return value

    @field_validator("agent_workspace")
    @classmethod
    def normalize_agent_workspace(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("/"):
            raise ValueError("CODEX_ADAPTER_AGENT_WORKSPACE must be an absolute path")
        return value

    def public_url(self, path: str) -> str:
        normalized = "/" + path.lstrip("/")
        if self.mcp_apps_public_base_url:
            return f"{self.mcp_apps_public_base_url}{normalized}"
        return normalized

    def allowed_origins(self) -> list[str]:
        values = [value.strip() for value in self.mcp_apps_allowed_origins.split(",")]
        values = [value for value in values if value]
        return values or ["*"]
