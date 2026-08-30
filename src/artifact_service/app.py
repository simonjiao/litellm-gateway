from __future__ import annotations

# FastAPI registers decorated local handlers dynamically.
# pyright: reportUnusedFunction=false
import secrets
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from sandbox_api.grants import GrantError, issue_grant, verify_grant

from .models import (
    ArtifactDescriptor,
    CompleteUploadRequest,
    CreateDownloadRequest,
    CreateUploadRequest,
    Deleted,
    DownloadTarget,
    UploadTarget,
)
from .settings import ArtifactSettings
from .store import (
    ArtifactConflictError,
    ArtifactNotFoundError,
    ArtifactValidationError,
    S3ArtifactStore,
    iter_s3_body,
)


def create_app(
    settings: ArtifactSettings | None = None,
    *,
    store: S3ArtifactStore | None = None,
) -> FastAPI:
    runtime = settings or ArtifactSettings()  # pyright: ignore[reportCallIssue]
    artifact_store = store or S3ArtifactStore(runtime)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        app.state.store = artifact_store
        try:
            yield
        finally:
            await artifact_store.close()

    app = FastAPI(title="Artifact Service", version="0.3.0", lifespan=lifespan)

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Any):
        if request.url.path == "/healthz" or request.url.path.startswith("/v1/transfers/"):
            return await call_next(request)
        authorization = request.headers.get("authorization", "")
        expected = runtime.api_key.get_secret_value()
        if not authorization.startswith("Bearer ") or not secrets.compare_digest(
            authorization.removeprefix("Bearer ").encode(), expected.encode()
        ):
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        return await call_next(request)

    @app.exception_handler(ArtifactNotFoundError)
    async def not_found(_: Request, __: ArtifactNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "Artifact not found"})

    @app.exception_handler(ArtifactConflictError)
    async def conflict(_: Request, exc: ArtifactConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ArtifactValidationError)
    async def invalid(_: Request, exc: ArtifactValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/uploads", response_model=UploadTarget, status_code=201)
    async def create_upload(body: CreateUploadRequest) -> UploadTarget:
        if body.max_bytes > runtime.max_file_bytes:
            raise HTTPException(status_code=413, detail="Artifact exceeds the service limit")
        artifact_id = body.artifact_id or f"artifact_{uuid.uuid4().hex}"
        upload_id = f"upload_{uuid.uuid4().hex}"
        token = issue_grant(
            runtime.capability_secret.get_secret_value(),
            issuer="artifact-service",
            audience="artifact-transfer",
            operation="artifact_upload",
            expires_in=runtime.capability_ttl_seconds,
            artifact_id=artifact_id,
            upload_id=upload_id,
            owner_id=body.owner_id,
            filename=body.filename,
            media_type=body.media_type,
            max_bytes=body.max_bytes,
            expected_sha256=body.expected_sha256,
            subject_id=body.subject_id,
            app_id=body.app_id,
        )
        claims = verify_grant(
            token,
            runtime.capability_secret.get_secret_value(),
            audience="artifact-transfer",
            operation="artifact_upload",
        )
        return UploadTarget(
            artifact_id=artifact_id,
            upload_id=upload_id,
            url=(
                f"{runtime.transfer_base_url}/v1/transfers/uploads/"
                f"{artifact_id}/{upload_id}"
            ),
            token=token,
            expires_at=int(claims["exp"]),
        )

    @app.put(
        "/v1/transfers/uploads/{artifact_id}/{upload_id}",
        response_model=ArtifactDescriptor,
    )
    async def upload(artifact_id: str, upload_id: str, request: Request) -> ArtifactDescriptor:
        claims = _capability(request, runtime, "artifact_upload")
        if claims.get("artifact_id") != artifact_id or claims.get("upload_id") != upload_id:
            raise HTTPException(status_code=403, detail="Upload capability binding does not match")
        content_length = request.headers.get("content-length")
        max_bytes = claims.get("max_bytes")
        if not isinstance(max_bytes, int) or max_bytes <= 0:
            raise HTTPException(status_code=403, detail="Upload capability limit is invalid")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Content-Length is invalid") from exc
            if declared_length < 0:
                raise HTTPException(status_code=400, detail="Content-Length is invalid")
            if declared_length > max_bytes:
                raise HTTPException(status_code=413, detail="Artifact exceeds the upload limit")
        return await artifact_store.upload_and_commit(
            artifact_id,
            upload_id,
            request.stream(),
            owner_id=_claim_string(claims, "owner_id"),
            filename=_claim_string(claims, "filename"),
            media_type=_claim_string(claims, "media_type"),
            max_bytes=max_bytes,
            expected_sha256=(
                str(claims["expected_sha256"])
                if isinstance(claims.get("expected_sha256"), str)
                else None
            ),
        )

    @app.post(
        "/v1/uploads/{artifact_id}/complete",
        response_model=ArtifactDescriptor,
    )
    async def complete_upload(
        artifact_id: str, _: CompleteUploadRequest
    ) -> ArtifactDescriptor:
        return await artifact_store.inspect(artifact_id)

    @app.get("/v1/artifacts/{artifact_id}", response_model=ArtifactDescriptor)
    async def inspect(artifact_id: str) -> ArtifactDescriptor:
        return await artifact_store.inspect(artifact_id)

    @app.post(
        "/v1/artifacts/{artifact_id}/downloads",
        response_model=DownloadTarget,
        status_code=201,
    )
    async def create_download(
        artifact_id: str, body: CreateDownloadRequest
    ) -> DownloadTarget:
        descriptor = await artifact_store.inspect(artifact_id)
        token = issue_grant(
            runtime.capability_secret.get_secret_value(),
            issuer="artifact-service",
            audience="artifact-transfer",
            operation="artifact_download",
            expires_in=runtime.capability_ttl_seconds,
            artifact_id=artifact_id,
            subject_id=body.subject_id,
            app_id=body.app_id,
            max_bytes=descriptor.size,
        )
        claims = verify_grant(
            token,
            runtime.capability_secret.get_secret_value(),
            audience="artifact-transfer",
            operation="artifact_download",
        )
        return DownloadTarget(
            artifact_id=artifact_id,
            url=f"{runtime.transfer_base_url}/v1/transfers/downloads/{artifact_id}",
            token=token,
            expires_at=int(claims["exp"]),
        )

    @app.get("/v1/transfers/downloads/{artifact_id}")
    async def download(artifact_id: str, request: Request) -> StreamingResponse:
        claims = _capability(request, runtime, "artifact_download")
        if claims.get("artifact_id") != artifact_id:
            raise HTTPException(
                status_code=403, detail="Download capability binding does not match"
            )
        descriptor, body = await artifact_store.open_download(artifact_id)
        filename = quote(descriptor.filename, safe="")
        return StreamingResponse(
            iter_s3_body(body),
            media_type=descriptor.media_type,
            headers={
                "Content-Length": str(descriptor.size),
                "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
                "ETag": f'"{descriptor.sha256}"',
            },
        )

    @app.delete("/v1/artifacts/{artifact_id}", response_model=Deleted)
    async def delete(artifact_id: str) -> Deleted:
        return Deleted(artifact_id=artifact_id, deleted=await artifact_store.delete(artifact_id))

    return app


def _capability(request: Request, settings: ArtifactSettings, operation: str) -> dict[str, Any]:
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    try:
        claims = verify_grant(
            authorization.removeprefix("Bearer "),
            settings.capability_secret.get_secret_value(),
            audience="artifact-transfer",
            operation=operation,
        )
    except GrantError as exc:
        raise HTTPException(status_code=403, detail="Artifact capability is invalid") from exc
    if claims.get("iss") != "artifact-service" or int(claims.get("exp", 0)) <= int(time.time()):
        raise HTTPException(status_code=403, detail="Artifact capability is invalid")
    return claims


def _claim_string(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=403, detail=f"Artifact capability {name} is invalid")
    return value
