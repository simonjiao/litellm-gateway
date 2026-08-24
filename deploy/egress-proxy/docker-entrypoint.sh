#!/bin/sh
set -eu

template=/etc/squid/squid.conf.template
runtime_config=/run/squid/squid.conf
upstream_config=/run/squid/upstream.conf
allowlist=/etc/squid/allowed-domains.txt
sandbox_cidr="${SANDBOX_CLIENT_CIDR:?SANDBOX_CLIENT_CIDR is required}"
upstream_host="${UPSTREAM_PROXY_HOST:-}"
upstream_port="${UPSTREAM_PROXY_PORT:-}"

if ! printf '%s\n' "${sandbox_cidr}" \
  | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}/([0-9]|[12][0-9]|3[0-2])$'; then
  echo "SANDBOX_CLIENT_CIDR must be an IPv4 CIDR" >&2
  exit 1
fi

if [ ! -r "${allowlist}" ]; then
  echo "Allowed-domain file is not readable: ${allowlist}" >&2
  exit 1
fi

if ! awk '
  BEGIN { entries = 0; valid = 1 }
  /^[[:space:]]*$/ || /^[[:space:]]*#/ { next }
  {
    entries += 1
    if ($0 !~ /^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$/) {
      valid = 0
    }
  }
  END { exit entries > 0 && valid ? 0 : 1 }
' "${allowlist}"; then
  echo "Allowed-domain file must contain only exact DNS hostnames" >&2
  exit 1
fi

if [ -n "${upstream_host}" ] || [ -n "${upstream_port}" ]; then
  if ! printf '%s\n' "${upstream_host}" \
    | grep -Eq '^([A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)(\.([A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?))*$'; then
    echo "UPSTREAM_PROXY_HOST must be a DNS hostname or IPv4 address" >&2
    exit 1
  fi
  case "${upstream_port}" in
    ''|*[!0-9]*)
      echo "UPSTREAM_PROXY_PORT must be an integer from 1 to 65535" >&2
      exit 1
      ;;
  esac
  if [ "${upstream_port}" -lt 1 ] || [ "${upstream_port}" -gt 65535 ]; then
    echo "UPSTREAM_PROXY_PORT must be an integer from 1 to 65535" >&2
    exit 1
  fi
  {
    printf 'cache_peer %s parent %s 0 no-query default\n' "${upstream_host}" "${upstream_port}"
    printf 'never_direct allow all\n'
  } >"${upstream_config}"
else
  printf '# Direct uplink mode.\n' >"${upstream_config}"
fi

sed "s|@SANDBOX_CLIENT_CIDR@|${sandbox_cidr}|g" "${template}" >"${runtime_config}"
squid -f "${runtime_config}" -k parse
exec squid -N -f "${runtime_config}"
