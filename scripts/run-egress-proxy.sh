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
managed_value="egress-proxy"
sandbox_network="${SANDBOX_MANAGER_EGRESS_NETWORK:-agent-egress}"
uplink_network="${SANDBOX_EGRESS_UPLINK_NETWORK:-codex-egress-uplink}"
proxy_image="${SANDBOX_EGRESS_PROXY_IMAGE:-litellm-codex-egress-proxy:0.1.0}"
proxy_container="${SANDBOX_EGRESS_PROXY_CONTAINER:-egress-proxy}"
proxy_alias="${SANDBOX_EGRESS_PROXY_ALIAS:-egress-proxy}"
proxy_url="${SANDBOX_MANAGER_EGRESS_PROXY_URL:-http://egress-proxy:3128}"
allowed_domains_file="${SANDBOX_EGRESS_ALLOWED_DOMAINS_FILE:-deploy/egress-proxy/allowed-domains.txt}"
upstream_proxy_url="${SANDBOX_EGRESS_UPSTREAM_PROXY_URL:-}"
upstream_relay_container="${SANDBOX_EGRESS_UPSTREAM_RELAY_CONTAINER:-egress-upstream-relay}"
upstream_relay_port="${SANDBOX_EGRESS_UPSTREAM_RELAY_PORT:-17890}"
memory_limit="${SANDBOX_EGRESS_PROXY_MEMORY_LIMIT:-256m}"
cpu_limit="${SANDBOX_EGRESS_PROXY_CPUS:-0.50}"
pids_limit="${SANDBOX_EGRESS_PROXY_PIDS_LIMIT:-128}"

