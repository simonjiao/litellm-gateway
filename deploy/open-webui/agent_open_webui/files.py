from __future__ import annotations

import re
import uuid
from typing import Any
from urllib.parse import unquote

import httpx
from fastapi import HTTPException, Request
from open_webui.models.config import Config
from open_webui.utils.access_control import has_permission

from .database import register_artifact_file
from .settings import SETTINGS
from .workspace import _artifact_request


async def upload_file(request: Request, user: Any) -> dict[str, Any]:
    if not SETTINGS.enabled:
        raise HTTPException(status_code=503, detail="File service is unavailable")
    if user.role != "admin" and not await has_permission(
        user.id, "chat.file_upload", await Config.get("user.permissions")
    ):
        raise HTTPException(status_code=403, detail="You do not have permission to upload files")
    try:
        size = int(request.headers["x-file-size"])
        name = unquote(request.headers["x-file-name"], errors="strict")
    except (KeyError, ValueError, UnicodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid file metadata") from exc
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    if not name or name in {".", ".."} or len(name) > 255 or any(ord(c) < 32 for c in name):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if size < 0 or size > SETTINGS.max_file_bytes:
        raise HTTPException(status_code=413, detail="File exceeds the upload limit")
    media_type = request.headers.get("x-file-type") or "application/octet-stream"
    if re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", media_type) is None:
        raise HTTPException(status_code=400, detail="Invalid file type")
    expected_sha256 = request.headers.get("x-file-sha256")
    if expected_sha256 is not None and re.fullmatch(r"[a-fA-F0-9]{64}", expected_sha256) is None:
        raise HTTPException(status_code=400, detail="Invalid file digest")
    target = await _artifact_request(
        "POST",
        "/v1/uploads",
        json_body={
            "owner_id": user.id,
            "subject_id": user.id,
            "filename": name,
            "media_type": media_type,
            "max_bytes": max(size, 1),
            "expected_sha256": expected_sha256.lower() if expected_sha256 else None,
        },
    )

    async def content():
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > size:
                raise HTTPException(status_code=422, detail="File size does not match")
            for offset in range(0, len(chunk), 64 * 1024):
                yield chunk[offset : offset + 64 * 1024]
        if received != size:
            raise HTTPException(status_code=422, detail="File size does not match")

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(3600, connect=10), trust_env=False
        ) as client:
            response = await client.put(
                target["url"],
                headers={"Authorization": f"Bearer {target['token']}", "Content-Type": media_type},
                content=content(),
            )
            response.raise_for_status()
            descriptor = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="File upload failed; please retry") from exc
    if (
        not isinstance(descriptor, dict)
        or descriptor.get("artifact_id") != target["artifact_id"]
        or descriptor.get("size") != size
    ):
        raise HTTPException(status_code=502, detail="File upload returned invalid data")
    try:
        result = await register_artifact_file(str(uuid.uuid4()), user.id, descriptor)
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="File metadata could not be saved; retry upload"
        ) from exc
    return {**result, "status": True}
