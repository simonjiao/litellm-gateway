from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException, status
from open_webui.models.chats import Chats
from open_webui.models.files import Files
from open_webui.models.users import Users
from open_webui.storage.provider import Storage
from open_webui.utils.access_control.files import has_access_to_file

from sandbox_api.artifact_refs import sandbox_candidates
from sandbox_api.grants import issue_grant

from .database import (
    bind_message_artifact,
    capture_barriers,
    create_publish_intent,
    create_tables,
    delete_workspace,
    due_publish_intents,
    get_candidate_intent,
    get_file_artifact,
    get_publish_intent,
    get_response_binding,
    get_workspace,
    insert_workspace,
    put_file_artifact,
    put_response_binding,
    update_publish_intent,
)
from .settings import SETTINGS

log = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None
_reconcile_task: asyncio.Task[None] | None = None
_intent_tasks: dict[str, asyncio.Task[None]] = {}
_intent_locks: dict[str, asyncio.Lock] = {}
_chat_locks: dict[str, asyncio.Lock] = {}
async def startup_workspace_bridge() -> None:
    global _client, _reconcile_task
    if not SETTINGS.enabled:
        return
    await create_tables()
    _client = httpx.AsyncClient(timeout=httpx.Timeout(3600, connect=10), trust_env=False)
    _reconcile_task = asyncio.create_task(_publish_reconciler(), name="artifact-publish-reconciler")


