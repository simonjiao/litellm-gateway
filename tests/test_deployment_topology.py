import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_deploys_services_as_separate_runc_images() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    services = compose["services"]

    assert compose["name"] == "agent"
    assert set(services) == {
        "open-webui",
        "artifact-service",
        "gateway",
        "adapter",
        "sandbox-manager",
    }
    assert compose["networks"]["control"]["name"] == "${CONTROL_NETWORK:-agent-control}"
    gateway = services["gateway"]
    adapter = services["adapter"]
    manager = services["sandbox-manager"]
    webui = services["open-webui"]
    artifact = services["artifact-service"]

    common_runtime = compose["x-common-runtime"]
    assert not {"build", "image", "command", "restart"} & common_runtime.keys()
    assert gateway["build"]["dockerfile"] == "deploy/gateway/Dockerfile"
    assert adapter["build"]["dockerfile"] == "deploy/responses-adapter/Dockerfile"
    assert manager["build"]["dockerfile"] == "deploy/sandbox-manager/Dockerfile"
    assert webui["build"]["dockerfile"] == "deploy/open-webui/Dockerfile"
    assert artifact["build"]["dockerfile"] == "deploy/artifact-service/Dockerfile"
    assert gateway["image"] == "${AGENT_GATEWAY_IMAGE:-agent-gateway:0.3.0}"
    assert adapter["image"] == "${AGENT_ADAPTER_IMAGE:-agent-adapter:0.3.0}"
    assert manager["image"] == "${AGENT_SANDBOX_MANAGER_IMAGE:-agent-sandbox-manager:0.3.0}"
    assert artifact["image"] == (
        "${AGENT_ARTIFACT_SERVICE_IMAGE:-agent-artifact-service:0.3.0}"
    )

    assert gateway["runtime"] == "runc"
    assert adapter["runtime"] == "runc"
    assert manager["runtime"] == "runc"
    assert artifact["runtime"] == "runc"
    assert manager["restart"] == "unless-stopped"
    assert set(gateway["networks"]) == {"control"}
    assert set(adapter["networks"]) == {"control", "agent-rpc"}
    assert set(manager["networks"]) == {"control", "storage"}
    assert set(artifact["networks"]) == {"control", "storage"}
    assert "ports" in gateway
    assert "ports" not in adapter
    assert "ports" not in manager
    assert manager["environment"]["DOCKER_HOST"] == "unix:///run/sandbox-engine/docker.sock"
    assert manager["environment"]["SANDBOX_MANAGER_STATE_DB_PATH"] == (
        "/var/lib/sandbox-manager/state.db"
    )
    assert manager["environment"]["SANDBOX_IMAGE"] == (
        "${SANDBOX_IMAGE:-codex-sandbox-worker:0.3.0}"
    )
    assert any(
        "SANDBOX_MANAGER_DOCKER_SOCKET" in volume
        and volume.endswith(":/run/sandbox-engine/docker.sock")
        for volume in manager["volumes"]
    )
    assert "sandbox-manager-state:/var/lib/sandbox-manager" in manager["volumes"]
    assert compose["volumes"]["sandbox-manager-state"]["name"] == (
        "${SANDBOX_MANAGER_STATE_VOLUME:-sandbox-manager-state}"
    )
    assert compose["networks"]["storage"]["name"] == (
        "${SANDBOX_MANAGER_STORAGE_NETWORK:-agent-storage}"
    )


def test_compose_deploys_open_webui_as_the_responses_client() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    webui = compose["services"]["open-webui"]

    assert webui["image"] == "${AGENT_OPEN_WEBUI_IMAGE:-agent-open-webui:0.3.0}"
    assert webui["runtime"] == "runc"
    assert webui["restart"] == "no"
    assert set(webui["networks"]) == {"control", "storage"}
    assert webui["ports"] == ["${OPEN_WEBUI_PORT:-3000}:8080"]
    assert webui["environment"]["OPENAI_API_BASE_URL"] == "http://gateway:4000/v1"
    assert webui["environment"]["OPENAI_API_KEY"] == "${LITELLM_MASTER_KEY}"
    assert json.loads(webui["environment"]["OPENAI_API_CONFIGS"]) == {
        "0": {"api_type": "responses"}
    }
    assert webui["environment"]["DEFAULT_MODELS"] == "codex-terra"
    assert webui["environment"]["ENABLE_OLLAMA_API"] == "false"
    assert webui["environment"]["ENABLE_SIGNUP"] == "false"
    assert webui["environment"]["ENABLE_RESPONSES_API_STATEFUL"] == "true"
    assert webui["environment"]["DEFAULT_MODEL_METADATA"] == (
        '{"capabilities":{"builtin_tools":false}}'
    )
    assert webui["environment"]["WEBUI_SECRET_KEY"] == (
        "${OPEN_WEBUI_SECRET_KEY:?set OPEN_WEBUI_SECRET_KEY}"
    )
    assert webui["environment"]["AGENT_ADAPTER_BASE_URL"] == "http://adapter:8090"
    assert webui["environment"]["AGENT_ARTIFACT_BASE_URL"] == (
        "http://artifact-service:8093"
    )
    assert webui["environment"]["S3_ADDRESSING_STYLE"] == "path"
    assert webui["depends_on"]["gateway"]["condition"] == "service_healthy"
    assert webui["volumes"] == ["open-webui-data:/app/backend/data"]
    assert compose["volumes"]["open-webui-data"]["name"] == (
        "${OPEN_WEBUI_DATA_VOLUME:-open-webui-data}"
    )


