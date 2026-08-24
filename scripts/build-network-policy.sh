#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

policy_image="${SANDBOX_NETWORK_POLICY_IMAGE:-litellm-network-policy:0.1.0}"
debian_mirror="${DEBIAN_MIRROR:-http://mirrors.tuna.tsinghua.edu.cn/debian}"
debian_security_mirror="${DEBIAN_SECURITY_MIRROR:-http://mirrors.tuna.tsinghua.edu.cn/debian-security}"

docker build \
  --file deploy/network-policy/Dockerfile \
  --build-arg "DEBIAN_MIRROR=${debian_mirror}" \
  --build-arg "DEBIAN_SECURITY_MIRROR=${debian_security_mirror}" \
  --tag "${policy_image}" \
  deploy/network-policy
