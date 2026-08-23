from __future__ import annotations

import json
import os

import httpx


def main() -> None:
    base_url = os.getenv("LITELLM_BASE_URL", "http://127.0.0.1:4000/v1")
    response = httpx.post(
        f"{base_url.rstrip('/')}/responses",
        headers={"Authorization": f"Bearer {os.environ['LITELLM_MASTER_KEY']}"},
        json={
            "model": "codex-app-server",
            "input": "Summarize the current workspace in three bullets.",
        },
        timeout=3600,
    )
    response.raise_for_status()
    print(json.dumps(response.json(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
