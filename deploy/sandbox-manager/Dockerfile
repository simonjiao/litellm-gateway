FROM python:3.11-slim-bookworm

ARG PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/sandbox-manager/src \
    HOME=/home/service

WORKDIR /opt/sandbox-manager
COPY src/sandbox_api ./src/sandbox_api
COPY src/sandbox_manager ./src/sandbox_manager

RUN --mount=type=cache,target=/root/.cache/pip \
    PIP_INDEX_URL="${PYPI_INDEX_URL}" pip install \
      "docker>=7.1,<8.0" \
      "fastapi>=0.115,<1.0" \
      "httpx>=0.28,<1.0" \
      "pydantic>=2.10,<3.0" \
      "pydantic-settings>=2.7,<3.0" \
      "uvicorn>=0.33,<1.0" \
    && groupadd --gid 10001 service \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin service \
    && mkdir -p /var/lib/sandbox-manager \
    && chown -R 10001:10001 /opt/sandbox-manager /home/service /var/lib/sandbox-manager

USER 10001:10001
EXPOSE 8092
ENTRYPOINT ["python", "-m", "sandbox_manager.cli"]
