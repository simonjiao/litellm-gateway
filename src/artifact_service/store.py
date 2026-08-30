from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress
from typing import Any

import boto3  # pyright: ignore[reportMissingTypeStubs]
from botocore.config import Config  # pyright: ignore[reportMissingTypeStubs]
from botocore.exceptions import ClientError  # pyright: ignore[reportMissingTypeStubs]

from .models import ArtifactDescriptor
from .settings import ArtifactSettings

_PART_SIZE = 8 * 1024 * 1024


class ArtifactNotFoundError(LookupError):
    pass


class ArtifactConflictError(RuntimeError):
    pass


class ArtifactValidationError(RuntimeError):
    pass


class S3ArtifactStore:
    def __init__(self, settings: ArtifactSettings, *, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client or boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
            config=Config(s3={"addressing_style": "path"}),
        )

    async def upload_and_commit(
        self,
        artifact_id: str,
        upload_id: str,
        body: AsyncIterator[bytes],
        *,
        owner_id: str,
        filename: str,
        media_type: str,
        max_bytes: int,
        expected_sha256: str | None,
    ) -> ArtifactDescriptor:
        with suppress(ArtifactNotFoundError):
            existing = await self.inspect(artifact_id)
            _match_existing(existing, owner_id, filename, media_type, expected_sha256)
            return existing

        staging_key = self._key(f"staging/{artifact_id}/{upload_id}")
        multipart = await asyncio.to_thread(
            self._client.create_multipart_upload,
            Bucket=self._settings.s3_bucket,
            Key=staging_key,
            ContentType=media_type,
        )
        multipart_id = multipart.get("UploadId")
        if not isinstance(multipart_id, str) or not multipart_id:
            raise ArtifactConflictError("Object storage did not create a multipart upload")
        parts: list[dict[str, Any]] = []
        multipart_open = True
        buffer = bytearray()
        digest = hashlib.sha256()
        size = 0
        try:
            async for chunk in body:
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise ArtifactValidationError("Artifact exceeds the upload limit")
                digest.update(chunk)
                buffer.extend(chunk)
                while len(buffer) >= _PART_SIZE:
                    part = bytes(buffer[:_PART_SIZE])
                    del buffer[:_PART_SIZE]
                    parts.append(
                        await self._upload_part(staging_key, multipart_id, len(parts) + 1, part)
                    )
            if buffer:
                parts.append(
                    await self._upload_part(
                        staging_key, multipart_id, len(parts) + 1, bytes(buffer)
                    )
                )
            actual_sha256 = digest.hexdigest()
            if expected_sha256 is not None and actual_sha256 != expected_sha256:
                raise ArtifactValidationError("Artifact digest does not match")
            if parts:
                await asyncio.to_thread(
                    self._client.complete_multipart_upload,
                    Bucket=self._settings.s3_bucket,
                    Key=staging_key,
                    UploadId=multipart_id,
                    MultipartUpload={"Parts": parts},
                )
                multipart_open = False
            else:
                await asyncio.to_thread(
                    self._client.abort_multipart_upload,
                    Bucket=self._settings.s3_bucket,
                    Key=staging_key,
                    UploadId=multipart_id,
                )
                multipart_open = False
                await asyncio.to_thread(
                    self._client.put_object,
                    Bucket=self._settings.s3_bucket,
                    Key=staging_key,
                    Body=b"",
                    ContentType=media_type,
                )
        except BaseException:
            if multipart_open:
                with suppress(Exception):
                    await asyncio.to_thread(
                        self._client.abort_multipart_upload,
                        Bucket=self._settings.s3_bucket,
                        Key=staging_key,
                        UploadId=multipart_id,
                    )
            raise

        descriptor = ArtifactDescriptor(
            artifact_id=artifact_id,
            owner_id=owner_id,
            filename=filename,
            media_type=media_type,
            size=size,
            sha256=actual_sha256,
            created_at=int(time.time()),
        )
        try:
            return await self._commit(staging_key, descriptor)
        finally:
            with suppress(Exception):
                await asyncio.to_thread(
                    self._client.delete_object,
                    Bucket=self._settings.s3_bucket,
                    Key=staging_key,
                )

    async def inspect(self, artifact_id: str) -> ArtifactDescriptor:
        try:
            response = await asyncio.to_thread(
                self._client.get_object,
                Bucket=self._settings.s3_bucket,
                Key=self._manifest_key(artifact_id),
            )
            body = response["Body"]
            payload = await asyncio.to_thread(body.read)
            with suppress(Exception):
                body.close()
            return ArtifactDescriptor.model_validate_json(payload)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                raise ArtifactNotFoundError(artifact_id) from exc
            raise
        except (KeyError, ValueError) as exc:
            raise ArtifactConflictError("Artifact manifest is invalid") from exc

    async def open_download(self, artifact_id: str) -> tuple[ArtifactDescriptor, Any]:
        descriptor = await self.inspect(artifact_id)
        try:
            response = await asyncio.to_thread(
                self._client.get_object,
                Bucket=self._settings.s3_bucket,
                Key=self._content_key(artifact_id, descriptor.sha256),
            )
        except ClientError as exc:
            raise ArtifactConflictError("Committed Artifact content is missing") from exc
        return descriptor, response["Body"]

    async def delete(self, artifact_id: str) -> bool:
        try:
            descriptor = await self.inspect(artifact_id)
        except ArtifactNotFoundError:
            return False
        await asyncio.to_thread(
            self._client.delete_objects,
            Bucket=self._settings.s3_bucket,
            Delete={
                "Objects": [
                    {"Key": self._manifest_key(artifact_id)},
                    {"Key": self._content_key(artifact_id, descriptor.sha256)},
                ],
                "Quiet": True,
            },
        )
        return True

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            await asyncio.to_thread(close)

    async def _upload_part(
        self, key: str, upload_id: str, part_number: int, content: bytes
    ) -> dict[str, Any]:
        response = await asyncio.to_thread(
            self._client.upload_part,
            Bucket=self._settings.s3_bucket,
            Key=key,
            UploadId=upload_id,
            PartNumber=part_number,
            Body=content,
        )
        etag = response.get("ETag")
        if not isinstance(etag, str):
            raise ArtifactConflictError("Object storage did not return an ETag")
        return {"ETag": etag, "PartNumber": part_number}

    async def _commit(
        self, staging_key: str, descriptor: ArtifactDescriptor
    ) -> ArtifactDescriptor:
        with suppress(ArtifactNotFoundError):
            existing = await self.inspect(descriptor.artifact_id)
            _match_existing(
                existing,
                descriptor.owner_id,
                descriptor.filename,
                descriptor.media_type,
                descriptor.sha256,
            )
            return existing
        await asyncio.to_thread(
            self._client.copy_object,
            Bucket=self._settings.s3_bucket,
            Key=self._content_key(descriptor.artifact_id, descriptor.sha256),
            CopySource={"Bucket": self._settings.s3_bucket, "Key": staging_key},
            ContentType=descriptor.media_type,
            MetadataDirective="REPLACE",
        )
        manifest = descriptor.model_dump_json().encode()
        try:
            await asyncio.to_thread(
                self._client.put_object,
                Bucket=self._settings.s3_bucket,
                Key=self._manifest_key(descriptor.artifact_id),
                Body=manifest,
                ContentType="application/json",
                IfNoneMatch="*",
            )
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code", "")) not in {
                "412",
                "PreconditionFailed",
            }:
                raise
            existing = await self.inspect(descriptor.artifact_id)
            try:
                _match_existing(
                    existing,
                    descriptor.owner_id,
                    descriptor.filename,
                    descriptor.media_type,
                    descriptor.sha256,
                )
            except ArtifactConflictError:
                if existing.sha256 != descriptor.sha256:
                    with suppress(Exception):
                        await asyncio.to_thread(
                            self._client.delete_object,
                            Bucket=self._settings.s3_bucket,
                            Key=self._content_key(
                                descriptor.artifact_id, descriptor.sha256
                            ),
                        )
                raise
            return existing
        return descriptor

    def _key(self, suffix: str) -> str:
        return f"{self._settings.s3_prefix}/{suffix}"

    def _manifest_key(self, artifact_id: str) -> str:
        return self._key(f"manifests/{artifact_id}.json")

    def _content_key(self, artifact_id: str, sha256: str) -> str:
        return self._key(f"content/{artifact_id}/{sha256}")


def iter_s3_body(body: Any) -> Iterator[bytes]:
    try:
        yield from body.iter_chunks(chunk_size=1024 * 1024)
    finally:
        with suppress(Exception):
            body.close()


def _match_existing(
    descriptor: ArtifactDescriptor,
    owner_id: str,
    filename: str,
    media_type: str,
    expected_sha256: str | None,
) -> None:
    if (
        descriptor.owner_id != owner_id
        or descriptor.filename != filename
        or descriptor.media_type != media_type
        or (expected_sha256 is not None and descriptor.sha256 != expected_sha256)
    ):
        raise ArtifactConflictError("Artifact id is already committed with different metadata")
