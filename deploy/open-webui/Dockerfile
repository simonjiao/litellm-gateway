FROM node:22-alpine3.20 AS frontend
WORKDIR /src
RUN apk add --no-cache git
# The frontend source and backend image must use the same upstream release.
ADD https://codeload.github.com/open-webui/open-webui/tar.gz/d3e8bf3405e848cfba377814d0aa7ba7290e414d /tmp/open-webui.tar.gz
RUN tar -xzf /tmp/open-webui.tar.gz --strip-components=1 -C /src \
    && rm /tmp/open-webui.tar.gz
RUN --mount=type=cache,target=/root/.npm npm ci --force --no-audit --maxsockets=8
COPY deploy/open-webui/frontend.patch /tmp/frontend.patch
RUN git apply /tmp/frontend.patch
COPY deploy/open-webui/frontend/agent-files.ts /src/src/lib/agent-files.ts
ENV NODE_OPTIONS=--max-old-space-size=8192
RUN npm run build

FROM ghcr.io/open-webui/open-webui:v0.11.1

COPY --from=frontend /src/build /app/build
COPY deploy/open-webui/agent_open_webui /app/backend/agent_open_webui
COPY src/sandbox_api /app/backend/sandbox_api
COPY deploy/open-webui/apply_patch.py /tmp/apply-agent-workspace-patch.py

RUN python /tmp/apply-agent-workspace-patch.py \
    && rm /tmp/apply-agent-workspace-patch.py \
    && python -m compileall -q /app/backend/agent_open_webui /app/backend/sandbox_api
