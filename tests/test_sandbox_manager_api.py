from __future__ import annotations

import httpx
import pytest

from sandbox_manager.app import create_app
from sandbox_manager.models import OperationInfo, SandboxInfo, WorkerConnection, WorkspaceInfo
from sandbox_manager.settings import ManagerSettings


class StubBackend:
    def __init__(self) -> None:
        self.renewed: list[str] = []
        self.terminated: list[str] = []
        self.created_workspaces: list[str] = []
        self.created_operations: list[str] = []
        self.sandbox_workspace_grants: list[str] = []
        self.info = SandboxInfo(
            id="sandbox_test",
            status="running",
            created_at=10,
            expires_at=70,
            worker=WorkerConnection(
                base_url="http://sandbox-worker-test:8091",
                api_key="worker-specific-secret",
            ),
        )

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def create(self, workspace_grant: str | None = None) -> SandboxInfo:
        if workspace_grant is not None:
            self.sandbox_workspace_grants.append(workspace_grant)
            return self.info.model_copy(
                update={
                    "workspace_id": "workspace_api_test01",
                    "recoverable": True,
                }
            )
        return self.info

    async def inspect(self, sandbox_id: str) -> SandboxInfo:
        assert sandbox_id == self.info.id
        return self.info

    async def renew(self, sandbox_id: str) -> SandboxInfo:
        assert sandbox_id == self.info.id
        self.renewed.append(sandbox_id)
        return self.info.model_copy(update={"expires_at": 130})

    async def terminate(self, sandbox_id: str) -> SandboxInfo:
        self.terminated.append(sandbox_id)
        return self.info.model_copy(
            update={"status": "terminated", "expires_at": None, "worker": None}
        )

    async def create_workspace(self, workspace_id: str, grant: str) -> WorkspaceInfo:
        assert grant == "signed-workspace-create-grant"
        self.created_workspaces.append(workspace_id)
        return WorkspaceInfo(
            id=workspace_id,
            kind="recoverable",
            status="detached_clean",
            generation=0,
            head_revision=None,
            active_sandbox_id=None,
            created_at=10,
            updated_at=10,
            delete_after=None,
        )

    async def inspect_workspace(self, workspace_id: str, grant: str) -> WorkspaceInfo:
        assert grant == "signed-workspace-inspect-grant"
        return await self.create_workspace(workspace_id, "signed-workspace-create-grant")

    async def release_workspace(self, workspace_id: str, grant: str) -> WorkspaceInfo:
        assert grant == "signed-workspace-release-grant"
        workspace = await self.inspect_workspace(workspace_id, "signed-workspace-inspect-grant")
        return workspace.model_copy(update={"delete_after": 1000})

    async def create_operation(self, grant: str) -> OperationInfo:
        self.created_operations.append(grant)
        return OperationInfo(
            id="operation_test",
            operation="publish",
            status="pending",
            workspace_id="workspace_api_test01",
            sandbox_id="sandbox_test",
            result=None,
            error=None,
            created_at=10,
            updated_at=10,
        )

    async def inspect_operation(self, operation_id: str) -> OperationInfo:
        assert operation_id == "operation_test"
        return (await self.create_operation("inspected")).model_copy(
            update={"status": "succeeded", "result": {"file_id": "file_test"}}
        )


@pytest.mark.asyncio
async def test_sandbox_manager_exposes_only_authenticated_lifecycle_operations() -> None:
    backend = StubBackend()
    settings = ManagerSettings(
        api_key="manager-secret",
        worker_token_secret="worker-token-secret-at-least-32-bytes",
    )
    app = create_app(settings, backend=backend)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://manager"
        ) as client:
            unauthenticated = await client.post("/v1/sandboxes")
            assert unauthenticated.status_code == 401

            headers = {"Authorization": "Bearer manager-secret"}
            created = await client.post("/v1/sandboxes", headers=headers)
            assert created.status_code == 201
            assert created.json()["id"] == "sandbox_test"
            assert created.json()["worker"] == {
                "base_url": "http://sandbox-worker-test:8091",
                "api_key": "worker-specific-secret",
            }

            inspected = await client.get("/v1/sandboxes/sandbox_test", headers=headers)
            assert inspected.status_code == 200
            assert inspected.json()["status"] == "running"

            renewed = await client.post("/v1/sandboxes/sandbox_test/lease", headers=headers)
            assert renewed.status_code == 200
            assert renewed.json()["expires_at"] == 130
            assert backend.renewed == ["sandbox_test"]

            assert (
                await client.post(
                    "/v1/sandboxes/sandbox_test/commands",
                    headers=headers,
                    json={"type": "rpc", "method": "thread/start"},
                )
            ).status_code == 404
            assert (
                await client.get("/v1/sandboxes/sandbox_test/events", headers=headers)
            ).status_code == 404

            terminated = await client.delete("/v1/sandboxes/sandbox_test", headers=headers)
            assert terminated.status_code == 200
            assert terminated.json()["status"] == "terminated"
            assert terminated.json()["worker"] is None
            assert backend.terminated == ["sandbox_test"]

            workspace = await client.post(
                "/v1/workspaces",
                headers=headers,
                json={
                    "workspace_id": "workspace_api_test01",
                    "grant": "signed-workspace-create-grant",
                },
            )
            assert workspace.status_code == 201
            assert workspace.json()["kind"] == "recoverable"
            assert backend.created_workspaces == ["workspace_api_test01"]

            operation = await client.post(
                "/v1/operations",
                headers=headers,
                json={"grant": "signed-operation-grant"},
            )
            assert operation.status_code == 202
            assert operation.json()["status"] == "pending"
            assert backend.created_operations == ["signed-operation-grant"]

            completed = await client.get("/v1/operations/operation_test", headers=headers)
            assert completed.status_code == 200
            assert completed.json()["result"] == {"file_id": "file_test"}
