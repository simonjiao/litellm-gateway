#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

docker run --rm --runtime runc --network none --read-only --tmpfs /tmp \
  -e DATA_DIR=/tmp/agent-bff-test \
  -e WEBUI_SECRET_KEY=isolated-bff-test-key \
  -e AGENT_WORKSPACE_ENABLED=false \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD/deploy/open-webui/agent_open_webui:/app/backend/agent_open_webui:ro" \
  -v "$PWD/deploy/open-webui/tests:/app/backend/tests_agent:ro" \
  --entrypoint sh "${AGENT_OPEN_WEBUI_IMAGE:-agent-open-webui:0.3.0}" \
  -c 'mkdir -p "$DATA_DIR" && python -m unittest discover -s tests_agent -v'
