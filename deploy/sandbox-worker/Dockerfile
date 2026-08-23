FROM node:24-bookworm-slim

ARG CODEX_VERSION=0.149.0

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force

RUN python3 -m venv /opt/worker-venv
ENV PATH="/opt/worker-venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /opt/gateway
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . \
    && pip install --no-cache-dir \
      "fastapi>=0.115,<1.0" \
      "pydantic>=2.10,<3.0" \
      "pydantic-settings>=2.7,<3.0" \
      "uvicorn>=0.33,<1.0"

RUN groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin agent \
    && mkdir -p /workspace /home/agent/.codex \
    && chown -R 10001:10001 /workspace /home/agent /opt/gateway

USER 10001:10001
WORKDIR /workspace
EXPOSE 8091
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8091/healthz', timeout=2)"
ENTRYPOINT ["sandbox-agent-worker"]
