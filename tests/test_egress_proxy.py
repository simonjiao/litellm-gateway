import os
import shutil
import subprocess
from pathlib import Path

from sandbox_manager.settings import ManagerSettings

ROOT = Path(__file__).resolve().parents[1]
PROXY_DIR = ROOT / "deploy" / "egress-proxy"


def test_agent_network_has_generic_default_name() -> None:
    settings = ManagerSettings()

    assert settings.egress_network == "agent-egress"
    assert settings.egress_proxy_url == "http://egress-proxy:3128"


def test_basic_smoke_allowlist_uses_exact_hosts() -> None:
    domains = [
        line.strip()
        for line in (PROXY_DIR / "allowed-domains.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert domains == ["chatgpt.com", "auth.openai.com"]
    assert all(not domain.startswith(".") for domain in domains)


def test_squid_policy_is_default_deny_and_rejects_private_destinations_first() -> None:
    lines = [
        line.strip()
        for line in (PROXY_DIR / "squid.conf.template").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    allow = "http_access allow sandbox_clients CONNECT TLS_port allowed_domains"
    assert "http_access deny !sandbox_clients" in lines
    assert "http_access deny !CONNECT" in lines
    assert "http_access deny !TLS_port" in lines
    assert lines.index("http_access deny denied_ipv4") < lines.index(allow)
    assert lines.index("http_access deny denied_ipv6") < lines.index(allow)
    assert lines[-1] == "http_access deny all"


def test_proxy_policy_does_not_decrypt_or_cache_tls() -> None:
    config = (PROXY_DIR / "squid.conf.template").read_text()
    entrypoint = (PROXY_DIR / "docker-entrypoint.sh").read_text()

    assert "ssl_bump" not in config
    assert "cache deny all" in config
    assert "pinger_enable off" in config
    assert "access_log stdio:/dev/stdout" in config
    assert "include /run/squid/upstream.conf" in config
    assert "never_direct allow all" in entrypoint


def test_proxy_healthcheck_probes_the_listener_without_pid_assumptions() -> None:
    dockerfile = (PROXY_DIR / "Dockerfile").read_text()

    assert "PeerPort=>3128" in dockerfile
    assert "-k check" not in dockerfile


def test_proxy_launcher_and_policy_check_do_not_pin_network_addresses() -> None:
    launcher = (ROOT / "scripts" / "run-egress-proxy.sh").read_text()
    policy_check = (ROOT / "scripts" / "check-egress-policy.sh").read_text()

    assert "\n  --ip " not in launcher
    assert '--runtime "${sandbox_runtime}"' in policy_check
    assert "--add-host" not in policy_check


def test_egress_check_delegates_direct_network_denials_to_ip_probe() -> None:
    policy_check = (ROOT / "scripts" / "check-egress-policy.sh").read_text()

    assert "check-agent-network-policy.sh" in policy_check
    assert "--direct-url" not in policy_check


def test_existing_egress_uplink_must_be_a_local_ipv4_bridge(tmp_path: Path) -> None:
    project = tmp_path / "project"
    scripts = project / "scripts"
    fake_bin = tmp_path / "bin"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    shutil.copy2(ROOT / "scripts" / "run-egress-proxy.sh", scripts)
    (project / ".env").write_text("")

    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
case "$*" in
  "network inspect agent-egress") exit 0 ;;
  *"{{.Internal}} agent-egress") printf '%s\\n' true ;;
  *".Subnet"*"agent-egress") printf '%s\\n' 172.30.0.0/24 ;;
  "network inspect codex-egress-uplink") exit 0 ;;
  *"{{.Internal}} codex-egress-uplink") printf '%s\\n' false ;;
  *"{{.Driver}} codex-egress-uplink") printf '%s\\n' overlay ;;
  *"{{.Scope}} codex-egress-uplink") printf '%s\\n' swarm ;;
  *"{{.EnableIPv6}} codex-egress-uplink") printf '%s\\n' false ;;
  *".Gateway"*"codex-egress-uplink") printf '%s\\n' 172.31.0.1 ;;
  "image inspect "*) exit 1 ;;
esac
exit 0
"""
    )
    docker.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = subprocess.run(
        ["/bin/bash", "scripts/run-egress-proxy.sh"],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "local IPv4 bridge" in result.stderr
