#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

managed_label="io.litellm-codex-gateway.component"
managed_value="agent-dns"
sandbox_network="${SANDBOX_MANAGER_EGRESS_NETWORK:-agent-egress}"
proxy_container="${SANDBOX_EGRESS_PROXY_CONTAINER:-egress-proxy}"
proxy_alias="${SANDBOX_EGRESS_PROXY_ALIAS:-egress-proxy}"
internal_services="${SANDBOX_AGENT_INTERNAL_SERVICES:-}"
dns_image="${SANDBOX_AGENT_DNS_IMAGE:-litellm-agent-dns:0.1.0}"
dns_container="${SANDBOX_AGENT_DNS_CONTAINER:-agent-dns}"
memory_limit="${SANDBOX_AGENT_DNS_MEMORY_LIMIT:-64m}"
cpu_limit="${SANDBOX_AGENT_DNS_CPUS:-0.25}"
pids_limit="${SANDBOX_AGENT_DNS_PIDS_LIMIT:-32}"
runtime_dir="$(pwd)/.runtime/agent-dns"
hosts_file="${runtime_dir}/hosts"
resolv_conf_file="${runtime_dir}/resolv.conf"

if ! docker network inspect "${sandbox_network}" >/dev/null 2>&1; then
  echo "Agent egress network '${sandbox_network}' is missing; run prepare-sandbox-network.sh first." >&2
  exit 1
fi
if [[ "$(docker network inspect --format '{{.Internal}}' "${sandbox_network}")" != "true" ]]; then
  echo "Agent egress network '${sandbox_network}' must be internal." >&2
  exit 1
fi
if ! docker image inspect "${dns_image}" >/dev/null 2>&1; then
  echo "DNS image '${dns_image}' is missing; run build-agent-dns.sh first." >&2
  exit 1
fi

records=("${proxy_alias}=${proxy_container}")
if [[ -n "${internal_services}" ]]; then
  IFS=',' read -r -a service_records <<<"${internal_services}"
  for record in "${service_records[@]}"; do
    if [[ ! "${record}" =~ ^[^=]+=[A-Za-z0-9][A-Za-z0-9_.-]*:([0-9]+)$ ]]; then
      echo "Internal service '${record}' must use dns-name=container-name:port." >&2
      exit 1
    fi
    service_port="${BASH_REMATCH[1]}"
    if ((10#${service_port} < 1 || 10#${service_port} > 65535)); then
      echo "Internal service port must be from 1 to 65535: ${record}" >&2
      exit 1
    fi
    records+=("${record}")
  done
fi

install -d -m 0700 "${runtime_dir}"
temporary_hosts="$(mktemp "${runtime_dir}/hosts.XXXXXX")"
cleanup_temporary_hosts() {
  rm -f -- "${temporary_hosts}"
}
trap cleanup_temporary_hosts EXIT

for record in "${records[@]}"; do
  if [[ "${record}" != *=* ]]; then
    echo "Internal service '${record}' must use dns-name=container-name:port." >&2
    exit 1
  fi
  dns_name="${record%%=*}"
  target="${record#*=}"
  container_name="${target%%:*}"
  if [[ ! "${dns_name}" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$ ]]; then
    echo "Invalid Agent DNS name: ${dns_name}" >&2
    exit 1
  fi
  if [[ ! "${container_name}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
    echo "Invalid internal service container name: ${container_name}" >&2
    exit 1
  fi
  if ! docker container inspect "${container_name}" >/dev/null 2>&1; then
    echo "Internal service container '${container_name}' is missing." >&2
    exit 1
  fi
  service_address="$(
    docker container inspect \
      --format "{{with index .NetworkSettings.Networks \"${sandbox_network}\"}}{{.IPAddress}}{{end}}" \
      "${container_name}"
  )"
  if [[ -z "${service_address}" ]]; then
    echo "Internal service '${container_name}' is not attached to '${sandbox_network}'." >&2
    exit 1
  fi
  printf '%s %s\n' "${service_address}" "${dns_name}" >>"${temporary_hosts}"
done

sort -u -o "${temporary_hosts}" "${temporary_hosts}"
chmod 0444 "${temporary_hosts}"
mv --force "${temporary_hosts}" "${hosts_file}"
trap - EXIT

if docker container inspect "${dns_container}" >/dev/null 2>&1; then
  existing_label="$(
    docker container inspect \
      --format "{{index .Config.Labels \"${managed_label}\"}}" \
      "${dns_container}"
  )"
  if [[ "${existing_label}" != "${managed_value}" ]]; then
    echo "Container '${dns_container}' exists but is not managed by this project." >&2
    exit 1
  fi
  docker container rm --force "${dns_container}" >/dev/null
fi

docker run --detach \
  --name "${dns_container}" \
  --label "${managed_label}=${managed_value}" \
  --runtime runc \
  --network "${sandbox_network}" \
  --network-alias "${dns_container}" \
  --restart unless-stopped \
  --read-only \
  --cap-drop ALL \
  --cap-add NET_BIND_SERVICE \
  --security-opt no-new-privileges:true \
  --memory "${memory_limit}" \
  --cpus "${cpu_limit}" \
  --pids-limit "${pids_limit}" \
  --tmpfs /run/agent-dns:rw,noexec,nosuid,nodev,size=1m,mode=0700,uid=65534,gid=65534 \
  --volume "${hosts_file}:/etc/agent-dns/hosts:ro" \
  "${dns_image}" >/dev/null

cleanup_on_error() {
  docker container rm --force "${dns_container}" >/dev/null 2>&1 || true
}
trap cleanup_on_error ERR

write_resolv_conf() {
  local dns_server temporary_file
  dns_server="$(
    docker container inspect \
      --format "{{with index .NetworkSettings.Networks \"${sandbox_network}\"}}{{.IPAddress}}{{end}}" \
      "${dns_container}"
  )"
  if [[ -z "${dns_server}" ]]; then
    echo "Agent DNS '${dns_container}' has no address on '${sandbox_network}'." >&2
    return 1
  fi

  temporary_file="$(mktemp "${runtime_dir}/resolv.conf.XXXXXX")"
  printf 'nameserver %s\noptions ndots:0 timeout:1 attempts:2\n' \
    "${dns_server}" >"${temporary_file}"
  chmod 0444 "${temporary_file}"
  mv --force "${temporary_file}" "${resolv_conf_file}"
}

for _ in $(seq 1 30); do
  health="$(docker container inspect --format '{{.State.Health.Status}}' "${dns_container}")"
  case "${health}" in
    healthy)
      write_resolv_conf
      trap - ERR
      echo "Agent DNS '${dns_container}' is healthy; runtime records and resolver are current."
      exit 0
      ;;
    unhealthy)
      docker container logs "${dns_container}" >&2
      echo "Agent DNS '${dns_container}' is unhealthy." >&2
      exit 1
      ;;
  esac
  sleep 1
done

docker container logs "${dns_container}" >&2
echo "Timed out waiting for Agent DNS '${dns_container}' to become healthy." >&2
exit 1
