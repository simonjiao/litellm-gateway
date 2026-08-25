#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# shellcheck source=scripts/lib/network-policy.sh
source scripts/lib/network-policy.sh

rpc_network="${SANDBOX_MANAGER_RPC_NETWORK:-agent-rpc}"
worker_port="${SANDBOX_MANAGER_WORKER_PORT:-8091}"
policy_image="${AGENT_NETWORK_POLICY_IMAGE:-agent-network-policy:0.1.0}"
forward_dispatcher="LITELLM_AR_FWD"
forward_chain_a="LITELLM_AR_F_A"
forward_chain_b="LITELLM_AR_F_B"
input_dispatcher="LITELLM_AR_INPUT"
input_chain_a="LITELLM_AR_I_A"
input_chain_b="LITELLM_AR_I_B"

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

bridge_name="$(network_policy_bridge_name "${rpc_network}")"

network_policy_select_iptables "${policy_image}"

network_policy_prepare_dispatcher \
  "${forward_dispatcher}" "${forward_chain_a}" "${forward_chain_b}"
forward_policy_chain="${NETWORK_POLICY_INACTIVE_CHAIN}"
forward_has_active="${NETWORK_POLICY_DISPATCH_HAS_ACTIVE}"
network_policy_prepare_dispatcher \
  "${input_dispatcher}" "${input_chain_a}" "${input_chain_b}"
input_policy_chain="${NETWORK_POLICY_INACTIVE_CHAIN}"
input_has_active="${NETWORK_POLICY_DISPATCH_HAS_ACTIVE}"

network_policy_iptables -A "${forward_policy_chain}" \
  -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
network_policy_iptables -A "${forward_policy_chain}" \
  -s "${adapter_address}/32" -o "${bridge_name}" \
  -p tcp --dport "${worker_port}" \
  -m conntrack --ctstate NEW -j ACCEPT
network_policy_iptables -A "${forward_policy_chain}" -j DROP

network_policy_iptables -A "${input_policy_chain}" \
  -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
network_policy_iptables -A "${input_policy_chain}" -j DROP

network_policy_switch_dispatcher \
  "${forward_dispatcher}" "${forward_policy_chain}" "${forward_has_active}"
network_policy_switch_dispatcher \
  "${input_dispatcher}" "${input_policy_chain}" "${input_has_active}"
network_policy_ensure_hook DOCKER-USER "${bridge_name}" "${forward_dispatcher}"
network_policy_ensure_hook INPUT "${bridge_name}" "${input_dispatcher}"

echo "agent-rpc policy applied on ${bridge_name}: ${adapter_address} may initiate Worker TCP/${worker_port}; other new connections are denied."