async def shutdown_workspace_bridge() -> None:
    global _client, _reconcile_task
    if _reconcile_task is not None:
        _reconcile_task.cancel()
        with suppress(asyncio.CancelledError):
            await _reconcile_task
        _reconcile_task = None
    for task in tuple(_intent_tasks.values()):
        task.cancel()
    for task in tuple(_intent_tasks.values()):
        with suppress(asyncio.CancelledError, Exception):
            await task
    _intent_tasks.clear()
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
    user_message_id = metadata.get("user_message_id")
    assistant_message_id = metadata.get("assistant_message_id") or metadata.get("message_id")
    if (
        not isinstance(chat_id, str)
        or not chat_id
        or chat_id.startswith("channel:")
        or not _valid_message_id(user_message_id)
        or not _valid_message_id(assistant_message_id)
    ):
        return payload

    await require_chat_write(chat_id, user)
    await _require_message(chat_id, str(user_message_id), "user")
    assistant = await Chats.get_message_by_id_and_message_id(chat_id, str(assistant_message_id))
    if assistant is not None and assistant.get("role") != "assistant":
        raise HTTPException(status_code=409, detail="Assistant message binding is invalid")
    existing_workspace_id = await get_workspace(chat_id)
    workspace_id = existing_workspace_id or await ensure_chat_workspace(chat_id)
    await _await_capture_barriers(workspace_id)

    previous_response_id = payload.get("previous_response_id")
    if not previous_response_id:
        user_message = metadata.get("user_message")
        parent_message_id = (
            user_message.get("parentId") if isinstance(user_message, dict) else None
        )
        if _valid_message_id(parent_message_id):
            await _require_message(chat_id, str(parent_message_id), "assistant")
            binding = await get_response_binding(chat_id, str(parent_message_id))
            if binding is None and existing_workspace_id is not None:
                binding = await _await_response_binding(chat_id, str(parent_message_id))
            if binding is not None:
                if (
                    binding["workspace_id"] != workspace_id
                    or binding["owner_user_id"] != user.id
                ):
                    raise HTTPException(
                        status_code=409, detail="Previous Response binding changed"
                    )
                previous_response_id = binding["response_id"]
                payload["previous_response_id"] = previous_response_id
            elif existing_workspace_id is not None:
                raise HTTPException(
                    status_code=409, detail="Previous Response binding is not ready"
                )
    if previous_response_id:
        workspace_grant = _grant("workspace_inspect", workspace_id=workspace_id)
        sandbox_id = None
    else:
        sandbox_id = f"sandbox_{uuid.uuid4().hex}"
        workspace_grant = _grant(
            "sandbox_create", workspace_id=workspace_id, sandbox_id=sandbox_id
        )

    checkout_grant, paths = await _checkout_grant(
        chat_id,
        workspace_id,
        sandbox_id,
        user,
        metadata,
        str(user_message_id),
        str(assistant_message_id),
    )
    publish_grant = _grant(
        "response_terminal",
        audience="open-webui-transfer",
        expires_in=SETTINGS.terminal_grant_ttl_seconds,
        chat_id=chat_id,
        workspace_id=workspace_id,
        user_id=user.id,
        user_message_id=str(user_message_id),
        assistant_message_id=str(assistant_message_id),
    )
    response_metadata = payload.get("metadata")
    if not isinstance(response_metadata, dict):
        response_metadata = {}
    response_metadata["agent_workspace_grant"] = workspace_grant
    response_metadata["agent_publish_grant"] = publish_grant
    response_metadata["agent_checkout_grant"] = checkout_grant
    payload["metadata"] = response_metadata

    output_path = f"/workspace/outputs/{assistant_message_id}"
    notes = [
        "Use /workspace/work as the working directory.",
        f"Write files for this answer under {output_path}.",
        f"Reference generated files as sandbox:{output_path}/<relative_path>.",
    ]
    if paths:
        notes.append("Attached files are read-only:")
        notes.extend(f"- /workspace/{path}" for path in paths)
    note = "\n".join(notes)
    instructions = payload.get("instructions")
    payload["instructions"] = f"{instructions}\n\n{note}" if instructions else note
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
    chat = await Chats.get_chat_by_id_for_user(chat_id, user)
    if chat is None or (chat.user_id != user.id and user.role != "admin"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found")
    return chat


async def register_uploaded_file(request: Any, user: Any, uploaded: Any) -> None:
    del request
    if not SETTINGS.enabled:
        return
    file_id = uploaded.get("id") if isinstance(uploaded, dict) else getattr(uploaded, "id", None)
    if not isinstance(file_id, str):
        raise HTTPException(status_code=502, detail="Uploaded file has no stable id")
    file = await Files.get_file_by_id(file_id)
    if file is None:
        raise HTTPException(status_code=502, detail="Uploaded file could not be inspected")
    await _ensure_file_artifact(file, user)


async def record_terminal_response(
    *,
    response_id: str,
    response_status: str,
    output_text: str,
    claims: dict[str, Any],
) -> list[str]:
    chat_id = _claim_string(claims, "chat_id")
    workspace_id = _claim_string(claims, "workspace_id")
    user_id = _claim_string(claims, "user_id")
    assistant_message_id = _claim_string(claims, "assistant_message_id")
    if await get_workspace(chat_id) != workspace_id:
        raise HTTPException(status_code=403, detail="Terminal Workspace binding changed")
    user = await Users.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=403, detail="Terminal user no longer exists")
    await require_chat_write(chat_id, user)
    message = await Chats.get_message_by_id_and_message_id(chat_id, assistant_message_id)
    if message is not None and message.get("role") != "assistant":
        raise HTTPException(status_code=409, detail="Terminal assistant message is invalid")
    await put_response_binding(
        chat_id=chat_id,
        assistant_message_id=assistant_message_id,
        response_id=response_id,
        workspace_id=workspace_id,
        owner_user_id=user_id,
    )
    if response_status != "completed":
        return []
    intent_ids: list[str] = []
    for relative_path in sandbox_candidates(output_text, assistant_message_id):
        intent_id = await create_publish_intent(
            chat_id=chat_id,
            owner_user_id=user_id,
            workspace_id=workspace_id,
            assistant_message_id=assistant_message_id,
            response_id=response_id,
            output_relative_path=relative_path,
        )
        intent_ids.append(intent_id)
        _schedule_intent(intent_id)
    return intent_ids


