from __future__ import annotations

import hashlib
import json
import os
import secrets

import httpx


def main() -> None:
    base_url = os.getenv("LITELLM_BASE_URL", "http://127.0.0.1:4000/v1")
    nonce = secrets.token_hex(16)
    expected_digest = hashlib.sha256(nonce.encode()).hexdigest()
    response = httpx.post(
        f"{base_url.rstrip('/')}/responses",
        headers={"Authorization": f"Bearer {os.environ['LITELLM_MASTER_KEY']}"},
        json={
            "model": "codex-app-server",
            "input": (
                "Run this exact workspace shell command and return its output: "
                f"printf '%s' '{nonce}' | sha256sum"
            ),
        },
        timeout=3600,
    )
    response.raise_for_status()
    payload = response.json()
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    if expected_digest not in rendered:
        raise RuntimeError("Basic smoke did not return verified shell command output")
    print(rendered)


if __name__ == "__main__":
    main()
