# Responses、Codex app-server 与 MCP Apps 协议映射

本文描述 Codex Sandbox Worker 的实现级协议，不定义上层架构接口。

Adapter 通过 Sandbox Manager 创建、查询、续租和销毁 Sandbox，并直接连接 Sandbox
Worker 驱动 `codex app-server`。生命周期接口不承载 Agent RPC/SSE，也不改变 Responses
映射。

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

| 能力 | LiteLLM | Adapter | 约束 |
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

## 请求映射

LiteLLM 根据公共模型目录完成准入和路由，并将解析后的 Codex 模型放入上游 Responses
`model` 字段。Adapter 不维护公共别名映射或全局模型覆盖；Manager 和 Worker 的生命周期
配置不包含模型或 effort。

| Responses 字段 | app-server | 处理 |
|---|---|---|
| `model` | `thread/start.model`、`thread/fork.model` | 直接使用 Gateway 解析后的 Codex 模型 |
| `input: string` / input_text | `turn/start.input[].type=text` | 映射为当前 Turn 输入 |
| input_image URL | `UserInput.Image` | 透传 URL 与 detail |
| function_call_output | `UserInput.Text` | 带 call ID 的文本输入，不实现函数工具循环 |
| `instructions` | `developerInstructions` | thread start/fork |
| `metadata` | ResponseRecord | 仅保存在 Adapter |
| `reasoning.effort` | `turn/start.effort` | 显式值覆盖 Gateway 目录默认值 |
| `reasoning.summary` | `turn/start.summary` | 透传 |
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
