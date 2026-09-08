from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from artifact_service.models import ArtifactDescriptor
from artifact_service.settings import ArtifactSettings
from artifact_service.store import ArtifactConflictError, S3ArtifactStore


class _Body:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def close(self) -> None:
        return None


class _DeleteDeniedClient:
    def __init__(self, descriptor: ArtifactDescriptor) -> None:
        self._descriptor = descriptor

    def get_object(self, **_: Any) -> dict[str, Any]:
        return {"Body": _Body(self._descriptor.model_dump_json().encode())}

    def delete_objects(self, **_: Any) -> dict[str, Any]:
        return {"Errors": [{"Code": "AccessDenied", "Message": "Access Denied"}]}


class _ReadinessClient:
    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.bucket: str | None = None

    def head_bucket(self, *, Bucket: str) -> dict[str, Any]:
        self.bucket = Bucket
        if self.fails:
            raise RuntimeError("unavailable")
        return {}


@pytest.mark.asyncio
async def test_delete_rejects_object_level_s3_errors() -> None:
    descriptor = ArtifactDescriptor(
        artifact_id="artifact_00000000000000000000000000000000",
        owner_id="user_one",
        filename="result.txt",
        media_type="text/plain",
        size=1,
        sha256="0" * 64,
        created_at=1,
    )
    settings = ArtifactSettings(
        api_key=SecretStr("a" * 32),
        capability_secret=SecretStr("b" * 32),
        s3_endpoint_url="http://rustfs:9000",
        s3_access_key_id="business-key",
        s3_secret_access_key=SecretStr("business-secret"),
        s3_bucket="agent-data",
    )
    store = S3ArtifactStore(settings, client=_DeleteDeniedClient(descriptor))

    with pytest.raises(ArtifactConflictError, match="rejected Artifact deletion"):
        await store.delete(descriptor.artifact_id)


@pytest.mark.asyncio
async def test_readiness_checks_the_configured_bucket_without_leaking_errors() -> None:
    settings = ArtifactSettings(
        api_key=SecretStr("a" * 32),
        capability_secret=SecretStr("b" * 32),
        s3_endpoint_url="http://rustfs:9000",
        s3_access_key_id="business-key",
        s3_secret_access_key=SecretStr("business-secret"),
        s3_bucket="agent-data",
    )
    client = _ReadinessClient()
    store = S3ArtifactStore(settings, client=client)
    assert await store.ready() is True
    assert client.bucket == "agent-data"

    unavailable = S3ArtifactStore(settings, client=_ReadinessClient(fails=True))
    assert await unavailable.ready() is False
