# Responses Gateway 设计

## 定位

系统以 **LiteLLM Python Proxy** 为 Responses Gateway，复用其 Responses 路由、部署认证和通用治理能力。Codex Responses Adapter 只负责 Responses 与 `codex app-server` 的协议映射，不实现第二套通用 Gateway Runtime。

Agent 执行与 Adapter 生命周期分离：独立 **Sandbox Agent Host** 使用 Docker Engine + gVisor `runsc` 管理 Agent Session。MCP Server、Tool 与 App 由独立部署的 **MCP Gateway** 提供，其内部设计不属于本系统。

## 架构

```text
┌─────────────────────────────────────────────────────────────┐
│ Open WebUI                                                  │
│ Browser: Chat UI + MCP Apps Host (iframe/AppBridge)         │
│ Backend/BFF: 用户认证、会话、访问控制、MCP Apps 同源代理     │
└────────────────────────────┬────────────────────────────────┘
                             │ Responses HTTP/SSE + 部署级凭证
                             ▼
┌─────────────────────────────────────────────────────────────┐
│ LiteLLM Responses Gateway                                   │
│ Responses API / Model Routing / Policy / Observability      │
└────────────────────────────┬────────────────────────────────┘
                             │ Responses-compatible HTTP/SSE
                             ▼
┌─────────────────────────────────────────────────────────────┐
│ Codex Responses Adapter                                     │
│ Responses 映射 / 进程内状态 / MCP Apps Session Interface    │
└────────────────────────────┬────────────────────────────────┘
                             │ AgentExecution HTTP/SSE
                             ▼
┌─────────────────────────────────────────────────────────────┐
│ Sandbox Agent Host                                          │
│ Docker Engine + gVisor runsc / start / inspect / event / TTL│
│ └─ Agent Session: Worker + codex app-server + Workspace     │
└────────────────────────────┬────────────────────────────────┘
                             │ MCP Streamable HTTP
                             ▼
┌─────────────────────────────────────────────────────────────┐
│ MCP Gateway（独立部署）：MCP Servers / Tools / Apps          │
└─────────────────────────────────────────────────────────────┘

Open WebUI BFF ── MCP Apps Session Interface ──► Adapter
```

Responses HTTP/SSE 是客户端 API；MCP Streamable HTTP 是 Agent 与 MCP Gateway 的后端协议，两者不是同一种传输语义。

## 职责与生命周期

| 模块 | 职责 |
|---|---|
| Open WebUI | 终端用户认证、会话、授权与通用 MCP Apps Host |
| LiteLLM | Responses 入口、模型路由、部署认证和 Gateway 治理 |
| Adapter | Response↔Thread/Turn/Item 映射及 MCP Apps 会话绑定 |
| Agent Host | AgentExecution 生命周期、内部事件恢复、Sandbox TTL |
| Docker + gVisor | 进程、文件、资源与网络隔离 |
| codex app-server | Agent Runtime 与 MCP Client |
| MCP Gateway | 独立提供和治理 MCP Server、Tool 与 App |

一次新 Response 创建一个 Agent Session Sandbox；`previous_response_id` 在前一 Response 终态后复用同一 Sandbox，并 fork 对应 Thread。Adapter 的执行驱动独立消费 Agent Host 事件，客户端 SSE 只是订阅者，断开不会取消 Agent。取消先发送 `turn/interrupt`，失败时终止 Sandbox；空闲 Sandbox 由 Agent Host 按 TTL 回收。

Agent Host 保留有限的内部事件历史，只用于 Adapter 与 Host 之间短暂断线恢复。它不构成主 Responses SSE 的公开重放能力。

## MCP Apps

Codex MCP tool item 带有 App 上下文时，Adapter 生成标准 `mcp_call` 及 `_meta.mcp_app`，并建立不可变作用域：

```text
response_id + origin_call_id + app_id + server_id + resource_uri + allowed_tools
```

`allowed_tools` 由同一 Codex 会话的 `app/read(includeTools=true)` 返回的 enabled tools 生成，客户端不能提供。Open WebUI 在 sandbox iframe 中运行 AppBridge；所有资源、工具与 elicitation 请求经 BFF 到 Adapter，Adapter 校验 AppSession 后才发送给原 Agent Session。

## 信任与状态

- Open WebUI 管理终端用户与租户；本系统只使用节点内或私网部署级 Bearer 凭证。
- 浏览器与 iframe 不直连 LiteLLM、Adapter、Agent Host 或 MCP Gateway。
- Docker API 只对 Agent Host 开放；Sandbox 固定为非 privileged、cap-drop all、非 root、只读根文件系统、资源限额、内部网络和策略代理出站。
- Codex Thread 固定使用 `/workspace`、`workspace-write` 与 `approvalPolicy=never`；Worker 只转发 MCP App elicitation，其余交互式请求立即拒绝。
- Response、AppSession、interaction 与 MCP Apps side-event 是 Adapter 单进程内存状态。Adapter 重启后不恢复；Docker 中遗留执行由 Agent Host 接管并最终按 TTL 清理。

本版不依赖 Redis Stream：不支持 `background=true`、公开主流重放、多实例状态恢复或 `store=false`，因此没有需要 Redis 承担的协议语义。LiteLLM 的 Responses polling 被显式关闭；若未来引入上述能力，应先定义持久化状态机，再选择共享事件与存储设施。

## 固定协议边界

- 客户端非空 `tools`：拒绝；
- `background=true`、`store=false`、`max_output_tokens`：拒绝；
- 主 Responses SSE 断线续传：不支持，仅允许 retrieve；
- MCP App elicitation：支持；其他 Shell、文件、Sandbox、登录或用户输入审批：fail-closed；
- 终端用户认证、用户/租户管理、MCP Gateway 内部实现：不包含。
