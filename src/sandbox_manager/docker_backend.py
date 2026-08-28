from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import docker
from docker.errors import NotFound

from sandbox_api.grants import GrantError, verify_grant

from .backend import (
    SandboxAuthorizationError,
    SandboxBackendError,
    SandboxConflictError,
    SandboxNotFoundError,
    WorkspaceNotFoundError,
)
from .models import SandboxInfo, SandboxStatus, WorkerConnection, WorkspaceInfo
from .settings import ManagerSettings
from .state import (
    SandboxRecord,
    StateConflictError,
    StateNotFoundError,
    StateStore,
    WorkspaceRecord,
)

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
    ) -> None:
        self._settings = settings
        self._docker: Any = docker_client
        self._state = state_store or StateStore(settings.state_db_path)
        self._records: dict[str, _SandboxRecord] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None

    async def startup(self) -> None:
        self._validate_files()
        self._state.startup()
        try:
            if self._docker is None:
                self._docker = await asyncio.to_thread(docker.from_env)
            await self._validate_docker()
            await self._reconcile_managed_containers()
            self._reaper_task = asyncio.create_task(self._reaper(), name="sandbox-manager-reaper")
        except Exception:
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

    async def create(self, workspace_grant: str | None = None) -> SandboxInfo:
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

        await self._ensure_workspace_volume(workspace)
        token = uuid.uuid4().hex
        sandbox_id = f"sandbox_{token}"
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
        return SandboxInfo(
            id=sandbox_id,
            status="terminated",
            created_at=record.created_at,
            expires_at=None,
            worker=None,
            workspace_id=record.workspace_id,
            recoverable=record.recoverable,
        )

    async def _validate_docker(self) -> None:
        info = await asyncio.to_thread(self._docker.info)
        runtimes = info.get("Runtimes") if isinstance(info, dict) else None
        if not isinstance(runtimes, dict) or self._settings.docker_runtime not in runtimes:
            raise SandboxBackendError(
                f"Docker runtime '{self._settings.docker_runtime}' is unavailable"
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

    def _validate_files(self) -> None:
        for label, path in (
            ("resolv_conf_file", self._settings.resolv_conf_file),
            ("codex_auth_file", self._settings.codex_auth_file),
            ("codex_config_file", self._settings.codex_config_file),
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
            if labels.get(ROLE_LABEL, "worker") != "worker":
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
        operation: str,
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
