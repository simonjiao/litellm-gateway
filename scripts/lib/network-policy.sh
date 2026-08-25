#!/usr/bin/env bash

network_policy_select_iptables() {
  local policy_image="$1"

  NETWORK_POLICY_IPTABLES=(iptables)
  if ((EUID != 0)); then
    if sudo -n true >/dev/null 2>&1; then
      NETWORK_POLICY_IPTABLES=(sudo -n iptables)
    else
      if ! docker image inspect "${policy_image}" >/dev/null 2>&1; then
        echo "Network policy image '${policy_image}' is missing; run build-network-policy.sh first." >&2
        return 1
      fi
      NETWORK_POLICY_IPTABLES=(
        docker run --rm
        --runtime runc
        --network host
        --read-only
        --cap-drop ALL
        --cap-add NET_ADMIN
        --security-opt no-new-privileges:true
        --tmpfs /run:rw,noexec,nosuid,nodev,size=1m
        "${policy_image}"
      )
    fi
  fi

  if ! network_policy_iptables -nL DOCKER-USER >/dev/null 2>&1; then
    echo "Docker's DOCKER-USER firewall chain is unavailable." >&2
    return 1
  fi
  if ! network_policy_iptables -nL INPUT >/dev/null 2>&1; then
    echo "The host INPUT firewall chain is unavailable." >&2
    return 1
  fi
}

network_policy_iptables() {
  "${NETWORK_POLICY_IPTABLES[@]}" "$@"
}

network_policy_bridge_name() {
  local network_name="$1"
  local network_id bridge_name

  network_id="$(docker network inspect --format '{{.Id}}' "${network_name}")"
  bridge_name="$(
    docker network inspect \
      --format '{{index .Options "com.docker.network.bridge.name"}}' \
      "${network_name}"
  )"
  if [[ -z "${bridge_name}" || "${bridge_name}" == "<no value>" ]]; then
    bridge_name="br-${network_id:0:12}"
  fi
  printf '%s\n' "${bridge_name}"
}

network_policy_ensure_chain() {
  local chain="$1"

  if network_policy_iptables -N "${chain}" 2>/dev/null; then
    return
  fi
  network_policy_iptables -nL "${chain}" >/dev/null
}

network_policy_prepare_dispatcher() {
  local dispatcher="$1"
  local chain_a="$2"
  local chain_b="$3"
  local rules action chain jump target remainder
  local active=""
  local rule_count=0

  network_policy_ensure_chain "${dispatcher}"
  network_policy_ensure_chain "${chain_a}"
  network_policy_ensure_chain "${chain_b}"

  rules="$(network_policy_iptables -S "${dispatcher}")"
  while read -r action chain jump target remainder; do
    if [[ "${action}" != "-A" ]]; then
      continue
    fi
    if [[ "${chain}" != "${dispatcher}" || "${jump}" != "-j" \
      || -n "${remainder:-}" ]]; then
      echo "Managed dispatcher '${dispatcher}' has unexpected rules." >&2
      return 1
    fi
    active="${target}"
    ((rule_count += 1))
  done <<<"${rules}"

  if ((rule_count > 1)); then
    echo "Managed dispatcher '${dispatcher}' must contain at most one rule." >&2
    return 1
  fi

  case "${active}" in
    "")
      NETWORK_POLICY_INACTIVE_CHAIN="${chain_a}"
      NETWORK_POLICY_DISPATCH_HAS_ACTIVE=false
      ;;
    "${chain_a}")
      NETWORK_POLICY_INACTIVE_CHAIN="${chain_b}"
      NETWORK_POLICY_DISPATCH_HAS_ACTIVE=true
      ;;
    "${chain_b}")
      NETWORK_POLICY_INACTIVE_CHAIN="${chain_a}"
      NETWORK_POLICY_DISPATCH_HAS_ACTIVE=true
      ;;
    *)
      echo "Managed dispatcher '${dispatcher}' points to unexpected chain '${active}'." >&2
      return 1
      ;;
  esac

  network_policy_iptables -F "${NETWORK_POLICY_INACTIVE_CHAIN}"
}

network_policy_switch_dispatcher() {
  local dispatcher="$1"
  local active_chain="$2"
  local has_active="$3"

  if [[ "${has_active}" == "true" ]]; then
    network_policy_iptables -R "${dispatcher}" 1 -j "${active_chain}"
  else
    network_policy_iptables -A "${dispatcher}" -j "${active_chain}"
  fi
}

network_policy_ensure_hook() {
  local parent_chain="$1"
  local bridge_name="$2"
  local dispatcher="$3"

  if ! network_policy_iptables -C "${parent_chain}" \
    -i "${bridge_name}" -j "${dispatcher}" 2>/dev/null; then
    network_policy_iptables -I "${parent_chain}" 1 \
      -i "${bridge_name}" -j "${dispatcher}"
  fi
}
