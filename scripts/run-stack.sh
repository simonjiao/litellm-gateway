#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo ".env is required; copy .env.example and configure deployment credentials first." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

# shellcheck source=scripts/lib/internal-services.sh
source scripts/lib/internal-services.sh

: "${LITELLM_MASTER_KEY:?LITELLM_MASTER_KEY is required}"
: "${CODEX_ADAPTER_API_KEY:?CODEX_ADAPTER_API_KEY is required}"
: "${SANDBOX_MANAGER_API_KEY:?SANDBOX_MANAGER_API_KEY is required}"
: "${SANDBOX_MANAGER_WORKER_TOKEN_SECRET:?SANDBOX_MANAGER_WORKER_TOKEN_SECRET is required}"

project_root="$(pwd)"
runtime_root="${project_root}/.runtime"
codex_config_root="${CODEX_HOME:-${HOME}/.codex}"
auth_source="${SANDBOX_MANAGER_CODEX_AUTH_SOURCE_FILE:-${codex_config_root}/auth.json}"
secret_root="${SANDBOX_MANAGER_SECRET_ROOT:-${runtime_root}/codex-secret}"
config_source="${SANDBOX_MANAGER_CODEX_CONFIG_SOURCE_FILE:-}"
resolv_conf_file="${runtime_root}/agent-dns/resolv.conf"
docker_socket="${SANDBOX_MANAGER_DOCKER_SOCKET:-/var/run/docker.sock}"

sandbox_parse_internal_services "${SANDBOX_AGENT_INTERNAL_SERVICES:-}"
internal_no_proxy_names=("${SANDBOX_INTERNAL_SERVICE_DNS_NAMES[@]}")

if [[ "${secret_root}" != /* ]]; then
  secret_root="$(realpath -m "${secret_root}")"
fi
secret_dir="${secret_root}/mounted"
auth_file="${secret_dir}/auth.json"
if [[ ! -r "${auth_source}" ]]; then
  echo "Codex authentication file is not readable: ${auth_source}" >&2
  exit 1
fi

install -d -m 0700 "${runtime_root}"
if [[ -e "${secret_root}" ]]; then
  secret_owner="$(stat -c '%u' "${secret_root}")"
  secret_mode="$(stat -c '%a' "${secret_root}")"
  if [[ ! -d "${secret_root}" || "${secret_owner}" != "${EUID}" \
    || "${secret_mode}" != "700" ]]; then
    echo "Secret root '${secret_root}' must be owned by the current user with mode 0700." >&2
    exit 1
  fi
else
  install -d -m 0700 "${secret_root}"
fi
install -d -m 0755 "${secret_dir}"
if [[ "$(realpath "${auth_source}")" != "${auth_file}" ]]; then
  install -m 0444 "${auth_source}" "${auth_file}"
fi

export SANDBOX_MANAGER_SECRET_DIR="${secret_dir}"
export SANDBOX_MANAGER_CODEX_AUTH_FILE="${auth_file}"
export SANDBOX_MANAGER_RESOLV_CONF_FILE="${resolv_conf_file}"
if [[ ! -e "${docker_socket}" ]]; then
  echo "Sandbox Manager Docker API socket is missing: ${docker_socket}" >&2
  exit 1
fi
export DOCKER_GID="$(stat -c '%g' "${docker_socket}")"
export SANDBOX_MANAGER_INTERNAL_NO_PROXY="$(
  IFS=,
  printf '%s' "${internal_no_proxy_names[*]}"
)"

if [[ -n "${config_source}" ]]; then
  if [[ ! -r "${config_source}" ]]; then
    echo "Codex configuration file is not readable: ${config_source}" >&2
    exit 1
  fi
  config_file="${secret_dir}/config.toml"
  if [[ "$(realpath "${config_source}")" != "${config_file}" ]]; then
    install -m 0444 "${config_source}" "${config_file}"
  fi
  export SANDBOX_MANAGER_CODEX_CONFIG_FILE="${config_file}"
else
  export SANDBOX_MANAGER_CODEX_CONFIG_FILE=""
fi

bash scripts/prepare-sandbox-network.sh
bash scripts/build-sandbox-worker.sh
bash scripts/build-egress-proxy.sh
bash scripts/build-agent-dns.sh
bash scripts/build-network-policy.sh
docker compose build gateway responses-adapter sandbox-manager

stop_entry_workloads_on_error() {
  docker compose stop gateway responses-adapter >/dev/null 2>&1 || true
}
trap stop_entry_workloads_on_error ERR

docker compose stop gateway responses-adapter
bash scripts/run-egress-proxy.sh
bash scripts/run-agent-dns.sh

if [[ ! -r "${resolv_conf_file}" ]]; then
  echo "Agent resolver was not generated: ${resolv_conf_file}" >&2
  exit 1
fi

docker compose up --detach --wait --wait-timeout 120 --force-recreate \
  sandbox-manager responses-adapter
bash scripts/apply-agent-rpc-policy.sh
bash scripts/apply-agent-egress-policy.sh
docker compose up --detach --wait --wait-timeout 120 --force-recreate --no-deps \
  gateway
trap - ERR

echo "Gateway, Responses Adapter, Sandbox Manager, Agent DNS, and policy egress are ready."
