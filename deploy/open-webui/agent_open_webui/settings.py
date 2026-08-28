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
    internal_transfer_base_url: str
    grant_ttl_seconds: int
    max_file_bytes: int

    @classmethod
    def from_env(cls) -> BridgeSettings:
        settings = cls(
            enabled=os.getenv("AGENT_WORKSPACE_ENABLED", "false").lower() == "true",
            adapter_base_url=os.getenv("AGENT_ADAPTER_BASE_URL", "http://adapter:8090").rstrip("/"),
            adapter_api_key=os.getenv("AGENT_ADAPTER_API_KEY", ""),
            signing_secret=os.getenv("AGENT_OPERATION_SIGNING_SECRET", ""),
            internal_transfer_base_url=os.getenv(
                "AGENT_INTERNAL_TRANSFER_BASE_URL",
                "http://open-webui:8080/api/agent/transfer",
            ).rstrip("/"),
            grant_ttl_seconds=int(os.getenv("AGENT_GRANT_TTL_SECONDS", "120")),
            max_file_bytes=int(os.getenv("AGENT_MAX_FILE_BYTES", str(1024**3))),
        )
        if settings.enabled:
            for name, value in (
                ("AGENT_ADAPTER_API_KEY", settings.adapter_api_key),
                ("AGENT_OPERATION_SIGNING_SECRET", settings.signing_secret),
            ):
                if len(value) < 32:
                    raise RuntimeError(f"{name} must contain at least 32 characters")
            for name, value in (
                ("AGENT_ADAPTER_BASE_URL", settings.adapter_base_url),
                ("AGENT_INTERNAL_TRANSFER_BASE_URL", settings.internal_transfer_base_url),
            ):
                parsed = urlsplit(value)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    raise RuntimeError(f"{name} must be an absolute HTTP(S) URL")
            if not 30 <= settings.grant_ttl_seconds <= 900:
                raise RuntimeError("AGENT_GRANT_TTL_SECONDS must be between 30 and 900")
            if settings.max_file_bytes <= 0:
                raise RuntimeError("AGENT_MAX_FILE_BYTES must be positive")
        return settings


SETTINGS = BridgeSettings.from_env()
