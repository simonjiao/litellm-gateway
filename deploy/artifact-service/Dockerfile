FROM python:3.11-slim-bookworm

ARG PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/artifact-service/src \
    HOME=/home/artifact-service

WORKDIR /opt/artifact-service
COPY src/artifact_service ./src/artifact_service
COPY src/sandbox_api ./src/sandbox_api

RUN --mount=type=cache,target=/root/.cache/pip \
    PIP_INDEX_URL="${PYPI_INDEX_URL}" pip install --timeout 120 --retries 5 \
      "fastapi>=0.115,<1.0" \
      "uvicorn>=0.33,<1.0" \
      "pydantic>=2.10,<3.0" \
      "pydantic-settings>=2.7,<3.0" \
      "boto3>=1.42,<2.0" \
    && groupadd --gid 10003 artifact-service \
    && useradd --uid 10003 --gid 10003 --create-home --shell /usr/sbin/nologin artifact-service \
    && chown -R 10003:10003 /opt/artifact-service /home/artifact-service

USER 10003:10003
CMD ["python", "-m", "artifact_service.cli"]
