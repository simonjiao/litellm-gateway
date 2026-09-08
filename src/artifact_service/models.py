from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ArtifactDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(pattern=r"^artifact_[a-f0-9]{32}$")
    owner_id: str = Field(min_length=1, max_length=256)
    filename: str = Field(
        min_length=1,
        max_length=512,
        pattern=r"^[^/\\\x00-\x20\x7f](?:[^/\\\x00-\x1f\x7f]{0,510}[^/\\\x00-\x20\x7f])?$",
    )
    media_type: str = Field(
        min_length=3,
        max_length=256,
        pattern=r"^[^\s/]+/[^\s/]+$",
    )
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: int


class CreateUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_id: str = Field(min_length=1, max_length=256)
    filename: str = Field(
        min_length=1,
        max_length=512,
        pattern=r"^[^/\\\x00-\x20\x7f](?:[^/\\\x00-\x1f\x7f]{0,510}[^/\\\x00-\x20\x7f])?$",
    )
    media_type: str = Field(
        default="application/octet-stream",
        min_length=3,
        max_length=256,
        pattern=r"^[^\s/]+/[^\s/]+$",
    )
    max_bytes: int = Field(gt=0)
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    artifact_id: str | None = Field(default=None, pattern=r"^artifact_[a-f0-9]{32}$")
    subject_id: str | None = Field(default=None, max_length=256)
    app_id: str | None = Field(default=None, max_length=256)


class UploadTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    upload_id: str
    method: str = "PUT"
    url: str
    token: str
    expires_at: int


class CompleteUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_id: str = Field(pattern=r"^upload_[a-f0-9]{32}$")


class CreateDownloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_id: str = Field(min_length=1, max_length=256)
    app_id: str | None = Field(default=None, max_length=256)


class DownloadTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    method: str = "GET"
    url: str
    token: str
    expires_at: int


class Deleted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    deleted: bool
