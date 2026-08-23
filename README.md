# LiteLLM Codex Responses Gateway

本项目以 LiteLLM Python Proxy 作为统一 Responses Gateway，把 `codex app-server` 作为一个 OpenAI-compatible Responses 后端接入，并由独立 Sandbox Agent Host 承载 Agent Session。

```text
Open WebUI ── Responses HTTP/SSE ──► LiteLLM Gateway
                                         │
                                         ▼
                                  Responses Adapter
                                         │ AgentExecution HTTP/SSE
                                         ▼
                              Sandbox Agent Host (Docker + gVisor)
                                         │
                                         ▼
                              Worker + codex app-server
                                         │ MCP Streamable HTTP
                                         ▼
                               独立部署的 MCP Gateway
```

Open WebUI Backend/BFF 负责用户认证、会话和 MCP Apps 同源代理；LiteLLM、Adapter 与 Agent Host 之间使用部署级 Bearer 凭证。MCP Gateway 的实现不在本仓库范围内。

## 已实现

- LiteLLM `/v1/responses` 的同步与 SSE 流式路由；
- Response 与 Codex Thread/Turn/Item 映射；
- retrieve、cancel、delete、input-items 和 `previous_response_id`；
- Responses SSE 断开后 Agent 继续执行，可通过 retrieve 获取状态；
- 每个 Agent Session 独立 Docker Sandbox，固定使用 gVisor `runsc`、非 root、只读根文件系统、资源限额和受控出站代理；
- MCP `mcp_call` item、MCP App HTML、AppBridge 工具调用和 elicitation 回传；
- `AppSession` 精确绑定 Response、origin call、App、Server、resource URI 与 app-server 返回的 enabled tools；
- LiteLLM、Adapter、Agent Host 和 Worker 的部署级 Bearer 认证。

## 部署

前置条件是 Docker Engine 已安装 `runsc` runtime，并有一个接入内部 Docker 网络的策略出站代理。Codex 认证与 MCP Server 配置由部署方提供，按 `.env` 中路径只读挂载到 Sandbox。

```bash
cp .env.example .env
uv sync --extra dev
bash scripts/build-sandbox-worker.sh
bash scripts/prepare-sandbox-network.sh
```

分别启动三个进程：

```bash
bash scripts/run-agent-host.sh
bash scripts/run-adapter.sh
bash scripts/run-gateway.sh
```

调用 Gateway：

```bash
curl -N http://127.0.0.1:4000/v1/responses \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"model":"codex-app-server","input":"hello","stream":true}'
```

MCP Apps 需要在 Open WebUI 的消息 renderer 中接入 [`frontend/mcp-apps-host`](frontend/mcp-apps-host)，并让 Backend/BFF 将 `/v1/mcp-apps/*` 同源代理到 Adapter。浏览器和 iframe 不直接访问 Adapter、Agent Host 或 MCP Gateway。

## 固定约束

- 不接受客户端传入非空 Responses `tools`；MCP Server、Tool 与 App 由 Codex 配置；
- 不支持 `background=true`、`store=false`、`max_output_tokens`；
- 不支持主 Responses SSE 断线续传或事件重放；
- 仅 MCP App elicitation 可交互，其他 app-server 反向审批全部 fail-closed；
- Response、AppSession、interaction 与 MCP Apps side-event 是 Adapter 单进程内存状态；
- 不包含终端用户、租户管理，也不包含 MCP Gateway 实现。

详细契约见[设计文档](docs/design.md)、[协议映射](docs/protocol-mapping.md)和 [MCP Apps 设计](docs/mcp-apps.md)。

## 验证

```bash
uv run pytest -q
uv run ruff check .
uv run basedpyright
npm --prefix frontend/mcp-apps-host run check
npm --prefix frontend/mcp-apps-host run build
```
