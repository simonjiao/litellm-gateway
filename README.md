# LiteLLM Responses Gateway

本项目以 LiteLLM 为统一 Responses Gateway，通过 Responses Adapter 驱动隔离的
Sandbox Worker。

```text
Open WebUI → LiteLLM Gateway → Responses Adapter → Sandbox Worker (runsc)
     └────→ Artifact Service              └──────────→ Sandbox Manager
                  │                                         ├─ Sandbox / Workspace control
                  └─ private object store                    └─ trusted one-shot operations
```

- Gateway、Adapter 与 Sandbox Manager 使用普通 `runc` 容器。
- Open WebUI 使用普通 `runc` 容器，并通过 Responses API 连接 Gateway。
- Gateway 发布允许使用的模型目录，Open WebUI 动态发现并在聊天中选择。
- Gateway、Adapter、Sandbox Manager 与 Sandbox Worker 使用独立运行镜像。
- Sandbox Manager 作为可信执行控制面，管理 Sandbox、Workspace 和受控文件操作；不代理
  Agent 或文件数据面。
- Artifact Service 是私有对象存储前的薄网关，管理不可变 manifest 和短期上传下载；不访问
  Workspace 或运行平台。
- Adapter 直接与 Sandbox Worker 通信。
- 每个 Sandbox Worker 独占 `runsc` 容器、工作区和 Agent Runtime 会话。
- Agent 互联网出站流量只能经过策略代理；容器之间使用 DNS，不固定 IP。

当前仓库已提供 Sandbox/Workspace 生命周期、对话绑定、Open WebUI Files checkout/publish，
以及基于 restic 和私有对象存储的 checkpoint/restore。独立 Artifact Service、外部 MCP 文件
接口、Turn 目录和批次原子操作仍待按设计实现。

## 文档

- [系统设计](docs/design.md)
- [Sandbox Manager](docs/sandbox-manager.md)
- [文件与 Workspace 存储](docs/storage.md)
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

Docker 参考部署启动后，Open WebUI 默认位于 `http://127.0.0.1:3000`；可在聊天输入区
选择 Gateway 发布的模型。首次注册的用户成为管理员。
