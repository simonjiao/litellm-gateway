#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

proxy_image="${AGENT_EGRESS_PROXY_IMAGE:-agent-egress-proxy:0.1.0}"
debian_mirror="${DEBIAN_MIRROR:-http://mirrors.tuna.tsinghua.edu.cn/debian}"
debian_security_mirror="${DEBIAN_SECURITY_MIRROR:-http://mirrors.tuna.tsinghua.edu.cn/debian-security}"

docker build \
  --file deploy/egress-proxy/Dockerfile \
  --build-arg "DEBIAN_MIRROR=${debian_mirror}" \
  --build-arg "DEBIAN_SECURITY_MIRROR=${debian_security_mirror}" \
  --tag "${proxy_image}" \
  deploy/egress-proxy