async def advance_candidate(
    *, chat_id: str, assistant_message_id: str, response_id: str, relative_path: str, user: Any
) -> dict[str, Any]:
    await require_chat_write(chat_id, user)
    await _require_message(chat_id, assistant_message_id, "assistant")
    workspace_id = await get_workspace(chat_id)
    if workspace_id is None:
        raise HTTPException(status_code=409, detail="Chat has no Workspace")
    await put_response_binding(
        chat_id=chat_id,
        assistant_message_id=assistant_message_id,
        response_id=response_id,
        workspace_id=workspace_id,
        owner_user_id=user.id,
    )
    intent = await get_candidate_intent(chat_id, assistant_message_id, relative_path)
    if intent is None:
        intent_id = await create_publish_intent(
            chat_id=chat_id,
            owner_user_id=user.id,
            workspace_id=workspace_id,
            assistant_message_id=assistant_message_id,
            response_id=response_id,
            output_relative_path=relative_path,
        )
    else:
        if intent["response_id"] != response_id:
            raise HTTPException(status_code=409, detail="Candidate Response binding changed")
        intent_id = str(intent["id"])
        if intent["state"] == "retryable":
            await update_publish_intent(intent_id, next_attempt_at=int(time.time()))
    task = _schedule_intent(intent_id)
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=30)
    current = await get_publish_intent(intent_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Publish intent not found")
    return current


async def artifact_download_target(artifact_id: str, user: Any) -> dict[str, Any]:
    from .database import get_file_by_artifact, message_artifact_chats

    file_binding = await get_file_by_artifact(artifact_id)
    if file_binding is not None:
        file_id = str(file_binding["file_id"])
        file = await Files.get_file_by_id(file_id)
        if file is not None and (
            file.user_id == user.id
            or user.role == "admin"
            or await has_access_to_file(file_id, "read", user)
        ):
            return await _artifact_request(
                "POST",
                f"/v1/artifacts/{artifact_id}/downloads",
                json_body={"subject_id": user.id},
            )
    for chat_id in await message_artifact_chats(artifact_id):
        if await Chats.get_chat_by_id_for_user(chat_id, user) is not None:
            return await _artifact_request(
                "POST",
                f"/v1/artifacts/{artifact_id}/downloads",
                json_body={"subject_id": user.id},
            )
    raise HTTPException(status_code=404, detail="Artifact not found")


async def uploaded_file_artifact(file_id: str) -> str | None:
    binding = await get_file_artifact(file_id)
    return str(binding["artifact_id"]) if binding is not None else None


async def _checkout_grant(
    chat_id: str,
    workspace_id: str,
    sandbox_id: str | None,
    user: Any,
    metadata: dict[str, Any],
    user_message_id: str,
    assistant_message_id: str,
) -> tuple[str, list[str]]:
    user_message = metadata.get("user_message")
    attachments = user_message.get("files", []) if isinstance(user_message, dict) else []
    artifacts: list[dict[str, Any]] = []
    paths: list[str] = []
    names: set[str] = set()
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
        descriptor = await _ensure_file_artifact(file, user)
        size = descriptor.get("size")
        if not isinstance(size, int) or size > SETTINGS.max_file_bytes:
            raise HTTPException(status_code=413, detail="Attached file is too large")
        filename = _unique_name(_safe_name(descriptor.get("filename") or file.filename), names)
        target = await _artifact_request(
            "POST",
            f"/v1/artifacts/{descriptor['artifact_id']}/downloads",
            json_body={"subject_id": user.id},
        )
        artifacts.append(
            {
                "artifact_id": descriptor["artifact_id"],
                "filename": filename,
                "size": size,
                "sha256": descriptor["sha256"],
                "max_bytes": max(size, 1),
                "url": target["url"],
                "token": target["token"],
            }
        )
        paths.append(f"uploads/{user_message_id}/{filename}")
        await bind_message_artifact(
            chat_id,
            user_message_id,
            str(descriptor["artifact_id"]),
            "upload",
            filename,
        )
    claims: dict[str, Any] = {
        "workspace_id": workspace_id,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "artifacts": artifacts,
        "idempotency_key": f"checkout:{chat_id}:{user_message_id}",
    }
    if sandbox_id is not None:
        claims["sandbox_id"] = sandbox_id
    return _grant("artifact_checkout", **claims), paths


async def _ensure_file_artifact(file: Any, user: Any) -> dict[str, Any]:
    existing = await get_file_artifact(file.id)
    if existing is not None:
        return json.loads(str(existing["descriptor_json"]))
    if file.user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="File owner cannot be delegated")
    metadata = file.meta or {}
    size = metadata.get("size")
    digest = file.hash or metadata.get("file_hash")
    if not isinstance(size, int) or not 0 <= size <= SETTINGS.max_file_bytes:
        raise HTTPException(status_code=413, detail="File exceeds the Artifact limit")
    path = Path(await asyncio.to_thread(Storage.get_file, file.path))
    actual_size = await asyncio.to_thread(_regular_file_size, path)
    if actual_size != size:
        raise HTTPException(status_code=409, detail="Uploaded file content is unavailable")
    if not isinstance(digest, str) or re.fullmatch(r"[a-fA-F0-9]{64}", digest) is None:
        digest = await asyncio.to_thread(_sha256_file, path)
    target = await _artifact_request(
        "POST",
        "/v1/uploads",
        json_body={
            "owner_id": file.user_id,
            "filename": _safe_name(metadata.get("name") or file.filename),
            "media_type": metadata.get("content_type") or "application/octet-stream",
            "max_bytes": max(size, 1),
            "expected_sha256": digest.lower(),
            "subject_id": user.id,
        },
    )
    if _client is None:
        raise HTTPException(status_code=503, detail="Artifact bridge is not ready")
    stream = await asyncio.to_thread(path.open, "rb")
    try:
        response = await _client.put(
            str(target["url"]),
            headers={
                "Authorization": f"Bearer {target['token']}",
                "Content-Type": metadata.get("content_type") or "application/octet-stream",
                "Content-Length": str(size),
            },
            content=_async_file_chunks(stream),
        )
        response.raise_for_status()
        descriptor = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Artifact upload failed") from exc
    finally:
        await asyncio.to_thread(stream.close)
    if not isinstance(descriptor, dict) or descriptor.get("artifact_id") != target["artifact_id"]:
        raise HTTPException(status_code=502, detail="Artifact upload returned invalid data")
    await _artifact_request(
        "POST",
        f"/v1/uploads/{descriptor['artifact_id']}/complete",
        json_body={"upload_id": target["upload_id"]},
    )
    await put_file_artifact(
        file.id,
        str(descriptor["artifact_id"]),
        file.user_id,
        json.dumps(descriptor, sort_keys=True, separators=(",", ":")),
    )
    return descriptor


