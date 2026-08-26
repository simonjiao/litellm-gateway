from __future__ import annotations

import hashlib
import json
import os
import secrets

import httpx

EXPECTED_MODELS = ("codex-sol", "codex-terra", "codex-luna")


def main() -> None:
    base_url = os.getenv("LITELLM_BASE_URL", "http://127.0.0.1:4000/v1").rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ['LITELLM_MASTER_KEY']}"}

    catalog_response = httpx.get(f"{base_url}/models", headers=headers, timeout=30)
    catalog_response.raise_for_status()
    catalog = catalog_response.json()
    published = {
        item.get("id") for item in catalog.get("data", []) if isinstance(item, dict)
    }
    if published != set(EXPECTED_MODELS):
        raise RuntimeError(
            f"Gateway model catalog mismatch: expected {list(EXPECTED_MODELS)}, "
            f"received {sorted(str(item) for item in published)}"
        )

    results: list[dict[str, object]] = []
    for model in EXPECTED_MODELS:
        nonce = secrets.token_hex(16)
        expected_digest = hashlib.sha256(nonce.encode()).hexdigest()
        response = httpx.post(
            f"{base_url}/responses",
            headers=headers,
            json={
                "model": model,
                "input": (
                    "Run this exact workspace shell command and return its output: "
                    f"printf '%s' '{nonce}' | sha256sum"
                ),
            },
            timeout=3600,
        )
        response.raise_for_status()
        payload = response.json()
        rendered = json.dumps(payload, ensure_ascii=False)
        if payload.get("model") != model:
            raise RuntimeError(
                f"Gateway returned model {payload.get('model')!r} for selected model {model!r}"
            )
        if expected_digest not in rendered:
            raise RuntimeError(
                f"Basic smoke did not return verified shell command output for {model}"
            )
        results.append(payload)

    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
