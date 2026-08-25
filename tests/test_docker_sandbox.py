from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from sandbox_manager.backend import SandboxBackendError, SandboxNotFoundError
from sandbox_manager.docker_backend import DockerSandboxBackend, build_container_spec
from sandbox_manager.settings import ManagerSettings


def _settings(**overrides: Any) -> ManagerSettings:
    values: dict[str, Any] = {
        "api_key": "manager-secret",
        "worker_token_secret": "worker-token-secret-at-least-32-bytes",
        "sandbox_image": "sandbox-worker:test",
        "docker_runtime": "runsc",
        "rpc_network": "agent-rpc",
        "egress_network": "agent-egress",
    }
    values.update(overrides)
    return ManagerSettings(**values)


def test_manager_reads_sandbox_image_without_renaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_IMAGE", "sandbox-image:test")

    assert ManagerSettings().sandbox_image == "sandbox-image:test"


def test_worker_container_spec_enforces_runtime_filesystem_and_network_boundaries() -> None:
    settings = _settings(
        resolv_conf_file=Path("/opt/deployment/resolv.conf"),
        egress_proxy_url="http://egress-proxy:3128",
        internal_no_proxy="mcp-gateway,local-model",
        memory_limit="2g",
        nano_cpus=2_000_000_000,
        pids_limit=256,
    )

    spec = build_container_spec(
        settings,
        "sandbox_abc",
        worker_host="sandbox-worker-abc",
        worker_api_key="worker-specific-secret",
    )

    assert spec["runtime"] == "runsc"
    assert spec["network"] == "agent-rpc"
    assert spec["name"] == "sandbox-worker-abc"
    assert "network_aliases" not in spec
    assert "ports" not in spec
    assert "ip" not in spec
    assert spec["privileged"] is False
    assert spec["cap_drop"] == ["ALL"]
    assert spec["read_only"] is True
    assert "no-new-privileges:true" in spec["security_opt"]
    assert spec["mem_limit"] == "2g"
    assert spec["nano_cpus"] == 2_000_000_000
    assert spec["pids_limit"] == 256
    assert spec["environment"]["SANDBOX_WORKER_API_KEY"] == "worker-specific-secret"
    assert spec["environment"]["HTTPS_PROXY"] == "http://egress-proxy:3128"
    assert spec["environment"]["NO_PROXY"] == (
        "127.0.0.1,localhost,mcp-gateway,local-model"
    )
    assert spec["volumes"]["sandbox-workspace-sandbox_abc"] == {
        "bind": "/workspace",
        "mode": "rw",
    }
    assert spec["volumes"]["/opt/deployment/resolv.conf"] == {
        "bind": "/etc/resolv.conf",
        "mode": "ro",
    }


class _Resource:
    def __init__(self, attrs: dict[str, Any] | None = None) -> None:
        self.attrs = attrs or {}
        self.status = "running"
        self.labels: dict[str, str] = {}
        self.removed = False
        self.remove_error: Exception | None = None
        self.networks_at_start: set[str] | None = None

    def reload(self) -> None:
        return None

    def logs(self, **_: Any) -> bytes:
        return b""

    def remove(self, **_: Any) -> None:
        if self.remove_error is not None:
            raise self.remove_error
        self.removed = True

    def start(self) -> None:
        self.networks_at_start = set(self.attrs["NetworkSettings"]["Networks"])


class _Network(_Resource):
    def __init__(
        self,
        name: str,
        *,
        internal: bool,
        connect_error: Exception | None = None,
    ) -> None:
        super().__init__({"Internal": internal})
        self.name = name
        self.connected: list[_Resource] = []
        self.connect_error = connect_error

    def connect(self, container: _Resource) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected.append(container)
        networks = container.attrs["NetworkSettings"]["Networks"]
        networks[self.name] = {}


class _Networks:
    def __init__(
        self,
        *,
        rpc_internal: bool,
        egress_internal: bool,
        egress_connect_error: Exception | None,
    ) -> None:
        self.items = {
            "agent-rpc": _Network("agent-rpc", internal=rpc_internal),
            "agent-egress": _Network(
                "agent-egress",
                internal=egress_internal,
                connect_error=egress_connect_error,
            ),
        }

    def get(self, name: str) -> _Network:
        return self.items[name]


class _Containers:
    def __init__(self, *, remove_error: Exception | None) -> None:
        self.specs: list[dict[str, Any]] = []
        self.created: list[_Resource] = []
        self.remove_error = remove_error

    def create(self, _: str, **spec: Any) -> _Resource:
        self.specs.append(spec)
        container = _Resource(
            {
                "State": {"Health": {"Status": "healthy"}},
                "NetworkSettings": {"Networks": {spec["network"]: {}}},
            }
        )
        container.labels = dict(spec["labels"])
        container.remove_error = self.remove_error
        self.created.append(container)
        return container

    def list(self, **_: Any) -> list[_Resource]:
        return [container for container in self.created if not container.removed]


