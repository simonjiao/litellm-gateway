# Responses Gateway 设计

本文定义 Responses Gateway 的组件职责、控制边界和运行不变量。

## 定位

LiteLLM 提供 Responses 入口、模型路由、认证和通用治理；Responses Adapter 负责
Responses 协议与 Sandbox Worker 接口之间的转换；Sandbox Manager 是可信执行控制面，
管理 Sandbox、Workspace 及受控文件操作，但不代理 Agent 数据流或文件字节流。

## 架构

```text
Browser
  │
  ▼
Open WebUI / 同源 BFF（Backend for Frontend）
  ├── Responses HTTP/SSE → LiteLLM Gateway → Responses Adapter ── RPC/SSE ─→ Worker (runsc)
  │                                                  │                         ├── /workspace/{uploads,work,outputs}
  │                                                  │ control                 ├── policy egress → Internet
  │                                                  ▼                         └── approved MCP / local model
  │                                            Sandbox Manager
  │                                              ├── Worker lifecycle
  │                                              ├── Workspace lifecycle
  │                                              └── one-shot operation (runc)
  │                                                    ├── one Workspace volume
  │                                                    └── Files / snapshot repository
  └── Files API → private object storage
```

控制面与数据面分离：Adapter 直接连接 Worker；文件传输和 Workspace 快照由 Manager
启动的可信一次性任务完成。Sandbox 不接收 Open WebUI、对象存储、Manager 或运行平台凭证。

## 职责

| 模块 | 职责 |
|---|---|
| Open WebUI / BFF | 用户认证、会话与文件访问控制；模型选择；上传、下载和生成物发布入口 |
| LiteLLM | Responses 入口、模型目录与路由、部署认证和治理 |
| Adapter | Responses 请求与事件映射、Worker RPC/SSE；调用 Manager 并转交 BFF 文件操作授权 |
| Sandbox Manager | Sandbox 生命周期、Workspace 生命周期、受控文件操作编排和操作状态恢复 |
| Sandbox Worker | 托管 Agent Runtime，只访问挂载给自己的 Workspace 和获准网络接口 |
| 一次性任务 | 在最小挂载和短期凭证下执行 checkout、publish、checkpoint、restore 或 retire |
| Open WebUI Files | 管理上传文件与已发布生成物，提供稳定 `file_id` 和授权下载入口 |
| 对象存储 | 保存 Open WebUI 文件对象及 Workspace 快照；不同用途使用独立 bucket/prefix 与凭证 |
| MCP Gateway | 独立提供和治理 MCP Server、Tool 与 App |

## Sandbox Manager 能力

Manager 的能力按接口和权限分为三组，但保持为一个控制面服务，不拆成多个微服务。详细状态、
约束和失败处理见 [Sandbox Manager 设计](sandbox-manager.md)。

### Sandbox 生命周期

- 创建、查询、续租和销毁 Worker，并管理健康检查、连接信息、独立凭证和执行 TTL；
- 为 Worker 设置 `runsc`、资源限制、网络域、Secret，并挂载请求指定的可恢复 Workspace；未指定
  时创建实例级临时 Workspace；
- 在取消、过期或异常退出后回收 Worker，并在 Manager 重启后对账受管资源。

### Workspace 生命周期

- 用 `workspace_id` 标识可恢复数据，用 `sandbox_id` 标识可销毁计算实例；
- 创建并挂载活动 Workspace，跟踪本地代次、远端 revision、租约和正在进行的操作；
- 执行 detach、checkpoint、restore 和延迟本地清理；restore 必须写入新的空卷并在校验成功后
  才启动 Worker；
- checkpoint 失败时保留唯一的本地副本并重试，只有已提交远端 revision 且满足保留条件时才
  删除本地卷。

### 受控文件操作

- 接收 BFF 已完成业务授权的操作授权，校验操作、`sandbox_id`、`workspace_id`、`turn_id`、
  精确对象或路径、大小、有效期和单次 nonce 的绑定；
- 可信控制面分配 `/workspace/uploads/<turn_id>`、`work` 和 `outputs/<turn_id>`；Agent 默认在
  `work` 中运行，只有当前 Turn 的 `outputs` 文件可发布；
