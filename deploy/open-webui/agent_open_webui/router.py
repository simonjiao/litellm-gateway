from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from open_webui.models.chats import Chats
from open_webui.models.files import Files
from open_webui.models.users import Users
from open_webui.routers.files import upload_file_handler
from open_webui.storage.provider import Storage
from open_webui.utils.access_control.files import has_access_to_file
from open_webui.utils.auth import get_verified_user
from pydantic import BaseModel, ConfigDict, Field

from sandbox_api.grants import GrantError, verify_grant

from .database import consume_transfer_nonce, get_workspace
from .settings import SETTINGS
from .workspace import _adapter_request, _grant, require_chat_write

router = APIRouter()


class ArtifactPublishForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: str = Field(min_length=1, max_length=256)
    message_id: str = Field(min_length=1, max_length=256)
    response_id: str = Field(pattern=r"^resp_[a-f0-9]{32}$")
    workspace_path: str = Field(min_length=1, max_length=4096)


@router.post("/artifacts/publish")
async def publish_artifact(
    form: ArtifactPublishForm,
    user: Annotated[Any, Depends(get_verified_user)],
):
    await require_chat_write(form.chat_id, user)
    message = await Chats.get_message_by_id_and_message_id(form.chat_id, form.message_id)
    if not message or message.get("role") != "assistant":
        raise HTTPException(status_code=404, detail="Assistant message not found")
    workspace_id = await get_workspace(form.chat_id)
    if workspace_id is None:
        raise HTTPException(status_code=409, detail="Chat has no Workspace")
    transfer_token = _grant(
        "file_write",
        audience="open-webui-transfer",
        workspace_id=workspace_id,
        chat_id=form.chat_id,
        message_id=form.message_id,
        response_id=form.response_id,
        user_id=user.id,
        workspace_path=form.workspace_path,
        max_bytes=SETTINGS.max_file_bytes,
    )
    operation_grant = _grant(
        "artifact_publish",
        workspace_id=workspace_id,
        workspace_path=form.workspace_path,
        max_bytes=SETTINGS.max_file_bytes,
        transfer_url=f"{SETTINGS.internal_transfer_base_url}/publish",
        transfer_token=transfer_token,
        idempotency_key=(f"publish:{form.chat_id}:{form.message_id}:{form.workspace_path}"),
    )
    return await _adapter_request(
        "POST",
        "/v1/artifacts/publish",
        json_body={"response_id": form.response_id, "grant": operation_grant},
    )


@router.get("/transfer/files/{file_id}")
async def transfer_file(file_id: str, request: Request):
    claims = await _transfer_claims(request, "file_read")
    if claims.get("file_id") != file_id:
        raise HTTPException(status_code=403, detail="Transfer binding does not match")
    user, _ = await _transfer_subject(claims)
    file = await Files.get_file_by_id(file_id)
    if file is None or not (
        file.user_id == user.id
        or user.role == "admin"
        or await has_access_to_file(file_id, "read", user)
    ):
        raise HTTPException(status_code=404, detail="File not found")
    if not file.path:
        raise HTTPException(status_code=404, detail="File content not found")
    resolved = Path(await asyncio.to_thread(Storage.get_file, file.path))
    file_size = await asyncio.to_thread(_regular_file_size, resolved)
    max_bytes = claims.get("max_bytes")
    if file_size is None or not isinstance(max_bytes, int) or file_size > max_bytes:
        raise HTTPException(status_code=413, detail="File exceeds transfer limit")
    return FileResponse(
        resolved,
        media_type=(file.meta or {}).get("content_type"),
        filename=(file.meta or {}).get("name") or file.filename,
    )


@router.post("/transfer/publish")
async def transfer_publish(
    request: Request,
    file: Annotated[UploadFile, File(...)],
):
    claims = await _transfer_claims(request, "file_write")
    user, workspace_id = await _transfer_subject(claims)
    message_id = _claim_string(claims, "message_id")
    workspace_path = _claim_string(claims, "workspace_path")
    if claims.get("workspace_id") != workspace_id:
        raise HTTPException(status_code=403, detail="Transfer binding does not match")
    message = await Chats.get_message_by_id_and_message_id(
        _claim_string(claims, "chat_id"), message_id
    )
    if not message or message.get("role") != "assistant":
        raise HTTPException(status_code=404, detail="Assistant message not found")
    max_bytes = claims.get("max_bytes")
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise HTTPException(status_code=403, detail="Transfer limit is invalid")
    actual_size = file.size
    if actual_size is None:
        await asyncio.to_thread(file.file.seek, 0, 2)
        actual_size = await asyncio.to_thread(file.file.tell)
        await asyncio.to_thread(file.file.seek, 0)
    if actual_size > max_bytes:
        raise HTTPException(status_code=413, detail="Artifact exceeds transfer limit")

    uploaded = await upload_file_handler(
        request,
        file,
        metadata={"source": "sandbox", "workspace_path": workspace_path},
        process=False,
        process_in_background=False,
        user=user,
        background_tasks=None,
        db=None,
    )
    file_id = getattr(uploaded, "id", None)
    if not isinstance(file_id, str):
        raise HTTPException(status_code=502, detail="Artifact storage returned invalid data")
    chat_id = _claim_string(claims, "chat_id")
    filename = getattr(uploaded, "filename", None) or Path(workspace_path).name
    download_url = f"/api/v1/files/{file_id}/content?attachment=true"
    file_entry = {
        "type": "file",
        "id": file_id,
        "name": filename,
        "url": download_url,
    }
    await Chats.insert_chat_files(chat_id, message_id, [file_id], user.id)
    attached = await Chats.add_message_files_by_id_and_message_id(chat_id, message_id, [file_entry])
    if attached is None:
        raise HTTPException(status_code=409, detail="Assistant message could not be updated")
    return {
        "file_id": file_id,
        "filename": filename,
        "download_url": download_url,
    }


async def _transfer_claims(request: Request, operation: str) -> dict[str, Any]:
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    try:
        claims = verify_grant(
            authorization.removeprefix("Bearer "),
            SETTINGS.signing_secret,
            audience="open-webui-transfer",
            operation=operation,
        )
    except GrantError as exc:
        raise HTTPException(status_code=403, detail="Transfer grant is invalid") from exc
    if claims.get("iss") != "open-webui-bff":
        raise HTTPException(status_code=403, detail="Transfer issuer is invalid")
    if not await consume_transfer_nonce(_claim_string(claims, "nonce"), claims["exp"]):
        raise HTTPException(status_code=403, detail="Transfer grant was already used")
    return claims


async def _transfer_subject(claims: dict[str, Any]) -> tuple[Any, str]:
    user_id = _claim_string(claims, "user_id")
    chat_id = _claim_string(claims, "chat_id")
    workspace_id = _claim_string(claims, "workspace_id")
    user = await Users.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=403, detail="Transfer user no longer exists")
    await require_chat_write(chat_id, user)
    if await get_workspace(chat_id) != workspace_id:
        raise HTTPException(status_code=403, detail="Transfer Workspace binding changed")
    return user, workspace_id


def _claim_string(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=403, detail=f"Transfer {name} is invalid")
    return value


def _regular_file_size(path: Path) -> int | None:
    return path.stat().st_size if path.is_file() else None
