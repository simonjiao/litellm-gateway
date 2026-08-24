from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DNS_DIR = ROOT / "deploy" / "agent-dns"


def test_agent_dns_only_serves_deployment_supplied_internal_records() -> None:
    config = (DNS_DIR / "dnsmasq.conf.template").read_text()

    assert "no-resolv" in config
    assert "no-hosts" in config
    assert "addn-hosts=/etc/agent-dns/hosts" in config
    assert "server=" not in config


def test_agent_dns_is_internal_and_uses_no_pinned_address() -> None:
    launcher = (ROOT / "scripts" / "run-agent-dns.sh").read_text()
    policy_check = (ROOT / "scripts" / "check-egress-policy.sh").read_text()

    assert '--network "${sandbox_network}"' in launcher
    assert "\n  --ip " not in launcher
    assert ':/etc/agent-dns/hosts:ro"' in launcher
    assert ':/etc/resolv.conf:ro"' in policy_check


def test_old_agent_host_launcher_is_removed() -> None:
    assert not (ROOT / "scripts" / "run-agent-host.sh").exists()
