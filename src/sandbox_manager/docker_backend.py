from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import re
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import docker
from docker.errors import NotFound

from sandbox_api.grants import GrantError, verify_grant

from .backend import (
    OperationNotFoundError,
    SandboxAuthorizationError,
    SandboxBackendError,
    SandboxConflictError,
    SandboxNotFoundError,
    WorkspaceNotFoundError,
)
from .models import OperationInfo, SandboxInfo, SandboxStatus, WorkerConnection, WorkspaceInfo
from .operation_runner import OPERATION_LABEL, DockerOperationRunner, OperationExecutionError
from .settings import ManagerSettings
from .state import (
    OperationRecord,
    SandboxRecord,
    StateConflictError,
    StateNotFoundError,
    StateStore,
    WorkspaceRecord,
)
from .sts import RustFSSTSClient

logger = logging.getLogger(__name__)

MANAGED_LABEL = "io.litellm-codex-gateway.managed"
ROLE_LABEL = "io.litellm-codex-gateway.role"
SANDBOX_LABEL = "io.litellm-codex-gateway.sandbox-id"
WORKSPACE_LABEL = "io.litellm-codex-gateway.workspace-id"
WORKSPACE_KIND_LABEL = "io.litellm-codex-gateway.workspace-kind"
CREATED_LABEL = "io.litellm-codex-gateway.created-at"
WORKER_HOST_LABEL = "io.litellm-codex-gateway.worker-host"


@dataclass(slots=True)
class _SandboxRecord:
    sandbox_id: str
    workspace_id: str
    workspace_volume: str
    recoverable: bool
    container: Any
    worker_host: str
    created_at: int
    expires_at: int


def build_container_spec(
    settings: ManagerSettings,
    sandbox_id: str,
    *,
    worker_host: str,
    worker_api_key: str,
    workspace_id: str | None = None,
    workspace_volume: str | None = None,
    recoverable: bool = False,
) -> dict[str, Any]:
    effective_workspace_id = workspace_id or sandbox_id
    effective_workspace_volume = workspace_volume or _legacy_workspace_volume(sandbox_id)
    volumes: dict[str, dict[str, str]] = {
        effective_workspace_volume: {"bind": "/workspace", "mode": "rw"},
        _runtime_state_volume(sandbox_id): {"bind": "/home/agent/.codex", "mode": "rw"},
    }
    if settings.codex_auth_file is not None:
        volumes[str(settings.codex_auth_file)] = {
            "bind": "/home/agent/.codex/auth.json",
            "mode": "ro",
        }
    if settings.codex_config_file is not None:
        volumes[str(settings.codex_config_file)] = {
            "bind": "/home/agent/.codex/config.toml",
            "mode": "ro",
        }
    if settings.resolv_conf_file is not None:
        volumes[str(settings.resolv_conf_file)] = {
            "bind": "/etc/resolv.conf",
            "mode": "ro",
        }

    no_proxy = ["127.0.0.1", "localhost", *settings.internal_no_proxy_names()]
    environment = {
        "SANDBOX_WORKER_API_KEY": worker_api_key,
        "SANDBOX_WORKER_HOST": "0.0.0.0",
        "SANDBOX_WORKER_PORT": str(settings.worker_port),
        "SANDBOX_WORKER_CODEX_COMMAND": settings.codex_command,
        "SANDBOX_WORKER_CODEX_WORKDIR": "/workspace",
        "SANDBOX_WORKER_CODEX_MODEL": settings.codex_model or "",
        "SANDBOX_WORKER_MCP_APPS_ENABLED": str(settings.mcp_apps_enabled).lower(),
        "HTTP_PROXY": settings.egress_proxy_url,
        "HTTPS_PROXY": settings.egress_proxy_url,
        "ALL_PROXY": settings.egress_proxy_url,
        "NO_PROXY": ",".join(no_proxy),
    }
    return {
        "name": worker_host,
        "hostname": worker_host,
        "init": True,
        "runtime": settings.docker_runtime,
        "network": settings.rpc_network,
        "environment": environment,
        "volumes": volumes,
        "labels": {
            MANAGED_LABEL: "true",
            ROLE_LABEL: "worker",
            SANDBOX_LABEL: sandbox_id,
            WORKSPACE_LABEL: effective_workspace_id,
            WORKSPACE_KIND_LABEL: "recoverable" if recoverable else "ephemeral",
            CREATED_LABEL: str(int(time.time())),
            WORKER_HOST_LABEL: worker_host,
        },
        "privileged": False,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "read_only": True,
        "user": settings.container_user,
        "mem_limit": settings.memory_limit,
        "nano_cpus": settings.nano_cpus,
        "pids_limit": settings.pids_limit,
        "tmpfs": {
            "/tmp": f"rw,nosuid,nodev,size={settings.tmpfs_limit}",
            "/run": "rw,nosuid,nodev,size=16m",
        },
    }


