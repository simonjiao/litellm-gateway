from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any, Literal

from .settings import ManagerSettings
from .state import WorkspaceRecord
from .sts import RustFSSTSClient, workspace_session_policy

OPERATION_LABEL = "io.litellm-codex-gateway.operation-id"
MANAGED_LABEL = "io.litellm-codex-gateway.managed"
ROLE_LABEL = "io.litellm-codex-gateway.role"
WORKSPACE_LABEL = "io.litellm-codex-gateway.workspace-id"
CREATED_LABEL = "io.litellm-codex-gateway.created-at"


class OperationExecutionError(RuntimeError):
    pass


class DockerOperationRunner:
    def __init__(
        self,
        settings: ManagerSettings,
        docker_client: Any,
        *,
        sts_client: RustFSSTSClient | None = None,
    ) -> None:
        self._settings = settings
        self._docker = docker_client
        self._sts = sts_client

    async def close(self) -> None:
        if self._sts is not None:
            await self._sts.aclose()

    async def checkpoint(self, operation_id: str, workspace: WorkspaceRecord) -> dict[str, Any]:
        environment, password_mount = await self._repository_access(workspace, mode="write")
        return await self._run(
            operation_id,
            "workspace-checkpoint",
            workspace,
            ["checkpoint", "--workspace-id", workspace.id],
            mount_path="/workspace",
            mount_mode="ro",
            environment=environment,
            extra_volumes=password_mount,
        )

    async def restore(
        self,
        operation_id: str,
        workspace: WorkspaceRecord,
        revision_id: str,
    ) -> dict[str, Any]:
        environment, password_mount = await self._repository_access(workspace, mode="read")
        return await self._run(
            operation_id,
            "workspace-restore",
            workspace,
            ["restore", "--revision", revision_id],
            mount_path="/restore",
            mount_mode="rw",
            environment=environment,
            extra_volumes=password_mount,
        )

    async def retire(self, operation_id: str, workspace: WorkspaceRecord) -> dict[str, Any]:
        environment, _ = await self._repository_access(workspace, mode="delete")
        for name in ("RESTIC_REPOSITORY", "RESTIC_PASSWORD_FILE", "RESTIC_CACHE_DIR"):
            environment.pop(name, None)
        settings = self._settings
        assert settings.object_store_endpoint is not None
        assert settings.workspace_bucket is not None
        prefix = f"{settings.workspace_prefix.strip('/')}/{workspace.id}"
        return await self._run(
            operation_id,
            "workspace-retire",
            workspace,
            [
                "retire",
                "--endpoint",
                settings.object_store_endpoint,
                "--bucket",
                settings.workspace_bucket,
                "--prefix",
                prefix,
            ],
            mount_path=None,
            mount_mode="ro",
            environment=environment,
        )

    async def checkout(
        self,
        operation_id: str,
        workspace: WorkspaceRecord,
        *,
        destination: str,
        url: str,
        token: str,
        max_bytes: int,
        sha256: str | None,
    ) -> dict[str, Any]:
        command = [
            "checkout",
            "--destination",
            destination,
            "--url",
            url,
            "--token",
            token,
            "--max-bytes",
            str(max_bytes),
        ]
        if sha256 is not None:
            command.extend(["--sha256", sha256])
        return await self._run(
            operation_id,
            "artifact-checkout",
            workspace,
            command,
            mount_path="/workspace",
            mount_mode="rw",
        )

    async def publish(
        self,
        operation_id: str,
        workspace: WorkspaceRecord,
        *,
        source: str,
        url: str,
        token: str,
        max_bytes: int,
    ) -> dict[str, Any]:
        return await self._run(
            operation_id,
            "artifact-publish",
            workspace,
            [
                "publish",
                "--source",
                source,
                "--url",
                url,
                "--token",
                token,
                "--max-bytes",
                str(max_bytes),
            ],
            mount_path="/workspace",
            mount_mode="ro",
        )

    async def _repository_access(
        self,
        workspace: WorkspaceRecord,
        *,
        mode: Literal["read", "write", "delete"],
    ) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
        settings = self._settings
        assert settings.object_store_endpoint is not None
        assert settings.workspace_bucket is not None
        assert settings.restic_password_file is not None
        prefix = f"{settings.workspace_prefix.strip('/')}/{workspace.id}"
        if settings.object_store_credential_mode == "sts":
            if self._sts is None:
                raise OperationExecutionError("STS credential mode is not initialized")
            credentials = await self._sts.assume_role(
                duration_seconds=settings.sts_duration_seconds,
                policy=workspace_session_policy(
                    settings.workspace_bucket, prefix, mode=mode
                ),
            )
            access_key = credentials.access_key
            secret_key = credentials.secret_key
            session_token = credentials.session_token
        else:
            assert settings.object_store_parent_access_key is not None
            assert settings.object_store_parent_secret_key is not None
            access_key = settings.object_store_parent_access_key
            secret_key = settings.object_store_parent_secret_key.get_secret_value()
            session_token = None
        environment = {
            "AWS_ACCESS_KEY_ID": access_key,
            "AWS_SECRET_ACCESS_KEY": secret_key,
            "AWS_DEFAULT_REGION": settings.object_store_region,
            "RESTIC_REPOSITORY": (
                f"s3:{settings.object_store_endpoint}/{settings.workspace_bucket}/{prefix}"
            ),
            "RESTIC_PASSWORD_FILE": "/run/secrets/restic-password",
            "RESTIC_CACHE_DIR": "/tmp/restic-cache",
        }
        if session_token is not None:
            environment["AWS_SESSION_TOKEN"] = session_token
        password_mount = {
            str(settings.restic_password_file): {
                "bind": "/run/secrets/restic-password",
                "mode": "ro",
            }
        }
        return environment, password_mount

    async def _run(
        self,
        operation_id: str,
        role: str,
        workspace: WorkspaceRecord,
        command: list[str],
        *,
        mount_path: str | None,
        mount_mode: str,
        environment: dict[str, str] | None = None,
        extra_volumes: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        volumes = dict(extra_volumes or {})
        if mount_path is not None:
            volumes[workspace.volume_name] = {"bind": mount_path, "mode": mount_mode}
        short_id = operation_id.removeprefix("operation_")[:24]
        spec: dict[str, Any] = {
            "name": f"{role}-{short_id}",
            "command": command,
            "runtime": self._settings.operation_runtime,
            "network": self._settings.storage_network,
            "environment": environment or {},
            "volumes": volumes,
            "labels": {
                MANAGED_LABEL: "true",
                ROLE_LABEL: "operation",
                OPERATION_LABEL: operation_id,
                WORKSPACE_LABEL: workspace.id,
                CREATED_LABEL: str(workspace.updated_at),
            },
            "privileged": False,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "read_only": True,
            "user": self._settings.container_user,
            "mem_limit": "512m",
            "nano_cpus": 1_000_000_000,
            "pids_limit": 128,
            "tmpfs": {"/tmp": "rw,nosuid,nodev,size=256m"},
        }
        container: Any = None
        try:
            container = await asyncio.to_thread(
                self._docker.containers.create,
                self._settings.storage_ops_image,
                **spec,
            )
            await asyncio.to_thread(container.start)
            wait_result = await asyncio.wait_for(
                asyncio.to_thread(container.wait),
                timeout=self._settings.storage_task_timeout_seconds,
            )
            status_code = wait_result.get("StatusCode") if isinstance(wait_result, dict) else None
            logs = await asyncio.to_thread(container.logs, stdout=True, stderr=True)
            output = logs.decode(errors="replace") if isinstance(logs, bytes) else str(logs)
            if status_code != 0:
                detail = output.strip().splitlines()
                raise OperationExecutionError(
                    detail[-1][:1000] if detail else f"operation exited with {status_code}"
                )
            result = _last_json_object(output)
            return result
        except TimeoutError as exc:
            raise OperationExecutionError("operation timed out") from exc
        except OperationExecutionError:
            raise
        except Exception as exc:
            raise OperationExecutionError(f"operation failed: {exc}") from exc
        finally:
            if container is not None:
                with suppress(Exception):
                    await asyncio.to_thread(container.remove, force=True, v=True)


def _last_json_object(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise OperationExecutionError("operation did not return a JSON result")
