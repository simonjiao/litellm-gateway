# Responses Gateway 设计

## 定位

LiteLLM 提供 Responses 入口、模型路由、认证和通用治理。Responses Adapter 负责
Responses 协议与 Sandbox Worker 接口之间的转换。Sandbox Manager 只负责 Sandbox
生命周期，不代理 Agent 数据流。具体 Agent Runtime 的协议适配属于实现级约定，不进入
本设计的架构接口。

## 架构

```text
Open WebUI / BFF
        │ Responses HTTP/SSE
        ▼
LiteLLM Gateway
        ▼
Responses Adapter ─── lifecycle / lease ───→ Sandbox Manager
        │                                         │
        │ Worker RPC/SSE                          │ workload control
        ▼                                         ▼
Sandbox Worker (runsc) ←────────────── Sandbox Runtime
        │
        ├── policy egress ──→ Internet
        └── approved internal interfaces ──→ MCP / local model
```

## 职责

| 模块 | 职责 |
|---|---|
| Open WebUI | 用户认证、会话、访问控制；发现并选择 Gateway 发布的模型 |
| LiteLLM | Responses 入口、模型目录与路由、部署认证和治理 |
| Adapter | Responses 请求与事件映射、Worker RPC/SSE、MCP Apps 会话绑定 |
| Sandbox Manager | 创建、查询、续租和销毁 Sandbox，返回 Worker 连接信息 |
| Sandbox Worker | 托管 Agent Runtime，提供稳定的 RPC、事件流和短期事件恢复接口 |
| Sandbox Runtime | 使用 `runsc` 执行 Agent，并落实资源、文件和网络隔离 |
| MCP Gateway | 独立提供和治理 MCP Server、Tool 与 App |

## 模型选择

Gateway 是公共模型目录的唯一来源，通过 `/v1/models` 发布可用的公共模型 ID。Open WebUI
展示该目录，并在 Responses 请求的 `model` 字段中发送用户选择。Gateway 校验并解析模型，
Adapter 将解析后的 Codex 模型传给 Agent Runtime；对外 Response 保持公共模型 ID。

## 权限模型

| 主体 | 必需权限 | 禁止权限 |
|---|---|---|
| Gateway | 调用 Adapter | 访问 Manager、Worker 或 Sandbox Runtime |
| Adapter | 调用 Manager 生命周期接口；连接 Worker | 控制底层运行平台 |
| Sandbox Manager | 管理 Sandbox Worker 工作负载 | 代理 Agent RPC；访问业务入口 |
| Sandbox Worker | 访问工作区、策略代理和明确允许的内部接口 | 访问 Adapter、Manager 或运行平台凭证 |
| egress-proxy | 访问允许的外部目标 | 接受非 Agent 网络来源或转发未授权目标 |

Gateway、Adapter、Manager 和 Worker 使用相互独立的部署凭证。每个 Worker 使用独立、
有生命周期约束的连接凭证。运行平台授予 Manager 的权限应限制在 Sandbox 工作负载范围；
无法细分权限的平台必须通过专用节点或等价隔离降低影响面。

## 网络控制

部署环境必须提供以下逻辑网络域，具体实现可以是容器网络、网络命名空间或编排平台网络策略：

| 网络域 | 成员 | 约束 |
|---|---|---|
| control | Open WebUI、Gateway、Adapter、Sandbox Manager、同栈 BFF | 普通工作负载网络，使用平台默认外网能力 |
| agent-rpc | Adapter、Sandbox Worker | 仅允许 Adapter 向 Worker 发起 RPC/SSE 连接 |
| agent-egress | Worker、DNS、egress-proxy、获准内部接口 | Agent 无默认互联网路由 |
| egress-uplink | egress-proxy | 提供策略代理的外部出口 |

必须允许 Adapter→Worker 的新连接及其返回流量，并拒绝 Worker→Adapter/Manager 的新连接。
Agent 的互联网访问默认拒绝，只允许经过 egress-proxy；访问 MCP 或本地模型等内部接口也
必须按服务身份和端口显式允许。Agent 网络成员变更是运行平台授权操作，只有 Manager 可附加
受管 Worker，部署方可附加明确声明的基础设施和内部服务。所有内部调用使用 DNS 服务名，
不在配置中固定 IP。

## 运行环境要求

Gateway、Adapter、Sandbox Manager 和 Sandbox Worker 必须使用独立、可单独发布的运行制品，
每个制品只包含自身职责所需的程序和依赖。构建过程可以共享基础环境与缓存，但不得合并最终
运行制品。

部署平台必须支持：

- 为不同工作负载选择 OCI runtime；
- 普通工作负载使用默认 `runc`，Sandbox Worker 显式使用 `runsc`；
- 动态创建、健康检查、续租和销毁 Worker；
- 工作负载级 DNS、方向性网络策略、Secret 和持久卷；
- CPU、内存、PID、临时文件系统和执行时长限制。

Sandbox Worker 固定为非 root、只读根文件系统、cap-drop all、`no-new-privileges`，
仅工作区和 Agent Runtime 状态目录可写。部署凭证以只读 Secret 注入，不写入镜像。

## 生命周期

新 Response 由 Adapter 请求 Manager 创建 Sandbox。Manager 在 Worker 健康后返回 Sandbox
ID、Worker 服务名、独立凭证和租约期限。Adapter 直接连接 Worker，并在执行期间续租。

`previous_response_id` 复用同一 Sandbox 和 Agent 会话。客户端 SSE 断开不取消 Agent；
Adapter 继续消费 Worker 事件。取消先调用 Worker 的中断接口，失败时请求 Manager 销毁
Sandbox。运行中的 Agent 执行和 MCP App 交互续租；终态 Response 不无限占用 Sandbox。
`previous_response_id` 或 AppSession 调用前必须确认并续租，Sandbox 已过期时返回
`sandbox_unavailable`，不重新创建或静默切换 Sandbox。

## Agent Runtime 约束

Sandbox Worker 必须让 Agent Runtime 明确接受外层 Sandbox 的权限和网络边界，不得创建
与 `runsc` 不兼容的内层 Sandbox。需要审批或权限提升的操作必须 fail closed。具体 Runtime
使用的字段、方法和取值由实现级协议定义。

## MCP Apps 与状态

Adapter 将带 App 上下文的 Agent Runtime tool event 映射为标准 `mcp_call`，并绑定：

```text
response_id + origin_call_id + app_id + server_id + resource_uri + allowed_tools
```

浏览器中的 AppBridge 经 BFF 调用 Adapter，不直接访问内部模块。Response、AppSession 和
MCP Apps side-event 保存在 Adapter 单进程内存中，不提供多实例恢复。AppSession 的保留
不延长 Sandbox 租约；Sandbox 已回收时，后续资源或工具调用 fail closed。

## 协议约束

- 客户端非空 `tools`：拒绝。
- `background=true`、`store=false`、`max_output_tokens`：拒绝。
- 主 Responses SSE 断线续传：不支持，仅允许 retrieve。
- MCP App elicitation：支持；其他交互式请求：fail closed。
- 终端用户与租户管理、MCP Gateway 内部实现：不包含。
