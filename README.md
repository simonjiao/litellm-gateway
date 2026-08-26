# LiteLLM Responses Gateway

本项目以 LiteLLM 为统一 Responses Gateway，通过 Responses Adapter 驱动隔离的
Sandbox Worker。

```text
Open WebUI → LiteLLM Gateway → Responses Adapter → Sandbox Worker (runsc)
                                      └──────────→ Sandbox Manager
```

- Gateway、Adapter 与 Sandbox Manager 使用普通 `runc` 容器。
- Open WebUI 使用普通 `runc` 容器，并通过 Responses API 连接 Gateway。
- Gateway、Adapter、Sandbox Manager 与 Sandbox Worker 使用独立运行镜像。
- Sandbox Manager 只管理 Sandbox 生命周期。
- Adapter 直接与 Sandbox Worker 通信。
- 每个 Sandbox Worker 独占 `runsc` 容器、工作区和 Agent Runtime 会话。
- Agent 互联网出站流量只能经过策略代理；容器之间使用 DNS，不固定 IP。

## 文档

- [系统设计](docs/design.md)
- [部署设计](docs/deployment.md)
- [Agent 出站策略](docs/egress-proxy.md)
- [协议映射](docs/protocol-mapping.md)
- [MCP Apps](docs/mcp-apps.md)

## 固定约束

- 不接受客户端传入非空 Responses `tools`。
- 不支持 `background=true`、`store=false`、`max_output_tokens`。
- 主 Responses SSE 不提供公开重放；断线后通过 retrieve 获取终态。
- 仅 MCP App elicitation 可交互，其他 Agent Runtime 反向审批 fail closed。
- Adapter 状态为单进程内存状态，不提供多实例恢复。

## 验证

```bash
uv run pytest -q
uv run ruff check .
uv run basedpyright
npm --prefix frontend/mcp-apps-host run check
npm --prefix frontend/mcp-apps-host run build
```

Docker 参考部署启动后，Open WebUI 默认位于 `http://127.0.0.1:3000`，模型为
`codex-app-server`。首次注册的用户成为管理员。