async def _drive_intent(intent_id: str) -> None:
    lock = _intent_locks.setdefault(intent_id, asyncio.Lock())
    async with lock:
        intent = await get_publish_intent(intent_id)
        if intent is None or intent["state"] in {"ready", "failed"}:
            return
        try:
            if intent["state"] in {"uploaded", "binding_retry"}:
                await _bind_published(intent)
                return
            if intent["operation_id"] and intent["state"] == "uploading":
                operation = await _wait_publish_operation(str(intent["operation_id"]))
                await _finish_publish_operation(intent, operation)
                return
            if intent["state"] == "pending":
                await _capture_intent(intent)
                intent = await get_publish_intent(intent_id) or intent
                if intent["state"] != "captured":
                    return
            if intent["state"] in {"captured", "retryable"}:
                await _upload_intent(intent)
        except HTTPException as exc:
            current = await get_publish_intent(intent_id) or intent
            if current["state"] == "pending":
                await _retry_capture_intent(current, str(exc.detail))
            else:
                await _retry_intent(current, str(exc.detail))
        except Exception as exc:
            log.exception("Failed to advance publish intent %s", intent_id)
            current = await get_publish_intent(intent_id) or intent
            if current["state"] == "pending":
                await _retry_capture_intent(current, str(exc))
            else:
                await _retry_intent(current, str(exc))


async def _capture_intent(intent: dict[str, Any]) -> None:
    grant = _grant(
        "artifact_publish",
        workspace_id=intent["workspace_id"],
        assistant_message_id=intent["assistant_message_id"],
        response_id=intent["response_id"],
        output_relative_path=intent["output_relative_path"],
        max_bytes=SETTINGS.max_file_bytes,
        idempotency_key=f"publish:{intent['id']}",
    )
    operation = await _adapter_request(
        "POST",
        "/v1/artifacts/publish",
        json_body={"response_id": intent["response_id"], "grant": grant},
    )
    operation_id = operation.get("id")
    if not isinstance(operation_id, str):
        raise HTTPException(status_code=502, detail="Publish operation has no id")
    if operation.get("phase") in {"captured", "uploading"}:
        await update_publish_intent(
            intent["id"], operation_id=operation_id, state="captured", error=None
        )
        return
    await update_publish_intent(intent["id"], operation_id=operation_id)
    if operation.get("status") == "failed":
        await update_publish_intent(
            intent["id"],
            state="failed",
            error=str(operation.get("error") or "Artifact capture failed")[:1000],
        )


