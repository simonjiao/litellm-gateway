from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

import docker
import httpx

from .backend import ExecutionNotFoundError, SandboxBackendError
from .models import AgentEvent, ExecutionInfo
from .settings import HostSettings
from .worker_client import WorkerClient

logger = logging.getLogger(__name__)

MANAGED_LABEL = "io.litellm-codex-gateway.managed"
EXECUTION_LABEL = "io.litellm-codex-gateway.execution-id"
CREATED_LABEL = "io.litellm-codex-gateway.created-at"


@dataclass(slots=True)
class _SandboxRecord:
    execution_id: str
    container: Any
    worker_url: str
    created_at: int
    expires_at: int
    last_event_id: int = -1
    subscribers: int = 0
    busy: int = 0


def build_container_spec(
    settings: HostSettings,
    execution_id: str,
) -> dict[str, Any]:
    workspace_volume = _workspace_volume(execution_id)
    codex_home_volume = _codex_home_volume(execution_id)
    volumes: dict[str, dict[str, str]] = {
        workspace_volume: {"bind": "/workspace", "mode": "rw"},
        codex_home_volume: {"bind": "/home/agent/.codex", "mode": "rw"},
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

    environment = {
        "SANDBOX_WORKER_API_KEY": settings.worker_api_key,
        "SANDBOX_WORKER_HOST": "0.0.0.0",
        "SANDBOX_WORKER_PORT": str(settings.worker_port),
        "SANDBOX_WORKER_CODEX_COMMAND": settings.codex_command,
        "SANDBOX_WORKER_CODEX_WORKDIR": "/workspace",
        "SANDBOX_WORKER_CODEX_MODEL": settings.codex_model or "",
        "SANDBOX_WORKER_CODEX_SANDBOX": "workspace-write",
        "SANDBOX_WORKER_CODEX_APPROVAL_POLICY": "never",
        "SANDBOX_WORKER_MCP_APPS_ENABLED": str(settings.mcp_apps_enabled).lower(),
        "HTTP_PROXY": settings.egress_proxy_url,
        "HTTPS_PROXY": settings.egress_proxy_url,
        "ALL_PROXY": settings.egress_proxy_url,
        "NO_PROXY": "127.0.0.1,localhost",
    }
    return {
        "name": f"codex-agent-{execution_id}",
        "detach": True,
        "init": True,
        "runtime": settings.docker_runtime,
        "network": settings.docker_network,
        "ports": {f"{settings.worker_port}/tcp": ("127.0.0.1", None)},
        "environment": environment,
        "volumes": volumes,
        "labels": {
            MANAGED_LABEL: "true",
            EXECUTION_LABEL: execution_id,
            CREATED_LABEL: str(int(time.time())),
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
    """Docker/gVisor implementation of the AgentExecution lifecycle."""

    def __init__(self, settings: HostSettings, *, docker_client: Any | None = None) -> None:
        self._settings = settings
        self._docker: Any = docker_client
        self._worker = WorkerClient(
            settings.worker_api_key,
            timeout_seconds=settings.worker_start_timeout_seconds,
        )
        self._records: dict[str, _SandboxRecord] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None

    async def startup(self) -> None:
        self._validate_files()
        try:
            if self._docker is None:
                self._docker = await asyncio.to_thread(docker.from_env)
            await self._validate_docker()
            await self._recover_managed_containers()
            self._reaper_task = asyncio.create_task(
                self._reaper(), name="sandbox-agent-host-reaper"
            )
        except Exception:
            await self._worker.aclose()
            if self._docker is not None:
                with suppress(Exception):
                    await asyncio.to_thread(self._docker.close)
            raise

    async def shutdown(self) -> None:
        # Containers deliberately outlive the host process; the next host instance
        # adopts them and its TTL reaper removes orphans.
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reaper_task
        await self._worker.aclose()
        if self._docker is not None:
            await asyncio.to_thread(self._docker.close)

    async def create(self) -> ExecutionInfo:
        execution_id = f"exec_{uuid.uuid4().hex}"
        spec = build_container_spec(self._settings, execution_id)
        await self._create_volumes(execution_id)
        container: Any = None
        try:
            container = await asyncio.to_thread(
                self._docker.containers.run,
                self._settings.image,
                **spec,
            )
            worker_url = await self._wait_for_worker(container)
            now = int(time.time())
            health = await self._worker.health(worker_url)
            last_event_id = int(health.get("last_event_id", -1))
            record = _SandboxRecord(
                execution_id=execution_id,
                container=container,
                worker_url=worker_url,
                created_at=now,
                expires_at=now + self._settings.execution_ttl_seconds,
                last_event_id=last_event_id,
            )
            async with self._lock:
                self._records[execution_id] = record
            return self._info(record, "running")
        except Exception:
            if container is not None:
                with suppress(Exception):
                    await asyncio.to_thread(container.remove, force=True, v=True)
            await self._remove_volumes(execution_id)
            raise

    async def inspect(self, execution_id: str) -> ExecutionInfo:
        record = await self._get(execution_id)
        await asyncio.to_thread(record.container.reload)
        status = str(record.container.status)
        if status != "running":
            return self._info(record, "failed")
        await self._touch(record)
        health = await self._worker.health(record.worker_url)
        record.last_event_id = int(health.get("last_event_id", record.last_event_id))
        return self._info(record, "running")

    async def rpc(self, execution_id: str, method: str, params: dict[str, Any]) -> Any:
        record = await self._get(execution_id)
        async with self._activity(record):
            return await self._worker.rpc(record.worker_url, method, params)

    async def resolve_server_request(
        self,
        execution_id: str,
        request_id: int | str,
        *,
        result: Any | None,
        error: dict[str, Any] | None,
    ) -> None:
        record = await self._get(execution_id)
        async with self._activity(record):
            await self._worker.resolve_server_request(
                record.worker_url,
                request_id,
                result=result,
                error=error,
            )

    async def events(
        self, execution_id: str, *, after: int, follow: bool
    ) -> AsyncIterator[AgentEvent]:
        record = await self._get(execution_id)
        async with self._subscription(record):
            async for event in self._worker.events(record.worker_url, after=after, follow=follow):
                record.last_event_id = max(record.last_event_id, event.id)
                await self._touch(record)
                yield event

    async def terminate(self, execution_id: str) -> ExecutionInfo:
        record = await self._get(execution_id)
        async with self._lock:
            self._records.pop(execution_id, None)
        with suppress(Exception):
            await asyncio.to_thread(record.container.remove, force=True, v=True)
        await self._remove_volumes(execution_id)
        return ExecutionInfo(
            id=execution_id,
            status="terminated",
            created_at=record.created_at,
            expires_at=None,
            last_event_id=record.last_event_id,
        )

    async def _validate_docker(self) -> None:
        info = await asyncio.to_thread(self._docker.info)
        runtimes = info.get("Runtimes") if isinstance(info, dict) else None
        if not isinstance(runtimes, dict) or self._settings.docker_runtime not in runtimes:
            raise SandboxBackendError(
                f"Docker runtime '{self._settings.docker_runtime}' is unavailable"
            )
        try:
            network = await asyncio.to_thread(
                self._docker.networks.get, self._settings.docker_network
            )
        except Exception as exc:
            raise SandboxBackendError(
                f"Docker network '{self._settings.docker_network}' is unavailable"
            ) from exc
        if network.attrs.get("Internal") is not True:
            raise SandboxBackendError(
                f"Docker network '{self._settings.docker_network}' must be internal; "
                "sandbox egress must pass through the configured proxy"
            )
        try:
            await asyncio.to_thread(self._docker.images.get, self._settings.image)
        except Exception as exc:
            raise SandboxBackendError(
                f"Sandbox worker image '{self._settings.image}' is unavailable"
            ) from exc

    def _validate_files(self) -> None:
        for label, path in (
            ("codex_auth_file", self._settings.codex_auth_file),
            ("codex_config_file", self._settings.codex_config_file),
        ):
            if path is not None and not path.is_file():
                raise SandboxBackendError(f"Configured {label} does not exist: {path}")

    async def _create_volumes(self, execution_id: str) -> None:
        labels = {MANAGED_LABEL: "true", EXECUTION_LABEL: execution_id}
        for name in (_workspace_volume(execution_id), _codex_home_volume(execution_id)):
            await asyncio.to_thread(self._docker.volumes.create, name=name, labels=labels)

    async def _remove_volumes(self, execution_id: str) -> None:
        if self._docker is None:
            return
        for name in (_workspace_volume(execution_id), _codex_home_volume(execution_id)):
            with suppress(Exception):
                volume = await asyncio.to_thread(self._docker.volumes.get, name)
                await asyncio.to_thread(volume.remove, force=True)

    async def _wait_for_worker(self, container: Any) -> str:
        deadline = asyncio.get_running_loop().time() + self._settings.worker_start_timeout_seconds
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.to_thread(container.reload)
            if str(container.status) in {"exited", "dead"}:
                logs = await asyncio.to_thread(container.logs, tail=100)
                raise SandboxBackendError(
                    f"Sandbox worker exited during startup: {logs.decode(errors='replace')}"
                )
            worker_url = _worker_url(container, self._settings.worker_port)
            if worker_url is not None:
                try:
                    await self._worker.health(worker_url)
                    return worker_url
                except (httpx.HTTPError, SandboxBackendError) as exc:
                    last_error = exc
            await asyncio.sleep(0.2)
        raise SandboxBackendError(f"Sandbox worker did not become ready: {last_error or 'timeout'}")

    async def _recover_managed_containers(self) -> None:
        containers = await asyncio.to_thread(
            self._docker.containers.list,
            all=True,
            filters={"label": f"{MANAGED_LABEL}=true"},
        )
        for container in containers:
            labels = container.labels or {}
            execution_id = labels.get(EXECUTION_LABEL)
            if not isinstance(execution_id, str) or not execution_id.startswith("exec_"):
                continue
            await asyncio.to_thread(container.reload)
            worker_url = _worker_url(container, self._settings.worker_port)
            if str(container.status) != "running" or worker_url is None:
                with suppress(Exception):
                    await asyncio.to_thread(container.remove, force=True, v=True)
                await self._remove_volumes(execution_id)
                continue
            try:
                health = await self._worker.health(worker_url)
            except Exception:
                logger.warning("Recovered sandbox %s is not healthy; removing it", execution_id)
                with suppress(Exception):
                    await asyncio.to_thread(container.remove, force=True, v=True)
                await self._remove_volumes(execution_id)
                continue
            now = int(time.time())
            created_at = _int_or(labels.get(CREATED_LABEL), now)
            self._records[execution_id] = _SandboxRecord(
                execution_id=execution_id,
                container=container,
                worker_url=worker_url,
                created_at=created_at,
                expires_at=now + self._settings.execution_ttl_seconds,
                last_event_id=_int_or(health.get("last_event_id"), -1),
            )

    async def _get(self, execution_id: str) -> _SandboxRecord:
        async with self._lock:
            record = self._records.get(execution_id)
        if record is None:
            raise ExecutionNotFoundError(execution_id)
        return record

    async def _touch(self, record: _SandboxRecord) -> None:
        async with self._lock:
            if record.execution_id in self._records:
                record.expires_at = int(time.time()) + self._settings.execution_ttl_seconds

    @asynccontextmanager
    async def _activity(self, record: _SandboxRecord) -> AsyncGenerator[None, None]:
        async with self._lock:
            record.busy += 1
            record.expires_at = int(time.time()) + self._settings.execution_ttl_seconds
        try:
            yield
        finally:
            async with self._lock:
                record.busy = max(0, record.busy - 1)
                record.expires_at = int(time.time()) + self._settings.execution_ttl_seconds

    @asynccontextmanager
    async def _subscription(self, record: _SandboxRecord) -> AsyncGenerator[None, None]:
        async with self._lock:
            record.subscribers += 1
            record.expires_at = int(time.time()) + self._settings.execution_ttl_seconds
        try:
            yield
        finally:
            async with self._lock:
                record.subscribers = max(0, record.subscribers - 1)
                record.expires_at = int(time.time()) + self._settings.execution_ttl_seconds

    async def _reaper(self) -> None:
        while True:
            await asyncio.sleep(self._settings.reaper_interval_seconds)
            now = int(time.time())
            async with self._lock:
                expired = [
                    record.execution_id
                    for record in self._records.values()
                    if record.expires_at <= now and record.busy == 0 and record.subscribers == 0
                ]
            for execution_id in expired:
                try:
                    await self.terminate(execution_id)
                    logger.info("Reaped idle sandbox %s", execution_id)
                except Exception:
                    logger.exception("Failed to reap sandbox %s", execution_id)

    @staticmethod
    def _info(record: _SandboxRecord, status: str) -> ExecutionInfo:
        return ExecutionInfo(
            id=record.execution_id,
            status=status,  # type: ignore[arg-type]
            created_at=record.created_at,
            expires_at=record.expires_at,
            last_event_id=record.last_event_id,
        )


def _worker_url(container: Any, worker_port: int) -> str | None:
    bindings = container.attrs.get("NetworkSettings", {}).get("Ports", {}).get(f"{worker_port}/tcp")
    if not isinstance(bindings, list) or not bindings:
        return None
    host_port = bindings[0].get("HostPort")
    if not isinstance(host_port, str) or not host_port:
        return None
    return f"http://127.0.0.1:{host_port}"


def _workspace_volume(execution_id: str) -> str:
    return f"codex-workspace-{execution_id}"


def _codex_home_volume(execution_id: str) -> str:
    return f"codex-home-{execution_id}"


def _int_or(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
