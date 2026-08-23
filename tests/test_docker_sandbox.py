from __future__ import annotations

from typing import Any

import pytest

from sandbox_agent_host.backend import SandboxBackendError
from sandbox_agent_host.docker_backend import DockerSandboxBackend, build_container_spec
from sandbox_agent_host.settings import HostSettings


def test_container_spec_is_gvisor_isolated_and_egress_is_proxy_only() -> None:
    settings = HostSettings(
        api_key="host-secret",
        worker_api_key="worker-secret",
        image="codex-agent-worker:test",
        docker_runtime="runsc",
        docker_network="agent-egress-internal",
        egress_proxy_url="http://egress-proxy:3128",
        memory_limit="2g",
        nano_cpus=2_000_000_000,
        pids_limit=256,
    )

    spec = build_container_spec(settings, "exec_abc")

    assert spec["runtime"] == "runsc"
    assert spec["privileged"] is False
    assert spec["cap_drop"] == ["ALL"]
    assert spec["read_only"] is True
    assert "no-new-privileges:true" in spec["security_opt"]
    assert spec["network"] == "agent-egress-internal"
    assert spec["ports"] == {"8091/tcp": ("127.0.0.1", None)}
    assert spec["mem_limit"] == "2g"
    assert spec["nano_cpus"] == 2_000_000_000
    assert spec["pids_limit"] == 256
    assert spec["environment"]["HTTPS_PROXY"] == "http://egress-proxy:3128"
    assert spec["environment"]["SANDBOX_WORKER_CODEX_SANDBOX"] == "workspace-write"
    assert spec["environment"]["SANDBOX_WORKER_CODEX_APPROVAL_POLICY"] == "never"
    assert spec["volumes"]["codex-workspace-exec_abc"]["bind"] == "/workspace"
    assert spec["volumes"]["codex-workspace-exec_abc"]["mode"] == "rw"


class _Resource:
    def __init__(self, attrs: dict[str, Any] | None = None) -> None:
        self.attrs = attrs or {}


class _Collection:
    def __init__(self, resource: _Resource | None = None) -> None:
        self.resource = resource or _Resource()

    def get(self, _: str) -> _Resource:
        return self.resource

    def list(self, **_: Any) -> list[Any]:
        return []


class _Docker:
    def __init__(self, *, runtimes: list[str], network_internal: bool) -> None:
        self._runtimes = runtimes
        self.networks = _Collection(_Resource({"Internal": network_internal}))
        self.images = _Collection()
        self.containers = _Collection()

    def info(self) -> dict[str, Any]:
        return {"Runtimes": {name: {} for name in self._runtimes}}

    def close(self) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("docker_client", "message"),
    [
        (_Docker(runtimes=["runc"], network_internal=True), "runsc"),
        (_Docker(runtimes=["runsc"], network_internal=False), "must be internal"),
    ],
)
async def test_host_startup_fails_closed_without_required_isolation(
    docker_client: _Docker, message: str
) -> None:
    backend = DockerSandboxBackend(
        HostSettings(api_key="host-secret", worker_api_key="worker-secret"),
        docker_client=docker_client,
    )
    with pytest.raises(SandboxBackendError, match=message):
        await backend.startup()
