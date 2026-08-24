FROM debian:bookworm-slim

ARG DEBIAN_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian
ARG DEBIAN_SECURITY_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian-security

RUN sed -i \
      -e "s|http://security.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
      -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
      -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
      /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates socat squid \
    && rm -rf /var/lib/apt/lists/* /var/log/squid/* /var/spool/squid/*

COPY --chmod=0444 squid.conf.template /etc/squid/squid.conf.template
COPY --chmod=0555 docker-entrypoint.sh /usr/local/bin/egress-proxy-entrypoint

RUN mkdir -p /run/squid \
    && chown proxy:proxy /run/squid

USER proxy:proxy
EXPOSE 3128
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD ["perl", "-MIO::Socket::INET", "-e", "exit(IO::Socket::INET->new(PeerAddr=>'127.0.0.1',PeerPort=>3128,Proto=>'tcp',Timeout=>2)?0:1)"]
ENTRYPOINT ["egress-proxy-entrypoint"]
