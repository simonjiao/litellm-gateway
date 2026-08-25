FROM node:24-bookworm-slim

ARG CODEX_VERSION=0.149.0
ARG DEBIAN_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian
ARG DEBIAN_SECURITY_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian-security

RUN sed -i \
      -e "s|http://security.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
      -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
      -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
      /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force

RUN python3 -m venv /opt/worker-venv
ENV PATH="/opt/worker-venv/bin:${PATH}" \
    HOME=/home/agent \
    PYTHONPATH=/opt/sandbox-worker/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /opt/sandbox-worker
COPY src/sandbox_api ./src/sandbox_api
COPY src/sandbox_worker ./src/sandbox_worker
ARG PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN PIP_INDEX_URL="${PYPI_INDEX_URL}" pip install --no-cache-dir \
      "fastapi>=0.115,<1.0" \
      "pydantic>=2.10,<3.0" \
      "pydantic-settings>=2.7,<3.0" \
      "uvicorn>=0.33,<1.0"

RUN groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin agent \
    && mkdir -p /workspace /home/agent/.codex \
    && chown -R 10001:10001 /workspace /home/agent /opt/sandbox-worker

USER 10001:10001
WORKDIR /workspace
EXPOSE 8091
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import os, urllib.request; port=os.environ.get('SANDBOX_WORKER_PORT', '8091'); urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=2)"
ENTRYPOINT ["python", "-m", "sandbox_worker.cli"]
