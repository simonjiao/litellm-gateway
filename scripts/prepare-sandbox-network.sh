#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

control_network="${CONTROL_NETWORK:-agent-control}"
rpc_network="${SANDBOX_MANAGER_RPC_NETWORK:-agent-rpc}"
egress_network="${SANDBOX_MANAGER_EGRESS_NETWORK:-agent-egress}"
storage_network="${SANDBOX_MANAGER_STORAGE_NETWORK:-agent-storage}"
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
  local actual_internal actual_driver actual_scope actual_ipv6

  if docker network inspect "${network_name}" >/dev/null 2>&1; then
    actual_internal="$(docker network inspect --format '{{.Internal}}' "${network_name}")"
    actual_driver="$(docker network inspect --format '{{.Driver}}' "${network_name}")"
    actual_scope="$(docker network inspect --format '{{.Scope}}' "${network_name}")"
    actual_ipv6="$(docker network inspect --format '{{.EnableIPv6}}' "${network_name}")"
    if [[ "${actual_internal}" != "${expected_internal}" ]]; then
      echo "Existing network '${network_name}' has internal=${actual_internal}; expected ${expected_internal}." >&2
      exit 1
    fi
    if [[ "${actual_driver}" != "bridge" || "${actual_scope}" != "local" \
      || "${actual_ipv6}" != "false" ]]; then
      echo "Existing network '${network_name}' must be a local IPv4 bridge." >&2
      exit 1
    fi
    return
  fi

  if [[ "${expected_internal}" == "true" ]]; then
    docker network create --driver bridge --scope local --ipv6=false \
      --internal "${network_name}" >/dev/null
  else
    docker network create --driver bridge --scope local --ipv6=false \
      "${network_name}" >/dev/null
  fi
}

for network_name in "${control_network}" "${rpc_network}" "${egress_network}" "${storage_network}"; do
  if [[ "${network_name}" == "bridge" || "${network_name}" == "host" || "${network_name}" == "none" ]]; then
    echo "Built-in Docker network '${network_name}' cannot be used by this deployment." >&2
    exit 1
  fi
done

ensure_network "${control_network}" false
ensure_network "${rpc_network}" true
ensure_network "${egress_network}" true
ensure_network "${storage_network}" false

echo "Networks are ready: control=${control_network}, agent-rpc=${rpc_network}, agent-egress=${egress_network}, storage=${storage_network}."
