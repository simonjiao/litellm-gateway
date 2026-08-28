from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException, status
from open_webui.models.chats import Chats
from open_webui.models.files import Files
from open_webui.utils.access_control.files import has_access_to_file

from sandbox_api.grants import issue_grant

from .database import delete_workspace, get_workspace, insert_workspace
from .settings import SETTINGS

log = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None
_chat_locks: dict[str, asyncio.Lock] = {}


async def startup_workspace_bridge() -> None:
    global _client
    if not SETTINGS.enabled:
        return
    from .database import create_tables

    await create_tables()
    _client = httpx.AsyncClient(timeout=httpx.Timeout(3600, connect=10), trust_env=False)


async def shutdown_workspace_bridge() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def inject_workspace_context(
    request: Any,
    user: Any,
    metadata: dict[str, Any] | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    del request
    if not SETTINGS.enabled or not isinstance(metadata, dict):
        return payload
    if metadata.get("internal") or metadata.get("task"):
        return payload
    chat_id = metadata.get("chat_id")
    if not isinstance(chat_id, str) or not chat_id or chat_id.startswith("channel:"):
        return payload
    if not metadata.get("user_message_id") or not metadata.get("assistant_message_id"):
        return payload

    await require_chat_write(chat_id, user)
    workspace_id = await ensure_chat_workspace(chat_id)
    previous_response_id = payload.get("previous_response_id")
    if previous_response_id:
        workspace_grant = _grant("workspace_inspect", workspace_id=workspace_id)
        sandbox_id = None
    else:
        sandbox_id = f"sandbox_{uuid.uuid4().hex}"
        workspace_grant = _grant(
            "sandbox_create",
            workspace_id=workspace_id,
            sandbox_id=sandbox_id,
        )

    checkout_grants, paths = await _checkout_grants(
        chat_id,
        workspace_id,
        sandbox_id,
        user,
        metadata,
    )
    response_metadata = payload.get("metadata")
    if not isinstance(response_metadata, dict):
        response_metadata = {}
    response_metadata["agent_workspace_grant"] = workspace_grant
    if checkout_grants:
        response_metadata["agent_checkout_grants"] = json.dumps(
            checkout_grants, separators=(",", ":")
        )
    payload["metadata"] = response_metadata
    if paths:
        attachment_note = "Attached files are available in the Workspace:\n" + "\n".join(
            f"- /workspace/{path}" for path in paths
        )
        instructions = payload.get("instructions")
        payload["instructions"] = (
            f"{instructions}\n\n{attachment_note}" if instructions else attachment_note
        )
    return payload


async def ensure_chat_workspace(chat_id: str) -> str:
    existing = await get_workspace(chat_id)
    if existing is not None:
        return existing
    lock = _chat_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        existing = await get_workspace(chat_id)
        if existing is not None:
            return existing
        workspace_id = f"workspace_{uuid.uuid4().hex}"
        await _adapter_request(
            "POST",
            "/v1/workspaces",
            json_body={
                "workspace_id": workspace_id,
                "grant": _grant("workspace_create", workspace_id=workspace_id),
            },
        )
        if await insert_workspace(chat_id, workspace_id):
            return workspace_id
        winner = await get_workspace(chat_id)
        await _release_workspace(workspace_id)
        if winner is None:
            raise HTTPException(status_code=503, detail="Workspace mapping could not be created")
        return winner


async def release_chat_workspace(chat_id: str) -> None:
    if not SETTINGS.enabled:
        return
    workspace_id = await get_workspace(chat_id)
    if workspace_id is None:
        return
    try:
        await _release_workspace(workspace_id)
    except Exception:
        log.exception("Failed to release Workspace for deleted chat %s", chat_id)
        return
    await delete_workspace(chat_id, workspace_id)


async def require_chat_write(chat_id: str, user: Any) -> Any:
    chat = await Chats.get_chat_by_id(chat_id)
    if chat is None or (chat.user_id != user.id and user.role != "admin"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found")
    return chat


async def _checkout_grants(
    chat_id: str,
    workspace_id: str,
    sandbox_id: str | None,
    user: Any,
    metadata: dict[str, Any],
) -> tuple[list[str], list[str]]:
    user_message = metadata.get("user_message")
    attachments = user_message.get("files", []) if isinstance(user_message, dict) else []
    grants: list[str] = []
    paths: list[str] = []
    seen: set[str] = set()
    for item in attachments if isinstance(attachments, list) else []:
        if not isinstance(item, dict) or item.get("type") != "file":
            continue
        file_id = item.get("id")
        if not isinstance(file_id, str) or file_id in seen:
            continue
        seen.add(file_id)
        file = await Files.get_file_by_id(file_id)
        if file is None or not (
            file.user_id == user.id
            or user.role == "admin"
            or await has_access_to_file(file_id, "read", user)
        ):
            raise HTTPException(status_code=404, detail="Attached file not found")
        size = (file.meta or {}).get("size")
        max_bytes = (
            min(size, SETTINGS.max_file_bytes) if isinstance(size, int) else SETTINGS.max_file_bytes
        )
        if max_bytes <= 0 or (isinstance(size, int) and size > SETTINGS.max_file_bytes):
            raise HTTPException(status_code=413, detail="Attached file is too large")
        filename = _safe_name((file.meta or {}).get("name") or file.filename)
        destination = f"uploads/{file_id}/{filename}"
        transfer_token = _grant(
            "file_read",
            audience="open-webui-transfer",
            workspace_id=workspace_id,
            chat_id=chat_id,
            user_id=user.id,
            file_id=file_id,
            max_bytes=max_bytes,
        )
        claims: dict[str, Any] = {
            "workspace_id": workspace_id,
            "file_id": file_id,
            "destination": destination,
            "max_bytes": max_bytes,
            "transfer_url": f"{SETTINGS.internal_transfer_base_url}/files/{file_id}",
            "transfer_token": transfer_token,
            "idempotency_key": (f"checkout:{chat_id}:{metadata.get('user_message_id')}:{file_id}"),
        }
        if sandbox_id is not None:
            claims["sandbox_id"] = sandbox_id
        digest = file.hash or (file.meta or {}).get("file_hash")
        if isinstance(digest, str) and re.fullmatch(r"[a-fA-F0-9]{64}", digest):
            claims["sha256"] = digest.lower()
        grants.append(_grant("artifact_checkout", **claims))
        paths.append(destination)
    return grants, paths


async def _release_workspace(workspace_id: str) -> None:
    await _adapter_request(
        "POST",
        f"/v1/workspaces/{workspace_id}/release",
        json_body={"grant": _grant("workspace_release", workspace_id=workspace_id)},
    )


async def _adapter_request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any],
) -> dict[str, Any]:
    if _client is None:
        raise HTTPException(status_code=503, detail="Workspace bridge is not ready")
    try:
        response = await _client.request(
            method,
            f"{SETTINGS.adapter_base_url}{path}",
            headers={"Authorization": f"Bearer {SETTINGS.adapter_api_key}"},
            json=json_body,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Workspace control is unavailable") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Workspace control rejected the request")
    value = response.json()
    if not isinstance(value, dict):
        raise HTTPException(status_code=502, detail="Workspace control returned invalid data")
    return value


def _grant(operation: str, *, audience: str = "sandbox-manager", **claims: Any) -> str:
    return issue_grant(
        SETTINGS.signing_secret,
        issuer="open-webui-bff",
        audience=audience,
        operation=operation,
        expires_in=SETTINGS.grant_ttl_seconds,
        **claims,
    )


def _safe_name(value: Any) -> str:
    name = Path(str(value or "file")).name
    normalized = re.sub(r"[^a-zA-Z0-9._-]", "_", name).strip("._")
    return (normalized or "file")[:160]
