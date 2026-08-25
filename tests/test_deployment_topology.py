from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_deploys_services_as_separate_runc_images() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    services = compose["services"]

    gateway = services["gateway"]
    adapter = services["responses-adapter"]
    manager = services["sandbox-manager"]

    common_runtime = compose["x-common-runtime"]
    assert not {"build", "image", "command", "restart"} & common_runtime.keys()
    assert gateway["build"]["dockerfile"] == "deploy/gateway/Dockerfile"
    assert adapter["build"]["dockerfile"] == "deploy/responses-adapter/Dockerfile"
    assert manager["build"]["dockerfile"] == "deploy/sandbox-manager/Dockerfile"
    assert gateway["image"] == "${AGENT_GATEWAY_IMAGE:-agent-gateway:0.3.0}"
    assert adapter["image"] == (
        "${AGENT_RESPONSES_ADAPTER_IMAGE:-agent-responses-adapter:0.3.0}"
    )
    assert manager["image"] == "${AGENT_SANDBOX_MANAGER_IMAGE:-agent-sandbox-manager:0.3.0}"

    assert gateway["runtime"] == "runc"
    assert adapter["runtime"] == "runc"
    assert manager["runtime"] == "runc"
    assert manager["restart"] == "unless-stopped"
    assert set(gateway["networks"]) == {"control"}
    assert set(adapter["networks"]) == {"control", "agent-rpc"}
    assert set(manager["networks"]) == {"control"}
    assert "ports" in gateway
    assert "ports" not in adapter
    assert "ports" not in manager
    assert manager["environment"]["DOCKER_HOST"] == "unix:///run/sandbox-engine/docker.sock"
    assert manager["environment"]["SANDBOX_IMAGE"] == (
        "${SANDBOX_IMAGE:-codex-sandbox-worker:0.3.0}"
    )
    assert any(
        "SANDBOX_MANAGER_DOCKER_SOCKET" in volume
        and volume.endswith(":/run/sandbox-engine/docker.sock")
        for volume in manager["volumes"]
    )


def test_runtime_images_copy_only_their_required_components() -> None:
    gateway = (ROOT / "deploy" / "gateway" / "Dockerfile").read_text()
    adapter = (ROOT / "deploy" / "responses-adapter" / "Dockerfile").read_text()
    manager = (ROOT / "deploy" / "sandbox-manager" / "Dockerfile").read_text()
    worker = (ROOT / "deploy" / "sandbox-worker" / "Dockerfile").read_text()

    assert "COPY src" not in gateway
    assert "COPY src/codex_responses_adapter" in adapter
    assert "COPY src/sandbox_manager ./src/sandbox_manager" in manager
    assert "COPY src/sandbox_worker ./src/sandbox_worker" in worker
    assert all("COPY src ./src" not in dockerfile for dockerfile in (adapter, manager, worker))


def test_entry_workload_restart_is_gated_by_host_network_policy() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())

    assert compose["services"]["gateway"]["restart"] == "no"
    assert compose["services"]["responses-adapter"]["restart"] == "no"


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


def test_worker_port_is_shared_by_manager_policy_and_healthcheck() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    manager_environment = compose["services"]["sandbox-manager"]["environment"]
    dockerfile = (ROOT / "deploy" / "sandbox-worker" / "Dockerfile").read_text()

    assert "SANDBOX_MANAGER_WORKER_PORT" in manager_environment
    assert "SANDBOX_WORKER_PORT" in dockerfile


def test_network_policy_has_a_non_sudo_runc_executor() -> None:
    policy = (ROOT / "scripts" / "lib" / "network-policy.sh").read_text()

    assert "--runtime runc" in policy
    assert "--network host" in policy
    assert "--cap-add NET_ADMIN" in policy


def test_network_component_images_use_agent_names() -> None:
    env_example = (ROOT / ".env.example").read_text()
    contracts = {
        "AGENT_EGRESS_PROXY_IMAGE": (
            "agent-egress-proxy:0.1.0",
            ("build-egress-proxy.sh", "run-egress-proxy.sh"),
        ),
        "AGENT_DNS_IMAGE": (
            "agent-dns:0.1.0",
            ("build-agent-dns.sh", "run-agent-dns.sh"),
        ),
        "AGENT_NETWORK_POLICY_IMAGE": (
            "agent-network-policy:0.1.0",
            (
                "build-network-policy.sh",
                "apply-agent-egress-policy.sh",
                "apply-agent-rpc-policy.sh",
            ),
        ),
    }

    for variable, (image, script_names) in contracts.items():
        assert f"{variable}={image}" in env_example
        for script_name in script_names:
            script = (ROOT / "scripts" / script_name).read_text()
            assert f"${{{variable}:-{image}}}" in script


def test_runtime_images_use_domestic_registry_except_for_the_missing_litellm_rc() -> None:
    compose = (ROOT / "compose.yaml").read_text()
    gateway = (ROOT / "deploy" / "gateway" / "Dockerfile").read_text()
    adapter = (ROOT / "deploy" / "responses-adapter" / "Dockerfile").read_text()
    manager = (ROOT / "deploy" / "sandbox-manager" / "Dockerfile").read_text()
    worker = (ROOT / "deploy" / "sandbox-worker" / "Dockerfile").read_text()

    assert "PYPI_INDEX_URL" in compose
    for dockerfile in (gateway, adapter, manager, worker):
        assert "ARG PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple" in dockerfile
        assert 'PIP_INDEX_URL="${PYPI_INDEX_URL}"' in dockerfile
    assert "ARG LITELLM_INDEX_URL=https://pypi.org/simple" in gateway
    assert 'PIP_INDEX_URL="${LITELLM_INDEX_URL}"' in gateway
    assert 'pip install --no-deps "litellm==${LITELLM_VERSION}"' in gateway
