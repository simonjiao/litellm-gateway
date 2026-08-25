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

egress_network="${SANDBOX_MANAGER_EGRESS_NETWORK:-agent-egress}"
dns_container="${SANDBOX_AGENT_DNS_CONTAINER:-agent-dns}"
proxy_container="${SANDBOX_EGRESS_PROXY_CONTAINER:-egress-proxy}"
internal_services="${SANDBOX_AGENT_INTERNAL_SERVICES:-}"
policy_image="${SANDBOX_NETWORK_POLICY_IMAGE:-litellm-network-policy:0.1.0}"
forward_dispatcher="LITELLM_AE_FWD"
forward_chain_a="LITELLM_AE_F_A"
forward_chain_b="LITELLM_AE_F_B"
input_dispatcher="LITELLM_AE_INPUT"
input_chain_a="LITELLM_AE_I_A"
input_chain_b="LITELLM_AE_I_B"

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
network_id="$(docker network inspect --format '{{.Id}}' "${egress_network}")"
bridge_name="$(
  docker network inspect \
    --format '{{index .Options "com.docker.network.bridge.name"}}' \
    "${egress_network}"
)"
if [[ -z "${bridge_name}" || "${bridge_name}" == "<no value>" ]]; then
  bridge_name="br-${network_id:0:12}"
fi

internal_service_addresses=()
internal_service_ports=()
if [[ -n "${internal_services}" ]]; then
  IFS=',' read -r -a service_records <<<"${internal_services}"
  for record in "${service_records[@]}"; do
    if [[ ! "${record}" =~ ^[^=]+=([A-Za-z0-9][A-Za-z0-9_.-]*):([0-9]+)$ ]]; then
      echo "Internal service '${record}' must use dns-name=container-name:port." >&2
      exit 1
    fi
    container_name="${BASH_REMATCH[1]}"
    service_port="${BASH_REMATCH[2]}"
    if ((10#${service_port} < 1 || 10#${service_port} > 65535)); then
      echo "Internal service port must be from 1 to 65535: ${record}" >&2
      exit 1
    fi
    internal_service_addresses+=("$(container_address "${container_name}")")
    internal_service_ports+=("${service_port}")
  done
fi

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
    service_port="${internal_service_ports[${index}]}"
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