- 当前消息被接受后，checkout 在本轮 Agent 任务提交前批量暂存、校验并原子提交；publish 只由
  已认证的上层请求显式触发，先固化只读快照，再上传并附加下载链接；
- 启动最多挂载一个 Workspace 的一次性可信任务，并持久记录操作状态、幂等键和提交结果，
  使重启后可以对账；不通过目录监听自动发布文件。

Manager 不判断用户、会话或对话是否有权访问 `file_id`；这是 Open WebUI/BFF 的业务授权职责。
`file_id` 本身不是凭证。Manager 也不转发文件字节、Agent RPC/SSE，且不向 Sandbox 下发
对象存储或运行平台凭证。

## 模型选择

Gateway 是公共模型目录的唯一来源，通过 `/v1/models` 发布可用的公共模型 ID。Open WebUI
展示该目录，并在 Responses 请求的 `model` 字段中发送用户选择。Gateway 校验并解析模型，
Adapter 将解析后的 Codex 模型传给 Agent Runtime；对外 Response 保持公共模型 ID。

## 文件与 Workspace 存储

存储按数据语义分层，详细流程见 [文件与 Workspace 存储](storage.md)：

| 数据 | 权威存储 | 说明 |
|---|---|---|
| 用户、对话、笔记、权限、文件元数据 | Open WebUI 数据库 | 配置 S3 不会把笔记和对话改存为对象 |
| 用户上传与已发布生成物 | Open WebUI Files + 私有对象存储 | 通过应用鉴权的稳定链接下载 |
| 活动 Workspace | 本地 POSIX 卷 | 低延迟读写；仅挂载给对应 Worker 或一次性任务 |
| Workspace revision | 对象存储中的 restic 仓库 | 后台增量 checkpoint 和按需 restore |
| Workspace/operation 控制状态 | Manager 持久数据库 | 记录本地代次、远端 head、租约与操作状态 |

对象存储无需由浏览器或 Sandbox 直接访问，可以只提供内网 HTTP；生产环境仍应在反向代理、
服务网格或对象存储端启用 TLS。其他系统可使用自己的 bucket/prefix 和凭证接入同一对象存储，
但不能复用 Open WebUI 的业务授权。

## 对话与 Workspace 绑定

Open WebUI/BFF 在受保护的映射表中维护 `chat_id → workspace_id`。对话首次执行 Agent 时创建
随机、可恢复的 Workspace；后续 Sandbox 复用或恢复它。没有已认证对话上下文的请求使用实例级
临时 Workspace。共享对话的访问跟随 Open WebUI ACL，克隆或分叉的对话默认创建新 Workspace。

浏览器不能指定或修改 `workspace_id`。BFF 根据对话 ACL 取得映射并签发短期操作授权；Manager
只处理不透明 `workspace_id`，不保存用户或对话 ACL。删除对话时解除映射，停止活动租约，并按
保留策略延迟清理本地卷和远端 revision。

## 权限模型

| 主体 | 必需权限 | 禁止权限 |
|---|---|---|
| Gateway | 调用 Adapter | 访问 Manager、Worker 或运行平台 |
| Open WebUI / BFF | 校验用户/对话/文件权限；签发短期、单次操作授权 | 控制 Worker 或向 Sandbox 暴露存储凭证 |
| Adapter | 调用 Manager 控制接口；连接 Worker | 控制底层运行平台或自行授予文件权限 |
| Sandbox Manager | 管理受管 Worker、Workspace 卷和一次性任务；验证操作授权 | 判断业务 ACL；代理 Agent 或文件数据面 |
| 一次性任务 | 按需访问一个 Workspace 和一次操作所需端点 | 访问其他 Workspace、长期凭证或通用运行平台接口 |
| Sandbox Worker | 访问自己的 Workspace、策略代理和明确允许的内部接口 | 访问 Files、对象存储、Adapter、Manager 或运行平台凭证 |
| egress-proxy | 访问允许的外部目标 | 接受非 Agent 网络来源或转发未授权目标 |

各服务使用独立部署凭证。操作授权必须短期、单次使用并绑定操作范围；底层平台授权应限制在
受管资源，无法细分权限时必须使用专用节点或等价隔离降低影响面。

## 网络控制

部署环境必须提供以下逻辑网络域；具体实现可以是容器网络、网络命名空间或编排平台网络策略：

