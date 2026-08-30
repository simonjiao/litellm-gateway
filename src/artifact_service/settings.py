from __future__ import annotations

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ArtifactSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARTIFACT_SERVICE_", extra="ignore")

    host: str = "0.0.0.0"
    port: int = Field(default=8093, ge=1, le=65535)
    api_key: SecretStr = Field(min_length=32)
    capability_secret: SecretStr = Field(min_length=32)
    capability_ttl_seconds: int = Field(default=300, ge=30, le=900)
    max_file_bytes: int = Field(default=1024**3, gt=0, le=10 * 1024**3)

    s3_endpoint_url: str
    s3_region: str = "us-east-1"
    s3_access_key_id: str = Field(min_length=1)
    s3_secret_access_key: SecretStr
    s3_bucket: str = Field(min_length=1)
    s3_prefix: str = "artifacts"
    transfer_base_url: str = "http://artifact-service:8093"

    @field_validator("s3_endpoint_url", "transfer_base_url")
    @classmethod
    def absolute_http_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("must be an absolute HTTP(S) URL")
        return normalized

    @field_validator("s3_prefix")
    @classmethod
    def normalized_prefix(cls, value: str) -> str:
        normalized = value.strip(" /")
        if not normalized or ".." in normalized.split("/"):
            raise ValueError("must be a non-empty object prefix")
        return normalized
