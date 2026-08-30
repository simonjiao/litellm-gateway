# Responses、Codex app-server 与 MCP Apps 协议映射

本文描述 Codex Sandbox Worker 的实现级协议，不定义上层架构接口。

Adapter 通过 Sandbox Manager 创建、查询、续租和销毁 Sandbox，并直接连接 Sandbox
Worker 驱动 `codex app-server`。生命周期接口不承载 Agent RPC/SSE，也不改变 Responses
映射。Workspace 和文件操作使用独立的 Manager 控制接口，不扩展 Responses 协议，见
[Sandbox Manager 设计](sandbox-manager.md)。

## Sandbox 执行策略

Worker 启动 Codex 会话时使用：

```json
{
  "approvalPolicy": "never",
  "sandboxPolicy": {
    "type": "externalSandbox",
    "networkAccess": "restricted"
  }
}
```

外层 `runsc` 和部署环境的网络策略负责强制隔离，Codex 不创建内层 Linux Sandbox。

## Responses 能力表

| 能力 | LiteLLM | 本版端到端 | 约束 |
|---|---|---|---|
| `POST /v1/responses`, `stream=false` | 路由/转换 | 支持 | 等待 Codex Turn 终态 |
| `stream=true` Responses SSE | 路由/转换 | 支持 | 文本、message、`mcp_call` 事件子集 |
| 输入 | Provider 相关 | 部分支持 | string、message/input_text、图片 URL；不解析 OpenAI file ID |
| 输出 | Provider 相关 | 部分支持 | assistant message、output text、`mcp_call` |
| 状态 | Provider 相关 | 部分支持 | `in_progress/completed/failed/incomplete` |
| retrieve/cancel/delete/input-items | 有端点 | 支持 | LiteLLM 编码 Response ID 做后端亲和；Adapter 状态仅进程内 |
| `previous_response_id` | 编码并路由 | 支持 | 同一 Sandbox 内 `thread/fork`；Sandbox 过期时返回 `sandbox_unavailable` |
| 客户端 `tools` | 可支持 | 不支持 | 非空显式拒绝；Codex 已配置 MCP 仍输出 `mcp_call` |
| `background=true` | 可原生或 Redis polling | 不支持 | Gateway polling 显式关闭，Adapter 拒绝 |
| `store=false` | Provider 相关 | 不支持 | Adapter 需要 Response/Thread/AppSession 映射 |
| `max_output_tokens` | 可透传 | 不支持 | app-server 无可靠执行约束，显式拒绝 |
| 主流断线续传 | Provider 相关 | 不支持 | SSE 断开不取消 Agent；retrieve 当前/最终状态 |
| conversation/compact/WebSocket | 部分支持 | 不支持 | 连续对话仅使用 `previous_response_id` |
| 持久化/多实例恢复 | 可配置 | 不支持 | Adapter 状态为单进程内存 |

MCP Apps 的资源、interaction 和 side-event 是本项目扩展；`mcp_call` item 与相关 Responses 事件保持标准形态。

## 文件边界

Responses 输入不解析 OpenAI file ID。`artifact_id` 的 checkout 和 Workspace 生成物 publish
不属于 Responses 字段映射：Open WebUI/BFF 完成业务 ACL 后，通过独立、单次授权请求 Manager
执行。Worker 只看到 Workspace 路径，`artifact_id` 本身不构成授权。上传、下载与发布流程见
[文件与 Workspace 存储](storage.md)。

当前消息附件按 `user_message_id` 批量 checkout 到 `uploads/<user_message_id>`；可发布文件只来自
`outputs/<assistant_message_id>`。BFF 验证两条消息属于同一对话链并绑定目标 Response，Adapter
必须等待 checkout 成功后才启动 Codex Turn。终态 Response 中的 `sandbox:` URI 只是候选文件；
BFF 收到终态事件后创建 publish intent，同一 Workspace 的下一 Turn 等待候选捕获完成。该流程
使用现有 Response 文本和 Manager 控制接口，发布与重试语义由存储设计定义。

Codex app-server 在 `turn/start` 后生成自己的 `turn.id`，Adapter 只用它匹配通知、取消和终态。
该 ID 不进入 Workspace 路径或文件授权；其他 Agent Runtime 可以使用各自的执行 ID，而不改变
文件接口。

