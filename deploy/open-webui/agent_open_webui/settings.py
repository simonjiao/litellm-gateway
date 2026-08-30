from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class BridgeSettings:
    enabled: bool
    adapter_base_url: str
    adapter_api_key: str
    signing_secret: str
    artifact_base_url: str
    artifact_api_key: str
    grant_ttl_seconds: int
    terminal_grant_ttl_seconds: int
    max_file_bytes: int
    publish_reconcile_seconds: int
    publish_retry_limit: int

    @classmethod
    def from_env(cls) -> BridgeSettings:
        settings = cls(
            enabled=os.getenv("AGENT_WORKSPACE_ENABLED", "false").lower() == "true",
            adapter_base_url=os.getenv("AGENT_ADAPTER_BASE_URL", "http://adapter:8090").rstrip("/"),
            adapter_api_key=os.getenv("AGENT_ADAPTER_API_KEY", ""),
            signing_secret=os.getenv("AGENT_OPERATION_SIGNING_SECRET", ""),
            artifact_base_url=os.getenv(
                "AGENT_ARTIFACT_BASE_URL", "http://artifact-service:8093"
            ).rstrip("/"),
            artifact_api_key=os.getenv("AGENT_ARTIFACT_API_KEY", ""),
            grant_ttl_seconds=int(os.getenv("AGENT_GRANT_TTL_SECONDS", "120")),
            terminal_grant_ttl_seconds=int(
                os.getenv("AGENT_TERMINAL_GRANT_TTL_SECONDS", "7200")
            ),
            max_file_bytes=int(os.getenv("AGENT_MAX_FILE_BYTES", str(1024**3))),
            publish_reconcile_seconds=int(os.getenv("AGENT_PUBLISH_RECONCILE_SECONDS", "15")),
            publish_retry_limit=int(os.getenv("AGENT_PUBLISH_RETRY_LIMIT", "8")),
        )
        if settings.enabled:
            for name, value in (
                ("AGENT_ADAPTER_API_KEY", settings.adapter_api_key),
                ("AGENT_OPERATION_SIGNING_SECRET", settings.signing_secret),
                ("AGENT_ARTIFACT_API_KEY", settings.artifact_api_key),
            ):
                if len(value) < 32:
                    raise RuntimeError(f"{name} must contain at least 32 characters")
            for name, value in (
                ("AGENT_ADAPTER_BASE_URL", settings.adapter_base_url),
                ("AGENT_ARTIFACT_BASE_URL", settings.artifact_base_url),
            ):
                parsed = urlsplit(value)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    raise RuntimeError(f"{name} must be an absolute HTTP(S) URL")
            if not 30 <= settings.grant_ttl_seconds <= 900:
                raise RuntimeError("AGENT_GRANT_TTL_SECONDS must be between 30 and 900")
            if not 900 <= settings.terminal_grant_ttl_seconds <= 86400:
                raise RuntimeError(
                    "AGENT_TERMINAL_GRANT_TTL_SECONDS must be between 900 and 86400"
                )
            if settings.max_file_bytes <= 0:
                raise RuntimeError("AGENT_MAX_FILE_BYTES must be positive")
            if not 5 <= settings.publish_reconcile_seconds <= 300:
                raise RuntimeError("AGENT_PUBLISH_RECONCILE_SECONDS must be between 5 and 300")
            if not 1 <= settings.publish_retry_limit <= 32:
                raise RuntimeError("AGENT_PUBLISH_RETRY_LIMIT must be between 1 and 32")
        return settings


SETTINGS = BridgeSettings.from_env()
