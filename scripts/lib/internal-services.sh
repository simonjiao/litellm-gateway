#!/usr/bin/env bash

sandbox_valid_dns_name() {
  local value="$1"
  [[ "${value}" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$ ]]
}

sandbox_valid_container_name() {
  local value="$1"
  [[ "${value}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]
}

sandbox_parse_internal_services() {
  local value="$1"
  local record dns_name endpoint container_name service_port
  local -a records=()

  SANDBOX_INTERNAL_SERVICE_DNS_NAMES=()
  SANDBOX_INTERNAL_SERVICE_CONTAINERS=()
  SANDBOX_INTERNAL_SERVICE_PORTS=()

  if [[ -z "${value}" ]]; then
    return
  fi

  IFS=',' read -r -a records <<<"${value}"
  for record in "${records[@]}"; do
    if [[ "${record}" != *=* ]]; then
      echo "Internal service '${record}' must use dns-name=container-name:port." >&2
      return 1
    fi
    dns_name="${record%%=*}"
    endpoint="${record#*=}"
    if [[ ! "${endpoint}" =~ ^([A-Za-z0-9][A-Za-z0-9_.-]*):([0-9]+)$ ]]; then
      echo "Internal service '${record}' must use dns-name=container-name:port." >&2
      return 1
    fi
    container_name="${BASH_REMATCH[1]}"
    service_port="${BASH_REMATCH[2]}"
    if ! sandbox_valid_dns_name "${dns_name}"; then
      echo "Invalid Agent DNS name: ${dns_name}" >&2
      return 1
    fi
    if ! sandbox_valid_container_name "${container_name}"; then
      echo "Invalid internal service container name: ${container_name}" >&2
      return 1
    fi
    if ((10#${service_port} < 1 || 10#${service_port} > 65535)); then
      echo "Internal service port must be from 1 to 65535: ${record}" >&2
      return 1
    fi
    SANDBOX_INTERNAL_SERVICE_DNS_NAMES+=("${dns_name}")
    SANDBOX_INTERNAL_SERVICE_CONTAINERS+=("${container_name}")
    SANDBOX_INTERNAL_SERVICE_PORTS+=("${service_port}")
  done
}
