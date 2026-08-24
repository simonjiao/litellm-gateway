#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

rpc_network="${SANDBOX_MANAGER_RPC_NETWORK:-agent-rpc}"
worker_port="${SANDBOX_MANAGER_WORKER_PORT:-8091}"
policy_chain="LITELLM_AGENT_RPC"

adapter_container="$(docker compose ps -q responses-adapter)"
if [[ -z "${adapter_container}" ]]; then
  echo "Responses Adapter is not running." >&2
  exit 1
fi

adapter_address="$(
  docker container inspect \
    --format "{{with index .NetworkSettings.Networks \"${rpc_network}\"}}{{.IPAddress}}{{end}}" \
    "${adapter_container}"
)"
if [[ -z "${adapter_address}" ]]; then
  echo "Responses Adapter is not attached to '${rpc_network}'." >&2
  exit 1
fi

network_id="$(docker network inspect --format '{{.Id}}' "${rpc_network}")"
bridge_name="$(
  docker network inspect \
    --format '{{index .Options "com.docker.network.bridge.name"}}' \
    "${rpc_network}"
)"
if [[ -z "${bridge_name}" || "${bridge_name}" == "<no value>" ]]; then
  bridge_name="br-${network_id:0:12}"
fi

iptables_command=(iptables)
if ((EUID != 0)); then
  if ! sudo -n true >/dev/null 2>&1; then
    echo "Applying the agent-rpc policy requires root or passwordless sudo." >&2
    exit 1
  fi
  iptables_command=(sudo -n iptables)
fi

if ! "${iptables_command[@]}" -nL DOCKER-USER >/dev/null 2>&1; then
  echo "Docker's DOCKER-USER firewall chain is unavailable." >&2
  exit 1
fi

"${iptables_command[@]}" -N "${policy_chain}" 2>/dev/null || true
"${iptables_command[@]}" -F "${policy_chain}"
"${iptables_command[@]}" -A "${policy_chain}" \
  -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
"${iptables_command[@]}" -A "${policy_chain}" \
  -s "${adapter_address}/32" -p tcp --dport "${worker_port}" \
  -m conntrack --ctstate NEW -j ACCEPT
"${iptables_command[@]}" -A "${policy_chain}" \
  -m conntrack --ctstate NEW -j DROP
"${iptables_command[@]}" -A "${policy_chain}" -j RETURN

if ! "${iptables_command[@]}" -C DOCKER-USER \
  -i "${bridge_name}" -o "${bridge_name}" -j "${policy_chain}" 2>/dev/null; then
  "${iptables_command[@]}" -I DOCKER-USER 1 \
    -i "${bridge_name}" -o "${bridge_name}" -j "${policy_chain}"
fi

echo "agent-rpc policy applied on ${bridge_name}: ${adapter_address} may initiate Worker TCP/${worker_port}; other new connections are denied."
