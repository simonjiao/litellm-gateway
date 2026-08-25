#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

dns_image="${AGENT_DNS_IMAGE:-agent-dns:0.1.0}"
debian_mirror="${DEBIAN_MIRROR:-http://mirrors.tuna.tsinghua.edu.cn/debian}"
debian_security_mirror="${DEBIAN_SECURITY_MIRROR:-http://mirrors.tuna.tsinghua.edu.cn/debian-security}"

docker build \
  --file deploy/agent-dns/Dockerfile \
  --build-arg "DEBIAN_MIRROR=${debian_mirror}" \
  --build-arg "DEBIAN_SECURITY_MIRROR=${debian_security_mirror}" \
  --tag "${dns_image}" \
  deploy/agent-dns
