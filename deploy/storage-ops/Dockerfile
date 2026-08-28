FROM python:3.11-slim-bookworm

ARG DEBIAN_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian
ARG DEBIAN_SECURITY_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian-security
ARG PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/storage-ops/src \
    HOME=/home/storage-ops

RUN sed -i \
      -e "s|http://security.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
      -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
      -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
      /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates restic \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/storage-ops
COPY src/storage_ops ./src/storage_ops

RUN --mount=type=cache,target=/root/.cache/pip \
    PIP_INDEX_URL="${PYPI_INDEX_URL}" pip install "httpx>=0.28,<1.0" \
    && groupadd --gid 10001 storage-ops \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin storage-ops \
    && mkdir -p /workspace /restore/workspace \
    && chown -R 10001:10001 /opt/storage-ops /home/storage-ops /workspace /restore

USER 10001:10001
ENTRYPOINT ["python", "-m", "storage_ops"]
