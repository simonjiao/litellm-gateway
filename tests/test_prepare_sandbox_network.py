from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "overrides, valid",
    [
        ({}, True),
        ({"SANDBOX_AGENT_DNS_IP": "172.22.128.7"}, False),
        ({"SANDBOX_AGENT_DNS_IP": "172.22.0.54"}, False),
        ({"SANDBOX_ADAPTER_RPC_IP": "172.21.0.1"}, False),
        ({"SANDBOX_RPC_IP_RANGE": "172.22.128.0/17"}, False),
        ({"SANDBOX_EGRESS_PROXY_IP": "172.23.0.54"}, False),
    ],
)
def test_static_addresses_cannot_overlap_worker_pools_or_each_other(
    overrides: dict[str, str], valid: bool
) -> None:
    result = subprocess.run(
        ["bash", "-c", "source scripts/lib/network-addresses.sh; network_addresses_validate"],
        cwd=ROOT,
        env={**os.environ, **overrides},
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is valid, result.stderr


def test_existing_dynamic_network_is_rejected_without_modification(tmp_path: Path) -> None:
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "prepare-sandbox-network.sh", scripts)
    shutil.copytree(ROOT / "scripts" / "lib", scripts / "lib")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
case "$*" in
  "info "*) printf '%s\\n' '{"runc":{},"runsc":{}}' ;;
  *"{{.Internal}} agent-control") printf '%s\\n' false ;;
  *"{{.Internal}}"*) printf '%s\\n' true ;;
  *"{{.Driver}}"*) printf '%s\\n' bridge ;;
  *"{{.Scope}}"*) printf '%s\\n' local ;;
  *"{{.EnableIPv6}}"*) printf '%s\\n' false ;;
  *".IPAM.Config"*) printf '%s\\n' '172.21.0.0/16  172.21.0.1' ;;
  "network inspect "*) exit 0 ;;
  *) printf '%s\\n' "$*" >>"${MUTATIONS}" ;;
esac
"""
    )
    docker.chmod(0o755)
    mutations = tmp_path / "mutations"
    result = subprocess.run(
        ["/bin/bash", "scripts/prepare-sandbox-network.sh"],
        cwd=project,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "MUTATIONS": str(mutations)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "IPAM differs" in result.stderr
    assert not mutations.exists()


def test_existing_agent_network_must_be_a_local_ipv4_bridge(tmp_path: Path) -> None:
    project = tmp_path / "project"
    scripts = project / "scripts"
    fake_bin = tmp_path / "bin"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    shutil.copy2(
        ROOT / "scripts" / "prepare-sandbox-network.sh",
        scripts / "prepare-sandbox-network.sh",
    )
    shutil.copytree(ROOT / "scripts" / "lib", scripts / "lib")
    (project / ".env").write_text("")

    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
case "$*" in
  "info --format {{json .Runtimes}}") printf '%s\\n' '{"runc":{},"runsc":{}}' ;;
  *"--format {{.Internal}} agent-control") printf '%s\\n' false ;;
  *"--format {{.Internal}} agent-rpc") printf '%s\\n' true ;;
  *"--format {{.Internal}} agent-egress") printf '%s\\n' true ;;
  *"--format {{.Driver}} agent-rpc") printf '%s\\n' overlay ;;
  *"network inspect"*) exit 0 ;;
esac
exit 0
"""
    )
    docker.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = subprocess.run(
        ["/bin/bash", "scripts/prepare-sandbox-network.sh"],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "local IPv4 bridge" in result.stderr
