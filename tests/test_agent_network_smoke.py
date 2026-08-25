from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_negative_network_probe_fails_when_a_denied_target_is_reachable() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "agent_network_smoke.py"),
                "--denied-target",
                f"forbidden=127.0.0.1:{port}",
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    assert result.returncode == 1
    assert "unexpectedly reachable" in result.stderr


def test_negative_network_probe_passes_when_a_target_is_unreachable() -> None:
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "agent_network_smoke.py"),
            "--denied-target",
            f"blocked=127.0.0.1:{port}",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
