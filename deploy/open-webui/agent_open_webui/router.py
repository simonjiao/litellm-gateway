from __future__ import annotations

import re
from typing import Annotated, Any

import anyio
import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse
from open_webui.models.chats import Chats
from open_webui.utils.auth import get_verified_user
from pydantic import BaseModel, ConfigDict, Field

from sandbox_api.grants import GrantError, verify_grant

from .database import consume_transfer_nonce, get_candidate_intent, get_response_binding
from .settings import SETTINGS
from .workspace import (
    _schedule_intent,
    advance_candidate,
    artifact_download_target,
    record_terminal_response,
    uploaded_file_artifact,
)

router = APIRouter()


class ArtifactPublishForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: str = Field(min_length=1, max_length=256)
    message_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    response_id: str = Field(pattern=r"^resp_[a-f0-9]{32}$")
    workspace_path: str = Field(min_length=1, max_length=4096)


class TerminalResponseForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_id: str = Field(pattern=r"^resp_[a-f0-9]{32}$")
    status: str = Field(pattern=r"^(completed|failed|incomplete)$")
    output_text: str = Field(max_length=2 * 1024 * 1024)
    grant: str = Field(min_length=16, max_length=64 * 1024)


@router.post("/events/response-terminal")
async def response_terminal(form: TerminalResponseForm) -> dict[str, Any]:
    claims = await _transfer_claims(form.grant, "response_terminal")
    intents = await record_terminal_response(
        response_id=form.response_id,
        response_status=form.status,
        output_text=form.output_text,
        claims=claims,
    )
    return {"accepted": True, "intent_ids": intents}


@router.post("/artifacts/publish")
async def publish_artifact(
    form: ArtifactPublishForm,
    user: Annotated[Any, Depends(get_verified_user)],
) -> dict[str, Any]:
    relative_path = _candidate_relative(form.workspace_path, form.message_id)
    return await advance_candidate(
        chat_id=form.chat_id,
        assistant_message_id=form.message_id,
        response_id=form.response_id,
        relative_path=relative_path,
        user=user,
    )


@router.get("/artifacts/{artifact_id}/download", response_model=None)
async def download_artifact(
    artifact_id: str,
    user: Annotated[Any, Depends(get_verified_user)],
    check: bool = False,
) -> StreamingResponse | JSONResponse:
    if re.fullmatch(r"artifact_[a-f0-9]{32}", artifact_id) is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    target = await artifact_download_target(artifact_id, user)
    if check:
        return JSONResponse(
            {"url": f"/api/agent/artifacts/{artifact_id}/download"},
            headers={"Cache-Control": "no-store"},
        )
    return await _proxy_download(target)


@router.get("/files/{file_id}/download", response_model=None)
async def uploaded_file_download(
    file_id: str, user: Annotated[Any, Depends(get_verified_user)], check: bool = False
) -> StreamingResponse | JSONResponse:
    artifact_id = await uploaded_file_artifact(file_id)
    if artifact_id is None:
        raise HTTPException(status_code=404, detail="File is unavailable")
    return await download_artifact(artifact_id, user, check)


@router.get("/candidates/download", response_model=None)
async def download_candidate(
    chat_id: str,
    message_id: str,
    path: str,
    user: Annotated[Any, Depends(get_verified_user)],
    check: bool = False,
) -> StreamingResponse | JSONResponse:
    if await Chats.get_chat_by_id_for_user(chat_id, user) is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    relative = _candidate_relative(path, message_id)
    binding = await get_response_binding(chat_id, message_id)
    intent = await get_candidate_intent(chat_id, message_id, relative)
    if binding is None or intent is None:
        raise HTTPException(status_code=409, detail="File is still being prepared")
    if intent["response_id"] != binding["response_id"]:
        raise HTTPException(status_code=404, detail="File is unavailable")
    if intent["state"] != "ready":
        if intent["state"] in {"failed", "expired"}:
            raise HTTPException(status_code=410, detail="File could not be prepared")
        _schedule_intent(str(intent["id"]))
        raise HTTPException(status_code=409, detail="File is still being prepared")
    return await download_artifact(str(intent["artifact_id"]), user, check)


async def _proxy_download(target: dict[str, Any]) -> StreamingResponse:
    url = target.get("url")
    token = target.get("token")
    if not isinstance(url, str) or not isinstance(token, str):
        raise HTTPException(status_code=502, detail="Artifact download target is invalid")
    client = httpx.AsyncClient(timeout=httpx.Timeout(3600, connect=10), trust_env=False)
    response = None
    try:
        request = client.build_request("GET", url, headers={"Authorization": f"Bearer {token}"})
        response = await client.send(request, stream=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        if response is not None:
            await response.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail="Artifact download failed") from exc

    async def content():
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            with anyio.CancelScope(shield=True):
                await response.aclose()
                await client.aclose()

    headers = {
        name: value
        for name in ("content-length", "content-disposition", "etag")
        if (value := response.headers.get(name)) is not None
    }
    headers["Cache-Control"] = "no-store"
    return StreamingResponse(
        content(),
        media_type=response.headers.get("content-type", "application/octet-stream"),
        headers=headers,
    )


async def _transfer_claims(token: str, operation: str) -> dict[str, Any]:
    try:
        claims = verify_grant(
            token,
            SETTINGS.signing_secret,
            audience="open-webui-transfer",
            operation=operation,
            max_lifetime=SETTINGS.terminal_grant_ttl_seconds,
        )
    except GrantError as exc:
        raise HTTPException(status_code=403, detail="Transfer grant is invalid") from exc
    if claims.get("iss") != "open-webui-bff":
        raise HTTPException(status_code=403, detail="Transfer issuer is invalid")
    nonce = claims.get("nonce")
    expires_at = claims.get("exp")
    if (
        not isinstance(nonce, str)
        or not isinstance(expires_at, int)
        or not await consume_transfer_nonce(nonce, expires_at)
    ):
        raise HTTPException(status_code=403, detail="Transfer grant was already used")
    return claims


def _candidate_relative(value: str, assistant_message_id: str) -> str:
    prefixes = (
        f"sandbox:/workspace/outputs/{assistant_message_id}/",
        f"/workspace/outputs/{assistant_message_id}/",
        f"outputs/{assistant_message_id}/",
    )
    relative = next(
        (value.removeprefix(prefix) for prefix in prefixes if value.startswith(prefix)),
        None,
    )
    if relative is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Candidate path is outside the current output directory",
        )
    parts = relative.split("/")
    if not relative or any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=422, detail="Candidate path is invalid")
    return relative
