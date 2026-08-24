#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

sandbox_network="${SANDBOX_MANAGER_EGRESS_NETWORK:-agent-egress}"
proxy_url="${SANDBOX_MANAGER_EGRESS_PROXY_URL:-http://egress-proxy:3128}"
worker_image="${SANDBOX_MANAGER_IMAGE:-litellm-codex-sandbox-worker:0.3.0}"
sandbox_runtime="${SANDBOX_MANAGER_DOCKER_RUNTIME:-runsc}"
dns_container="${SANDBOX_AGENT_DNS_CONTAINER:-agent-dns}"
check_script="$(realpath scripts/egress_policy_smoke.py)"
resolv_conf_file="$(pwd)/.runtime/agent-dns/resolv.conf"

if ! docker container inspect "${dns_container}" >/dev/null 2>&1; then
  echo "Agent DNS '${dns_container}' is missing; run run-agent-dns.sh first." >&2
  exit 1
fi
if [[ "$(docker container inspect --format '{{.State.Health.Status}}' "${dns_container}")" != "healthy" ]]; then
  echo "Agent DNS '${dns_container}' is not healthy." >&2
  exit 1
fi

dns_server="$(
  docker container inspect \
    --format "{{with index .NetworkSettings.Networks \"${sandbox_network}\"}}{{.IPAddress}}{{end}}" \
    "${dns_container}"
)"
if [[ -z "${dns_server}" ]]; then
  echo "Agent DNS '${dns_container}' is not attached to '${sandbox_network}'." >&2
  exit 1
fi
if [[ ! -r "${resolv_conf_file}" ]] \
  || ! grep -Fxq "nameserver ${dns_server}" "${resolv_conf_file}"; then
  echo "Agent DNS resolver file is missing or stale; run run-agent-dns.sh again." >&2
  exit 1
fi

docker run --rm \
  --runtime "${sandbox_runtime}" \
  --network "${sandbox_network}" \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --user 10001:10001 \
  --env PYTHONDONTWRITEBYTECODE=1 \
  --volume "${resolv_conf_file}:/etc/resolv.conf:ro" \
  --volume "${check_script}:/opt/egress-policy-smoke.py:ro" \
  --entrypoint python3 \
  "${worker_image}" \
  /opt/egress-policy-smoke.py \
  --proxy "${proxy_url}" \
  --allowed-url https://chatgpt.com/backend-api/codex \
  --allowed-url https://auth.openai.com/oauth/token \
  --denied-url https://example.com/ \
  --direct-url https://example.com/
