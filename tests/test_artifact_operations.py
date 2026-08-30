from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, cast

import pytest

from sandbox_api.grants import issue_grant
from sandbox_manager.docker_backend import DockerSandboxBackend
from sandbox_manager.operation_runner import DockerOperationRunner, OperationExecutionError
from sandbox_manager.settings import ManagerSettings
from sandbox_manager.state import StateStore, WorkspaceRecord


class _Runner:
    def __init__(self) -> None:
        self.checkout_artifacts: list[dict[str, Any]] | None = None
        self.capture_calls = 0
        self.upload_calls = 0
        self.upload_gate = asyncio.Event()
        self.fail_uploads = 0

    async def checkout(
        self,
        operation_id: str,
        workspace: WorkspaceRecord,
        *,
        user_message_id: str,
        assistant_message_id: str,
        artifacts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        del operation_id, workspace, user_message_id, assistant_message_id
        self.checkout_artifacts = artifacts
        return {"path": "uploads/user_message_one"}

    async def capture(
        self,
        operation_id: str,
        workspace: WorkspaceRecord,
        *,
        source: str,
        max_bytes: int,
    ) -> dict[str, Any]:
        del operation_id, workspace, max_bytes
        self.capture_calls += 1
        return {
            "source": source,
            "filename": "result.txt",
            "size": 6,
            "sha256": "1" * 64,
        }

    async def upload_capture(
        self,
        operation_id: str,
        workspace: WorkspaceRecord,
        *,
        url: str,
        token: str,
    ) -> dict[str, Any]:
        del operation_id, workspace, url, token
        self.upload_calls += 1
        await self.upload_gate.wait()
        if self.upload_calls <= self.fail_uploads:
            raise OperationExecutionError("temporary Artifact outage")
        return {
            "artifact_id": "artifact_" + "1" * 32,
            "owner_id": "user_one",
            "filename": "result.txt",
            "media_type": "text/plain",
            "size": 6,
            "sha256": "1" * 64,
            "created_at": int(time.time()),
        }


def _backend(tmp_path: Path, runner: _Runner) -> tuple[DockerSandboxBackend, StateStore]:
    store = StateStore(str(tmp_path / "manager.db"))
    store.startup()
    workspace_id = "workspace_" + "1" * 32
    sandbox_id = "sandbox_" + "2" * 32
    store.create_workspace(workspace_id, kind="recoverable", volume_name="workspace-volume")
    store.claim_workspace(
        workspace_id,
        sandbox_id=sandbox_id,
        worker_host="sandbox-worker-test",
        container_name="sandbox-worker-test",
        created_at=int(time.time()),
        expires_at=int(time.time()) + 3600,
    )
    store.mark_sandbox_running(sandbox_id)
    settings = ManagerSettings(
        operation_signing_secret="operation-signing-secret-at-least-32-bytes",
        artifact_transfer_base_url="http://artifact-service:8093/v1/transfers",
        state_db_path=str(tmp_path / "manager.db"),
    )
    backend = DockerSandboxBackend(
        settings,
        docker_client=cast(Any, object()),
        state_store=store,
        operation_runner=cast(DockerOperationRunner, runner),
    )
    return backend, store


def _grant(operation: str, **claims: Any) -> str:
    return issue_grant(
        "operation-signing-secret-at-least-32-bytes",
        issuer="open-webui-bff",
        audience="sandbox-manager",
        operation=operation,
        expires_in=120,
        **claims,
    )


async def _terminal(backend: DockerSandboxBackend, operation_id: str):
    for _ in range(100):
        operation = await backend.inspect_operation(operation_id)
        if operation.status in {"succeeded", "failed"}:
            return operation
        await asyncio.sleep(0.01)
    raise AssertionError("operation did not finish")


@pytest.mark.asyncio
async def test_manager_executes_one_atomic_checkout_batch(tmp_path: Path) -> None:
    runner = _Runner()
    backend, store = _backend(tmp_path, runner)
    artifact_id = "artifact_" + "0" * 32
    operation = await backend.create_operation(
        _grant(
            "artifact_checkout",
            workspace_id="workspace_" + "1" * 32,
            sandbox_id="sandbox_" + "2" * 32,
            user_message_id="user_message_one",
            assistant_message_id="assistant_message_one",
            artifacts=[
                {
                    "artifact_id": artifact_id,
                    "filename": "input.txt",
                    "size": 5,
                    "sha256": "0" * 64,
                    "max_bytes": 5,
                    "url": f"http://artifact-service:8093/v1/transfers/downloads/{artifact_id}",
                    "token": "download-capability",
                }
            ],
            idempotency_key="checkout:chat:user-message",
        )
    )
    completed = await _terminal(backend, operation.id)
    store.close()

    assert completed.status == "succeeded"
    assert runner.checkout_artifacts is not None
    assert runner.checkout_artifacts[0]["token"] == "download-capability"


@pytest.mark.asyncio
async def test_publish_releases_capture_barrier_and_retries_from_spool(tmp_path: Path) -> None:
    runner = _Runner()
    runner.fail_uploads = 1
    backend, store = _backend(tmp_path, runner)
    identity = {
        "workspace_id": "workspace_" + "1" * 32,
        "assistant_message_id": "assistant_message_one",
        "response_id": "resp_" + "3" * 32,
        "output_relative_path": "result.txt",
        "max_bytes": 1024,
        "idempotency_key": "publish:intent-one",
    }
    target = {
        "artifact_id": "artifact_" + "1" * 32,
        "upload_url": (
            "http://artifact-service:8093/v1/transfers/uploads/"
            f"artifact_{'1' * 32}/upload_{'4' * 32}"
        ),
    }
    first = await backend.create_operation(_grant("artifact_publish", **identity))

    for _ in range(100):
        captured = await backend.inspect_operation(first.id)
        if captured.phase in {"captured", "uploading"}:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("publish did not reach the capture barrier")
    assert captured.status == "running"

    uploading = await backend.create_operation(
        _grant(
            "artifact_publish",
            **identity,
            **target,
            upload_token="first-upload-capability",
        )
    )
    assert uploading.id == first.id
    runner.upload_gate.set()
    failed = await _terminal(backend, first.id)
    assert failed.status == "failed"
    assert failed.phase == "captured"
    assert failed.result is not None and failed.result["capture"]["size"] == 6

    retried = await backend.create_operation(
        _grant(
            "artifact_publish",
            **identity,
            **target,
            upload_token="second-upload-capability",
        )
    )
    assert retried.id == first.id
    succeeded = await _terminal(backend, first.id)
    store.close()

    assert succeeded.status == "succeeded"
    assert succeeded.result is not None
    assert succeeded.result["artifact"]["artifact_id"] == target["artifact_id"]
    assert runner.capture_calls == 3
    assert runner.upload_calls == 2
