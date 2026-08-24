FROM python:3.11-slim-bookworm

ARG PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG LITELLM_INDEX_URL=https://pypi.org/simple
ARG LITELLM_VERSION=1.99.0rc1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/service

WORKDIR /opt/gateway
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config

RUN --mount=type=cache,target=/root/.cache/pip \
    PIP_DEFAULT_TIMEOUT=60 PIP_INDEX_URL="${LITELLM_INDEX_URL}" \
      pip install --no-deps "litellm==${LITELLM_VERSION}" \
    && PIP_DEFAULT_TIMEOUT=60 PIP_INDEX_URL="${PYPI_INDEX_URL}" \
      pip install . \
    && groupadd --gid 10001 service \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin service \
    && chown -R 10001:10001 /opt/gateway /home/service

USER 10001:10001
