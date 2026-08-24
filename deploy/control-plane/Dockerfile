FROM python:3.11-slim-bookworm

ARG PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/service

WORKDIR /opt/gateway
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config

RUN PIP_INDEX_URL="${PYPI_INDEX_URL}" pip install --no-cache-dir . \
    && groupadd --gid 10001 service \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin service \
    && chown -R 10001:10001 /opt/gateway /home/service

USER 10001:10001
