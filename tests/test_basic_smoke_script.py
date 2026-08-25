from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class _UnverifiedResponseHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers["Content-Length"])
        self.rfile.read(content_length)
        payload = json.dumps({"output_text": "request completed"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


class _VerifiedResponseHandler(_UnverifiedResponseHandler):
    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(content_length))
        match = re.search(r"'([0-9a-f]{32})' \| sha256sum", request["input"])
        assert match is not None
        digest = hashlib.sha256(match.group(1).encode()).hexdigest()
        payload = json.dumps({"output_text": digest}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _run_smoke(
    handler: type[BaseHTTPRequestHandler],
) -> subprocess.CompletedProcess[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        env = os.environ.copy()
        env["LITELLM_BASE_URL"] = f"http://127.0.0.1:{server.server_port}/v1"
        env["LITELLM_MASTER_KEY"] = "test-key"
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "smoke.py")],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    return result


def test_basic_smoke_rejects_a_response_without_verified_command_output() -> None:
    result = _run_smoke(_UnverifiedResponseHandler)

    assert result.returncode != 0
    assert "verified shell command output" in result.stderr


def test_basic_smoke_accepts_verified_command_output() -> None:
    result = _run_smoke(_VerifiedResponseHandler)

    assert result.returncode == 0, result.stderr
