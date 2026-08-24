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
sandbox_runtime="${SANDBOX_MANAGER_DOCKER_RUNTIME:-runsc}"
runtimes="$(docker info --format '{{json .Runtimes}}')"

case "${runtimes}" in
  *\"${sandbox_runtime}\"*) ;;
  *)
    echo "Required Docker runtime '${sandbox_runtime}' is unavailable." >&2
    exit 1
    ;;
esac

ensure_network() {
  local network_name="$1"
  local expected_internal="$2"
  local actual_internal

  if docker network inspect "${network_name}" >/dev/null 2>&1; then
    actual_internal="$(docker network inspect --format '{{.Internal}}' "${network_name}")"
    if [[ "${actual_internal}" != "${expected_internal}" ]]; then
      echo "Existing network '${network_name}' has internal=${actual_internal}; expected ${expected_internal}." >&2
      exit 1
    fi
    return
  fi

  if [[ "${expected_internal}" == "true" ]]; then
    docker network create --internal "${network_name}" >/dev/null
  else
    docker network create "${network_name}" >/dev/null
  fi
}

for network_name in "${control_network}" "${rpc_network}" "${egress_network}"; do
  if [[ "${network_name}" == "bridge" || "${network_name}" == "host" || "${network_name}" == "none" ]]; then
    echo "Built-in Docker network '${network_name}' cannot be used by this deployment." >&2
    exit 1
  fi
done

ensure_network "${control_network}" false
ensure_network "${rpc_network}" true
ensure_network "${egress_network}" true

echo "Networks are ready: control=${control_network}, agent-rpc=${rpc_network}, agent-egress=${egress_network}."
