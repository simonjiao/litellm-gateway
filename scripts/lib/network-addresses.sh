#!/usr/bin/env bash

# Deployment defaults; .env may override them. Static service addresses must be
# outside Docker's dynamic allocation pools, including while a service is absent.
export SANDBOX_RPC_SUBNET="${SANDBOX_RPC_SUBNET:-172.21.0.0/16}"
export SANDBOX_RPC_GATEWAY="${SANDBOX_RPC_GATEWAY:-172.21.0.1}"
export SANDBOX_RPC_IP_RANGE="${SANDBOX_RPC_IP_RANGE:-172.21.128.0/17}"
export SANDBOX_ADAPTER_RPC_IP="${SANDBOX_ADAPTER_RPC_IP:-172.21.0.10}"
export SANDBOX_EGRESS_SUBNET="${SANDBOX_EGRESS_SUBNET:-172.22.0.0/16}"
export SANDBOX_EGRESS_GATEWAY="${SANDBOX_EGRESS_GATEWAY:-172.22.0.1}"
export SANDBOX_EGRESS_IP_RANGE="${SANDBOX_EGRESS_IP_RANGE:-172.22.128.0/17}"
export SANDBOX_AGENT_DNS_IP="${SANDBOX_AGENT_DNS_IP:-172.22.0.53}"
export SANDBOX_EGRESS_PROXY_IP="${SANDBOX_EGRESS_PROXY_IP:-172.22.0.54}"

network_addresses_validate() {
  python3 - <<'PY'
import ipaddress
import os
import sys

try:
    networks = []
    for role, services in (
        ("RPC", ["SANDBOX_ADAPTER_RPC_IP"]),
        ("EGRESS", ["SANDBOX_AGENT_DNS_IP", "SANDBOX_EGRESS_PROXY_IP"]),
    ):
        prefix = f"SANDBOX_{role}_"
        subnet = ipaddress.IPv4Network(os.environ[prefix + "SUBNET"])
        pool = ipaddress.IPv4Network(os.environ[prefix + "IP_RANGE"])
        gateway = ipaddress.IPv4Address(os.environ[prefix + "GATEWAY"])
        if not pool.subnet_of(subnet) or pool == subnet:
            raise ValueError(f"{prefix}IP_RANGE must be a proper subnetwork of {subnet}")
        addresses = [gateway, *(ipaddress.IPv4Address(os.environ[key]) for key in services)]
        if len(set(addresses)) != len(addresses):
            raise ValueError(f"{role} gateway and static addresses must be distinct")
        for address in addresses:
            if address not in subnet or address in (subnet.network_address, subnet.broadcast_address):
                raise ValueError(f"{address} is not a usable address in {subnet}")
            if address in pool:
                raise ValueError(f"Static address {address} must be outside dynamic pool {pool}")
        networks.append(subnet)
    if networks[0].overlaps(networks[1]):
        raise ValueError("RPC and egress subnets must not overlap")
except ValueError as exc:
    print(f"Invalid Agent network addresses: {exc}", file=sys.stderr)
    sys.exit(1)
PY
}

network_address_expect() {
  local service="$1" actual="$2" expected="$3"
  if [[ "${actual}" != "${expected}" ]]; then
    echo "${service} address '${actual}' does not match configured '${expected}'; redeploy the service." >&2
    return 1
  fi
}
