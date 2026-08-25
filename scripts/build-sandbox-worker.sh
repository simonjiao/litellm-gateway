#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

sandbox_image="${SANDBOX_IMAGE:-codex-sandbox-worker:0.3.0}"
codex_version="${CODEX_VERSION:-0.149.0}"
debian_mirror="${DEBIAN_MIRROR:-http://mirrors.tuna.tsinghua.edu.cn/debian}"
debian_security_mirror="${DEBIAN_SECURITY_MIRROR:-http://mirrors.tuna.tsinghua.edu.cn/debian-security}"
pypi_index_url="${PYPI_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"

docker build \
  --file deploy/sandbox-worker/Dockerfile \
  --build-arg "CODEX_VERSION=${codex_version}" \
  --build-arg "DEBIAN_MIRROR=${debian_mirror}" \
  --build-arg "DEBIAN_SECURITY_MIRROR=${debian_security_mirror}" \
  --build-arg "PYPI_INDEX_URL=${pypi_index_url}" \
  --tag "${sandbox_image}" \
  .
