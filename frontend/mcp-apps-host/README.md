# MCP Apps Host 接入模块

该 TypeScript 模块把 Responses `mcp_call._meta.mcp_app` 渲染为 sandbox iframe，并用 MCP AppBridge 连接 UI。它负责 resource/read、允许的 tools/call、tool input/result、elicitation side-event、显示模式和外部链接回调。

生产环境必须由 Open WebUI Backend/BFF 将描述符中的 `/v1/mcp-apps/*` URL 改写或代理为同源地址。浏览器使用 Open WebUI session cookie；Adapter Bearer 凭证只保存在 BFF。

```ts
import {
  GatewayMcpAppSession,
  GatewayMcpAppView,
  getMcpAppDescriptor,
} from "@example/litellm-codex-mcp-apps-host";

const descriptor = getMcpAppDescriptor(mcpCallItem);
if (descriptor) {
  const iframe = document.createElement("iframe");
  messageContainer.append(iframe);

  const view = new GatewayMcpAppView(iframe, mcpCallItem, {
    callbacks: {
      onOpenLink: async (url) => confirm(`打开链接？\n${url}`),
      onRequestDisplayMode: async (mode) => mode,
    },
  });
  await view.mount();

  const session = new GatewayMcpAppSession(descriptor, {
    onElicitation: async (interaction) => renderInteraction(interaction),
  });
  await session.start();
}

// 收到同一 MCP item 的 response.output_item.done：
view.update(completedMcpCallItem);
```

`GatewayMcpAppView` 会校验 resource URI 与 `allowed_tools`，但服务端 AppSession 校验仍是最终安全边界。真正的画布/表单由 MCP Server 返回的 `text/html;profile=mcp-app` 提供。
