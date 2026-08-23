import {
  AppBridge,
  PostMessageTransport,
  buildAllowAttribute,
} from "@modelcontextprotocol/ext-apps/app-bridge";
import type {
  CallToolResult,
  ReadResourceResult,
} from "@modelcontextprotocol/sdk/types.js";

type McpUiDisplayMode = "inline" | "fullscreen" | "pip";
type McpUiResourcePermissions = NonNullable<
  Parameters<typeof buildAllowAttribute>[0]
>;

export interface McpAppDescriptor {
  response_id: string;
  call_id: string;
  server: string;
  tool: string;
  resource_uri: string;
  resource_url: string;
  events_url: string;
  state_url: string;
  tool_call_url: string;
  connector_id?: string | null;
  link_id?: string | null;
  app_id: string;
  allowed_tools: string[];
}

export interface ResponsesMcpCallItem {
  id: string;
  type: "mcp_call";
  arguments: string;
  name: string;
  server_label: string;
  status?: "in_progress" | "completed" | "incomplete" | "calling" | "failed";
  output?: string | null;
  error?: { message?: string } | null;
  _meta?: {
    mcp_app?: McpAppDescriptor;
    [key: string]: unknown;
  };
}

export interface McpAppInteraction {
  id: string;
  object: "mcp_app.interaction";
  response_id: string;
  method: string;
  params: Record<string, unknown>;
  created_at: number;
  status: "pending" | "resolved" | "expired" | "cancelled";
  result?: unknown;
  resolve_url?: string;
}

export interface ElicitationResolution {
  action: "accept" | "decline" | "cancel";
  content?: unknown;
  _meta?: unknown;
}

export interface McpAppHostCallbacks {
  onMessage?: (message: unknown) => Promise<void> | void;
  onModelContext?: (context: unknown) => Promise<void> | void;
  onOpenLink?: (url: string) => Promise<boolean> | boolean;
  onRequestDisplayMode?: (
    mode: McpUiDisplayMode,
  ) => Promise<McpUiDisplayMode> | McpUiDisplayMode;
  onElicitation?: (
    interaction: McpAppInteraction,
  ) => Promise<ElicitationResolution>;
}

interface ResponseState {
  response_id: string;
  closed: boolean;
  interactions: McpAppInteraction[];
}

interface SideEvent {
  type: string;
  data?: {
    interaction?: McpAppInteraction;
    [key: string]: unknown;
  };
}

export function getMcpAppDescriptor(
  item: ResponsesMcpCallItem,
): McpAppDescriptor | null {
  return item._meta?.mcp_app ?? null;
}

export class GatewayMcpAppClient {
  async readResource(
    descriptor: McpAppDescriptor,
    uri = descriptor.resource_uri,
  ): Promise<ReadResourceResult> {
    if (uri !== descriptor.resource_uri) {
      throw new Error("MCP App resource is outside the bound AppSession");
    }
    const url = toUrl(descriptor.resource_url);
    url.searchParams.set("uri", uri);
    url.searchParams.set("format", "json");
    const response = await fetch(url, { credentials: "same-origin" });
    return readJson<ReadResourceResult>(response);
  }

  async callTool(
    descriptor: McpAppDescriptor,
    name: string,
    args: Record<string, unknown> | undefined,
    meta?: unknown,
  ): Promise<CallToolResult> {
    if (!descriptor.allowed_tools.includes(name)) {
      throw new Error("MCP App tool is outside the bound AppSession");
    }
    const response = await fetch(toUrl(descriptor.tool_call_url), {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        server: descriptor.server,
        tool: name,
        origin_call_id: descriptor.call_id,
        arguments: args ?? {},
        _meta: meta,
      }),
    });
    return readJson<CallToolResult>(response);
  }

  async state(descriptor: McpAppDescriptor): Promise<ResponseState> {
    const response = await fetch(toUrl(descriptor.state_url), {
      credentials: "same-origin",
    });
    return readJson<ResponseState>(response);
  }

  async resolve(
    interaction: McpAppInteraction,
    resolution: ElicitationResolution,
  ): Promise<McpAppInteraction> {
    if (!interaction.resolve_url) {
      throw new Error("MCP App interaction is missing resolve_url");
    }
    const response = await fetch(toUrl(interaction.resolve_url), {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(resolution),
    });
    return readJson<McpAppInteraction>(response);
  }
}