class DockerSandboxBackend:
    """Docker implementation of the Sandbox and Workspace control plane."""

    def __init__(
        self,
        settings: ManagerSettings,
        *,
        docker_client: Any | None = None,
        state_store: StateStore | None = None,
        operation_runner: DockerOperationRunner | None = None,
    ) -> None:
        self._settings = settings
        self._docker: Any = docker_client
        self._state = state_store or StateStore(settings.state_db_path)
        self._records: dict[str, _SandboxRecord] = {}
        self._lock = asyncio.Lock()
        self._workspace_locks: dict[str, asyncio.Lock] = {}
        self._operation_runner = operation_runner
        self._owns_operation_runner = False
        self._operation_tasks: set[asyncio.Task[None]] = set()
        self._reaper_task: asyncio.Task[None] | None = None

    async def startup(self) -> None:
        self._validate_files()
        self._state.startup()
        try:
            if self._docker is None:
                self._docker = await asyncio.to_thread(docker.from_env)
            await self._validate_docker()
            if self._settings.storage_enabled and self._operation_runner is None:
                assert self._settings.object_store_endpoint is not None
                assert self._settings.object_store_parent_access_key is not None
                assert self._settings.object_store_parent_secret_key is not None
                sts = None
                if self._settings.object_store_credential_mode == "sts":
                    sts = RustFSSTSClient(
                        self._settings.object_store_endpoint,
                        self._settings.object_store_parent_access_key,
                        self._settings.object_store_parent_secret_key.get_secret_value(),
                        region=self._settings.object_store_region,
                    )
                self._operation_runner = DockerOperationRunner(
                    self._settings,
                    self._docker,
                    sts_client=sts,
                )
                self._owns_operation_runner = True
            await self._reconcile_managed_containers()
            await self._recover_incomplete_operations()
            self._reaper_task = asyncio.create_task(self._reaper(), name="sandbox-manager-reaper")
        except Exception:
            if self._owns_operation_runner and self._operation_runner is not None:
                with suppress(Exception):
                    await self._operation_runner.close()
            if self._docker is not None:
                with suppress(Exception):
                    await asyncio.to_thread(self._docker.close)
            self._state.close()
            raise

    async def shutdown(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reaper_task
        for task in tuple(self._operation_tasks):
            task.cancel()
        for task in tuple(self._operation_tasks):
            with suppress(asyncio.CancelledError, Exception):
                await task
        if self._owns_operation_runner and self._operation_runner is not None:
            await self._operation_runner.close()
        if self._docker is not None:
            await asyncio.to_thread(self._docker.close)
        self._state.close()

    async def create_workspace(self, workspace_id: str, grant: str) -> WorkspaceInfo:
        self._authorize(grant, "workspace_create", workspace_id)
        volume_name = _workspace_volume(workspace_id)
        try:
            record = self._state.create_workspace(
                workspace_id,
                kind="recoverable",
                volume_name=volume_name,
            )
            await self._ensure_workspace_volume(record)
        except StateConflictError as exc:
            raise SandboxConflictError(str(exc)) from exc
        except Exception as exc:
            if isinstance(exc, (SandboxConflictError, SandboxAuthorizationError)):
                raise
            raise SandboxBackendError(
                f"Failed to create Workspace '{workspace_id}': {exc}"
            ) from exc
        return _workspace_info(record)

    async def inspect_workspace(self, workspace_id: str, grant: str) -> WorkspaceInfo:
        self._authorize(grant, "workspace_inspect", workspace_id)
        return _workspace_info(self._get_workspace(workspace_id))

    async def release_workspace(self, workspace_id: str, grant: str) -> WorkspaceInfo:
        self._authorize(grant, "workspace_release", workspace_id)
        workspace = self._get_workspace(workspace_id)
        if workspace.active_sandbox_id is not None:
            await self.terminate(workspace.active_sandbox_id)
        delete_after = int(time.time()) + self._settings.workspace_delete_grace_seconds
        try:
            return _workspace_info(
                self._state.schedule_workspace_delete(workspace_id, delete_after)
            )
        except StateNotFoundError as exc:
            raise WorkspaceNotFoundError(str(exc)) from exc

    async def create_operation(self, grant: str) -> OperationInfo:
        claims = self._authorize(grant, None)
        requested_operation = claims.get("operation")
        operation_names = {
            "artifact_checkout": "checkout",
            "artifact_publish": "publish",
            "workspace_checkpoint": "checkpoint",
            "workspace_restore": "restore",
        }
        if not isinstance(requested_operation, str) or requested_operation not in operation_names:
            raise SandboxAuthorizationError("Operation grant has an unsupported operation")
        if self._operation_runner is None:
            raise SandboxBackendError("Storage operations are disabled")
        workspace_id = _required_claim(claims, "workspace_id")
        workspace = self._get_workspace(workspace_id)
        operation = operation_names[requested_operation]
        sandbox_id: str | None = None
        input_data: dict[str, Any]
        idempotency_key: str

        if operation in {"checkout", "publish"}:
            claimed_sandbox_id = claims.get("sandbox_id")
            if claimed_sandbox_id is None:
                sandbox_id = workspace.active_sandbox_id
                if sandbox_id is None:
                    raise SandboxAuthorizationError("Operation Workspace has no active Sandbox")
            elif isinstance(claimed_sandbox_id, str) and claimed_sandbox_id:
                sandbox_id = claimed_sandbox_id
            else:
                raise SandboxAuthorizationError("Operation sandbox_id is invalid")
            try:
                sandbox = self._state.get_sandbox(sandbox_id)
            except StateNotFoundError as exc:
                raise SandboxAuthorizationError("Operation Sandbox does not exist") from exc
            if (
                sandbox.workspace_id != workspace.id
                or sandbox.status != "running"
                or workspace.active_sandbox_id != sandbox_id
            ):
                raise SandboxAuthorizationError("Operation Sandbox binding is not active")
            url = _required_claim(claims, "transfer_url")
            token = _required_claim(claims, "transfer_token")
            self._validate_transfer_url(url)
            max_bytes = claims.get("max_bytes")
            if not isinstance(max_bytes, int) or not 0 < max_bytes <= 10 * 1024**3:
                raise SandboxAuthorizationError("Operation max_bytes is invalid")
            if operation == "checkout":
                destination = _required_claim(claims, "destination")
                sha256 = claims.get("sha256")
                if sha256 is not None and (
                    not isinstance(sha256, str) or re.fullmatch(r"[a-fA-F0-9]{64}", sha256) is None
                ):
                    raise SandboxAuthorizationError("Operation sha256 is invalid")
                input_data = {
                    "file_id": _required_claim(claims, "file_id"),
                    "destination": destination,
                    "max_bytes": max_bytes,
                    "sha256": sha256,
                }
            else:
                source = _required_claim(claims, "workspace_path")
                input_data = {
                    "workspace_path": source,
                    "max_bytes": max_bytes,
                }
            claims = {
                **claims,
                "transfer_url": url,
                "transfer_token": token,
                "max_bytes": max_bytes,
            }
            idempotency_key = _idempotency_key(claims, requested_operation)
        elif operation == "checkpoint":
            if workspace.active_sandbox_id is not None:
                raise SandboxConflictError(f"Workspace '{workspace.id}' still has a writer")
            if workspace.status not in {"detached_dirty", "detached_clean"}:
                raise SandboxConflictError(
                    f"Workspace '{workspace.id}' cannot checkpoint while {workspace.status}"
                )
            input_data = {"generation": workspace.generation}
            idempotency_key = f"checkpoint:{workspace.id}:{workspace.generation}"
        else:
            revision_id = claims.get("revision_id") or workspace.head_revision
            if not isinstance(revision_id, str) or revision_id != workspace.head_revision:
                raise SandboxAuthorizationError("Restore revision is not the Workspace head")
            if workspace.active_sandbox_id is not None:
                raise SandboxConflictError(f"Workspace '{workspace.id}' still has a writer")
            if workspace.status not in {"remote_only", "detached_clean"}:
                raise SandboxConflictError(
                    f"Workspace '{workspace.id}' cannot restore while {workspace.status}"
                )
            input_data = {"revision_id": revision_id}
            idempotency_key = f"restore:{workspace.id}:{revision_id}"

        operation_id = f"operation_{uuid.uuid4().hex}"
        try:
            record, created = self._state.create_operation(
                operation_id,
                operation=operation,
                workspace_id=workspace.id,
                sandbox_id=sandbox_id,
                idempotency_key=idempotency_key,
                input_data=input_data,
            )
        except StateConflictError as exc:
            raise SandboxConflictError(str(exc)) from exc
        if operation == "checkpoint" and workspace.status == "detached_clean":
            record = self._state.update_operation(
                record.id,
                status="succeeded",
                result={"revision_id": workspace.head_revision, "already_clean": True},
            )
        elif created or record.status == "failed":
            if operation == "checkpoint":
                coroutine = self._checkpoint_workspace(workspace.id)
            elif operation == "restore":
                coroutine = self._restore_requested_workspace(record)
            else:
                coroutine = self._execute_file_operation(record, claims)
            self._schedule_operation(coroutine, name=f"{operation}-{record.id}")
        return _operation_info(record)

    async def inspect_operation(self, operation_id: str) -> OperationInfo:
        try:
            return _operation_info(self._state.get_operation(operation_id))
        except StateNotFoundError as exc:
            raise OperationNotFoundError(str(exc)) from exc

    async def _execute_file_operation(
        self,
        operation: OperationRecord,
        claims: dict[str, Any],
    ) -> None:
        if self._operation_runner is None:
            self._state.update_operation(
                operation.id,
                status="failed",
                error="storage operations are disabled",
            )
            return
        workspace = self._get_workspace(operation.workspace_id)
        try:
            self._state.update_operation(operation.id, status="running")
            if operation.operation == "checkout":
                result = await self._operation_runner.checkout(
                    operation.id,
                    workspace,
                    destination=str(operation.input["destination"]),
                    url=str(claims["transfer_url"]),
                    token=str(claims["transfer_token"]),
                    max_bytes=int(operation.input["max_bytes"]),
                    sha256=(
                        str(operation.input["sha256"])
                        if operation.input.get("sha256") is not None
                        else None
                    ),
                )
            else:
                result = await self._operation_runner.publish(
                    operation.id,
                    workspace,
                    source=str(operation.input["workspace_path"]),
                    url=str(claims["transfer_url"]),
                    token=str(claims["transfer_token"]),
                    max_bytes=int(operation.input["max_bytes"]),
                )
            self._state.update_operation(operation.id, status="succeeded", result=result)
        except Exception as exc:
            self._state.update_operation(
                operation.id,
                status="failed",
                error=_operation_error(exc),
            )
            logger.exception("Failed %s operation %s", operation.operation, operation.id)

    async def _restore_requested_workspace(self, operation: OperationRecord) -> None:
        try:
            workspace = self._get_workspace(operation.workspace_id)
            if workspace.status == "detached_clean":
                self._state.update_operation(
                    operation.id,
                    status="succeeded",
                    result={"revision_id": workspace.head_revision, "already_materialized": True},
                )
                return
            if workspace.status != "remote_only":
                raise SandboxConflictError(
                    f"Workspace '{workspace.id}' cannot restore while {workspace.status}"
                )
            await self._prepare_workspace(workspace)
        except Exception as exc:
            with suppress(StateNotFoundError):
                self._state.update_operation(
                    operation.id,
                    status="failed",
                    error=_operation_error(exc),
                )
            logger.exception("Failed requested restore for Workspace %s", operation.workspace_id)

    def _validate_transfer_url(self, url: str) -> None:
        base_url = self._settings.files_transfer_base_url
        parsed = urlsplit(url)
        if (
            base_url is None
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or not (url == base_url or url.startswith(f"{base_url}/"))
        ):
            raise SandboxAuthorizationError("Operation transfer_url is not allowed")

    async def create(self, workspace_grant: str | None = None) -> SandboxInfo:
        requested_sandbox_id: str | None = None
        if workspace_grant is None:
            token = uuid.uuid4().hex
            workspace_id = f"workspace_{token}"
            workspace = self._state.create_workspace(
                workspace_id,
                kind="ephemeral",
                volume_name=_workspace_volume(workspace_id),
            )
        else:
            claims = self._authorize(workspace_grant, "sandbox_create")
            workspace_id = claims.get("workspace_id")
            if not isinstance(workspace_id, str):
                raise SandboxAuthorizationError("Workspace grant has no workspace_id")
            workspace = self._get_workspace(workspace_id)
            if workspace.kind != "recoverable":
                raise SandboxAuthorizationError(
                    "Workspace grant does not select a recoverable Workspace"
                )
            claimed_sandbox_id = claims.get("sandbox_id")
            if claimed_sandbox_id is not None:
                if (
                    not isinstance(claimed_sandbox_id, str)
                    or re.fullmatch(r"sandbox_[a-f0-9]{32}", claimed_sandbox_id) is None
                ):
                    raise SandboxAuthorizationError("Workspace grant has an invalid sandbox_id")
                requested_sandbox_id = claimed_sandbox_id

        workspace = await self._prepare_workspace(workspace)
        token = uuid.uuid4().hex
        sandbox_id = requested_sandbox_id or f"sandbox_{token}"
        token = sandbox_id.removeprefix("sandbox_")
        worker_host = f"sandbox-worker-{token}"
        worker_api_key = self._worker_api_key(sandbox_id)
        now = int(time.time())
        expires_at = now + self._settings.execution_ttl_seconds
        try:
            self._state.claim_workspace(
                workspace.id,
                sandbox_id=sandbox_id,
                worker_host=worker_host,
                container_name=worker_host,
                created_at=now,
                expires_at=expires_at,
            )
        except StateConflictError as exc:
            if workspace.kind == "ephemeral":
                await self._remove_volume(workspace.volume_name)
                self._state.delete_workspace(workspace.id)
            raise SandboxConflictError(str(exc)) from exc

        spec = build_container_spec(
            self._settings,
            sandbox_id,
            worker_host=worker_host,
            worker_api_key=worker_api_key,
            workspace_id=workspace.id,
            workspace_volume=workspace.volume_name,
            recoverable=workspace.kind == "recoverable",
        )
        container: Any = None
        try:
            await self._create_runtime_volume(sandbox_id, workspace.id)
            container = await asyncio.to_thread(
                self._docker.containers.create,
                self._settings.sandbox_image,
                **spec,
            )
            egress_network = await asyncio.to_thread(
                self._docker.networks.get, self._settings.egress_network
            )
            await asyncio.to_thread(egress_network.connect, container)
            await asyncio.to_thread(container.start)
            await self._wait_for_worker(container)
            self._state.mark_sandbox_running(sandbox_id)
            record = _SandboxRecord(
                sandbox_id=sandbox_id,
                workspace_id=workspace.id,
                workspace_volume=workspace.volume_name,
                recoverable=workspace.kind == "recoverable",
                container=container,
                worker_host=worker_host,
                created_at=now,
                expires_at=expires_at,
            )
            async with self._lock:
                self._records[sandbox_id] = record
            return self._info(record, "running")
        except BaseException as creation_error:
            cleanup_failed: Exception | None = None
            if container is not None:
                try:
                    await self._remove_container(container, sandbox_id)
                except SandboxBackendError as exc:
                    cleanup_failed = exc
            if cleanup_failed is not None:
                record = _SandboxRecord(
                    sandbox_id=sandbox_id,
                    workspace_id=workspace.id,
                    workspace_volume=workspace.volume_name,
                    recoverable=workspace.kind == "recoverable",
                    container=container,
                    worker_host=worker_host,
                    created_at=now,
                    expires_at=now,
                )
                async with self._lock:
                    self._records[sandbox_id] = record
                raise SandboxBackendError(
                    f"Sandbox creation failed and Worker cleanup failed: {cleanup_failed}"
                ) from creation_error
            await self._finish_failed_creation(sandbox_id, workspace)
            raise

    async def inspect(self, sandbox_id: str) -> SandboxInfo:
        record = await self._get(sandbox_id)
        return self._info(record, await self._status(record.container))

    async def renew(self, sandbox_id: str) -> SandboxInfo:
        record = await self._get(sandbox_id)
        status = await self._status(record.container)
        if status == "running":
            expires_at = int(time.time()) + self._settings.execution_ttl_seconds
            try:
                self._state.renew_sandbox(sandbox_id, expires_at)
            except StateConflictError as exc:
                raise SandboxConflictError(str(exc)) from exc
            async with self._lock:
                if sandbox_id in self._records:
                    record.expires_at = expires_at
        return self._info(record, status)

    async def terminate(self, sandbox_id: str) -> SandboxInfo:
        record = await self._get(sandbox_id)
        await self._remove_container(record.container, sandbox_id)
        async with self._lock:
            if self._records.get(sandbox_id) is record:
                self._records.pop(sandbox_id)
        await self._remove_volume(_runtime_state_volume(sandbox_id))
        workspace_status = "detached_dirty" if record.recoverable else "deleting"
        with suppress(StateNotFoundError):
            self._state.finish_sandbox(
                sandbox_id,
                status="terminated",
                workspace_status=workspace_status,
            )
        if not record.recoverable:
            await self._remove_volume(record.workspace_volume)
            with suppress(StateConflictError):
                self._state.delete_workspace(record.workspace_id)
        elif self._settings.storage_enabled:
            self._schedule_operation(
                self._checkpoint_workspace(record.workspace_id),
                name=f"checkpoint-{record.workspace_id}",
            )
        return SandboxInfo(
            id=sandbox_id,
            status="terminated",
            created_at=record.created_at,
            expires_at=None,
            worker=None,
            workspace_id=record.workspace_id,
            recoverable=record.recoverable,
        )

    async def _checkpoint_workspace(self, workspace_id: str) -> None:
        if self._operation_runner is None:
            return
        lock = self._workspace_locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            workspace = self._get_workspace(workspace_id)
            if workspace.active_sandbox_id is not None or workspace.status != "detached_dirty":
                return
            operation_id = f"operation_{uuid.uuid4().hex}"
            operation, _ = self._state.create_operation(
                operation_id,
                operation="checkpoint",
                workspace_id=workspace.id,
                sandbox_id=None,
                idempotency_key=f"checkpoint:{workspace.id}:{workspace.generation}",
                input_data={"generation": workspace.generation},
            )
            if operation.status == "succeeded":
                return
            try:
                workspace = self._state.transition_workspace(
                    workspace.id,
                    expected={"detached_dirty"},
                    status="checkpointing",
                )
                self._state.update_operation(operation.id, status="running")
                result = await self._operation_runner.checkpoint(operation.id, workspace)
                revision_id = result.get("revision_id")
                if not isinstance(revision_id, str) or not revision_id:
                    raise OperationExecutionError("checkpoint returned no revision_id")
                self._state.commit_revision(
                    workspace.id,
                    operation_id=operation.id,
                    revision_id=revision_id,
                    generation=workspace.generation,
                    result=result,
                )
                logger.info("Checkpointed Workspace %s at %s", workspace.id, revision_id)
            except Exception as exc:
                with suppress(StateNotFoundError):
                    self._state.update_operation(
                        operation.id,
                        status="failed",
                        error=_operation_error(exc),
                    )
                with suppress(StateConflictError, StateNotFoundError):
                    self._state.transition_workspace(
                        workspace.id,
                        expected={"checkpointing"},
                        status="detached_dirty",
                    )
                logger.exception("Failed to checkpoint Workspace %s", workspace.id)

    def _schedule_operation(self, coroutine: Any, *, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._operation_tasks.add(task)
        task.add_done_callback(self._operation_tasks.discard)

    async def _validate_docker(self) -> None:
        info = await asyncio.to_thread(self._docker.info)
        runtimes = info.get("Runtimes") if isinstance(info, dict) else None
        if not isinstance(runtimes, dict) or self._settings.docker_runtime not in runtimes:
            raise SandboxBackendError(
                f"Docker runtime '{self._settings.docker_runtime}' is unavailable"
            )
        if self._settings.storage_enabled and self._settings.operation_runtime not in runtimes:
            raise SandboxBackendError(
                f"Docker runtime '{self._settings.operation_runtime}' is unavailable"
            )
        for network_name in (self._settings.rpc_network, self._settings.egress_network):
            try:
                network = await asyncio.to_thread(self._docker.networks.get, network_name)
            except Exception as exc:
                raise SandboxBackendError(
                    f"Docker network '{network_name}' is unavailable"
                ) from exc
            if network.attrs.get("Internal") is not True:
                raise SandboxBackendError(f"Docker network '{network_name}' must be internal")
        try:
            await asyncio.to_thread(self._docker.images.get, self._settings.sandbox_image)
        except Exception as exc:
            raise SandboxBackendError(
                f"Sandbox image '{self._settings.sandbox_image}' is unavailable"
            ) from exc
        if self._settings.storage_enabled:
            try:
                await asyncio.to_thread(self._docker.networks.get, self._settings.storage_network)
            except Exception as exc:
                raise SandboxBackendError(
                    f"Docker network '{self._settings.storage_network}' is unavailable"
                ) from exc
            try:
                await asyncio.to_thread(self._docker.images.get, self._settings.storage_ops_image)
            except Exception as exc:
                raise SandboxBackendError(
                    f"Storage operation image '{self._settings.storage_ops_image}' is unavailable"
                ) from exc

    def _validate_files(self) -> None:
        for label, path in (
            ("resolv_conf_file", self._settings.resolv_conf_file),
            ("codex_auth_file", self._settings.codex_auth_file),
            ("codex_config_file", self._settings.codex_config_file),
            ("restic_password_file", self._settings.restic_password_file),
        ):
            if path is not None and not path.is_file():
                raise SandboxBackendError(f"Configured {label} does not exist: {path}")

    async def _ensure_workspace_volume(self, workspace: WorkspaceRecord) -> None:
        labels = {
            MANAGED_LABEL: "true",
            ROLE_LABEL: "workspace",
            WORKSPACE_LABEL: workspace.id,
            WORKSPACE_KIND_LABEL: workspace.kind,
        }
        try:
            await asyncio.to_thread(
                self._docker.volumes.create,
                name=workspace.volume_name,
                labels=labels,
            )
        except Exception as exc:
            raise SandboxBackendError(
                f"Failed to create Workspace volume '{workspace.volume_name}': {exc}"
            ) from exc

    async def _prepare_workspace(self, workspace: WorkspaceRecord) -> WorkspaceRecord:
        lock = self._workspace_locks.setdefault(workspace.id, asyncio.Lock())
        async with lock:
            current = self._get_workspace(workspace.id)
            volume_exists = await self._volume_exists(current.volume_name)
            if current.status != "remote_only" and not volume_exists:
                if current.head_revision is not None and current.active_sandbox_id is None:
                    try:
                        current = self._state.transition_workspace(
                            current.id,
                            expected={"detached_clean"},
                            status="remote_only",
                        )
                    except StateConflictError as exc:
                        raise SandboxConflictError(str(exc)) from exc
                elif current.status == "detached_dirty":
                    raise SandboxBackendError(f"Dirty Workspace '{current.id}' has no local volume")
                else:
                    await self._ensure_workspace_volume(current)
                    return current

            if current.status != "remote_only":
                return current
            if (
                not self._settings.storage_enabled
                or self._operation_runner is None
                or current.head_revision is None
            ):
                raise SandboxBackendError(
                    f"Workspace '{current.id}' requires object-storage restore"
                )
            revision_id = current.head_revision

            if await self._volume_exists(current.volume_name):
                await self._remove_volume(current.volume_name)
            await self._ensure_workspace_volume(current)
            operation_id = f"operation_{uuid.uuid4().hex}"
            operation, _ = self._state.create_operation(
                operation_id,
                operation="restore",
                workspace_id=current.id,
                sandbox_id=None,
                idempotency_key=f"restore:{current.id}:{revision_id}",
                input_data={"revision_id": revision_id},
            )
            try:
                current = self._state.transition_workspace(
                    current.id,
                    expected={"remote_only"},
                    status="restoring",
                )
                self._state.update_operation(operation.id, status="running")
                result = await self._operation_runner.restore(
                    operation.id,
                    current,
                    revision_id,
                )
                self._state.update_operation(operation.id, status="succeeded", result=result)
                return self._state.transition_workspace(
                    current.id,
                    expected={"restoring"},
                    status="detached_clean",
                )
            except Exception as exc:
                await self._remove_volume(current.volume_name)
                with suppress(StateNotFoundError):
                    self._state.update_operation(
                        operation.id,
                        status="failed",
                        error=_operation_error(exc),
                    )
                with suppress(StateConflictError, StateNotFoundError):
                    self._state.transition_workspace(
                        current.id,
                        expected={"restoring"},
                        status="remote_only",
                    )
                if isinstance(exc, SandboxBackendError):
                    raise
                raise SandboxBackendError(
                    f"Failed to restore Workspace '{current.id}': {_operation_error(exc)}"
                ) from exc

    async def _volume_exists(self, name: str) -> bool:
        try:
            await asyncio.to_thread(self._docker.volumes.get, name)
        except (NotFound, KeyError):
            return False
        except Exception as exc:
            raise SandboxBackendError(
                f"Failed to inspect Workspace volume '{name}': {exc}"
            ) from exc
        return True

    async def _create_runtime_volume(self, sandbox_id: str, workspace_id: str) -> None:
        labels = {
            MANAGED_LABEL: "true",
            ROLE_LABEL: "runtime-state",
            SANDBOX_LABEL: sandbox_id,
            WORKSPACE_LABEL: workspace_id,
        }
        await asyncio.to_thread(
            self._docker.volumes.create,
            name=_runtime_state_volume(sandbox_id),
            labels=labels,
        )

    async def _remove_volume(self, name: str) -> None:
        if self._docker is None:
            return
        with suppress(Exception):
            volume = await asyncio.to_thread(self._docker.volumes.get, name)
            await asyncio.to_thread(volume.remove, force=True)

    async def _remove_volume_checked(self, name: str) -> None:
        try:
            volume = await asyncio.to_thread(self._docker.volumes.get, name)
        except (NotFound, KeyError):
            return
        try:
            await asyncio.to_thread(volume.remove, force=True)
        except Exception as exc:
            raise SandboxBackendError(f"Failed to remove Workspace volume '{name}': {exc}") from exc

    async def _wait_for_worker(self, container: Any) -> None:
        deadline = asyncio.get_running_loop().time() + self._settings.worker_start_timeout_seconds
        last_health = "starting"
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.to_thread(container.reload)
            if str(container.status) in {"exited", "dead"}:
                logs = await asyncio.to_thread(container.logs, tail=100)
                if isinstance(logs, bytes):
                    logs = logs.decode(errors="replace")
                raise SandboxBackendError(f"Sandbox worker exited during startup: {logs}")
            last_health = _health_status(container)
            if str(container.status) == "running" and last_health == "healthy":
                return
            if last_health == "unhealthy":
                logs = await asyncio.to_thread(container.logs, tail=100)
                if isinstance(logs, bytes):
                    logs = logs.decode(errors="replace")
                raise SandboxBackendError(f"Sandbox worker is unhealthy: {logs}")
            await asyncio.sleep(0.2)
        raise SandboxBackendError(f"Sandbox worker did not become ready: health={last_health}")

    async def _reconcile_managed_containers(self) -> None:
        active = {record.id: record for record in self._state.active_sandboxes()}
        seen: set[str] = set()
        containers = await asyncio.to_thread(
            self._docker.containers.list,
            all=True,
            filters={"label": f"{MANAGED_LABEL}=true"},
        )
        now = int(time.time())
        for container in containers:
            labels = container.labels or {}
            role = labels.get(ROLE_LABEL, "worker")
            if role == "operation":
                await self._remove_container(container, labels.get(OPERATION_LABEL))
                continue
            if role != "worker":
                continue
            sandbox_id = labels.get(SANDBOX_LABEL)
            state_record = active.get(sandbox_id) if isinstance(sandbox_id, str) else None
            workspace_id = labels.get(WORKSPACE_LABEL)
            valid = (
                state_record is not None
                and workspace_id == state_record.workspace_id
                and state_record.expires_at is not None
                and state_record.expires_at > now
                and await self._status(container) == "running"
            )
            if valid and state_record is not None:
                if state_record.status == "starting":
                    self._state.mark_sandbox_running(state_record.id)
                    state_record = self._state.get_sandbox(state_record.id)
                workspace = self._get_workspace(state_record.workspace_id)
                self._records[state_record.id] = self._record_from_state(
                    state_record, workspace, container
                )
                seen.add(state_record.id)
                continue

            await self._remove_container(
                container,
                sandbox_id if isinstance(sandbox_id, str) else None,
            )
            if isinstance(sandbox_id, str):
                await self._remove_volume(_runtime_state_volume(sandbox_id))
            if state_record is not None:
                await self._finish_reconciled_sandbox(state_record)
                seen.add(state_record.id)
            elif labels.get(WORKSPACE_KIND_LABEL) == "ephemeral" and isinstance(workspace_id, str):
                await self._remove_volume(_workspace_volume(workspace_id))

        for sandbox_id, state_record in active.items():
            if sandbox_id not in seen:
                await self._finish_reconciled_sandbox(state_record)

    async def _recover_incomplete_operations(self) -> None:
        for operation in self._state.incomplete_operations():
            self._state.update_operation(
                operation.id,
                status="failed",
                error="Manager restarted before the operation result was committed",
            )
        for workspace in self._state.workspaces_with_status("checkpointing"):
            with suppress(StateConflictError, StateNotFoundError):
                self._state.transition_workspace(
                    workspace.id,
                    expected={"checkpointing"},
                    status="detached_dirty",
                )
        for workspace in self._state.workspaces_with_status("restoring"):
            await self._remove_volume(workspace.volume_name)
            with suppress(StateConflictError, StateNotFoundError):
                self._state.transition_workspace(
                    workspace.id,
                    expected={"restoring"},
                    status="remote_only",
                )

    async def _finish_reconciled_sandbox(self, sandbox: SandboxRecord) -> None:
        try:
            workspace = self._state.get_workspace(sandbox.workspace_id)
        except StateNotFoundError:
            return
        workspace_status = "detached_dirty" if workspace.kind == "recoverable" else "deleting"
        with suppress(StateNotFoundError):
            self._state.finish_sandbox(
                sandbox.id,
                status="failed",
                workspace_status=workspace_status,
            )
        await self._remove_volume(_runtime_state_volume(sandbox.id))
        if workspace.kind == "ephemeral":
            await self._remove_volume(workspace.volume_name)
            with suppress(StateConflictError):
                self._state.delete_workspace(workspace.id)

    async def _finish_failed_creation(self, sandbox_id: str, workspace: WorkspaceRecord) -> None:
        workspace_status = "detached_dirty" if workspace.kind == "recoverable" else "deleting"
        with suppress(StateNotFoundError):
            self._state.finish_sandbox(
                sandbox_id,
                status="failed",
                workspace_status=workspace_status,
            )
        await self._remove_volume(_runtime_state_volume(sandbox_id))
        if workspace.kind == "ephemeral":
            await self._remove_volume(workspace.volume_name)
            with suppress(StateConflictError):
                self._state.delete_workspace(workspace.id)

    @staticmethod
    async def _remove_container(container: Any, sandbox_id: str | None) -> None:
        try:
            await asyncio.to_thread(container.remove, force=True, v=True)
        except NotFound:
            return
        except Exception as exc:
            identity = f" Sandbox '{sandbox_id}'" if sandbox_id is not None else " managed Sandbox"
            raise SandboxBackendError(f"Failed to remove{identity}: {exc}") from exc

    async def _get(self, sandbox_id: str) -> _SandboxRecord:
        async with self._lock:
            record = self._records.get(sandbox_id)
        if record is None:
            raise SandboxNotFoundError(sandbox_id)
        return record

    def _get_workspace(self, workspace_id: str) -> WorkspaceRecord:
        try:
            return self._state.get_workspace(workspace_id)
        except StateNotFoundError as exc:
            raise WorkspaceNotFoundError(str(exc)) from exc

    @staticmethod
    async def _status(container: Any) -> SandboxStatus:
        await asyncio.to_thread(container.reload)
        if str(container.status) != "running" or _health_status(container) != "healthy":
            return "failed"
        return "running"

    async def _reaper(self) -> None:
        while True:
            await asyncio.sleep(self._settings.reaper_interval_seconds)
            now = int(time.time())
            async with self._lock:
                expired = [
                    record.sandbox_id
                    for record in self._records.values()
                    if record.expires_at <= now
                ]
            for sandbox_id in expired:
                try:
                    await self.terminate(sandbox_id)
                    logger.info("Reaped expired sandbox %s", sandbox_id)
                except Exception:
                    logger.exception("Failed to reap sandbox %s", sandbox_id)
            if self._settings.storage_enabled:
                for workspace in self._state.workspaces_with_status("detached_dirty"):
                    self._schedule_operation(
                        self._checkpoint_workspace(workspace.id),
                        name=f"checkpoint-{workspace.id}",
                    )
                cutoff = now - self._settings.workspace_local_retention_seconds
                for workspace in self._state.cleanup_candidates(cutoff):
                    try:
                        await self._cleanup_local_workspace(workspace.id)
                    except Exception:
                        logger.exception(
                            "Failed to clean local volume for Workspace %s", workspace.id
                        )

    async def _cleanup_local_workspace(self, workspace_id: str) -> None:
        lock = self._workspace_locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            workspace = self._get_workspace(workspace_id)
            cutoff = int(time.time()) - self._settings.workspace_local_retention_seconds
            if (
                workspace.status != "detached_clean"
                or workspace.head_revision is None
                or workspace.active_sandbox_id is not None
                or workspace.updated_at > cutoff
            ):
                return
            await self._remove_volume_checked(workspace.volume_name)
            self._state.transition_workspace(
                workspace.id,
                expected={"detached_clean"},
                status="remote_only",
            )

    def _info(self, record: _SandboxRecord, status: SandboxStatus) -> SandboxInfo:
        worker = None
        if status == "running":
            worker = WorkerConnection(
                base_url=f"http://{record.worker_host}:{self._settings.worker_port}",
                api_key=self._worker_api_key(record.sandbox_id),
            )
        return SandboxInfo(
            id=record.sandbox_id,
            status=status,
            created_at=record.created_at,
            expires_at=record.expires_at,
            worker=worker,
            workspace_id=record.workspace_id,
            recoverable=record.recoverable,
        )

    def _worker_api_key(self, sandbox_id: str) -> str:
        digest = hmac.new(
            self._settings.worker_token_secret.encode(),
            sandbox_id.encode(),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def _authorize(
        self,
        token: str,
        operation: str | None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            claims = verify_grant(
                token,
                self._settings.operation_signing_secret,
                audience="sandbox-manager",
                operation=operation,
            )
        except GrantError as exc:
            raise SandboxAuthorizationError(str(exc)) from exc
        if claims.get("iss") != self._settings.operation_grant_issuer:
            raise SandboxAuthorizationError("Workspace grant issuer does not match")
        if workspace_id is not None and claims.get("workspace_id") != workspace_id:
            raise SandboxAuthorizationError("Workspace grant binding does not match")
        nonce = claims["nonce"]
        expires_at = claims["exp"]
        if not self._state.consume_nonce(nonce, expires_at):
            raise SandboxAuthorizationError("Workspace grant was already consumed")
        return claims

    @staticmethod
    def _record_from_state(
        sandbox: SandboxRecord, workspace: WorkspaceRecord, container: Any
    ) -> _SandboxRecord:
        assert sandbox.expires_at is not None
        return _SandboxRecord(
            sandbox_id=sandbox.id,
            workspace_id=workspace.id,
            workspace_volume=workspace.volume_name,
            recoverable=workspace.kind == "recoverable",
            container=container,
            worker_host=sandbox.worker_host,
            created_at=sandbox.created_at,
            expires_at=sandbox.expires_at,
        )


def _workspace_info(record: WorkspaceRecord) -> WorkspaceInfo:
    return WorkspaceInfo(
        id=record.id,
        kind=record.kind,  # type: ignore[arg-type]
        status=record.status,  # type: ignore[arg-type]
        generation=record.generation,
        head_revision=record.head_revision,
        active_sandbox_id=record.active_sandbox_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
        delete_after=record.delete_after,
    )


def _operation_info(record: OperationRecord) -> OperationInfo:
    return OperationInfo(
        id=record.id,
        operation=record.operation,
        status=record.status,  # type: ignore[arg-type]
        workspace_id=record.workspace_id,
        sandbox_id=record.sandbox_id,
        result=record.result,
        error=record.error,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _required_claim(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise SandboxAuthorizationError(f"Operation {name} is invalid")
    return value


def _idempotency_key(claims: dict[str, Any], operation: str) -> str:
    value = claims.get("idempotency_key")
    if value is None:
        value = claims.get("nonce")
    if not isinstance(value, str) or not value or len(value) > 256:
        raise SandboxAuthorizationError("Operation idempotency_key is invalid")
    return f"{operation}:{value}"


def _operation_error(exc: BaseException) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return message[:1000]


def _health_status(container: Any) -> str:
    state = container.attrs.get("State", {})
    health = state.get("Health", {}) if isinstance(state, dict) else {}
    status = health.get("Status") if isinstance(health, dict) else None
    return status if isinstance(status, str) else "missing"


def _workspace_volume(workspace_id: str) -> str:
    digest = hashlib.sha256(workspace_id.encode()).hexdigest()[:32]
    return f"agent-workspace-{digest}"


def _legacy_workspace_volume(sandbox_id: str) -> str:
    return f"sandbox-workspace-{sandbox_id}"


def _runtime_state_volume(sandbox_id: str) -> str:
    return f"sandbox-runtime-{sandbox_id}"
