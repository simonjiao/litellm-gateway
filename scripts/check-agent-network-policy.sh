#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

control_network="${CONTROL_NETWORK:-responses-control}"
rpc_network="${SANDBOX_MANAGER_RPC_NETWORK:-agent-rpc}"
egress_network="${SANDBOX_MANAGER_EGRESS_NETWORK:-agent-egress}"
worker_image="${SANDBOX_IMAGE:-codex-sandbox-worker:0.3.0}"
sandbox_runtime="${SANDBOX_MANAGER_DOCKER_RUNTIME:-runsc}"
worker_port="${SANDBOX_MANAGER_WORKER_PORT:-8091}"
gateway_host_port="${LITELLM_PORT:-4000}"
probe_script="$(realpath scripts/agent_network_smoke.py)"
resolv_conf_file="$(pwd)/.runtime/agent-dns/resolv.conf"
probe_container="agent-network-smoke-$$"

if [[ ! "${gateway_host_port}" =~ ^[0-9]+$ ]] \
  || ((10#${gateway_host_port} < 1 || 10#${gateway_host_port} > 65535)); then
  echo "LITELLM_PORT must be a host port from 1 to 65535." >&2
  exit 1
fi
if [[ ! -r "${resolv_conf_file}" ]]; then
  echo "Agent resolver is missing; run run-stack.sh first." >&2
  exit 1
fi

compose_container() {
  local service="$1"
  local container_id
  container_id="$(docker compose ps -q "${service}")"
  if [[ -z "${container_id}" ]]; then
    echo "Required service '${service}' is not running." >&2
    return 1
  fi
  printf '%s\n' "${container_id}"
}

container_address() {
  local container_id="$1"
  local network_name="$2"
  local address
  address="$(
    docker container inspect \
      --format "{{with index .NetworkSettings.Networks \"${network_name}\"}}{{.IPAddress}}{{end}}" \
      "${container_id}"
  )"
  if [[ -z "${address}" ]]; then
    echo "Container '${container_id}' has no address on '${network_name}'." >&2
    return 1
  fi
  printf '%s\n' "${address}"
}

network_gateway() {
  local network_name="$1"
  local gateway
  gateway="$(
    docker network inspect \
      --format '{{range .IPAM.Config}}{{if .Gateway}}{{.Gateway}}{{"\n"}}{{end}}{{end}}' \
      "${network_name}" \
      | awk 'index($0, ":") == 0 && NF { print; exit }'
  )"
  if [[ -z "${gateway}" ]]; then
    echo "Network '${network_name}' has no IPv4 gateway." >&2
    return 1
  fi
  printf '%s\n' "${gateway}"
}

adapter_container="$(compose_container responses-adapter)"
manager_container="$(compose_container sandbox-manager)"
gateway_container="$(compose_container gateway)"
adapter_address="$(container_address "${adapter_container}" "${rpc_network}")"
manager_address="$(container_address "${manager_container}" "${control_network}")"
gateway_address="$(container_address "${gateway_container}" "${control_network}")"
rpc_gateway="$(network_gateway "${rpc_network}")"
egress_gateway="$(network_gateway "${egress_network}")"
public_address="$(
  python3 -c \
    "import socket; print(socket.getaddrinfo('chatgpt.com', 443, socket.AF_INET, socket.SOCK_STREAM)[0][4][0])"
)"

cleanup_probe() {
  docker container rm --force "${probe_container}" >/dev/null 2>&1 || true
}
trap cleanup_probe EXIT

docker container create \
  --name "${probe_container}" \
  --label io.litellm-codex-gateway.managed=true \
  --label io.litellm-codex-gateway.sandbox-id=sandbox_agent_network_smoke \
  --runtime "${sandbox_runtime}" \
  --network "${rpc_network}" \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --user 10001:10001 \
  --memory 256m \
  --cpus 0.50 \
  --pids-limit 64 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
  --tmpfs /run:rw,noexec,nosuid,nodev,size=8m \
  --volume "${resolv_conf_file}:/etc/resolv.conf:ro" \
  --volume "${probe_script}:/opt/agent-network-smoke.py:ro" \
  --entrypoint python3 \
  "${worker_image}" \
  -m http.server "${worker_port}" --bind 0.0.0.0 >/dev/null
docker network connect "${egress_network}" "${probe_container}"
docker container start "${probe_container}" >/dev/null

adapter_can_connect=false
for _ in $(seq 1 30); do
  if docker container exec "${adapter_container}" python -c \
    "import urllib.request; urllib.request.urlopen('http://${probe_container}:${worker_port}/', timeout=2).read(1)" \
    >/dev/null 2>&1; then
    adapter_can_connect=true
    break
  fi
  sleep 0.2
done
if [[ "${adapter_can_connect}" != "true" ]]; then
  docker container logs "${probe_container}" >&2
  echo "Adapter could not connect to the runsc probe by service name." >&2
  exit 1
fi

docker container exec "${probe_container}" python3 /opt/agent-network-smoke.py \
  --denied-target "adapter=${adapter_address}:8090" \
  --denied-target "manager=${manager_address}:8092" \
  --denied-target "gateway=${gateway_address}:4000" \
  --denied-target "rpc-host=${rpc_gateway}:${gateway_host_port}" \
  --denied-target "egress-host=${egress_gateway}:${gateway_host_port}" \
  --denied-target "internet=${public_address}:443"

echo "Agent network policy passed: Adapter can reach Worker; Worker cannot reach control, host, or direct Internet targets."
