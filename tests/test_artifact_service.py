from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Iterator
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr

from artifact_service.app import create_app
from artifact_service.models import ArtifactDescriptor
from artifact_service.settings import ArtifactSettings
from artifact_service.store import ArtifactNotFoundError, S3ArtifactStore


class _Body:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.closed = False

    def iter_chunks(self, chunk_size: int) -> Iterator[bytes]:
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]

    def close(self) -> None:
        self.closed = True


class _ArtifactStore:
    def __init__(self) -> None:
        self.descriptors: dict[str, ArtifactDescriptor] = {}
        self.contents: dict[str, bytes] = {}

    async def upload_and_commit(
        self,
        artifact_id: str,
        upload_id: str,
        body: AsyncIterator[bytes],
        **metadata: Any,
    ) -> ArtifactDescriptor:
        assert upload_id.startswith("upload_")
        content = b"".join([chunk async for chunk in body])
        digest = hashlib.sha256(content).hexdigest()
        assert digest == metadata["expected_sha256"]
        descriptor = ArtifactDescriptor(
            artifact_id=artifact_id,
            owner_id=metadata["owner_id"],
            filename=metadata["filename"],
            media_type=metadata["media_type"],
            size=len(content),
            sha256=digest,
            created_at=100,
        )
        self.descriptors[artifact_id] = descriptor
        self.contents[artifact_id] = content
        return descriptor

    async def inspect(self, artifact_id: str) -> ArtifactDescriptor:
        try:
            return self.descriptors[artifact_id]
        except KeyError as exc:
            raise ArtifactNotFoundError(artifact_id) from exc

    async def open_download(self, artifact_id: str) -> tuple[ArtifactDescriptor, _Body]:
        return await self.inspect(artifact_id), _Body(self.contents[artifact_id])

    async def delete(self, artifact_id: str) -> bool:
        existed = artifact_id in self.descriptors
        self.descriptors.pop(artifact_id, None)
        self.contents.pop(artifact_id, None)
        return existed

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_upload_manifest_and_capability_download_are_atomic() -> None:
    settings = ArtifactSettings(
        api_key=SecretStr("a" * 32),
        capability_secret=SecretStr("b" * 32),
        s3_endpoint_url="http://rustfs:9000",
        s3_access_key_id="business-key",
        s3_secret_access_key=SecretStr("business-secret"),
        s3_bucket="agent-data",
        transfer_base_url="http://test",
    )
    store = _ArtifactStore()
    app = create_app(settings, store=cast(S3ArtifactStore, store))
    service_headers = {"Authorization": f"Bearer {'a' * 32}"}
    content = b"immutable artifact\n"

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            missing = await client.get(
                "/v1/artifacts/artifact_00000000000000000000000000000000",
                headers=service_headers,
            )
            assert missing.status_code == 404

            created = await client.post(
                "/v1/uploads",
                headers=service_headers,
                json={
                    "owner_id": "user_one",
                    "filename": "result.txt",
                    "media_type": "text/plain",
                    "max_bytes": len(content),
                    "expected_sha256": hashlib.sha256(content).hexdigest(),
                    "subject_id": "user_one",
                },
            )
            assert created.status_code == 201, created.text
            target = created.json()

            wrong_binding = await client.put(
                "/v1/transfers/uploads/artifact_00000000000000000000000000000000/"
                f"{target['upload_id']}",
                headers={"Authorization": f"Bearer {target['token']}"},
                content=content,
            )
            assert wrong_binding.status_code == 403

            uploaded = await client.put(
                target["url"],
                headers={"Authorization": f"Bearer {target['token']}"},
                content=content,
            )
            assert uploaded.status_code == 200, uploaded.text
            descriptor = uploaded.json()
            assert descriptor["sha256"] == hashlib.sha256(content).hexdigest()

            inspected = await client.get(
                f"/v1/artifacts/{target['artifact_id']}", headers=service_headers
            )
            assert inspected.json() == descriptor

            download = await client.post(
                f"/v1/artifacts/{target['artifact_id']}/downloads",
                headers=service_headers,
                json={"subject_id": "user_one", "app_id": "mcp_app_one"},
            )
            downloaded = await client.get(
                download.json()["url"],
                headers={"Authorization": f"Bearer {download.json()['token']}"},
            )
            assert downloaded.status_code == 200
            assert downloaded.content == content
            assert "result.txt" in downloaded.headers["content-disposition"]

            deleted = await client.delete(
                f"/v1/artifacts/{target['artifact_id']}", headers=service_headers
            )
            assert deleted.json()["deleted"] is True


@pytest.mark.asyncio
async def test_control_api_rejects_capability_and_missing_service_auth() -> None:
    settings = ArtifactSettings(
        api_key=SecretStr("a" * 32),
        capability_secret=SecretStr("b" * 32),
        s3_endpoint_url="http://rustfs:9000",
        s3_access_key_id="business-key",
        s3_secret_access_key=SecretStr("business-secret"),
        s3_bucket="agent-data",
    )
    app = create_app(settings, store=cast(S3ArtifactStore, _ArtifactStore()))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/uploads",
                json={
                    "owner_id": "user_one",
                    "filename": "x",
                    "max_bytes": 1,
                },
            )
            assert response.status_code == 401