class _Volumes:
    def __init__(self) -> None:
        self.items: dict[str, _Resource] = {}

    def create(self, *, name: str, **_: Any) -> _Resource:
        volume = _Resource()
        self.items[name] = volume
        return volume

    def get(self, name: str) -> _Resource:
        return self.items[name]


class _Collection:
    def get(self, _: str) -> _Resource:
        return _Resource()


class _Docker:
    def __init__(
        self,
        *,
        runtimes: list[str],
        rpc_internal: bool = True,
        egress_internal: bool = True,
        egress_connect_error: Exception | None = None,
        container_remove_error: Exception | None = None,
    ) -> None:
        self._runtimes = runtimes
        self.networks = _Networks(
            rpc_internal=rpc_internal,
            egress_internal=egress_internal,
            egress_connect_error=egress_connect_error,
        )
        self.images = _Collection()
        self.containers = _Containers(remove_error=container_remove_error)
        self.volumes = _Volumes()

    def info(self) -> dict[str, Any]:
        return {"Runtimes": {name: {} for name in self._runtimes}}

    def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_manager_creates_dns_discovered_workers_with_independent_credentials() -> None:
    docker_client = _Docker(runtimes=["runsc"])
    backend = DockerSandboxBackend(_settings(), docker_client=docker_client)
    await backend.startup()
    try:
        first = await backend.create()
        second = await backend.create()
    finally:
        await backend.shutdown()

    assert first.worker is not None
    assert second.worker is not None
    assert first.worker.base_url.startswith("http://sandbox-worker-")
    assert first.worker.base_url.endswith(":8091")
    assert first.worker.base_url != second.worker.base_url
    assert first.worker.api_key != second.worker.api_key
    assert all(spec["network"] == "agent-rpc" for spec in docker_client.containers.specs)
    assert len(docker_client.networks.items["agent-egress"].connected) == 2
    assert all(
        container.networks_at_start == {"agent-rpc", "agent-egress"}
        for container in docker_client.containers.created
    )


@pytest.mark.asyncio
async def test_manager_reports_termination_only_after_worker_removal() -> None:
    docker_client = _Docker(runtimes=["runsc"])
    backend = DockerSandboxBackend(_settings(), docker_client=docker_client)
    await backend.startup()
    sandbox = await backend.create()
    container = docker_client.containers.created[0]
    container.remove_error = RuntimeError("remove failed")

    try:
        with pytest.raises(SandboxBackendError, match="remove failed"):
            await backend.terminate(sandbox.id)

        assert (await backend.inspect(sandbox.id)).status == "running"

        container.remove_error = None
        terminated = await backend.terminate(sandbox.id)
        assert terminated.status == "terminated"
        assert container.removed is True
    finally:
        await backend.shutdown()


@pytest.mark.asyncio
async def test_manager_discards_workers_without_recoverable_lease_on_restart() -> None:
    docker_client = _Docker(runtimes=["runsc"])
    first_backend = DockerSandboxBackend(_settings(), docker_client=docker_client)
    await first_backend.startup()
    sandbox = await first_backend.create()
    await first_backend.shutdown()

    second_backend = DockerSandboxBackend(_settings(), docker_client=docker_client)
    await second_backend.startup()
    try:
        assert docker_client.containers.created[0].removed is True
        with pytest.raises(SandboxNotFoundError):
            await second_backend.inspect(sandbox.id)
    finally:
        await second_backend.shutdown()


@pytest.mark.asyncio
async def test_manager_retries_worker_cleanup_after_creation_failure() -> None:
    docker_client = _Docker(
        runtimes=["runsc"],
        egress_connect_error=RuntimeError("connect failed"),
        container_remove_error=RuntimeError("remove failed"),
    )
    backend = DockerSandboxBackend(
        _settings(reaper_interval_seconds=0.01),
        docker_client=docker_client,
    )
    await backend.startup()

    try:
        with pytest.raises(SandboxBackendError, match="cleanup failed"):
            await backend.create()

        container = docker_client.containers.created[0]
        assert container.removed is False
        container.remove_error = None
        for _ in range(100):
            if container.removed:
                break
            await asyncio.sleep(0.01)
        assert container.removed is True
    finally:
        await backend.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("docker_client", "message"),
    [
        (_Docker(runtimes=["runc"]), "runsc"),
        (_Docker(runtimes=["runsc"], rpc_internal=False), "agent-rpc.*internal"),
        (_Docker(runtimes=["runsc"], egress_internal=False), "agent-egress.*internal"),
    ],
)
async def test_manager_fails_closed_without_required_isolation(
    docker_client: _Docker, message: str
) -> None:
    backend = DockerSandboxBackend(_settings(), docker_client=docker_client)
    with pytest.raises(SandboxBackendError, match=message):
        await backend.startup()
