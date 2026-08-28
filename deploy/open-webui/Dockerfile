FROM ghcr.io/open-webui/open-webui:v0.11.1

COPY deploy/open-webui/agent_open_webui /app/backend/agent_open_webui
COPY src/sandbox_api /app/backend/sandbox_api
COPY deploy/open-webui/apply_patch.py /tmp/apply-agent-workspace-patch.py

RUN python /tmp/apply-agent-workspace-patch.py \
    && rm /tmp/apply-agent-workspace-patch.py \
    && python -m compileall -q /app/backend/agent_open_webui /app/backend/sandbox_api
