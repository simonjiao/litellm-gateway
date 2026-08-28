from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from sandbox_manager.operation_runner import DockerOperationRunner
from sandbox_manager.settings import ManagerSettings
from sandbox_manager.state import WorkspaceRecord
from sandbox_manager.sts import RustFSSTSClient


class _OperationContainer:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.started = False
        self.removed = False

    def start(self) -> None:
        self.started = True

    def wait(self) -> dict[str, int]:
        return {"StatusCode": 0}

    def logs(self, **_: Any) -> bytes:
        command = self.spec["command"]
        result = (
            {"revision_id": "snapshot-test"}
            if command[0] != "publish"
            else {"file_id": "file-test"}
        )
        return json.dumps(result).encode()

    def remove(self, **_: Any) -> None:
        self.removed = True


class _OperationContainers:
    def __init__(self) -> None:
        self.created: list[_OperationContainer] = []

    def create(self, _: str, **spec: Any) -> _OperationContainer:
        container = _OperationContainer(spec)
        self.created.append(container)
        return container


class _OperationDocker:
    def __init__(self) -> None:
        self.containers = _OperationContainers()


@pytest.mark.asyncio
async def test_checkpoint_task_gets_one_workspace_and_prefix_scoped_sts(
    tmp_path: Path,
) -> None:
    password_file = tmp_path / "restic-password"
    password_file.write_text("repository-password")
    captured_policy: dict[str, Any] = {}

    def sts_handler(request: httpx.Request) -> httpx.Response:
        form = httpx.QueryParams(request.content.decode())
        captured_policy.update(json.loads(form["Policy"]))
        return httpx.Response(
            200,
            content=(
                "<AssumeRoleResponse><AssumeRoleResult><Credentials>"
                "<AccessKeyId>temporary-access</AccessKeyId>"
                "<SecretAccessKey>temporary-secret</SecretAccessKey>"
                "<SessionToken>temporary-token</SessionToken>"
                "<Expiration>2030-01-01T00:00:00Z</Expiration>"
                "</Credentials></AssumeRoleResult></AssumeRoleResponse>"
            ),
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(sts_handler))
    sts = RustFSSTSClient(
        "http://rustfs:9000",
        "parent-access",
        "parent-secret",
        client=http,
    )
    settings = ManagerSettings(
        storage_enabled=True,
        object_store_endpoint="http://rustfs:9000",
        object_store_parent_access_key="parent-access",
        object_store_parent_secret_key=SecretStr("parent-secret"),
        workspace_bucket="agent-workspaces",
        restic_password_file=password_file,
    )
    docker_client = _OperationDocker()
    runner = DockerOperationRunner(settings, docker_client, sts_client=sts)
    workspace = WorkspaceRecord(
        id="workspace_scope_test",
        kind="recoverable",
        status="checkpointing",
        volume_name="agent-workspace-test",
        generation=1,
        head_revision=None,
        active_sandbox_id=None,
        created_at=10,
        updated_at=20,
        delete_after=None,
    )

    result = await runner.checkpoint("operation_test", workspace)
    await runner.close()
    await http.aclose()

    assert result["revision_id"] == "snapshot-test"
    container = docker_client.containers.created[0]
    assert container.started is True
    assert container.removed is True
    assert container.spec["runtime"] == "runc"
    assert container.spec["network"] == "agent-storage"
    assert container.spec["volumes"][workspace.volume_name] == {
        "bind": "/workspace",
        "mode": "ro",
    }
    assert container.spec["environment"]["AWS_ACCESS_KEY_ID"] == "temporary-access"
    assert "parent-access" not in container.spec["environment"].values()
    object_resource = captured_policy["Statement"][1]["Resource"][0]
    assert object_resource == "arn:aws:s3:::agent-workspaces/repositories/workspace_scope_test/*"