async def _upload_intent(intent: dict[str, Any]) -> None:
    attempts = int(intent["attempts"])
    if attempts >= SETTINGS.publish_retry_limit:
        await update_publish_intent(intent["id"], state="failed", error="Retry limit reached")
        return
    user = await Users.get_user_by_id(str(intent["owner_user_id"]))
    if user is None:
        await update_publish_intent(intent["id"], state="failed", error="Owner no longer exists")
        return
    await require_chat_write(str(intent["chat_id"]), user)
    artifact_id = intent["artifact_id"]
    filename = Path(str(intent["output_relative_path"])).name
    target_body: dict[str, Any] = {
        "owner_id": intent["owner_user_id"],
        "filename": filename,
        "media_type": "application/octet-stream",
        "max_bytes": SETTINGS.max_file_bytes,
        "subject_id": intent["owner_user_id"],
    }
    if isinstance(artifact_id, str):
        target_body["artifact_id"] = artifact_id
    target = await _artifact_request("POST", "/v1/uploads", json_body=target_body)
    artifact_id = str(target["artifact_id"])
    grant = _grant(
        "artifact_publish",
        workspace_id=intent["workspace_id"],
        assistant_message_id=intent["assistant_message_id"],
        response_id=intent["response_id"],
        output_relative_path=intent["output_relative_path"],
        artifact_id=artifact_id,
        upload_url=target["url"],
        upload_token=target["token"],
        max_bytes=SETTINGS.max_file_bytes,
        idempotency_key=f"publish:{intent['id']}",
    )
    await update_publish_intent(
        intent["id"],
        artifact_id=artifact_id,
        attempts=attempts + 1,
        next_attempt_at=int(time.time()) + SETTINGS.publish_reconcile_seconds,
        error=None,
    )
    operation = await _adapter_request(
        "POST",
        "/v1/artifacts/publish",
        json_body={"response_id": intent["response_id"], "grant": grant},
    )
    operation_id = operation.get("id")
    if not isinstance(operation_id, str):
        raise HTTPException(status_code=502, detail="Publish operation has no id")
    await update_publish_intent(intent["id"], operation_id=operation_id, state="uploading")
    if operation.get("status") in {"pending", "running"}:
        operation = await _wait_publish_operation(operation_id)
    current = await get_publish_intent(str(intent["id"]))
    if current is not None:
        await _finish_publish_operation(current, operation)


async def _wait_publish_operation(operation_id: str) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + 3600
    while True:
        operation = await _adapter_request(
            "GET", f"/v1/artifacts/operations/{operation_id}", json_body=None
        )
        if operation.get("status") not in {"pending", "running"}:
            return operation
        if asyncio.get_running_loop().time() >= deadline:
            raise HTTPException(status_code=504, detail="Artifact upload timed out")
        await asyncio.sleep(0.25)


async def _finish_publish_operation(intent: dict[str, Any], operation: dict[str, Any]) -> None:
    if operation.get("status") != "succeeded":
        if operation.get("phase") in {"captured", "uploading"}:
            await _retry_intent(intent, str(operation.get("error") or "Artifact upload failed"))
        else:
            await update_publish_intent(
                intent["id"],
                state="failed",
                error=str(operation.get("error") or "Artifact capture failed")[:1000],
            )
        return
    result = operation.get("result")
    descriptor = result.get("artifact") if isinstance(result, dict) else None
    if not isinstance(descriptor, dict) or descriptor.get("artifact_id") != intent["artifact_id"]:
        await _retry_intent(intent, "Publish operation returned an invalid Artifact")
        return
    await update_publish_intent(
        intent["id"],
        state="uploaded",
        descriptor_json=json.dumps(descriptor, sort_keys=True, separators=(",", ":")),
        error=None,
    )
    current = await get_publish_intent(str(intent["id"]))
    if current is not None:
        await _bind_published(current)


async def _bind_published(intent: dict[str, Any]) -> None:
    descriptor_json = intent.get("descriptor_json")
    if not isinstance(descriptor_json, str):
        await _retry_intent(intent, "Uploaded Artifact descriptor is missing", binding=True)
        return
    descriptor = json.loads(descriptor_json)
    try:
        message = await _require_message(
            str(intent["chat_id"]), str(intent["assistant_message_id"]), "assistant"
        )
    except HTTPException:
        await _retry_intent(intent, "Assistant message is not committed yet", binding=True)
        return
    files = message.get("files") if isinstance(message.get("files"), list) else []
    artifact_id = str(descriptor["artifact_id"])
    if not any(isinstance(item, dict) and item.get("id") == artifact_id for item in files):
        entry = {
            "type": "file",
            "id": artifact_id,
            "name": descriptor["filename"],
            "url": f"/api/agent/artifacts/{artifact_id}/download",
        }
        attached = await Chats.add_message_files_by_id_and_message_id(
            str(intent["chat_id"]), str(intent["assistant_message_id"]), [entry]
        )
        if attached is None:
            await _retry_intent(intent, "Assistant message could not be updated", binding=True)
            return
    await bind_message_artifact(
        str(intent["chat_id"]),
        str(intent["assistant_message_id"]),
        artifact_id,
        "output",
        str(descriptor["filename"]),
    )
    await update_publish_intent(intent["id"], state="ready", error=None)


