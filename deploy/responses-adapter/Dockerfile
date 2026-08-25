FROM python:3.11-slim-bookworm

ARG PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/responses-adapter/src \
    HOME=/home/service

WORKDIR /opt/responses-adapter
COPY src/codex_responses_adapter ./src/codex_responses_adapter
COPY src/sandbox_manager/__init__.py src/sandbox_manager/models.py ./src/sandbox_manager/
COPY src/sandbox_worker/__init__.py src/sandbox_worker/models.py ./src/sandbox_worker/

RUN --mount=type=cache,target=/root/.cache/pip \
    PIP_INDEX_URL="${PYPI_INDEX_URL}" pip install \
      "fastapi>=0.115,<1.0" \
      "httpx>=0.28,<1.0" \
      "pydantic>=2.10,<3.0" \
      "pydantic-settings>=2.7,<3.0" \
      "uvicorn>=0.33,<1.0" \
    && groupadd --gid 10001 service \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin service \
    && chown -R 10001:10001 /opt/responses-adapter /home/service

USER 10001:10001
EXPOSE 8090
ENTRYPOINT ["python", "-m", "codex_responses_adapter.cli"]
