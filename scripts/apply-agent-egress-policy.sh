#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

egress_network="${SANDBOX_MANAGER_EGRESS_NETWORK:-agent-egress}"
dns_container="${SANDBOX_AGENT_DNS_CONTAINER:-agent-dns}"
proxy_container="${SANDBOX_EGRESS_PROXY_CONTAINER:-egress-proxy}"
internal_services="${SANDBOX_AGENT_INTERNAL_SERVICES:-}"
policy_chain="LITELLM_AGENT_EGRESS"

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

iptables_command=(iptables)
if ((EUID != 0)); then
  if ! sudo -n true >/dev/null 2>&1; then
    echo "Applying the agent-egress policy requires root or passwordless sudo." >&2
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
  -d "${dns_address}/32" -p udp --dport 53 \
  -m conntrack --ctstate NEW -j ACCEPT
"${iptables_command[@]}" -A "${policy_chain}" \
  -d "${dns_address}/32" -p tcp --dport 53 \
  -m conntrack --ctstate NEW -j ACCEPT
"${iptables_command[@]}" -A "${policy_chain}" \
  -d "${proxy_address}/32" -p tcp --dport 3128 \
  -m conntrack --ctstate NEW -j ACCEPT

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
    service_address="$(container_address "${container_name}")"
    "${iptables_command[@]}" -A "${policy_chain}" \
      -d "${service_address}/32" -p tcp --dport "${service_port}" \
      -m conntrack --ctstate NEW -j ACCEPT
  done
fi

"${iptables_command[@]}" -A "${policy_chain}" \
  -m conntrack --ctstate NEW -j DROP
"${iptables_command[@]}" -A "${policy_chain}" -j RETURN

if ! "${iptables_command[@]}" -C DOCKER-USER \
  -i "${bridge_name}" -o "${bridge_name}" -j "${policy_chain}" 2>/dev/null; then
  "${iptables_command[@]}" -I DOCKER-USER 1 \
    -i "${bridge_name}" -o "${bridge_name}" -j "${policy_chain}"
fi

echo "agent-egress policy applied on ${bridge_name}: DNS, policy proxy, and declared internal service ports are allowed."