async def _retry_intent(intent: dict[str, Any], error: str, *, binding: bool = False) -> None:
    attempts = int(intent.get("attempts") or 0)
    state = (
        "binding_retry"
        if binding
        else ("failed" if attempts >= SETTINGS.publish_retry_limit else "retryable")
    )
    delay = min(300, SETTINGS.publish_reconcile_seconds * (2 ** min(attempts, 4)))
    await update_publish_intent(
        str(intent["id"]),
        state=state,
        next_attempt_at=int(time.time()) + delay,
        error=error[:1000],
    )


async def _retry_capture_intent(intent: dict[str, Any], error: str) -> None:
    await update_publish_intent(
        str(intent["id"]),
        state="pending",
        next_attempt_at=int(time.time()) + SETTINGS.publish_reconcile_seconds,
        error=error[:1000],
    )


async def _await_capture_barriers(workspace_id: str) -> None:
    deadline = asyncio.get_running_loop().time() + 3600
    while True:
        pending = await capture_barriers(workspace_id)
        if not pending:
            return
        now = int(time.time())
        for intent in pending:
            if int(intent["next_attempt_at"]) <= now:
                _schedule_intent(str(intent["id"]))
        if asyncio.get_running_loop().time() >= deadline:
            raise HTTPException(status_code=503, detail="Previous Artifact capture did not finish")
        await asyncio.sleep(0.1)


async def _await_response_binding(
    chat_id: str, assistant_message_id: str
) -> dict[str, str] | None:
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        binding = await get_response_binding(chat_id, assistant_message_id)
        if binding is not None:
            return binding
        await asyncio.sleep(0.1)
    return None


async def _publish_reconciler() -> None:
    while True:
        try:
            for intent in await due_publish_intents(int(time.time())):
                _schedule_intent(str(intent["id"]))
        except Exception:
            log.exception("Artifact publish reconciliation failed")
        await asyncio.sleep(SETTINGS.publish_reconcile_seconds)


def _schedule_intent(intent_id: str) -> asyncio.Task[None]:
    existing = _intent_tasks.get(intent_id)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(_drive_intent(intent_id), name=f"publish-{intent_id}")
    _intent_tasks[intent_id] = task

    def finished(_: asyncio.Task[None]) -> None:
        if _intent_tasks.get(intent_id) is task:
            _intent_tasks.pop(intent_id, None)

    task.add_done_callback(finished)
    return task


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
    json_body: dict[str, Any] | None,
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


async def _artifact_request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None,
) -> dict[str, Any]:
    if _client is None:
        raise HTTPException(status_code=503, detail="Artifact bridge is not ready")
    try:
        response = await _client.request(
            method,
            f"{SETTINGS.artifact_base_url}{path}",
            headers={"Authorization": f"Bearer {SETTINGS.artifact_api_key}"},
            json=json_body,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Artifact Service is unavailable") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Artifact Service rejected the request")
    value = response.json()
    if not isinstance(value, dict):
        raise HTTPException(status_code=502, detail="Artifact Service returned invalid data")
    return value


def _grant(
    operation: str,
    *,
    audience: str = "sandbox-manager",
    expires_in: int | None = None,
    **claims: Any,
) -> str:
    return issue_grant(
        SETTINGS.signing_secret,
        issuer="open-webui-bff",
        audience=audience,
        operation=operation,
        expires_in=expires_in or SETTINGS.grant_ttl_seconds,
        **claims,
    )


async def _require_message(chat_id: str, message_id: str, role: str) -> dict[str, Any]:
    message = await Chats.get_message_by_id_and_message_id(chat_id, message_id)
    if not message or message.get("role") != role:
        raise HTTPException(status_code=404, detail=f"{role.title()} message not found")
    return message


def _valid_message_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None
    )


def _safe_name(value: Any) -> str:
    name = Path(str(value or "file")).name
    normalized = re.sub(r"[^a-zA-Z0-9._-]", "_", name).strip("._")
    return (normalized or "file")[:160]


def _unique_name(name: str, used: set[str]) -> str:
    candidate = name
    stem = Path(name).stem
    suffix = Path(name).suffix
    index = 2
    while candidate in used:
        candidate = f"{stem}-{index}{suffix}"
        index += 1
    used.add(candidate)
    return candidate


def _claim_string(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=403, detail=f"Transfer {name} is invalid")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _async_file_chunks(stream: Any):
    while True:
        chunk = await asyncio.to_thread(stream.read, 1024 * 1024)
        if not chunk:
            return
        yield chunk


def _regular_file_size(path: Path) -> int | None:
    return path.stat().st_size if path.is_file() else None
