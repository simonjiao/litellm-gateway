#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo ".env is required; copy .env.example and configure it first." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

exec uv run python scripts/smoke.py
