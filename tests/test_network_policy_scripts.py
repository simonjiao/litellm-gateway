from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _policy_project(
    tmp_path: Path, script_name: str, *, extra_env: str = ""
) -> tuple[Path, Path, dict[str, str]]:
    project = tmp_path / "project"
    scripts = project / "scripts"
    fake_bin = tmp_path / "bin"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    shutil.copy2(ROOT / "scripts" / script_name, scripts / script_name)
    source_library = ROOT / "scripts" / "lib" / "network-policy.sh"
    if source_library.exists():
        library_dir = scripts / "lib"
        library_dir.mkdir()
        shutil.copy2(source_library, library_dir / source_library.name)
    (project / ".env").write_text(
        "SANDBOX_MANAGER_RPC_NETWORK=agent-rpc\n"
        "SANDBOX_MANAGER_EGRESS_NETWORK=agent-egress\n"
        "SANDBOX_MANAGER_WORKER_PORT=8091\n"
        "SANDBOX_NETWORK_POLICY_IMAGE=network-policy:test\n"
        f"{extra_env}"
    )

    iptables_log = tmp_path / "iptables.log"
    _write_executable(
        fake_bin / "sudo",
        """#!/bin/sh
if [ "$1" = "-n" ] && [ "$2" = "true" ]; then
  exit 0
fi
if [ "$1" = "-n" ] && [ "$2" = "iptables" ]; then
  shift 2
  exec iptables "$@"
fi
exit 1
""",
    )
    _write_executable(
        fake_bin / "iptables",
        """#!/bin/sh
printf '%s\\n' "$*" >>"${IPTABLES_LOG}"
case "$1" in
  -nL) exit 0 ;;
  -C) exit 1 ;;
  -S) exit 0 ;;
esac
exit 0
""",
    )
    _write_executable(
        fake_bin / "docker",
        """#!/bin/sh
case "$*" in
  "compose ps -q responses-adapter") printf '%s\\n' adapter-id ;;
  *"container inspect"*"missing"*) exit 1 ;;
  *"container inspect"*) printf '%s\\n' 172.30.0.2 ;;
  *"com.docker.network.bridge.name"*) printf '%s\\n' '<no value>' ;;
  *"network inspect"*".Id"*) printf '%s\\n' 0123456789abcdef ;;
esac
exit 0
""",
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["IPTABLES_LOG"] = str(iptables_log)
    return project, iptables_log, env


def _run_policy_script(
    tmp_path: Path, script_name: str, *, extra_env: str = ""
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    project, iptables_log, env = _policy_project(
        tmp_path, script_name, extra_env=extra_env
    )
    result = subprocess.run(
        ["/bin/bash", f"scripts/{script_name}"],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    log_lines = iptables_log.read_text().splitlines() if iptables_log.exists() else []
    commands = [shlex.split(line) for line in log_lines if line.strip()]
    return result, commands


def test_rpc_policy_blocks_agent_ingress_to_other_networks_and_the_host(
    tmp_path: Path,
) -> None:
    result, commands = _run_policy_script(tmp_path, "apply-agent-rpc-policy.sh")

    assert result.returncode == 0, result.stderr
    forward_hooks = [
        command
        for command in commands
        if "DOCKER-USER" in command and "-i" in command and "-j" in command
    ]
    assert forward_hooks
    assert all("-o" not in command for command in forward_hooks)
    assert any(
        "INPUT" in command and "-i" in command and "-j" in command
        for command in commands
    )


def test_egress_policy_blocks_agent_ingress_to_other_networks_and_the_host(
    tmp_path: Path,
) -> None:
    result, commands = _run_policy_script(tmp_path, "apply-agent-egress-policy.sh")

    assert result.returncode == 0, result.stderr
    forward_hooks = [
        command
        for command in commands
        if "DOCKER-USER" in command and "-i" in command and "-j" in command
    ]
    assert forward_hooks
    assert all("-o" not in command for command in forward_hooks)
    assert any(
        "INPUT" in command and "-i" in command and "-j" in command
        for command in commands
    )


def test_egress_policy_validates_every_destination_before_mutating_firewall(
    tmp_path: Path,
) -> None:
    result, commands = _run_policy_script(
        tmp_path,
        "apply-agent-egress-policy.sh",
        extra_env="SANDBOX_AGENT_INTERNAL_SERVICES=service=missing:8080\n",
    )

    assert result.returncode != 0
    mutating_operations = {"-N", "-F", "-A", "-I", "-R", "-D", "-X"}
    assert not any(command[0] in mutating_operations for command in commands)


@pytest.mark.parametrize(
    "script_name",
    ["apply-agent-rpc-policy.sh", "apply-agent-egress-policy.sh"],
)
def test_policy_rebuilds_an_unreferenced_chain_before_switching(
    tmp_path: Path, script_name: str
) -> None:
    result, commands = _run_policy_script(tmp_path, script_name)

    assert result.returncode == 0, result.stderr
    hook = next(
        command
        for command in commands
        if command[:3] == ["-I", "DOCKER-USER", "1"] and "-j" in command
    )
    dispatcher = hook[hook.index("-j") + 1]
    flushed_chains = {
        command[1] for command in commands if command[0] == "-F" and len(command) == 2
    }
    assert dispatcher not in flushed_chains
    switches = [
        command
        for command in commands
        if command[0] in {"-A", "-R"}
        and len(command) > 3
        and command[1] == dispatcher
        and "-j" in command
    ]
    assert switches
    active_chain = switches[-1][switches[-1].index("-j") + 1]
    assert active_chain in flushed_chains
