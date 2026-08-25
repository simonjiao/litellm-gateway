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

from .backend import SandboxBackendError, SandboxNotFoundError
from .models import SandboxInfo, SandboxStatus, WorkerConnection
from .settings import ManagerSettings

logger = logging.getLogger(__name__)

MANAGED_LABEL = "io.litellm-codex-gateway.managed"
SANDBOX_LABEL = "io.litellm-codex-gateway.sandbox-id"
CREATED_LABEL = "io.litellm-codex-gateway.created-at"
WORKER_HOST_LABEL = "io.litellm-codex-gateway.worker-host"


@dataclass(slots=True)
class _SandboxRecord:
    sandbox_id: str
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
) -> dict[str, Any]:
    volumes: dict[str, dict[str, str]] = {
        _workspace_volume(sandbox_id): {"bind": "/workspace", "mode": "rw"},
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
            SANDBOX_LABEL: sandbox_id,
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
    """Docker implementation of the Sandbox lifecycle control plane."""

    def __init__(self, settings: ManagerSettings, *, docker_client: Any | None = None) -> None:
        self._settings = settings
        self._docker: Any = docker_client
        self._records: dict[str, _SandboxRecord] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None

    async def startup(self) -> None:
        self._validate_files()
        try:
            if self._docker is None:
                self._docker = await asyncio.to_thread(docker.from_env)
            await self._validate_docker()
            await self._discard_managed_containers()
            self._reaper_task = asyncio.create_task(
                self._reaper(), name="sandbox-manager-reaper"
            )
        except Exception:
            if self._docker is not None:
                with suppress(Exception):
                    await asyncio.to_thread(self._docker.close)
            raise

    async def shutdown(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reaper_task
        if self._docker is not None:
            await asyncio.to_thread(self._docker.close)

    async def create(self) -> SandboxInfo:
        token = uuid.uuid4().hex
        sandbox_id = f"sandbox_{token}"
        worker_host = f"sandbox-worker-{token}"
        worker_api_key = self._worker_api_key(sandbox_id)
        spec = build_container_spec(
            self._settings,
            sandbox_id,
            worker_host=worker_host,
            worker_api_key=worker_api_key,
        )
        container: Any = None
        try:
            await self._create_volumes(sandbox_id)
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
            now = int(time.time())
            record = _SandboxRecord(
                sandbox_id=sandbox_id,
                container=container,
                worker_host=worker_host,
                created_at=now,
                expires_at=now + self._settings.execution_ttl_seconds,
            )
            async with self._lock:
                self._records[sandbox_id] = record
            return self._info(record, "running")
        except BaseException as creation_error:
            if container is not None:
                try:
                    await self._remove_container(container, sandbox_id)
                except SandboxBackendError as cleanup_error:
                    now = int(time.time())
                    async with self._lock:
                        self._records[sandbox_id] = _SandboxRecord(
                            sandbox_id=sandbox_id,
                            container=container,
                            worker_host=worker_host,
                            created_at=now,
                            expires_at=now,
                        )
                    raise SandboxBackendError(
                        f"Sandbox creation failed and Worker cleanup failed: {cleanup_error}"
                    ) from creation_error
            await self._remove_volumes(sandbox_id)
            raise

    async def inspect(self, sandbox_id: str) -> SandboxInfo:
        record = await self._get(sandbox_id)
        return self._info(record, await self._status(record.container))

    async def renew(self, sandbox_id: str) -> SandboxInfo:
        record = await self._get(sandbox_id)
        status = await self._status(record.container)
        if status == "running":
            async with self._lock:
                if sandbox_id in self._records:
                    record.expires_at = int(time.time()) + self._settings.execution_ttl_seconds
        return self._info(record, status)

    async def terminate(self, sandbox_id: str) -> SandboxInfo:
        record = await self._get(sandbox_id)
        await self._remove_container(record.container, sandbox_id)
        async with self._lock:
            if self._records.get(sandbox_id) is record:
                self._records.pop(sandbox_id)
        await self._remove_volumes(sandbox_id)
        return SandboxInfo(
            id=sandbox_id,
            status="terminated",
            created_at=record.created_at,
            expires_at=None,
            worker=None,
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
                raise SandboxBackendError(
                    f"Docker network '{network_name}' must be internal"
                )
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

    async def _create_volumes(self, sandbox_id: str) -> None:
        labels = {MANAGED_LABEL: "true", SANDBOX_LABEL: sandbox_id}
        for name in (_workspace_volume(sandbox_id), _runtime_state_volume(sandbox_id)):
            await asyncio.to_thread(self._docker.volumes.create, name=name, labels=labels)

    async def _remove_volumes(self, sandbox_id: str) -> None:
        if self._docker is None:
            return
        for name in (_workspace_volume(sandbox_id), _runtime_state_volume(sandbox_id)):
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
        raise SandboxBackendError(
            f"Sandbox worker did not become ready: health={last_health}"
        )

    async def _discard_managed_containers(self) -> None:
        containers = await asyncio.to_thread(
            self._docker.containers.list,
            all=True,
            filters={"label": f"{MANAGED_LABEL}=true"},
        )
        for container in containers:
            labels = container.labels or {}
            sandbox_id = labels.get(SANDBOX_LABEL)
            valid_sandbox_id = (
                sandbox_id
                if isinstance(sandbox_id, str) and sandbox_id.startswith("sandbox_")
                else None
            )
            await self._remove_container(container, valid_sandbox_id)
            if valid_sandbox_id is not None:
                await self._remove_volumes(valid_sandbox_id)

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
        )

    def _worker_api_key(self, sandbox_id: str) -> str:
        digest = hmac.new(
            self._settings.worker_token_secret.encode(),
            sandbox_id.encode(),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _health_status(container: Any) -> str:
    state = container.attrs.get("State", {})
    health = state.get("Health", {}) if isinstance(state, dict) else {}
    status = health.get("Status") if isinstance(health, dict) else None
    return status if isinstance(status, str) else "missing"


def _workspace_volume(sandbox_id: str) -> str:
    return f"sandbox-workspace-{sandbox_id}"


def _runtime_state_volume(sandbox_id: str) -> str:
    return f"sandbox-runtime-{sandbox_id}"
