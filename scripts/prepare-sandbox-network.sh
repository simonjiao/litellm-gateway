#!/usr/bin/env bash
set -euo pipefail

sandbox_network="${SANDBOX_HOST_DOCKER_NETWORK:-codex-agent-egress}"
sandbox_runtime="${SANDBOX_HOST_DOCKER_RUNTIME:-runsc}"
runtimes="$(docker info --format '{{json .Runtimes}}')"

case "${runtimes}" in
  *\"${sandbox_runtime}\"*) ;;
  *)
    echo "Required Docker runtime '${sandbox_runtime}' is unavailable." >&2
    exit 1
    ;;
esac

if docker network inspect "${sandbox_network}" >/dev/null 2>&1; then
  internal="$(docker network inspect --format '{{.Internal}}' "${sandbox_network}")"
  if [[ "${internal}" != "true" ]]; then
    echo "Existing network '${sandbox_network}' is not internal." >&2
    exit 1
  fi
else
  docker network create --internal "${sandbox_network}"
fi

echo "Sandbox network '${sandbox_network}' is ready. Attach only the policy egress proxy and explicitly allowed internal services."
