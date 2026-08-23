# MCP Apps 交互设计

## 范围

Responses Gateway 仍是本系统主入口。MCP Gateway 独立部署；本系统只让后端 Agent 使用其 MCP Server/Tool/App，并让 Open WebUI 对已绑定的特定 MCP App 完成 UI 交互。

Open WebUI 需要两部分：Browser 中的通用 MCP Apps Host，以及 Backend/BFF 的认证与同源代理。具体画布或表单由 MCP Server 返回的 `text/html;profile=mcp-app` 资源实现，本项目不把业务 UI 写死在聊天客户端。

## 交互链路

```text
Agent → MCP Gateway tool → mcpToolCall(appContext)
  → Adapter 建立 AppSession 并输出 mcp_call._meta.mcp_app
  → Open WebUI BFF 代理资源
  → Browser sandbox iframe + AppBridge 渲染
  → elicitation 或 App callServerTool
  → BFF → Adapter → 原 Agent Session → MCP Gateway
  → CallToolResult 返回 App，同时原 Turn 可继续完成
```

连续 Turn 中，`mcpServer/elicitation/request` 会挂起 MCP tool。用户提交 `accept/decline/cancel` 后，结果回到同一 app-server 请求，最终 tool result 与 Agent Message 继续通过 Responses 流返回。非流式 Turn 或交互超时按 `cancel` 处理。

App 也可直接调用同一 AppSession 允许的 Server tool；结果立即返回 App，不自动成为新的模型输入。需要 Agent 继续处理时，由 Open WebUI 发起后续 Response。

## AppSession

每个 AppSession 不可变地绑定：

```text
response_id + origin_call_id + app_id + server_id + resource_uri + allowed_tools
```

`allowed_tools` 来自 app-server `app/read(includeTools=true)` 中 `isEnabled=true` 的 tool summary。资源读取和工具调用必须命中全部绑定字段；跨 Response、跨 Server、跨 resource 或未授权 tool 均拒绝。

AppSession 在 Response 终态后保留，以便已渲染 UI 读取结果；删除 Response 或 Adapter 重启时清除。

## 浏览器边界

- iframe 使用 `sandbox`，不启用 `allow-same-origin`；
- MCP UI metadata 经服务端转为 CSP 与 iframe permissions；
- 外部链接、消息、model context 与显示模式由 Open WebUI 回调明确处理；
- 浏览器请求使用 BFF 同源 cookie/session，不携带 Adapter 部署密钥；
- BFF 使用 Adapter Bearer 凭证代理 `_meta.mcp_app` 中的资源、state、events、resolve 与 tools/call URL；
- 浏览器与 iframe 不直接访问 Adapter、Agent Host 或 MCP Gateway。

框架无关的 Host 实现位于 [`frontend/mcp-apps-host`](../frontend/mcp-apps-host)。
