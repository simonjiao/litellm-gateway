from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
    (project / ".env").write_text("")

    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
case "$*" in
  "info --format {{json .Runtimes}}") printf '%s\\n' '{"runc":{},"runsc":{}}' ;;
  *"--format {{.Internal}} responses-control") printf '%s\\n' false ;;
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