/** Mounts one MCP App resource in a sandboxed iframe. */
export class GatewayMcpAppView {
  readonly descriptor: McpAppDescriptor;

  private readonly client: GatewayMcpAppClient;
  private readonly callbacks: McpAppHostCallbacks;
  private bridge: AppBridge | null = null;

  constructor(
    private readonly iframe: HTMLIFrameElement,
    private item: ResponsesMcpCallItem,
    options: {
      client?: GatewayMcpAppClient;
      callbacks?: McpAppHostCallbacks;
    } = {},
  ) {
    const descriptor = getMcpAppDescriptor(item);
    if (!descriptor) {
      throw new Error("Responses MCP call does not contain _meta.mcp_app");
    }
    this.descriptor = descriptor;
    this.client = options.client ?? new GatewayMcpAppClient();
    this.callbacks = options.callbacks ?? {};
  }

  async mount(): Promise<void> {
    const resource = await this.client.readResource(this.descriptor);
    const selected = resource.contents.find(
      (content) => content.uri === this.descriptor.resource_uri,
    ) ?? resource.contents[0];
    const permissions = readUiPermissions(
      (selected as { _meta?: unknown } | undefined)?._meta,
    );

    this.iframe.setAttribute(
      "sandbox",
      "allow-scripts allow-forms allow-downloads",
    );
    this.iframe.setAttribute("referrerpolicy", "no-referrer");
    const allow = buildAllowAttribute(permissions);
    if (allow) this.iframe.setAttribute("allow", allow);

    const loaded = waitForLoad(this.iframe);
    this.iframe.src = this.descriptor.resource_url;
    await loaded;
    if (!this.iframe.contentWindow) {
      throw new Error("MCP App iframe has no contentWindow");
    }

    const bridge = new AppBridge(
      null,
      { name: "litellm-codex-mcp-apps-host", version: "0.3.0" },
      { openLinks: {}, serverTools: {}, logging: {} },
    );
    this.bridge = bridge;

    bridge.oncalltool = async (params) =>
      this.client.callTool(
        this.descriptor,
        params.name,
        params.arguments as Record<string, unknown> | undefined,
        params._meta,
      );
    bridge.onreadresource = async (params) =>
      this.client.readResource(this.descriptor, params.uri);
    bridge.onlistresources = async () => ({
      resources: [
        {
          uri: this.descriptor.resource_uri,
          name: this.descriptor.tool,
          mimeType: "text/html;profile=mcp-app",
        },
      ],
    });
    bridge.onmessage = async (params) => {
      await this.callbacks.onMessage?.(params);
      return {};
    };
    bridge.onupdatemodelcontext = async (params) => {
      await this.callbacks.onModelContext?.(params);
      return {};
    };
    bridge.onopenlink = async ({ url }) => {
      const allowed = await this.callbacks.onOpenLink?.(url);
      if (allowed !== true) return { isError: true };
      window.open(url, "_blank", "noopener,noreferrer");
      return {};
    };
    bridge.onsizechange = ({ width, height }) => {
      if (width != null) this.iframe.style.width = `${width}px`;
      if (height != null) this.iframe.style.height = `${height}px`;
    };
    bridge.onrequestdisplaymode = async ({ mode }) => {
      const selected = await this.callbacks.onRequestDisplayMode?.(mode);
      return { mode: selected ?? "inline" };
    };
    bridge.oninitialized = () => {
      bridge.sendToolInput({ arguments: parseArguments(this.item.arguments) });
      this.sendTerminalResult(this.item);
    };

    await bridge.connect(
      new PostMessageTransport(
        this.iframe.contentWindow,
        this.iframe.contentWindow,
      ),
    );
  }