| 网络域 | 成员 | 约束 |
|---|---|---|
| control | Open WebUI、Gateway、Adapter、Manager、同源 BFF | 普通工作负载网络 |
| agent-rpc | Adapter、Worker | 仅允许 Adapter 向 Worker 发起 RPC/SSE 连接 |
| agent-egress | Worker、DNS、egress-proxy、获准内部接口 | Worker 无默认互联网路由 |
| storage | Open WebUI Files、对象存储、受控一次性任务 | Worker 不加入；按操作限制访问方向和端点 |
| egress-uplink | egress-proxy | 提供策略代理的外部出口 |

必须允许 Adapter→Worker 的新连接及其返回流量，并拒绝 Worker→Adapter/Manager/Files/对象
存储的新连接。Agent 互联网访问默认拒绝，只允许经过 egress-proxy；MCP 或本地模型等内部
接口也必须按服务身份和端口显式允许。所有内部调用使用 DNS 服务名，不在配置中固定 IP。

## 运行环境要求

Gateway、Adapter、Manager 和 Worker 使用独立、可单独发布的运行制品。构建可以共享基础环境
与缓存，但不得合并最终运行制品。部署平台必须支持：

- 普通服务和一次性任务使用 `runc`，Worker 显式使用 `runsc`；
- 动态创建、健康检查、续租和销毁 Worker；
- Workspace 卷的创建、挂载、卸载和延迟删除；
- 工作负载级 DNS、方向性网络策略、Secret、持久卷和资源限制；
- Manager 控制状态的持久化，以及重启后的资源和操作对账。

Worker 固定为非 root、只读根文件系统、cap-drop all、`no-new-privileges`；Workspace 顶层和
上传目录由可信控制面管理，Agent 只写 `work`、当前 Turn 的 `outputs`、Runtime 状态和临时目录。
部署凭证以只读 Secret 注入，不写入镜像。

## 生命周期

新 Response 由 Adapter 请求 Manager 创建 Sandbox。Manager 挂载指定的可恢复 Workspace，或
创建临时 Workspace；在 Worker 健康后返回 `sandbox_id`、连接地址、独立凭证和租约期限。
Adapter 直接连接 Worker，并在执行期间续租。

`previous_response_id` 复用同一 Sandbox 和 Agent 会话。客户端 SSE 断开不取消 Agent；
Adapter 继续消费 Worker 事件。取消先调用 Worker 中断接口，失败时请求 Manager 销毁
Sandbox。Sandbox 过期时返回 `sandbox_unavailable`，不静默切换实例。

销毁 Sandbox 只回收计算实例，不等于删除可恢复 Workspace。需要持久化的 Workspace 在停止
写入后进入后台 checkpoint，成功提交远端 revision 后按保留策略延迟清理本地卷；再次使用时
restore 到新卷。临时 Workspace 可随 Sandbox 回收。只保存 `/workspace`，不保存进程、内存、
临时运行状态或 Secret。

## Agent Runtime 约束

Worker 必须让 Agent Runtime 明确接受外层 Sandbox 的权限和网络边界，不得创建与 `runsc`
不兼容的内层 Sandbox。默认工作目录为 `/workspace/work`；输入目录和当前输出目录由可信控制面
注入，Agent 不得自行选择 `turn_id`。需要审批或权限提升的操作必须 fail closed。具体 Runtime
字段、方法和取值由实现级协议定义。

## MCP Apps 与状态

Adapter 将带 App 上下文的 Agent Runtime tool event 映射为标准 `mcp_call`，并绑定：

```text
response_id + origin_call_id + app_id + server_id + resource_uri + allowed_tools
```

浏览器中的 AppBridge 经 BFF 调用 Adapter，不直接访问内部模块。Response、AppSession 和
MCP Apps side-event 保存在 Adapter 单进程内存中，不提供多实例恢复。AppSession 的保留不
延长 Sandbox 租约；Sandbox 已回收时，后续资源或工具调用 fail closed。

## 协议约束

- 客户端非空 `tools`：拒绝。
- `background=true`、`store=false`、`max_output_tokens`：拒绝。
- 主 Responses SSE 断线续传：不支持，仅允许 retrieve。
- MCP App elicitation：支持；其他交互式请求：fail closed。
- 终端用户与租户管理、MCP Gateway 内部实现：不包含。