upstream_host=""
upstream_port=""
if [[ -n "${upstream_proxy_url}" ]]; then
  if [[ "${upstream_proxy_url}" =~ ^http://([A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?):([0-9]+)$ ]]; then
    upstream_host="${BASH_REMATCH[1]}"
    upstream_port="${BASH_REMATCH[3]}"
  else
    echo "SANDBOX_EGRESS_UPSTREAM_PROXY_URL must use http://host:port with no path or credentials." >&2
    exit 1
  fi
  if ((10#${upstream_port} < 1 || 10#${upstream_port} > 65535)); then
    echo "Upstream proxy port must be from 1 to 65535." >&2
    exit 1
  fi
fi

if [[ "${proxy_url}" != "http://${proxy_alias}:3128" ]]; then
  echo "SANDBOX_MANAGER_EGRESS_PROXY_URL must match http://${proxy_alias}:3128" >&2
  exit 1
fi

if ! docker network inspect "${sandbox_network}" >/dev/null 2>&1; then
  echo "Sandbox network '${sandbox_network}' is missing; run prepare-sandbox-network.sh first." >&2
  exit 1
fi
if [[ "$(docker network inspect --format '{{.Internal}}' "${sandbox_network}")" != "true" ]]; then
  echo "Sandbox network '${sandbox_network}' must be internal." >&2
  exit 1
fi

sandbox_cidr="$(
  docker network inspect \
    --format '{{range .IPAM.Config}}{{if .Subnet}}{{.Subnet}}{{"\n"}}{{end}}{{end}}' \
    "${sandbox_network}" \
    | awk 'index($0, ":") == 0 && NF { print; exit }'
)"
if [[ -z "${sandbox_cidr}" ]]; then
  echo "Sandbox network '${sandbox_network}' has no IPv4 subnet." >&2
  exit 1
fi

if docker network inspect "${uplink_network}" >/dev/null 2>&1; then
  uplink_internal="$(docker network inspect --format '{{.Internal}}' "${uplink_network}")"
  uplink_driver="$(docker network inspect --format '{{.Driver}}' "${uplink_network}")"
  uplink_scope="$(docker network inspect --format '{{.Scope}}' "${uplink_network}")"
  uplink_ipv6="$(docker network inspect --format '{{.EnableIPv6}}' "${uplink_network}")"
  if [[ "${uplink_internal}" != "false" || "${uplink_driver}" != "bridge" \
    || "${uplink_scope}" != "local" || "${uplink_ipv6}" != "false" ]]; then
    echo "Uplink network '${uplink_network}' must be a non-internal local IPv4 bridge." >&2
    exit 1
  fi
else
  docker network create --driver bridge --scope local --ipv6=false \
    "${uplink_network}" >/dev/null
fi

uplink_gateway="$(
  docker network inspect \
    --format '{{range .IPAM.Config}}{{if .Gateway}}{{.Gateway}}{{"\n"}}{{end}}{{end}}' \
    "${uplink_network}" \
    | awk 'index($0, ":") == 0 && NF { print; exit }'
)"
if [[ -z "${uplink_gateway}" ]]; then
  echo "Uplink network '${uplink_network}' has no IPv4 gateway." >&2
  exit 1
fi

if ! docker image inspect "${proxy_image}" >/dev/null 2>&1; then
  echo "Proxy image '${proxy_image}' is missing; run build-egress-proxy.sh first." >&2
  exit 1
fi
if [[ ! -r "${allowed_domains_file}" ]]; then
  echo "Allowed-domain file is not readable: ${allowed_domains_file}" >&2
  exit 1
fi
allowed_domains_file="$(realpath "${allowed_domains_file}")"

assert_managed_or_missing() {
  local container_name="$1"
  local expected_value="$2"
  if ! docker container inspect "${container_name}" >/dev/null 2>&1; then
    return
  fi
  existing_label="$(
    docker container inspect \
      --format "{{index .Config.Labels \"${managed_label}\"}}" \
      "${container_name}"
  )"
  if [[ "${existing_label}" != "${expected_value}" ]]; then
    echo "Container '${container_name}' exists but is not managed by this project." >&2
    exit 1
  fi
}

relay_managed_value="egress-upstream-relay"
assert_managed_or_missing "${proxy_container}" "${managed_value}"
assert_managed_or_missing "${upstream_relay_container}" "${relay_managed_value}"
docker container rm --force "${proxy_container}" >/dev/null 2>&1 || true
docker container rm --force "${upstream_relay_container}" >/dev/null 2>&1 || true

proxy_created=false
relay_created=false
cleanup_on_error() {
  if [[ "${proxy_created}" == "true" ]]; then
    docker container rm --force "${proxy_container}" >/dev/null 2>&1 || true
  fi
  if [[ "${relay_created}" == "true" ]]; then
    docker container rm --force "${upstream_relay_container}" >/dev/null 2>&1 || true
  fi
}
trap cleanup_on_error ERR

if [[ "${upstream_host}" == "127.0.0.1" || "${upstream_host}" == "localhost" ]]; then
  if [[ ! "${upstream_relay_port}" =~ ^[0-9]+$ ]] \
    || ((10#${upstream_relay_port} < 1 || 10#${upstream_relay_port} > 65535)); then
    echo "SANDBOX_EGRESS_UPSTREAM_RELAY_PORT must be from 1 to 65535." >&2
    exit 1
  fi
  docker run --detach \
    --name "${upstream_relay_container}" \
    --label "${managed_label}=${relay_managed_value}" \
    --runtime runc \
    --network host \
    --no-healthcheck \
    --restart unless-stopped \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --memory 64m \
    --cpus 0.25 \
    --pids-limit 64 \
    --entrypoint socat \
    "${proxy_image}" \
    "TCP-LISTEN:${upstream_relay_port},bind=${uplink_gateway},reuseaddr,fork" \
    "TCP:${upstream_host}:${upstream_port}" >/dev/null
  relay_created=true
  upstream_host="${uplink_gateway}"
  upstream_port="${upstream_relay_port}"
fi

proxy_env_args=()
if [[ -n "${upstream_host}" ]]; then
  proxy_env_args+=(
    --env "UPSTREAM_PROXY_HOST=${upstream_host}"
    --env "UPSTREAM_PROXY_PORT=${upstream_port}"
  )
fi

docker run --detach \
  --name "${proxy_container}" \
  --label "${managed_label}=${managed_value}" \
  --network "${uplink_network}" \
  --runtime runc \
  --restart unless-stopped \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --memory "${memory_limit}" \
  --cpus "${cpu_limit}" \
  --pids-limit "${pids_limit}" \
  --tmpfs /run/squid:rw,noexec,nosuid,nodev,size=1m,mode=1777 \
  --tmpfs /var/log/squid:rw,noexec,nosuid,nodev,size=8m,mode=1777 \
  --tmpfs /var/spool/squid:rw,noexec,nosuid,nodev,size=16m,mode=1777 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777 \
  --env "SANDBOX_CLIENT_CIDR=${sandbox_cidr}" \
  "${proxy_env_args[@]}" \
  --volume "${allowed_domains_file}:/etc/squid/allowed-domains.txt:ro" \
  "${proxy_image}" >/dev/null
proxy_created=true

# Keep the non-internal uplink as the default route while exposing only the
# network-scoped alias to Agent sandboxes.
docker network connect \
  --alias "${proxy_alias}" \
  --gw-priority -1 \
  "${sandbox_network}" \
  "${proxy_container}"

for _ in $(seq 1 30); do
  health="$(docker container inspect --format '{{.State.Health.Status}}' "${proxy_container}")"
  case "${health}" in
    healthy)
      trap - ERR
      echo "Egress proxy '${proxy_container}' is healthy on ${sandbox_network} (${sandbox_cidr})."
      exit 0
      ;;
    unhealthy)
      docker container logs "${proxy_container}" >&2
      echo "Egress proxy '${proxy_container}' is unhealthy." >&2
      exit 1
      ;;
  esac
  sleep 1
done

docker container logs "${proxy_container}" >&2
echo "Timed out waiting for egress proxy '${proxy_container}' to become healthy." >&2
exit 1
