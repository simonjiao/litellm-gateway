from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_deploys_control_plane_as_separate_runc_services() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    services = compose["services"]

    gateway = services["gateway"]
    adapter = services["responses-adapter"]
    manager = services["sandbox-manager"]

    assert gateway["runtime"] == "runc"
    assert adapter["runtime"] == "runc"
    assert manager["runtime"] == "runc"
    assert set(gateway["networks"]) == {"control"}
    assert set(adapter["networks"]) == {"control", "agent-rpc"}
    assert set(manager["networks"]) == {"control"}
    assert "ports" in gateway
    assert "ports" not in adapter
    assert "ports" not in manager
    assert "/var/run/docker.sock:/var/run/docker.sock" in manager["volumes"]


def test_compose_uses_dns_and_contains_no_fixed_network_address() -> None:
    text = (ROOT / "compose.yaml").read_text()

    assert "http://responses-adapter:8090/v1" in text
    assert "http://sandbox-manager:8092" in text
    assert "ipv4_address" not in text
    assert "subnet:" not in text


def test_agent_rpc_policy_is_directional_and_uses_runtime_discovery() -> None:
    policy = (ROOT / "scripts" / "apply-agent-rpc-policy.sh").read_text()

    assert "docker container inspect" in policy
    assert "--ctstate ESTABLISHED,RELATED" in policy
    assert "--ctstate NEW" in policy
    assert "-j DROP" in policy
    assert "SANDBOX_MANAGER_RPC_NETWORK" in policy
    assert "ipv4_address" not in policy


def test_stack_launcher_applies_both_agent_network_policies() -> None:
    launcher = (ROOT / "scripts" / "run-stack.sh").read_text()

    assert "prepare-sandbox-network.sh" in launcher
    assert "run-egress-proxy.sh" in launcher
    assert "run-agent-dns.sh" in launcher
    assert "docker compose up" in launcher
    assert "apply-agent-rpc-policy.sh" in launcher
    assert "apply-agent-egress-policy.sh" in launcher


def test_agent_egress_policy_allows_only_declared_destinations() -> None:
    policy = (ROOT / "scripts" / "apply-agent-egress-policy.sh").read_text()

    assert "SANDBOX_AGENT_INTERNAL_SERVICES" in policy
    assert "docker container inspect" in policy
    assert "--dport 53" in policy
    assert "--dport 3128" in policy
    assert "--ctstate NEW" in policy
    assert "-j DROP" in policy
    assert "ipv4_address" not in policy


def test_worker_image_uses_the_persisted_agent_home() -> None:
    dockerfile = (ROOT / "deploy" / "sandbox-worker" / "Dockerfile").read_text()

    assert "HOME=/home/agent" in dockerfile


def test_network_policy_has_a_non_sudo_runc_executor() -> None:
    policy = (ROOT / "scripts" / "lib" / "network-policy.sh").read_text()

    assert "--runtime runc" in policy
    assert "--network host" in policy
    assert "--cap-add NET_ADMIN" in policy


def test_control_plane_uses_domestic_registry_except_for_the_missing_litellm_rc() -> None:
    compose = (ROOT / "compose.yaml").read_text()
    dockerfile = (ROOT / "deploy" / "control-plane" / "Dockerfile").read_text()

    assert "CONTROL_PLANE_PYPI_INDEX_URL" in compose
    assert "ARG PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple" in dockerfile
    assert "ARG LITELLM_INDEX_URL=https://pypi.org/simple" in dockerfile
    assert "--mount=type=cache,target=/root/.cache/pip" in dockerfile
    assert 'PIP_INDEX_URL="${LITELLM_INDEX_URL}"' in dockerfile
    assert 'pip install --no-deps "litellm==${LITELLM_VERSION}"' in dockerfile
    assert 'PIP_INDEX_URL="${PYPI_INDEX_URL}"' in dockerfile
    assert "pip install ." in dockerfile
