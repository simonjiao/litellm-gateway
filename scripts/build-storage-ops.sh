#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

image="${AGENT_STORAGE_OPS_IMAGE:-agent-storage-ops:0.3.0}"

docker build \
  --build-arg "PYPI_INDEX_URL=${PYPI_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}" \
  --file deploy/storage-ops/Dockerfile \
  --tag "${image}" \
  .