  update(item: ResponsesMcpCallItem): void {
    if (item.id !== this.item.id) return;
    this.item = item;
    this.sendTerminalResult(item);
  }

  async dispose(): Promise<void> {
    const bridge = this.bridge;
    this.bridge = null;
    if (bridge) {
      try {
        await bridge.teardownResource({});
      } finally {
        this.iframe.removeAttribute("src");
      }
    }
  }

  private sendTerminalResult(item: ResponsesMcpCallItem): void {
    const bridge = this.bridge;
    if (!bridge) return;
    if (item.status === "failed" || item.status === "incomplete") {
      bridge.sendToolCancelled({
        reason: item.error?.message ?? "MCP tool call failed",
      });
      return;
    }
    if (item.status !== "completed" || !item.output) return;
    bridge.sendToolResult(parseJson<CallToolResult>(item.output));
  }
}

/** Handles response-level elicitation requests while the Responses SSE remains open. */
export class GatewayMcpAppSession {
  private readonly client: GatewayMcpAppClient;
  private readonly handled = new Set<string>();
  private source: EventSource | null = null;

  constructor(
    private readonly descriptor: McpAppDescriptor,
    private readonly callbacks: McpAppHostCallbacks,
    client?: GatewayMcpAppClient,
  ) {
    this.client = client ?? new GatewayMcpAppClient();
  }

  async start(): Promise<void> {
    const state = await this.client.state(this.descriptor);
    for (const interaction of state.interactions) {
      if (interaction.status === "pending") await this.handle(interaction);
    }
    if (state.closed) return;

    const source = new EventSource(this.descriptor.events_url);
    source.addEventListener("mcp_app.elicitation.requested", (event) => {
      const payload = parseJson<SideEvent>((event as MessageEvent).data);
      const interaction = payload.data?.interaction;
      if (interaction) void this.handle(interaction);
    });
    source.addEventListener("mcp_app.response.closed", () => this.stop());
    this.source = source;
  }

  stop(): void {
    this.source?.close();
    this.source = null;
  }

  private async handle(interaction: McpAppInteraction): Promise<void> {
    if (this.handled.has(interaction.id)) return;
    this.handled.add(interaction.id);
    const callback = this.callbacks.onElicitation;
    const resolution = callback
      ? await callback(interaction)
      : { action: "cancel" as const };
    await this.client.resolve(interaction, resolution);
  }
}

function toUrl(value: string): URL {
  return new URL(value, window.location.href);
}

async function readJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as unknown;
  if (!response.ok) {
    throw new Error(`MCP Apps gateway request failed (${response.status})`);
  }
  return body as T;
}

function parseArguments(value: string): Record<string, unknown> {
  const parsed = parseJson<unknown>(value);
  return isRecord(parsed) ? parsed : {};
}

function parseJson<T>(value: string): T {
  return JSON.parse(value) as T;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readUiPermissions(
  meta: unknown,
): McpUiResourcePermissions | undefined {
  if (!isRecord(meta) || !isRecord(meta.ui) || !isRecord(meta.ui.permissions)) {
    return undefined;
  }
  return meta.ui.permissions as McpUiResourcePermissions;
}

function waitForLoad(iframe: HTMLIFrameElement): Promise<void> {
  return new Promise((resolve, reject) => {
    const onLoad = () => {
      cleanup();
      resolve();
    };
    const onError = () => {
      cleanup();
      reject(new Error("Failed to load MCP App resource"));
    };
    const cleanup = () => {
      iframe.removeEventListener("load", onLoad);
      iframe.removeEventListener("error", onError);
    };
    iframe.addEventListener("load", onLoad, { once: true });
    iframe.addEventListener("error", onError, { once: true });
  });
}
