FROM debian:bookworm-slim

ARG DEBIAN_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian
ARG DEBIAN_SECURITY_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian-security

RUN sed -i \
      -e "s|http://security.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
      -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
      -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
      /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install --yes --no-install-recommends dnsmasq-base netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=0444 dnsmasq.conf.template /etc/agent-dns/dnsmasq.conf.template
COPY --chmod=0555 docker-entrypoint.sh /usr/local/bin/agent-dns-entrypoint

RUN chmod 0555 /etc/agent-dns

USER 65534:65534
EXPOSE 53/tcp 53/udp
HEALTHCHECK --interval=10s --timeout=3s --start-period=3s --retries=3 \
  CMD ["nc", "-z", "-w", "2", "127.0.0.1", "53"]
ENTRYPOINT ["agent-dns-entrypoint"]