同源 BFF 在 Responses metadata 中注入两个保留字段：`agent_workspace_grant` 授权 Workspace
创建或恢复，`agent_checkout_grants` 携带当前消息的 checkout 授权列表；有 checkout 时前者必需。
Adapter 在建立 `ResponseRecord` 前移除并消费两者，只将授权转交 Manager，不在响应、日志或
事件中返回。没有 Workspace 授权的新 Sandbox 使用临时 Workspace；签名无效、过期或绑定不匹配
时在创建 Sandbox 前拒绝。`previous_response_id` 必须继续绑定原 Workspace，不能通过新授权切换。

## 请求映射

| Responses 字段 | app-server | 处理 |
|---|---|---|
| `model` | `thread/start.model`、`thread/fork.model` | 直接使用 Gateway 解析后的 Codex 模型 |
| `input: string` / input_text | `turn/start.input[].type=text` | 映射为当前 Turn 输入 |
| input_image URL | `UserInput.Image` | 透传 URL 与 detail |
| function_call_output | `UserInput.Text` | 带 call ID 的文本输入，不实现函数工具循环 |
| `instructions` | `developerInstructions` | thread start/fork |
| `metadata` | ResponseRecord | 保存公开字段；移除并消费 `agent_workspace_grant`、`agent_checkout_grants` |
| `reasoning.effort/summary` | turn start | 透传 |
| `service_tier` | Thread/Turn `serviceTier` | 透传 |
| `previous_response_id` | `thread/fork` | 使用前一 Thread/Turn 与同一 Agent Session |
| `stream` | Responses SSE | app-server notification 转事件 |
| 非空 `tools` | — | 拒绝 |
| `background=true`, `store=false`, `max_output_tokens` | — | 拒绝 |
| 其他非空未知字段 | — | 拒绝，不静默忽略 |

## 事件与 item 映射

| app-server 通知/item | Responses 输出 |
|---|---|
| `item/agentMessage/delta` | `response.output_text.delta` |
| completed agentMessage | content/output-item done |
| started `mcpToolCall` | `response.output_item.added` + `response.mcp_call.in_progress` |
| MCP arguments | `response.mcp_call_arguments.done` |
| completed/failed MCP call | 对应 `response.mcp_call.*` + output-item done |
| `turn/completed` | `response.completed/incomplete/failed` |

`mcp_call.output` 保存完整 MCP `CallToolResult` JSON，包括 `content`、`structuredContent`、`isError` 与 `_meta`。

## MCP Apps 协议

Worker 在 app-server `initialize` 中声明：

```json
{
  "capabilities": {
    "experimentalApi": true,
    "requestAttestation": false,
    "extensions": {
      "openai/form": {},
      "io.modelcontextprotocol/ui": {
        "mimeTypes": ["text/html;profile=mcp-app"]
      }
    }
  }
}
```

AppSession 建立流程：

```text
mcpToolCall.appContext
  → app/read(appIds=[app_id], threadId, includeTools=true)
  → enabled toolSummaries
  → bind(response, origin call, app, server, resource, allowed tools)
```

资源和 App 工具调用均复用原 Sandbox 与 Thread：

```text
Open WebUI AppBridge
  → BFF
  → Adapter resource/read | tools/call
  → Sandbox Worker RPC
  → app-server mcpServer/resource/read | mcpServer/tool/call
  → MCP Gateway
```

Adapter 必须精确校验 Response、origin call、App/connector、Server、resource URI 与 allowed tool。客户端不能扩展作用域。

Elicitation 流程：

```text
app-server mcpServer/elicitation/request
  → Sandbox Worker
  → Adapter side-event
  → Open WebUI UI
  → resolve {action, content, _meta}
  → 原 MCP tool/Turn 继续
```

支持 `accept/decline/cancel`；超时或非流式请求返回 `cancel`。其他 app-server 反向交互请求由 Worker 立即拒绝。

主 Responses 事件只有单调 `sequence_number`，不提供重放。MCP Apps side-event 单独保留有限进程内历史，支持 `Last-Event-ID/after`，两者不能混用。
