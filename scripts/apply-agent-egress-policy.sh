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
# shellcheck source=scripts/lib/internal-services.sh
source scripts/lib/internal-services.sh

egress_network="${SANDBOX_MANAGER_EGRESS_NETWORK:-agent-egress}"
dns_container="${SANDBOX_AGENT_DNS_CONTAINER:-agent-dns}"
proxy_container="${SANDBOX_EGRESS_PROXY_CONTAINER:-egress-proxy}"
policy_image="${AGENT_NETWORK_POLICY_IMAGE:-agent-network-policy:0.1.0}"
forward_dispatcher="LITELLM_AE_FWD"
forward_chain_a="LITELLM_AE_F_A"
forward_chain_b="LITELLM_AE_F_B"
input_dispatcher="LITELLM_AE_INPUT"
input_chain_a="LITELLM_AE_I_A"
input_chain_b="LITELLM_AE_I_B"

sandbox_parse_internal_services "${SANDBOX_AGENT_INTERNAL_SERVICES:-}"

container_address() {
  local container_name="$1"
  local address
  address="$(
    docker container inspect \
      --format "{{with index .NetworkSettings.Networks \"${egress_network}\"}}{{.IPAddress}}{{end}}" \
      "${container_name}"
  )"
  if [[ -z "${address}" ]]; then
    echo "Container '${container_name}' is not attached to '${egress_network}'." >&2
    return 1
  fi
  printf '%s\n' "${address}"
}

dns_address="$(container_address "${dns_container}")"
proxy_address="$(container_address "${proxy_container}")"
bridge_name="$(network_policy_bridge_name "${egress_network}")"

internal_service_addresses=()
for container_name in "${SANDBOX_INTERNAL_SERVICE_CONTAINERS[@]}"; do
  internal_service_addresses+=("$(container_address "${container_name}")")
done

is_declared_egress_service() {
  local member="$1"
  local declared_container

  if [[ "${member}" == "${dns_container}" || "${member}" == "${proxy_container}" ]]; then
    return 0
  fi
  for declared_container in "${SANDBOX_INTERNAL_SERVICE_CONTAINERS[@]}"; do
    if [[ "${member}" == "${declared_container}" ]]; then
      return 0
    fi
  done
  return 1
}

network_members="$(
  docker network inspect \
    --format '{{range .Containers}}{{println .Name}}{{end}}' \
    "${egress_network}"
)"
while IFS= read -r member; do
  if [[ -z "${member}" ]] || is_declared_egress_service "${member}"; then
    continue
  fi
  managed="$(
    docker container inspect \
      --format '{{index .Config.Labels "io.litellm-codex-gateway.managed"}}' \
      "${member}"
  )"
  sandbox_id="$(
    docker container inspect \
      --format '{{index .Config.Labels "io.litellm-codex-gateway.sandbox-id"}}' \
      "${member}"
  )"
  if [[ "${managed}" == "true" && "${sandbox_id}" == sandbox_* ]]; then
    continue
  fi
  echo "Agent egress network '${egress_network}' has undeclared member '${member}'." >&2
  exit 1
done <<<"${network_members}"

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
for non_worker_address in \
  "${dns_address}" "${proxy_address}" "${internal_service_addresses[@]}"; do
  network_policy_iptables -A "${forward_policy_chain}" \
    -s "${non_worker_address}/32" -j DROP
done
network_policy_iptables -A "${forward_policy_chain}" \
  -o "${bridge_name}" -d "${dns_address}/32" -p udp --dport 53 \
  -m conntrack --ctstate NEW -j ACCEPT
network_policy_iptables -A "${forward_policy_chain}" \
  -o "${bridge_name}" -d "${dns_address}/32" -p tcp --dport 53 \
  -m conntrack --ctstate NEW -j ACCEPT
network_policy_iptables -A "${forward_policy_chain}" \
  -o "${bridge_name}" -d "${proxy_address}/32" -p tcp --dport 3128 \
  -m conntrack --ctstate NEW -j ACCEPT

if ((${#internal_service_addresses[@]} > 0)); then
  for index in "${!internal_service_addresses[@]}"; do
    service_address="${internal_service_addresses[${index}]}"
    service_port="${SANDBOX_INTERNAL_SERVICE_PORTS[${index}]}"
    network_policy_iptables -A "${forward_policy_chain}" \
      -o "${bridge_name}" -d "${service_address}/32" \
      -p tcp --dport "${service_port}" \
      -m conntrack --ctstate NEW -j ACCEPT
  done
fi

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

echo "agent-egress policy applied on ${bridge_name}: DNS, policy proxy, and declared internal service ports are allowed."
