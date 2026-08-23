#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

sandbox_image="${SANDBOX_HOST_IMAGE:-litellm-codex-sandbox-worker:0.3.0}"
codex_version="${CODEX_VERSION:-0.149.0}"

docker build \
  --file deploy/sandbox-worker/Dockerfile \
  --build-arg "CODEX_VERSION=${codex_version}" \
  --tag "${sandbox_image}" \
  .
