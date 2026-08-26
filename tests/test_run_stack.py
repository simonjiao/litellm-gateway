from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _run_stack(
    tmp_path: Path, *, extra_env: str = "", fail_script: str = ""
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    project = tmp_path / "project"
    scripts = project / "scripts"
    runtime_dns = project / ".runtime" / "agent-dns"
    fake_bin = tmp_path / "bin"
    scripts.mkdir(parents=True)
    runtime_dns.mkdir(parents=True)
    fake_bin.mkdir()
    shutil.copy2(ROOT / "scripts" / "run-stack.sh", scripts / "run-stack.sh")
    shutil.copytree(ROOT / "scripts" / "lib", scripts / "lib")

    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}")
    fake_socket = tmp_path / "docker.sock"
    fake_socket.touch()
    (runtime_dns / "resolv.conf").write_text("nameserver 172.30.0.2\n")
    (project / ".env").write_text(
        "LITELLM_MASTER_KEY=gateway-test-key\n"
        "OPEN_WEBUI_SECRET_KEY=open-webui-test-key-at-least-32-bytes\n"
        "CODEX_ADAPTER_API_KEY=adapter-test-key\n"
        "SANDBOX_MANAGER_API_KEY=manager-test-key\n"
        "SANDBOX_MANAGER_WORKER_TOKEN_SECRET=worker-token-secret-at-least-32-bytes\n"
        f"SANDBOX_MANAGER_CODEX_AUTH_SOURCE_FILE={auth_file}\n"
        f"SANDBOX_MANAGER_DOCKER_SOCKET={fake_socket}\n"
        f"{extra_env}"
    )

    action_log = tmp_path / "actions.log"
    _write_executable(
        fake_bin / "bash",
        """#!/bin/sh
printf 'bash %s\\n' "$*" >>"${ACTION_LOG}"
if [ "$*" = "${FAIL_SCRIPT}" ]; then
  exit 1
fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "docker",
        """#!/bin/sh
printf 'docker %s\\n' "$*" >>"${ACTION_LOG}"
exit 0
""",
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["ACTION_LOG"] = str(action_log)
    env["FAIL_SCRIPT"] = fail_script
    result = subprocess.run(
        ["/bin/bash", "scripts/run-stack.sh"],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, action_log, project


def test_stack_exposes_gateway_only_after_network_policy_is_ready(tmp_path: Path) -> None:
    result, action_log, _ = _run_stack(tmp_path)

    assert result.returncode == 0, result.stderr
    actions = action_log.read_text().splitlines()
    stop_entry_workloads = next(
        index
        for index, action in enumerate(actions)
        if action == "docker compose stop open-webui gateway responses-adapter"
    )
    start_control = next(
        index
        for index, action in enumerate(actions)
        if action.startswith("docker compose up ")
        and "sandbox-manager" in action
        and "responses-adapter" in action
        and "gateway" not in action
    )
    apply_rpc = actions.index("bash scripts/apply-agent-rpc-policy.sh")
    apply_egress = actions.index("bash scripts/apply-agent-egress-policy.sh")
    start_gateway = next(
        index
        for index, action in enumerate(actions)
        if action.startswith("docker compose up ")
        and "gateway" in action
        and "sandbox-manager" not in action
        and "responses-adapter" not in action
    )

    assert stop_entry_workloads < start_control < apply_rpc < apply_egress < start_gateway


def test_stack_exposes_open_webui_only_after_gateway_is_ready(tmp_path: Path) -> None:
    result, action_log, _ = _run_stack(tmp_path)

    assert result.returncode == 0, result.stderr
    actions = action_log.read_text().splitlines()
    stop_entrypoints = actions.index(
        "docker compose stop open-webui gateway responses-adapter"
    )
    apply_egress = actions.index("bash scripts/apply-agent-egress-policy.sh")
    start_gateway = next(
        index
        for index, action in enumerate(actions)
        if action.startswith("docker compose up ")
        and "gateway" in action
        and "open-webui" not in action
        and "sandbox-manager" not in action
        and "responses-adapter" not in action
    )
    start_webui = next(
        index
        for index, action in enumerate(actions)
        if action.startswith("docker compose up ")
        and "open-webui" in action
        and "gateway" not in action
    )

    assert stop_entrypoints < apply_egress < start_gateway < start_webui


def test_stack_rejects_a_secret_root_visible_to_other_host_users(tmp_path: Path) -> None:
    secret_root = tmp_path / "shared-secret"
    secret_root.mkdir(mode=0o755)

    result, _, _ = _run_stack(
        tmp_path,
        extra_env=f"SANDBOX_MANAGER_SECRET_ROOT={secret_root}\n",
    )

    assert result.returncode != 0
    assert "must be owned by the current user with mode 0700" in result.stderr
    assert secret_root.stat().st_mode & 0o777 == 0o755


def test_stack_stops_entry_workloads_when_policy_application_fails(
    tmp_path: Path,
) -> None:
    result, action_log, _ = _run_stack(
        tmp_path,
        fail_script="scripts/apply-agent-egress-policy.sh",
    )

    assert result.returncode != 0
    actions = action_log.read_text().splitlines()
    failed_policy = actions.index("bash scripts/apply-agent-egress-policy.sh")
    assert any(
        index > failed_policy
        and action == "docker compose stop open-webui gateway responses-adapter"
        for index, action in enumerate(actions)
    )
