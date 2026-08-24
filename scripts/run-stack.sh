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

: "${LITELLM_MASTER_KEY:?LITELLM_MASTER_KEY is required}"
: "${CODEX_ADAPTER_API_KEY:?CODEX_ADAPTER_API_KEY is required}"
: "${SANDBOX_MANAGER_API_KEY:?SANDBOX_MANAGER_API_KEY is required}"
: "${SANDBOX_MANAGER_WORKER_TOKEN_SECRET:?SANDBOX_MANAGER_WORKER_TOKEN_SECRET is required}"

project_root="$(pwd)"
runtime_root="${project_root}/.runtime"
codex_config_root="${CODEX_HOME:-${HOME}/.codex}"
auth_source="${SANDBOX_MANAGER_CODEX_AUTH_SOURCE_FILE:-${codex_config_root}/auth.json}"
secret_dir="${SANDBOX_MANAGER_SECRET_DIR:-${runtime_root}/codex}"
auth_file="${secret_dir}/auth.json"
config_source="${SANDBOX_MANAGER_CODEX_CONFIG_SOURCE_FILE:-}"
resolv_conf_file="${runtime_root}/agent-dns/resolv.conf"

internal_no_proxy_names=()
if [[ -n "${SANDBOX_AGENT_INTERNAL_SERVICES:-}" ]]; then
  IFS=',' read -r -a service_records <<<"${SANDBOX_AGENT_INTERNAL_SERVICES}"
  for record in "${service_records[@]}"; do
    if [[ ! "${record}" =~ ^([^=]+)=([A-Za-z0-9][A-Za-z0-9_.-]*):([0-9]+)$ ]]; then
      echo "Internal service '${record}' must use dns-name=container-name:port." >&2
      exit 1
    fi
    internal_no_proxy_names+=("${BASH_REMATCH[1]}")
  done
fi

if [[ "${secret_dir}" != /* ]]; then
  secret_dir="$(realpath -m "${secret_dir}")"
  auth_file="${secret_dir}/auth.json"
fi
if [[ ! -r "${auth_source}" ]]; then
  echo "Codex authentication file is not readable: ${auth_source}" >&2
  exit 1
fi

install -d -m 0700 "${runtime_root}"
install -d -m 0755 "${secret_dir}"
if [[ "$(realpath "${auth_source}")" != "${auth_file}" ]]; then
  install -m 0444 "${auth_source}" "${auth_file}"
fi

export SANDBOX_MANAGER_SECRET_DIR="${secret_dir}"
export SANDBOX_MANAGER_CODEX_AUTH_FILE="${auth_file}"
export SANDBOX_MANAGER_RESOLV_CONF_FILE="${resolv_conf_file}"
export DOCKER_GID="$(stat -c '%g' /var/run/docker.sock)"
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
docker compose build
bash scripts/run-egress-proxy.sh
bash scripts/run-agent-dns.sh

if [[ ! -r "${resolv_conf_file}" ]]; then
  echo "Agent resolver was not generated: ${resolv_conf_file}" >&2
  exit 1
fi

docker compose up --detach --wait --wait-timeout 120
bash scripts/apply-agent-rpc-policy.sh
bash scripts/apply-agent-egress-policy.sh

echo "Gateway, Responses Adapter, Sandbox Manager, Agent DNS, and policy egress are ready."
