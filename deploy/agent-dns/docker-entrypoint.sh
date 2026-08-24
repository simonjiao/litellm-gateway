#!/bin/sh
set -eu

template=/etc/agent-dns/dnsmasq.conf.template
runtime_config=/run/agent-dns/dnsmasq.conf
hosts_file=/etc/agent-dns/hosts

if [ ! -r "${hosts_file}" ]; then
  echo "Agent DNS hosts file is not readable: ${hosts_file}" >&2
  exit 1
fi

if ! awk '
  BEGIN { entries = 0; valid = 1 }
  /^[[:space:]]*$/ || /^[[:space:]]*#/ { next }
  NF != 2 { valid = 0; next }
  {
    entries += 1
    split($1, octets, ".")
    if (length(octets) != 4) {
      valid = 0
    }
    for (i = 1; i <= 4; i += 1) {
      if (octets[i] !~ /^[0-9]+$/ || octets[i] + 0 > 255) {
        valid = 0
      }
    }
    if ($2 !~ /^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$/) {
      valid = 0
    }
  }
  END { exit entries > 0 && valid ? 0 : 1 }
' "${hosts_file}"; then
  echo "Agent DNS hosts file must contain only '<IPv4> <exact-hostname>' records" >&2
  exit 1
fi

cp "${template}" "${runtime_config}"
dnsmasq --test --conf-file="${runtime_config}"
exec dnsmasq --keep-in-foreground --conf-file="${runtime_config}"