def test_gateway_publishes_the_selectable_codex_model_catalog() -> None:
    config = yaml.safe_load((ROOT / "config" / "litellm.yaml").read_text())
    models = config["model_list"]

    assert [entry["model_name"] for entry in models] == [
        "codex-sol",
        "codex-terra",
        "codex-luna",
    ]
    assert {
        entry["model_name"]: entry["litellm_params"]["model"] for entry in models
    } == {
        "codex-sol": "openai/gpt-5.6-sol",
        "codex-terra": "openai/gpt-5.6-terra",
        "codex-luna": "openai/gpt-5.6-luna",
    }
    for entry in models:
        params = entry["litellm_params"]
        assert params["api_base"] == "os.environ/CODEX_ADAPTER_BASE_URL"
        assert params["api_key"] == "os.environ/CODEX_ADAPTER_API_KEY"


def test_runtime_images_copy_only_their_required_components() -> None:
    gateway = (ROOT / "deploy" / "gateway" / "Dockerfile").read_text()
    adapter = (ROOT / "deploy" / "responses-adapter" / "Dockerfile").read_text()
    manager = (ROOT / "deploy" / "sandbox-manager" / "Dockerfile").read_text()
    worker = (ROOT / "deploy" / "sandbox-worker" / "Dockerfile").read_text()
    storage_ops = (ROOT / "deploy" / "storage-ops" / "Dockerfile").read_text()
    webui = (ROOT / "deploy" / "open-webui" / "Dockerfile").read_text()

    assert "COPY src" not in gateway
    assert "COPY src/codex_responses_adapter" in adapter
    assert "src/sandbox_manager ./src/sandbox_manager" in manager
    assert "COPY src/sandbox_worker ./src/sandbox_worker" in worker
    assert "COPY src/storage_ops ./src/storage_ops" in storage_ops
    assert "FROM ghcr.io/open-webui/open-webui:v0.11.1" in webui
    assert all("COPY src ./src" not in dockerfile for dockerfile in (adapter, manager, worker))


def test_entry_workload_restart_is_gated_by_host_network_policy() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())

    assert compose["services"]["gateway"]["restart"] == "no"
    assert compose["services"]["adapter"]["restart"] == "no"


def test_compose_uses_dns_and_contains_no_fixed_network_address() -> None:
    text = (ROOT / "compose.yaml").read_text()

    assert "http://adapter:8090/v1" in text
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
    assert "build-storage-ops.sh" in launcher


def test_open_webui_workspace_bridge_is_a_pinned_thin_patch() -> None:
    dockerfile = (ROOT / "deploy" / "open-webui" / "Dockerfile").read_text()
    patcher = (ROOT / "deploy" / "open-webui" / "apply_patch.py").read_text()

    assert "FROM ghcr.io/open-webui/open-webui:v0.11.1" in dockerfile
    assert "Open WebUI v0.11.1 patch anchor changed" in patcher
    assert "inject_workspace_context" in patcher
    assert "release_chat_workspace" in patcher


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
    storage_ops = (ROOT / "deploy" / "storage-ops" / "Dockerfile").read_text()

    assert "PYPI_INDEX_URL" in compose
    for dockerfile in (gateway, adapter, manager, worker, storage_ops):
        assert "ARG PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple" in dockerfile
        assert 'PIP_INDEX_URL="${PYPI_INDEX_URL}"' in dockerfile
    assert "ARG LITELLM_INDEX_URL=https://pypi.org/simple" in gateway
    assert 'PIP_INDEX_URL="${LITELLM_INDEX_URL}"' in gateway
    assert 'pip install --no-deps "litellm==${LITELLM_VERSION}"' in gateway
